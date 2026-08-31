-- lecture-ai SQLite schema v1
-- 定位：全局索引 + 去重表。真相源是每个 session 的 metadata.json。
-- 因此这里刻意不存转录全文（大文本存文件），只存路径与状态。

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
  key        TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  teacher    TEXT,
  semester   TEXT,
  glossary   TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id          TEXT PRIMARY KEY,
  course_key  TEXT REFERENCES courses(key),
  date        TEXT NOT NULL,
  start_time  TEXT,
  end_time    TEXT,
  state       TEXT NOT NULL,
  failed_from TEXT,
  error       TEXT,
  dir         TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_state ON sessions(state);
CREATE INDEX IF NOT EXISTS idx_sessions_date  ON sessions(date);

-- sha256 作主键：内容相同的文件（哪怕改了名）也只会被处理一次
CREATE TABLE IF NOT EXISTS files (
  sha256     TEXT PRIMARY KEY,
  path       TEXT NOT NULL,
  orig_name  TEXT,
  type       TEXT NOT NULL,
  size       INTEGER NOT NULL,
  timestamp  TEXT,
  session_id TEXT REFERENCES sessions(id),
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_session ON files(session_id);

-- UNIQUE(session_id, step) + upsert => 天然幂等
CREATE TABLE IF NOT EXISTS processing (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL REFERENCES sessions(id),
  step        TEXT NOT NULL,
  status      TEXT NOT NULL,
  provider    TEXT,
  model       TEXT,
  started_at  TEXT,
  finished_at TEXT,
  elapsed_sec REAL,
  error       TEXT,
  UNIQUE(session_id, step)
);

CREATE INDEX IF NOT EXISTS idx_processing_session ON processing(session_id);
