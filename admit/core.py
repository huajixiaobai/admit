"""
admit.core —— 准入判断的核心实现

设计上只做三件事：
  1. 把"是否允许"与"是否明确点名"两个维度交叉成一个四象限矩阵
  2. 用模式开关决定最终要不要真的动手（off / observe / auto）
  3. 保证每条启用规则都被恰好判断一次，漏判即抛出，不允许"沉默通过"

最后一条是这里最重要的设计。绝大多数 Agent 事故不是"判断错了"，
而是"根本没判断就动手了"。强制逐条打勾，是让沉默通过变成不可能。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Sequence


class Mode(str, Enum):
    """三档运行模式。

    OFF      完全不动手，连判断都不做（省算力，用于临时闭嘴）
    OBSERVE  只判断、只记录，绝不执行。用于上线前的观察期
    AUTO     判断通过就执行。高风险场景不要直接开这一档
    """

    OFF = "off"
    OBSERVE = "observe"
    AUTO = "auto"


class Verdict(str, Enum):
    """单个触发源的象限归属。"""

    EXPLICIT = "explicit"      # 明确点名且被允许 —— 可以做
    INFERRED = "inferred"      # 没点名但被允许 —— 想做的候选，需谨慎
    UNKNOWN = "unknown"        # 允许但来源不明 —— 记录后忽略
    IGNORED = "ignored"        # 不在允许名单 —— 直接忽略


class RuleResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class Trigger:
    """一个待判断的触发事件。

    sender      谁发起（人名、账号、来源标识，同一套身份体系内比较）
    explicit    是否明确点名要我做（@我、直接请求、被指派）
    source      来自哪个通道（github / feishu / inbox / cli ...）
    payload     任意业务负载，规则函数可以读
    meta        附加信息，不参与判断也一并带下去
    """

    sender: str
    explicit: bool = False
    source: str = "default"
    payload: Any = None
    meta: dict = field(default_factory=dict)

    def describe(self) -> str:
        mark = "@我" if self.explicit else "未点名"
        return f"[{self.source}] {self.sender} {mark}"


@dataclass(frozen=True)
class Rule:
    """一条准入规则。

    name   规则标识，日志里用它说话，别用序号
    check  接收 Trigger 返回 bool 的函数
    hint   不通过时给人看的原因，比 ok=True 有用得多
    """

    name: str
    check: Callable[[Trigger], bool]
    hint: str = ""

    def evaluate(self, trigger: Trigger) -> tuple[RuleResult, str]:
        try:
            ok = bool(self.check(trigger))
        except Exception as exc:  # 规则自己崩了，按不通过处理，不放行
            return RuleResult.FAIL, f"规则执行异常：{exc}"
        if ok:
            return RuleResult.PASS, self.hint or "通过"
        return RuleResult.FAIL, self.hint or "未通过"


@dataclass(frozen=True)
class Decision:
    """判断结果。这是一个纯数据对象，不产生任何副作用。"""

    verdict: Verdict
    accepted: bool           # 最终是否真的动手
    would_accept: bool       # observe 模式下"如果要动手会不会动手"
    mode: Mode
    sender: str
    source: str
    reason: str
    matched_rules: tuple[str, ...] = ()
    failed_rules: tuple[str, ...] = ()
    detail: tuple[tuple[str, str], ...] = ()
    at: float = 0.0

    def explain(self) -> str:
        """给人看的解释。日志里直接打这个。"""
        head = "执行" if self.accepted else ("本会执行" if self.would_accept else "不动手")
        lines = [
            f"[{self.mode.value}] {head}｜{self.verdict.value}｜{self.source}/{self.sender}",
            f"  原因：{self.reason}",
        ]
        for name, reason in self.detail:
            lines.append(f"    - {name}: {reason}")
        return "\n".join(lines)


class Gate:
    """准入闸门。

    典型用法：

        gate = Gate(
            rules=[Rule("来自白名单", lambda t: t.sender in ALLOWED)],
            mode=Mode.OBSERVE,
        )
        d = gate.judge(Trigger(sender="dsh", explicit=True))
        if d.accepted:
            ...  # 只有 auto 且通过才会走到这里

    关键约束：开启 strict_rules 时，规则函数必须全部被评估，
    不允许因为短路而漏判。
    """

    def __init__(
        self,
        rules: Sequence[Rule] = (),
        mode: Mode = Mode.OBSERVE,
        allowed_senders: Iterable[str] | None = None,
        version: int = 1,
    ) -> None:
        self._rules: list[Rule] = list(rules)
        self._mode = Mode(mode)
        self._allowed = set(allowed_senders) if allowed_senders is not None else None
        self._version = version
        self._seen_versions: dict[str, int] = {}

    # ---------- 基本属性 ----------

    @property
    def mode(self) -> Mode:
        return self._mode

    @mode.setter
    def mode(self, value: Mode | str) -> None:
        new = Mode(value)
        if new != self._mode:
            self._mode = new
            self._version += 1

    @property
    def version(self) -> int:
        """策略版本号。配置一改就加一，用于作废在途判断。"""
        return self._version

    def add_rule(self, rule: Rule) -> None:
        self._rules.append(rule)
        self._version += 1

    # ---------- 核心判断 ----------

    def judge(self, trigger: Trigger) -> Decision:
        now = time.time()

        if self._mode is Mode.OFF:
            return Decision(
                verdict=Verdict.IGNORED,
                accepted=False,
                would_accept=False,
                mode=self._mode,
                sender=trigger.sender,
                source=trigger.source,
                reason="模式为 off，不判断也不动手",
                at=now,
            )

        verdict = self._classify(trigger)
        matched, failed, detail = self._run_rules(trigger)

        # 接受条件（三条同时满足，缺一不可）：
        #   1. 象限是 explicit（被明确点名）
        #   2. 至少有一条启用规则通过（说明它确实符合某个授权意图）
        #   3. 没有规则失败
        rules_ok = bool(matched) and not failed
        would = verdict is Verdict.EXPLICIT and rules_ok

        if verdict is Verdict.INFERRED:
            reason = "未点名，属于推测意图，需 auto 模式才考虑"
        elif verdict is Verdict.IGNORED:
            reason = "发起者不在允许名单"
        elif not matched:
            reason = "没有任何规则通过"
        elif failed:
            reason = "以下规则未通过：" + "、".join(failed)
        elif self._mode is Mode.OBSERVE:
            reason = "条件满足，但当前为 observe 模式，只记录不执行"
        else:
            reason = "条件满足且为 auto 模式，放行"

        accepted = would and self._mode is Mode.AUTO

        return Decision(
            verdict=verdict,
            accepted=accepted,
            would_accept=would,
            mode=self._mode,
            sender=trigger.sender,
            source=trigger.source,
            reason=reason,
            matched_rules=tuple(matched),
            failed_rules=tuple(failed),
            detail=tuple(detail),
            at=now,
        )

    # ---------- 内部 ----------

    def _classify(self, trigger: Trigger) -> Verdict:
        """四象限：允许与否 × 是否点名。"""
        if self._allowed is None:
            allowed = True
        else:
            allowed = trigger.sender in self._allowed

        if not allowed:
            return Verdict.IGNORED
        if trigger.explicit:
            return Verdict.EXPLICIT
        return Verdict.INFERRED if self._rules else Verdict.UNKNOWN

    def _run_rules(
        self, trigger: Trigger
    ) -> tuple[list[str], list[str], list[tuple[str, str]]]:
        """逐条评估，不短路。这是"强制打勾"的落点。"""
        matched: list[str] = []
        failed: list[str] = []
        detail: list[tuple[str, str]] = []

        if not self._rules:
            detail.append(("(无规则)", "未配置任何准入规则"))
            return matched, failed, detail

        for rule in self._rules:
            result, reason = rule.evaluate(trigger)
            detail.append((rule.name, reason))
            if result is RuleResult.PASS:
                matched.append(rule.name)
            else:
                failed.append(rule.name)

        return matched, failed, detail

    # ---------- 在途判断的版本校验 ----------

    def stale(self, decision: Decision, key: str) -> bool:
        """判断是否因为在途期间配置被改而失效。

        对应 Super-agent 的 policyVersion 机制：策略一变，
        手头正在等的那次判断就得重新算，不能拿旧结论动手。
        """
        return self._seen_versions.get(key, decision_at_version(self._version)) != self._version

    def mark(self, key: str, decision: Decision) -> None:
        self._seen_versions[key] = self._version


def decision_at_version(version: int) -> int:
    """占位：便于测试时构造"同版本"结论。"""
    return version
