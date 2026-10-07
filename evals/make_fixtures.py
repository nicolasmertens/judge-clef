"""Build the synthetic eval fixtures (no real user data in this public repo).

Each case is a tiny Claude Code style transcript with a fake usage block that
sets the context size, the next user prompt, and the expected label.
Run: python3 evals/make_fixtures.py
"""

import json
import os

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

CASES = [
    # name, context tokens, [(role, text)], next prompt, expected label
    ("small-new-topic", 40_000,
     [("user", "Rename the config loader to load_settings."), ("assistant", "Done, renamed in 3 files, tests pass.")],
     "Now write a haiku about autumn.", "no need"),
    ("heavy-done-new-topic", 520_000,
     [("user", "Fix the flaky date parser test."), ("assistant", "Fixed: timezone offset was dropped. All 214 tests pass, committed as a1b2c3."),
      ("user", "great thanks"), ("assistant", "You're welcome.")],
     "Different thing: draft a short email to my landlord about the broken heater.", "compact now"),
    ("heavy-mid-debug", 520_000,
     [("user", "The upload endpoint returns 500."), ("assistant", "Traced it to the S3 client; trying a retry wrapper now."),
      ("assistant", "Retry wrapper added, still 500. The signature looks wrong, checking the clock skew next.")],
     "still failing with the same SignatureDoesNotMatch, keep going", "no need"),
    ("heavy-done-same-topic", 420_000,
     [("user", "Add pagination to the users API."), ("assistant", "Pagination added with cursor tokens; tests pass and docs updated.")],
     "Now add the same pagination to the orders API.", "compact soon"),
    ("medium-done-new-topic", 180_000,
     [("user", "Upgrade the project to Node 26."), ("assistant", "Upgraded; CI is green and the lockfile is regenerated.")],
     "Unrelated: what's a good name for a recipe app?", "compact soon"),
    ("huge-new-topic", 850_000,
     [("user", "Refactor the billing module."), ("assistant", "Refactor complete and merged; 0 failing tests.")],
     "Let's plan my trip to Lisbon next month.", "compact now"),
    ("huge-waiting-answer", 800_000,
     [("user", "Compare these three database options for me in detail."),
      ("assistant", "I've analysed Postgres and SQLite so far; DuckDB analysis is next, then the recommendation.")],
     "go on", "no need"),
    ("medium-mid-edit", 200_000,
     [("user", "Split the 2,000 line server.ts into modules."), ("assistant", "Moved routes and auth into modules; db and jobs still to go.")],
     "continue with db", "no need"),
    ("tiny", 9_000,
     [("user", "hi"), ("assistant", "Hi! What are we working on?")],
     "list the files in this folder", "no need"),
    ("heavy-done-thanks", 450_000,
     [("user", "Write the migration for the new invoices table."), ("assistant", "Migration written, applied to staging, verified with a select. Done.")],
     "thanks! next up, help me design the onboarding screens for the mobile app", "compact now"),
]


def write(name, tokens, turns):
    lines = []
    for i, (role, text) in enumerate(turns):
        if role == "user":
            lines.append({"type": "user", "isSidechain": False, "message": {"role": "user", "content": text}})
        else:
            last = i == len(turns) - 1
            usage = {"input_tokens": 2, "cache_read_input_tokens": tokens - 2 if last else tokens // 2,
                     "cache_creation_input_tokens": 0, "output_tokens": 100}
            lines.append({"type": "assistant", "isSidechain": False,
                          "message": {"role": "assistant", "model": "claude-opus-5-5", "usage": usage,
                                      "content": [{"type": "text", "text": text}]}})
    with open(os.path.join(HERE, name + ".jsonl"), "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")


if __name__ == "__main__":
    os.makedirs(HERE, exist_ok=True)
    cases = []
    for name, tokens, turns, prompt, label in CASES:
        write(name, tokens, turns)
        cases.append({"name": name, "tokens": tokens, "prompt": prompt, "expect": label})
    with open(os.path.join(HERE, "cases.json"), "w") as fh:
        json.dump(cases, fh, indent=2)
    print(f"wrote {len(cases)} fixtures to {HERE}")
