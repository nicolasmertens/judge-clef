"""Read a Claude Code session transcript (JSONL) without loading the model.

Two things come out of it:
  * the exact context size of the main thread, taken from the usage block of
    the last main-chain assistant message (input + cache read + cache write);
  * a compact text tail of the conversation for the decision model.

Tool results are left out of the tail on purpose: they are the bulk of the
tokens, carry the most sensitive data, and say little about whether a task is
finished.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Snapshot:
    context_tokens: int = 0
    model: str = ""
    turns: int = 0               # real user prompts since the last compaction
    compactions: int = 0
    tail: list = field(default_factory=list)  # [(role, text)]


def _text_of(content) -> tuple[str, list]:
    """Return (text, tool_names) for a message content field."""
    if isinstance(content, str):
        return content, []
    text, tools = [], []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            text.append(part.get("text", ""))
        elif kind == "tool_use":
            tools.append(part.get("name", "tool"))
    return "\n".join(t for t in text if t), tools


def _is_compact_marker(rec: dict) -> bool:
    return (rec.get("type") == "system" and rec.get("subtype") == "compact_boundary") or bool(
        rec.get("isCompactSummary")
    )


def read(path: str, tail_chars: int = 24000) -> Snapshot:
    snap = Snapshot()
    events: list = []
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return snap
    with fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("isSidechain"):
                continue
            if _is_compact_marker(rec):
                snap.compactions += 1
                snap.turns = 0
                snap.context_tokens = 0
                events = []
                continue
            kind = rec.get("type")
            msg = rec.get("message") or {}
            if kind == "assistant":
                usage = msg.get("usage") or {}
                total = sum(int(usage.get(k) or 0) for k in
                            ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                if total:
                    snap.context_tokens = total
                    snap.model = msg.get("model") or snap.model
                text, tools = _text_of(msg.get("content"))
                if text:
                    events.append(("assistant", text))
                if tools:
                    events.append(("tools", ", ".join(tools)))
            elif kind == "user" and not rec.get("isMeta"):
                text, _ = _text_of(msg.get("content"))
                if text and not text.lstrip().startswith("<"):  # skip injected command/system blocks
                    snap.turns += 1
                    events.append(("user", text))

    # Keep the newest events that fit in tail_chars; merge runs of tool calls.
    out, used = [], 0
    for role, text in reversed(events):
        if role == "tools" and out and out[-1][0] == "tools":
            continue
        if used + len(text) > tail_chars:
            if not out:
                out.append((role, text[-tail_chars:]))
            break
        out.append((role, text))
        used += len(text)
    snap.tail = list(reversed(out))
    return snap


def render_state(snap: Snapshot, next_prompt: str = "") -> str:
    lines = [f"Conversation so far: {snap.turns} user turns, about {snap.context_tokens:,} tokens of context.", ""]
    for role, text in snap.tail:
        label = {"user": "USER", "assistant": "ASSISTANT", "tools": "ASSISTANT USED TOOLS"}[role]
        lines.append(f"{label}: {text.strip()}")
    if next_prompt:
        lines += ["", f"NEW USER MESSAGE (just sent, not answered yet): {next_prompt.strip()}"]
    return "\n".join(lines)
