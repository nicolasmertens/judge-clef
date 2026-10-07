"""The verdict: should this session compact now? (+ an effort hint placeholder)

Context size is measured, never guessed. Clef only judges *timing*: is a task
finished, is the next message a new topic, would compacting lose half-done
work. The final number blends size pressure with those three probabilities.
Without Clef (no credentials, network error, local-only directory) the same
formula runs with neutral priors, so the footer still shows the exact size.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import asdict, dataclass

from . import clef, transcript

# Size zones in tokens. Long-context quality drops gradually, not at a cliff;
# these defaults are a starting point, tune them with evals/ on your own data.
FLOOR = int(os.environ.get("JUDGE_CLEF_FLOOR", 60_000))     # below: never suggest compacting
HEAVY = int(os.environ.get("JUDGE_CLEF_HEAVY", 300_000))    # size pressure reaches 1.0 here

QUESTIONS = {
    "task_done": {
        "type": "noul",
        "instructions": (
            "Looking at the most recent exchange before the new user message: did the assistant "
            "finish the task it was working on, with the result delivered or confirmed and no "
            "open step it still has to do?"
        ),
    },
    "new_topic": {
        "type": "noul",
        "instructions": (
            "Is the NEW USER MESSAGE about a different task or subject than the recent conversation, "
            "so that most of the earlier details are no longer needed to answer it?"
        ),
    },
    "work_in_flight": {
        "type": "noul",
        "instructions": (
            "Is there half-done work whose fine details would be lost by summarising the conversation "
            "now, such as a debugging session midway, an edit in progress, or an answer the user is "
            "still waiting for?"
        ),
    },
    "effort": {
        "type": "choice",
        "instructions": "How much reasoning effort will answering the NEW USER MESSAGE need?",
        "criteria": {
            "low": "Trivial: a lookup, a yes/no, a tiny edit, a status line",
            "medium": "Routine work with a clear path",
            "high": "Real reasoning: debugging, design, multi-step changes",
            "xhigh": "Hard or high-stakes: subtle bugs after failed fixes, money, legal, architecture",
        },
    },
}


@dataclass
class Verdict:
    context_tokens: int
    turns: int
    compact_score: float          # 0..1, higher = compact now
    label: str                    # "compact now" | "compact soon" | "no need"
    reasons: list
    effort_hint: str | None
    source: str                   # "clef" | "local" | "clef-failed"
    clef_ms: int = 0
    clef_input_tokens: int = 0
    error: str = ""


def _local_only(cwd: str) -> bool:
    pats = [p for p in os.environ.get("JUDGE_CLEF_LOCAL_ONLY", "").split(":") if p]
    cwd = os.path.realpath(os.path.expanduser(cwd or ""))
    for p in pats:
        p = os.path.realpath(os.path.expanduser(p))
        if cwd == p or cwd.startswith(p.rstrip("/") + "/") or fnmatch.fnmatch(cwd, p):
            return True
    return False


def size_pressure(tokens: int) -> float:
    if tokens <= FLOOR:
        return 0.0
    return min(1.0, (tokens - FLOOR) / max(1, HEAVY - FLOOR))


def score(tokens: int, task_done: float, new_topic: float, in_flight: float) -> float:
    # A fresh topic is the strongest signal (old details are noise). A finished
    # task on the same topic is weaker: the next step may still need the details.
    timing = 0.35 * task_done + 0.55 * new_topic + 0.10 * (1.0 - in_flight)
    # A new topic makes even moderate context pure noise: let it pull harder.
    pressure = size_pressure(tokens)
    if new_topic > 0.7:
        pressure = min(1.0, pressure * 1.5)
    return round(pressure * timing, 2)


def label_for(s: float) -> str:
    return "compact now" if s >= 0.55 else "compact soon" if s >= 0.30 else "no need"


def judge(transcript_path: str, next_prompt: str = "", cwd: str = "", use_clef: bool = True) -> Verdict:
    snap = transcript.read(transcript_path, int(os.environ.get("JUDGE_CLEF_TAIL_CHARS", 24000)))
    td, nt, wf, effort, source = 0.5, 0.3, 0.5, None, "local"
    ms = itoks = 0
    err = ""
    if use_clef and size_pressure(snap.context_tokens) == 0 and not os.environ.get("JUDGE_CLEF_ALWAYS"):
        use_clef = False  # small context: nothing to decide, skip the call
    if use_clef and not _local_only(cwd):
        res = clef.ask(transcript.render_state(snap, next_prompt), QUESTIONS)
        ms, itoks, err = res.latency_ms, res.input_tokens, res.error
        if res.ok:
            source = "clef"
            def p(name, default):
                v = clef.noul(res.answers, name)
                return default if v is None else v
            td, nt, wf = p("task_done", td), p("new_topic", nt), p("work_in_flight", wf)
            effort, _ = clef.choice(res.answers, "effort")
        else:
            source = "clef-failed"
    s = score(snap.context_tokens, td, nt, wf)
    reasons = []
    if source == "clef":
        if td >= 0.6:
            reasons.append("task done")
        if nt >= 0.6:
            reasons.append("new topic")
        if wf >= 0.6:
            reasons.append("work in flight")
    if snap.context_tokens >= HEAVY:
        reasons.append("heavy context")
    return Verdict(snap.context_tokens, snap.turns, s, label_for(s), reasons, effort, source, ms, itoks, err)


def footer(v: Verdict) -> str:
    k = f"{round(v.context_tokens / 1000)}k"
    why = f" ({', '.join(v.reasons)})" if v.reasons else ""
    tail = "" if v.source == "clef" else " [no clef]" if v.source == "local" else " [clef failed]"
    parts = [f"Judge Clef: {k} context", f"{v.label} {v.compact_score:.2f}{why}"]
    if v.effort_hint:
        parts.append(f"effort hint {v.effort_hint}")
    return " · ".join(parts) + tail


def as_dict(v: Verdict) -> dict:
    return asdict(v)
