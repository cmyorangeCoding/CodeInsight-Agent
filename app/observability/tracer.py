import time
import uuid
from contextlib import contextmanager
from threading import Lock
from typing import Callable, Any


class Tracer:
    """线程安全版本链路追踪器。

    设计要点：
    - 事件模型：一个 span 拆成 span_start + span_end / span_error 两条事件
    - 支持多线程并发打点；支持外部传入 trace_id；内置汇总统计
    - 可选 on_event 回调：如果设置，每次 add_event 时会实时调用它，
      调用方可以把事件推到 SSE 队列（实现"实时流式"）；不设置则只攒在 events 列表里
      （默认行为，向后兼容所有既有用法）
    """

    # on_event 签名：callable(event_type: str, event_data: dict[str, Any]) -> None
    # 设为 None 时不做实时推送；设置后每次 add_event 都会同步调用它
    _on_event: Callable[[str, dict[str, Any]], None] | None = None

    def __init__(
        self,
        trace_id: str | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        # 支持外部传入 trace_id，用于跨组件串联整条链路；不传则自动生成 uuid
        self.trace_id = trace_id or str(uuid.uuid4())
        # 存储所有事件列表（始终保留，用于事后汇总 / 离线分析 / summary()）
        self.events: list[dict[str, Any]] = []
        # 互斥锁，保护 events 读写，解决多线程并发竞态问题
        self._lock = Lock()
        # tracer 实例创建时刻，用于计算整条 trace 总耗时
        self.started_at = time.perf_counter()
        # 可选的实时事件回调（推荐通过构造器传入；也可在外部赋值覆盖）
        self._on_event = on_event

    def add_event(self, event_type: str, **data: Any) -> dict[str, Any]:
        """底层通用打点接口：新增一条事件。

        :param event_type: 事件类型（任意字符串，例如 tool_start / rag_hybrid_search）
        :param data: 事件携带的自定义元数据（关键字参数形式）
        :return: 刚刚构造好的事件 dict
        """
        event: dict[str, Any] = {
            "type": event_type,
            "timestamp": time.time(),  # 墙钟时间戳，便于日志离线排序
            **data,
        }
        # 锁保护：同一时刻只允许一个线程修改 self.events
        with self._lock:
            self.events.append(event)

        # ---- 实时推送到 SSE 队列（如果设置了回调） ----
        # 回调里禁止抛异常：一旦回调崩了，主业务流程也跟着挂 —— 兜底 try/except
        cb = self._on_event
        if cb is not None:
            try:
                # 把 type / timestamp 拿掉（bridge.emit 自己会填），只保留业务字段
                cb(
                    event_type,
                    {k: v for k, v in event.items() if k not in ("type", "timestamp")},
                )
            except Exception:
                # 实时推送失败绝不影响主流程；出错事件留在 events 列表里，事后可查
                pass

        return event


    @contextmanager
    def span(self, name: str, **metadata):
        """
        span上下文管理器，包裹一段业务代码
        进入with块 → 写入span_start事件
        正常退出 → 写入span_end，携带耗时
        异常退出 → 写入span_error，携带耗时+异常信息，并且向上抛出异常
        """
        # 记录span开始高精度时间
        start = time.perf_counter()
        # span启动事件，只记录名称与元数据，无耗时
        self.add_event("span_start", name=name, **metadata)
        try:
            yield   # 将控制权交给with内部业务代码，这里不返回event，无法在with内部修改元数据
        except Exception as exc:
            # 捕获异常：计算耗时，写入span_error事件
            elapsed_ms = round(
                (time.perf_counter() - start) * 1000,
                2
            )
            self.add_event(
                "span_error",
                name=name,
                elapsed_ms=elapsed_ms,
                error=str(exc),
            )
            raise  # 不吞异常，继续向上抛出
        else:
            # 无异常正常结束分支
            elapsed_ms = round(
                (time.perf_counter() - start) * 1000,
                2
            )
            self.add_event(
                "span_end",
                name=name,
                elapsed_ms=elapsed_ms,
                status="success",
            )

    def summary(self):
        """生成整条链路汇总统计，读取events前先加锁拷贝，避免遍历中别的线程修改列表"""
        with self._lock:
            events = list(self.events)  # 拷贝一份快照，防止遍历过程中列表被并发修改

        # 提取所有带elapsed_ms的span耗时，用于统计
        durations = [
            event["elapsed_ms"]
            for event in events
            if "elapsed_ms" in event
        ]
        return {
            "trace_id": self.trace_id,
            # tracer实例创建到当前时刻总耗时
            "total_elapsed_ms": round(
                (time.perf_counter() - self.started_at) * 1000, 2
            ),
            "event_count": len(events),
            # 统计error类型事件总数
            "error_count": sum(
                event["type"].endswith("_error")
                for event in events
            ),
            "recorded_span_ms": round(sum(durations), 2),
            "events": events,
        }

    def print_trace(self):
        """打印追踪链路，先拿到summary快照再输出"""
        result = self.summary()
        print(f"\nTrace ID: {result['trace_id']}")
        print(f"Total time: {result['total_elapsed_ms']} ms")
        print(f"Events: {result['event_count']}")
        print(f"Errors: {result['error_count']}")
        for event in result["events"]:
            # 打印时隐藏type/timestamp，只输出业务信息
            details = {
                key: value
                for key, value in event.items()
                if key not in ("type", "timestamp")
            }
            print(f"[{event['type']}] {details}")
