"""本地重排：cross-encoder 把「query + 候选文档」一起读一遍，直接给相关性打分。

**为什么召回之后还要重排。** 召回阶段用的是双塔模型：文档在入库时就被压成一个
向量，之后再也看不到 query，两者只在最后做一次点积。信息在压缩那一步就丢了。
实测里这一点很具体——一个问句的前 3 名相似度只差 0.008，因为 384 个浮点数
装不下「这句话到底在问什么」。

交叉编码器换了个做法：把 query 和一篇候选拼成一条句子
（`<s> query </s></s> passage </s>`），送进同一个注意力窗口。模型能读到
「这个词在这句话里是什么意思」，也就是交互信息。代价是**不能预计算**——
每篇候选每次查询都得过一遍模型，所以它只负责精排几十条，不能拿来做召回。

**什么时候它救不了命。** 如果正确答案根本不在候选池里（评测集上曾有 42% 的
topic 标准答案落在前 50 名之外），重排无论多强都没用。所以顺序永远是
先修召回、再上重排。

**为什么不把分数 sigmoid 成 0~1。** int8 量化会改变 logit 的绝对量级，
把它包装成「相关度 87%」会让人误以为这是个可跨模型、跨语料比较的数值。
这里只保证同一批候选内的顺序，展示层也只当作排序依据。
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_MODEL = "Xenova/bge-reranker-base"
# cross-encoder 的上限是 512 token：query 通常几十 token，剩下的留给正文。
# 截断是必要的——一篇 9000 字周刊全塞进去会被截成一万多 token，模型直接报错。
MAX_LENGTH = 512

# padding 宽度吸附到这几个档位。
#
# 原来这里写的是 `width = max(len(ids))`，即「本批最长有多长就多宽」。
# 看着省计算，实测是个陷阱：ONNX Runtime 的 memory pattern 按输入 shape 缓存，
# 每换一个 query，最长 passage 的长度就变一点，于是几乎每次都是全新 shape，
# 每次都要重新做一遍内存规划。同一 query 重复跑 1.6 秒，换一个 query 变 12 秒。
# 吸附到 5 个档位后，shape 种类从「几乎无限」降到 5 种，规划只发生 5 次。
# 代价是每批最多多算 127 个 token 的 padding，远小于省下的规划开销。
WIDTH_BUCKETS = (128, 192, 256, 384, MAX_LENGTH)


class RerankUnavailable(RuntimeError):
    """本地重排模型不可用。携带可执行的修复提示。"""


class Reranker(Protocol):
    name: str

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """返回与 `passages` 等长的分数列表，越大越相关。"""
        ...


class OnnxCrossEncoder:
    """ONNX Runtime 上的 bge-reranker 类交叉编码器。

    与 `OnnxEmbedder` 的差别在输入构造：那边是单序列，这边是 `(query, passage)`
    对。tokenizers 的 `encode_batch` 接受元组，会按模型自带的 post_processor
    拼成 `<s> query </s></s> passage </s>`——**这个格式不能自己手拼**，
    少一个 `</s>` 分数就会整体偏移，而且不会报错。
    """

    def __init__(self, model_dir: Path, model_name: str) -> None:
        self.name = model_name
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as error:  # pragma: no cover - 取决于部署环境
            raise RerankUnavailable(
                "缺少 onnxruntime / tokenizers / numpy，无法本地重排。\n"
                "  pip install -r requirements.txt\n"
                f"原始错误：{error}"
            ) from error

        onnx_path = model_dir / "model_quantized.onnx"
        if not onnx_path.exists():
            onnx_path = model_dir / "model.onnx"
        tokenizer_path = model_dir / "tokenizer.json"
        missing = [
            path.name for path in (onnx_path, tokenizer_path) if not path.exists()
        ]
        if missing:
            raise RerankUnavailable(
                f"模型目录 {model_dir} 缺少 {', '.join(missing)}。\n"
                "运行 python scripts/fetch_reranker.py 下载（走国内镜像）。"
            )

        self._np = np
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._tokenizer.enable_truncation(max_length=MAX_LENGTH)
        session_options = ort.SessionOptions()
        session_options.intra_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(onnx_path), sess_options=session_options, providers=["CPUExecutionProvider"]
        )
        self._input_names = {item.name for item in self._session.get_inputs()}
        self._output_name = self._session.get_outputs()[0].name

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        np = self._np
        # 一次把所有候选编码完再喂给模型，比逐条推理快得多：
        # 模型调用的固定开销（session.run 的输入拷贝、线程调度）摊薄到一批上。
        encodings = self._tokenizer.encode_batch([(query, text) for text in passages])
        # 吸附到固定档位，理由见 WIDTH_BUCKETS 的注释
        raw_width = max(len(encoding.ids) for encoding in encodings)
        width = next((b for b in WIDTH_BUCKETS if b >= raw_width), MAX_LENGTH)
        input_ids = np.zeros((len(encodings), width), dtype=np.int64)
        attention_mask = np.zeros((len(encodings), width), dtype=np.int64)
        for row, encoding in enumerate(encodings):
            length = len(encoding.ids)
            input_ids[row, :length] = encoding.ids
            attention_mask[row, :length] = encoding.attention_mask

        feeds: dict[str, object] = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(input_ids)

        logits = self._session.run([self._output_name], feeds)[0]
        # 输出形状是 [batch, 1]（二分类头），个别导出是 [batch]，两种都接住
        flat = logits.reshape(len(passages), -1)[:, 0]
        return [float(value) for value in flat]


def default_model_dir(model_name: str | None = None) -> Path:
    """与 embedding 模型同根目录，换机器只搬一个文件夹。"""

    if settings.rerank_model_dir:
        return Path(settings.rerank_model_dir).expanduser()
    safe_name = (model_name or resolved_model_name()).replace("/", "__")
    return Path.home() / ".cache" / "devinsight" / "models" / safe_name


def resolved_model_name() -> str:
    # 与 embedding 的 DEFAULT_MODEL 同理由：`.env` 留空会被读成空字符串
    return (settings.rerank_model or "").strip() or DEFAULT_MODEL


def is_enabled() -> bool:
    return (settings.rerank_backend or "onnx").strip().lower() not in (
        "disabled",
        "off",
        "none",
    )


def build_reranker() -> Reranker:
    if not is_enabled():
        raise RerankUnavailable("重排已被配置关闭（RERANK_BACKEND=disabled）")
    return OnnxCrossEncoder(default_model_dir(), resolved_model_name())


def get_reranker() -> Reranker:
    """进程内单例。构造失败不缓存，下次调用会重新尝试（用户可能刚下完模型）。"""

    global _reranker
    if _reranker is not None:
        return _reranker
    with _lock:
        if _reranker is None:
            _reranker = build_reranker()
            logger.info("reranker_ready", extra={"model": _reranker.name})
    return _reranker


def set_reranker(reranker: Reranker | None) -> None:
    """测试注入。生产代码不要调用。"""

    global _reranker
    _reranker = reranker


_reranker: Reranker | None = None
_lock = threading.Lock()
