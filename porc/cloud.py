"""Optional S3 persistence so the bot can run in a stateless container (ECS Fargate / Lambda).

Enabled when PORC_S3_BUCKET is set:
  * the ledger file is pulled from s3://<bucket>/<prefix>/state/processed_loads.json before the run
    and pushed back after every change, so a crash never loses the "already submitted" record;
  * downloads/, logs/ and discovery/ are pushed under <prefix>/runs/<timestamp>/ at the end of the run.
Without the variable everything stays on the local disk exactly as before.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def load_ssm_parameters(prefix: str | None = None) -> int:
    """Load SecureString parameters under PORC_SSM_PREFIX (e.g. /porc-automation/) into os.environ.

    Parameter name after the prefix is the variable name (TPRO_PASSWORD, ZEUS_PASSWORD, ...).
    Existing environment variables win, so a value can still be overridden per function.
    Returns the number of variables set. No-op when the prefix is not configured."""
    prefix = (prefix or os.environ.get("PORC_SSM_PREFIX", "")).strip()
    if not prefix:
        return 0
    import boto3

    ssm = boto3.client("ssm")
    prefix = "/" + prefix.strip("/") + "/"
    count = 0
    token = None
    while True:
        kwargs = {"Path": prefix, "Recursive": False, "WithDecryption": True, "MaxResults": 10}
        if token:
            kwargs["NextToken"] = token
        page = ssm.get_parameters_by_path(**kwargs)
        for p in page.get("Parameters", []):
            name = p["Name"][len(prefix):]
            if name and name not in os.environ:
                os.environ[name] = p["Value"]
                count += 1
        token = page.get("NextToken")
        if not token:
            break
    log.info("SSM: %d parameter(s) loaded from %s", count, prefix)
    return count


class S3Sync:
    def __init__(self, bucket: str, prefix: str = "porc"):
        import boto3  # imported lazily so local runs do not need boto3

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.s3 = boto3.client("s3")
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    @classmethod
    def from_env(cls) -> "S3Sync | None":
        bucket = os.environ.get("PORC_S3_BUCKET", "").strip()
        if not bucket:
            return None
        return cls(bucket, os.environ.get("PORC_S3_PREFIX", "porc"))

    # ---------------------------------------------------------------- ledger
    def _ledger_key(self) -> str:
        return f"{self.prefix}/state/processed_loads.json"

    def pull_ledger(self, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.s3.download_file(self.bucket, self._ledger_key(), str(local))
            log.info("S3: ledger pulled from s3://%s/%s", self.bucket, self._ledger_key())
        except self.s3.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                log.info("S3: no ledger yet at s3://%s/%s (first run)", self.bucket, self._ledger_key())
            else:
                raise

    def push_ledger(self, local: Path) -> None:
        if local.exists():
            self.s3.upload_file(str(local), self.bucket, self._ledger_key())

    # ------------------------------------------------------------- artifacts
    def push_dir(self, local_dir: Path, name: str) -> int:
        if not local_dir.exists():
            return 0
        count = 0
        for path in local_dir.rglob("*"):
            if path.is_file():
                key = f"{self.prefix}/runs/{self.run_id}/{name}/{path.relative_to(local_dir).as_posix()}"
                self.s3.upload_file(str(path), self.bucket, key)
                count += 1
        if count:
            log.info("S3: %d file(s) from %s -> s3://%s/%s/runs/%s/%s/", count, local_dir.name, self.bucket,
                     self.prefix, self.run_id, name)
        return count
