"""Tiny eval: does the verdict match the expected label on the fixtures?

Pass bar: at least 8 of 10 labels right with Clef, and the measured context
size must equal the fixture size in every case (that part is deterministic).
Run: python3 evals/run.py [--no-clef] [--dir evals/private]
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from judge_clef import judge  # noqa: E402

PASS_BAR = 0.8


def main():
    args = sys.argv[1:]
    d = args[args.index("--dir") + 1] if "--dir" in args else os.path.join(os.path.dirname(__file__), "fixtures")
    use_clef = "--no-clef" not in args
    os.environ["JUDGE_CLEF_ALWAYS"] = "1"
    cases = json.load(open(os.path.join(d, "cases.json")))
    right = size_ok = 0
    for c in cases:
        v = judge.judge(os.path.join(d, c["name"] + ".jsonl"), c["prompt"], use_clef=use_clef)
        ok = v.label == c["expect"]
        right += ok
        size_ok += v.context_tokens == c["tokens"]
        print(f"{'PASS' if ok else 'FAIL'}  {c['name']:<24} expect={c['expect']:<13} got={v.label:<13} "
              f"score={v.compact_score:.2f} src={v.source} {','.join(v.reasons)}")
    rate = right / len(cases)
    print(f"\nlabels {right}/{len(cases)} ({rate:.0%}), sizes exact {size_ok}/{len(cases)}, bar {PASS_BAR:.0%}")
    sys.exit(0 if rate >= PASS_BAR and size_ok == len(cases) else 1)


if __name__ == "__main__":
    main()
