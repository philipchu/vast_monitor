# vastwatch.db — Interface Contract for Consumers

This document describes how external systems (e.g. `vast_bidder`) are expected to query `vastwatch.db`. Read this before writing any queries against the database.

## Connection

```python
import sqlite3
conn = sqlite3.connect("../vast_monitor/vastwatch.db", check_same_thread=False)
conn.row_factory = sqlite3.Row
```

The database is SQLite. It is written by the `vast_monitor` collector and should be opened read-only by consumers where possible.

## Primary Table: `offers_raw`

Periodic snapshots of every GPU offer on Vast.ai. One row per offer per snapshot.

| Column | Type | Description |
|--------|------|-------------|
| `ts` | TEXT | ISO 8601 snapshot timestamp (e.g. `2026-02-25T14:00:00Z`) |
| `offer_id` | INTEGER | Offer listing ID |
| `machine_id` | INTEGER | Physical machine ID — stable across snapshots |
| `gpu_name` | TEXT | GPU model (e.g. `RTX 4090`, `RTX PRO 6000 WS`) |
| `num_gpus` | INTEGER | GPUs in this offer |
| `gpu_frac` | REAL | Fraction of GPU allocated |
| `gpu_total_ram_gb` | REAL | Total VRAM across all GPUs in the offer |
| `dph_total_usd` | REAL | Total $/hr for the whole offer |
| `reliability2` | REAL | Host reliability score (0–1) |
| `geolocation` | TEXT | e.g. `Texas, US` |
| `type` | TEXT | Offer type |
| `rentable` | INTEGER | Whether the offer is rentable |
| `rented` | INTEGER | **Only populated for RTX 4090. Do not use for other GPUs.** |
| `availability_state` | TEXT | `available`, `rented`, or `unavailable` |
| `verified` | INTEGER | 1 = host is verified |
| `deverified` | INTEGER | 1 = host was stripped of verified status |

### Critical rules

- **Use `availability_state` for demand signals, never `rented`.** The `rented` column is only populated for RTX 4090; for all other GPUs it is always 0.
- **Per-GPU price**: `dph_total_usd / num_gpus`
- **Occupancy**: `rented_count / (rented_count + available_count)` — exclude `unavailable` from the denominator.
- Snapshots are taken every few minutes. `COUNT(*)` is proportional to time, not unique events.

## Host-Tier Views

Three views are pre-defined for segmenting the market by host verification status. Use these instead of adding repeated `WHERE verified = ...` clauses.

| View | Definition | Approx. share |
|------|-----------|---------------|
| `offers_verified` | `verified = 1 AND deverified = 0` | ~52% |
| `offers_deverified` | `deverified = 1` | ~28% |
| `offers_unverified` | `verified = 0 AND deverified = 0` | ~20% |

All views have full historical coverage — `verified` and `deverified` are non-null on every row back to the first snapshot (Sep 2025).

### Intended usage pattern

Pass a `host_tier` parameter to your data provider and resolve it to a view name at construction time:

```python
_TABLE_MAP = {
    'all':        'offers_raw',
    'verified':   'offers_verified',
    'unverified': 'offers_unverified',
    'deverified': 'offers_deverified',
}

class MarketDataProvider:
    def __init__(self, db_path: str, host_tier: str = 'all'):
        self._table = _TABLE_MAP[host_tier]
        self._tier = host_tier
        ...

    def get_market_snapshot(self, gpu_name, num_gpus):
        query = f"""
            SELECT dph_total_usd / num_gpus as price_per_gpu, availability_state
            FROM {self._table}          -- ← view or offers_raw
            WHERE ts = ? AND gpu_name = ? AND num_gpus = ?
        """
```

### Cache key contamination

If you cache query results in-process, include the tier in every cache key. Two providers with different tiers sharing a cache will otherwise return each other's data:

```python
# Wrong
cache_key = f"snapshot:{gpu_name}:{num_gpus}"

# Correct
cache_key = f"snapshot:{self._tier}:{gpu_name}:{num_gpus}"
```

### Transition queries (clearing price detection)

When detecting `available → rented` transitions across consecutive snapshots, the tier filter belongs on the **JOIN step**, not the timestamp discovery step. Snapshot timestamps are shared across all host tiers, so filtering by tier in the CTE that collects distinct `ts` values is unnecessary and will not reduce the number of pairs to iterate.

```python
# Step 1: discover timestamps (no tier filter needed here)
pairs_query = """
    WITH snapshots AS (
        SELECT DISTINCT ts FROM offers_raw
        WHERE gpu_name = ? AND num_gpus = ? AND ts >= ?
        ORDER BY ts
    )
    SELECT ts, LAG(ts) OVER (ORDER BY ts) as prev_ts FROM snapshots
"""

# Step 2: detect transitions (tier filter goes here, on both sides of the join)
transition_query = f"""
    SELECT prev.dph_total_usd / prev.num_gpus
    FROM {self._table} curr
    JOIN {self._table} prev
        ON prev.ts = ? AND prev.machine_id = curr.machine_id
        AND prev.gpu_name = ? AND prev.num_gpus = ?
    WHERE curr.ts = ? AND curr.gpu_name = ? AND curr.num_gpus = ?
      AND curr.availability_state = 'rented'
      AND prev.availability_state = 'available'
"""
```

## Indexes

```
idx_ts                 (ts)
idx_gpu                (gpu_name, geolocation, type)
idx_gpu_ts             (gpu_name, ts)
idx_gpu_numgpus_ts     (gpu_name, num_gpus, ts)
idx_machine_gpu_ts     (machine_id, gpu_name, num_gpus, ts)
idx_clearing_query     (gpu_name, num_gpus, ts, machine_id, availability_state)
idx_transitions        (ts, machine_id, gpu_name, num_gpus, availability_state, dph_total_usd)
```

`verified` and `deverified` are not indexed. They post-filter the results of an already-indexed scan on `gpu_name + num_gpus + ts`, which is efficient enough — the leading columns reduce the scanned set to a narrow time/GPU window, and the ~50/50 verified split then halves that cheaply.

## Schema Validation

When connecting, verify the table exists before running queries:

```python
cursor = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='offers_raw'"
)
assert cursor.fetchone() is not None, "vastwatch.db missing offers_raw table"
```

The views (`offers_verified`, etc.) are created by `vastwatch/schema.sql` and will exist in any properly initialized database. You do not need to validate them separately.

## Performance Notes

- **`get_market_snapshot`** (single timestamp): fast — uses `idx_clearing_query`.
- **`get_utilization_history` / `get_hourly_utilization`** (time-range aggregations): moderate — uses `idx_gpu_ts`. At 1-week lookback these can take several seconds on a large database; cache results for at least 5–60 minutes depending on freshness requirements.
- **Transition queries**: the iterative Python approach (loop over timestamp pairs, one indexed query per pair) is orders of magnitude faster than equivalent CTE self-joins on SQLite. A 168-hour lookback takes ~75ms with the iterative approach vs. >30s with a CTE join. Do not rewrite transition detection as a single CTE.
