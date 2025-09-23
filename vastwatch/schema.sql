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
  deverified INTEGER,
  availability_state TEXT
);
CREATE INDEX IF NOT EXISTS idx_ts  ON offers_raw(ts);
CREATE INDEX IF NOT EXISTS idx_gpu ON offers_raw(gpu_name, geolocation, type);
