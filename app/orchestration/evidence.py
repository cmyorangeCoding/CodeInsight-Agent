"""证据采集：统一工具结果格式 + 从工具输出中提取可核验证据引用。
基于 LangChain 的回调钩子，自动捕获每一次工具调用、提取证据引用（EvidenceRef），
打包成 ToolResult，供给 executor /verifier 使用

- ``EvidenceCollector`` 作为 LangChain 回调，捕获每次工具调用的
  名称 / 参数摘要 / 状态 / 耗时 / 结果摘要 / 错误，并解析出证据引用。
- 解析出的行号直接来自工具实际返回的文本（RAG/搜索工具会带行号），
  绝不由模型猜测。
- 每个记录通过线程局部的 ``step_id`` 归属到具体步骤，便于并行场景下
  各步骤独立保留自己的结果与证据。
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from app.orchestration.models import EvidenceRef, ToolResult

MAX_RESULT_PREVIEW = 600  # ToolResult.result_summary 最大长度
MAX_EVIDENCE_PER_CALL = 30 # 单次工具调用最多提取30条证据
MAX_EVIDENCE_SNIPPET = 300  # 证据片段snippet上限

# RAG 工具输出行：--- [1] app/models/llm.py:10-18 符号: get_llm (rrf=0.0328) ---
_RAG_HEADER_RE = re.compile(
    r"^---\s*\[(?P<idx>\d+)\]\s*(?P<path>[^\s:]+)"
    r"(?::(?P<start>\d+))?(?:-(?P<end>\d+))?"
    r"(?:\s*符号:\s*(?P<symbol>[^\s(]+))?"
)

# search_code 输出块：File: xxx / Line: N / <snippet>
_FILE_RE = re.compile(r"^File:\s*(?P<path>.+)$")
_LINE_RE = re.compile(r"^Line:\s*(?P<line>\d+)$")

# 通用路径识别（兜底）
_PATH_RE = re.compile(r"[\w./\\-]+\.(?:py|md|txt|toml|yml|yaml|json|java|js|ts)")


def _norm_path(path: str) -> str:
    """路径归一：统一为正斜杠，去掉 ./ 前缀与仓库文件夹名前缀。"""
    p = (path or "").strip().replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p


# 文本截断，超长内容末尾加省略号，控制 token
def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


#主证据提取入口
def extract_evidence(
    tool_name: str,
    args: dict[str, Any],
    output: str,
    step_id: str | None = None,
) -> list[EvidenceRef]:
    """根据工具类型，从输入参数与实际输出中解析证据引用。"""
    refs: list[EvidenceRef] = []

    if tool_name == "search_code_rag":
        refs = _parse_rag_output(output, step_id)
    elif tool_name == "search_code":
        refs = _parse_search_code_output(output, step_id)
    elif tool_name == "read_file":
        path = _norm_path(str(args.get("relative_path", "")))
        if path:
            refs.append(
                EvidenceRef(
                    file_path=path,
                    note="已读取文件完整内容",
                    source_tool=tool_name,
                )
            )

    # 兜底：工具输出里出现的路径，至少记为「文件级」证据
    if not refs and output:
        for path in dict.fromkeys(_PATH_RE.findall(output)):
            refs.append(
                EvidenceRef(
                    file_path=_norm_path(path),
                    note="工具输出中出现的文件路径",
                    source_tool=tool_name,
                )
            )

    return refs[:MAX_EVIDENCE_PER_CALL]


#专门解析 `search_code_rag` 工具返回的文本
def _parse_rag_output(output: str, step_id: str | None) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    current: EvidenceRef | None = None
    body: list[str] = []

    def flush() -> None:
        if current is not None and body:
            current.snippet = _clip("\n".join(body).strip(), MAX_EVIDENCE_SNIPPET)

    for line in (output or "").splitlines():
        m = _RAG_HEADER_RE.match(line.strip())
        if m:
            flush()
            body = []
            start = int(m.group("start")) if m.group("start") else None
            end = int(m.group("end")) if m.group("end") else None
            current = EvidenceRef(
                file_path=_norm_path(m.group("path")),
                symbol_name=m.group("symbol"),
                line_start=start,
                line_end=end,
                note="混合检索召回的代码片段",
                source_tool="search_code_rag",
            )
            refs.append(current)
        else:
            body.append(line)
    flush()
    return refs


#专门解析 `search_code` 的输出
def _parse_search_code_output(output: str, step_id: str | None) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    path: str | None = None
    line: int | None = None
    body: list[str] = []

    def flush() -> None:
        if path:
            refs.append(
                EvidenceRef(
                    file_path=path,
                    line_start=line,
                    snippet=_clip("\n".join(body).strip(), MAX_EVIDENCE_SNIPPET) or None,
                    note="精确文本匹配命中的代码行",
                    source_tool="search_code",
                )
            )

    for raw in (output or "").splitlines():
        fm = _FILE_RE.match(raw)
        lm = _LINE_RE.match(raw)
        if fm:
            flush()
            body = []
            path = _norm_path(fm.group("path"))
            line = None
        elif lm:
            line = int(lm.group("line"))
        elif path:
            body.append(raw)
    flush()
    return refs


class EvidenceCollector(BaseCallbackHandler):
    """线程安全的工具调用证据采集回调。"""

    def __init__(self) -> None:
        self._records: list[ToolResult] = []  #存放所有 ToolResult 记录
        self._pending: dict[str, dict[str, Any]] = {}  #保存正在执行、还没结束的工具调用临时信息，key=run_id
        self._lock = threading.Lock()  #线程锁，保护共享变量
        self._local = threading.local()  #线程局部存储对象

    # ------------------------------------------------------------- 步骤归属
    def set_step(self, step_id: str | None) -> None:
        """设置当前线程正在执行的步骤（并行时各线程互不影响）。"""
        self._local.step_id = step_id

    #读取当前线程绑定的 step_id
    def current_step(self) -> str | None:
        return getattr(self._local, "step_id", None)

    # ------------------------------------------------------------- 回调
    #工具开始执行时触发
    #上锁，把本次调用信息（工具名称、参数摘要、开始时间、step_id）放到`_pending`字典，run_id 作为 key
    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs) -> None:
        with self._lock:
            self._pending[str(run_id)] = {
                "tool": (serialized or {}).get("name", "unknown_tool"),
                "args_summary": _clip(str(input_str), 200),
                "start": time.perf_counter(),
                "step_id": self.current_step(),
            }

    def on_tool_end(self, output, *, run_id, **kwargs) -> None:
        self._finish(str(run_id), output=str(output), error=None)

    def on_tool_error(self, error, *, run_id, **kwargs) -> None:
        self._finish(str(run_id), output="", error=str(error))

    #【核心私有方法】工具结束（成功 / 失败都会进这里）
    def _finish(self, rid: str, output: str, error: str | None) -> None:
        with self._lock:
            info = self._pending.pop(rid, None) #从_pending 取出本次调用信息，并删掉 pending 记录
        if info is None:
            return
        elapsed_ms = round((time.perf_counter() - info["start"]) * 1000, 2)
        tool_name = info["tool"]
        args_summary = info["args_summary"]
        step_id = info.get("step_id")

        evidence: list[EvidenceRef] = []
        if error is None:
            # 参数摘要形如 "{'keyword': 'x'}"，尽力解析出原始参数用于提路径
            args = _parse_args_summary(args_summary)
            evidence = extract_evidence(tool_name, args, output, step_id)

        record = ToolResult(
            tool_name=tool_name,
            args_summary=args_summary,
            status="error" if error else "success",
            elapsed_ms=elapsed_ms,
            result_summary=_clip(output, MAX_RESULT_PREVIEW),
            evidence=evidence,
            error=error,
            step_id=step_id,
        )
        with self._lock:
            self._records.append(record)

    # ------------------------------------------------------------- 读取
    #读取收集到的 ToolResult 记录
    def records(self, step_id: str | None = None) -> list[ToolResult]:
        with self._lock:
            snapshot = list(self._records)
        if step_id is None:
            return snapshot
        return [r for r in snapshot if r.step_id == step_id]


    def all_evidence(self) -> list[EvidenceRef]:
        out: list[EvidenceRef] = []
        for r in self.records():
            out.extend(r.evidence)
        return out

    def evidence_for(self, step_id: str) -> list[EvidenceRef]:
        out: list[EvidenceRef] = []
        for r in self.records(step_id):
            out.extend(r.evidence)
        return out

    def tool_call_count(self) -> int:
        return len(self.records())

    def error_count(self) -> int:
        return sum(1 for r in self.records() if r.status == "error")


#把字符串形式的字典，还原成真实 python 字典。
def _parse_args_summary(args_summary: str) -> dict[str, Any]:
    """把 ``"{'keyword': 'x', 'max_results': 5}"`` 形式的摘要解析回字典。"""
    import ast

    try:
        value = ast.literal_eval(args_summary)
        if isinstance(value, dict):
            return value
    except (ValueError, SyntaxError):
        pass
    return {}
