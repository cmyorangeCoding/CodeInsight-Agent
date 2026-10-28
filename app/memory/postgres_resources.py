import os
from contextlib import contextmanager

import httpx
from dotenv import load_dotenv
from langchain_openai import OpenAIEmbeddings
# LangGraph Postgres 持久化：保存对话状态（checkpoint 断点）
from langgraph.checkpoint.postgres import PostgresSaver
# LangGraph Postgres 向量存储：用于长期记忆、向量检索记忆库
from langgraph.store.postgres import PostgresStore

#加载.env环境变量
load_dotenv()


def create_embeddings():
    """
    构造Embedding向量化实例（阿里云Dashscope兼容OpenAI接口）
    作用：把文本转为向量，用于PostgresStore向量检索记忆

    注意：禁用 httpx 默认代理 ——
      Windows 系统代理常设着梯子 127.0.0.1:7892，
      DashScope 是国内服务，不走梯子。
      若不禁用会被路由到代理 → TLS 超时 ConnectionError。
    """
    # 显式创建禁用代理的 httpx.Client：
    # trust_env=False → 不读 HTTP_PROXY / HTTPS_PROXY 环境变量
    # proxy=None      → 不读 Windows Internet Settings 系统代理
    no_proxy_client = httpx.Client(trust_env=False, proxy=None)

    return OpenAIEmbeddings(
        model=os.getenv(
            "EMBEDDING_MODEL",
            "text-embedding-v3",
        ),
        api_key=os.environ["DASHSCOPE_API_KEY"],
        base_url=(
            "https://dashscope.aliyuncs.com/"
            "compatible-mode/v1"
        ),
        dimensions=int(
            os.getenv("EMBEDDING_DIMENSIONS", "1024")
        ),
        # 关键：LangChain 默认会把文本先转成 token 数组再发送
        # (check_embedding_ctx_length=True)，但 DashScope 只接受原始字符串，
        # 否则会报 "contents is neither str nor list of str"。
        check_embedding_ctx_length=False,
        # 关键：禁用 httpx 默认代理 —— DashScope 国内服务不走梯子
        http_client=no_proxy_client,
    )


@contextmanager
def open_memory_resources():
    """
    上下文管理器：统一打开Postgres数据库资源，提供LangGraph记忆全套组件
    使用方式：with open_memory_resources() as (checkpointer, store, embeddings):
    退出with块时自动关闭数据库连接，释放资源，避免连接泄漏
    返回三元组：
        checkpointer: PostgresSaver 对话断点持久化（LangGraph线程状态、对话历史快照）
        store: PostgresStore 向量记忆存储（长期记忆，支持向量相似度检索）
        embeddings: 向量化实例，文本转向量
    """

    # 数据库连接字符串，从.env读取 DATABASE_URL，格式 postgresql://user:password@host:port/dbname
    db_url = os.environ["DATABASE_URL"]
    # 实例化向量模型
    embeddings = create_embeddings()

    # 嵌套with，同时管理 PostgresSaver 和 PostgresStore 的数据库连接
    with (
        PostgresSaver.from_conn_string(db_url) as checkpointer,
        PostgresStore.from_conn_string(
            db_url,
            index={
                "dims": int(
                    os.getenv("EMBEDDING_DIMENSIONS", "1024")
                ),
                "embed": embeddings,# 指定向量生成器
                "fields": ["text"],# 对store中记录的text字段做向量化，用于向量检索
            },
        ) as store,
    ):
        # 初始化组件需要的表和迁移。
        # 生产环境可将 setup 放入单独的数据库迁移步骤。
        checkpointer.setup()
        store.setup()

        # 把三个对象抛出给with代码块使用
        yield checkpointer, store, embeddings