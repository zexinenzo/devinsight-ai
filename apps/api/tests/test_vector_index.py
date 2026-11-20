"""向量落库路径的测试（不连数据库）。

用一个记录调用的假 session 替代 AsyncSession，验证三件事：
1. 写进库的参数里**没有任何原文**——「不存文本」这个承诺必须由测试守住，
   否则哪天有人顺手加一列 `text` 调试，没人会发现；
2. 向量条数和块数不一致时整批拒绝，而不是按顺序错位贴上去；
3. 模型不可用时抛错，不静默跳过。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest
from app.services import embedding_service, vector_index_service
from app.services.embedding_service import EmbeddingUnavailable, HashingEmbedder


class FakeSession:
    """只记录执行了什么，不真连库。"""

    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        return None


class ShortEmbedder(HashingEmbedder):
    """少返回一个向量的坏 embedder，用于验证错位保护。"""

    name = "short-embedder"

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return super().embed_passages(texts)[:-1]


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def long_text() -> str:
    return "这段话用来凑长度，让切块逻辑真的切出多块来。" * 60


def test_empty_items_does_nothing(session):
    import asyncio

    embedding_service.set_embedder(HashingEmbedder(64))
    stats = asyncio.run(vector_index_service.index_documents(session, []))

    assert stats["documents"] == 0
    assert session.statements == []


def test_short_text_is_skipped_not_written(session):
    import asyncio

    embedding_service.set_embedder(HashingEmbedder(64))
    document_id = uuid.uuid4()
    stats = asyncio.run(
        vector_index_service.index_documents(session, [(document_id, "太短了")])
    )

    assert stats["skipped_short"] == 1
    assert stats["chunks"] == 0
    # 短文本仍然会先 DELETE 旧索引（replace=True），但不该有 INSERT
    assert not any("INSERT" in statement for statement, _ in session.statements)


def test_insert_payload_contains_no_original_text(session, long_text):
    """核心断言：落库参数只有位置和向量，没有一句原文。

    如果哪天有人为了「调试方便」加了一列 text，这条测试会立刻失败。
    """

    import asyncio

    embedding_service.set_embedder(HashingEmbedder(64))
    document_id = uuid.uuid4()
    asyncio.run(vector_index_service.index_documents(session, [(document_id, long_text)]))

    inserts = [params for statement, params in session.statements if "INSERT" in statement]
    assert len(inserts) == 1
    rows = inserts[0]
    assert rows

    allowed = {"document_id", "chunk_index", "char_start", "char_end", "embedding", "model"}
    for row in rows:
        assert set(row) == allowed
        # 向量的文本形式，绝不该是一段能读的文章
        assert str(row["embedding"]).startswith("[")
        assert row["char_end"] > row["char_start"]
        assert row["model"] == "hashing-ngram-v1"


def test_offsets_written_match_the_chunks(session, long_text):
    import asyncio

    embedding_service.set_embedder(HashingEmbedder(64))
    asyncio.run(vector_index_service.index_documents(session, [(uuid.uuid4(), long_text)]))

    rows = [params for statement, params in session.statements if "INSERT" in statement][0]
    for row in rows:
        # 区间必须能在这段原文里取出来，且非空
        assert long_text[row["char_start"] : row["char_end"]].strip()


def test_vector_count_mismatch_aborts_whole_batch(session, long_text):
    """向量数量与块数不一致时必须整批拒绝。

    按顺序 zip 会把向量贴到错误的区间上——库里看起来正常，检索却指向别处，
    这种错误没有任何报错，只能靠这个检查拦住。
    """

    import asyncio

    embedding_service.set_embedder(ShortEmbedder(64))

    with pytest.raises(RuntimeError, match="向量数量与块数不一致"):
        asyncio.run(vector_index_service.index_documents(session, [(uuid.uuid4(), long_text)]))


def test_unavailable_embedder_propagates(session, long_text, monkeypatch):
    """模型不可用要抛出去，让调用方决定怎么办，不能在这里吞掉。"""

    import asyncio

    monkeypatch.setattr(embedding_service.settings, "embedding_backend", "disabled")
    embedding_service.set_embedder(None)

    with pytest.raises(EmbeddingUnavailable):
        asyncio.run(vector_index_service.index_documents(session, [(uuid.uuid4(), long_text)]))

    assert session.statements == []


def test_replace_false_does_not_delete(session, long_text):
    """only_missing 场景不能删已有索引，否则每补一批就重算一遍。"""

    import asyncio

    embedding_service.set_embedder(HashingEmbedder(64))
    asyncio.run(
        vector_index_service.index_documents(session, [(uuid.uuid4(), long_text)], replace=False)
    )

    assert not any("DELETE" in statement for statement, _ in session.statements)


class _Rows:
    """让 `execute(...).all()` 能返回预设行，供需要读结果的逻辑使用。"""

    def __init__(self, rows: Sequence[tuple[str, int]]) -> None:
        self._rows = rows

    def all(self):
        return self._rows


class RowsSession(FakeSession):
    """FakeSession + 可预设的 SELECT 结果。"""

    def __init__(self, rows: Sequence[tuple[str, int]] | None = None) -> None:
        super().__init__()
        self._rows = list(rows or [])

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        return _Rows(self._rows)


def test_purge_stale_models_deletes_other_vector_spaces():
    """换模型后，旧向量空间的行必须被显式清掉。

    `replace=True` 只删**当前模型名下**的块。写了别的模型名的旧行，没有任何写入路径
    会碰到它们，会永久留在表里：覆盖率统计虚高、存储白占、临时排查 SQL 拿到两个
    向量空间混在一起的结果。所以重建前必须单独清一次。
    """

    import asyncio

    embedding_service.set_embedder(HashingEmbedder(64))
    session = RowsSession([("hashing-ngram-v1", 787)])

    stale = asyncio.run(vector_index_service.purge_stale_models(session))

    assert stale == {"hashing-ngram-v1": 787}
    deletes = [(statement, params) for statement, params in session.statements if "DELETE" in statement]
    assert len(deletes) == 1
    # 条件是 `model <> :model`，参数是**要保留**的模型名，方向反了会把当前向量全删光
    assert "model <> :model" in deletes[0][0]
    assert deletes[0][1]["model"] == "hashing-ngram-v1"


def test_purge_stale_models_noop_when_table_is_clean():
    """只有当前模型时不发 DELETE，避免每次增量补课都全表扫一遍。"""

    import asyncio

    embedding_service.set_embedder(HashingEmbedder(64))
    session = RowsSession([])

    stale = asyncio.run(vector_index_service.purge_stale_models(session))

    assert stale == {}
    assert not any("DELETE" in statement for statement, _ in session.statements)
