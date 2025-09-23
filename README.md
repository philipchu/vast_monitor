# VastWatch — Vast.ai Utilization Tracker

Measure whether Vast.ai devices are actually being used, and turn that into utilization + demand signals by GPU family, VRAM bucket, region, and rental type. This README seeds the repo and tells Codex (or you) exactly what to build.

---

## TL;DR

- A tiny Python service polls Vast.ai’s **Search Offers** API on a schedule.
- It stores snapshots of **available** vs **in-use** (rented) offers.
- It computes rolling utilization and price metrics by GPU, region, etc.
- Output lives in a single SQLite (or DuckDB) file and is easy to query / dashboard.

---

## Why

You want to know:
1) Are devices on Vast actually rented?
2) Which GPUs/regions/price bands show high utilization (market appeal)?
3) How quickly do offers get rented after appearing (time-to-rent)?

---

## Architecture

```
vastwatch/
  ├─ README.md              # this file
  ├─ vastwatch/
  │   ├─ __init__.py
  │   ├─ client.py          # API calls to Vast (search offers)
  │   ├─ collector.py       # scheduler/loop that writes snapshots to DB
  │   ├─ schema.sql         # tables + indexes for SQLite/DuckDB
  │   ├─ report.py          # quick text reports (utilization, prices)
  │   └─ queries/           # saved SQL for dashboards/analysis
  ├─ .env.example           # VAST_API_KEY, poll interval, DB path
  ├─ requirements.txt
  ├─ Dockerfile             # optional container build
  └─ docker-compose.yml     # optional runtime
```

**Flow**

1. **Collector** calls the Vast **search offers** endpoint three ways each poll:
   - `rented=false`, `rentable=true`   → currently **available** offers
   - `rented=true`,  `rentable=false`  → offers Vast marks as **rented/in-use**
   - `rented=false`, `rentable=false`  → **unrentable** offers (offline, paused, or just leased)  
   We record Vast’s raw `rentable`/`rented` flags as-is. Reports later assume `rentable=false` means “utilized,” so be aware utilization may be overstated when hosts go offline.
2. It **normalizes** fields (GPU name, VRAM, price, location, rental type, rented/rentable flags, etc.).
3. It **appends** those rows to an **append-only** `offers_raw` table with an ISO timestamp.
4. **Report** queries (and optional dashboards) compute:
   - Utilization (`rented / (rented + available)`) by GPU/region/type
   - Price bands (median/avg/percentiles) for **rented vs available**
   - Time-to-rent (TTR) by tracking state flips per `offer_id`

> Notes  
> • Keep the API’s default “bundling” behavior on (don’t ask for every identical ask). It reduces duplicate rows and noise.  
> • For whole-GPU demand, filter `gpu_frac = 1`. Track interruptible vs on-demand separately.

---

## Data Model

**Table: `offers_raw` (append-only)**

| Column              | Type       | Description                                   |
|---------------------|------------|-----------------------------------------------|
| `ts`                | TEXT       | ISO timestamp of the poll                     |
| `offer_id`          | INTEGER    | Stable ID for the offer                       |
| `machine_id`        | INTEGER    | Machine ID attached to the offer              |
| `gpu_name`          | TEXT       | e.g., `RTX 4090`, `A100`, `H100`              |
| `num_gpus`          | INTEGER    | Number of GPUs in the offer                   |
| `gpu_frac`          | REAL       | Fractional GPU share (1.0 = whole GPU)        |
| `gpu_total_ram_gb`  | REAL       | VRAM per GPU or total, as provided            |
| `dph_total_usd`     | REAL       | Dollars per hour total (all resources)        |
| `reliability2`      | REAL       | Provider reliability score (if provided)      |
| `geolocation`       | TEXT       | Region/country string                         |
| `type`              | TEXT       | `on-demand` or `interruptible` (if provided)  |
| `rentable`          | INTEGER    | 1/0                                           |
| `rented`            | INTEGER    | 1/0                                           |
| `verified`          | INTEGER    | 1 if offer is verified                        |
| `deverified`        | INTEGER    | 1 if offer was previously verified then revoked |
| `availability_state`| TEXT       | `available`, `rented`, `unavailable`, or `unknown` |

**Indexes**
- `idx_ts (ts)`
- `idx_gpu (gpu_name, geolocation, type)`

---

## Example Metrics

- **Offer-count utilization**  
  `util = rented_offers / (rented_offers + available_offers)`

- **GPU-weighted utilization (optional)**  
  Weight each row by `num_gpus * gpu_frac` for heavier instances.

- **Price views**  
  Compare `dph_total_usd` distributions for rented vs available per GPU.

- **Time-to-rent (TTR)**  
  Measure time between first seen **available** → first seen **rented** per `offer_id`.

---

## Prereqs

- Python 3.10+
- SQLite (built-in) or DuckDB (optional)
- A Vast API key

---

## Setup

```bash
git clone <your-repo> vastwatch && cd vastwatch

# (Optional) Python virtualenv
python -m venv .venv
source .venv/bin/activate

# Install deps
cat > requirements.txt <<'REQ'
requests
duckdb>=1.0.0   # optional, if you prefer DuckDB alongside SQLite
sqlite-utils    # optional helper
python-dotenv
REQ
pip install -r requirements.txt

# Seed env vars
cp .env.example .env
# then edit .env to set VAST_API_KEY, etc.
```

**`.env.example`**
```env
# Required
VAST_API_KEY=YOUR_VAST_API_KEY

# Optional
VW_DB=vastwatch.db
VW_POLL_INTERVAL_SEC=360
VW_TIMEOUT_SEC=60
VW_INCLUDE_UNVERIFIED=1
# Override only if Vast changes their API hostname/path
VAST_BASE_URL=https://cloud.vast.ai/api/v0
```

---

## Implementation Notes (what Codex should build)

### `vastwatch/client.py`
- Expose `search_offers(rented: bool, rentable: bool | None = True, extra_filters: dict | None) -> list[dict]`
- Call the Vast bundles search endpoint (`POST https://cloud.vast.ai/api/v0/bundles/`) with a body containing the query operators, for example:
  ```json
  {
    "rentable": {"eq": true},
    "rented":   {"eq": false},
    "verified": {"eq": true},
    "external": {"eq": false},
    "limit": 10000,
    "type": "on-demand"
  }
  ```
- Auth via `Authorization: Bearer ${VAST_API_KEY}`.
- Optional env `VW_INCLUDE_UNVERIFIED=0` to limit to verified providers; default collects everything.
- `normalize(offer, ts)` → map to the schema above.
- Handle HTTP 429/5xx with backoff (raise a typed error the collector can catch).

- Read `.env` (dotenv) for config.
- Initialize DB with `schema.sql` (create table + indexes if not exists).
- **Loop**:
  1. Call `search_offers(False, rentable=True)`  (available)
  2. Call `search_offers(True, rentable=False)` (API-reported rented)
  3. Call `search_offers(False, rentable=False)` (unrentable / potentially rented)
  4. Normalize with a state hint, store raw flags, and `INSERT` all rows with the same `ts`
  5. Sleep `VW_POLL_INTERVAL_SEC` (default 360)
- On errors: exponential backoff with jitter, log and continue.
  - Default poll/backoff interval is the Vast minimum (60s) plus 500% headroom (360s).

### `vastwatch/schema.sql`
```sql
CREATE TABLE IF NOT EXISTS offers_raw (
  ts TEXT NOT NULL,
  offer_id INTEGER NOT NULL,
  machine_id INTEGER NOT NULL,
  gpu_name TEXT,
  num_gpus INTEGER,
  gpu_frac REAL,
  gpu_total_ram_gb REAL,
  dph_total_usd REAL,
  reliability2 REAL,
  geolocation TEXT,
  type TEXT,
  rentable INTEGER,
  rented INTEGER,
  verified INTEGER,
  deverified INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ts  ON offers_raw(ts);
CREATE INDEX IF NOT EXISTS idx_gpu ON offers_raw(gpu_name, geolocation, type);
```

### `vastwatch/report.py`
- Connect to `VW_DB` and run canned queries.
- Default mode runs the occupancy report (and also prints the latest snapshot).
- `--mode latest` and `--mode both` let you focus the output; occupancy respects the same window and uses the poll cadence (default = 360s).

Default latest snapshot groups the newest poll by `gpu_name` + GPU-count bucket, counts how many offers are `rentable=true` (available) versus `rentable=false` (assumed utilized), and reports utilization + average prices for each group. The assumed utilization ignores Vast’s flaky `rented` flag; raw `rented` counts are still shown for reference. A warning reflecting this assumption is printed with every report.

- Print as a TSV to stdout.

---

## Running It

```bash
# 1) Start the collector (writes snapshots into VW_DB)
python -m vastwatch.collector

# 2) After a few polls, print occupancy (default) + latest snapshot
python -m vastwatch.report

# 3) Occupancy for a specific window (e.g., last 7 days only)
python -m vastwatch.report --since $(date -u -d '7 days ago' +%FT%TZ)

# Snapshot only
python -m vastwatch.report --mode latest
```

---

## Useful SQL “Recipes”

**Daily aggregates**
```sql
INSERT INTO agg_gpu_daily
SELECT
  DATE(ts)                            AS date,
  gpu_name,
  geolocation,
  COALESCE(type,'any')               AS type,
  COUNT(*) FILTER (WHERE rented=1)   AS rented_offers,
  COUNT(*) FILTER (WHERE rented=0)   AS available_offers,
  ROUND(100.0 * CAST(COUNT(*) FILTER (WHERE rented=1) AS FLOAT) /
       NULLIF(COUNT(*),0), 2)        AS utilization_pct,
  MEDIAN(CASE WHEN rented=1 THEN dph_total_usd END) AS median_price_rented,
  MEDIAN(CASE WHEN rented=0 THEN dph_total_usd END) AS median_price_avail,
  QUANTILE_CONT(dph_total_usd,0.10)  FILTER (WHERE rented=0) AS p10_price_avail,
  QUANTILE_CONT(dph_total_usd,0.90)  FILTER (WHERE rented=0) AS p90_price_avail
FROM offers_raw
GROUP BY 1,2,3,4;
```

**Time-to-Rent (TTR) (naïve first pass)**  
“First seen available” → “first seen rented” per `offer_id` (within a 48h window):
```sql
WITH seen AS (
  SELECT offer_id, ts, rented
  FROM offers_raw
),
first_avail AS (
  SELECT offer_id, MIN(ts) AS ts_avail
  FROM seen
  WHERE rented=0
  GROUP BY offer_id
),
first_rented AS (
  SELECT offer_id, MIN(ts) AS ts_rented
  FROM seen
  WHERE rented=1
  GROUP BY offer_id
)
SELECT
  a.offer_id,
  a.ts_avail,
  r.ts_rented,
  (JULIANDAY(r.ts_rented) - JULIANDAY(a.ts_avail)) * 24.0 AS hours_to_rent
FROM first_avail a
JOIN first_rented r USING (offer_id)
WHERE r.ts_rented > a.ts_avail
  AND (JULIANDAY(r.ts_rented) - JULIANDAY(a.ts_avail)) * 24.0 <= 48
ORDER BY hours_to_rent;
```

**Whole-GPU cut (gpu_frac = 1)**
```sql
SELECT gpu_name,
       ROUND(100.0 * SUM(CASE WHEN rented=1 THEN 1 ELSE 0 END) / COUNT(*), 1) AS util_pct
FROM offers_raw
WHERE ts = (SELECT MAX(ts) FROM offers_raw)
  AND COALESCE(gpu_frac,1.0) >= 0.999
GROUP BY gpu_name
ORDER BY util_pct DESC;
```

**Device occupancy (time-weighted)**
```sql
-- See vastwatch/queries/device_occupancy.sql for bind-variable friendly version
WITH ordered AS (
  SELECT
    offer_id,
    machine_id,
    gpu_name,
    ts,
    rented,
    LEAD(ts) OVER (PARTITION BY offer_id ORDER BY ts) AS next_ts
  FROM offers_raw
), durations AS (
  SELECT
    offer_id,
    machine_id,
    gpu_name,
    rented,
    ts AS start_ts,
    COALESCE(next_ts, datetime(ts, '+360 seconds')) AS end_ts
  FROM ordered
), totals AS (
  SELECT
    offer_id,
    machine_id,
    gpu_name,
    SUM((julianday(replace(end_ts,'T',' ')) - julianday(replace(start_ts,'T',' '))) * 24.0) AS total_hours,
    SUM(CASE WHEN rented=1 THEN (julianday(replace(end_ts,'T',' ')) - julianday(replace(start_ts,'T',' '))) * 24.0 ELSE 0 END) AS rented_hours
  FROM durations
  GROUP BY 1,2,3
)
SELECT
  offer_id,
  machine_id,
  gpu_name,
  ROUND(100.0 * rented_hours / NULLIF(total_hours,0), 2) AS rented_pct,
  ROUND(rented_hours, 3) AS rented_hours,
  ROUND(total_hours, 3) AS total_hours
FROM totals
WHERE total_hours > 0
ORDER BY rented_pct DESC, rented_hours DESC;
```

---

## Configuration & Filters

- **Environment**
- `VAST_API_KEY` (required)
- `VW_DB` path (default `vastwatch.db`)
- `VW_POLL_INTERVAL_SEC` (default `360`)
- `VW_TIMEOUT_SEC` (default `60`)
- `VW_INCLUDE_UNVERIFIED` (defaults to `1`; set to `0` to limit to verified only)
- `VAST_BASE_URL` (defaults to `https://cloud.vast.ai/api/v0`; change only if Vast moves the bundles endpoint)

- **API Filters (examples)**
  - `gpu_name` equals one of (`RTX 4090`, `H100`, …)
  - `gpu_total_ram` (VRAM) min
  - `geolocation` region or country
  - `type` in (`on-demand`, `interruptible`)
  - `gpu_frac = 1` (whole GPU only)

> Start broad, then layer filters for specific market slices.

---

## Docker (optional)

**`Dockerfile`**
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY vastwatch ./vastwatch
ENV VW_DB=/data/vastwatch.db
VOLUME ["/data"]
CMD ["python", "-m", "vastwatch.collector"]
```

**`docker-compose.yml`**
```yaml
services:
  collector:
    build: .
    env_file: .env
    environment:
      - VW_DB=/data/vastwatch.db
    volumes:
      - ./data:/data
    restart: unless-stopped
```

Run:
```bash
docker compose up -d --build
```

---

## Systemd (optional)

Create `/etc/systemd/system/vastwatch.service`:
```ini
[Unit]
Description=VastWatch Collector
After=network-online.target

[Service]
EnvironmentFile=/etc/vastwatch.env
WorkingDirectory=/opt/vastwatch
ExecStart=/opt/vastwatch/.venv/bin/python -m vastwatch.collector
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vastwatch
```

---

## Caveats & Tips

- **Bundling**: Keep default bundling ON to avoid duplicate “identical” asks; it makes market-level stats cleaner.
- **Fractional GPUs**: Compare apples-to-apples by including a **whole-GPU** view.
- **Interruptible vs On-Demand**: Treat separately; demand profiles differ.
- **Rate limits**: Back off on 429/5xx with jitter; polling every 1–5 minutes is plenty.

---

## License

MIT (or your choice). Add `LICENSE` before publishing.
- Occupancy mode example:

```bash
python -m vastwatch.report --mode occupancy --since 2024-09-01 --min-samples 4 --limit 50
```
