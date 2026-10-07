import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from judge_clef import judge, transcript  # noqa: E402


def asst(text, tokens, sidechain=False, tool=None):
    content = [{"type": "text", "text": text}] if text else []
    if tool:
        content.append({"type": "tool_use", "name": tool, "input": {}})
    return {"type": "assistant", "isSidechain": sidechain, "message": {
        "model": "m", "content": content,
        "usage": {"input_tokens": 1, "cache_read_input_tokens": tokens - 1, "cache_creation_input_tokens": 0}}}


def user(content):
    return {"type": "user", "isSidechain": False, "message": {"content": content}}


def write(recs):
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    f.write("\n".join(json.dumps(r) for r in recs))
    f.close()
    return f.name


class TranscriptTests(unittest.TestCase):
    def test_context_is_last_main_chain_usage(self):
        p = write([user("a"), asst("x", 1000), asst("sub", 99999, sidechain=True), asst("y", 2500)])
        self.assertEqual(transcript.read(p).context_tokens, 2500)

    def test_compact_boundary_resets(self):
        p = write([user("a"), asst("x", 500000), {"type": "system", "subtype": "compact_boundary"}, user("b")])
        s = transcript.read(p)
        self.assertEqual((s.context_tokens, s.compactions, s.turns), (0, 1, 1))

    def test_tool_results_left_out_of_tail(self):
        secret = "SECRET-TOOL-OUTPUT"
        p = write([user("go"), asst("", 100, tool="Bash"),
                   user([{"type": "tool_result", "content": secret}]), asst("done", 200)])
        state = transcript.render_state(transcript.read(p), "next")
        self.assertNotIn(secret, state)
        self.assertIn("ASSISTANT USED TOOLS: Bash", state)

    def test_tail_respects_budget(self):
        p = write([user("a" * 5000), asst("b" * 5000, 10), user("c" * 100)])
        tail = transcript.read(p, tail_chars=6000).tail
        self.assertLessEqual(sum(len(t) for _, t in tail), 6000)
        self.assertEqual(tail[-1][1], "c" * 100)

    def test_missing_file_is_harmless(self):
        self.assertEqual(transcript.read("/nonexistent.jsonl").context_tokens, 0)


class ScoreTests(unittest.TestCase):
    def test_small_context_never_compacts(self):
        self.assertEqual(judge.score(30000, 1, 1, 0), 0.0)

    def test_new_topic_at_heavy_context_compacts(self):
        self.assertEqual(judge.label_for(judge.score(600000, 1, 1, 0)), "compact now")

    def test_work_in_flight_holds(self):
        self.assertEqual(judge.label_for(judge.score(600000, 0.1, 0.1, 0.95)), "no need")

    def test_local_only_dirs_skip_clef(self):
        os.environ["JUDGE_CLEF_LOCAL_ONLY"] = "/tmp/private-books"
        try:
            self.assertTrue(judge._local_only("/tmp/private-books/2026"))
            self.assertFalse(judge._local_only("/tmp/other"))
        finally:
            del os.environ["JUDGE_CLEF_LOCAL_ONLY"]


if __name__ == "__main__":
    unittest.main()
