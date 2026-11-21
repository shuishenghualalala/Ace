"""Extract recorded assistant responses from an Ace llm.jsonl trace.

The output is a replay fixture for ``tests/e2e/replay_server.py``. Review the
result before committing: prompts and tool arguments can contain local paths,
user data or secrets even though the response text usually does not.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.e2e.replay_server import recorded_responses  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True, type=Path, help="E2E llm.jsonl path")
    parser.add_argument("--output", required=True, type=Path, help="fixture JSONL to write")
    parser.add_argument("--session-id", default="", help="only extract one session")
    parser.add_argument("--force", action="store_true", help="overwrite an existing fixture")
    args = parser.parse_args()

    if not args.trace.is_file():
        parser.error(f"trace does not exist: {args.trace}")
    if args.output.exists() and not args.force:
        parser.error(f"output exists; pass --force to replace it: {args.output}")

    responses = recorded_responses(args.trace, session_id=args.session_id)
    if not responses:
        parser.error("no assistant responses found in the trace")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in responses),
        encoding="utf-8",
    )
    print(f"wrote {len(responses)} responses to {args.output}")
    print("review the fixture for local paths, user data and secrets before committing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
