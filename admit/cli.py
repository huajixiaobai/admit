"""
admit.cli —— 命令行入口，供 shell 脚本或工作流直接调用

用法：
    python3 -m admit.cli run --inbox /sdcard/dsh_inbox --mode observe
    python3 -m admit.cli judge --sender dsh --explicit
    python3 -m admit.cli selftest

退出码约定（供 shell 判断）：
    0  正常完成，且有动作被（或本会被）执行
    1  正常完成，但什么都没做
    2  参数或配置错误
    3  内部异常
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from .core import Gate, Mode, Rule, Trigger
from .inbox import DEFAULT_RULES_PATH, heartbeat, load_rules, run_once


def _cmd_run(args: argparse.Namespace) -> int:
    conf = load_rules(args.rules) if args.rules else []
    allowed = args.allow.split(",") if args.allow else None

    result = run_once(
        inbox=args.inbox,
        rules_conf=conf,
        mode=args.mode,
        allowed=allowed,
        patterns=tuple(args.patterns.split(",")),
        sender=args.sender,
        dry_run=args.dry_run,
    )

    print(f"[{args.mode}] {result.summary()}")

    if args.verbose and result.decisions:
        for d in result.decisions:
            print(d.explain())
            print()

    # 心跳与后续告警交给外部，这里只保证数据被写下来
    state = Path(args.inbox) / ".state"
    ticks = 0
    tick_file = state / "total_ticks"
    if tick_file.exists():
        try:
            ticks = int(tick_file.read_text().strip() or "0")
        except ValueError:
            ticks = 0
    ticks += 1
    state.mkdir(parents=True, exist_ok=True)
    tick_file.write_text(str(ticks))

    dead_dir = Path(args.inbox) / "dead"
    dead = len(list(dead_dir.iterdir())) if dead_dir.is_dir() else 0
    heartbeat(state / "health.json", ticks, result.total, dead)

    return 0 if (result.executed or result.observed) else 1


def _cmd_judge(args: argparse.Namespace) -> int:
    conf = load_rules(args.rules) if args.rules else []
    allowed_set = set(args.allow.split(",")) if args.allow else None

    # 这里刻意不复用 inbox.build_gate：命令行单次判断应该更直接
    from .inbox import build_gate
    gate = build_gate(conf, mode=args.mode, allowed=list(allowed_set) if allowed_set else None)

    trigger = Trigger(sender=args.sender, explicit=args.explicit, source="cli")
    decision = gate.judge(trigger)
    print(decision.explain())
    return 0 if decision.accepted else 1


def _cmd_selftest(_args: argparse.Namespace) -> int:
    """内置自检。没有测试框架也能跑的验证。"""
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            failures.append(name)
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")

    allow_rule = Rule("来自dsh", lambda t: t.sender == "dsh", "只有 dsh 可以发起")
    gate = Gate(rules=[allow_rule], mode=Mode.OBSERVE, allowed_senders=["dsh", "user"])

    d = gate.judge(Trigger(sender="dsh", explicit=True))
    check("observe 下显式请求不执行", d.accepted is False)
    check("observe 下记录 would_accept", d.would_accept is True)

    gate.mode = Mode.AUTO
    d = gate.judge(Trigger(sender="dsh", explicit=True))
    check("auto 下显式请求被放行", d.accepted is True)

    d = gate.judge(Trigger(sender="stranger", explicit=True))
    check("未在白名单被拒", d.accepted is False)
    check("未在白名单象限为 ignored", d.verdict.value == "ignored")

    d = gate.judge(Trigger(sender="dsh", explicit=False))
    check("未点名不放行", d.accepted is False)

    gate.mode = Mode.OFF
    d = gate.judge(Trigger(sender="dsh", explicit=True))
    check("off 模式一律不动", d.accepted is False and d.would_accept is False)

    # 规则抛异常时必须拒绝而不是放行
    boom = Rule("会崩的规则", lambda t: 1 / 0)
    g2 = Gate(rules=[boom], mode=Mode.AUTO)
    d = g2.judge(Trigger(sender="x", explicit=True))
    check("规则异常时拒绝放行", d.accepted is False)

    # 版本号：改模式必须让版本变化
    g3 = Gate(mode=Mode.OBSERVE)
    v1 = g3.version
    g3.mode = Mode.AUTO
    check("改模式后版本递增", g3.version > v1)

    # 无规则时不放行（安全默认值）
    g4 = Gate(rules=[], mode=Mode.AUTO, allowed_senders=None)
    d = g4.judge(Trigger(sender="x", explicit=True))
    check("无规则时不放行", d.accepted is False)

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{', '.join(failures)}")
        return 3
    print("全部通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="admit",
        description="给自建 Agent 的准入判断层",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="扫描信箱并按准入规则处理")
    p_run.add_argument("--inbox", required=True, help="信箱目录")
    p_run.add_argument("--rules", default=None, help=f"规则文件（默认 {DEFAULT_RULES_PATH}）")
    p_run.add_argument("--mode", default="observe", choices=[m.value for m in Mode])
    p_run.add_argument("--allow", default=None, help="允许的发起来源，逗号分隔")
    p_run.add_argument("--patterns", default=".msg,.md", help="参与扫描的后缀，逗号分隔")
    p_run.add_argument("--sender", default="dsh", help="推断不出来源时的默认发送者")
    p_run.add_argument("--dry-run", action="store_true", help="只判断，不移动任何文件")
    p_run.add_argument("-v", "--verbose", action="store_true", help="逐条输出判断理由")
    p_run.set_defaults(func=_cmd_run)

    p_judge = sub.add_parser("judge", help="对单个来源做一次判断")
    p_judge.add_argument("--sender", required=True)
    p_judge.add_argument("--explicit", action="store_true", help="是否明确点名")
    p_judge.add_argument("--rules", default=None)
    p_judge.add_argument("--mode", default="observe", choices=[m.value for m in Mode])
    p_judge.add_argument("--allow", default=None)
    p_judge.set_defaults(func=_cmd_judge)

    p_test = sub.add_parser("selftest", help="跑内置自检")
    p_test.set_defaults(func=_cmd_selftest)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())