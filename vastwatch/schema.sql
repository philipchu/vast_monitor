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

-- Host-tier views for verified/deverified/unverified market segmentation.
-- "Verified" = verified=1 AND deverified=0 (cleanly verified host).
-- "Deverified" = deverified=1 (previously verified but removed, or never finished).
-- "Unverified" = verified=0 AND deverified=0 (never verified).
-- All historical rows have non-null values for both columns, so these views have full coverage.
CREATE VIEW IF NOT EXISTS offers_verified AS
    SELECT * FROM offers_raw WHERE verified = 1 AND deverified = 0;

CREATE VIEW IF NOT EXISTS offers_deverified AS
    SELECT * FROM offers_raw WHERE deverified = 1;

CREATE VIEW IF NOT EXISTS offers_unverified AS
    SELECT * FROM offers_raw WHERE verified = 0 AND deverified = 0;
