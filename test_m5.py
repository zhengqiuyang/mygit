"""mygit M5 的单元测试:python test_m5.py -v

覆盖:gc 打包后仓库功能完整(log/cat-file/新提交混读 loose+pack)、
pack 文件被真 git verify-pack 校验通过,以及终极对拍——
把 .mygit 改名 .git 后,真 git 可以直接遍历 mygit 的提交历史。
"""
import glob
import os
import shutil
import tempfile
import unittest

from test_m2 import FIXED_DATES, git_run, run, write_file


class Milestone5Tests(unittest.TestCase):
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

    def test_gc_then_repo_still_works(self):
        self.commit("a.txt", b"one\n", "first")
        self.commit("dir/b.txt", b"two\n", "second")
        run("tag", "-a", "v1", "-m", "note", cwd=self.cwd,
            env_extra=FIXED_DATES)
        gc = run("gc", cwd=self.cwd)
        self.assertEqual(gc.returncode, 0)
        # loose 对象已清空,只剩 pack
        leftovers = [p for p in glob.glob(os.path.join(self.cwd, ".mygit",
                                                       "objects", "*", "*"))
                     if os.sep + "pack" + os.sep not in p]
        self.assertEqual(leftovers, [])
        self.assertTrue(glob.glob(os.path.join(self.cwd, ".mygit", "objects",
                                               "pack", "pack-*.pack")))
        # 全部命令在 pack-only 状态下工作正常
        log = run("log", cwd=self.cwd).stdout
        self.assertIn("second", log)
        self.assertIn("first", log)
        head_tree = run("write-tree", cwd=self.cwd).stdout.strip()
        self.assertRegex(head_tree, r"^[0-9a-f]{40}$")
        # gc 后继续提交:loose 与 pack 混读
        self.commit("c.txt", b"three\n", "third")
        log = run("log", cwd=self.cwd).stdout
        self.assertIn("third", log)
        self.assertIn("first", log)

    def test_gc_empty_repo(self):
        result = run("gc", cwd=self.cwd)
        self.assertEqual(result.returncode, 0)
        self.assertIn("nothing to pack", result.stdout)

    def test_cat_file_from_pack(self):
        self.commit("a.txt", b"payload\n", "first")
        sha = None
        with open(os.path.join(self.cwd, ".mygit", "index"), encoding="utf-8") as f:
            for line in f:
                sha = line.split("\t")[1]
        run("gc", cwd=self.cwd)
        body = run("cat-file", sha, cwd=self.cwd, text=False)
        self.assertEqual(body.stdout, b"payload\n")

    def test_real_git_verifies_mygit_pack(self):
        self.commit("a.txt", b"one\n", "first")
        self.commit("b.txt", b"two\n", "second")
        run("gc", cwd=self.cwd)
        pack_idx = glob.glob(os.path.join(self.cwd, ".mygit", "objects",
                                          "pack", "pack-*.idx"))[0]
        # 真 git 原地校验我们的 idx/pack(crc32、校验和、fanout 全查)
        result = git_run("verify-pack", "-v", pack_idx, cwd=self.cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(": ok", result.stdout)
        self.assertIn("non delta: 6 objects", result.stdout)

    def test_real_git_reads_packed_mygit_repo(self):
        """终极对拍:.mygit 改名 .git 后,真 git 直接读 mygit 的仓库。"""
        self.commit("a.txt", b"one\n", "first")
        later = dict(FIXED_DATES)
        later["GIT_AUTHOR_DATE"] = "@1760009999 +0800"
        later["GIT_COMMITTER_DATE"] = "@1760009999 +0800"
        self.commit("b.txt", b"two\n", "second", dates=later)
        run("tag", "-a", "v1", "-m", "note", cwd=self.cwd,
            env_extra=FIXED_DATES)
        run("gc", cwd=self.cwd)
        mygit_head = self.read_ref("refs", "heads", "main")

        with tempfile.TemporaryDirectory() as other:
            shutil.copytree(self.cwd, os.path.join(other, "repo"))
            os.rename(os.path.join(other, "repo", ".mygit"),
                      os.path.join(other, "repo", ".git"))
            repo = os.path.join(other, "repo")
            # HEAD 指向同一提交
            self.assertEqual(git_run("rev-parse", "HEAD", cwd=repo).stdout.strip(),
                             mygit_head)
            # 能遍历完整历史
            log = git_run("log", "--oneline", cwd=repo)
            self.assertEqual(log.returncode, 0, log.stderr)
            self.assertIn("second", log.stdout)
            self.assertIn("first", log.stdout)
            # 能解引用我们的附注标签
            tag = git_run("cat-file", "-t", "v1", cwd=repo)
            self.assertEqual(tag.stdout.strip(), "tag")
            # 能读出具体文件内容
            show = git_run("show", "HEAD:a.txt", cwd=repo)
            self.assertEqual(show.stdout, "one\n")


if __name__ == "__main__":
    unittest.main()
