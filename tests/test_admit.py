"""
admit 的回归测试。

用标准库 unittest，不引入任何测试依赖——因为这个库在手机上跑，
装 pytest 的代价比写几行断言高得多。
"""

import os
import tempfile
import unittest
from pathlib import Path

from admit.core import Decision, Gate, Mode, Rule, Trigger, Verdict
from admit.inbox import _is_explicit, _sender_of, run_once


class TestQuadrant(unittest.TestCase):
    """四象限分类。"""

    def setUp(self):
        self.rule = Rule("任意", lambda t: True)
        self.gate = Gate(
            rules=[self.rule], mode=Mode.AUTO, allowed_senders=["dsh", "user"]
        )

    def test_explicit_allowed(self):
        d = self.gate.judge(Trigger(sender="dsh", explicit=True))
        self.assertIs(d.verdict, Verdict.EXPLICIT)
        self.assertTrue(d.accepted)

    def test_inferred_allowed(self):
        d = self.gate.judge(Trigger(sender="dsh", explicit=False))
        self.assertIs(d.verdict, Verdict.INFERRED)
        self.assertFalse(d.accepted)

    def test_explicit_not_allowed(self):
        d = self.gate.judge(Trigger(sender="stranger", explicit=True))
        self.assertIs(d.verdict, Verdict.IGNORED)
        self.assertFalse(d.accepted)

    def test_inferred_not_allowed(self):
        d = self.gate.judge(Trigger(sender="stranger", explicit=False))
        self.assertIs(d.verdict, Verdict.IGNORED)


class TestMode(unittest.TestCase):
    """三档模式的行为边界。"""

    def test_off_does_nothing(self):
        g = Gate(rules=[Rule("ok", lambda t: True)], mode=Mode.OFF)
        d = g.judge(Trigger(sender="dsh", explicit=True))
        self.assertFalse(d.accepted)
        self.assertFalse(d.would_accept)

    def test_observe_never_executes(self):
        g = Gate(rules=[Rule("ok", lambda t: True)], mode=Mode.OBSERVE)
        d = g.judge(Trigger(sender="dsh", explicit=True))
        self.assertFalse(d.accepted)
        self.assertTrue(d.would_accept)

    def test_auto_executes(self):
        g = Gate(rules=[Rule("ok", lambda t: True)], mode=Mode.AUTO)
        d = g.judge(Trigger(sender="dsh", explicit=True))
        self.assertTrue(d.accepted)

    def test_mode_change_bumps_version(self):
        g = Gate(mode=Mode.OBSERVE)
        before = g.version
        g.mode = Mode.AUTO
        self.assertGreater(g.version, before)

    def test_same_mode_does_not_bump(self):
        g = Gate(mode=Mode.OBSERVE)
        before = g.version
        g.mode = Mode.OBSERVE
        self.assertEqual(g.version, before)


class TestFailSafe(unittest.TestCase):
    """失败时一律不放行。"""

    def test_rule_exception_blocks(self):
        g = Gate(rules=[Rule("boom", lambda t: 1 / 0)], mode=Mode.AUTO)
        d = g.judge(Trigger(sender="dsh", explicit=True))
        self.assertFalse(d.accepted)

    def test_no_rules_blocks(self):
        g = Gate(rules=[], mode=Mode.AUTO, allowed_senders=None)
        d = g.judge(Trigger(sender="dsh", explicit=True))
        self.assertFalse(d.accepted)

    def test_any_rule_failure_blocks(self):
        g = Gate(
            rules=[Rule("yes", lambda t: True), Rule("no", lambda t: False)],
            mode=Mode.AUTO,
        )
        d = g.judge(Trigger(sender="dsh", explicit=True))
        self.assertFalse(d.accepted)
        self.assertIn("no", d.failed_rules)

    def test_all_rules_evaluated(self):
        """短路会导致漏判，这里验证每条都跑到了。"""
        seen = []

        def mark(name):
            def inner(t):
                seen.append(name)
                return True
            return inner

        g = Gate(
            rules=[Rule("a", mark("a")), Rule("b", mark("b")), Rule("c", mark("c"))],
            mode=Mode.AUTO,
        )
        g.judge(Trigger(sender="dsh", explicit=True))
        self.assertEqual(seen, ["a", "b", "c"])


class TestSenderInference(unittest.TestCase):
    """来源推断必须保守。"""

    def test_filename_prefix(self):
        p = Path("/tmp/DSH_TO_OPERIT_001.md")
        self.assertEqual(_sender_of(p, "", None), "dsh")

    def test_unknown_not_defaulted(self):
        p = Path("/tmp/stranger_msg.md")
        self.assertEqual(_sender_of(p, "hello", None), "unknown")

    def test_default_argument_respected(self):
        p = Path("/tmp/whatever.md")
        self.assertEqual(_sender_of(p, "", "user"), "user")

    def test_explicit_markers(self):
        self.assertTrue(_is_explicit("@大肥鱼 帮我看一下"))
        self.assertTrue(_is_explicit("请执行这个"))
        self.assertFalse(_is_explicit("只是同步一下进度"))


class TestInboxRun(unittest.TestCase):
    """端到端：真建目录、真移动文件。"""

    def test_observe_mode_archives(self):
        with tempfile.TemporaryDirectory() as td:
            inbox = Path(td) / "inbox"
            inbox.mkdir()
            (inbox / "DSH_TO_OPERIT_1.md").write_text("@大肥鱼 请干活", encoding="utf-8")
            (inbox / "REPLY_2.md").write_text("通知而已", encoding="utf-8")

            conf = [{"name": "来自dsh", "type": "sender_prefix", "value": "dsh"}]
            res = run_once(
                inbox=inbox,
                rules_conf=conf,
                mode=Mode.OBSERVE,
                allowed=["dsh"],
            )

            self.assertEqual(res.total, 2)
            self.assertEqual(res.observed, 1)
            self.assertEqual(res.skipped, 1)
            self.assertEqual(res.executed, 0)
            # observe 下仍然归档，否则下一轮会重复判断
            self.assertTrue((inbox / "processed" / "DSH_TO_OPERIT_1.md").exists())

    def test_auto_mode_executes(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            inbox = Path(td) / "inbox"
            inbox.mkdir()
            (inbox / "DSH_TO_OPERIT_1.md").write_text("@大肥鱼 请干活", encoding="utf-8")

            conf = [{"name": "来自dsh", "type": "sender_prefix", "value": "dsh"}]
            res = run_once(
                inbox=inbox,
                rules_conf=conf,
                mode=Mode.AUTO,
                allowed=["dsh"],
                executor=lambda p, b: calls.append(p.name),
            )

            self.assertEqual(res.executed, 1)
            self.assertEqual(calls, ["DSH_TO_OPERIT_1.md"])

    def test_dry_run_moves_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            inbox = Path(td) / "inbox"
            inbox.mkdir()
            (inbox / "DSH_TO_OPERIT_1.md").write_text("@大肥鱼 请干活", encoding="utf-8")

            conf = [{"name": "来自dsh", "type": "sender_prefix", "value": "dsh"}]
            run_once(
                inbox=inbox,
                rules_conf=conf,
                mode=Mode.AUTO,
                allowed=["dsh"],
                dry_run=True,
            )
            self.assertTrue((inbox / "DSH_TO_OPERIT_1.md").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)