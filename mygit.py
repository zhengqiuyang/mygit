#!/usr/bin/env python3
"""mygit — 从零实现的迷你 git。

M1 对象库:blob 的哈希与磁盘布局与真 git 完全兼容。
M2 暂存与提交:index(暂存区)/ write-tree(嵌套 tree 对象)/ commit / log,
tree 与 commit 对象同样与真 git 格式兼容——固定作者与日期时,
对同一内容会产出和真 git 完全相同的 commit SHA。
M3 引用与检出:branch / checkout(-b)/ tag(轻量与附注标签对象);
checkout 会把工作区与暂存区同步到目标提交的树。
M4 diff 与 status:Myers O(ND) 最短编辑距离算法、unified diff 输出、
status(已暂存/未暂存/未跟踪三区对比)。
M5 packfile 与 gc:PACK v2 / IDX v2 真实格式(无 delta),
gc 把 loose 对象打包并在校验后删除;对象读取自动回退到 pack。
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
    """读对象:先 loose,再 pack。"""
    path = object_path(sha)
    if os.path.exists(path):
        with open(path, "rb") as f:
            raw = zlib.decompress(f.read())
        header, _, body = raw.partition(b"\0")
        otype, size = header.decode().split()
        if int(size) != len(body):
            sys.exit(f"mygit: {sha[:12]}: corrupt object (size mismatch)")
        return otype, body
    for pack_path in _list_packs():
        _shas, offsets = _load_idx(pack_path)
        if sha in offsets:
            return _read_packed_object(pack_path, offsets[sha])
    sys.exit(f"mygit: {sha[:12]}: object not found")


# ---------- packfile:PACK v2 / IDX v2(M5) ----------

PACK_TYPE_NUM = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
PACK_TYPE_NAME = {num: name for name, num in PACK_TYPE_NUM.items()}
IDX_MAGIC = b"\xfftOc"

_pack_cache = {}  # idx 路径 -> (sha 列表, {sha: 偏移})


def _encode_pack_header(otype, size):
    """pack 对象头:type 3 位 + 变长 7 位组编码的 size。"""
    byte = (PACK_TYPE_NUM[otype] << 4) | (size & 0x0F)
    size >>= 4
    out = bytearray()
    while size:
        out.append(byte | 0x80)
        byte = size & 0x7F
        size >>= 7
    out.append(byte)
    return bytes(out)


def _pack_dir():
    return git_path("objects", "pack")


def _list_packs():
    pack_dir = _pack_dir()
    if not os.path.isdir(pack_dir):
        return []
    return sorted(os.path.join(pack_dir, name)
                  for name in os.listdir(pack_dir) if name.endswith(".pack"))


def _load_idx(pack_path):
    idx_path = pack_path[:-len(".pack")] + ".idx"
    cached = _pack_cache.get(idx_path)
    if cached is not None:
        return cached
    with open(idx_path, "rb") as f:
        data = f.read()
    if data[:4] != IDX_MAGIC or int.from_bytes(data[4:8], "big") != 2:
        sys.exit(f"mygit: {idx_path}: 不支持的 idx 格式")
    count = int.from_bytes(data[8 + 255 * 4:8 + 256 * 4], "big")
    shas_start = 8 + 256 * 4
    shas = [data[shas_start + 20 * i:shas_start + 20 * (i + 1)].hex()
            for i in range(count)]
    offsets_start = shas_start + count * 20 + count * 4  # 跳过 crc32 表
    offsets = {}
    for i, sha in enumerate(shas):
        offset = int.from_bytes(
            data[offsets_start + 4 * i:offsets_start + 4 * (i + 1)], "big")
        if offset & 0x80000000:
            sys.exit(f"mygit: {idx_path}: 大 pack(64 位偏移)暂不支持")
        offsets[sha] = offset & 0x7FFFFFFF
    _pack_cache[idx_path] = (shas, offsets)
    return shas, offsets


def _read_packed_object(pack_path, offset):
    with open(pack_path, "rb") as f:
        f.seek(offset)
        byte = f.read(1)[0]
        otype = (byte >> 4) & 0x07
        size = byte & 0x0F
        shift = 4
        while byte & 0x80:
            byte = f.read(1)[0]
            size |= (byte & 0x7F) << shift
            shift += 7
        if otype not in PACK_TYPE_NAME:
            sys.exit(f"mygit: pack 中出现 delta 对象(type {otype}),M5 暂不支持")
        content = zlib.decompressobj().decompress(f.read())
    if len(content) != size:
        sys.exit(f"mygit: {pack_path}: pack 对象大小不匹配")
    return PACK_TYPE_NAME[otype], content


def _iter_loose_objects():
    objects_dir = git_path("objects")
    if not os.path.isdir(objects_dir):
        return
    for sub in sorted(os.listdir(objects_dir)):
        if len(sub) != 2:
            continue  # 跳过 pack 目录等
        subdir = os.path.join(objects_dir, sub)
        for name in sorted(os.listdir(subdir)):
            yield sub + name, os.path.join(subdir, name)


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


# ---------- tree 读取与检出(M3) ----------

def read_tree(sha):
    """解析 tree 对象,返回 [(mode, name, hex_sha)]。"""
    otype, body = read_object(sha)
    if otype != "tree":
        sys.exit(f"mygit: {sha[:12]}: not a tree")
    entries = []
    i = 0
    while i < len(body):
        j = body.index(b"\0", i)
        mode, name = body[i:j].decode().split(" ", 1)
        hex_sha = body[j + 1:j + 21].hex()
        entries.append((mode, name, hex_sha))
        i = j + 21
    return entries


def flatten_tree(tree_sha, prefix=""):
    """递归展开 tree,返回 {path: (mode, sha)}。"""
    files = {}
    for mode, name, sha in read_tree(tree_sha):
        path = prefix + name
        if mode == "40000":
            files.update(flatten_tree(sha, path + "/"))
        else:
            files[path] = (mode, sha)
    return files


def tree_from_commit(sha):
    info, _, _ = parse_commit(sha)
    return info["tree"]


def _prune_empty_dirs(path, stop):
    """删除 path 的空父目录链,直到 stop(不含)为止。"""
    parent = os.path.dirname(path)
    while parent.startswith(stop) and parent != stop:
        try:
            os.rmdir(parent)
        except OSError:
            break
        parent = os.path.dirname(parent)


def checkout_tree(commit_sha):
    """把工作区与 index 同步到目标提交的树。
    只删除旧 index 里跟踪、目标里没有的文件,不动未跟踪文件。"""
    root = repo_root()
    old_index = read_index()
    target = flatten_tree(tree_from_commit(commit_sha))
    for path in old_index:
        if path not in target:
            full = os.path.join(root, *path.split("/"))
            if os.path.exists(full):
                os.remove(full)
                _prune_empty_dirs(full, root)
    for path, (mode, sha) in target.items():
        dest = os.path.join(root, *path.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        _, blob = read_object(sha)
        with open(dest, "wb") as f:
            f.write(blob)
    write_index(target)


def _branch_names():
    heads = git_path("refs", "heads")
    if not os.path.isdir(heads):
        return []
    return sorted(os.listdir(heads))


def _write_head(branch):
    with open(git_path("HEAD"), "w", encoding="utf-8") as f:
        f.write(f"ref: refs/heads/{branch}\n")


def _create_branch(name):
    if "/" in name:
        sys.exit("mygit: M3 暂不支持包含 '/' 的分支名")
    head = head_commit()
    if head is None:
        sys.exit("error: Not a valid object name: 还没有任何提交")
    ref_file = git_path("refs", "heads", name)
    if os.path.exists(ref_file):
        sys.exit(f"fatal: a branch named '{name}' already exists")
    os.makedirs(os.path.dirname(ref_file), exist_ok=True)
    with open(ref_file, "w", encoding="utf-8") as f:
        f.write(head + "\n")


def _index_matches_head():
    """index 是否与 HEAD 提交的树完全一致(无已暂存未提交的改动)。"""
    head = head_commit()
    if head is None:
        return not read_index()
    return read_index() == flatten_tree(tree_from_commit(head))


# ---------- diff:Myers 算法与 unified diff(M4) ----------

def myers_ops(a, b):
    """Myers O(ND) 最短编辑脚本 + 回溯。返回自上而下的操作序列
    [('=', 行), ('-', 旧行), ('+', 新行)],其中 '-'/'+' 的总数最小。"""
    n, m = len(a), len(b)
    v = {1: 0}
    trace = []
    total_d = None
    for d in range(n + m + 1):
        trace.append(dict(v))
        for k in range(-d, d + 1, 2):
            if k == -d or (k != d and v.get(k - 1, -1) < v.get(k + 1, -1)):
                x = v.get(k + 1, 0)
            else:
                x = v.get(k - 1, 0) + 1
            y = x - k
            while x < n and y < m and a[x] == b[y]:
                x += 1
                y += 1
            v[k] = x
            if x >= n and y >= m:
                total_d = d
                break
        if total_d is not None:
            break
    ops = []
    x, y = n, m
    for d in range(total_d, 0, -1):
        v_prev = trace[d]
        k = x - y
        if k == -d or (k != d and v_prev.get(k - 1, -1) < v_prev.get(k + 1, -1)):
            prev_k = k + 1
        else:
            prev_k = k - 1
        prev_x = v_prev.get(prev_k, 0)
        prev_y = prev_x - prev_k
        while x > prev_x and y > prev_y:
            ops.append(("=", a[x - 1]))
            x, y = x - 1, y - 1
        if x == prev_x:      # down:插入 b 的一行
            ops.append(("+", b[prev_y]))
        else:                # right:删除 a 的一行
            ops.append(("-", a[prev_x]))
        x, y = prev_x, prev_y
    while x > 0 and y > 0:   # d=0 的对角线
        ops.append(("=", a[x - 1]))
        x, y = x - 1, y - 1
    ops.reverse()
    return ops


def make_hunks(ops, context=3):
    """把操作序列切成带上下文的 hunk。
    返回 [(a_start, a_stop, b_start, b_stop, [(前缀, 行)])],行号 0 基、半开区间。"""
    if not any(op != "=" for op, _ in ops):
        return []
    ann = []
    a_no = b_no = 0
    for op, line in ops:
        ann.append((op, line, a_no, b_no))
        if op == "=":
            a_no += 1
            b_no += 1
        elif op == "-":
            a_no += 1
        else:
            b_no += 1
    a_total, b_total = a_no, b_no
    # 变更区段
    regions = []
    start = None
    for i, (op, *_rest) in enumerate(ann):
        if op != "=" and start is None:
            start = i
        elif op == "=" and start is not None:
            regions.append((start, i))
            start = None
    if start is not None:
        regions.append((start, len(ann)))
    # 间隔不超过 2*context 的区段合并成一个 hunk
    merged = [list(regions[0])]
    for s, e in regions[1:]:
        if s - merged[-1][1] <= 2 * context:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    hunks = []
    n = len(ann)
    for s, e in merged:
        s = max(0, s - context)
        e = min(n, e + context)
        seg = ann[s:e]
        a_start, b_start = seg[0][2], seg[0][3]
        if e < n:
            a_stop, b_stop = ann[e][2], ann[e][3]
        else:
            a_stop, b_stop = a_total, b_total
        lines = [(" " if op == "=" else op, line) for op, line, *_r in seg]
        hunks.append((a_start, a_stop, b_start, b_stop, lines))
    return hunks


def format_range(start, stop):
    length = stop - start
    beginning = start + 1 if length > 0 else start
    if length == 1:
        return str(beginning)
    return f"{beginning},{length}"


def blob_sha_of(data):
    """只算 blob 哈希、不落盘。"""
    return hashlib.sha1(f"blob {len(data)}".encode() + b"\0" + data).hexdigest()


def print_file_diff(path, old_bytes, new_bytes, status, mode):
    """按 git 风格打印一个文件的 unified diff。"""
    print(f"diff --git a/{path} b/{path}")
    if status == "new":
        print(f"new file mode {mode}")
    elif status == "deleted":
        print(f"deleted file mode {mode}")
    old7 = blob_sha_of(old_bytes)[:7] if old_bytes is not None else "0000000"
    new7 = blob_sha_of(new_bytes)[:7] if new_bytes is not None else "0000000"
    print(f"index {old7}..{new7} {mode}")
    if (old_bytes is not None and b"\0" in old_bytes) or \
            (new_bytes is not None and b"\0" in new_bytes):
        print(f"Binary files a/{path} and b/{path} differ")
        return
    print("--- a/" + path if old_bytes is not None else "--- /dev/null")
    print("+++ b/" + path if new_bytes is not None else "+++ /dev/null")
    a = old_bytes.decode("utf-8", "replace").splitlines() if old_bytes else []
    b = new_bytes.decode("utf-8", "replace").splitlines() if new_bytes else []
    for a0, a1, b0, b1, seg in make_hunks(myers_ops(a, b)):
        print(f"@@ -{format_range(a0, a1)} +{format_range(b0, b1)} @@")
        for prefix, line in seg:
            print(prefix + line)


def _diff_sides(old_files, new_files, load_old, load_new):
    """old_files/new_files: {path: (mode, sha)},逐文件输出 diff。"""
    for path in sorted(set(old_files) | set(new_files)):
        old = old_files.get(path)
        new = new_files.get(path)
        if old == new:
            continue
        old_bytes = load_old(path, old) if old else None
        new_bytes = load_new(path, new) if new else None
        if old and new and old_bytes == new_bytes:
            continue  # 只有 mode 不同,M4 不展开
        status = "new" if old is None else ("deleted" if new is None else None)
        mode = (new or old)[0]
        print_file_diff(path, old_bytes, new_bytes, status, mode)


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


def cmd_branch(args):
    repo_root()
    if args.delete:
        if args.name is None:
            sys.exit("mygit: branch -d 需要一个分支名")
        if args.name == head_branch():
            sys.exit(f"error: cannot delete branch '{args.name}' checked out here")
        ref_file = git_path("refs", "heads", args.name)
        if not os.path.exists(ref_file):
            sys.exit(f"error: branch '{args.name}' not found")
        with open(ref_file, encoding="utf-8") as f:
            sha = f.read().strip()
        os.remove(ref_file)
        print(f"Deleted branch {args.name} (was {sha[:7]}).")
        return
    if args.name is None:
        current = head_branch()
        for name in _branch_names():
            print(("* " if name == current else "  ") + name)
        return
    _create_branch(args.name)


def cmd_checkout(args):
    repo_root()
    if args.create:
        _create_branch(args.branch)
        _write_head(args.branch)
        print(f"Switched to a new branch '{args.branch}'")
        return
    ref_file = git_path("refs", "heads", args.branch)
    if not os.path.exists(ref_file):
        sys.exit(f"error: pathspec '{args.branch}' did not match any branch")
    if args.branch == head_branch():
        print(f"Already on '{args.branch}'")
        return
    if not _index_matches_head():
        sys.exit("error: your staged changes would be overwritten by checkout,\n"
                 "please commit them before switching branches")
    with open(ref_file, encoding="utf-8") as f:
        target = f.read().strip()
    checkout_tree(target)
    _write_head(args.branch)
    print(f"Switched to branch '{args.branch}'")


def cmd_tag(args):
    repo_root()
    tags_dir = git_path("refs", "tags")
    if args.name is None:
        if os.path.isdir(tags_dir):
            for name in sorted(os.listdir(tags_dir)):
                print(name)
        return
    head = head_commit()
    if head is None:
        sys.exit("error: cannot tag: 还没有任何提交")
    if args.annotate and not args.message:
        sys.exit("mygit: 附注标签需要 -m 提供标签说明")
    ref_file = os.path.join(tags_dir, args.name)
    if os.path.exists(ref_file):
        sys.exit(f"fatal: tag '{args.name}' already exists")
    if args.annotate:
        name, email = _config("user.name"), _config("user.email")
        ts, tz = env_date("GIT_COMMITTER_DATE")
        message = args.message if args.message.endswith("\n") else args.message + "\n"
        body = (f"object {head}\ntype commit\ntag {args.name}\n"
                f"tagger {name} <{email}> {ts} {tz}\n\n{message}")
        sha = write_object(body.encode("utf-8"), "tag")
    else:
        sha = head
    os.makedirs(tags_dir, exist_ok=True)
    with open(ref_file, "w", encoding="utf-8") as f:
        f.write(sha + "\n")


def cmd_diff(args):
    repo_root()
    index = read_index()
    if args.cached or args.staged:
        head = head_commit()
        old_files = flatten_tree(tree_from_commit(head)) if head else {}

        def load_object(path, entry):
            return read_object(entry[1])[1]

        _diff_sides(old_files, dict(index), load_object, load_object)
        return
    root = repo_root()
    disk = {}
    new_files = {}
    for path, entry in index.items():
        full = os.path.join(root, *path.split("/"))
        if os.path.isfile(full):
            with open(full, "rb") as f:
                disk[path] = f.read()
            # 新侧的身份是磁盘内容的哈希,而不是 index 里的旧 sha
            new_files[path] = (entry[0], blob_sha_of(disk[path]))
    _diff_sides(dict(index), new_files,
                lambda p, e: read_object(e[1])[1],
                lambda p, e: disk[p])


def cmd_status(args):
    root = repo_root()
    print(f"On branch {head_branch() or '(无提交)'}")
    head = head_commit()
    head_files = flatten_tree(tree_from_commit(head)) if head else {}
    index = read_index()
    staged = []
    for path in sorted(set(head_files) | set(index)):
        h = head_files.get(path)
        i = index.get(path)
        if h == i:
            continue
        if h is None:
            staged.append(("new file", path))
        elif i is None:
            staged.append(("deleted", path))
        else:
            staged.append(("modified", path))
    unstaged = []
    for path in sorted(index):
        full = os.path.join(root, *path.split("/"))
        if not os.path.isfile(full):
            unstaged.append(("deleted", path))
            continue
        with open(full, "rb") as f:
            data = f.read()
        if blob_sha_of(data) != index[path][1]:
            unstaged.append(("modified", path))
    untracked = sorted(rel for _full, rel in iter_worktree_files(root)
                       if rel not in index)
    if staged:
        print("\nChanges to be committed:")
        for kind, path in staged:
            print(f"  {kind + ':':<12}{path}")
    if unstaged:
        print("\nChanges not staged for commit:")
        for kind, path in unstaged:
            print(f"  {kind + ':':<12}{path}")
    if untracked:
        print("\nUntracked files:")
        for path in untracked:
            print(f"  {path}")
    if not (staged or unstaged or untracked):
        print("\nnothing to commit, working tree clean")


def cmd_gc(args):
    repo_root()
    loose = list(_iter_loose_objects())
    if not loose:
        print("nothing to pack(没有 loose 对象)")
        return
    # 读出全部 loose 对象,idx 要求按 sha 有序
    objects = []
    for sha, path in loose:
        with open(path, "rb") as f:
            raw = zlib.decompress(f.read())
        header, _, body = raw.partition(b"\0")
        otype, _size = header.decode().split()
        objects.append((sha, otype, body))
    objects.sort(key=lambda item: item[0])

    # 组装 PACK v2
    pack = bytearray(b"PACK" + (2).to_bytes(4, "big")
                     + len(objects).to_bytes(4, "big"))
    entries = []  # (sha, offset, crc32)
    for sha, otype, body in objects:
        offset = len(pack)
        chunk = _encode_pack_header(otype, len(body)) + zlib.compress(body)
        pack += chunk
        entries.append((sha, offset, zlib.crc32(chunk) & 0xFFFFFFFF))
    pack_sha = hashlib.sha1(pack).hexdigest()
    pack += bytes.fromhex(pack_sha)

    # 组装 IDX v2:magic+version, 256 fanout, sha 表, crc32 表, 偏移表, 双校验和
    idx = bytearray(IDX_MAGIC + (2).to_bytes(4, "big"))
    shas = [entry[0] for entry in entries]
    for i in range(256):
        count = sum(1 for s in shas if int(s[:2], 16) <= i)
        idx += count.to_bytes(4, "big")
    for sha in shas:
        idx += bytes.fromhex(sha)
    for _sha, _offset, crc in entries:
        idx += crc.to_bytes(4, "big")
    for _sha, offset, _crc in entries:
        if offset >= 0x80000000:
            sys.exit("mygit: pack 超过 2GB,大偏移表暂不支持")
        idx += offset.to_bytes(4, "big")
    idx += bytes.fromhex(pack_sha)
    idx += hashlib.sha1(idx).digest()

    pack_dir = _pack_dir()
    os.makedirs(pack_dir, exist_ok=True)
    pack_path = os.path.join(pack_dir, f"pack-{pack_sha}.pack")
    idx_path = os.path.join(pack_dir, f"pack-{pack_sha}.idx")
    with open(pack_path, "wb") as f:
        f.write(pack)
    with open(idx_path, "wb") as f:
        f.write(idx)

    # 从 pack 读回逐个校验,全部通过才删 loose
    _shas, offsets = _load_idx(pack_path)
    for sha, otype, body in objects:
        got_type, got_body = _read_packed_object(pack_path, offsets[sha])
        if got_type != otype or got_body != body:
            sys.exit(f"mygit: pack 校验失败,loose 对象已保留:{sha[:12]}")
    for _sha, path in loose:
        os.remove(path)
        try:
            os.rmdir(os.path.dirname(path))
        except OSError:
            pass
    print(f"packed {len(objects)} objects into pack-{pack_sha[:8]}")


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
    p.add_argument("-t", dest="type", choices=["blob", "tree", "commit", "tag"],
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

    p = sub.add_parser("branch", help="list, create, or delete branches")
    p.add_argument("name", nargs="?")
    p.add_argument("-d", dest="delete", action="store_true")
    p.set_defaults(fn=cmd_branch)

    p = sub.add_parser("checkout", help="switch branches (syncs worktree)")
    p.add_argument("branch")
    p.add_argument("-b", dest="create", action="store_true",
                   help="create the branch before switching")
    p.set_defaults(fn=cmd_checkout)

    p = sub.add_parser("tag", help="list or create tags")
    p.add_argument("name", nargs="?")
    p.add_argument("-a", "--annotate", action="store_true",
                   help="create an annotated tag object")
    p.add_argument("-m", "--message")
    p.set_defaults(fn=cmd_tag)

    p = sub.add_parser("diff", help="show changes (worktree vs index; "
                                    "--staged: index vs HEAD)")
    p.add_argument("--cached", action="store_true")
    p.add_argument("--staged", action="store_true")
    p.set_defaults(fn=cmd_diff)

    p = sub.add_parser("status", help="show staged/unstaged/untracked changes")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("gc", help="pack loose objects (PACK/IDX v2, no deltas)")
    p.set_defaults(fn=cmd_gc)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
