"""
索引构建流程（ingestion）

职责：扫描仓库 → 读取文件 → 计算 hash → 切块 → 批量向量化 → 入库。
不负责检索、不负责 Agent 工具。

依赖：
  - RepositoryAnalyzer（已有，负责安全扫描/读取）
  - chunker（切块）
  - CodeVectorStore（入库，ORM 版）
  - embeddings（向量化）
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from app.rag.chunker import chunk_file, CodeChunk
from app.rag.code_vector_store import CodeVectorStore
from app.tools.repository import RepositoryAnalyzer


# 单文件大小上限：超过则跳过（避免大文件产生海量 chunk / 超长文本）
MAX_FILE_SIZE = 500 * 1024  # 500 KB

# 单次调用 embedding API 的最大文本数（分批，避免一次请求超限）
# DashScope 限制单次最多 10 条文本，超过会报 "batch size is invalid"
EMBED_BATCH_SIZE = 10

# 每处理多少个文件打印一次进度
PROGRESS_EVERY = 100  # 每 100 个文件推一次进度，增量索引时也有"在跑"的感觉


def _sha256(text: str) -> str:
    """计算文本的 SHA-256 哈希。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_embedding_text(chunk: CodeChunk) -> str:
    """
    组装 embedding 文本：文件路径 + 符号名 + 代码内容。
    与实际存储的 content 分开，embedding_text 可包含上下文提示。
    """
    parts = [f"文件：{chunk.file_path}"]
    if chunk.symbol_name:
        parts.append(f"符号：{chunk.symbol_name}")
    parts.append("代码：")
    parts.append(chunk.content)
    return "\n".join(parts)


class RepositoryIndexer:
    """仓库索引构建器。"""

    def __init__(
        self,
        repository: RepositoryAnalyzer,
        store: CodeVectorStore,
        embeddings: Any,
        repository_id: str,
    ):
        self.repository = repository
        self.store = store
        self.embeddings = embeddings
        self.repository_id = repository_id

    def index_repository(
        self,
        max_files: int | None = None,
        progress_callback=None,
    ) -> dict:
        """
        全量/增量索引整个仓库。

        :param max_files: 限制处理文件数（首次验证链路用），None 表示不限制
        :param progress_callback: 可选回调 callable(seq, total, file_path, indexed, skipped)
                                  —— 每处理 PROGRESS_EVERY 个文件调一次，用于前端 SSE 进度推送
        :return: 统计信息 {indexed, skipped, deleted, chunks_written}
        """
        # 1. 扫描仓库文件
        files = self.repository.index_files()
        if max_files is not None:
            files = files[:max_files]

        total_files = len(files)
        print(f"  待处理文件：{total_files} 个")

        # 2. 获取已索引文件的 hash，用于增量判断
        existing_hashes = self.store.get_file_hashes(self.repository_id)
        existing_paths = set(existing_hashes.keys())  #把文件路径改成set去重

        indexed = 0
        skipped = 0
        chunks_written = 0

        for seq, file_info in enumerate(files, start=1):
            file_path = file_info["path"]

            # 进度打印 + 回调推送
            if seq % PROGRESS_EVERY == 0 or seq == total_files:
                msg = f"  ...进度 {seq}/{total_files}（已索引 {indexed} 个文件）"
                print(msg)
                # 任何时候都推 —— 让前端有"在跑"的感觉；PROGRESS_EVERY=100 足够低频不刷屏
                if progress_callback:
                    try:
                        progress_callback(seq, total_files, file_path, indexed, skipped)
                    except Exception:
                        pass  # 回调失败不阻塞索引

            # 3. 大文件直接跳过，避免产生海量 chunk
            size = file_info.get("size", 0)
            if size > MAX_FILE_SIZE:
                print(f"  [跳过] 文件过大 {file_path}（{size} bytes）")
                skipped += 1
                continue

            # 4. 读取文件内容
            try:
                content = self.repository.read_file(file_path)
            except Exception as e:
                print(f"  [跳过] 读取失败 {file_path}: {e}")
                skipped += 1
                continue

            # 5. 计算hash，未变更则跳过
            content_hash = _sha256(content)
            if existing_hashes.get(file_path) == content_hash:
                skipped += 1
                existing_paths.discard(file_path)
                continue

            # 6. 切块
            chunks = chunk_file(content, file_path)
            if not chunks:
                skipped += 1
                existing_paths.discard(file_path)
                continue

            # 7. 组装 embedding_text 并分批向量化
            embedding_texts = [_build_embedding_text(c) for c in chunks]
            vectors: list = []
            try:
                for i in range(0, len(embedding_texts), EMBED_BATCH_SIZE):
                    batch = embedding_texts[i : i + EMBED_BATCH_SIZE]
                    vectors.extend(self.embeddings.embed_documents(batch))
            except Exception as e:
                print(f"  [跳过] 向量化失败 {file_path}: {e}")
                skipped += 1
                continue

            # 8. 组装入库数据
            chunk_dicts = []
            for chunk, emb in zip(chunks, vectors):
                chunk_dicts.append(
                    {
                        "id": str(uuid.uuid4()),
                        "file_path": chunk.file_path,
                        "symbol_name": chunk.symbol_name,
                        "language": chunk.language,
                        "line_start": chunk.line_start,
                        "line_end": chunk.line_end,
                        "content": chunk.content,
                        "embedding_text": _build_embedding_text(chunk),
                        "embedding": emb,
                        "content_hash": content_hash,
                    }
                )

            # 9. 事务内 upsert（删旧 + 写新）
            try:
                n = self.store.upsert_file_chunks(
                    self.repository_id, file_path, chunk_dicts
                )
                chunks_written += n
                indexed += 1
                existing_paths.discard(file_path)
            except Exception as e:
                print(f"  [跳过] 入库失败 {file_path}: {e}")
                skipped += 1
                continue

        # 10. 清理已删除文件的 chunk
        deleted = 0
        for stale_path in existing_paths:
            self.store.delete_file_chunks(self.repository_id, stale_path)
            deleted += 1

        return {
            "indexed": indexed,
            "skipped": skipped,
            "deleted": deleted,
            "chunks_written": chunks_written,
        }
