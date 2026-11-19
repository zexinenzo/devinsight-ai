"""把正文写进向量索引：切块 → 算向量 → 存向量 → 丢弃文本。

这个模块是「向量化但不存原文」唯一执行落库的地方，所以有三条硬约束：

1. **`text` 只在函数栈上存在。** 切完块、算完向量，函数返回时它就随栈一起消失，
   不进 ORM 对象、不进日志、不进异常信息。
2. **模型缺失不降级。** 拿不到 embedder 就抛 `EmbeddingUnavailable`，由调用方决定
   是跳过这一批还是让整个任务失败。用假向量顶上只会让检索「有结果但都是错的」。
3. **写入用文本字面量 + CAST。** pgvector 的原生类型在 asyncpg 下需要
   `register_vector` 给连接注册 codec，没注册就报 `no encoder for type 'vector'`。
   与其在各个连接入口都记得注册一次，不如让 SQL 自己写 `CAST(:emb AS vector)`，
   Postgres 会把 `'[0.1,0.2,...]'` 这个字符串解析成向量。少一个隐式前置条件。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.services import embedding_service

logger = get_logger(__name__)

# 一次送进模型的最大块数。CPU 推理是同步的，批太大既吃内存又让事件循环停顿更久；
# 32 块在 e5-small 上大约 200~400ms，是「值得丢进线程」的量级。
EMBED_BATCH = 32

_INSERT_SQL = text(
    """
    INSERT INTO document_chunks
        (document_id, chunk_index, char_start, char_end, embedding, model)
    VALUES
        (:document_id, :chunk_index, :char_start, :char_end, CAST(:embedding AS vector), :model)
    ON CONFLICT (document_id, model, chunk_index) DO UPDATE
        SET embedding  = EXCLUDED.embedding,
            char_start = EXCLUDED.char_start,
            char_end   = EXCLUDED.char_end
    """
)


def _vector_literal(values: Sequence[float]) -> str:
    """保留旧名，实际实现已提到 embedding_service：入库与查询必须同一个格式化。"""

    return embedding_service.to_vector_literal(values)


async def index_documents(
    session: AsyncSession,
    items: Sequence[tuple[uuid.UUID, str]],
    *,
    replace: bool = True,
) -> dict[str, int]:
    """给一批 (文档 ID, 正文) 建向量索引。

    `replace=True` 时先清掉该文档在这个模型下的旧块——正文可能被重新抓过，
    旧的块位置和新的对不上，留着只会让检索命中一个已经不存在的位置。

    返回计数用于运维观察：`chunks` 远小于 `documents` 通常意味着正文太短、
    本来就不需要索引，而不是出错。
    """

    counters = {"documents": 0, "chunks": 0, "skipped_short": 0, "chunks_dropped": 0}
    if not items:
        return counters

    # 拿不到模型就直接抛出去，交给调用方决定是跳过这批还是让任务失败。
    # 不在这里 try/except 后原样再抛一次——那只是把异常绕了一圈，还多一层栈。
    # 也绝不降级成假向量：搜索结果「有、但全错」比直接失败难发现得多。
    embedder = embedding_service.get_embedder()

    # 第一遍：只切块，不碰模型。切块是纯 CPU 字符串操作，很快，
    # 先切完能一次性算出总量，也避免 embedding 失败时白算。
    pending: list[tuple[uuid.UUID, list[embedding_service.Chunk]]] = []
    for document_id, raw_text in items:
        chunks = embedding_service.chunk_text(raw_text or "")
        if not chunks:
            counters["skipped_short"] += 1
            continue
        pending.append((document_id, chunks))

    if not pending:
        return counters

    model_name = embedder.name
    if replace:
        await session.execute(
            text("DELETE FROM document_chunks WHERE document_id = ANY(:ids) AND model = :model"),
            {"ids": [document_id for document_id, _ in pending], "model": model_name},
        )

    # 第二遍：分批算向量。推理是同步 CPU 计算，丢进线程，别把事件循环堵住。
    flat: list[embedding_service.Chunk] = [chunk for _, chunks in pending for chunk in chunks]
    vectors: list[list[float]] = []
    for offset in range(0, len(flat), EMBED_BATCH):
        batch = [chunk.text for chunk in flat[offset : offset + EMBED_BATCH]]
        vectors.extend(await asyncio.to_thread(embedder.embed_passages, batch))

    if len(vectors) != len(flat):
        # 数量和块数对不上，说明下标会错位——宁可整批失败也不要把向量贴到错误的区间上
        raise RuntimeError(
            f"向量数量与块数不一致：{len(vectors)} != {len(flat)}，拒绝写入错位的索引"
        )

    rows: list[dict[str, object]] = []
    cursor = 0
    for document_id, chunks in pending:
        for chunk in chunks:
            rows.append(
                {
                    "document_id": document_id,
                    "chunk_index": chunk.index,
                    "char_start": chunk.char_start,
                    "char_end": chunk.char_end,
                    "embedding": _vector_literal(vectors[cursor]),
                    "model": model_name,
                }
            )
            cursor += 1
        counters["documents"] += 1

    if rows:
        await session.execute(_INSERT_SQL, rows)

    counters["chunks"] = len(rows)
    logger.info("vector_index_done", extra={**counters, "model": model_name})
    return counters


async def index_from_stored_content(
    session: AsyncSession,
    *,
    limit: int = 500,
    only_missing: bool = True,
) -> dict[str, int]:
    """用**已经入库的正文**补索引。

    索引质量取决于「库里的正文还剩多少」：

    - `full_text` / `cc_by_nc_nd` 的源存了全文，这条路径能索引到完整内容，最理想。
    - `metadata_excerpt` 的源在库里只有 600 字，索引它就是索引那 600 字。
      这仍有价值——关键词检索只覆盖标题和摘要，这 600 字原本完全不可检索——但
      召回率明显低于抓取时索引全文。所以**主要路径是抓取那一刻**（见
      content_service.run_enrich），这条路径只是存量补课和换模型后的重建。

    换句话说：能索引多少，不取决于模型，取决于你手上还留着多少原文。
    """

    model_name = embedding_service.get_embedder().name
    where_missing = ""
    if only_missing:
        where_missing = """
            AND NOT EXISTS (
                SELECT 1 FROM document_chunks c
                 WHERE c.document_id = d.id AND c.model = :model
            )
        """

    statement = text(
        f"""
        SELECT d.id, d.content_text
          FROM documents d
         WHERE d.content_text IS NOT NULL
           AND length(d.content_text) > :min_chars
           {where_missing}
         ORDER BY d.fetched_at DESC
         LIMIT :limit
        """
    )
    rows = (
        await session.execute(
            statement,
            {
                "limit": limit,
                "model": model_name,
                "min_chars": embedding_service.CHUNK_CHARS,
            },
        )
    ).all()

    return await index_documents(
        session, [(row[0], row[1]) for row in rows], replace=not only_missing
    )


async def purge_stale_models(
    session: AsyncSession,
    *,
    keep_model: str | None = None,
) -> dict[str, int]:
    """删掉不属于当前模型的向量块，返回 `{旧模型名: 删除行数}`。

    换模型就是换向量空间，旧块留着有三个坏处：

    1. 覆盖率统计里的 `EXISTS` 是按文档判断"有没有块"，不带 model 条件，
       旧行会让「已建索引」虚高，把真实缺口盖住；
    2. 白占表空间，还要被 HNSW 索引一起维护；
    3. 任何一个没带 model 条件的临时排查 SQL，都会拿到两个空间的混淆结果。

    检索路径本身是带 `WHERE model = :model` 的，所以旧行不会被召回——
    这也正是它难发现的原因：接口表现一切正常，问题只在统计口径和存储上慢慢漂移。
    """

    model_name = keep_model or embedding_service.get_embedder().name
    rows = (
        await session.execute(
            text(
                """
                SELECT model, count(*)
                  FROM document_chunks
                 WHERE model <> :model
                 GROUP BY model
                 ORDER BY count(*) DESC
                """
            ),
            {"model": model_name},
        )
    ).all()
    if not rows:
        return {}

    await session.execute(
        text("DELETE FROM document_chunks WHERE model <> :model"), {"model": model_name}
    )
    stale = {str(row[0]): int(row[1]) for row in rows}
    logger.info("vector_index_purged", extra={"stale": stale, "keep": model_name})
    return stale
