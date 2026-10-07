"""Local Messages API gateway: Clef re-picks effort before every model call.

Claude Code points ANTHROPIC_BASE_URL here (your claude.ai login keeps working;
credentials are forwarded untouched and never logged). For routed models, each
request that ends in a user turn (a new prompt, or the results of a tool batch)
gets one decision; a change is inserted as an effort-only system message at the
end, keyed by the hash of everything before it, and replayed byte-identically
on every later request. Everything else passes through unchanged.

Decisions are journaled before forwarding, so a gateway restart replays exactly
what the model saw. Resume a gateway session through the gateway: without the
statements the history would look edited.

Design adapted from jev-opus (MIT, github.com/WXK-AI/jev-opus).
"""

from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import effort as ef
from . import effort
from . import insert
from .insert import LEVELS

CACHE = os.path.expanduser(os.environ.get("JUDGE_CLEF_CACHE", "~/.cache/judge-clef"))
TOKEN_HEADER = "x-judge-clef-token"
HOP = {"host", "connection", "content-length", "accept-encoding", "transfer-encoding", "keep-alive",
       "proxy-connection", "upgrade", TOKEN_HEADER}
DROP_RESPONSE = {"content-length", "transfer-encoding", "connection", "keep-alive"}
SIDE_QUERY_MARKERS = ("[SUGGESTION MODE:",)
CWD_RE = re.compile(r"Primary working directory: (\S+)")


def _env_list(name, default):
    return [m.strip() for m in os.environ.get(name, default).split(",") if m.strip()]


class Gateway:
    def __init__(self, upstream: str | None = None, token: str | None = None):
        self.upstream = urllib.parse.urlparse(upstream or os.environ.get("JUDGE_CLEF_UPSTREAM", "https://api.anthropic.com"))
        self.token = token or secrets.token_urlsafe(24)
        self.models = _env_list("JUDGE_CLEF_ROUTE_MODELS", "claude-opus-5-5,claude-opus-5,claude-fable-5-1")
        self.lo = os.environ.get("JUDGE_CLEF_MIN_EFFORT", "low")
        self.hi = os.environ.get("JUDGE_CLEF_MAX_EFFORT", "high")
        if LEVELS.index(self.lo) > LEVELS.index(self.hi):
            self.lo, self.hi = self.hi, self.lo
        self.lock = threading.Lock()                     # guards maps + journal
        self.thread_locks: dict = {}
        self.boundaries: dict = {}                       # prefix hash -> (index, effort|None)
        self.threads: dict = {}                          # thread key -> ef.Thread
        os.makedirs(CACHE, exist_ok=True)
        self.journal_path = os.path.join(CACHE, "effort-journal.jsonl")
        self.log_path = os.path.join(CACHE, "gateway.log")
        self._load()

    # ---- journal -------------------------------------------------------
    def _load(self):
        try:
            fh = open(self.journal_path, encoding="utf-8")
        except FileNotFoundError:
            return
        with fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("t") == "ins":
                    self.boundaries[r["h"]] = (r["i"], r.get("e"))
                elif r.get("t") == "thread":
                    self.threads[r["k"]] = ef.Thread(**r["s"])

    def _append(self, *records):
        with open(self.journal_path, "a", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def log(self, msg: str):
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        except OSError:
            pass

    # ---- routing -------------------------------------------------------
    def routable(self, body: dict) -> bool:
        model = str(body.get("model", ""))
        return any(model == m or model.startswith(m + "[") for m in self.models)

    def replay(self, messages: list, hashes: list) -> list:
        ins = []
        for i, h in enumerate(hashes):
            hit = self.boundaries.get(h)
            if hit and hit[1] and hit[0] == i:
                ins.append((i, hit[1]))
        return ins

    def transform(self, path: str, body: dict, headers: dict) -> tuple:
        """Return (new body or None, note for the log)."""
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or not self.routable(body):
            return None, ""
        if os.environ.get("JUDGE_CLEF_DEBUG"):
            shape = " ".join(
                f"{m.get('role', '?')[0]}{'[' + m['output_config']['effort'] + ']' if insert.is_effort_statement(m) else ''}"
                f"{':tr' if effort.has_tool_results(m) else ''}" for m in messages)
            self.log(f"debug {path} model={body.get('model')} top={body.get('output_config')} tools={len(body.get('tools') or [])} "
                     f"beta={headers.get('anthropic-beta', '')} :: {shape}")
        hashes = insert.prefix_hashes(messages)
        last = _last_turn(messages)
        side = (path.endswith("/count_tokens") or not body.get("tools") or last.get("role") != "user"
                or any(mk in ef.user_text(last) for mk in SIDE_QUERY_MARKERS))
        if not side and hashes[-1] not in self.boundaries:
            note = self.decide(body, messages, hashes, headers)
        else:
            note = "replay"
        ins = self.replay(messages, hashes)
        if not ins:
            return None, note
        out = dict(body)
        out["messages"] = insert.apply(messages, ins)
        return out, note

    def decide(self, body: dict, messages: list, hashes: list, headers: dict) -> str:
        session = headers.get("x-claude-code-session-id", "no-session")
        agent = headers.get("x-claude-code-agent-id", "main")
        key = f"{session}|{agent}|{hashes[1][:16]}"
        with self.lock:
            tlock = self.thread_locks.setdefault(key, threading.Lock())
        with tlock:
            if hashes[-1] in self.boundaries:          # a concurrent retry decided already
                return "replay"
            n = len(messages)
            forwarded = insert.apply(messages, self.replay(messages, hashes))
            in_force = insert.effort_in_force(forwarded, (body.get("output_config") or {}).get("effort"))
            t = self.threads.get(key) or ef.Thread()
            t.current = LEVELS.index(in_force)
            cwd = self._cwd(body)
            use_clef = not _local_only(cwd)
            prompt = ef.last_prompt(messages)
            last = _last_turn(messages)
            # Claude Code states its own /effort level on every prompt. Only a *change*
            # of that level is the user taking over; it pauses routing until the next prompt.
            prompting = not ef.has_tool_results(last)
            client = next((m["output_config"]["effort"] for m in reversed(messages) if insert.is_effort_statement(m)), None)
            if client and t.client is not None and client != t.client:
                t.manual = True
            elif prompting:
                t.manual = False
            t.client = client or t.client
            if t.manual:
                target, why, src, ms = t.current, ["manual /effort"], "manual", 0
            elif prompting:
                state = f"USER REQUEST: {prompt[:4000]}"
                target, why, src, ms = ef.decide_task(state, prompt, use_clef)
                t.base, t.fails, t.raised_last = target, 0, False
                t.trail = []
            else:
                round_state, failed = ef.tool_round(messages)
                state = f"USER REQUEST: {prompt[:2000]}\nCURRENT EFFORT: {in_force}\n{round_state}"
                target, why, src, ms = ef.decide_step(t, state, failed, use_clef)
                t.fails = t.fails + 1 if failed else 0
            target = ef.clamp(target, self.lo, self.hi)
            t.raised_last = target > t.current
            effort = LEVELS[target]
            change = effort if effort != in_force else None
            t.current = target
            t.trail = (t.trail + [effort])[-8:]
            records = [{"t": "ins", "h": hashes[-1], "i": n, "e": change},
                       {"t": "thread", "k": key, "s": {"base": t.base, "current": t.current, "fails": t.fails,
                                                      "raised_last": t.raised_last, "trail": t.trail,
                                                      "client": t.client, "manual": t.manual}}]
            with self.lock:
                self._append(*records)               # journal first, then forward
                self.boundaries[hashes[-1]] = (n, change)
                self.threads[key] = t
            self._status(session, effort, in_force, why, src)
            arrow = f"{in_force} -> {effort.upper()}" if change else effort
            return f"{arrow} [{src} {ms}ms] {', '.join(why)}"

    @staticmethod
    def _cwd(body: dict) -> str:
        sysp = body.get("system")
        text = sysp if isinstance(sysp, str) else " ".join(
            b.get("text", "") for b in sysp or [] if isinstance(b, dict))
        m = CWD_RE.search(text)
        return m.group(1) if m else ""

    def _status(self, session, effort, previous, why, src):
        safe = "".join(c for c in session if c.isalnum() or c in "-_") or "unknown"
        try:
            with open(os.path.join(CACHE, f"effort-{safe}.json"), "w") as fh:
                json.dump({"effort": effort, "previous": previous, "why": why, "source": src, "at": time.time()}, fh)
        except OSError:
            pass


def _local_only(cwd: str) -> bool:
    from .judge import _local_only as check
    return bool(cwd) and check(cwd)


def make_handler(gw: Gateway):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # keep stderr quiet; we log ourselves
            pass

        def _proxy(self):
            if self.headers.get(TOKEN_HEADER) != gw.token:
                self.send_error(401, "missing or wrong gateway token")
                return
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items() if k.lower() not in HOP}
            path = self.path
            note, routed = "", False
            if self.command == "POST" and raw and urllib.parse.urlparse(path).path in ("/v1/messages", "/v1/messages/count_tokens"):
                try:
                    body = json.loads(raw)
                    new, note = gw.transform(urllib.parse.urlparse(path).path, body, headers)
                    routed = gw.routable(body)
                    if new is not None:
                        raw = json.dumps(new, ensure_ascii=False).encode()
                        headers["anthropic-beta"] = insert.add_beta(headers.get("anthropic-beta"))
                except ValueError:
                    pass
                except OSError as e:  # journal unwritable: never forward a transcript we cannot replay
                    self.send_error(502, f"judge-clef journal error: {e}")
                    return
            headers["accept-encoding"] = "identity"
            headers["content-length"] = str(len(raw))
            cls = http.client.HTTPSConnection if gw.upstream.scheme == "https" else http.client.HTTPConnection
            conn = cls(gw.upstream.netloc, timeout=600)
            try:
                conn.request(self.command, path, body=raw if raw else None, headers=headers)
                up = conn.getresponse()
                self.send_response(up.status)
                for k, v in up.getheaders():
                    if k.lower() not in DROP_RESPONSE:
                        self.send_header(k, v)
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                head = b""
                while True:
                    chunk = up.read1(65536)
                    if not chunk:
                        break
                    if routed and len(head) < 65536:
                        head += chunk
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                if routed and note:
                    gw.log(f"{headers.get('x-claude-code-session-id', '-')[:8]} {up.status} {note} {_usage(head)}")
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                conn.close()

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _proxy

    return Handler


def _usage(head: bytes) -> str:
    m = re.search(rb'"usage"\s*:\s*(\{[^{}]*(\{[^{}]*\}[^{}]*)*\})', head)
    if not m:
        return ""
    try:
        u = json.loads(m.group(1))
    except ValueError:
        return ""
    return (f"cache_read={u.get('cache_read_input_tokens', 0)} cache_write={u.get('cache_creation_input_tokens', 0)} "
            f"input={u.get('input_tokens', 0)}")


def serve(gw: Gateway, port: int = 0) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(gw))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _last_turn(messages: list) -> dict:
    """Last real message: Claude Code ends a new prompt with its own effort statement."""
    for m in reversed(messages):
        if not insert.is_effort_statement(m):
            return m
    return {}
