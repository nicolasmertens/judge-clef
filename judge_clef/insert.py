"""Effort statements inside a Messages API transcript, cache-safe.

An effort change is an effort-only system message,
``{"role": "system", "content": [], "output_config": {"effort": "high"}}``
(beta ``mid-conversation-output-config-2026-07-01``). Claude Code resends the
whole history on every request, so every statement made so far must be put
back at exactly the same place each time: then the prefix the model saw never
changes, and both the prompt cache and preserved thinking stay valid.

Each statement is keyed by the rolling hash of the messages *before* it. A
hash at position i fixes the entire prefix, so branches, rewinds and subagents
never pick up a statement that does not belong to them.

Idea and canonical form adapted from jev-opus (MIT, github.com/WXK-AI/jev-opus).
"""

from __future__ import annotations

import hashlib
import json

LEVELS = ("low", "medium", "high", "xhigh", "max")
BETA = "mid-conversation-output-config-2026-07-01"
KNOWN_BETAS = (BETA, "per-turn-control-2026-07-01", "mid-conversation-effort-2026-08-01")


def is_effort(v) -> bool:
    return v in LEVELS


def canonical(value, as_block: bool = False, in_args: bool = False):
    """Claude Code moves cache_control breakpoints and switches string content
    to text-block arrays between requests; the API renders both the same, so
    neither may count as a change. Tool arguments are compared verbatim."""
    if isinstance(value, list):
        return [canonical(v, False, in_args) for v in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for k, v in value.items():
        if not in_args and as_block and k == "cache_control":
            continue
        if not in_args and k == "content" and isinstance(v, str):
            out[k] = [{"type": "text", "text": v}]
        elif not in_args and k == "content" and isinstance(v, list):
            out[k] = [canonical(b, True, False) for b in v]
        elif as_block and k == "input":
            out[k] = canonical(v, False, True)
        else:
            out[k] = canonical(v, False, in_args)
    return out


def prefix_hashes(messages: list) -> list:
    """hashes[i] = hash of canonical messages[0:i]; len = len(messages) + 1."""
    out, h = [""], ""
    for m in messages:
        d = hashlib.sha256()
        d.update(h.encode())
        d.update(json.dumps(canonical(m), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
        h = d.hexdigest()
        out.append(h)
    return out


def effort_message(effort: str) -> dict:
    return {"role": "system", "content": [], "output_config": {"effort": effort}}


def is_effort_statement(m) -> bool:
    return isinstance(m, dict) and m.get("role") == "system" and is_effort((m.get("output_config") or {}).get("effort"))


def apply(messages: list, insertions: list) -> list:
    """insertions: [(index, effort)] relative to Claude Code's own messages."""
    by_index = {}
    for i, e in insertions:
        by_index.setdefault(i, []).append(e)
    out = []
    for i in range(len(messages) + 1):
        for e in by_index.get(i, []):
            out.append(effort_message(e))
        if i < len(messages):
            out.append(messages[i])
    return out


def effort_in_force(forwarded: list, top_level) -> str:
    """Last effort statement in the forwarded transcript, else the top-level value."""
    for m in reversed(forwarded):
        if is_effort_statement(m):
            return m["output_config"]["effort"]
    return top_level if is_effort(top_level) else "medium"  # Opus 5.5 API default


def add_beta(header: str | None) -> str:
    vals = [v.strip() for v in (header or "").split(",") if v.strip()]
    if not any(v in KNOWN_BETAS for v in vals):
        vals.append(BETA)
    return ",".join(vals)
