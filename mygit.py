#!/usr/bin/env python3
"""mygit — 从零实现的迷你 git。

M1 里程碑:对象库。blob 对象的哈希与磁盘布局与真 git 完全兼容,
可以用 `git hash-object <file>` 对拍验证。
"""
import argparse
import hashlib
import os
import sys
import zlib

GIT_DIR = ".mygit"


def repo_root():
    """从当前目录向上找 .mygit,返回仓库根目录。"""
    cwd = os.getcwd()
    while True:
        if os.path.isdir(os.path.join(cwd, GIT_DIR)):
            return cwd
        parent = os.path.dirname(cwd)
        if parent == cwd:
            sys.exit("mygit: not a mygit repository (or any parent directory)")
        cwd = parent


def object_path(sha):
    root = repo_root()
    return os.path.join(root, GIT_DIR, "objects", sha[:2], sha[2:])


def write_object(data, otype="blob"):
    """按 git 的 loose object 格式落盘:<type> <size>\\0<data>,zlib 压缩。"""
    full = f"{otype} {len(data)}".encode() + b"\0" + data
    sha = hashlib.sha1(full).hexdigest()
    path = object_path(sha)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(zlib.compress(full))
    return sha


def read_object(sha):
    path = object_path(sha)
    if not os.path.exists(path):
        sys.exit(f"mygit: {sha[:12]}: object not found")
    with open(path, "rb") as f:
        raw = zlib.decompress(f.read())
    header, _, body = raw.partition(b"\0")
    otype, size = header.decode().split()
    if int(size) != len(body):
        sys.exit(f"mygit: {sha[:12]}: corrupt object (size mismatch)")
    return otype, body


def cmd_init(args):
    existed = os.path.isdir(GIT_DIR)
    os.makedirs(os.path.join(GIT_DIR, "objects"), exist_ok=True)
    word = "Reinitialized existing" if existed else "Initialized empty"
    print(f"{word} mygit repository in {os.path.abspath(GIT_DIR)}")


def cmd_hash_object(args):
    repo_root()  # 必须在仓库内
    with open(args.file, "rb") as f:
        data = f.read()
    print(write_object(data))


def cmd_cat_file(args):
    otype, body = read_object(args.object)
    if args.type and otype != args.type:
        sys.exit(f"mygit: {args.object[:12]}: object is a {otype}, not a {args.type}")
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def main():
    parser = argparse.ArgumentParser(prog="mygit", description="a miniature git")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create an empty repository")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("hash-object", help="compute and store a file as a blob")
    p.add_argument("file")
    p.set_defaults(fn=cmd_hash_object)

    p = sub.add_parser("cat-file", help="print an object's contents")
    p.add_argument("object")
    p.add_argument("-t", dest="type", choices=["blob"], help="expected type")
    p.set_defaults(fn=cmd_cat_file)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
