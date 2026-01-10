from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Sequence

try:
    import duckdb  # type: ignore
except Exception:  # pragma: no cover - optional
    duckdb = None  # type: ignore

try:
    from rich import box
    from rich.console import Console
    from rich.table import Table

    _RICH_CONSOLE: Console | None = Console()  # type: ignore[assignment]
except Exception:  # pragma: no cover - optional
    Table = None  # type: ignore
    box = None  # type: ignore
    _RICH_CONSOLE = None

VAST_MIN_POLL_INTERVAL_SEC = 60
ASSUMPTION_WARNING = (
    "NOTE: Reports treat offers with rentable=0 as utilized. If a host is offline, paused, or under"
    " maintenance, utilization will be overstated. API-reported rented counts are shown separately."
)
_WARNING_PRINTED = False


def _maybe_print_warning() -> None:
    global _WARNING_PRINTED
    if not _WARNING_PRINTED:
        print(ASSUMPTION_WARNING)
        _WARNING_PRINTED = True


LATEST_SNAPSHOT_QUERY = """
WITH latest AS (
  SELECT *
  FROM offers_raw
  WHERE ts = (SELECT MAX(ts) FROM offers_raw)
),
agg AS (
  SELECT
    gpu_name,
    CASE
      WHEN num_gpus BETWEEN 1 AND 10 THEN CAST(num_gpus AS TEXT) || 'x'
      WHEN num_gpus > 10 THEN '10x+'
      ELSE 'unknown'
    END AS gpus_per_machine,
    COUNT(*) AS total_offers,
    SUM(CASE WHEN rentable = 1 THEN 1 ELSE 0 END) AS available_offers,
    SUM(CASE WHEN rentable = 0 THEN 1 ELSE 0 END) AS assumed_utilized_offers,
    SUM(CASE WHEN rented = 1 THEN 1 ELSE 0 END) AS api_rented_offers,
    SUM(CASE WHEN rentable = 0 AND COALESCE(rented,0) = 0 THEN 1 ELSE 0 END) AS unrentable_unflagged_offers,
    SUM(CASE WHEN rentable IS NULL THEN 1 ELSE 0 END) AS unknown_rentable_offers,
    ROUND(
      100.0 * CAST(SUM(CASE WHEN rentable = 0 THEN 1 ELSE 0 END) AS FLOAT) /
      NULLIF(COUNT(*),0), 1
    ) AS assumed_utilization_pct,
    ROUND(
      100.0 * CAST(SUM(CASE WHEN rented = 1 THEN 1 ELSE 0 END) AS FLOAT) /
      NULLIF(COUNT(*),0), 1
    ) AS api_rented_pct,
    ROUND(AVG(CASE WHEN rentable = 1 THEN dph_total_usd END), 3) AS avg_price_available,
    ROUND(AVG(CASE WHEN rentable = 0 THEN dph_total_usd END), 3) AS avg_price_utilized,
    SUM(CASE WHEN COALESCE(verified, 0) = 1 THEN 1 ELSE 0 END) AS verified_offers,
    SUM(CASE WHEN COALESCE(deverified, 0) = 1 THEN 1 ELSE 0 END) AS deverified_offers,
    AVG(num_gpus) AS avg_gpu_count
  FROM latest
  {where_clause}
  GROUP BY 1,2
),
occupancy AS (
  SELECT
    gpu_name,
    CASE
      WHEN num_gpus BETWEEN 1 AND 10 THEN CAST(num_gpus AS TEXT) || 'x'
      WHEN num_gpus > 10 THEN '10x+'
      ELSE 'unknown'
    END AS gpus_per_machine,
    COUNT(*) AS occupancy_samples,
    ROUND(AVG(CASE WHEN rentable = 0 THEN 1.0 ELSE 0.0 END) * 100.0, 2) AS assumed_rented_time_pct,
    ROUND(AVG(CASE WHEN rented = 1 THEN 1.0 ELSE 0.0 END) * 100.0, 2) AS api_rented_time_pct
  FROM offers_raw
  GROUP BY 1,2
)
SELECT
  DENSE_RANK() OVER (
    ORDER BY
      agg.assumed_utilized_offers DESC,
      agg.assumed_utilization_pct DESC,
      agg.available_offers DESC
  ) AS util_rank,
  agg.gpu_name,
  agg.gpus_per_machine AS gpus,
  agg.total_offers AS offers_total,
  agg.available_offers AS offers_avail,
  agg.assumed_utilized_offers AS offers_util_assumed,
  agg.api_rented_offers AS offers_util_api,
  agg.unrentable_unflagged_offers AS offers_unflagged,
  agg.unknown_rentable_offers AS offers_rentable_unknown,
  agg.assumed_utilization_pct AS util_pct_assumed,
  agg.api_rented_pct AS util_pct_api,
  agg.avg_price_available AS price_avail_avg,
  agg.avg_price_utilized AS price_util_avg,
  ROUND(
    CASE
      WHEN agg.avg_gpu_count IS NULL OR agg.avg_gpu_count = 0 OR agg.avg_price_utilized IS NULL THEN NULL
      ELSE agg.avg_price_utilized * (agg.assumed_utilization_pct / 100.0) / agg.avg_gpu_count
    END,
    4
  ) AS "ex$_per_gpu",
  agg.verified_offers,
  agg.deverified_offers,
  COALESCE(occupancy.assumed_rented_time_pct, 0.0) AS time_pct_assumed,
  COALESCE(occupancy.api_rented_time_pct, 0.0) AS time_pct_api,
  COALESCE(occupancy.occupancy_samples, 0) AS occupancy_samples
FROM agg
LEFT JOIN occupancy
  ON occupancy.gpu_name = agg.gpu_name
  AND occupancy.gpus_per_machine = agg.gpus_per_machine
ORDER BY
  util_rank,
  agg.gpu_name,
  gpus;
"""

LATEST_SNAPSHOT_QUERY_SQLITE_FALLBACK = """
WITH latest AS (
  SELECT *
  FROM offers_raw
  WHERE ts = (SELECT MAX(ts) FROM offers_raw)
),
agg AS (
  SELECT
    gpu_name,
    CASE
      WHEN num_gpus BETWEEN 1 AND 10 THEN CAST(num_gpus AS TEXT) || 'x'
      WHEN num_gpus > 10 THEN '10x+'
      ELSE 'unknown'
    END AS gpus_per_machine,
    COUNT(*) AS total_offers,
    SUM(CASE WHEN rentable = 1 THEN 1 ELSE 0 END) AS available_offers,
    SUM(CASE WHEN rentable = 0 THEN 1 ELSE 0 END) AS assumed_utilized_offers,
    SUM(CASE WHEN rented = 1 THEN 1 ELSE 0 END) AS api_rented_offers,
    SUM(CASE WHEN rentable = 0 AND COALESCE(rented,0) = 0 THEN 1 ELSE 0 END) AS unrentable_unflagged_offers,
    SUM(CASE WHEN rentable IS NULL THEN 1 ELSE 0 END) AS unknown_rentable_offers,
    ROUND(
      100.0 * CAST(SUM(CASE WHEN rentable = 0 THEN 1 ELSE 0 END) AS FLOAT) /
      NULLIF(COUNT(*),0), 1
    ) AS assumed_utilization_pct,
    ROUND(
      100.0 * CAST(SUM(CASE WHEN rented = 1 THEN 1 ELSE 0 END) AS FLOAT) /
      NULLIF(COUNT(*),0), 1
    ) AS api_rented_pct,
    ROUND(AVG(CASE WHEN rentable = 1 THEN dph_total_usd END), 3) AS avg_price_available,
    ROUND(AVG(CASE WHEN rentable = 0 THEN dph_total_usd END), 3) AS avg_price_utilized,
    SUM(CASE WHEN COALESCE(verified, 0) = 1 THEN 1 ELSE 0 END) AS verified_offers,
    SUM(CASE WHEN COALESCE(deverified, 0) = 1 THEN 1 ELSE 0 END) AS deverified_offers,
    AVG(num_gpus) AS avg_gpu_count
  FROM latest
  {where_clause}
  GROUP BY 1,2
),
occupancy AS (
  SELECT
    gpu_name,
    CASE
      WHEN num_gpus BETWEEN 1 AND 10 THEN CAST(num_gpus AS TEXT) || 'x'
      WHEN num_gpus > 10 THEN '10x+'
      ELSE 'unknown'
    END AS gpus_per_machine,
    COUNT(*) AS occupancy_samples,
    ROUND(AVG(CASE WHEN rentable = 0 THEN 1.0 ELSE 0.0 END) * 100.0, 2) AS assumed_rented_time_pct,
    ROUND(AVG(CASE WHEN rented = 1 THEN 1.0 ELSE 0.0 END) * 100.0, 2) AS api_rented_time_pct
  FROM offers_raw
  GROUP BY 1,2
)
SELECT
  DENSE_RANK() OVER (
    ORDER BY
      agg.assumed_utilized_offers DESC,
      agg.assumed_utilization_pct DESC,
      agg.available_offers DESC
  ) AS util_rank,
  agg.gpu_name,
  agg.gpus_per_machine AS gpus,
  agg.total_offers AS offers_total,
  agg.available_offers AS offers_avail,
  agg.assumed_utilized_offers AS offers_util_assumed,
  agg.api_rented_offers AS offers_util_api,
  agg.unrentable_unflagged_offers AS offers_unflagged,
  agg.unknown_rentable_offers AS offers_rentable_unknown,
  agg.assumed_utilization_pct AS util_pct_assumed,
  agg.api_rented_pct AS util_pct_api,
  agg.avg_price_available AS price_avail_avg,
  agg.avg_price_utilized AS price_util_avg,
  ROUND(
    CASE
      WHEN agg.avg_gpu_count IS NULL OR agg.avg_gpu_count = 0 OR agg.avg_price_utilized IS NULL THEN NULL
      ELSE agg.avg_price_utilized * (agg.assumed_utilization_pct / 100.0) / agg.avg_gpu_count
    END,
    4
  ) AS "ex$_per_gpu",
  agg.verified_offers,
  agg.deverified_offers,
  COALESCE(occupancy.assumed_rented_time_pct, 0.0) AS time_pct_assumed,
  COALESCE(occupancy.api_rented_time_pct, 0.0) AS time_pct_api,
  COALESCE(occupancy.occupancy_samples, 0) AS occupancy_samples
FROM agg
LEFT JOIN occupancy
  ON occupancy.gpu_name = agg.gpu_name
  AND occupancy.gpus_per_machine = agg.gpus_per_machine
ORDER BY
  util_rank,
  agg.gpu_name,
  gpus;
"""


def _connect(db_path: str):
    if db_path.lower().endswith(".duckdb") or (duckdb and db_path.lower().endswith(".ddb")):
        if not duckdb:
            print("duckdb package not installed, but DuckDB path provided", file=sys.stderr)
            sys.exit(2)
        return duckdb.connect(database=db_path), "duckdb"
    return sqlite3.connect(db_path), "sqlite"


def _print_rows(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    if _RICH_CONSOLE and Table:
        table = Table(
            box=box.SIMPLE if box else None,
            show_lines=False,
            pad_edge=False,
            expand=True,
            header_style="bold",
        )
        for header in headers:
            table.add_column(header, overflow="fold", no_wrap=False)
        for row in rows:
            table.add_row(*("" if value is None else str(value) for value in row))
        _RICH_CONSOLE.print(table)
        return

    print("\t".join(headers))
    for r in rows:
        print("\t".join("" if v is None else str(v) for v in r))


def _print_tsv(cursor, rows: Sequence[Sequence[Any]]) -> None:
    colnames = [d[0] for d in cursor.description]
    _print_rows(colnames, rows)


def _normalize_filter_values(values: Sequence[str] | None) -> list[str]:
    if not values:
        return []
    normalized: list[str] = []
    for value in values:
        if not value:
            continue
        parts = [part.strip() for part in value.split(',')]
        normalized.extend(part for part in parts if part)
    return normalized


def _build_gpu_filters(
    gpu_names: Sequence[str] | None,
    gpu_counts: Sequence[str] | None,
) -> tuple[str, list[str], list[int]]:
    clauses: list[str] = []
    name_tokens = [token.lower() for token in _normalize_filter_values(gpu_names)]
    if name_tokens:
        name_clauses = [
            "LOWER(gpu_name) LIKE '%' || ? || '%'"
            for _ in name_tokens
        ]
        clauses.append(f"({' OR '.join(name_clauses)})")
    count_tokens = []
    for token in _normalize_filter_values(gpu_counts):
        try:
            count_tokens.append(int(token))
        except ValueError:
            continue
    if count_tokens:
        placeholders = ",".join("?" for _ in count_tokens)
        clauses.append(f"num_gpus IN ({placeholders})")
    return " AND ".join(clauses), name_tokens, count_tokens


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
    else:
        existing_rows = conn.execute("PRAGMA table_info('offers_raw')").fetchall()
        existing = {row[1] for row in existing_rows}
        for col, col_type in required_columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE offers_raw ADD COLUMN {col} {col_type}")


def _parse_iso8601(value: str) -> datetime:
    val = value.strip()
    if not val:
        raise ValueError("empty timestamp")
    if val.endswith("Z"):
        val = val[:-1] + "+00:00"
    if "T" not in val:
        val = f"{val}T00:00:00+00:00"
    elif "+" not in val and "-" not in val.split("T")[1]:
        val = val + "+00:00"
    return datetime.fromisoformat(val)


def _format_iso8601(dt: datetime) -> str:
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _coerce_sort_value(value: Any):
    if value is None:
        return float('-inf')
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except Exception:
        return str(value)


def _sort_rows(
    rows: Sequence[Sequence[Any]],
    description: Sequence[Sequence[Any]],
    sort_spec: str | None,
) -> Sequence[Sequence[Any]]:
    if not rows or not sort_spec or not description:
        return rows
    col_name = sort_spec.strip()
    if not col_name:
        return rows
    descending = True
    if col_name[0] in {"+", "-"}:
        descending = col_name[0] != "+"
        col_name = col_name[1:]
    colnames = [d[0] for d in description]
    if col_name not in colnames:
        return rows
    idx = colnames.index(col_name)
    return sorted(
        rows,
        key=lambda r: _coerce_sort_value(r[idx]),
        reverse=descending,
    )


def _run_latest(
    conn,
    sort_spec: str | None,
    where_sql: str | None = None,
    title: str | None = None,
    gpu_names: Sequence[str] | None = None,
    gpu_counts: Sequence[str] | None = None,
) -> None:
    cur = conn.cursor()
    sql = LATEST_SNAPSHOT_QUERY
    extra_filter_sql, name_tokens, count_tokens = _build_gpu_filters(gpu_names, gpu_counts)
    filters: list[str] = []
    params: list[Any] = []
    if where_sql:
        filters.append(where_sql)
    if extra_filter_sql:
        filters.append(extra_filter_sql)
        params.extend(name_tokens)
        params.extend(count_tokens)
    where_clause = f"WHERE {' AND '.join(filters)}\n" if filters else ""
    query = sql.format(where_clause=where_clause)
    try:
        rows = cur.execute(query, params).fetchall()
        rows = _sort_rows(rows, cur.description, sort_spec)
    except Exception:
        fallback_query = LATEST_SNAPSHOT_QUERY_SQLITE_FALLBACK.format(where_clause=where_clause)
        rows = cur.execute(fallback_query, params).fetchall()
        rows = _sort_rows(rows, cur.description, sort_spec)
    _maybe_print_warning()
    if title:
        print(title)
    _print_tsv(cur, rows)


def _run_occupancy(
    conn,
    since: str | None,
    until: str | None,
    min_samples: int,
    min_total_minutes: float,
    limit: int | None,
) -> None:
    cur = conn.cursor()
    bounds = cur.execute("SELECT MIN(ts), MAX(ts) FROM offers_raw").fetchone()
    headers = [
        "offer_id",
        "machine_id",
        "gpu_name",
        "samples",
        "total_hours",
        "available_hours",
        "assumed_rented_hours",
        "api_rented_hours",
        "unknown_hours",
        "available_pct",
        "assumed_rented_pct",
        "api_rented_pct",
        "unknown_pct",
    ]
    if not bounds or bounds[0] is None:
        _print_rows(headers, [])
        return

    poll_env = os.environ.get("VW_POLL_INTERVAL_SEC")
    try:
        configured_interval = int(poll_env) if poll_env else None
    except ValueError:
        configured_interval = None
    poll_interval_sec = configured_interval if configured_interval and configured_interval > 0 else VAST_MIN_POLL_INTERVAL_SEC * 6
    poll_interval_sec = max(poll_interval_sec, VAST_MIN_POLL_INTERVAL_SEC * 6)
    min_ts_str, max_ts_str = bounds
    since_dt = _parse_iso8601(since) if since else _parse_iso8601(min_ts_str)
    until_dt = _parse_iso8601(until) if until else _parse_iso8601(max_ts_str) + timedelta(seconds=poll_interval_sec)
    if until_dt <= since_dt:
        print("since must be earlier than until", file=sys.stderr)
        sys.exit(2)

    since_iso = _format_iso8601(since_dt)
    until_iso = _format_iso8601(until_dt)

    sql = (
        "SELECT offer_id, machine_id, gpu_name, ts, rentable, rented, availability_state "
        "FROM offers_raw WHERE ts >= ? AND ts <= ? ORDER BY offer_id, ts"
    )
    rows = cur.execute(sql, (since_iso, until_iso)).fetchall()
    if not rows:
        _maybe_print_warning()
        _print_rows(headers, [])
        return

    def parse_ts(ts: str) -> datetime:
        return _parse_iso8601(ts)

    stats: Dict[int, Dict[str, Any]] = {}
    for idx, row in enumerate(rows):
        offer_id, machine_id, gpu_name, ts_str, rentable_flag, rented_flag, availability_state = row
        current_dt = parse_ts(ts_str)
        entry = stats.setdefault(
            offer_id,
            {
                "machine_id": machine_id,
                "gpu_name": gpu_name,
                "samples": 0,
                "total_sec": 0.0,
                "available_sec": 0.0,
                "assumed_rented_sec": 0.0,
                "api_rented_sec": 0.0,
                "unknown_sec": 0.0,
            },
        )
        entry["samples"] += 1

        if idx + 1 < len(rows) and rows[idx + 1][0] == offer_id:
            next_dt = parse_ts(rows[idx + 1][3])
        else:
            next_dt = current_dt + timedelta(seconds=poll_interval_sec)
        if next_dt > until_dt:
            next_dt = until_dt
        if current_dt < since_dt and next_dt > since_dt:
            current_dt = since_dt
        delta_sec = (next_dt - current_dt).total_seconds()
        if delta_sec <= 0:
            continue
        entry["total_sec"] += delta_sec
        rentable_bool = None if rentable_flag is None else bool(rentable_flag)
        rented_bool = None if rented_flag is None else bool(rented_flag)

        if rentable_bool is True:
            entry["available_sec"] += delta_sec
        elif rentable_bool is False:
            entry["assumed_rented_sec"] += delta_sec
        else:
            entry["unknown_sec"] += delta_sec

        if rented_bool:
            entry["api_rented_sec"] += delta_sec

    rows_out = []
    min_total_seconds = max(min_total_minutes * 60.0, 0.0)
    for offer_id, entry in stats.items():
        if entry["samples"] < max(min_samples, 1):
            continue
        if entry["total_sec"] < min_total_seconds:
            continue
        if entry["total_sec"] <= 0:
            continue
        total_hours = entry["total_sec"] / 3600.0
        available_hours = entry["available_sec"] / 3600.0
        assumed_rented_hours = entry["assumed_rented_sec"] / 3600.0
        api_rented_hours = entry["api_rented_sec"] / 3600.0
        unknown_hours = entry["unknown_sec"] / 3600.0
        available_pct = 100.0 * entry["available_sec"] / entry["total_sec"]
        assumed_rented_pct = 100.0 * entry["assumed_rented_sec"] / entry["total_sec"]
        api_rented_pct = 100.0 * entry["api_rented_sec"] / entry["total_sec"]
        unknown_pct = 100.0 * entry["unknown_sec"] / entry["total_sec"]
        rows_out.append(
            (
                offer_id,
                entry["machine_id"],
                entry["gpu_name"],
                entry["samples"],
                round(total_hours, 3),
                round(available_hours, 3),
                round(assumed_rented_hours, 3),
                round(api_rented_hours, 3),
                round(unknown_hours, 3),
                round(available_pct, 2),
                round(assumed_rented_pct, 2),
                round(api_rented_pct, 2),
                round(unknown_pct, 2),
            )
        )

    rows_out.sort(key=lambda r: (r[10], r[6]), reverse=True)
    if limit is not None:
        rows_out = rows_out[:limit]

    _maybe_print_warning()
    _print_rows(headers, rows_out)


def main() -> None:
    parser = argparse.ArgumentParser(description="VastWatch reporting")
    parser.add_argument(
        "--mode",
        choices=["latest", "occupancy", "both"],
        default="latest",
        help="Report to run (default: latest utilization snapshot)",
    )
    parser.add_argument("--since", help="Start of occupancy window (ISO8601 UTC)")
    parser.add_argument("--until", help="End of occupancy window (ISO8601 UTC)")
    parser.add_argument("--min-samples", type=int, default=2, help="Minimum snapshots per offer")
    parser.add_argument(
        "--min-total-minutes",
        type=float,
        default=0.0,
        help="Minimum sampled minutes per offer",
    )
    parser.add_argument("--limit", type=int, help="Trim occupancy output to N rows")
    parser.add_argument(
        "--sort",
        default="-api_rented_pct",
        help=(
            "Column to sort the latest snapshot by. Prefix with '+' for ascending, '-' for descending. "
            "Defaults to '-api_rented_pct'."
        ),
    )
    parser.add_argument(
        "--split-verified",
        action="store_true",
        help="When set, print separate tables for verified and non-verified providers",
    )
    parser.add_argument(
        "--gpu-name",
        dest="gpu_names",
        action="append",
        help="Include only offers whose GPU name contains any of these substrings (repeat or comma separated)",
    )
    parser.add_argument(
        "--gpu-count",
        dest="gpu_counts",
        action="append",
        help="Include only offers with these GPU counts per machine (repeat or comma separated)",
    )
    args = parser.parse_args()

    db_path = os.environ.get("VW_DB", "vastwatch.db")
    conn, backend = _connect(db_path)
    try:
        schema_path = Path(__file__).with_name("schema.sql")
        sql_schema = schema_path.read_text(encoding="utf-8")
        conn.executescript(sql_schema) if backend == "sqlite" else conn.execute(sql_schema)
        _ensure_columns(conn, backend)
    except Exception:
        pass

    gpu_name_filters = _normalize_filter_values(getattr(args, "gpu_names", None))
    gpu_count_filters = _normalize_filter_values(getattr(args, "gpu_counts", None))

    def run_latest_tables():
        if args.split_verified:
            _run_latest(
                conn,
                args.sort,
                where_sql="COALESCE(verified,0)=1",
                title="Verified providers",
                gpu_names=gpu_name_filters or None,
                gpu_counts=gpu_count_filters or None,
            )
            print()
            _run_latest(
                conn,
                args.sort,
                where_sql="COALESCE(verified,0)=0",
                title="Unverified+deverified providers",
                gpu_names=gpu_name_filters or None,
                gpu_counts=gpu_count_filters or None,
            )
        else:
            _run_latest(
                conn,
                args.sort,
                gpu_names=gpu_name_filters or None,
                gpu_counts=gpu_count_filters or None,
            )

    if args.mode == "latest":
        run_latest_tables()
        return
    run_latest_after = False
    if args.mode in {"occupancy", "both"}:
        _run_occupancy(
            conn,
            since=args.since,
            until=args.until,
            min_samples=args.min_samples,
            min_total_minutes=args.min_total_minutes,
            limit=args.limit,
        )
        run_latest_after = args.mode == "both"
    if args.mode == "both":
        print()
        run_latest_tables()
    elif run_latest_after:
        print()
        run_latest_tables()


if __name__ == "__main__":
    main()
