
from langchain.agents.middleware import AgentMiddleware


class TokenBudgetExceededError(RuntimeError):
    """Token 预算不足，拒绝继续调用模型。"""


class TokenBudgetMiddleware(AgentMiddleware):
    """在每次模型调用前检查输入和任务累计 Token 预算。"""

    def __init__(self, token_budget):
        super().__init__()
        # 注入当前任务的TokenBudget实例，一个任务对应一份预算
        self.token_budget = token_budget

    @staticmethod
    def _serialize_messages(messages) -> str:
        """将本次模型请求的消息转换为用于 Token 估算的文本。"""
        parts = []

        for message in messages:
            role = getattr(message, "type", message.__class__.__name__)
            content = getattr(message, "content", str(message))

            parts.append(f"{role}: {content}")

            # 保留工具调用信息，避免遗漏部分模型上下文。
            tool_calls = getattr(message, "tool_calls", None)
            if tool_calls:
                parts.append(f"tool_calls: {tool_calls}")

        return "\n".join(parts)

    @staticmethod
    def _serialize_tools(tools) -> str:
        """提取工具名称和描述，纳入输入预算估算。"""
        parts = []

        for tool in tools or []:
            if isinstance(tool, dict):
                name = tool.get("name", "unknown_tool")
                description = tool.get("description", "")
            else:
                name = getattr(tool, "name", "unknown_tool")
                description = getattr(tool, "description", "")

            parts.append(f"{name}: {description}")

        return "\n".join(parts)

    def wrap_model_call(self, request, handler):

        """
        中间件核心钩子：拦截模型调用请求
        拿到 LLM 请求对象 → 把消息对象列表 → 转字符串；
        把工具对象列表 → 转字符串 → 合并成完整 prompt 文本 → 估算 token 数量 → 预算校验。
        request: 本次LLM请求对象，包含messages、tools等
        handler: 下游处理器，调用handler(request)才会真正发起LLM请求
        """

        budget = self.token_budget

        # 1. 第一步：先看这个任务历史累计token是否已经超预算
        report = budget.summary()

        if report["over_budget"]:
            raise TokenBudgetExceededError(
                "任务累计 Token 已超出预算，已拦截后续模型调用。"
            )

        # 2. 序列化本次请求的消息 + 工具描述，拼成完整prompt文本用于估算token
        messages_text = self._serialize_messages(request.messages)
        tools_text = self._serialize_tools(
            getattr(request, "tools", None)
        )
        prompt_text = "\n".join(
            part for part in (messages_text, tools_text) if part
        )

        # 3. 调用budget的check_context做校验：
        # ① 本次输入token上限 ② 预留输出token ③ 任务总剩余预算
        try:
            budget.check_context(prompt_text)
        except ValueError as e:
            raise TokenBudgetExceededError(str(e)) from e

        # 4. 只有预算检查通过，才允许真正执行模型请求。
        return handler(request)