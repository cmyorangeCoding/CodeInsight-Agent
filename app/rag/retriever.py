"""
混合检索器（retriever）—— SQLAlchemy ORM 版

职责：向量召回 + 关键词召回 + RRF 融合，返回相关代码片段。
不负责切块、不负责入库。

"""
from __future__ import annotations

from time import perf_counter
from typing import Any

from sqlalchemy import select, func, text, literal_column
from sqlalchemy.orm import Session

from app.rag.models import CodeChunk


def reciprocal_rank_fusion(
    result_lists: list[list[dict]],
    k: int = 60,
) -> list[dict]:
    """
    RRF（Reciprocal Rank Fusion）融合多路召回结果
    核心思想：不使用原始打分（向量距离/关键词分数），只用**排名**计算新分数，
    解决向量分数和全文检索分数量纲不一致无法直接相加的问题。
    公式：RRF_score = sum(1 / (k + rank))
    rank从1开始，排名越靠前，贡献分数越高。k是平滑系数，一般取60。
    """
    scores: dict[str, float] = {} # key: chunk_id, value: RRF累加分数
    chunks: dict[str, dict] = {} # 缓存chunk完整数据，id映射到chunk字典

    for results in result_lists:
        for rank, chunk in enumerate(results, start=1):
            chunk_id = chunk["id"]
            # 累加RRF分数，同一条文档在多个召回命中会叠加得分
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
            chunks[chunk_id] = chunk

    # 按RRF分数降序排序，分数越高越相关
    ranked_ids = sorted(scores, key=scores.get, reverse=True)

    # 组装返回结果，追加rrf_score字段
    return [
        {**chunks[chunk_id], "rrf_score": scores[chunk_id]}
        for chunk_id in ranked_ids
    ]


def _chunk_to_dict(chunk: CodeChunk, extra: dict | None = None) -> dict:
    """把 ORM 对象转成字典（检索结果统一用 dict 返回）。"""
    result = {
        "id": chunk.id,
        "file_path": chunk.file_path,
        "symbol_name": chunk.symbol_name,
        "line_start": chunk.line_start,
        "line_end": chunk.line_end,
        "content": chunk.content,
    }
    if extra:
        result.update(extra)
    return result


class CodeRetriever:
    """代码片段混合检索器。"""

    def __init__(
        self,
        session: Session,
        embeddings: Any,
        repository_id: str,
        vector_top_k: int = 20,
        keyword_top_k: int = 20,
        final_top_k: int = 10,
        tracer=None,
    ):
        self.session = session
        self.embeddings = embeddings
        self.repository_id = repository_id
        self.vector_top_k = vector_top_k
        self.keyword_top_k = keyword_top_k
        self.final_top_k = final_top_k
        self.tracer = tracer  # 可选，用于上报检索结构化事件

    def _log(self, event_type: str, **data) -> None:
        """可观测性打点：只记录结构化元数据，不记录完整 chunk 内容。"""
        if self.tracer is not None:
            try:
                self.tracer.add_event(event_type, **data)
            except Exception:
                pass

    def vector_search(self, query: str) -> list[dict]:
        """
        向量近邻检索

        pgvector.sqlalchemy 自动把 list[float] 转成 vector 参数，
        不需要手动拼字符串。
        """
        # 将用户查询文本转为向量（单独计时，便于定位 Embedding 接口慢的问题）
        embed_start = perf_counter()
        query_vec = self.embeddings.embed_query(query)
        embed_ms = round((perf_counter() - embed_start) * 1000, 2)


        stmt = (
            select(
                CodeChunk,
                CodeChunk.embedding.cosine_distance(query_vec).label("distance"),
            )
            .where(CodeChunk.repository_id == self.repository_id)
            .order_by(CodeChunk.embedding.cosine_distance(query_vec))
            .limit(self.vector_top_k)
        )

        db_start = perf_counter()
        rows = self.session.execute(stmt).all()
        db_ms = round((perf_counter() - db_start) * 1000, 2)

        results = [_chunk_to_dict(row[0], {"distance": row[1]}) for row in rows]
        self._log(
            "rag_vector_search",
            repository_id=self.repository_id,
            query_preview=query[:100],
            candidate_count=len(results),
            embed_ms=embed_ms,
            db_ms=db_ms,
        )
        return results

    def keyword_search(self, query: str) -> list[dict]:
        """
        PostgreSQL 全文检索（关键词召回）。

        全文检索是 PG 特有功能，通过 SQLAlchemy 的 func/text 调用
        ts_rank_cd 和 plainto_tsquery 函数，
        框架仍然负责参数化，防注入。
        """
        # content_tsv 是 PG 生成列，ORM 里没声明，用 literal_column() 引用列名
        # （literal_column 是列表达式，支持 .op()/@ @ 等运算符，text() 不行）
        tsv = literal_column("content_tsv")
        # plainto_tsquery('simple'::regconfig, query)：把输入文本转为 ts 查询，
        # simple 词典不做词干还原，适合代码标识符。
        # 'simple' 必须显式转成 regconfig，否则 PG 无法确定函数重载。
        tsquery = func.plainto_tsquery(text("'simple'::regconfig"), query)

        stmt = (
            select(
                CodeChunk,
                func.ts_rank_cd(tsv, tsquery).label("keyword_score"),
            )
            .where(
                CodeChunk.repository_id == self.repository_id,
                tsv.op("@@")(tsquery),  # 生成 content_tsv @@ plainto_tsquery(...)
            )
            .order_by(text("keyword_score DESC"))# 关键词分数越高越相关，降序
            .limit(self.keyword_top_k)
        )

        db_start = perf_counter()
        rows = self.session.execute(stmt).all()
        db_ms = round((perf_counter() - db_start) * 1000, 2)

        results = [_chunk_to_dict(row[0], {"keyword_score": row[1]}) for row in rows]
        self._log(
            "rag_keyword_search",
            repository_id=self.repository_id,
            query_preview=query[:100],
            candidate_count=len(results),
            db_ms=db_ms,
        )
        return results

    def hybrid_search(self, query: str) -> list[dict]:
        """
        混合检索：向量 + 关键词，RRF 融合。

        :return: 融合后的 Top-N 代码片段，每个包含 rrf_score
        """
        started = perf_counter()
        vector_results = self.vector_search(query)
        keyword_results = self.keyword_search(query)

        if not vector_results and not keyword_results:
            self._log(
                "rag_hybrid_search",
                repository_id=self.repository_id,
                query_preview=query[:100],
                vector_count=0,
                keyword_count=0,
                selected_count=0,
                elapsed_ms=round((perf_counter() - started) * 1000, 2),
            )
            return []

        fused = reciprocal_rank_fusion([vector_results, keyword_results])
        selected = fused[: self.final_top_k]

        # 结构化记录 RRF 排名与最终选中的 chunk（只记路径/行号/分数，不记内容）
        self._log(
            "rag_hybrid_search",
            repository_id=self.repository_id,
            query_preview=query[:100],
            vector_count=len(vector_results),
            keyword_count=len(keyword_results),
            fused_count=len(fused),
            selected_count=len(selected),
            selected_chunks=[
                {
                    "file_path": r.get("file_path"),
                    "line_start": r.get("line_start"),
                    "line_end": r.get("line_end"),
                    "symbol_name": r.get("symbol_name"),
                    "rrf_score": round(r.get("rrf_score", 0.0), 6),
                }
                for r in selected
            ],
            elapsed_ms=round((perf_counter() - started) * 1000, 2),
        )
        return selected
