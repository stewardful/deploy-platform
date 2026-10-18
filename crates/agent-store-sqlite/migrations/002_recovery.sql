ALTER TABLE applications ADD COLUMN desired TEXT NOT NULL DEFAULT 'running';
ALTER TABLE applications ADD COLUMN recovery TEXT NOT NULL DEFAULT '{"attempts":0,"next_attempt_ms":0,"running_since_ms":0,"error_code":null}';
PRAGMA user_version = 2;
