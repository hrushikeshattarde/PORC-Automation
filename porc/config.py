"""Settings loaded from the project .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
# Where state/, downloads/, logs/, discovery/ live. In AWS Lambda the image is read-only, so the
# handler points this at /tmp. Locally it is the project folder.
DATA_ROOT = Path(os.environ.get("PORC_DATA_DIR") or ROOT)
FILE_VALUES = {k: (v or "") for k, v in dotenv_values(ROOT / ".env").items()}


def _env(*names: str, default: str | None = None) -> str | None:
    """Upper-case keys may be overridden by the process environment; lower-case aliases (link /
    username / password) are read from the .env file only, because Windows already defines
    USERNAME (case-insensitively) and would otherwise shadow the Zeus login."""
    for name in names:
        value = os.environ.get(name) if name.isupper() else None
        if value in (None, ""):
            value = FILE_VALUES.get(name)
        if value not in (None, ""):
            return value.strip()
    return default


class ConfigError(RuntimeError):
    """Required configuration is missing (raised instead of exiting so Lambda keeps its warm runtime)."""


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass(frozen=True)
class Settings:
    # Transport Pro
    tpro_base_url: str
    tpro_username: str
    tpro_password: str
    # Zeus / SDI supplier portal
    zeus_url: str
    zeus_username: str
    zeus_password: str
    # Transport Pro Postgres replica (read-only; source of the load "Internal Comments" = Zeus Requested By)
    tpro_db_host: str = ""
    tpro_db_port: int = 5432
    tpro_db_name: str = ""
    tpro_db_user: str = ""
    tpro_db_password: str = ""
    # Business rules
    customer_id: int = 8828
    customer_name: str = "Walmart c/o SDI MRO"
    lookback_days: int = 14
    start_date: str = ""                                     # go-live date (YYYY-MM-DD): loads delivered earlier are never processed
    min_freight: float = 0.01                                # loads whose Freight Bill total is below this are skipped (so $0 never gets an RC)
    rate_confirmation_doc_type: str = "Rate Confirmation"   # TPro document type name for upload
    rate_confirmation_doc_type_id: int = 143                 # same type, numeric id (idempotency check)
    bol_doc_type_id: int = 12                                # "Bill Of Lading"
    requested_by_default: str = ""
    strict_store_match: bool = False
    headless: bool = True
    # Paths
    downloads_dir: Path = DATA_ROOT / "downloads"
    logs_dir: Path = DATA_ROOT / "logs"
    state_dir: Path = DATA_ROOT / "state"
    discovery_dir: Path = DATA_ROOT / "discovery"

    @classmethod
    def load(cls) -> "Settings":
        missing = []
        tpro_base = _env("TPRO_BASE_URL", default="https://cli.transportpro.net/publicapi/v1")
        tpro_user = _env("TPRO_USERNAME")
        tpro_pass = _env("TPRO_PASSWORD")
        zeus_url = _env("ZEUS_URL", "link", default="https://supplierportal.sdi.com/signin")
        zeus_user = _env("ZEUS_USERNAME", "username")
        zeus_pass = _env("ZEUS_PASSWORD", "password")
        for key, val in (("TPRO_USERNAME", tpro_user), ("TPRO_PASSWORD", tpro_pass),
                         ("ZEUS_USERNAME/username", zeus_user), ("ZEUS_PASSWORD/password", zeus_pass)):
            if not val:
                missing.append(key)
        if missing:
            raise ConfigError(f"Missing required configuration values: {', '.join(missing)} "
                              f"(.env locally, or SSM parameters under PORC_SSM_PREFIX in AWS)")
        return cls(
            tpro_base_url=tpro_base.rstrip("/"),
            tpro_username=tpro_user,
            tpro_password=tpro_pass,
            zeus_url=zeus_url,
            zeus_username=zeus_user,
            zeus_password=zeus_pass,
            tpro_db_host=_env("TPRO_DB_HOST", default="") or "",
            tpro_db_port=int(_env("TPRO_DB_PORT", default="5432")),
            tpro_db_name=_env("TPRO_DB_NAME", default="") or "",
            tpro_db_user=_env("TPRO_DB_USER", default="") or "",
            tpro_db_password=_env("TPRO_DB_PASSWORD", default="") or "",
            customer_id=int(_env("PORC_CUSTOMER_ID", default="8828")),
            lookback_days=int(_env("PORC_LOOKBACK_DAYS", default="14")),
            start_date=_env("PORC_START_DATE", default="") or "",
            min_freight=float(_env("PORC_MIN_FREIGHT", default="0.01")),
            requested_by_default=_env("PORC_REQUESTED_BY_DEFAULT", default="") or "",
            strict_store_match=_bool(_env("PORC_STRICT_STORE_MATCH"), False),
            headless=_bool(_env("PORC_HEADLESS"), True),
        )

    @property
    def tpro_db_enabled(self) -> bool:
        return bool(self.tpro_db_host and self.tpro_db_name and self.tpro_db_user and self.tpro_db_password)

    def ensure_dirs(self) -> None:
        for d in (self.downloads_dir, self.logs_dir, self.state_dir, self.discovery_dir):
            d.mkdir(parents=True, exist_ok=True)
