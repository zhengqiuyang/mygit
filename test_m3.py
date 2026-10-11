"""mygit M3 的单元测试:python test_m3.py -v

覆盖:branch 列表/创建/删除、checkout 切换分支并同步工作区、
脏 index 拒绝切换、轻量/附注标签,以及附注标签对象与真 git 的 SHA 对拍。
"""
import os
import tempfile
import unittest

from test_m2 import FIXED_DATES, HERE, git_run, run, write_file


class Milestone3Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = tmp.name
        run("init", cwd=self.cwd)

    def commit(self, path, content, message, dates=None):
        write_file(self.cwd, path, content)
        run("add", path, cwd=self.cwd)
        run("commit", "-m", message, cwd=self.cwd, env_extra=dates or FIXED_DATES)

    def read_ref(self, *parts):
        with open(os.path.join(self.cwd, ".mygit", *parts),
                  encoding="utf-8") as f:
            return f.read().strip()

    def path(self, rel):
        return os.path.join(self.cwd, *rel.split("/"))

    # ----- branch -----

    def test_branch_list_create_delete(self):
        self.commit("a.txt", b"one\n", "first")
        run("branch", "dev", cwd=self.cwd)
        listing = run("branch", cwd=self.cwd).stdout
        self.assertIn("* main", listing)
        self.assertIn("dev", listing)
        self.assertEqual(
            run("branch", "-d", "dev", cwd=self.cwd).returncode, 0)
        self.assertNotIn("dev", run("branch", cwd=self.cwd).stdout)

    def test_branch_delete_current_fails(self):
        self.commit("a.txt", b"one\n", "first")
        result = run("branch", "-d", "main", cwd=self.cwd, check=False)
        self.assertNotEqual(result.returncode, 0)

    def test_duplicate_branch_fails(self):
        self.commit("a.txt", b"one\n", "first")
        run("branch", "dev", cwd=self.cwd)
        result = run("branch", "dev", cwd=self.cwd, check=False)
        self.assertNotEqual(result.returncode, 0)

    def test_branch_without_commits_fails(self):
        result = run("branch", "dev", cwd=self.cwd, check=False)
        self.assertNotEqual(result.returncode, 0)

    # ----- checkout -----

    def test_checkout_switches_worktree(self):
        self.commit("a.txt", b"one\n", "first")
        run("checkout", "-b", "dev", cwd=self.cwd)
        self.commit("b.txt", b"two\n", "second")
        run("checkout", "main", cwd=self.cwd)
        self.assertFalse(os.path.exists(self.path("b.txt")))
        with open(self.path("a.txt"), "rb") as f:
            self.assertEqual(f.read(), b"one\n")
        run("checkout", "dev", cwd=self.cwd)
        with open(self.path("b.txt"), "rb") as f:
            self.assertEqual(f.read(), b"two\n")

    def test_checkout_restores_deleted_tracked_file(self):
        self.commit("a.txt", b"one\n", "first")
        os.remove(self.path("a.txt"))
        # 已在目标分支时直接返回,不做恢复(与真 git 的 Already on 行为一致);
        # 切走再切回则会按目标树把文件补回来。
        run("checkout", "main", cwd=self.cwd)
        run("branch", "dev", cwd=self.cwd)
        run("checkout", "dev", cwd=self.cwd)
        run("checkout", "main", cwd=self.cwd)
        with open(self.path("a.txt"), "rb") as f:
            self.assertEqual(f.read(), b"one\n")

    def test_checkout_missing_branch_fails(self):
        self.commit("a.txt", b"one\n", "first")
        result = run("checkout", "nope", cwd=self.cwd, check=False)
        self.assertNotEqual(result.returncode, 0)

    def test_checkout_refuses_staged_changes(self):
        self.commit("a.txt", b"one\n", "first")
        run("branch", "dev", cwd=self.cwd)
        write_file(self.cwd, "a.txt", b"dirty\n")
        run("add", "a.txt", cwd=self.cwd)
        result = run("checkout", "dev", cwd=self.cwd, check=False)
        self.assertNotEqual(result.returncode, 0)
        with open(self.path("a.txt"), "rb") as f:
            self.assertEqual(f.read(), b"dirty\n")  # 工作区未被破坏

    # ----- tag -----

    def test_lightweight_tag_points_at_commit(self):
        self.commit("a.txt", b"one\n", "first")
        run("tag", "v0", cwd=self.cwd)
        self.assertEqual(self.read_ref("refs", "tags", "v0"),
                         self.read_ref("refs", "heads", "main"))
        self.assertIn("v0", run("tag", cwd=self.cwd).stdout)

    def test_duplicate_tag_fails(self):
        self.commit("a.txt", b"one\n", "first")
        run("tag", "v0", cwd=self.cwd)
        result = run("tag", "v0", cwd=self.cwd, check=False)
        self.assertNotEqual(result.returncode, 0)

    def test_annotated_tag_object_format(self):
        self.commit("a.txt", b"one\n", "first")
        run("tag", "-a", "v1", "-m", "release", cwd=self.cwd,
            env_extra=FIXED_DATES)
        tag_sha = self.read_ref("refs", "tags", "v1")
        commit_sha = self.read_ref("refs", "heads", "main")
        self.assertNotEqual(tag_sha, commit_sha)
        body = run("cat-file", tag_sha, cwd=self.cwd, text=False)
        self.assertEqual(body.stdout.decode(),
                         f"object {commit_sha}\ntype commit\ntag v1\n"
                         f"tagger {_tagger_line()}\n\nrelease\n")

    def test_annotated_tag_sha_matches_real_git(self):
        """对拍:同样的提交内容、身份、日期与说明,
        mygit 与真 git 的附注标签对象 SHA 必须一致。"""
        content = b"tagged\n"
        self.commit("t.txt", content, "first")
        run("tag", "-a", "v1", "-m", "note", cwd=self.cwd,
            env_extra=FIXED_DATES)
        mygit_tag = self.read_ref("refs", "tags", "v1")
        with tempfile.TemporaryDirectory() as other:
            write_file(other, "t.txt", content)
            git_run("init", "-b", "main", cwd=other)
            git_run("add", "t.txt", cwd=other)
            git_run("commit", "-m", "first", cwd=other, env_extra=FIXED_DATES)
            tagged = git_run("tag", "-a", "v1", "-m", "note", cwd=other,
                             env_extra=FIXED_DATES)
            self.assertEqual(tagged.returncode, 0, tagged.stderr)
            git_tag = git_run("rev-parse", "v1", cwd=other).stdout.strip()
        self.assertEqual(mygit_tag, git_tag)


def _tagger_line():
    """从 git 拿身份,拼出期望的 tagger 行(时间戳取 FIXED_DATES)。"""
    name = git_run("config", "--get", "user.name", cwd=HERE).stdout.strip()
    email = git_run("config", "--get", "user.email", cwd=HERE).stdout.strip()
    return f"{name} <{email}> 1760000000 +0800"


if __name__ == "__main__":
    unittest.main()
