-- Scenario runs, step state and unified scenario journal (schema version 4).
CREATE TABLE scenario_runs (
    id TEXT PRIMARY KEY
    , scenario_id TEXT NOT NULL
    , scope TEXT NOT NULL CHECK (scope IN ('global', 'project', 'builtin'))
    , project_id TEXT
    , scenario_version INTEGER NOT NULL
    , scenario_sha256 TEXT NOT NULL
    , definition_json TEXT NOT NULL
    , inputs_json TEXT NOT NULL
    , status TEXT NOT NULL CHECK (status IN ('running', 'waiting', 'paused', 'done', 'failed', 'cancelled'))
    , task_entry_id TEXT
    , created_by_chat TEXT
    , created_by_agent TEXT
    , created_at TEXT NOT NULL
    , updated_at TEXT NOT NULL
    , revision INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE scenario_steps (
    run_id TEXT NOT NULL REFERENCES scenario_runs(id) ON DELETE CASCADE
    , step_key TEXT NOT NULL
    , step_id TEXT NOT NULL
    , position INTEGER NOT NULL
    , matrix_index INTEGER
    , matrix_json TEXT
    , type TEXT NOT NULL
    , status TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'claimed', 'waiting_human', 'done', 'failed', 'skipped', 'blocked'))
    , executor_role TEXT NOT NULL
    , executor_agent TEXT
    , claimed_by_chat TEXT
    , claimed_by_agent TEXT
    , lease_until TEXT
    , attempts INTEGER NOT NULL DEFAULT 0
    , rejections INTEGER NOT NULL DEFAULT 0
    , stop_continuations INTEGER NOT NULL DEFAULT 0
    , result_json TEXT
    , reason TEXT
    , started_at TEXT
    , finished_at TEXT
    , PRIMARY KEY (run_id, step_key)
);

CREATE TABLE scenario_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT
    , at TEXT NOT NULL
    , scenario_id TEXT
    , run_id TEXT
    , step_key TEXT
    , action TEXT NOT NULL
    , chat TEXT
    , agent TEXT
    , detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_scenario_runs_status_project ON scenario_runs(status, project_id);
CREATE INDEX idx_scenario_runs_chat ON scenario_runs(created_by_chat);
CREATE INDEX idx_scenario_runs_scenario ON scenario_runs(scenario_id, project_id, status, created_at);
CREATE INDEX idx_scenario_steps_run_status ON scenario_steps(run_id, status);
CREATE INDEX idx_scenario_steps_claimed_chat ON scenario_steps(claimed_by_chat, status) WHERE claimed_by_chat IS NOT NULL;
CREATE INDEX idx_scenario_steps_executor_agent ON scenario_steps(status, executor_agent) WHERE executor_agent IS NOT NULL;
CREATE INDEX idx_scenario_events_at ON scenario_events(at DESC, id DESC);
CREATE INDEX idx_scenario_events_run ON scenario_events(run_id, id DESC);
CREATE INDEX idx_scenario_events_scenario ON scenario_events(scenario_id, id DESC);
