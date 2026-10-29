"""Entry point: python run_bot.py [--dry-run] [--limit N] [--load-id ID] [--headed] [--no-submit] [--discover]"""
import sys

from porc.config import ConfigError
from porc.runner import run

if __name__ == "__main__":
    try:
        sys.exit(run())
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
