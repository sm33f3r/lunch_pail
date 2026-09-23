"""
reporter/__main__.py

CLI entrypoint for the reporter pipeline.

Usage:
    python -m reporter          # run forever (default, for deployment)
    python -m reporter --once   # run one cycle and exit (useful for debugging)
"""

import argparse

from reporter.orchestrator import run_forever, run_once


def main() -> None:
    parser = argparse.ArgumentParser(prog="reporter", description="NFL market reporter")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one pipeline cycle and exit instead of looping forever.",
    )
    args = parser.parse_args()

    if args.once:
        run_once()
    else:
        run_forever()


if __name__ == "__main__":
    main()
