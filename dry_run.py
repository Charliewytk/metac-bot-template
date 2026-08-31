#!/usr/bin/env python3
"""
Offline dry-run for the Metaculus template.

Loads local fixture posts, prints the duplicate-forecast plan, then
re-runs the planner as if those forecasts had landed. The second pass
must be empty. This process never talks to Metaculus and cannot submit.

    python dry_run.py
    python dry_run.py --fixtures tests/fixtures/posts.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from forecast_guard import (
    SubmitBlocked,
    decide_forecasts,
    install_dry_run_network_guard,
    mark_questions_forecasted,
    resolve_publish_intent,
    set_publish_allowed,
)


DEFAULT_FIXTURES = Path(__file__).resolve().parent / "tests" / "fixtures" / "posts.json"


def load_posts(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "results" in payload:
        return list(payload["results"])
    if isinstance(payload, list):
        return payload
    raise ValueError(f"Fixture {path} must be a list of posts or {{'results': [...]}}")


def _print_targets(label: str, decision) -> None:
    print(f"{label}: {len(decision.targets)} to forecast, {len(decision.skipped)} skipped")
    for target in decision.targets:
        print(
            f"  FORECAST  post={target.post_id} q={target.question_id} "
            f"kind={target.kind} type={target.question_type}  {target.title}"
        )
    for target in decision.skipped:
        why = "already forecasted" if target.already_forecasted else f"status={target.status}"
        print(
            f"  SKIP      post={target.post_id} q={target.question_id} "
            f"kind={target.kind} ({why})  {target.title}"
        )


def run_offline_dry_run(fixtures: Path) -> int:
    # Latch shut before anything else. --submit is not accepted here.
    set_publish_allowed(False)
    install_dry_run_network_guard()
    if resolve_publish_intent(submit=False, dry_run=True):
        raise SubmitBlocked("Offline dry-run resolved publish=True; refusing to continue.")

    posts = load_posts(fixtures)
    first = decide_forecasts(posts, skip_already=True)
    print(f"🤖  offline dry-run  fixtures={fixtures}")
    print("    publish=no  (this path cannot submit)\n")
    _print_targets("First pass", first)

    after = mark_questions_forecasted(posts, first.target_ids)
    second = decide_forecasts(after, skip_already=True)
    print()
    _print_targets("Second pass (idempotency)", second)

    if second.targets:
        print("\n❌  Second pass still wanted to forecast; guard is not idempotent.")
        return 1

    print("\n✅  Dry-run complete. No Metaculus writes. Second pass was a no-op.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline dry-run: plan forecasts from local fixtures, never submit."
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURES,
        help="JSON file of Metaculus posts (default: tests/fixtures/posts.json)",
    )
    args = parser.parse_args(argv)
    if not args.fixtures.is_file():
        print(f"❌  Fixture file not found: {args.fixtures}", file=sys.stderr)
        return 2
    return run_offline_dry_run(args.fixtures)


if __name__ == "__main__":
    raise SystemExit(main())
