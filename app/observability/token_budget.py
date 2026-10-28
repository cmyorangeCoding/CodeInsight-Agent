from dataclasses import dataclass, field
from typing import Any
import tiktoken
import threading


def estimate_tokens(text: str) -> int:
    """估算文本 Token 数，不保证与 Qwen 服务端计数完全一致。"""
    try:
        # cl100k_base：GPT3.5/GPT4所用编码，本地用来预估算
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        # 计数库异常时使用粗略兜底，避免预算检查直接失效
        return max(1, len(text) // 2)


@dataclass
class TokenBudget:
    """
    Token预算管理类，适配多Agent、并行工具调用
    一个用户任务 = 一个TokenBudget实例，任务结束销毁
    """
    # 单次任务：输入+输出总token上限
    max_total_tokens: int = 30000
    # 单次LLM调用，输入上下文最大上限
    max_input_tokens: int = 12000
    # 默认预留输出token（实例全局配置）
    reserved_output_tokens: int = 2000

    # ---- 预估值（本地估算，调用LLM前） ----
    estimated_input_tokens: int = 0         # 【本次】check_context 算出的预估输入token（覆盖）
    cumulative_estimated_input: int = 0     # 【全任务累计】所有check的预估输入之和，仅参考

    # ---- 真实值（LLM服务端返回，调用成功后记录，可信） ----
    actual_input_tokens: int = 0
    actual_output_tokens: int = 0

    warnings: list[str] = field(default_factory=list)
    # 线程锁，并行工具/多Agent共享同一个budget实例时保护读写。
    # 使用可重入锁 RLock：summary() 持锁期间会调用 actual_total_tokens 属性，
    # 该属性内部也会请求同一把锁，普通 Lock 会导致死锁。
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    @property
    def actual_total_tokens(self) -> int:
        """全任务累计真实总token消耗（输入+输出）"""
        with self._lock:
            return self.actual_input_tokens + self.actual_output_tokens

    def check_context(
        self,
        text: str,
        reserved_output_tokens: int | None = None,
    ) -> int:
        """
        【LLM调用前执行】上下文token预检查
        :param text: 准备发给LLM的完整prompt/上下文文本
        :param reserved_output_tokens: 临时覆盖本次预留输出token，不传使用实例默认值
        :return: 本次估算输入token数量
        """
        input_tokens = estimate_tokens(text)
        # 使用实例默认值，可临时覆盖
        reserved = reserved_output_tokens if reserved_output_tokens is not None else self.reserved_output_tokens

        with self._lock:
            # 本次预估值，覆盖，用于本次校验
            self.estimated_input_tokens = input_tokens
            # 累计预估，不断累加
            self.cumulative_estimated_input += input_tokens

            # 校验1：单次输入不能超过单次输入上限
            if input_tokens > self.max_input_tokens:
                raise ValueError(
                    f"上下文超单次输入上限：估算 {input_tokens} tokens，上限 {self.max_input_tokens}。"
                    "请缩减搜索结果或分批读取文件。"
                )
            # 校验2：本次输入+预留输出，不能超过任务总预算
            if input_tokens + reserved > self.max_total_tokens:
                raise ValueError(
                    "输入上下文加预留输出已超过任务总预算，请缩减上下文或调整预算。"
                )
        return input_tokens

    def record_usage(self, input_tokens: int, output_tokens: int) -> None:
        """
        【LLM调用成功后执行】记录服务端返回真实token用量，累加
        :param input_tokens: LLM返回prompt_tokens
        :param output_tokens: LLM返回completion_tokens
        """
        with self._lock:
            self.actual_input_tokens += max(0, input_tokens)
            self.actual_output_tokens += max(0, output_tokens)

            total = self.actual_input_tokens + self.actual_output_tokens
            if total > self.max_total_tokens:
                msg = f"任务 Token 预算已超出：实际累计 {total}/{self.max_total_tokens}"
                if msg not in self.warnings:
                    self.warnings.append(msg)

    def summary(self) -> dict[str, Any]:
        """生成预算汇总报告，埋点/打印日志"""
        with self._lock:
            return {
                "estimated_input_tokens": self.estimated_input_tokens,
                "cumulative_estimated_input": self.cumulative_estimated_input,
                "actual_input_tokens": self.actual_input_tokens,
                "actual_output_tokens": self.actual_output_tokens,
                "actual_total_tokens": self.actual_total_tokens,
                "max_total_tokens": self.max_total_tokens,
                "max_input_tokens": self.max_input_tokens,
                "reserved_output_tokens": self.reserved_output_tokens,
                "over_budget": self.actual_total_tokens > self.max_total_tokens,
                "warnings": list(self.warnings),
            }
