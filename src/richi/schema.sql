/* Core schema, unchanged since v1. Init v2 also applies graph_schema.sql.
   FTS is installed separately when supported. */
CREATE TABLE metadata (
    key TEXT PRIMARY KEY
    , value TEXT NOT NULL
)
;
CREATE TABLE projects (
    id TEXT PRIMARY KEY
    , name TEXT NOT NULL
    , repo_path TEXT
    , description TEXT NOT NULL DEFAULT ''
    , source TEXT
    , verified_at TEXT
)
;
CREATE TABLE entries (
    id TEXT PRIMARY KEY
    , kind TEXT NOT NULL CHECK (kind IN ('task', 'decision', 'fact', 'note', 'investigation', 'experiment', 'procedure'))
    , title TEXT NOT NULL
    , summary TEXT NOT NULL
    , jira_key TEXT
    , work_state TEXT CHECK (work_state IN ('planned', 'in_progress', 'implemented', 'merged', 'released', 'blocked', 'done'))
    , knowledge_state TEXT NOT NULL CHECK (knowledge_state IN ('confirmed', 'hypothesis', 'superseded'))
    , sources TEXT NOT NULL DEFAULT '[]'
    , tags TEXT NOT NULL DEFAULT '[]'
    , aliases TEXT NOT NULL DEFAULT '[]'
    , created_at TEXT NOT NULL
    , updated_at TEXT NOT NULL
    , verified_at TEXT
)
;
CREATE TABLE entry_projects (
    entry_id TEXT NOT NULL REFERENCES entries(id)
    , project_id TEXT NOT NULL REFERENCES projects(id)
    , PRIMARY KEY (entry_id, project_id)
)
;
CREATE INDEX entry_projects_project ON entry_projects(project_id, entry_id)
;
CREATE INDEX entries_updated ON entries(updated_at DESC)
;
CREATE INDEX entries_jira ON entries(jira_key)
;
CREATE TABLE relations (
    id TEXT PRIMARY KEY
    , from_project TEXT NOT NULL REFERENCES projects(id)
    , to_project TEXT NOT NULL REFERENCES projects(id)
    , kind TEXT NOT NULL
    , description TEXT NOT NULL DEFAULT ''
    , source TEXT
    , knowledge_state TEXT NOT NULL CHECK (knowledge_state IN ('confirmed', 'hypothesis', 'superseded'))
    , verified_at TEXT
    , CHECK (from_project <> to_project)
    , CHECK (knowledge_state <> 'confirmed' OR length(trim(source)) > 0 AND source IS NOT NULL)
)
;
CREATE INDEX relations_from ON relations(from_project)
;
CREATE INDEX relations_to ON relations(to_project)
;
CREATE TABLE entry_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT
    , entry_id TEXT NOT NULL REFERENCES entries(id)
    , changed_at TEXT NOT NULL
    , before_json TEXT
    , after_json TEXT NOT NULL
    , event_id TEXT UNIQUE
)
;
CREATE INDEX entry_history_entry ON entry_history(entry_id, id)
;
CREATE TRIGGER entry_history_immutable_update
BEFORE UPDATE ON entry_history
BEGIN
    SELECT RAISE(ABORT, 'entry_history is append-only');
END
;
CREATE TRIGGER entry_history_immutable_delete
BEFORE DELETE ON entry_history
BEGIN
    SELECT RAISE(ABORT, 'entry_history is append-only');
END
;
CREATE TABLE entry_events (
    event_id TEXT PRIMARY KEY
    , entry_id TEXT NOT NULL REFERENCES entries(id)
    , payload_hash TEXT NOT NULL
)
;
