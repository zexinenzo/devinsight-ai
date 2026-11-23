# 项目约定

<!-- retro:start -->
## 导航

- `docs/README.md` — 全部设计文档的索引。找「某阶段为什么这么做」先看这里，不要猜文件名。
- `CODING_STANDARDS.md` — 评审时读的规则，实现时不必读。
- `scripts/ops.py` — 查库状态只用它：`pool` / `dups` / `dirty` / `find` / `stats`。

## 交给用户的命令

每条命令都可直接复制运行：绝对路径、真实参数、不带尖括号。缺值时先输出取值的命令，拿到输出再输出执行命令。

## 查数据

文档里的库内计数只从 `.venv/bin/python scripts/ops.py stats` 取。
<!-- retro:end -->
