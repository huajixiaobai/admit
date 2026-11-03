"""
admit —— 给自建 Agent 的"该不该主动动手"判断层

纯 Python 标准库实现，零依赖。灵感来自 DeepSeek Super-agent 的
policy.gate() 四象限准入矩阵与 off/observe/auto 三档模式，
但代码为独立重写，不包含任何来自原项目的代码。

核心想法：一个会自己动手的 Agent，缺的从来不是"能不能做"，
而是"该不该做"。这个库只负责回答后一个问题。
"""

from .core import (
    Decision,
    Gate,
    Mode,
    Rule,
    RuleResult,
    Trigger,
    Verdict,
)

__all__ = [
    "Decision",
    "Gate",
    "Mode",
    "Rule",
    "RuleResult",
    "Trigger",
    "Verdict",
]

__version__ = "0.1.0"
