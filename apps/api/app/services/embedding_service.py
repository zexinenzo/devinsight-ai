"""本地向量化：把长正文压成向量存起来，原文一个字都不落库。

这条链路存在的理由，是「授权」和「召回」两个约束在这里正面冲突：

- 授权上，`metadata_excerpt` 级别的源只允许我们存 600 字片段（见 content_service 的
  三级 rights_mode）。全文一旦入库，性质就从「自己看」变成「替代原站」。
- 召回上，600 字的 ILIKE 只能命中标题和导语。读者记得的那句话往往在第三段之后，
  现有检索永远够不着。

向量化把这两件事拆开了：正文在内存里待过一次，切成块、算成向量，然后把文字丢掉。
库里留下的是 384 个浮点数加一个字符区间，没有一句可读的原文。检索能定位到
「这篇文章第 1200 字附近讲过这个」，展示仍然只给标题、摘要和原文链接。

**为什么用本地模型，不用第三方 embedding API。**
把全文 POST 出去做向量化，法律性质会从「我们自己存储」变成「向第三方提供作品」，
等于绕过了 rights_mode 这道闸门。本地推理不产生任何对外传输。

**为什么用 ONNX 而不是 sentence-transformers。**
同一个模型权重，torch 要多装 2 GB 以上。这个项目只需要「文本进、向量出」这一件事，
onnxruntime + tokenizers 一共几十兆，冷启动也更快。

**为什么模型缺失时必须报错，不能降级。**
降级成随机向量或字符哈希，检索会「有结果但都是错的」，比直接报错更难发现。
所以 `get_embedder()` 在 onnx 后端下找不到模型文件就抛 `EmbeddingUnavailable`，
调用方自己决定是跳过还是中止。每条向量都记录 `model` 名，混合索引能被查出来。
"""

from __future__ import annotations

import threading
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# 切块长度。e5-small 是 XLM-R 结构，上限 512 token；中文大约 1 字 1 token，
# 留出前缀和标点的余量，400 字是安全值。切得太碎会丢上下文，切得太长会被截断。
CHUNK_CHARS = 400
# 相邻块重叠 80 字：一句话正好跨在切点上时，至少有一块能完整包含它
CHUNK_OVERLAP = 80
# 断句优先找这些字符，让块边界落在句子之间而不是词中间
_BREAK_CHARS = "\n。！？；.!?;"
# 尾块短于这个长度就直接丢掉（那是重叠区剩下来的碎片，没有独立语义）
MIN_CHUNK_CHARS = 40


class EmbeddingUnavailable(RuntimeError):
    """本地模型不可用。携带可执行的修复提示，不要把它变成静默降级。"""


@dataclass(frozen=True)
class Chunk:
    """一个待向量化的片段。

    只记位置不记文本——`text` 字段在写完向量之后就该被丢弃，不要持久化。
    """

    index: int
    char_start: int
    char_end: int
    text: str


def chunk_text(
    text_value: str,
    *,
    size: int = CHUNK_CHARS,
    overlap: int = CHUNK_OVERLAP,
) -> list[Chunk]:
    """把正文切成带字符区间的块，尽量在句子边界断开。

    区间是相对于**传入的原始字符串**算的，所以调用方必须先切块再截断；
    先截到 600 字再切，区间就失去意义了。
    """

    if not text_value:
        return []

    # 太短的内容不值得进索引：三五个字的片段既没有可检索的语义，句向量本身也不可靠
    # （模型在这么短的输入上会产生接近随机方向的向量，反而污染近邻结果）。
    # 门槛放在切块这一层而不是调用方，是为了让「进不进索引」只有一个判断点。
    if len(text_value.strip()) < MIN_CHUNK_CHARS:
        return []

    total = len(text_value)
    chunks: list[Chunk] = []
    start = 0
    while start < total:
        end = min(start + size, total)
        if end < total:
            # 只在窗口后 40% 的范围里找断点：往前找太远会让块变得过短
            cut = _find_break(text_value, start + int(size * 0.6), end)
            if cut is not None:
                end = cut
        piece = text_value[start:end]
        if piece.strip():
            # index 用「已产出的块数」，保证连续；不直接用循环变量，
            # 因为空片段会被跳过，留下空洞的 index 对不上库里的唯一约束
            chunks.append(Chunk(len(chunks), start, end, piece))
        if end >= total:
            break
        start = max(end - overlap, start + 1)

    if len(chunks) > 1 and len(chunks[-1].text.strip()) < MIN_CHUNK_CHARS:
        chunks.pop()
    return chunks


def _find_break(text_value: str, low: int, high: int) -> int | None:
    """在 [low, high) 里从后往前找一个断句字符，返回断点下标（含该字符）。"""

    for position in range(high - 1, low - 1, -1):
        if text_value[position] in _BREAK_CHARS:
            return position + 1
    return None


class Embedder(Protocol):
    """最小接口。测试注入假实现，生产注入 ONNX 实现。"""

    name: str
    dimensions: int

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """字符二元组哈希向量。

    **只用于测试和离线演示**，不是语义模型。但它不是随机向量：共享字词越多，
    余弦越接近，所以「存进去能用相似度查回来」这条链路能被真实地验证，
    而不是靠一个恒定返回同样值的桩。

    名字里带 hashing-ngram，会写进 document_chunks.model —— 一旦库里混进这种向量，
    查 `select distinct model` 就能看出来。
    """

    name = "hashing-ngram-v1"
    dimensions = 384

    def __init__(self, dimensions: int = 384) -> None:
        self.dimensions = dimensions

    def _vector(self, text_value: str) -> list[float]:
        vector = [0.0] * self.dimensions
        normalized = " ".join(text_value.lower().split())
        if not normalized:
            return vector
        for position in range(len(normalized) - 1):
            gram = normalized[position : position + 2]
            # crc32 而不是 hash()：后者每个进程加盐，跨进程结果不一致，
            # 会导致「入库用的向量」和「查询用的向量」根本不在一个空间里
            slot = zlib.crc32(gram.encode("utf-8")) % self.dimensions
            vector[slot] += 1.0
        norm = sum(value * value for value in vector) ** 0.5
        if norm == 0:
            return vector
        return [value / norm for value in vector]

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text_value) for text_value in texts]

    def embed_query(self, text_value: str) -> list[float]:
        return self._vector(text_value)


class OnnxEmbedder:
    """ONNX Runtime 上的 e5 / bge 类句向量模型。

    参数全在构造时加载一次，之后 `embed_passages` 可以被反复调用。
    推理是同步 CPU 计算，调用方要放进线程里跑，别阻塞事件循环。
    """

    def __init__(self, model_dir: Path, model_name: str, dimensions: int) -> None:
        self.name = model_name
        self.dimensions = dimensions
        self._prefix_query, self._prefix_passage = _resolve_prefixes(model_name)

        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as error:  # pragma: no cover - 取决于部署环境
            raise EmbeddingUnavailable(
                "缺少 onnxruntime / tokenizers / numpy，无法本地向量化。\n"
                "  pip install -r requirements.txt\n"
                f"原始错误：{error}"
            ) from error

        onnx_path = model_dir / "model.onnx"
        tokenizer_path = model_dir / "tokenizer.json"
        missing = [path.name for path in (onnx_path, tokenizer_path) if not path.exists()]
        if missing:
            raise EmbeddingUnavailable(
                f"模型目录 {model_dir} 缺少 {', '.join(missing)}。\n"
                "运行 python ../scripts/fetch_embedding_model.py 下载（走国内镜像）。"
            )

        self._np = np
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._tokenizer.enable_truncation(max_length=512)
        session_options = ort.SessionOptions()
        # 单线程足够：这个进程的瓶颈是数据库和网络，不是 CPU。
        # 多线程反而会和 Celery 的并发争抢，把小机器拖死。
        session_options.intra_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(onnx_path), sess_options=session_options, providers=["CPUExecutionProvider"]
        )
        self._input_names = {item.name for item in self._session.get_inputs()}
        self._output_name = self._session.get_outputs()[0].name

        # 维度自检：模型实际输出多少维，和配置声明多少维，必须是同一个数。
        # 不做这一步的话，错误会推迟到 INSERT 时才以「expected 384 dimensions,
        # not 1536」的形式出现——那是 Postgres 在报列类型，而不是在说
        # 「EMBEDDING_DIMENSIONS 配错了」，排查会绕很大一圈。
        output_shape = self._session.get_outputs()[0].shape
        actual = output_shape[-1] if isinstance(output_shape, (list, tuple)) and output_shape else None
        if isinstance(actual, int) and actual != dimensions:
            raise EmbeddingUnavailable(
                f"模型 {model_name} 输出 {actual} 维，但 EMBEDDING_DIMENSIONS={dimensions}。\n"
                f"两者必须一致：这个值决定了 document_chunks.embedding 的 vector({dimensions})。\n"
                "改维度要同时改 .env 并新写一条迁移重建索引。"
            )

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._encode([self._prefix_passage + text_value for text_value in texts])

    def embed_query(self, text_value: str) -> list[float]:
        return self._encode([self._prefix_query + text_value])[0]

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        np = self._np
        encodings = self._tokenizer.encode_batch(list(texts))
        width = max(len(encoding.ids) for encoding in encodings)
        input_ids = np.zeros((len(encodings), width), dtype=np.int64)
        attention_mask = np.zeros((len(encodings), width), dtype=np.int64)
        for row, encoding in enumerate(encodings):
            length = len(encoding.ids)
            input_ids[row, :length] = encoding.ids
            attention_mask[row, :length] = encoding.attention_mask

        feeds: dict[str, object] = {"input_ids": input_ids, "attention_mask": attention_mask}
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(input_ids)

        hidden = self._session.run([self._output_name], feeds)[0]
        # 均值池化：只对真实 token 求平均。把 padding 一起算进去会让短文本的向量
        # 被大量零向量稀释，长短文本之间的余弦就失去可比性。
        mask = attention_mask[..., None].astype(hidden.dtype)
        summed = (hidden * mask).sum(axis=1)
        counts = np.clip(mask.sum(axis=1), 1e-9, None)
        pooled = summed / counts
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        normalized = pooled / np.clip(norms, 1e-9, None)
        return normalized.astype(np.float32).tolist()


def to_vector_literal(values: Sequence[float]) -> str:
    """把向量写成 pgvector 认的文本形式 `[0.1,0.2,...]`。

    入库和查询必须用同一个格式化函数。两处各写一份，只要有一处精度或分隔符不同，
    查出来的距离就会和入库时算的不一致——而且不会报错，只是排序悄悄变差。

    保留 6 位小数：float32 的有效精度约 7 位十进制，再多是噪声，还会让字符串变长。
    """

    return "[" + ",".join(f"{value:.6f}" for value in values) + "]"


def _resolve_prefixes(model_name: str) -> tuple[str, str]:
    """e5 系列必须带 "query:" / "passage:" 前缀，否则检索质量断崖式下降。

    这是训练方式决定的：e5 是在「带前缀的 (query, passage) 对」上对比学习的，
    不带前缀等于用了它没见过的输入分布。bge 系列则不需要。
    """

    lowered = model_name.lower()
    if "e5" in lowered:
        return "query: ", "passage: "
    return "", ""


_embedder: Embedder | None = None
_lock = threading.Lock()


# 兜底模型名。`.env` 里留空（`EMBEDDING_MODEL=`）时 pydantic-settings 会把它
# 读成空字符串而不是「未设置」，空串直接拿去拼路径会得到 models/ 这种荒唐目录，
# 而且报错跟模型名毫无关系。这里显式兜一层。
DEFAULT_MODEL = "intfloat/multilingual-e5-small"


def resolved_model_name() -> str:
    return (settings.embedding_model or "").strip() or DEFAULT_MODEL


def default_model_dir(model_name: str | None = None) -> Path:
    """模型默认落在用户缓存目录，不进仓库。

    显式传入 model_name 时用它算目录，否则用配置里的模型名。
    下载脚本靠这个参数让 `--model` 真的指向对应的目录，而不是永远落到配置值上。
    """

    if settings.embedding_model_dir:
        return Path(settings.embedding_model_dir).expanduser()
    safe_name = (model_name or resolved_model_name()).replace("/", "__")
    return Path.home() / ".cache" / "devinsight" / "models" / safe_name


def build_embedder() -> Embedder:
    """按配置构造 embedder。不缓存，只给 `get_embedder` 和测试用。"""

    backend = (settings.embedding_backend or "onnx").strip().lower()
    if backend == "hashing":
        # 明确的自选降级，用于测试与离线演示
        return HashingEmbedder(settings.embedding_dimensions)
    if backend in ("disabled", "off", "none"):
        raise EmbeddingUnavailable("向量化已被配置关闭（EMBEDDING_BACKEND=disabled）")
    return OnnxEmbedder(
        default_model_dir(), resolved_model_name(), settings.embedding_dimensions
    )


def get_embedder() -> Embedder:
    """进程内单例。构造失败会抛 EmbeddingUnavailable，不缓存失败结果。"""

    global _embedder
    if _embedder is not None:
        return _embedder
    with _lock:
        if _embedder is None:
            _embedder = build_embedder()
            logger.info(
                "embedder_ready",
                extra={"model": _embedder.name, "dimensions": _embedder.dimensions},
            )
    return _embedder


def set_embedder(embedder: Embedder | None) -> None:
    """测试注入。生产代码不要调用。"""

    global _embedder
    _embedder = embedder


def is_enabled() -> bool:
    return (settings.embedding_backend or "onnx").strip().lower() not in (
        "disabled",
        "off",
        "none",
    )
