# mygit — 从零实现迷你 git

对标真 git 逐步实现,每一步都用真 git **对拍验证**(同样的输入必须产出同样的对象哈希)。

## 里程碑

- [x] **M1 对象库**:`init` / `hash-object` / `cat-file`。blob 采用
  `<type> <size>\0` 头 + zlib 压缩 + SHA-1,按 `objects/xx/yyyy...` 布局存盘,
  与真 git 完全兼容(同一文件得到同一个 SHA)。
- [ ] **M2 暂存与提交**:`add`(index 文件)/ `write-tree` / `commit-tree` /
  `commit` / `log`
- [ ] **M3 引用**:`branch` / `checkout` / `tag`(轻量标签)
- [ ] **M4 diff 与 status**:Myers diff 算法、index 与工作区对比
- [ ] **M5(可选)**:packfile 与 `gc`

## 对拍验证(M1)

```bash
python mygit.py init
printf 'hello\n' > hello.txt
python mygit.py hash-object hello.txt   # 应与下面一行完全一致
git hash-object hello.txt
```
