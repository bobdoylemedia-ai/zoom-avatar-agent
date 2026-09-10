"""Stop the agent worker without losing the meeting notes.

    uv run python src/stop_agent.py

The old version of this force-killed the worker and stopped there, so a meeting
that was still in progress lost its recap, PDF and email. Stopping the worker
still cannot be made graceful on Windows (see catalog.stop_worker), so instead
this writes any missing notes straight from the transcript afterwards -- which
also covers a crash or a closed window.
"""

from __future__ import annotations

import pathlib
import sys

from dotenv import load_dotenv

_ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(_ROOT / ".env.local")
load_dotenv(_ROOT / ".env")

import catalog  # noqa: E402


def main() -> int:
    status = catalog.worker_status()
    if not status["running"]:
        print("The agent was not running.")
    else:
        print(f"Asking the agent to stop (pids {', '.join(str(p) for p in status['pids'])})...")
        result = catalog.stop_worker()
        print(f"Stopped {result['stopped']} process(es).")

    pending = catalog.transcripts_without_notes()
    if not pending:
        print("Every meeting has its notes.")
        return 0

    print(f"\n{len(pending)} meeting(s) still need notes. Writing them now...")
    results = catalog.reconcile_notes()
    for r in results:
        print(f"  {r['stem']}: {'done' if r['ok'] else 'FAILED - ' + r.get('error', '?')}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
