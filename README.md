# Judge Clef

**Tells you when to `/compact`.** Every turn, Judge Clef measures the exact context size of your Claude Code session and asks [Cloudflare Clef](https://developers.cloudflare.com/workers-ai/models/clef-flash/) whether *now* is a good moment to compact. The answer lands as one line at the end of Claude's reply:

```
Judge Clef: 649k context · compact now 0.93 (task done, new topic, heavy context) · effort hint low
```

## Why

Long sessions get worse near the end: the model starts missing details that are still in the window. Most people never compact. On the author's machine, 196 recent sessions had a median peak context of 108k tokens, a p90 of 274k and a max of 873k, and only one of them ever compacted.

Size alone isn't enough to decide. Compacting in the middle of a debugging session throws away the exact details you need, while compacting right after a task is done (or when you switch topics) costs almost nothing. So Judge Clef splits the job in two:

- **Size is measured, never guessed.** It comes from the `usage` block of the last main-thread assistant message in the transcript (input + cache read + cache write).
- **Timing is judged by Clef**, Cloudflare's typed decision model. Clef returns probabilities, not prose, in about half a second. It answers three yes/no questions:
  - `task_done`: did the last task finish, with nothing left open?
  - `new_topic`: is the new message about something else?
  - `work_in_flight`: would summarising now lose half-done work?

The score is `size_pressure × timing`, which maps to **compact now** (≥ 0.55), **compact soon** (≥ 0.30) or **no need**. Below 60k tokens it never suggests compacting, and it doesn't call Clef at all. Judge Clef only suggests compacting. Claude never compacts on its own.

## Install

Requirements: Python 3.9+ (stdlib only, no dependencies), Claude Code, and a Cloudflare account with Workers AI.

```bash
git clone https://github.com/nicolasmertens/judge-clef ~/judge-clef
```

Add the hook to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "~/judge-clef/bin/judge-clef hook", "timeout": 10 } ] }
    ]
  },
  "statusLine": { "type": "command", "command": "~/judge-clef/bin/judge-clef statusline" }
}
```

Or install it as a plugin: `/plugin marketplace add nicolasmertens/judge-clef`, then `/plugin install judge-clef@judge-clef`.

### Credentials

Provide a Cloudflare account id and an API token with **Workers AI: Read**. Set them directly, or as a command that prints them (useful with a password manager or vault):

```bash
export CLOUDFLARE_ACCOUNT_ID=...            # or JUDGE_CLEF_ACCOUNT_ID
export CLOUDFLARE_API_TOKEN=...             # or JUDGE_CLEF_API_TOKEN
# or:
export CLOUDFLARE_API_TOKEN_CMD="op read op://dev/cloudflare/token"
```

Without credentials it still runs: the footer shows the exact size with neutral timing and is marked `[no clef]`.

## Configuration

| Variable | Default | What it does |
| --- | --- | --- |
| `JUDGE_CLEF_MODEL` | `clef-flash` | `clef` (27B) for higher precision |
| `JUDGE_CLEF_FLOOR` | `60000` | below this, never suggest compacting and skip Clef |
| `JUDGE_CLEF_HEAVY` | `300000` | size pressure reaches 1.0 here |
| `JUDGE_CLEF_TAIL_CHARS` | `24000` | how much recent conversation Clef reads |
| `JUDGE_CLEF_LOCAL_ONLY` | | colon-separated dirs; sessions started there never send text to Clef |
| `JUDGE_CLEF_FOOTER` | `1` | `0` = only cache the verdict for the status line |
| `JUDGE_CLEF_FOOTER_AFTER` | | e.g. `cost footer line` to place the line after another footer |

## Privacy and cost

- **What goes to Cloudflare:** the user and assistant text of the recent turns (up to `JUDGE_CLEF_TAIL_CHARS`), the names of the tools that ran, and the new prompt. **Tool results (file contents, command output) are never sent.**
- **Sensitive projects:** add them to `JUDGE_CLEF_LOCAL_ONLY` and nothing leaves the machine. The check uses the session's working directory.
- **Cost:** a typical call is 1.5k to 8k input tokens. At $0.09 per million input tokens for clef-flash, that's under $0.001 per prompt, and output is not billed. Calls are skipped entirely below the floor. Small volumes fit in the Workers AI free daily allocation.

## Eval

`evals/` has 10 synthetic sessions with an expected label each (no real user data). The pass bar is 8/10 labels right and exact sizes on all 10.

```bash
python3 evals/make_fixtures.py && python3 evals/run.py           # with Clef
python3 evals/run.py --no-clef                                    # size-only baseline
```

At v0.1.0:

| Mode | Labels right | Sizes exact |
| --- | --- | --- |
| Clef (clef-flash) | **9/10** | 10/10 |
| Size only (no Clef) | 4/10 | 10/10 |

The one miss is debatable: a 180k session with an unrelated new topic gets "compact now" where the label says "soon". To tune the thresholds on your own sessions, put labeled fixtures in `evals/private/` (gitignored) and run `python3 evals/run.py --dir evals/private`.

## Other commands

```bash
judge-clef check SESSION.jsonl --prompt "next message" [--json] [--no-clef]
judge-clef scan ~/.claude/projects        # peak context per session, how many ever compacted
```

## Placeholder: effort hint

The same Clef call also answers a fourth question: how much reasoning effort the next message needs (`low` / `medium` / `high` / `xhigh`). Today this is **only a hint** in the footer and status line. Nothing changes the effort level yet.

The full version is what [jev-opus](https://github.com/WXK-AI/jev-opus) does with TypeSafe Jev: a local gateway re-picks effort before every API call without breaking the prompt cache. Clef follows the same System One API as Jev ("drop-in compatible", per Cloudflare's [launch changelog](https://developers.cloudflare.com/changelog/post/2026-10-01-clef-workers-ai/)), so that gateway could run on Clef by swapping its client's endpoint and model. That's the planned next step, once the hint has shown it agrees with what you'd have picked.

## License

MIT
