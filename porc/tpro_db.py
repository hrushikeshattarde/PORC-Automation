"""Read-only access to the Transport Pro Postgres replica.

Used for the one field the public API does not return: the load-level "Internal Comments"
(`public.loads.gn_comments`), which holds the Zeus "Requested By" person, e.g.
"123456789 Jane Smith", "Sam Lee", "REF# 234567890     PAT PORTER".

Configure in .env: TPRO_DB_HOST, TPRO_DB_PORT, TPRO_DB_NAME, TPRO_DB_USER, TPRO_DB_PASSWORD.
Every connection is opened read-only; the bot never writes to this database.
"""
from __future__ import annotations

import logging

import psycopg

log = logging.getLogger(__name__)


class TProDBError(RuntimeError):
    pass


class TProDB:
    def __init__(self, host: str, port: int, dbname: str, user: str, password: str, connect_timeout: int = 20):
        self._conninfo = dict(host=host, port=port, dbname=dbname, user=user, password=password,
                              connect_timeout=connect_timeout, sslmode="prefer")
        self._conn: psycopg.Connection | None = None

    def _connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            try:
                self._conn = psycopg.connect(**self._conninfo)
                self._conn.read_only = True
                self._conn.autocommit = True
            except psycopg.Error as exc:
                raise TProDBError(f"could not connect to the TPro database: {exc}") from exc
        return self._conn

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()

    def get_internal_comments(self, load_id: int) -> str | None:
        """Return loads.gn_comments for the load (None when the load is unknown)."""
        try:
            with self._connection().cursor() as cur:
                cur.execute('select "gn_comments" from public.loads where "id" = %s', (load_id,))
                row = cur.fetchone()
        except psycopg.Error as exc:
            self.close()
            raise TProDBError(f"query failed for load {load_id}: {exc}") from exc
        if row is None:
            log.warning("TPro DB: load %s not found in public.loads", load_id)
            return None
        value = (row[0] or "").strip()
        log.info("TPro DB: load %s Internal Comments = %r", load_id, value)
        return value
