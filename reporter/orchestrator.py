"""
reporter/orchestrator.py

Runs the full report-writing pipeline on a schedule.

run_once()   -- one pipeline cycle; catches and logs errors, never raises.
run_forever() -- loops run_once() every settings.polling_interval_seconds.
"""

from __future__ import annotations

import time

from reporter.config import settings
from reporter.report.report_writer import write_all_reports


def run_once() -> None:
    """
    Execute one full pipeline cycle.

    Calls write_all_reports(), prints a summary to stdout, and swallows any
    exception so a transient failure (network outage, bad API response) does
    not crash the process or the surrounding run_forever() loop.
    """
    try:
        written = write_all_reports()
        count = len(written)
        if count:
            print(f"[reporter] Cycle complete: {count} report(s) written.")
            for path in written:
                print(f"  {path.name}")
        else:
            print("[reporter] Cycle complete: no reportable games (0 reports written).")
    except Exception as exc:
        print(f"[reporter] ERROR in cycle: {exc}")


def run_forever() -> None:
    """
    Loop run_once() indefinitely, sleeping between cycles.

    Interval is taken from settings.polling_interval_seconds.
    KeyboardInterrupt and SystemExit propagate normally so the container
    can be stopped cleanly.
    """
    interval = settings.polling_interval_seconds
    print(f"[reporter] Starting loop (interval={interval}s). Press Ctrl-C to stop.")
    while True:
        run_once()
        time.sleep(interval)
