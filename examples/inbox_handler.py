#!/usr/bin/env python3
"""
示例：把 admit 接进一个"文件即队列"的信箱处理器。

这是 handler_v2.sh 的 Python 版本，区别在于它会先问一句"该不该处理"。
对比一下两边的逻辑：

  handler_v2.sh   找到文件 → 直接 cat → 归档
  本文件          找到文件 → 判断来源与意图 → 通过了才 cat → 归档

多出来的那一句判断，就是全部差异。

用法：
    python3 examples/inbox_handler.py --inbox /tmp/test_inbox --mode observe -v
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from admit.inbox import build_gate, load_rules, scan_inbox, _archive, _is_explicit, _sender_of
from admit import Trigger


def handle(inbox: str, mode: str, allowed: list[str], rules_path: str | None, verbose: bool):
    root = Path(inbox)
    proc = root / "processed"

    conf = load_rules(rules_path) if rules_path else []
    gate = build_gate(conf, mode=mode, allowed=allowed or None)

    files = scan_inbox(root, (".msg", ".md"))
    if not files:
        print(f"[{mode}] 信箱为空")
        return 0

    done = 0
    for path in files:
        body = path.read_text(encoding="utf-8", errors="replace")
        trigger = Trigger(
            sender=_sender_of(path, body, None),
            explicit=_is_explicit(body),
            source="inbox",
            payload=body,
            meta={"filename": path.name, "size": path.stat().st_size},
        )

        decision = gate.judge(trigger)

        if decision.accepted:
            # 这里是真正动手的位置。示例里只打印，实际应该调下游。
            print(f"===== 执行: {path.name} =====")
            print(body.strip()[:500])
            done += 1
            _archive(path, proc)
        elif decision.would_accept:
            # observe 模式下的重点：记录"本会做什么"，但不做
            print(f"----- 观察: {path.name}（本会执行，未执行）-----")
            if verbose:
                print(decision.explain())
            _archive(path, proc)  # 归档避免重复判断
        else:
            if verbose:
                print(f"----- 跳过: {path.name} -----")
                print(decision.explain())

    print(f"\n[{mode}] 共 {len(files)} 条，实际执行 {done} 条")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--inbox", required=True)
    p.add_argument("--mode", default="observe", choices=["off", "observe", "auto"])
    p.add_argument("--allow", default="dsh,user")
    p.add_argument("--rules", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    sys.exit(handle(a.inbox, a.mode, a.allow.split(","), a.rules, a.verbose))