"""
admit.inbox —— 把 admit 接到"文件即队列"的信箱场景

这个模块是为 Operit 工作流 + dsh 信箱这套土法 Agent 写的适配层。
它假设消息是投放在某个目录里的文件，处理完移到 processed/。

之所以单独成模块而不是塞进 core：判断逻辑应当与具体投递方式无关，
今天用文件队列，明天换 HTTP 或 MQ，core 一行都不用改。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .core import Decision, Gate, Mode, Rule, Trigger, Verdict


DEFAULT_RULES_PATH = "admit_rules.json"


@dataclass
class InboxResult:
    total: int = 0
    executed: int = 0
    observed: int = 0      # 本会执行但因 observe 没做
    skipped: int = 0
    decisions: list[Decision] | None = None

    def summary(self) -> str:
        return (
            f"扫描 {self.total} 条｜执行 {self.executed}｜"
            f"观察 {self.observed}｜跳过 {self.skipped}"
        )


def load_rules(path: str | os.PathLike) -> list[dict]:
    """从 JSON 读取规则配置。

    没有配置文件不是错误——没有规则就意味着谁都不许动手，
    这是安全的默认值，不该抛异常让人去猜。
    """
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("rules", [])
    return list(data)


def build_gate(
    rules_conf: Sequence[dict],
    mode: Mode | str = Mode.OBSERVE,
    allowed: Sequence[str] | None = None,
) -> Gate:
    """把 JSON 描述转成 Gate。

    当前支持的规则类型刻意保持很少。规则种类越多，
    配置就越像一门编程语言，最后必然变成没人看得懂的 DSL。
    """
    rules: list[Rule] = []
    for item in rules_conf:
        kind = item.get("type", "sender_prefix")
        hint = item.get("hint", "")

        if kind == "sender_prefix":
            prefix = item.get("value", "")
            rules.append(
                Rule(
                    name=item.get("name", f"发送者前缀={prefix}"),
                    check=lambda t, p=prefix: t.sender.startswith(p),
                    hint=hint or f"要求发送者以 {prefix} 开头",
                )
            )
        elif kind == "filename_suffix":
            suffix = item.get("value", "")
            rules.append(
                Rule(
                    name=item.get("name", f"文件名后缀={suffix}"),
                    check=lambda t, s=suffix: str(t.meta.get("filename", "")).endswith(s),
                    hint=hint or f"要求文件名以 {s} 结尾",
                )
            )
        elif kind == "max_size":
            limit = int(item.get("value", 0))
            rules.append(
                Rule(
                    name=item.get("name", f"大小上限={limit}"),
                    check=lambda t, n=limit: int(t.meta.get("size", 0)) <= n,
                    hint=hint or f"要求文件不超过 {limit} 字节",
                )
            )
        elif kind == "not_contains":
            needle = item.get("value", "")
            rules.append(
                Rule(
                    name=item.get("name", f"不含={needle}"),
                    check=lambda t, s=needle: s not in str(t.payload or ""),
                    hint=hint or f"正文不得包含 {needle}",
                )
            )
        # 未知类型直接跳过而不是报错：配置文件写错了不该让整个循环停摆

    return Gate(rules=rules, mode=mode, allowed_senders=allowed)


def scan_inbox(inbox: str | os.PathLike, patterns: Sequence[str]) -> list[Path]:
    """扫描待处理文件，按修改时间升序（老的先处理）。"""
    root = Path(inbox)
    if not root.is_dir():
        return []
    found: list[Path] = []
    for child in root.iterdir():
        if not child.is_file():
            continue
        if any(child.name.endswith(p) for p in patterns):
            found.append(child)
    found.sort(key=lambda p: p.stat().st_mtime)
    return found


def run_once(
    inbox: str | os.PathLike,
    rules_conf: Sequence[dict],
    mode: Mode | str = Mode.OBSERVE,
    allowed: Sequence[str] | None = None,
    patterns: Sequence[str] = (".msg", ".md"),
    processed_dir: str | os.PathLike | None = None,
    sender: str = "dsh",
    executor=None,
    dry_run: bool = False,
) -> InboxResult:
    """跑一轮。返回统计，不打印任何东西——输出是调用方的事。"""
    root = Path(inbox)
    proc = Path(processed_dir) if processed_dir else root / "processed"
    gate = build_gate(rules_conf, mode=mode, allowed=allowed)

    files = scan_inbox(root, patterns)
    result = InboxResult(total=len(files), decisions=[])

    for path in files:
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            body = ""

        trigger = Trigger(
            sender=_sender_of(path, body, None),
            explicit=_is_explicit(body),
            source="inbox",
            payload=body,
            meta={"filename": path.name, "size": path.stat().st_size},
        )

        decision = gate.judge(trigger)
        result.decisions.append(decision)

        if decision.accepted:
            result.executed += 1
            if executor is not None and not dry_run:
                executor(path, body)
            if not dry_run:
                _archive(path, proc)
        elif decision.would_accept:
            result.observed += 1
            if not dry_run:
                _archive(path, proc)
        else:
            result.skipped += 1

    return result


def _archive(path: Path, proc: Path) -> None:
    proc.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(path), str(proc / path.name))
    except OSError:
        pass


def _is_explicit(body: str) -> bool:
    """消息是否明确点名要我做。

    这里只能用启发式——真实系统里这个字段应当由上游结构化给出。
    启发式存在的意义是：起码比"默认当成点名"安全。
    """
    markers = ("@大肥鱼", "@operit", "@assistant", "请处理", "请执行", "帮我")
    return any(m in body for m in markers)


UNKNOWN_SENDER = "unknown"


def _sender_of(path: Path, body: str, default: str | None = None) -> str:
    """从文件名或正文推断发起者。

    推断失败时返回 UNKNOWN_SENDER 而不是 default。这一点很关键：
    早期版本把认不出来的消息归到 default（通常是白名单里的 dsh），
    结果一个来源不明的文件也能通过准入检查。宁可多拒，不可错放。
    """
    name = path.name.lower()
    # 文件名里出现身份词才算，且要求出现在前两个词元里
    tokens = name.replace("-", "_").split("_")
    head_tokens = tokens[:2]
    for guess in ("dsh", "operit", "user", "system"):
        if guess in head_tokens:
            return guess
    # 正文开头声明身份的也算
    head = body[:200].lower()
    for guess in ("from dsh", "from operit", "发件人: dsh", "发件人：dsh"):
        if guess in head:
            return "dsh" if "dsh" in guess else "operit"
    return default or UNKNOWN_SENDER


def heartbeat(path: str | os.PathLike, tick: int, backlog: int, dead: int) -> None:
    """写心跳文件，供外部看门狗检查。

    单纯写时间戳不够用——看门狗需要知道"还活着但积压爆了"和
    "彻底没动静"是两回事，所以三个数字一起写。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "at": int(time.time()),
        "tick": tick,
        "backlog": backlog,
        "dead": dead,
        "alive": True,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")