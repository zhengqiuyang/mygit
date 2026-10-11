"""mygit M1 的单元测试:python test_m1.py -v"""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MYGIT = os.path.join(HERE, "mygit.py")


def run(*args, cwd, check=True, text=True):
    return subprocess.run(
        [sys.executable, MYGIT, *args],
        cwd=cwd, capture_output=True, text=text, check=check,
    )


class Milestone1Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = tmp.name

    def test_init_creates_object_dir(self):
        result = run("init", cwd=self.cwd)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(
            os.path.isdir(os.path.join(self.cwd, ".mygit", "objects"))
        )

    def test_hash_object_matches_git_format(self):
        run("init", cwd=self.cwd)
        content = b"hello token-forge\n"
        path = os.path.join(self.cwd, "hello.txt")
        with open(path, "wb") as f:
            f.write(content)

        sha = run("hash-object", path, cwd=self.cwd).stdout.strip()

        expected = hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()
        self.assertEqual(sha, expected)
        # 对象确实按 loose object 布局落盘
        objpath = os.path.join(self.cwd, ".mygit", "objects", sha[:2], sha[2:])
        self.assertTrue(os.path.isfile(objpath))

    def test_cat_file_roundtrip(self):
        run("init", cwd=self.cwd)
        content = b"roundtrip payload \x00\x01\x02\n"
        path = os.path.join(self.cwd, "data.bin")
        with open(path, "wb") as f:
            f.write(content)
        sha = run("hash-object", path, cwd=self.cwd).stdout.strip()

        result = run("cat-file", sha, cwd=self.cwd, text=False)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, content)

    def test_cat_file_missing_object_fails(self):
        run("init", cwd=self.cwd)
        bogus = "0" * 40
        result = run("cat-file", bogus, cwd=self.cwd, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
