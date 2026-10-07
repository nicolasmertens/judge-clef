"""judge-clef command line.

  judge-clef hook           UserPromptSubmit hook: reads hook JSON on stdin
  judge-clef statusline     status line command: prints the last verdict (no API call)
  judge-clef check FILE     judge one transcript by hand  [--prompt TEXT] [--json] [--no-clef]
  judge-clef scan DIR       peak context per session in a Claude Code projects dir
"""

from __future__ import annotations

import glob
import json
import os
import sys

from . import judge, transcript

CACHE = os.path.expanduser(os.environ.get("JUDGE_CLEF_CACHE", "~/.cache/judge-clef"))

FOOTER_INSTRUCTION = (
    "Judge Clef (context monitor) for this turn: `{line}`. "
    "End your final reply with this line verbatim on its own line{after}. "
    "If the verdict is \"compact now\", you may add one short sentence suggesting /compact; never compact on your own."
)


def _cache_path(session_id: str) -> str:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "unknown"
    return os.path.join(CACHE, safe + ".json")


def cmd_hook() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    path = data.get("transcript_path") or ""
    if not path:
        return 0
    v = judge.judge(path, data.get("prompt", ""), data.get("cwd", ""))
    line = judge.footer(v)
    try:
        os.makedirs(CACHE, exist_ok=True)
        with open(_cache_path(data.get("session_id", "")), "w") as fh:
            json.dump({**judge.as_dict(v), "footer": line}, fh)
    except OSError:
        pass
    if os.environ.get("JUDGE_CLEF_FOOTER", "1") == "0":
        return 0
    after = os.environ.get("JUDGE_CLEF_FOOTER_AFTER", "")
    ctx = FOOTER_INSTRUCTION.format(line=line, after=f", directly after the {after}" if after else "")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}}))
    return 0


def cmd_statusline() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    try:
        with open(_cache_path(data.get("session_id", ""))) as fh:
            print(json.load(fh)["footer"])
            return 0
    except (OSError, ValueError, KeyError):
        pass
    snap = transcript.read(data.get("transcript_path", ""), 0)
    print(f"Judge Clef: {round(snap.context_tokens / 1000)}k context")
    return 0


def cmd_check(args: list) -> int:
    if not args:
        print(__doc__)
        return 2
    prompt = ""
    if "--prompt" in args:
        i = args.index("--prompt")
        prompt = args[i + 1] if i + 1 < len(args) else ""
    v = judge.judge(args[0], prompt, os.getcwd(), use_clef="--no-clef" not in args)
    print(json.dumps(judge.as_dict(v), indent=2) if "--json" in args else judge.footer(v))
    return 0


def cmd_scan(args: list) -> int:
    d = os.path.expanduser(args[0] if args else "~/.claude/projects")
    files = sorted(glob.glob(os.path.join(d, "**", "*.jsonl"), recursive=True), key=os.path.getmtime)[-200:]
    peaks, compacted = [], 0
    for f in files:
        peak, seen = 0, False
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"compact_boundary"' in line or '"isCompactSummary":true' in line:
                    seen = True
                if '"usage"' not in line:
                    continue
                try:
                    u = (json.loads(line).get("message") or {}).get("usage") or {}
                except ValueError:
                    continue
                peak = max(peak, sum(int(u.get(k) or 0) for k in
                                     ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
        if peak:
            peaks.append(peak)
            compacted += seen
    if not peaks:
        print("no sessions with usage data found")
        return 1
    peaks.sort()
    q = lambda p: peaks[min(len(peaks) - 1, int(len(peaks) * p))]
    print(f"sessions={len(peaks)} median={q(0.5):,} p75={q(0.75):,} p90={q(0.9):,} max={peaks[-1]:,} "
          f"compacted={compacted} over_{judge.HEAVY // 1000}k={sum(p >= judge.HEAVY for p in peaks)}")
    return 0


def main(argv: list | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if cmd == "hook":
            return cmd_hook()
        if cmd == "statusline":
            return cmd_statusline()
        if cmd == "check":
            return cmd_check(rest)
        if cmd == "scan":
            return cmd_scan(rest)
    except Exception as e:  # a monitor must never break the session
        print(f"judge-clef: {type(e).__name__}: {e}", file=sys.stderr)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
