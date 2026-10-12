"""mygit M4 的单元测试:python test_m4.py -v

覆盖:Myers 最短编辑脚本的正确性与最小性、hunk 头行号计算、
diff(工作区/已暂存)与 status 的行为,以及与真 git diff 的输出对拍。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mygit import make_hunks, myers_ops  # noqa: E402
from test_m2 import FIXED_DATES, git_run, run, write_file  # noqa: E402


def replay(ops, a, b):
    """回放编辑脚本:'='/'-' 消耗 a 的行,'+' 输出新行,必须恰好重建出 b。"""
    out = []
    ia = 0
    for op, line in ops:
        if op in ("=", "-"):
            assert ia < len(a) and a[ia] == line, (op, line, ia, a)
            ia += 1
            if op == "=":
                out.append(line)
        else:
            out.append(line)
    assert ia == len(a), "a 未被消耗完"
    return out == b


class MyersTests(unittest.TestCase):
    def test_identical(self):
        ops = myers_ops(["a", "b"], ["a", "b"])
        self.assertEqual(ops, [("=", "a"), ("=", "b")])

    def test_pure_insert_and_delete(self):
        self.assertEqual(myers_ops(["x"], ["x", "y"]),
                         [("=", "x"), ("+", "y")])
        self.assertEqual(myers_ops(["x", "y"], ["x"]),
                         [("=", "x"), ("-", "y")])
        self.assertEqual(myers_ops([], ["n"]), [("+", "n")])
        self.assertEqual(myers_ops(["o"], []), [("-", "o")])

    def test_classic_example_is_minimal(self):
        # Myers 论文中的经典例子,最短编辑距离为 5
        a = list("ABCABBA")
        b = list("CBABAC")
        ops = myers_ops(a, b)
        self.assertTrue(replay(ops, a, b))
        self.assertEqual(sum(1 for op, _ in ops if op != "="), 5)

    def test_replay_on_longer_cases(self):
        cases = [
            (["l1", "l2", "l3", "l4"], ["l1", "lX", "l3"]),
            (["a"], ["b"]),
            (["k1", "k2", "k3", "k4", "k5"],
             ["k1", "k2", "n1", "k3", "k4", "k5", "n2"]),
        ]
        for a, b in cases:
            self.assertTrue(replay(myers_ops(a, b), a, b), (a, b))

    def test_hunk_ranges(self):
        # 在 5 行文件中部改 1 行,context=3 覆盖全文件
        a = ["1", "2", "3", "4", "5"]
        b = ["1", "2", "X", "4", "5"]
        hunks = make_hunks(myers_ops(a, b))
        self.assertEqual(len(hunks), 1)
        a0, a1, b0, b1, seg = hunks[0]
        self.assertEqual((a0, a1, b0, b1), (0, 5, 0, 5))
        self.assertIn(("-", "3"), seg)
        self.assertIn(("+", "X"), seg)


class DiffStatusTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = tmp.name
        run("init", cwd=self.cwd)

    def commit(self, path, content, message):
        write_file(self.cwd, path, content)
        run("add", path, cwd=self.cwd)
        run("commit", "-m", message, cwd=self.cwd, env_extra=FIXED_DATES)

    def path(self, rel):
        return os.path.join(self.cwd, *rel.split("/"))

    def test_diff_worktree_shows_unstaged(self):
        self.commit("f.txt", b"one\ntwo\nthree\n", "first")
        write_file(self.cwd, "f.txt", b"one\nTWO\nthree\n")
        out = run("diff", cwd=self.cwd).stdout
        self.assertIn("diff --git a/f.txt b/f.txt", out)
        self.assertIn("--- a/f.txt", out)
        self.assertIn("+++ b/f.txt", out)
        self.assertIn("@@ -1,3 +1,3 @@", out)
        self.assertIn("-two", out)
        self.assertIn("+TWO", out)
        run("add", "f.txt", cwd=self.cwd)
        self.assertEqual(run("diff", cwd=self.cwd).stdout, "")
        staged = run("diff", "--staged", cwd=self.cwd).stdout
        self.assertIn("+TWO", staged)

    def test_diff_staged_new_file(self):
        write_file(self.cwd, "n.txt", b"hello\n")
        run("add", "n.txt", cwd=self.cwd)
        out = run("diff", "--staged", cwd=self.cwd).stdout
        self.assertIn("new file mode 100644", out)
        self.assertIn("--- /dev/null", out)
        self.assertIn("+hello", out)

    def test_diff_worktree_deleted_file(self):
        self.commit("f.txt", b"data\n", "first")
        os.remove(self.path("f.txt"))
        out = run("diff", cwd=self.cwd).stdout
        self.assertIn("deleted file mode 100644", out)
        self.assertIn("-data", out)

    def test_status_sections(self):
        write_file(self.cwd, "u.txt", b"u\n")
        out = run("status", cwd=self.cwd).stdout
        self.assertIn("Untracked files:", out)
        self.assertIn("u.txt", out)

        self.commit("a.txt", b"v1\n", "first")
        write_file(self.cwd, "a.txt", b"v2\n")
        write_file(self.cwd, "b.txt", b"new\n")
        run("add", "b.txt", cwd=self.cwd)
        out = run("status", cwd=self.cwd).stdout
        self.assertIn("Changes to be committed:", out)
        self.assertIn("new file:", out)
        self.assertIn("b.txt", out)
        self.assertIn("Changes not staged for commit:", out)
        self.assertIn("modified:", out)
        self.assertIn("a.txt", out)

        os.remove(self.path("u.txt"))
        run("add", "a.txt", cwd=self.cwd)
        run("commit", "-m", "second", cwd=self.cwd, env_extra=FIXED_DATES)
        out = run("status", cwd=self.cwd).stdout
        self.assertIn("nothing to commit", out)

    def test_diff_output_matches_real_git(self):
        """对拍:同一份修改,mygit diff 与真 git diff 的正文完全一致。"""
        old = b"alpha\nbeta\ngamma\ndelta\nepsilon\n"
        new = b"alpha\nbeta\nGAMMA\ndelta\nepsilon\n"
        # A:mygit
        self.commit("f.txt", old, "first")
        write_file(self.cwd, "f.txt", new)
        mine = run("diff", cwd=self.cwd).stdout
        # B:真 git
        with tempfile.TemporaryDirectory() as other:
            write_file(other, "f.txt", old)
            git_run("init", "-q", "-b", "main", cwd=other)
            git_run("add", "f.txt", cwd=other)
            git_run("commit", "-q", "-m", "first", cwd=other,
                    env_extra=FIXED_DATES)
            write_file(other, "f.txt", new)
            real = git_run("diff", cwd=other).stdout
        keep = ("diff --git", "---", "+++", "@@", "+", "-")
        mine_lines = [l for l in mine.splitlines()
                      if l.startswith(keep) and not l.startswith("index ")]
        real_lines = [l for l in real.splitlines()
                      if l.startswith(keep) and not l.startswith("index ")]
        self.assertEqual(mine_lines, real_lines)


if __name__ == "__main__":
    unittest.main()
