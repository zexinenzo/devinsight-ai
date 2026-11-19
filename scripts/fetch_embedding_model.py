"""下载本地句向量模型（ONNX 导出），供 embedding_service 使用。

为什么单独写一个脚本，而不是让 huggingface_hub 在首次推理时自动拉：

1. **官方站点在部分网络下不可达。** 实测 huggingface.co 返回代理 502，hf-mirror.com
   正常。自动下载的失败信息会埋在模型加载的异常栈里，很难看出是网络问题。
2. **470 MB 不该发生在请求路径上。** 首次检索请求触发一次 470 MB 下载，会直接超时，
   看起来像检索功能坏了。
3. **要能明确报出「下到一半断了」。** 断点续传、大小校验、失败nonzero 退出，
   这些是脚本该做的事，不是库的默认行为。

用法（在仓库根目录）：

    python scripts/fetch_embedding_model.py
    python scripts/fetch_embedding_model.py --mirror https://www.modelscope.cn   # 换源
    python scripts/fetch_embedding_model.py --check                             # 只检查不下载

下载完成后 `python -m app.cli embed --limit 500` 就能建索引了。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

# 允许直接 `python scripts/xxx.py` 运行：把 apps/api 放进 sys.path，
# 否则 `import app.*` 会失败（文档里曾经就是这么写的，实际跑不起来）
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

DEFAULT_MIRROR = "https://hf-mirror.com"
# 只需要推理必需的四件套。model.safetensors / pytorch_model.bin 用不上，
# 拉了等于白下 1 GB。
FILES = (
    "onnx/model.onnx",
    "onnx/tokenizer.json",
    "onnx/tokenizer_config.json",
    "onnx/special_tokens_map.json",
)


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def download(url: str, target: Path) -> None:
    """流式下载，支持断点续传，先写 .part 再改名。

    两个细节都是踩过的：

    - **写 .part 再改名**：直接写目标文件的话，中断会在磁盘上留下一个
      「存在但截断」的模型，下次运行 onnxruntime 解析失败，报错指向模型格式
      而不是下载不完整，排查方向从一开始就是错的。
    - **必须支持续传**：470 MB 在弱网下几乎一定会断。没有续传时，每次重试都从
      0 开始，实际表现是「永远下不完」。第一次实测就卡在 77 MB 不动。
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    already = partial.stat().st_size if partial.exists() else 0

    headers = {"Range": f"bytes={already}-"} if already else {}
    # read 超时必须显式给：默认的 60 秒在「服务器一点点吐字节」时永远不触发，
    # 进程会静默挂在那里，看起来像还在下载
    timeout = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=15.0)

    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True, headers=headers) as response:
        if already and response.status_code == 416:
            # 服务器说范围越界：说明文件其实已经完整
            print("    已存在完整文件，跳过")
            partial.replace(target)
            return
        response.raise_for_status()

        resumed = response.status_code == 206
        if already and not resumed:
            # 服务器忽略了 Range，返回整个文件。只能从头写，不能往后追加，
            # 否则文件内容会变成「旧的前半段 + 完整的新文件」
            print("    服务器不支持续传，从头下载")
            already = 0

        total_header = response.headers.get("content-length")
        total = int(total_header) + already if total_header else 0
        mode = "ab" if resumed else "wb"
        done = already
        last_report = 0
        with partial.open(mode) as handle:
            for chunk in response.iter_bytes(chunk_size=1 << 20):
                handle.write(chunk)
                done += len(chunk)
                if total and done - last_report > total // 10:
                    last_report = done
                    print(f"    {done / total:5.1%}  {human(done)} / {human(total)}", flush=True)

    size = partial.stat().st_size
    if total and size != total:
        raise SystemExit(f"下载不完整：{partial} 期望 {total} 字节，实际 {size} 字节")
    partial.replace(target)
    print(f"    完成 {human(size)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载本地句向量模型")
    parser.add_argument("--mirror", default=DEFAULT_MIRROR, help=f"模型站点（默认 {DEFAULT_MIRROR}）")
    parser.add_argument("--model", default=None, help="覆盖 EMBEDDING_MODEL")
    parser.add_argument("--check", action="store_true", help="只检查文件是否齐全，不下载")
    args = parser.parse_args(argv)

    from app.core.config import settings
    from app.services.embedding_service import default_model_dir, resolved_model_name

    model_name = args.model or resolved_model_name()
    target_dir = default_model_dir(model_name)
    missing = [name for name in FILES if not (target_dir / Path(name).name).exists()]

    print(f"模型：{model_name}")
    print(f"目录：{target_dir}")
    print(f"维度：{settings.embedding_dimensions}（必须与模型输出一致，否则插入会报维度不匹配）")

    if not missing:
        size = sum((target_dir / Path(name).name).stat().st_size for name in FILES)
        print(f"状态：已就绪（{human(size)}）")
        print("下一步：cd apps/api && python -m app.cli embed --limit 500")
        return 0

    print(f"缺失：{', '.join(Path(name).name for name in missing)}")
    if args.check:
        print("（--check 模式，不下载）")
        return 1

    base = args.mirror.rstrip("/")
    for name in missing:
        url = f"{base}/{model_name}/resolve/main/{name}"
        target = target_dir / Path(name).name
        print(f"  下载 {Path(name).name}  ← {url}")
        try:
            download(url, target)
        except httpx.HTTPStatusError as error:
            print(f"  失败：HTTP {error.response.status_code}")
            print("  提示：换一个镜像试试，例如 --mirror https://huggingface.co "
                  "或 https://www.modelscope.cn")
            return 1

    print("完成。下一步：cd apps/api && python -m app.cli embed --limit 500")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
