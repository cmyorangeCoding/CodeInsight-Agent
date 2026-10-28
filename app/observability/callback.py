from time import perf_counter
from langchain_core.callbacks import BaseCallbackHandler
import threading


class TraceCallback(BaseCallbackHandler):
    """
    LangChain 自定义回调处理器，对接线程安全版本Tracer链路追踪器
    作用：捕获LangChain LLM / Tool 的生命周期事件，自动打点、计算耗时；
    同时对接TokenBudget，拿到LLM返回的真实token用量并累计。
    原理：LangChain每个执行单元(llm/tool)都有唯一run_id，用来配对start和end事件
    """
    def __init__(self, tracer, token_budget=None):
        self.tracer = tracer          # 外部传入的 Tracer实例，用来上报事件
        self.token_budget = token_budget # 可选，TokenBudget实例，统计token消耗
        self._starts = {}              # 缓存run_id对应的开始时间，用来计算耗时
        self._lock = threading.Lock() # 保护_starts字典，并行场景防竞态

    def _start(self, run_id, event_type, **data):
        """通用开始打点函数：记录当前run_id的起始时间，上报start事件"""
        rid = str(run_id)
        with self._lock:
            self._starts[rid] = perf_counter()
        # 调用tracer.add_event写入起始事件
        self.tracer.add_event(event_type,** data)

    def _end(self, run_id, event_type, **data):
        """通用结束打点函数：取出run_id的起始时间，计算耗时，上报end/error事件"""
        rid = str(run_id)
        start = None
        with self._lock:
            start = self._starts.pop(rid, None)
        # 如果存在起始时间，计算毫秒耗时；不存在则elapsed_ms=None
        elapsed_ms = (
            round((perf_counter() - start) * 1000, 2)
            if start is not None
            else None
        )
        # 上报结束事件，附带耗时和自定义数据
        self.tracer.add_event(
            event_type,
            elapsed_ms=elapsed_ms,
            **data,
        )

    def on_tool_start(
        self, serialized, input_str, *, run_id, **kwargs
    ):
        """工具调用开始回调：LangChain执行Tool前触发"""
        self._start(
            run_id,
            "tool_start",
            tool=serialized.get("name", "unknown_tool"), # 获取工具名
            input_preview=str(input_str)[:300],          # 输入预览，截断防止日志过大
        )

    def on_tool_end(self, output, *, run_id, **kwargs):
        """工具调用正常结束回调：工具成功返回结果触发"""
        self._end(
            run_id,
            "tool_end",
            output_preview=str(output)[:300], # 输出预览截断
        )

    def on_tool_error(self, error, *, run_id, **kwargs):
        """工具调用异常回调：工具抛出异常时触发"""
        self._end(
            run_id,
            "tool_error",
            error=str(error),
        )

    def on_chat_model_start(
        self, serialized, messages, *, run_id, **kwargs
    ):
        """ChatModel（对话模型，ChatOpenAI/ChatQwen等）开始回调"""
        # messages是二维列表，统计消息总条数
        message_count = sum(len(batch) for batch in messages)
        self._start(
            run_id,
            "model_start",
            # 从模型配置里提取模型名称，兼容两种key：model_name / model
            model=serialized.get("kwargs", {}).get("model_name")
                  or serialized.get("kwargs", {}).get("model"),
            message_count=message_count,
        )

    def on_llm_start(
        self, serialized, prompts, *, run_id, **kwargs
    ):
        """底层LLM回调（非chat原始字符串prompt模式），兼容老的LLM接口"""
        self._start(
            run_id,
            "model_start",
            prompt_count=len(prompts),
        )

    def on_llm_end(self, response, *, run_id, **kwargs):
        """LLM调用正常结束回调：拿到模型返回response，提取token用量"""
        input_tokens = 0
        output_tokens = 0
        # 分支1：新版LangChain，usage_metadata挂载在AIMessage上（推荐）
        for generation_list in response.generations:
            for generation in generation_list:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None) or {}
                input_tokens += usage.get("input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)

        # 分支2：兜底兼容旧版本LangChain，token_usage放在llm_output
        if input_tokens == 0 and output_tokens == 0:
            llm_output = response.llm_output or {}
            usage = llm_output.get("token_usage") or {}
            input_tokens = usage.get(
                "prompt_tokens", usage.get("input_tokens", 0)
            )
            output_tokens = usage.get(
                "completion_tokens", usage.get("output_tokens", 0)
            )
        # 上报model_end事件，附带token统计
        self._end(
            run_id,
            "model_end",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        # 如果传入TokenBudget实例，则累加真实token用量
        if self.token_budget is not None:
            self.token_budget.record_usage(
                input_tokens, output_tokens
            )

    def on_llm_error(self, error, *, run_id, **kwargs):
        """LLM调用异常回调，模型请求失败触发"""
        self._end(
            run_id,
            "model_error",
            error=str(error),
        )
