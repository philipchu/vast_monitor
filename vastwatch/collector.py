from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from dotenv import load_dotenv

try:
    import duckdb  # type: ignore
except Exception:  # pragma: no cover - optional
    duckdb = None  # type: ignore

from .client import BACKOFF_BASE_SEC, VAST_MIN_POLL_INTERVAL_SEC, VastAPIError, normalize, search_offers


logger = logging.getLogger("vastwatch.collector")


def _ts_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(db_path: str):
    if db_path.lower().endswith(".duckdb") or (duckdb and db_path.lower().endswith(".ddb")):
        if not duckdb:
            raise RuntimeError("duckdb package not installed, but DuckDB path provided")
        conn = duckdb.connect(database=db_path)
        return conn, "duckdb"
    # default: sqlite
    conn = sqlite3.connect(db_path)
    return conn, "sqlite"


def _init_db(conn, backend: str, schema_path: Path) -> None:
    sql = schema_path.read_text(encoding="utf-8")
    conn.executescript(sql) if backend == "sqlite" else conn.execute(sql)
    _ensure_columns(conn, backend)


def _ensure_columns(conn, backend: str) -> None:
    required_columns = {
        "verified": "INTEGER",
        "deverified": "INTEGER",
        "availability_state": "TEXT",
    }
    if backend == "sqlite":
        cur = conn.execute("PRAGMA table_info(offers_raw)")
        existing = {row[1] for row in cur.fetchall()}
        for col, col_type in required_columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE offers_raw ADD COLUMN {col} {col_type}")
        conn.commit()
    else:  # duckdb
        existing_rows = conn.execute("PRAGMA table_info('offers_raw')").fetchall()
        existing = {row[1] for row in existing_rows}
        for col, col_type in required_columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE offers_raw ADD COLUMN {col} {col_type}")


def _insert_rows(conn, backend: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    cols = [
        "ts",
        "offer_id",
        "machine_id",
        "gpu_name",
        "num_gpus",
        "gpu_frac",
        "gpu_total_ram_gb",
        "dph_total_usd",
        "reliability2",
        "geolocation",
        "type",
        "rentable",
        "rented",
        "verified",
        "deverified",
        "availability_state",
    ]
    values = [tuple(row.get(c) for c in cols) for row in rows]
    placeholders = ",".join(["?"] * len(cols))
    sql = f"INSERT INTO offers_raw ({', '.join(cols)}) VALUES ({placeholders})"
    if backend == "sqlite":
        conn.executemany(sql, values)
        conn.commit()
    else:  # duckdb
        conn.executemany(sql, values)


def _load_filters_from_env() -> Optional[Dict[str, Any]]:
    # Optional: allow passing extra q filters via env JSON
    raw = os.environ.get("VW_EXTRA_FILTERS_JSON")
    if not raw:
        return None
    try:
        import json

        return json.loads(raw)
    except Exception as e:
        logger.warning("Failed to parse VW_EXTRA_FILTERS_JSON: %s", e)
        return None


def main() -> None:
    load_dotenv()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    api_key = os.environ.get("VAST_API_KEY")
    if not api_key:
        logger.error("VAST_API_KEY is not set. Populate .env or environment.")
        sys.exit(1)

    db_path = os.environ.get("VW_DB", "vastwatch.db")
    poll_env = os.environ.get("VW_POLL_INTERVAL_SEC")
    try:
        configured_interval = int(poll_env) if poll_env else None
    except ValueError:
        logger.warning(
            "Invalid VW_POLL_INTERVAL_SEC=%r; falling back to minimum with cushion", poll_env
        )
        configured_interval = None
    base_interval = configured_interval if configured_interval and configured_interval > 0 else VAST_MIN_POLL_INTERVAL_SEC * 6
    poll_interval = max(base_interval, VAST_MIN_POLL_INTERVAL_SEC * 6)
    timeout_sec = float(os.environ.get("VW_TIMEOUT_SEC", 60))
    include_unverified_raw = os.environ.get("VW_INCLUDE_UNVERIFIED", "1").strip()

    conn, backend = _connect(db_path)
    schema_path = Path(__file__).with_name("schema.sql")
    _init_db(conn, backend, schema_path)
    logger.info("DB initialized at %s using %s", db_path, backend)
    logger.info("Collector poll interval set to %s seconds", poll_interval)
    if include_unverified_raw.lower() in {"1", "true", "yes", "on", "all"}:
        logger.info("Including unverified/deverified offers in polling")

    extra_q = _load_filters_from_env()

    backoff_attempt = 0
    while True:
        ts = _ts_now()
        try:
            logger.info("Polling Vast offers (available + rented + unavailable)…")
            available = search_offers(
                rented=False,
                rentable=True,
                extra_filters=extra_q,
                timeout_sec=timeout_sec,
            )
            # Query rented offers: rented=True without rentable filter
            # (rentable=False causes API to return all non-rentable offers)
            rented = search_offers(
                rented=True,
                rentable=None,  # Don't filter by rentable for rented query
                extra_filters=extra_q,
                timeout_sec=timeout_sec,
            )
            unavailable = search_offers(
                rented=False,
                rentable=False,
                extra_filters=extra_q,
                timeout_sec=timeout_sec,
            )
            logger.info(
                "Fetched %d available, %d rented, %d unavailable offers",
                len(available),
                len(rented),
                len(unavailable),
            )

            av_rows = [normalize(of, ts, source_state="available") for of in available]
            r_rows = [normalize(of, ts, source_state="rented") for of in rented]
            un_rows = [normalize(of, ts, source_state="unavailable") for of in unavailable]
            total_rows = len(av_rows) + len(r_rows) + len(un_rows)
            _insert_rows(conn, backend, av_rows + r_rows + un_rows)
            logger.info("Inserted %d rows for ts=%s", total_rows, ts)

            # Reset collector backoff after success
            backoff_attempt = 0
            time.sleep(poll_interval)
        except VastAPIError as e:
            backoff_attempt += 1
            delay = min(BACKOFF_BASE_SEC * 4, BACKOFF_BASE_SEC * max(backoff_attempt, 1))
            logger.warning(
                "Vast API error: %s (status=%s). Backing off %.1fs.", e, getattr(e, "status", None), delay
            )
            time.sleep(delay)
        except Exception as e:
            backoff_attempt += 1
            delay = min(BACKOFF_BASE_SEC * 4, BACKOFF_BASE_SEC * max(backoff_attempt, 1))
            logger.exception("Unexpected error. Backing off %.1fs: %s", delay, e)
            time.sleep(delay)


if __name__ == "__main__":
    main()
