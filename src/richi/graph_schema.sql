/* Additive schema 2: also used inside the explicit v1 -> v2 migration. */
CREATE TABLE entities (
    id TEXT PRIMARY KEY
    , kind TEXT NOT NULL CHECK (kind IN ('pull_request', 'metric', 'component', 'concept', 'document', 'jira'))
    , title TEXT NOT NULL
    , summary TEXT NOT NULL
    , sources TEXT NOT NULL DEFAULT '[]'
    , knowledge_state TEXT NOT NULL CHECK (knowledge_state IN ('confirmed', 'hypothesis', 'superseded'))
    , verified_at TEXT
    , tags TEXT NOT NULL DEFAULT '[]'
    , aliases TEXT NOT NULL DEFAULT '[]'
    , created_at TEXT NOT NULL
    , updated_at TEXT NOT NULL
)
;
CREATE TABLE graph_nodes (
    ref TEXT PRIMARY KEY NOT NULL
    , project_id TEXT UNIQUE REFERENCES projects(id)
    , entry_id TEXT UNIQUE REFERENCES entries(id)
    , entity_id TEXT UNIQUE REFERENCES entities(id)
    , CHECK ((project_id IS NOT NULL) + (entry_id IS NOT NULL) + (entity_id IS NOT NULL) = 1)
    , CHECK (ref = CASE
        WHEN project_id IS NOT NULL THEN 'project:' || project_id
        WHEN entry_id IS NOT NULL THEN 'entry:' || entry_id
        ELSE 'entity:' || entity_id END)
)
;
INSERT INTO graph_nodes(ref, project_id)
SELECT
    'project:' || id
    , id
FROM projects
;
INSERT INTO graph_nodes(ref, entry_id)
SELECT
    'entry:' || id
    , id
FROM entries
;
CREATE TRIGGER graph_project_insert AFTER INSERT ON projects
BEGIN
    INSERT INTO graph_nodes(ref, project_id) VALUES ('project:' || new.id, new.id);
END
;
CREATE TRIGGER graph_entry_insert AFTER INSERT ON entries
BEGIN
    INSERT INTO graph_nodes(ref, entry_id) VALUES ('entry:' || new.id, new.id);
END
;
CREATE TRIGGER graph_entity_insert AFTER INSERT ON entities
BEGIN
    INSERT INTO graph_nodes(ref, entity_id) VALUES ('entity:' || new.id, new.id);
END
;
CREATE TABLE graph_edges (
    id TEXT PRIMARY KEY
    , from_ref TEXT NOT NULL REFERENCES graph_nodes(ref)
    , to_ref TEXT NOT NULL REFERENCES graph_nodes(ref)
    , kind TEXT NOT NULL
    , description TEXT NOT NULL DEFAULT ''
    , sources TEXT NOT NULL DEFAULT '[]'
    , knowledge_state TEXT NOT NULL CHECK (knowledge_state IN ('confirmed', 'hypothesis', 'superseded'))
    , verified_at TEXT
    , created_at TEXT NOT NULL
    , updated_at TEXT NOT NULL
)
;
CREATE INDEX graph_edges_from ON graph_edges(from_ref)
;
CREATE INDEX graph_edges_to ON graph_edges(to_ref)
;
CREATE TABLE entity_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT
    , object_id TEXT NOT NULL REFERENCES entities(id)
    , changed_at TEXT NOT NULL
    , before_json TEXT
    , after_json TEXT NOT NULL
)
;
CREATE INDEX entity_history_object ON entity_history(object_id, id)
;
CREATE TABLE edge_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT
    , object_id TEXT NOT NULL REFERENCES graph_edges(id)
    , changed_at TEXT NOT NULL
    , before_json TEXT
    , after_json TEXT NOT NULL
)
;
CREATE INDEX edge_history_object ON edge_history(object_id, id)
;
CREATE TRIGGER entity_history_immutable_update BEFORE UPDATE ON entity_history
BEGIN
    SELECT RAISE(ABORT, 'entity_history is append-only');
END
;
CREATE TRIGGER entity_history_immutable_delete BEFORE DELETE ON entity_history
BEGIN
    SELECT RAISE(ABORT, 'entity_history is append-only');
END
;
CREATE TRIGGER edge_history_immutable_update BEFORE UPDATE ON edge_history
BEGIN
    SELECT RAISE(ABORT, 'edge_history is append-only');
END
;
CREATE TRIGGER edge_history_immutable_delete BEFORE DELETE ON edge_history
BEGIN
    SELECT RAISE(ABORT, 'edge_history is append-only');
END
;
CREATE VIEW graph_links AS
SELECT
    'edge:' || id AS id
    , id AS native_id
    , from_ref
    , to_ref
    , kind
    , knowledge_state
    , 'explicit' AS origin
FROM graph_edges
UNION ALL
SELECT
    'relation:' || id AS id
    , id AS native_id
    , 'project:' || from_project AS from_ref
    , 'project:' || to_project AS to_ref
    , kind
    , knowledge_state
    , 'project_relation' AS origin
FROM relations
UNION ALL
SELECT
    'membership:' || length(ep.entry_id) || ':' || ep.entry_id || ':' || ep.project_id AS id
    , ep.entry_id AS native_id
    , 'entry:' || ep.entry_id AS from_ref
    , 'project:' || ep.project_id AS to_ref
    , 'belongs_to_project' AS kind
    , e.knowledge_state
    , 'entry_project' AS origin
FROM entry_projects ep
    JOIN entries   e ON e.id = ep.entry_id
;
