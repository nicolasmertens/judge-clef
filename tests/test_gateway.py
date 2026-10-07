import copy
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from judge_clef import clef, effort, gateway, insert  # noqa: E402

TOOLS = [{"name": "Bash", "input_schema": {"type": "object"}}]


def req(messages, model="claude-opus-5-5", top="high"):
    return {"model": model, "tools": TOOLS, "output_config": {"effort": top}, "messages": messages}


def tool_round(text, err=False, uid="t1", cmd="pytest"):
    return [
        {"role": "assistant", "content": [{"type": "tool_use", "id": uid, "name": "Bash", "input": {"command": cmd}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": uid, "content": text, "is_error": err}]},
    ]


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        gateway.CACHE = self.tmp
        self._ask = clef.ask
        clef.ask = lambda *a, **k: clef.ClefResult(ok=False, error="offline in tests")  # local policy only
        os.environ["JUDGE_CLEF_MAX_EFFORT"] = "xhigh"
        self.gw = gateway.Gateway(upstream="http://127.0.0.1:9")
        self.h = {"x-claude-code-session-id": "s1"}

    def tearDown(self):
        clef.ask = self._ask
        del os.environ["JUDGE_CLEF_MAX_EFFORT"]

    def statements(self, body):
        return [(i, m["output_config"]["effort"]) for i, m in enumerate(body["messages"]) if insert.is_effort_statement(m)]

    def test_short_prompt_drops_to_low_and_is_replayed_identically(self):
        m1 = [{"role": "user", "content": "list files"}]
        out1, note = self.gw.transform("/v1/messages", req(m1), self.h)
        self.assertEqual(self.statements(out1), [(1, "low")])
        m2 = m1 + tool_round("a.py b.py")
        out2, _ = self.gw.transform("/v1/messages", req(m2), self.h)
        self.assertEqual(out2["messages"][:2], out1["messages"])  # earlier prefix unchanged, byte for byte

    def test_failure_raises_and_retry_is_stable(self):
        m = [{"role": "user", "content": "fix the failing date parser test please, it broke after the refactor"}]
        self.gw.transform("/v1/messages", req(m), self.h)
        m = m + tool_round("1 failed, 12 passed\nAssertionError", err=False)
        out, note = self.gw.transform("/v1/messages", req(m), self.h)
        self.assertIn("failing check", note)
        first = self.statements(out)
        again, note2 = self.gw.transform("/v1/messages", req(copy.deepcopy(m)), self.h)
        self.assertEqual(note2, "replay")
        self.assertEqual(self.statements(again), first)
        self.assertEqual(first[-1][1], "high")

    def test_pass_after_fail_steps_down_one_level(self):
        m = [{"role": "user", "content": "fix the failing date parser test please, it broke after the refactor"}]
        self.gw.transform("/v1/messages", req(m), self.h)
        m += tool_round("1 failed\nAssertionError", uid="a")
        self.gw.transform("/v1/messages", req(m), self.h)    # medium -> high
        m += tool_round("edited", uid="b", cmd="sed -i ...")
        self.gw.transform("/v1/messages", req(m), self.h)    # hold right after a raise
        m += tool_round("13 passed", uid="c")
        out, _ = self.gw.transform("/v1/messages", req(m), self.h)
        self.assertEqual(self.statements(out)[-1][1], "medium")

    def test_cache_control_moves_do_not_change_the_prefix(self):
        a = [{"role": "user", "content": "hello there"}]
        b = [{"role": "user", "content": [{"type": "text", "text": "hello there", "cache_control": {"type": "ephemeral"}}]}]
        self.assertEqual(insert.prefix_hashes(a), insert.prefix_hashes(b))

    def test_side_requests_replay_but_never_decide(self):
        m1 = [{"role": "user", "content": "list files"}]
        self.gw.transform("/v1/messages", req(m1), self.h)
        m2 = m1 + tool_round("a.py")
        counted, note = self.gw.transform("/v1/messages/count_tokens", req(m2), self.h)
        self.assertEqual(note, "replay")
        self.assertEqual(self.statements(counted), [(1, "low")])
        self.assertNotIn(insert.prefix_hashes(m2)[-1], self.gw.boundaries)

    def test_other_models_pass_through(self):
        out, _ = self.gw.transform("/v1/messages", req([{"role": "user", "content": "hi"}], model="claude-haiku-4-5"), self.h)
        self.assertIsNone(out)

    def test_restart_replays_from_journal(self):
        m1 = [{"role": "user", "content": "list files"}]
        out1, _ = self.gw.transform("/v1/messages", req(m1), self.h)
        fresh = gateway.Gateway(upstream="http://127.0.0.1:9")
        m2 = m1 + tool_round("a.py")
        out2, _ = fresh.transform("/v1/messages/count_tokens", req(m2), self.h)
        self.assertEqual(out2["messages"][:2], out1["messages"])

    def test_manual_effort_is_respected(self):
        # Claude Code states its level after every prompt; that alone is not manual.
        stmt = lambda e: {"role": "system", "content": [], "output_config": {"effort": e}}  # noqa: E731
        m = [{"role": "user", "content": "list files"}, stmt("medium")]
        _, note = self.gw.transform("/v1/messages", req(m), self.h)
        self.assertNotIn("manual", note)
        # The user runs /effort max mid-prompt: Claude Code's level changes -> pause.
        m = m + tool_round("x") + [stmt("max")]
        m.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t9", "content": "y"}]})
        _, note = self.gw.transform("/v1/messages", req(m), self.h)
        self.assertIn("manual", note)


class PolicyTests(unittest.TestCase):
    def test_extreme_critical_goes_max(self):
        r, _ = effort.task_effort("architecture", 0.9, 3.8, 0.9)
        self.assertEqual(r, effort.MAX)

    def test_chat_is_capped(self):
        r, _ = effort.task_effort("chat", 0.9, 3.0, 0.0)
        self.assertEqual(r, effort.MEDIUM)

    def test_third_failure_escalates_hard(self):
        t = effort.Thread(base=effort.MEDIUM, current=effort.HIGH, fails=2)
        r, why = effort.step_effort(t, "diagnosing", 0.9, 2.0, 0.2, True)
        self.assertGreaterEqual(r, effort.XHIGH)

    def test_beta_header_added_once(self):
        self.assertEqual(insert.add_beta("oauth-2025-04-20"), "oauth-2025-04-20," + insert.BETA)
        self.assertEqual(insert.add_beta(insert.BETA), insert.BETA)


if __name__ == "__main__":
    unittest.main()
