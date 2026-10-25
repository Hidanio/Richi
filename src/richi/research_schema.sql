/* Additive schema 3: native roles extend canonical cards and histories. */
CREATE TABLE entry_research (
    entry_id TEXT PRIMARY KEY REFERENCES entries(id),
    role TEXT NOT NULL CHECK (role IN ('track','task','hypothesis','experiment','run','decision')),
    payload TEXT NOT NULL,
    assessment TEXT,
    outcome TEXT,
    applicability TEXT,
    observed_at TEXT
);
CREATE INDEX entry_research_role ON entry_research(role, entry_id);
CREATE INDEX entry_research_assessment ON entry_research(assessment, entry_id);
CREATE INDEX entry_research_outcome ON entry_research(outcome, entry_id);
CREATE INDEX entry_research_applicability ON entry_research(applicability, entry_id);
CREATE INDEX entry_research_observed ON entry_research(observed_at, entry_id);
CREATE INDEX research_track_members ON graph_edges(to_ref, from_ref)
    WHERE kind = 'in_track' AND knowledge_state <> 'superseded';
CREATE INDEX research_semantic_links ON graph_edges(kind, to_ref, from_ref, knowledge_state);
CREATE TABLE research_revisions (
    object_ref TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK (revision > 0)
);
INSERT INTO research_revisions(object_ref, revision)
SELECT 'entry:' || id, max(1, (SELECT count(*) FROM entry_history WHERE entry_id = entries.id)) FROM entries;
INSERT INTO research_revisions(object_ref, revision)
SELECT 'entity:' || id, max(1, (SELECT count(*) FROM entity_history WHERE object_id = entities.id)) FROM entities;
INSERT INTO research_revisions(object_ref, revision)
SELECT 'edge:' || id, max(1, (SELECT count(*) FROM edge_history WHERE object_id = graph_edges.id)) FROM graph_edges;
CREATE TABLE research_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    object_ref TEXT NOT NULL,
    revision INTEGER NOT NULL,
    event_kind TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX research_events_object ON research_events(object_ref, seq);
CREATE TABLE research_event_tracks (
    seq INTEGER NOT NULL REFERENCES research_events(seq),
    track_id TEXT NOT NULL REFERENCES entries(id),
    PRIMARY KEY (track_id, seq)
);
CREATE INDEX research_event_tracks_seq ON research_event_tracks(seq, track_id);
CREATE TABLE research_requests (
    request_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    response TEXT NOT NULL
);
CREATE TRIGGER research_events_immutable_update BEFORE UPDATE ON research_events BEGIN
    SELECT RAISE(ABORT, 'research_events is append-only');
END;
CREATE TRIGGER research_events_immutable_delete BEFORE DELETE ON research_events BEGIN
    SELECT RAISE(ABORT, 'research_events is append-only');
END;
CREATE TRIGGER research_event_tracks_immutable_update BEFORE UPDATE ON research_event_tracks BEGIN
    SELECT RAISE(ABORT, 'research_event_tracks is append-only');
END;
CREATE TRIGGER research_event_tracks_immutable_delete BEFORE DELETE ON research_event_tracks BEGIN
    SELECT RAISE(ABORT, 'research_event_tracks is append-only');
END;
INSERT INTO metadata(key, value) VALUES ('research_store_uuid', lower(hex(randomblob(16))));
