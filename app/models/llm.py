import os

import httpx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

#加载.env文件
load_dotenv()

# DashScope 是国内服务，不走梯子代理。
# httpx 默认会读 Windows 系统代理（梯子 127.0.0.1:7892），
# 若不禁用会被路由到代理 → TLS 超时 → OpenAIConnectionError。
_NO_PROXY_CLIENT = httpx.Client(trust_env=False, proxy=None)


def get_llm():
    return ChatOpenAI(
        model=os.getenv("DASHSCOPE_MODEL"),
        api_key=os.getenv("DASHSCOPE_API_KEY"),
        base_url=os.getenv("DASHSCOPE_BASE_URL"),
        http_client=_NO_PROXY_CLIENT,
        extra_body={
            "enable_thinking": False
        }
    )


def get_summarization_llm():
    """摘要压缩/复杂度路由判断专用的轻量模型。
    """
    return ChatOpenAI(
        model=os.getenv("SUMMARIZATION_MODEL", "qwen-flash"),
        api_key=os.getenv("DASHSCOPE_API_KEY"),
        base_url=os.getenv("DASHSCOPE_BASE_URL"),
        http_client=_NO_PROXY_CLIENT,
        temperature=0,
        extra_body={
            "enable_thinking": False
        }
    )
