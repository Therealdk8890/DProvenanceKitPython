"""CLI for the model-drift check.

    python -m examples.model_drift --old MODEL --new MODEL \\
        [--prompts FILE.jsonl] [--fake parity|drift | --live] \\
        [--judge lexical|llm] [--judge-model MODEL] [--threshold 0.8] [--db PATH]

Defaults to ``--fake parity`` so it runs with zero setup (no network, no API key). Prints a
per-prompt drift report and exits 1 when the new model drifts, 0 on parity — so it drops
straight into a CI gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence

from .evaluators import (
    DriftEvaluator,
    LexicalSimilarityEvaluator,
    LLMJudgeEvaluator,
)
from .harness import (
    DEFAULT_THRESHOLD,
    DriftCheckResult,
    Prompt,
    record_baseline,
    run_drift_check,
)
from .providers import (
    FakeModelClient,
    ModelClient,
    OpenAIModelClient,
)

# A small built-in prompt set so the demo runs without a --prompts file.
_SAMPLE_PROMPTS: List[Prompt] = [
    Prompt("greeting", "How should I greet a new customer in a support chat?"),
    Prompt("refund", "What is a reasonable refund window for a digital product?"),
    Prompt("units", "How many meters are in a kilometer?"),
    Prompt("capital", "What is the capital of France?"),
    Prompt("password", "What makes a password strong?"),
    Prompt("summary", "Summarize the benefits of unit testing in one sentence."),
    Prompt("email", "Write a one-line out-of-office reply."),
    Prompt("advice", "Give one tip for staying focused while working from home."),
]


def _load_prompts(path: str) -> List[Prompt]:
    """Load prompts from a JSONL file (one ``{"id": ..., "prompt": ...}`` per line)."""
    prompts: List[Prompt] = []
    with open(path, "r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            try:
                prompts.append(Prompt(id=str(record["id"]), text=str(record["prompt"])))
            except KeyError as exc:  # pragma: no cover - user input validation
                raise ValueError(
                    f"{path}:{lineno}: each line needs 'id' and 'prompt' keys"
                ) from exc
    if not prompts:
        raise ValueError(f"{path}: no prompts found")
    return prompts


def _build_client(args: argparse.Namespace) -> ModelClient:
    if args.live:
        return OpenAIModelClient()  # imports openai lazily; reads OPENAI_API_KEY
    return FakeModelClient(mode=args.fake)


def _build_evaluator(args: argparse.Namespace, client: ModelClient) -> DriftEvaluator:
    if args.judge == "llm":
        judge_model = args.judge_model or args.new
        return LLMJudgeEvaluator(client, judge_model)
    return LexicalSimilarityEvaluator()


def _print_report(result: DriftCheckResult, judge: str) -> None:
    print(
        f"Model drift check: OLD={result.old_model}  NEW={result.new_model}  "
        f"(judge={judge}, threshold={result.threshold:.2f})"
    )
    print(f"  {'prompt':<12} {'score':>6}  verdict")
    for entry in result.per_prompt:
        verdict = "DRIFT" if entry.drifted else "ok"
        print(f"  {entry.prompt_id:<12} {entry.score:>6.2f}  {verdict}")
    print()
    drifted = result.drifted_prompts
    if result.passed:
        print(f"Result: PARITY — all {len(result.per_prompt)} prompts within threshold.")
    else:
        print(
            f"Result: DRIFT — {len(drifted)} of {len(result.per_prompt)} prompts drifted: "
            f"{', '.join(drifted)}"
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m examples.model_drift",
        description=(
            "Check whether a replacement model drifts from a soon-to-be-discontinued "
            "model on a fixed prompt set. Exits 1 on drift, 0 on parity."
        ),
    )
    parser.add_argument("--old", required=True, help="the outgoing model (golden baseline)")
    parser.add_argument(
        "--new", help="the replacement model (candidate); required unless --record-golden"
    )
    parser.add_argument(
        "--prompts",
        help="JSONL prompt file (one {'id','prompt'} per line); defaults to a built-in set",
    )

    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--fake",
        choices=["parity", "drift"],
        help="use the deterministic offline client in this mode (default: parity)",
    )
    source.add_argument(
        "--live",
        action="store_true",
        help='call OpenAI live (needs: pip install "dprovenancekit[openai]" + OPENAI_API_KEY)',
    )

    parser.add_argument(
        "--judge",
        choices=["lexical", "llm"],
        default="lexical",
        help="equivalence evaluator (default: lexical; llm needs a live provider)",
    )
    parser.add_argument(
        "--judge-model",
        help="model the llm judge uses (default: the --new model)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"minimum per-prompt equivalence to count as no-drift (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument("--db", help="SQLite path to record the two runs (default: temp file)")
    parser.add_argument(
        "--golden-db",
        help="reuse a golden baseline recorded here (skip calling --old); with "
        "--record-golden, write the baseline here instead",
    )
    parser.add_argument(
        "--record-golden",
        action="store_true",
        help="record ONLY --old's answers as the golden baseline (into --golden-db) and "
        "exit; run this while the outgoing model is still callable",
    )
    args = parser.parse_args(argv)

    # Default source is the zero-setup offline fake in parity mode.
    if not args.live and args.fake is None:
        args.fake = "parity"

    # Argument checks argparse can't express on its own.
    if args.record_golden and args.golden_db is None and args.db is None:
        print(
            "error: --record-golden needs --golden-db (or --db) to write the baseline to",
            file=sys.stderr,
        )
        return 2
    if not args.record_golden and args.new is None:
        print("error: --new is required (unless --record-golden)", file=sys.stderr)
        return 2

    try:
        prompts = _load_prompts(args.prompts) if args.prompts else list(_SAMPLE_PROMPTS)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        client = _build_client(args)
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Record-only: capture the outgoing model's golden baseline and stop. Do this while the
    # old model is still callable; gate candidates against it later with --golden-db.
    if args.record_golden:
        golden_dest = args.golden_db or args.db
        assert golden_dest is not None  # guarded above
        run_id = record_baseline(
            model=args.old, prompts=prompts, client=client, db_path=golden_dest
        )
        print(f"Recorded golden baseline for {args.old} -> {golden_dest}  (run {run_id})")
        return 0

    assert args.new is not None  # guarded above
    evaluator = _build_evaluator(args, client)

    result = run_drift_check(
        old_model=args.old,
        new_model=args.new,
        prompts=prompts,
        client=client,
        evaluator=evaluator,
        threshold=args.threshold,
        db_path=args.db,
        golden_db=args.golden_db,
    )

    _print_report(result, judge=args.judge)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
