# Vast Monitor

GPU rental market monitoring tool for Vast.ai. Collects periodic snapshots of all GPU offers and stores them in SQLite for market analysis.

## Database: vastwatch.db

Single table `offers_raw` with periodic snapshots of every listing on Vast.ai.

### Schema

| Column | Type | Description |
|--------|------|-------------|
| ts | TEXT | ISO 8601 timestamp of the snapshot |
| offer_id | INTEGER | Unique offer listing ID |
| machine_id | INTEGER | Physical machine ID (stable across snapshots) |
| gpu_name | TEXT | GPU model name (e.g. `RTX PRO 6000 WS`, `RTX 4090`) |
| num_gpus | INTEGER | Number of GPUs in this offer (1, 2, 4, 8, etc.) |
| gpu_frac | REAL | Fraction of GPU allocated |
| gpu_total_ram_gb | REAL | Total VRAM across all GPUs in the offer |
| dph_total_usd | REAL | Total price in $/hour for the whole offer |
| reliability2 | REAL | Host reliability score (0-1) |
| geolocation | TEXT | Location string (e.g. `Texas, US`, `Spain, ES`) |
| type | TEXT | Offer type |
| rentable | INTEGER | Whether the offer is rentable (not reliably populated for all GPUs) |
| rented | INTEGER | Whether currently rented (only populated for RTX 4090; do NOT use for other GPUs) |
| verified | INTEGER | Whether the host is verified |
| deverified | INTEGER | Whether the host was deverified |
| availability_state | TEXT | `available`, `rented`, or `unavailable` — **this is the primary demand signal** |

### Critical Notes

- **Use `availability_state` for occupancy, NOT `rented`**. The `rented` column is only populated for RTX 4090. For all other GPUs it's always 0.
- **Per-GPU pricing**: `dph_total_usd / num_gpus` gives the per-GPU hourly rate.
- **Per-GPU VRAM**: `gpu_total_ram_gb / num_gpus` gives VRAM per GPU.
- **Occupancy formula**: `rented_obs / (rented_obs + available_obs)` — exclude `unavailable` from the denominator.
- Data goes back to ~Sep 2025 for some GPUs, Nov 2025 for others.
- Snapshots are taken periodically (roughly every few minutes), so `COUNT(*)` is proportional to time, not unique events.

### Useful Indexes

```
idx_ts (ts)
idx_gpu (gpu_name, geolocation, type)
idx_gpu_ts (gpu_name, ts)
idx_gpu_numgpus_ts (gpu_name, num_gpus, ts)
idx_machine_gpu_ts (machine_id, gpu_name, num_gpus, ts)
idx_clearing_query (gpu_name, num_gpus, ts, machine_id, availability_state)
idx_transitions (ts, machine_id, gpu_name, num_gpus, availability_state, dph_total_usd)
```

## See Also

- `memory/queries.md` — 10-step SQL query playbook for market research (discovery, occupancy, pricing, ROI, growth, geo, competitive)
- `memory/research-2026-02.md` — Feb 2026 research findings: RTX Pro line, full ROI rankings across all GPUs, demand growth signals
