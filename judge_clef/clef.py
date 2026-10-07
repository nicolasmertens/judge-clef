"""Minimal Clef client (Cloudflare Workers AI, System One API).

Never raises: on any failure it returns ``ok=False`` so callers fall back to
local heuristics. Clef follows the same wire format as Jev:

    POST {model, state, questions: {name: {type, instructions, criteria?}}}
    ->   {result: {answers: {name: {...}}, usage: {input_tokens, ...}}}
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_MODEL = "clef-flash"
API = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/{model}"


@dataclass
class ClefResult:
    ok: bool
    answers: dict = field(default_factory=dict)
    input_tokens: int = 0
    latency_ms: int = 0
    error: str = ""


def _from_env_or_cmd(name: str) -> str:
    """Read NAME, or run NAME_CMD (e.g. a vault lookup) and use its stdout."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    cmd = os.environ.get(name + "_CMD", "").strip()
    if not cmd:
        return ""
    try:
        out = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def credentials() -> tuple[str, str]:
    account = _from_env_or_cmd("JUDGE_CLEF_ACCOUNT_ID") or _from_env_or_cmd("CLOUDFLARE_ACCOUNT_ID")
    token = _from_env_or_cmd("JUDGE_CLEF_API_TOKEN") or _from_env_or_cmd("CLOUDFLARE_API_TOKEN")
    return account, token


def ask(state: str, questions: dict, model: str | None = None, timeout: float = 4.0) -> ClefResult:
    model = model or os.environ.get("JUDGE_CLEF_MODEL", DEFAULT_MODEL)
    account, token = credentials()
    if not (account and token):
        return ClefResult(ok=False, error="no Cloudflare credentials")
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(
        API.format(account=account, model=model),
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        return ClefResult(ok=False, error=f"{type(e).__name__}: {e}"[:200],
                          latency_ms=int((time.monotonic() - t0) * 1000))
    latency = int((time.monotonic() - t0) * 1000)
    result = payload.get("result") or {}
    answers = result.get("answers")
    if not payload.get("success") or not isinstance(answers, dict):
        return ClefResult(ok=False, error=str(payload.get("errors"))[:200], latency_ms=latency)
    return ClefResult(
        ok=True,
        answers=answers,
        input_tokens=int((result.get("usage") or {}).get("input_tokens", 0)),
        latency_ms=latency,
    )


def noul(answers: dict, name: str) -> float | None:
    a = answers.get(name)
    if isinstance(a, dict) and isinstance(a.get("noul"), (int, float)):
        return max(0.0, min(1.0, float(a["noul"])))
    return None


def choice(answers: dict, name: str) -> tuple[str | None, dict]:
    a = answers.get(name)
    if isinstance(a, dict) and isinstance(a.get("choice"), str):
        return a["choice"], a.get("probabilities") or {}
    return None, {}
