"""AWS Lambda entry point (container image).

Event shape (all optional):
  {"args": ["--limit", "8"]}            CLI arguments for the bot, same as run_bot.py
Defaults to a normal unattended run capped at 8 loads so it fits the 15-minute Lambda limit;
the next scheduled run picks up whatever is left.

Environment expected on the function:
  PORC_SSM_PREFIX   e.g. /porc-automation   (SecureString parameters: TPRO_USERNAME, TPRO_PASSWORD,
                    ZEUS_USERNAME, ZEUS_PASSWORD, TPRO_DB_HOST, TPRO_DB_NAME, TPRO_DB_USER, TPRO_DB_PASSWORD, ...)
  PORC_S3_BUCKET    bucket that keeps state/processed_loads.json and per-run artifacts
"""
from __future__ import annotations

import logging
import os
import sys

# Lambda's image filesystem is read-only except /tmp; Chromium and the bot both need writable dirs.
os.environ.setdefault("PORC_DATA_DIR", "/tmp/porc")
os.environ.setdefault("HOME", "/tmp")
os.environ.setdefault("XDG_CONFIG_HOME", "/tmp/.config")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/.cache")
os.environ.setdefault("PORC_HEADLESS", "true")
os.environ.setdefault("PORC_IN_LAMBDA", "1")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

log = logging.getLogger("porc.lambda")


def handler(event, context):
    from porc.cloud import load_ssm_parameters

    load_ssm_parameters()               # must run before porc.config builds Settings
    from porc.runner import run

    from porc.config import ConfigError

    args = list((event or {}).get("args") or ["--limit", "8"])
    log.info("PORC Lambda start: args=%s request_id=%s", args, getattr(context, "aws_request_id", "?"))
    try:
        rc = run(args)
    except ConfigError as exc:
        log.error("%s", exc)
        return {"exit_code": 2, "error": str(exc), "args": args}
    return {"exit_code": rc, "args": args}
