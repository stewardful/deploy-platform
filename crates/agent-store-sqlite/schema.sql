CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE applications (
  name TEXT PRIMARY KEY,
  spec TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  phase TEXT NOT NULL,
  instance_id TEXT,
  desired TEXT NOT NULL DEFAULT 'running',
  recovery TEXT NOT NULL DEFAULT '{"attempts":0,"next_attempt_ms":0,"running_since_ms":0,"error_code":null}',
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE operations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL REFERENCES applications(name),
  phase TEXT NOT NULL,
  error_code TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE api_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL UNIQUE,
  command TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed')),
  result TEXT,
  error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX api_jobs_pending ON api_jobs(state,id);
PRAGMA user_version = 3;
