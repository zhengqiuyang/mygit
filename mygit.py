#!/usr/bin/env python3
"""mygit — 从零实现的迷你 git。

M1 对象库:blob 的哈希与磁盘布局与真 git 完全兼容。
M2 暂存与提交:index(暂存区)/ write-tree(嵌套 tree 对象)/ commit / log,
tree 与 commit 对象同样与真 git 格式兼容——固定作者与日期时,
对同一内容会产出和真 git 完全相同的 commit SHA。
"""
import argparse
import hashlib
import os
import subprocess
import sys
import time
import zlib
from datetime import datetime, timedelta, timezone

GIT_DIR = ".mygit"


# ---------- 仓库与对象库(M1) ----------

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


def git_path(*parts):
    return os.path.join(repo_root(), GIT_DIR, *parts)


def object_path(sha):
    return git_path("objects", sha[:2], sha[2:])


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


# ---------- 引用:HEAD 与分支(M2) ----------

def head_branch():
    """HEAD 指向的分支名,如 'main';HEAD 不存在时返回 None。"""
    head_file = git_path("HEAD")
    if not os.path.exists(head_file):
        return None
    content = open(head_file, encoding="utf-8").read().strip()
    if not content.startswith("ref: refs/heads/"):
        return None  # 分离头指针,M2 暂不支持
    return content[len("ref: refs/heads/"):]


def head_commit():
    """当前分支指向的提交 SHA;还没有提交时返回 None。"""
    branch = head_branch()
    if branch is None:
        return None
    ref_file = git_path("refs", "heads", branch)
    if not os.path.exists(ref_file):
        return None
    with open(ref_file, encoding="utf-8") as f:
        return f.read().strip()


def update_head(sha):
    """把当前分支移到新提交上,返回分支名。"""
    head_file = git_path("HEAD")
    if not os.path.exists(head_file):
        with open(head_file, "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/main\n")
        branch = "main"
    else:
        branch = head_branch()
    ref_file = git_path("refs", "heads", branch)
    os.makedirs(os.path.dirname(ref_file), exist_ok=True)
    with open(ref_file, "w", encoding="utf-8") as f:
        f.write(sha + "\n")
    return branch


# ---------- 暂存区 index(M2) ----------
# 格式自定义(真 git 的 index 是二进制格式,属内部实现,无需对拍):
# 每行 mode<TAB>sha<TAB>path,按 path 排序,path 统一用 / 分隔。

def read_index():
    index_file = git_path("index")
    entries = {}
    if os.path.exists(index_file):
        with open(index_file, encoding="utf-8") as f:
            for line in f:
                mode, sha, path = line.rstrip("\n").split("\t")
                entries[path] = (mode, sha)
    return entries


def write_index(entries):
    index_file = git_path("index")
    with open(index_file, "w", encoding="utf-8") as f:
        for path in sorted(entries):
            mode, sha = entries[path]
            f.write(f"{mode}\t{sha}\t{path}\n")


def iter_worktree_files(top):
    """遍历 top 下的所有普通文件,(绝对路径, 相对仓库根的 posix 路径)。"""
    root = repo_root()
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [d for d in dirnames if d != GIT_DIR]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            yield full, rel


# ---------- tree 对象(M2) ----------
# tree 条目:<mode> <name>\\0 + 20 字节二进制 SHA,按 git 规则排序
# (目录名按追加 '/' 后参与比较,目录 mode 写作 "40000" 而非 "040000")。

def write_tree_from_index(entries):
    """把 index 内容递归组织成(可能嵌套的)tree 对象,返回根 tree 的 SHA。"""
    tree = {}
    for path, (mode, sha) in entries.items():
        parts = path.split("/")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = (mode, sha)

    def write_node(node):
        items = []
        for name, value in node.items():
            if isinstance(value, dict):
                items.append((name, "40000", write_node(value)))
            else:
                mode, sha = value
                items.append((name, mode, sha))
        items.sort(key=lambda it: it[0] + "/" if it[1] == "40000" else it[0])
        payload = b""
        for name, mode, sha in items:
            payload += f"{mode} {name}".encode() + b"\0" + bytes.fromhex(sha)
        return write_object(payload, "tree")

    return write_node(tree)


# ---------- commit 对象与身份(M2) ----------

def _config(key):
    """复用全局 git 的身份配置。"""
    result = subprocess.run(
        ["git", "config", "--get", key], capture_output=True, text=True
    )
    value = result.stdout.strip()
    if not value:
        sys.exit(f"mygit: 请先运行 git config {key} 完成配置")
    return value


def _local_tz(now):
    if time.daylight and time.localtime(now).tm_isdst:
        offset = -time.altzone
    else:
        offset = -time.timezone
    sign = "+" if offset >= 0 else "-"
    offset = abs(offset)
    return f"{sign}{offset // 3600:02d}{(offset % 3600) // 60:02d}"


def env_date(env_name):
    """读 GIT_AUTHOR_DATE / GIT_COMMITTER_DATE(支持 '@<unix> <tz>' 原始格式),
    缺省用当前时间与本机时区。"""
    raw = os.environ.get(env_name, "").strip().lstrip("@")
    if raw:
        try:
            ts, tz = raw.split()
            return int(ts), tz
        except ValueError:
            sys.exit(f"mygit: 无法解析 {env_name}={raw!r}(期望 '@<unix> <tz>')")
    now = time.time()
    return int(now), _local_tz(now)


def build_commit(tree_sha, parent, message):
    name, email = _config("user.name"), _config("user.email")
    lines = [f"tree {tree_sha}"]
    if parent:
        lines.append(f"parent {parent}")
    for kind in ("author", "committer"):
        ts, tz = env_date(f"GIT_{kind.upper()}_DATE")
        lines.append(f"{kind} {name} <{email}> {ts} {tz}")
    if not message.endswith("\n"):
        message += "\n"
    body = "\n".join(lines) + "\n\n" + message
    return write_object(body.encode("utf-8"), "commit")


def parse_commit(sha):
    otype, body = read_object(sha)
    if otype != "commit":
        sys.exit(f"mygit: {sha[:12]}: not a commit")
    text = body.decode("utf-8")
    header, _, message = text.partition("\n\n")
    info, parents = {}, []
    for line in header.splitlines():
        key, _, value = line.partition(" ")
        if key == "parent":
            parents.append(value)
        else:
            info[key] = value
    return info, parents, message


# ---------- 子命令 ----------

def cmd_init(args):
    existed = os.path.isdir(GIT_DIR)
    os.makedirs(os.path.join(GIT_DIR, "objects"), exist_ok=True)
    with open(os.path.join(GIT_DIR, "HEAD"), "w", encoding="utf-8") as f:
        f.write("ref: refs/heads/main\n")
    word = "Reinitialized existing" if existed else "Initialized empty"
    print(f"{word} mygit repository in {os.path.abspath(GIT_DIR)}")


def cmd_hash_object(args):
    repo_root()
    with open(args.file, "rb") as f:
        print(write_object(f.read()))


def cmd_cat_file(args):
    otype, body = read_object(args.object)
    if args.type and otype != args.type:
        sys.exit(f"mygit: {args.object[:12]}: object is a {otype}, not a {args.type}")
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def cmd_add(args):
    root = repo_root()
    entries = read_index()
    staged = []
    for target in args.files:
        full = os.path.normpath(os.path.join(root, target))
        if not os.path.exists(full):
            sys.exit(f"mygit: pathspec '{target}' did not match any files")
        if os.path.isfile(full):
            staged.append((full, os.path.relpath(full, root).replace(os.sep, "/")))
        else:
            staged.extend(iter_worktree_files(full))
    for path, rel in staged:
        with open(path, "rb") as f:
            entries[rel] = ("100644", write_object(f.read()))
    write_index(entries)
    print(f"staged {len(staged)} file(s)")


def cmd_write_tree(args):
    repo_root()
    entries = read_index()
    if not entries:
        sys.exit("mygit: empty index, nothing to write")
    print(write_tree_from_index(entries))


def cmd_commit(args):
    repo_root()
    entries = read_index()
    if not entries:
        sys.exit("mygit: nothing to commit (empty index)")
    tree_sha = write_tree_from_index(entries)
    parent = head_commit()
    sha = build_commit(tree_sha, parent, args.message)
    branch = update_head(sha)
    marker = "" if parent else " (root-commit)"
    print(f"[{branch} {sha[:7]}]{marker} {args.message}")


def cmd_log(args):
    sha = head_commit()
    if sha is None:
        sys.exit("mygit: your current branch does not have any commits yet")
    first = True
    while sha:
        info, parents, message = parse_commit(sha)
        if not first:
            print()
        print(f"commit {sha}")
        print(f"Author: {info['author']}")
        ts, tz = info["author"].rsplit(" ", 2)[1:]
        offset = timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5]))
        if tz.startswith("-"):
            offset = -offset
        dt = datetime.fromtimestamp(int(ts), timezone(offset))
        print(f"Date:   {dt.strftime('%a %b %d %H:%M:%S %Y')} {tz}")
        print()
        for line in message.rstrip("\n").split("\n"):
            print(f"    {line}")
        sha = parents[0] if parents else None
        first = False


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
    p.add_argument("-t", dest="type", choices=["blob", "tree", "commit"],
                   help="expected type")
    p.set_defaults(fn=cmd_cat_file)

    p = sub.add_parser("add", help="stage files into the index")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("write-tree", help="build tree objects from the index")
    p.set_defaults(fn=cmd_write_tree)

    p = sub.add_parser("commit", help="record a snapshot from the index")
    p.add_argument("-m", "--message", required=True)
    p.set_defaults(fn=cmd_commit)

    p = sub.add_parser("log", help="show commit history")
    p.set_defaults(fn=cmd_log)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
