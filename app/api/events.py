"""结构化事件协议 —— Tracer / Orchestrator → SSE 事件流。

- ``RunEvent`` ≈ 一条 SSE 推送消息的 DTO（统一信封：事件名 + 序号 + 负载）；
- ``EventType`` ：限定事件只能是这 16 个名字之一
- ``to_sse_dict`` 返回的 dict 会被 sse-starlette 渲染成 HTTP 帧文本：
      event: tool_started
      data: {"run_id": "...", ...}
  相当于 SseEmitter.event().name("tool_started").data(json)。
- ``RunState`` ≈ 任务域对象

设计原则：
- 每个事件有统一的公共字段（run_id, sequence, type, timestamp, data）
- 事件来源优先复用现有模块（Tracer、Orchestrator、Callback），不伪造
- 事件按 run_id 隔离，多任务互不串流
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- 事件类型
# 枚举所有允许推送前端的事件名称，强约束，防止随便乱写事件名
EventType = Literal[
    "run_started",       # 任务开始
    "repo_connected",    # 仓库连接成功
    "routing_selected",  # 路由策略选定（简化版，只有 strategy）
    "decision_event",    # 路由决策细粒度事件（route_decision 实时推：source/confidence/reason）
    "indexing_started",  # 索引开始
    "indexing_progress", # 索引进度（每 PROGRESS_EVERY 个文件推一次）
    "indexing_completed",# 索引完成
    "retrieval_completed",# RAG 检索完成
    "tool_started",      # 工具调用开始
    "tool_completed",    # 工具调用结束
    "llm_started",       # LLM 调用开始（model_start 实时推，前端可以显示"思考中..."）
    "llm_error",         # LLM 调用异常（model_error 实时推，前端知道哪个 LLM 崩了）
    "plan_event",        # Plan 模式的计划生成/验证事件（planner.py 实时推）
    "verification_updated",# 验证状态（orchestrator 返回后主动推的结论）
    "answer_delta",      # 回答增量（保留给将来的流式输出）
    "answer_final",      # 最终回答
    "usage_updated",     # Token 用量（model_end 实时推）
    "run_completed",     # 任务完成
    "run_failed",        # 任务失败
    "run_cancelled",     # 任务取消
    "info",              # 通用信息（verification_event 走这个）
]


class RunEvent(BaseModel):
    """统一事件模型：每一条发给前端的SSE消息都用这个结构"""

    run_id: str  # 任务唯一ID，用来区分不同会话，多任务隔离
    sequence: int  # 事件递增序列号，前端可用来排序、丢包校验
    type: EventType  # 事件类型，只能是上面Literal里面定义的值
    timestamp: float = Field(default_factory=time.time)  # 事件产生时间戳，自动填充
    data: dict[str, Any] = Field(default_factory=dict)   # 事件业务负载，放每个事件自定义数据

    def to_sse(self) -> str:
        """
        序列化成原生SSE帧字符串
        格式标准：event:xxx\ndata:xxx\n\n
        直接原生 SSE 协议使用，sse-starlette一般不用这个
        """
        payload = self.model_dump_json()
        return f"event: {self.type}\ndata: {payload}\n\n"

    def to_sse_dict(self) -> dict:
        """sse-starlette EventSourceResponse 需要的 dict 形式。

        注意：EventSourceResponse 会对 yield 的 str 再包一层 ``data: ``，
        直接 yield to_sse() 的字符串会导致双重包装/解析失败，必须用 dict。
        """
        return {
            "event": self.type,
            "data": self.model_dump_json(),
            "id": str(self.sequence),
        }


# --------------------------------------------------------------------------- 任务状态
# 任务状态枚举
RunStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


@dataclass
class RunState:
    """任务运行时状态对象，存在内存，保存单次任务全部上下文"""
    run_id: str = field(default_factory=lambda: f"run-{uuid.uuid4().hex[:12]}")
    status: RunStatus = "pending"
    question: str = ""
    repo_source: str = "local"       # local / github
    repo_identifier: str = ""        # 本地路径或 "owner/repo"
    repository_id: str = ""          # 仓库唯一资源id
    answer: str = ""
    strategy: str | None = None      # 当前选中的路由/编排策略名称
    start_time: float = 0.0
    end_time: float = 0.0
    tool_calls: list[dict] = field(default_factory=list)   # 本次任务所有工具调用记录
    error: str | None = None

    def elapsed_ms(self) -> float:
        """
        计算任务耗时，单位毫秒
        如果还没结束（end_time=0），就拿当前时间算实时耗时
        """
        end = self.end_time or time.time()
        return round((end - self.start_time) * 1000, 2)
