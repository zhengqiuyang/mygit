# mygit — 从零实现迷你 git

[![CI](https://github.com/zhengqiuyang/mygit/actions/workflows/ci.yml/badge.svg)](https://github.com/zhengqiuyang/mygit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-informational.svg)](./LICENSE)

对标真 git 逐步实现,每一步都用真 git **对拍验证**(同样的输入必须产出同样的对象哈希)。

## 里程碑

- [x] **M1 对象库**:`init` / `hash-object` / `cat-file`。blob 采用
  `<type> <size>\0` 头 + zlib 压缩 + SHA-1,按 `objects/xx/yyyy...` 布局存盘,
  与真 git 完全兼容(同一文件得到同一个 SHA)。
- [x] **M2 暂存与提交**:`add`(index 暂存区)/ `write-tree`(嵌套 tree 对象)/
  `commit` / `log`。支持 `GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE` 固定日期;
  固定作者与日期时,与真 git 对同一内容产出**相同的 commit SHA**(见 test_m2.py 对拍)。
- [x] **M3 引用**:`branch`(列表/创建/删除)/ `checkout`(含 `-b`,
  同步工作区与暂存区,拒绝有已暂存改动时切换)/ `tag`(轻量 + 附注标签对象)。
  附注标签对象与真 git 格式完全兼容——固定身份与日期时 SHA 一致(见 test_m3.py)。
- [x] **M4 diff 与 status**:手写 Myers O(ND) 最短编辑距离算法 + unified diff
  (`@@ -a,b +c,d @@` 带上下文 hunk,`diff` 工作区对比、`diff --staged` 暂存区对比、
  新文件/删除文件/Binary files 提示);`status` 三区对比(已暂存/未暂存/未跟踪)。
  与真 `git diff` 的正文输出对拍一致。
- [ ] **M5(可选)**:packfile 与 `gc`

## 对拍验证(M1)

```bash
python mygit.py init
printf 'hello\n' > hello.txt
python mygit.py hash-object hello.txt   # 应与下面一行完全一致
git hash-object hello.txt
```
