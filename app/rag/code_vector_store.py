"""
代码片段存储（code_vector_store）—— SQLAlchemy ORM

职责：封装 code_chunks 表的增删改查。
不负责切块、不负责向量化、不负责检索召回。

用 ORM 之后，不再手写任何 SQL 的 INSERT/DELETE/SELECT，
框架自动生成参数化 SQL，防注入、类型安全。
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine, select, delete, func
from sqlalchemy.orm import Session, sessionmaker

from dotenv import load_dotenv

from app.rag.models import CodeChunk, Base

load_dotenv()


def create_session_factory() -> sessionmaker[Session]:
    """
    创建 Session 工厂

    create_engine() = 配置连接池
    sessionmaker() = 从工厂创建 Session
    """
    # SQLAlchemy 的 "postgresql://" 默认指向 psycopg2 驱动，
    # 而本项目装的是 psycopg3（包名 psycopg）。
    # 这里统一改写成 "postgresql+psycopg://"，明确使用 psycopg3 驱动。
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)

    engine = create_engine(
        url,
        # echo=True 时会打印框架生成的 SQL
        echo=False,
    )
    return sessionmaker(bind=engine, expire_on_commit=False)


class CodeVectorStore:
    """
    code_chunks 表的 CRUD 封装
    """

    def __init__(self, session: Session):
        self.session = session

    def upsert_file_chunks(
        self,
        repository_id: str,
        file_path: str,
        chunks: list[dict],
    ) -> int:
        """
        对单个文件执行 upsert：事务内先删旧 chunk，再写入新 chunk。
        """
        if not chunks:
            self.delete_file_chunks(repository_id, file_path)
            return 0

        # 先删除该文件的所有旧 chunk
        self.session.execute(
            delete(CodeChunk).where(
                CodeChunk.repository_id == repository_id,
                CodeChunk.file_path == file_path,
            )
        )

        # 逐条添加新 chunk
        for c in chunks:
            chunk = CodeChunk(
                id=c["id"],
                repository_id=repository_id,
                file_path=c["file_path"],
                symbol_name=c.get("symbol_name"),
                language=c["language"],
                line_start=c["line_start"],
                line_end=c["line_end"],
                content=c["content"],
                embedding_text=c["embedding_text"],
                embedding=c["embedding"],  # pgvector 自动适配 list[float] -> vector
                content_hash=c["content_hash"],
            )
            self.session.add(chunk)

        self.session.commit()
        return len(chunks)

    def delete_file_chunks(self, repository_id: str, file_path: str) -> None:
        """删除指定文件的所有 chunk"""
        self.session.execute(
            delete(CodeChunk).where(
                CodeChunk.repository_id == repository_id,
                CodeChunk.file_path == file_path,
            )
        )
        self.session.commit()

    def delete_repository_chunks(self, repository_id: str) -> None:
        """删除整个仓库的所有 chunk。"""
        self.session.execute(
            delete(CodeChunk).where(CodeChunk.repository_id == repository_id)
        )
        self.session.commit()

    def get_file_hashes(self, repository_id: str) -> dict[str, str]:
        """
        获取仓库内所有已索引文件的 content_hash（增量索引用）。

        Java 对照：
            em.createQuery("SELECT c.filePath, c.contentHash FROM CodeChunk c WHERE c.repositoryId = :rid", Tuple.class)
        """
        stmt = (
            select(CodeChunk.file_path, CodeChunk.content_hash)
            .where(CodeChunk.repository_id == repository_id)
            .distinct(CodeChunk.file_path)
        )
        rows = self.session.execute(stmt).all()
        return {row[0]: row[1] for row in rows}

    def get_file_chunks(
        self, repository_id: str, file_path: str
    ) -> list[CodeChunk]:
        """获取指定文件的所有 chunk（按行号排序）。"""
        stmt = (
            select(CodeChunk)
            .where(
                CodeChunk.repository_id == repository_id,
                CodeChunk.file_path == file_path,
            )
            .order_by(CodeChunk.line_start)
        )
        return list(self.session.scalars(stmt))

    def count_chunks(self, repository_id: str) -> int:
        """统计仓库已索引的 chunk 总数。"""
        stmt = (
            select(func.count())
            .select_from(CodeChunk)
            .where(CodeChunk.repository_id == repository_id)
        )
        return self.session.scalar(stmt) or 0
