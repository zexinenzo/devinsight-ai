"""下载本地重排模型（cross-encoder，ONNX 导出），供 rerank_service 使用。

与 `fetch_embedding_model.py` 分工不同：那边下的是「句向量」模型（把文本变成
一个点，可以预先算好存库），这边下的是「交叉编码器」（把 query 和 passage
拼成一条句子送进模型，直接输出一个相关性分数）。

为什么重排必须用交叉编码器，而不能继续用向量：

- 双塔（bi-encoder）在文档入库时就把文本压成了向量，之后再也看不到 query，
  两者只在最后做一次点积。信息在压缩那一步就丢了。
- 交叉编码器每次把 query 和候选文档放在同一个注意力窗口里读一遍，能捕捉
  「这个词在这句话里是什么意思」级别的细节。代价是**不能预计算**——每篇文档
  每次查询都要过一次模型，所以它只能用来精排几十条候选，不能拿来做召回。

用法（在仓库根目录）：

    python scripts/fetch_reranker.py
    python scripts/fetch_reranker.py --mirror https://huggingface.co
    python scripts/fetch_reranker.py --check

默认下 int8 量化版（279 MB）。fp32 是 1.06 GB，量化对**排序**的影响很小，
对**分数绝对值**有影响——所以代码里只比较分数大小，不用它做跨模型的阈值判断。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_embedding_model import DEFAULT_MIRROR, download, human

DEFAULT_REPO = "Xenova/bge-reranker-base"
FILES = (
    "onnx/model_quantized.onnx",
    "tokenizer.json",
    "tokenizer_config.json",
)

# 与 embedding 模型放在同一个根目录下，换机器只需搬一个文件夹
MODELS_ROOT = Path.home() / ".cache" / "devinsight" / "models"


def target_dir(repo: str) -> Path:
    return MODELS_ROOT / repo.replace("/", "__")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载本地重排模型")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"模型仓库（默认 {DEFAULT_REPO}）")
    parser.add_argument("--mirror", default=DEFAULT_MIRROR, help=f"模型站点（默认 {DEFAULT_MIRROR}）")
    parser.add_argument("--check", action="store_true", help="只检查文件是否齐全，不下载")
    args = parser.parse_args(argv)

    directory = target_dir(args.repo)
    missing = [name for name in FILES if not (directory / Path(name).name).exists()]

    print(f"模型：{args.repo}")
    print(f"目录：{directory}")
    print("用途：cross-encoder 精排（每篇候选都要过模型，只用于 top-N 重排）")

    if not missing:
        size = sum((directory / Path(name).name).stat().st_size for name in FILES)
        print(f"状态：已就绪（{human(size)}）")
        return 0

    print(f"缺失：{', '.join(Path(name).name for name in missing)}")
    if args.check:
        print("（--check 模式，不下载）")
        return 1

    base = args.mirror.rstrip("/")
    for name in missing:
        url = f"{base}/{args.repo}/resolve/main/{name}"
        print(f"  下载 {Path(name).name}  ← {url}")
        try:
            download(url, directory / Path(name).name)
        except httpx.HTTPStatusError as error:
            print(f"  失败：HTTP {error.response.status_code}")
            print("  提示：换一个镜像试试，例如 --mirror https://huggingface.co")
            return 1

    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
