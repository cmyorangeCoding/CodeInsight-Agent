"""仓库索引与 Provider 构建的共享模块（原 app/main.py 中的非 CLI 部分迁移至此）。

为什么需要这个模块：
- ``ensure_index`` 是"切片 + 向量化 + 入库"的唯一触发点，FastAPI 后端（app/api/main.py）
  和评测运行器（evals/run_eval.py）都要用；
- ``_build_local_provider`` 是"从配置构建本地仓库 Provider"的统一入口；
- 而原 app/main.py 同时还是一个交互式命令行入口（input() 对话循环），
  Web 化之后 CLI 已废弃，为避免"删入口文件时把共享函数一起删掉"，
  先把共享逻辑抽到独立模块。

给 SpringBoot 开发者的类比：
- 本模块 ≈ 一个 @Service 工具类（只提供无状态的装配方法，不持有请求态）；
- ``LocalRepositoryProvider`` ≈ 一个"仓库连接"的 Bean，每次任务新建一个实例，
  类似为每个请求创建一个 Connection，避免多任务互相串用仓库；
- ``ensure_index`` ≈ 幂等的资源初始化（类似 Flyway migrate：有索引就跳过，没有才建）。
"""
from __future__ import annotations

import os

from app.config import settings
from app.repository import LocalRepositoryProvider


def build_local_provider(repo_path: str | None = None, repository_id: str | None = None):
    """构建本地仓库 Provider（原 ``_build_local_provider``，去下划线改为公开函数）。

    Provider 是"仓库访问"的统一抽象：本地仓库、GitHub 远程仓库都实现同一套接口
    （get_repo_tree / read_file / search_code ...），上层工具与编排层不关心背后是哪种来源。

    参数：
        repo_path: 本地仓库路径；不传则用配置里的默认路径，配置也没有就兜底当前工作目录
        repository_id: 向量库里的仓库标识（同一标识的索引会被复用）；不传用配置默认值
    """
    if repo_path is None:
        repo_path = settings.repo.default_local_path
    if not repo_path:
        # 兜底：当前工作目录
        repo_path = os.getcwd()
    rid = repository_id or settings.repo.default_repository_id
    return LocalRepositoryProvider(repo_path, repository_id=rid)


def ensure_index(session, embeddings, provider, force: bool = False, progress_callback=None) -> None:
    """确保仓库已完成向量索引（幂等）。

    这是切片 + 向量化 + 入库的唯一触发点：
      - 数据库中还没有索引 → 建立全量索引（130 个文件约几分钟，取决于 embedding 速度）
      - 已有索引且未强制 → 增量检查（按 file hash 判断变更）
      - force=True → 全量重建（先清所有 chunk 再重建）

    参数（类比一次 Service 调用的依赖注入）：
        session:             SQLAlchemy DB Session —— pgvector 表的读写通道（类比 MyBatis SqlSession）
        embeddings:          Embedding 模型客户端 —— 把代码文本转成向量（调用 DashScope）
        provider:            RepositoryProvider —— 告诉索引器"索引哪个仓库、文件从哪读"
        force:               是否强制全量重建
        progress_callback:   可选回调 callable(seq, total, file_path, indexed, skipped)
                             —— 用于前端 SSE 推送索引进度
    """
    from app.rag.code_vector_store import CodeVectorStore
    from app.rag.ingestion import RepositoryIndexer
    from app.tools.repository import RepositoryAnalyzer

    # LocalRepositoryProvider 内部持有 RepositoryAnalyzer（本地文件读取器）；
    # 其他 provider（如未克隆的 GitHub provider）没有该属性，则现场包一个
    analyzer = (
        provider._analyzer
        if hasattr(provider, "_analyzer")
        else RepositoryAnalyzer(provider.repo_path)
    )
    store = CodeVectorStore(session)
    indexer = RepositoryIndexer(
        repository=analyzer,
        store=store,
        embeddings=embeddings,
        repository_id=provider.repository_id,
    )

    # force=True → 全量重建（先清所有 chunk）
    # force=False → 增量检查（index_repository 内部按 file hash 判断：未变更跳过、变更/新增重索引、删除清理）
    existing = store.count_chunks(provider.repository_id)
    if force and existing > 0:
        store.delete_repository_chunks(provider.repository_id)
        print(f"✓ 强制重建索引：清除了 {existing} 个旧 chunk")

    if existing > 0 and not force:
        print(f"开始增量索引（已有 {existing} 个 chunk，按文件 hash 判断变更）...")
    else:
        print("开始建立代码索引（切片 + 向量化 + 入库，可能需要等一会儿）...")

    stats = indexer.index_repository(progress_callback=progress_callback)
    print(
        f"✓ 索引完成：索引 {stats['indexed']} 个文件，"
        f"跳过 {stats['skipped']} 个，清理 {stats['deleted']} 个，"
        f"写入 {stats['chunks_written']} 个 chunk"
    )
