"""向量化与切块的单元测试。

这里刻意不碰数据库、不碰真模型：切块是纯字符串函数，哈希向量是确定性的，
两者都能在没有基础设施的机器上验证。数据库写入路径由 `--embed` 命令在真实
库上验证（见 README 的阶段 4 实测），真模型的质量只能靠人工看检索结果。

测试的重心是**不变量**，不是「跑通」：
- 每个块记录的区间，必须能原样切出块里的文本（偏移错位是最难发现的 bug）；
- 相邻块必须有重叠（否则跨切点的一句话两边都不完整）；
- 向量必须是单位长度（否则余弦相似度失去意义）。
"""

from __future__ import annotations

import pytest

from app.services import embedding_service
from app.services.embedding_service import (
    CHUNK_OVERLAP,
    HashingEmbedder,
    chunk_text,
    to_vector_literal,
)


def test_chunk_offsets_match_text_exactly():
    """块里存的文本必须等于原文对应区间——偏移错位会静默毁掉整个索引。

    这条断言看起来是同义反复（chunk.text 本来就是切出来的），但它能抓住
    「strip 之后忘了补偿偏移」「按句子切完没更新 end」这类改动。
    """

    raw = "第一段内容。" * 200
    chunks = chunk_text(raw)

    assert len(chunks) > 3
    for chunk in chunks:
        assert raw[chunk.char_start : chunk.char_end] == chunk.text


def test_chunk_indices_are_contiguous():
    """index 必须连续。空片段会被跳过，用循环变量当 index 会留下空洞，
    撞上 (document_id, model, chunk_index) 唯一约束时表现为随机丢块。"""

    chunks = chunk_text("这是一个很长的段落，" * 300)

    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


def test_adjacent_chunks_overlap():
    """相邻块要有重叠，否则正好跨在切点上的句子两边都不完整。"""

    chunks = chunk_text("中文句子内容可能很长很长。" * 300)

    for previous, current in zip(chunks, chunks[1:]):
        gap = previous.char_end - current.char_start
        # 允许因为断句对齐而偏离，但不允许完全无重叠、也不允许重叠过多
        assert 0 < gap <= CHUNK_OVERLAP + 40


def test_chunk_prefers_sentence_boundary():
    """块尾应尽量落在断句字符上，而不是把词切断。"""

    chunks = chunk_text("这是一句话。" * 300, size=200, overlap=20)

    # 除了最后一块，其余都应该以断句字符收尾
    for chunk in chunks[:-1]:
        assert chunk.text.rstrip()[-1] in "。！？；.!?;"


def test_short_text_becomes_single_chunk():
    """够长但不足一块的文本，应该是一个完整块，区间覆盖全文。"""

    raw = (
        "这句话足够长，可以被当作一个独立的知识片段来索引，而且不会被切成两块；"
        "补上这一句是为了超过四十个字的下限，下限本身由另一条测试守着。"
    )
    chunks = chunk_text(raw)

    assert len(chunks) == 1
    assert chunks[0].char_start == 0
    assert chunks[0].char_end == len(raw)


def test_too_short_text_is_not_indexed():
    """低于门槛的文本不产生块——短到没有语义的片段进了索引只会污染近邻。"""

    assert chunk_text("太短了") == []
    assert chunk_text("只有十几个字的一段话，不够一个块") == []


def test_empty_text_yields_nothing():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_trailing_fragment_is_dropped():
    """尾块只是重叠区剩下的碎片时丢掉——留着一个 5 字的块，检索命中它也没有信息量。"""

    # 长度刚过一块多一点，尾部只剩很短的残片
    raw = "句子内容。" * 80 + "尾巴"
    chunks = chunk_text(raw, size=330, overlap=80)

    assert all(len(chunk.text.strip()) >= embedding_service.MIN_CHUNK_CHARS for chunk in chunks)


def test_hashing_embedder_is_deterministic_across_instances():
    """跨实例必须一致：入库和查询是两个进程，哈希加盐会让它们不在同一个空间。"""

    first = HashingEmbedder(64).embed_query("人工智能 数据中心")
    second = HashingEmbedder(64).embed_query("人工智能 数据中心")

    assert first == second


def test_hashing_embedder_is_normalized():
    vector = HashingEmbedder(128).embed_query("一段用来做归一化检查的文本")

    norm = sum(value * value for value in vector) ** 0.5
    assert norm == pytest.approx(1.0, rel=1e-6)


def test_hashing_embedder_ranks_related_above_unrelated():
    """共享字词多的文本，余弦必须更高。这条保证了「存进去能查回来」不是幻觉。"""

    embedder = HashingEmbedder(256)
    passages = embedder.embed_passages(
        [
            "乌兰察布的数据中心已经成了算力战略重点",
            "今天午饭吃了面条，天气不错",
        ]
    )
    query = embedder.embed_query("乌兰察布 数据中心")

    related = sum(a * b for a, b in zip(query, passages[0]))
    unrelated = sum(a * b for a, b in zip(query, passages[1]))
    assert related > unrelated


def test_vector_literal_format():
    """pgvector 的文本形式必须是 `[a,b,c]`，不能有空格、不能是元组括号。"""

    literal = to_vector_literal([0.5, -0.25, 1.0])

    assert literal == "[0.500000,-0.250000,1.000000]"
    assert " " not in literal


def test_e5_requires_prefixes_but_bge_does_not():
    """e5 的训练方式决定了它必须带前缀，漏掉前缀检索质量会断崖式下降。"""

    assert embedding_service._resolve_prefixes("intfloat/multilingual-e5-small") == (
        "query: ",
        "passage: ",
    )
    assert embedding_service._resolve_prefixes("BAAI/bge-small-zh-v1.5") == ("", "")


def test_unavailable_embedder_raises_instead_of_degrading(monkeypatch):
    """模型不可用必须抛错，不能悄悄换成假向量。

    降级的后果是「检索有结果、但结果都是错的」，比直接失败难发现得多。
    """

    monkeypatch.setattr(embedding_service.settings, "embedding_backend", "disabled")
    embedding_service.set_embedder(None)

    with pytest.raises(embedding_service.EmbeddingUnavailable):
        embedding_service.build_embedder()


def test_empty_model_name_falls_back_to_default(monkeypatch):
    """.env 里写 `EMBEDDING_MODEL=`（空串）时不能被当成模型名。

    空串会被拿去拼路径，得到 models/ 这种目录，报错内容跟真实原因毫无关系。
    """

    monkeypatch.setattr(embedding_service.settings, "embedding_model", "")

    assert embedding_service.resolved_model_name() == embedding_service.DEFAULT_MODEL
