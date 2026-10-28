from dataclasses import dataclass
from app.observability.token_budget import TokenBudget
from typing import Optional


# 使用dataclass简化数据类，用来封装单条代码上下文信息
@dataclass
class ContextItem:
    # 相对仓库的文件路径
    file: str
    # 匹配关键词所在行号
    line: int
    # 匹配到的那一行原始内容
    content: str
    # 匹配行附带的前后代码片段（上下文）
    snippet: str
    # 相关性打分，默认0分
    score: int = 0


class ContextBuilder:

    def __init__(self,token_budget: Optional[TokenBudget] = None,max_chars: int = 12000):
        # 上下文总字符上限，防止拼接后文本过长，超出LLM输入窗口
        self.max_chars = max_chars
        # 外部注入：同一个任务共用一个budget实例
        self.token_budget = token_budget

    def build(self, results: list[dict]) -> str:
        """
        把search_code返回的匹配结果列表，组装成一段可供LLM直接使用的代码上下文字符串
        :param results: RepositoryAnalyzer.search_code输出的匹配结果字典列表
        :return: 拼接完成的上下文文本
        """

        if not results:
            return "没有可用的代码上下文。"

        items = []

        # 检索结果字典，转为 `ContextItem` 对象
        for result in results:
            item = ContextItem(
                file=result["file"],
                line=result["line"],
                content=result["content"],
                snippet=result["snippet"],
            )

            #计算相关性分数
            item.score = self._calculate_score(item)
            items.append(item)

        # 相关性高的结果排前面
        items.sort(
            key=lambda item: item.score,
            reverse=True
        )

        selected = []
        current_chars = 0

        for item in items:

            text = (
                f"File: {item.file}\n"
                f"Line: {item.line}\n"
                f"{item.snippet}\n"
                f"\n"
            )

            #累加字符长度，一旦加上当前片段会超过 `max_chars`，立刻停止
            if current_chars + len(text) > self.max_chars:
                break

            selected.append(text)
            current_chars += len(text)

        context="\n".join(selected)

        # 只有传入budget的时候，才执行校验
        if self.token_budget is not None:
            self.token_budget.check_context(context)

        return context


    # 相关性打分规则
    @staticmethod
    def _calculate_score(item: ContextItem) -> int:
        score = 0

        # 命中行本身有内容
        if item.content:
            score += 1

        # 类、方法、函数定义通常更有价值
        keywords = [
            "class ",
            "def ",
            "function ",
            "public ",
            "private ",
            "protected ",
        ]

        # 命中关键字+2
        for keyword in keywords:
            if keyword in item.snippet:
                score += 2

        return score