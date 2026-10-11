"""mygit M2 的单元测试:python test_m2.py -v

覆盖:index 读写、tree 对象格式(与真 git 逐字节一致)、commit 对象格式、
以及与真 git 的 SHA 对拍(固定作者与日期时 commit SHA 必须相同)。
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MYGIT = os.path.join(HERE, "mygit.py")

FIXED_DATES = {
    "GIT_AUTHOR_DATE": "@1760000000 +0800",
    "GIT_COMMITTER_DATE": "@1760000000 +0800",
}


def run(*args, cwd, env_extra=None, check=True, text=True):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, MYGIT, *args],
        cwd=cwd, capture_output=True, text=text, check=check, env=env,
    )


def git_run(*args, cwd, env_extra=None):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=env,
    )


def obj_sha(otype, payload):
    """按 git 对象格式计算 SHA,供测试独立复核(不依赖被测代码)。"""
    full = f"{otype} {len(payload)}".encode() + b"\0" + payload
    return hashlib.sha1(full).hexdigest()


def tree_payload(entries):
    """entries: [(mode, name, sha_hex)],拼出 tree 对象的原始字节。"""
    payload = b""
    for mode, name, sha in entries:
        payload += f"{mode} {name}".encode() + b"\0" + bytes.fromhex(sha)
    return payload


def write_file(cwd, relpath, content):
    path = os.path.join(cwd, *relpath.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


class Milestone2Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = tmp.name
        run("init", cwd=self.cwd)

    def read_index(self):
        entries = {}
        with open(os.path.join(self.cwd, ".mygit", "index"), encoding="utf-8") as f:
            for line in f:
                mode, sha, path = line.rstrip("\n").split("\t")
                entries[path] = (mode, sha)
        return entries

    def test_add_builds_index(self):
        write_file(self.cwd, "a.txt", b"alpha\n")
        result = run("add", "a.txt", cwd=self.cwd)
        self.assertEqual(result.returncode, 0)
        entries = self.read_index()
        self.assertIn("a.txt", entries)
        mode, sha = entries["a.txt"]
        self.assertEqual(mode, "100644")
        self.assertEqual(sha, obj_sha("blob", b"alpha\n"))

    def test_add_directory_recurses(self):
        write_file(self.cwd, "src/main.py", b"print('hi')\n")
        write_file(self.cwd, "src/util.py", b"pass\n")
        run("add", "src", cwd=self.cwd)
        entries = self.read_index()
        self.assertEqual(set(entries), {"src/main.py", "src/util.py"})

    def test_add_missing_path_fails(self):
        result = run("add", "nope.txt", cwd=self.cwd, check=False)
        self.assertNotEqual(result.returncode, 0)

    def test_write_tree_matches_git_format(self):
        blob_a = obj_sha("blob", b"alpha\n")
        blob_g = obj_sha("blob", b"gamma\n")
        inner = obj_sha("tree", tree_payload([("100644", "c.txt", blob_g)]))
        root = obj_sha("tree", tree_payload(
            [("100644", "a.txt", blob_a), ("40000", "b", inner)]
        ))
        write_file(self.cwd, "a.txt", b"alpha\n")
        write_file(self.cwd, "b/c.txt", b"gamma\n")
        run("add", ".", cwd=self.cwd)
        result = run("write-tree", cwd=self.cwd)
        self.assertEqual(result.stdout.strip(), root)

    def test_commit_object_format(self):
        write_file(self.cwd, "a.txt", b"alpha\n")
        run("add", "a.txt", cwd=self.cwd)
        run("commit", "-m", "first", cwd=self.cwd, env_extra=FIXED_DATES)
        with open(os.path.join(self.cwd, ".mygit", "refs", "heads", "main"),
                  encoding="utf-8") as f:
            sha = f.read().strip()
        body = run("cat-file", sha, cwd=self.cwd, text=False).stdout.decode()
        lines = body.split("\n")
        self.assertTrue(lines[0].startswith("tree "))
        self.assertNotIn("parent", body)  # 根提交没有 parent
        author = [l for l in lines if l.startswith("author ")][0]
        # author Name <email> 1760000000 +0800
        self.assertTrue(author.endswith("1760000000 +0800"), author)
        self.assertRegex(author, r"^author .+ <.+> 1760000000 \+0800$")
        self.assertIn("\n\nfirst\n", body)

    def test_commit_sha_matches_real_git(self):
        """对拍:同样的内容、身份、日期,mygit 与真 git 的 commit SHA 必须一致。"""
        content = b"hello m2\n"
        # A:mygit
        write_file(self.cwd, "hello.txt", content)
        run("add", "hello.txt", cwd=self.cwd)
        run("commit", "-m", "first", cwd=self.cwd, env_extra=FIXED_DATES)
        with open(os.path.join(self.cwd, ".mygit", "refs", "heads", "main"),
                  encoding="utf-8") as f:
            mygit_sha = f.read().strip()
        # B:真 git
        with tempfile.TemporaryDirectory() as other:
            write_file(other, "hello.txt", content)
            git_run("init", "-b", "main", cwd=other)
            git_run("add", "hello.txt", cwd=other)
            commit = git_run("commit", "-m", "first", cwd=other, env_extra=FIXED_DATES)
            self.assertEqual(commit.returncode, 0, commit.stderr)
            git_sha = git_run("rev-parse", "HEAD", cwd=other).stdout.strip()
        self.assertEqual(mygit_sha, git_sha)

    def test_log_shows_history_newest_first(self):
        write_file(self.cwd, "a.txt", b"one\n")
        run("add", "a.txt", cwd=self.cwd)
        run("commit", "-m", "first", cwd=self.cwd, env_extra=FIXED_DATES)
        write_file(self.cwd, "a.txt", b"two\n")
        run("add", "a.txt", cwd=self.cwd)
        later = dict(FIXED_DATES)
        later["GIT_AUTHOR_DATE"] = "@1760009999 +0800"
        later["GIT_COMMITTER_DATE"] = "@1760009999 +0800"
        run("commit", "-m", "second", cwd=self.cwd, env_extra=later)
        result = run("log", cwd=self.cwd)
        out = result.stdout
        self.assertIn("second", out)
        self.assertIn("first", out)
        self.assertIn("Author:", out)
        self.assertLess(out.index("second"), out.index("first"))


if __name__ == "__main__":
    unittest.main()
