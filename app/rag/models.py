
from sqlalchemy import Column, String, Integer, Text, Index
from sqlalchemy.orm import DeclarativeBase

# pgvector 的 SQLAlchemy 适配，Vector(1024) 对应 PostgreSQL 的 vector(1024) 类型
from pgvector.sqlalchemy import Vector


class Base(DeclarativeBase):
    """所有 ORM 实体的基类（等价于 JPA 的 @MappedSuperclass 基础类）。"""
    pass


class CodeChunk(Base):
    """
    code_chunks 表的 ORM 映射。
    """

    #表名
    __tablename__ = "code_chunks"

    # 主键（等价于 @Id）
    id = Column(String, primary_key=True)

    # 基础字段
    repository_id = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    symbol_name = Column(String, nullable=True)
    language = Column(String, nullable=True)
    line_start = Column(Integer, nullable=False)
    line_end = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    embedding_text = Column(Text, nullable=False)

    # 向量字段：pgvector 提供，1024 维（和 Embedding 模型输出维度一致）
    embedding = Column(Vector(1024), nullable=False)

    content_hash = Column(String, nullable=False)

    # content_tsv 是 PostgreSQL 生成列（GENERATED ALWAYS AS ... STORED），
    # ORM 不需要写它，查询时用原生 SQL 函数即可。
    # 所以这里不声明这个字段。

    __table_args__ = (
        # 索引定义
        Index("idx_code_chunks_repository", "repository_id"),
        Index("idx_code_chunks_file", "repository_id", "file_path"),
    )

    def __repr__(self):
        return (
            f"<CodeChunk(id={self.id[:8]}..., "
            f"file={self.file_path}, symbol={self.symbol_name}, "
            f"L{self.line_start}-{self.line_end})>"
        )
