"""One-time setup: copy the bot credentials from .env into AWS SSM Parameter Store under /porc-automation/.

Run from the project folder after `aws sso login --profile paybot-admin`:

    python deploy/put_parameters.py

Passwords and user names are stored as SecureString (encrypted with the account's default SSM KMS key);
URLs and the DB host/port/name as plain String. Values are never printed. Re-run any time a credential
changes; the Lambda reads the parameters at every start (PORC_SSM_PREFIX=/porc-automation).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
PROFILE = os.environ.get("AWS_PROFILE", "paybot-admin")
REGION = os.environ.get("AWS_REGION", "us-east-1")
PREFIX = "/porc-automation/"

# SSM parameter name -> accepted .env keys (first non-empty wins)
MAPPING = {
    "TPRO_BASE_URL": ("TPRO_BASE_URL",),
    "TPRO_USERNAME": ("TPRO_USERNAME",),
    "TPRO_PASSWORD": ("TPRO_PASSWORD",),
    "ZEUS_URL": ("ZEUS_URL", "link"),
    "ZEUS_USERNAME": ("ZEUS_USERNAME", "username"),
    "ZEUS_PASSWORD": ("ZEUS_PASSWORD", "password"),
    "TPRO_DB_HOST": ("TPRO_DB_HOST",),
    "TPRO_DB_PORT": ("TPRO_DB_PORT",),
    "TPRO_DB_NAME": ("TPRO_DB_NAME",),
    "TPRO_DB_USER": ("TPRO_DB_USER",),
    "TPRO_DB_PASSWORD": ("TPRO_DB_PASSWORD",),
    "PORC_REQUESTED_BY_DEFAULT": ("PORC_REQUESTED_BY_DEFAULT",),
}
SECURE = ("PASSWORD", "USERNAME", "USER")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def aws_session() -> boto3.Session:
    """boto3 with the SSO profile; if its cached SSO token is stale while the AWS CLI's is fresh
    (the CLI refreshes tokens on its own), borrow the CLI's exported credentials instead."""
    import json
    import subprocess

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    try:
        session.client("sts").get_caller_identity()
        return session
    except Exception as exc:  # noqa: BLE001 - TokenRetrievalError / SSOTokenLoadError etc.
        print(f"boto3 SSO credentials unusable ({type(exc).__name__}); trying the AWS CLI's credentials")
    try:
        out = subprocess.run(["aws", "configure", "export-credentials", "--profile", PROFILE, "--format", "process"],
                             capture_output=True, text=True, check=True, shell=(os.name == "nt")).stdout
        creds = json.loads(out)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"No valid AWS credentials. Run:  aws sso login --profile {PROFILE}   then re-run this script. ({exc})")
    return boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                         aws_session_token=creds.get("SessionToken"), region_name=REGION)


def main() -> int:
    env = read_env(ROOT / ".env")
    ssm = aws_session().client("ssm")
    written, skipped = [], []
    for name, keys in MAPPING.items():
        value = next((env[k] for k in keys if env.get(k)), None)
        if not value:
            skipped.append(name)
            continue
        kind = "SecureString" if any(s in name for s in SECURE) else "String"
        ssm.put_parameter(Name=PREFIX + name, Value=value, Type=kind, Overwrite=True,
                          Description="PORC Automation (Zeus rate-confirmation bot)")
        written.append(f"{name} ({kind})")
    print(f"written under {PREFIX}: " + ", ".join(written))
    if skipped:
        print("skipped (not in .env): " + ", ".join(skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
