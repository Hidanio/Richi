"""Optional derived recall indexes. Build explicitly; every read is read-only.

The index is only a candidate gate: recall still applies its existing lexical
scores, guards and state/project filters. SQL-only dirty triggers let older
writers remain compatible. Dirty records always enter the candidate set.
Sources, history and evidence bodies are never copied into this index.
"""

import json
import re

from . import decision_cards, measurements


VERSION = "2"
VERSION_KEY = "richi_search_index_version"
KEYS = "richi_search_keys"
RECORDS = "richi_search_records"
DIRTY = "richi_search_dirty"
TABLES = {"entries": "entry", "entities": "entity"}
FAMILIES = {"entry": 1, "entity": 2}
FIELDS = {"entry": {"id": 1, "jira": 2, "alias": 3},
          "entity": {"id": 4, "alias": 5, "token": 6}}
INDEXED_COLUMNS = {"entries": ("id", "jira_key", "aliases"),
                   "entities": ("id", "title", "summary", "tags", "aliases")}
MAX_QUERY_CHARS = 4096
MAX_QUERY_WORDS = 64
MAX_RECORD_CHARS = 262144
MAX_RECORD_KEYS = 16384
MAX_KEY_CHARS = 4096
MAX_RECORD_KEY_BYTES = 1048576
MAX_ISSUE_KEYS = 2000
FILTER_INDEXES = {
    "richi_search_entries_state_kind": ("entries", "knowledge_state, kind"),
    "richi_search_entries_state_work": ("entries", "knowledge_state, work_state"),
    "richi_search_entities_state_kind": ("entities", "knowledge_state, kind"),
}

# A separate optional index lets releases that understand lookup format 2 keep
# using and rebuilding it. Store short lexical prefixes, never citations or rows.
ANNOTATIONS = "richi_brief_prefixes_v1"
ANNOTATION_VERSION = "richi_brief_prefix_version"
ANNOTATION_TRIGGERS = (ANNOTATIONS + "_insert", ANNOTATIONS + "_update", ANNOTATIONS + "_delete")
ANNOTATION_PREFIX_CHARS = 12
_WORD_START = re.compile(r"(?<![^\W_])(?=([^\W_][\w./:-]{0,%d}))" % (ANNOTATION_PREFIX_CHARS - 1))


def _annotation_prefixes(key, raw):
    from . import recall

    payload = json.loads(raw)
    if key.startswith(decision_cards.KEY_PREFIX):
        texts = decision_cards.search_fields(payload["card"])
    else:
        texts = (row[field] for rows in payload["rows"].values() for row in rows for field in measurements.FIELDS)
    prefixes = {""}  # A complete, possibly empty, set. Absence means scan the original.
    for text in texts:
        for word in _WORD_START.finditer(recall.normalize(text)):
            prefixes.add(word.group(1))
            if len(prefixes) > MAX_RECORD_KEYS:
                return ()
    return prefixes


def _build_annotation_index(conn, objects, api):
    version = conn.execute("SELECT value FROM metadata WHERE key = ?", (ANNOTATION_VERSION,)).fetchone()
    owned = {ANNOTATIONS, *ANNOTATION_TRIGGERS}
    if version is None and owned.intersection(objects):
        raise api.MemoryError("Reserved brief search index objects exist without an ownership marker")
    if version is not None and version[0] != "1":
        raise api.MemoryError("Unsupported derived brief search index version")
    for name in ANNOTATION_TRIGGERS:
        conn.execute("DROP TRIGGER IF EXISTS " + name)
    conn.execute("DROP TABLE IF EXISTS " + ANNOTATIONS)
    conn.execute("CREATE TABLE " + ANNOTATIONS + " (sidecar TEXT, prefix TEXT, "
                 "PRIMARY KEY (sidecar, prefix)) WITHOUT ROWID")
    families = (decision_cards.KEY_PREFIX + "*", measurements.KEY_PREFIX + "*")
    for key, raw in conn.execute("SELECT key, value FROM metadata WHERE key GLOB ? OR key GLOB ?", families):
        try:
            prefixes = _annotation_prefixes(key, raw)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            continue  # The reader still checks the original payload.
        conn.executemany("INSERT INTO " + ANNOTATIONS + " VALUES (?, ?)", ((key, prefix) for prefix in prefixes))
    # A writer of any generation invalidates the looked-up key in its own
    # transaction. Missing rows use the authoritative value until the next build.
    events = (("INSERT", ("NEW.key",)), ("UPDATE OF key, value", ("OLD.key", "NEW.key")),
              ("DELETE", ("OLD.key",)))
    for name, (event, references) in zip(ANNOTATION_TRIGGERS, events):
        condition = " OR ".join(f"{ref} GLOB '{pattern}'" for ref in references for pattern in families)
        conn.execute(f"CREATE TRIGGER {name} AFTER {event} ON metadata WHEN {condition} "
                     f"BEGIN DELETE FROM {ANNOTATIONS} WHERE sidecar IN ({', '.join(references)}); END")
    conn.execute("INSERT OR REPLACE INTO metadata VALUES (?, '1')", (ANNOTATION_VERSION,))


def annotation_gate(conn, terms):
    """SQL for sidecar rows that could match. Missing indexes and dirty rows always pass."""
    names = (ANNOTATIONS, *ANNOTATION_TRIGGERS)
    installed = dict(conn.execute("SELECT name,type FROM sqlite_master WHERE name IN (?,?,?,?)", names))
    if installed.get(ANNOTATIONS) != "table" or any(installed.get(n) != "trigger" for n in ANNOTATION_TRIGGERS):
        return "", []
    version = conn.execute("SELECT value FROM metadata WHERE key = ?", (ANNOTATION_VERSION,)).fetchone()
    if not version or version[0] != "1":
        return "", []
    # terms_for only returns letters, numbers and -_.:/, so GLOB's only
    # metacharacter here is the final '*'. Numeric boundaries are checked on the record.
    patterns = sorted({term[:ANNOTATION_PREFIX_CHARS] + "*" for term in terms})
    lookup = f"SELECT 1 FROM {ANNOTATIONS} a WHERE a.sidecar = m.key AND "
    matches = " OR ".join("a.prefix GLOB ?" for _ in patterns)
    return f" AND (NOT EXISTS ({lookup}a.prefix = '') OR EXISTS ({lookup}({matches})))", patterns


def _trigger_names(table):
    return ["richi_search_{}_{}".format(table, event) for event in ("insert", "update", "delete")]


def _schema(conn):
    objects = {row[0]: row[1] for row in conn.execute("SELECT name, type FROM sqlite_master")}
    families = {table: family for table, family in TABLES.items() if objects.get(table) == "table"}
    row = (conn.execute("SELECT value FROM metadata WHERE key = ?", (VERSION_KEY,)).fetchone()
           if objects.get("metadata") == "table" else None)
    version = row[0] if row else None
    required = {KEYS: "table", RECORDS: "table", DIRTY: "table", KEYS + "_record": "index"}
    for table in families:
        required.update({name: "trigger" for name in _trigger_names(table)})
    ready = (version == VERSION and "entries" in families and
             all(objects.get(name) == kind for name, kind in required.items()))
    return objects, families, version, ready


def status(conn):
    """Report optional index coverage without creating or repairing anything."""
    objects, families, version, ready = _schema(conn)
    result = {
        "available": ready, "version": version, "supported_version": VERSION,
        "keys": {}, "dirty": {}, "fallback_records": {}, "key_bytes": 0,
        "filter_indexes": sorted(name for name in FILTER_INDEXES if objects.get(name) == "index"),
        "dirty_behavior": "Scan dirty records directly, including oversized records.",
    }
    if not ready:
        result["status"] = "absent" if version is None else "unsupported" if version != VERSION else "incomplete"
        return result
    result["status"] = "ready"
    for family in families.values():
        fields = tuple(FIELDS[family].values())
        row = conn.execute("SELECT count(*), coalesce(sum(length(CAST(key AS BLOB))), 0) FROM " +
                           KEYS + " WHERE field IN (" + ",".join("?" for _ in fields) + ")", fields).fetchone()
        result["keys"][family] = row[0]
        result["key_bytes"] += row[1]
        result["dirty"][family] = conn.execute(
            "SELECT count(*) FROM " + DIRTY + " WHERE family = ?", (family,)).fetchone()[0]
        result["fallback_records"][family] = conn.execute(
            "SELECT count(*) FROM " + DIRTY + " WHERE family = ? AND reason = 'oversized'",
            (family,)).fetchone()[0]
    return result


def _record_keys(record, family):
    """Return all conservative keys, or None to retain direct-scan fallback."""
    from . import recall

    aliases = json.loads(record["aliases"])
    texts = [record["id"], *aliases]
    if family == "entry":
        texts.append(record["jira_key"] or "")
    else:
        tags = json.loads(record["tags"])
        texts.extend([record["title"], record["summary"], *tags])
    if sum(len(value) for value in texts) > MAX_RECORD_CHARS:
        return None
    normalized = [recall.normalize(value) for value in texts]
    if sum(len(value) for value in normalized) > MAX_RECORD_CHARS:
        return None
    keys, key_bytes = set(), 0

    def add(field, key):
        nonlocal key_bytes
        if not key or (field, key) in keys:
            return True
        if len(key) > MAX_KEY_CHARS:
            return False
        key_bytes += len(key.encode("utf-8"))
        keys.add((field, key))
        return len(keys) <= MAX_RECORD_KEYS and key_bytes <= MAX_RECORD_KEY_BYTES

    identity = normalized[0]
    # Store each native ID once. Query routing also strips one matching family
    # prefix, retaining both interpretations when an ID itself contains it.
    if not add("id", identity):
        return None
    alias_phrases = [recall.phrase(alias) for alias in aliases]
    for alias in alias_phrases:
        if not add("alias", alias):
            return None
    if family == "entry":
        return keys if add("jira", recall.normalize(record["jira_key"])) else None

    # matches_term() starts after *any* non-alphanumeric character. WORDS keeps
    # internal punctuation, so each separator suffix must also be searchable:
    # repo.foo/bar -> repo.foo/bar, foo/bar, bar. A whole-token-only index loses
    # these valid lexical matches. Aliases are scored in their phrase form.
    lexical_texts = [recall.normalize(record[field]) for field in ("id", "title", "summary")]
    lexical_texts.extend(recall.normalize(tag) for tag in tags)
    lexical_texts.extend(alias_phrases)
    for text in lexical_texts:
        for match in recall.WORDS.finditer(text):
            token = match.group(0)
            if not add("token", token):
                return None
            for separator in re.finditer(r"[-_.:/]", token):
                if not add("token", token[separator.end():]):
                    return None
    return keys


def _fields(table):
    return "id, jira_key, aliases" if table == "entries" else "id, title, summary, tags, aliases"


def _replace(conn, family, identity, record):
    keys = _record_keys(record, family) if record is not None else set()
    mapped = conn.execute("SELECT record FROM " + RECORDS + " WHERE family = ? AND id = ?",
                          (FAMILIES[family], identity)).fetchone()
    if mapped is not None:
        conn.execute("DELETE FROM " + KEYS + " WHERE record = ?", (mapped[0],))
    if record is None or keys is None:
        conn.execute("DELETE FROM " + RECORDS + " WHERE family = ? AND id = ?", (FAMILIES[family], identity))
    if keys is None:
        conn.execute("INSERT OR REPLACE INTO " + DIRTY + " (family, id, reason) VALUES (?, ?, 'oversized')",
                     (family, identity))
        return
    if record is not None:
        record_id = mapped[0] if mapped is not None else conn.execute(
            "INSERT INTO " + RECORDS + " (family, id) VALUES (?, ?)", (FAMILIES[family], identity)).lastrowid
        conn.executemany("INSERT INTO " + KEYS + " (field, key, record) VALUES (?, ?, ?)",
                         ((FIELDS[family][field], key, record_id) for field, key in sorted(keys)))
    conn.execute("DELETE FROM " + DIRTY + " WHERE family = ? AND id = ?", (family, identity))


def refresh_record(conn, table, identity):
    """Refresh after a normal write. Absent/incomplete indexes remain untouched.

    The caller's transaction normally covers the write and refresh. A savepoint
    also keeps this refresh atomic for existing direct autocommit API callers;
    their preceding write already marked the record dirty if refresh fails.
    """
    if table not in TABLES or not _schema(conn)[3]:
        return
    conn.execute("SAVEPOINT richi_search_refresh")
    try:
        dirty = conn.execute("SELECT reason FROM " + DIRTY + " WHERE family = ? AND id = ?",
                             (TABLES[table], identity)).fetchone()
        # Relevant SQL changes mark dirty even for older writers. Metadata and
        # evidence edits need no new keys; oversized records stay on the scan
        # path until an indexed field changes or an explicit rebuild retries.
        if dirty is None or dirty[0] == "oversized":
            conn.execute("RELEASE richi_search_refresh")
            return
        row = conn.execute("SELECT " + _fields(table) + " FROM " + table + " WHERE id = ?", (identity,)).fetchone()
        _replace(conn, TABLES[table], identity, dict(row) if row is not None else None)
        conn.execute("RELEASE richi_search_refresh")
    except BaseException:
        conn.execute("ROLLBACK TO richi_search_refresh")
        conn.execute("RELEASE richi_search_refresh")
        raise


def build(conn, api):
    """Atomically rebuild derived keys, SQL dirty triggers and filter Btrees.

    A write transaction is mandatory. No user_version, knowledge, history or
    FTS changes; previous packages can still write because triggers use SQL.
    A savepoint preserves the old index even if the caller catches an error.
    """
    if not conn.in_transaction:
        raise api.MemoryError("Search index build requires an explicit write transaction")
    api.backend(conn)
    objects, families, version, _ = _schema(conn)
    owned = {KEYS, RECORDS, DIRTY, KEYS + "_record", *FILTER_INDEXES}
    owned.update(name for table in TABLES for name in _trigger_names(table))
    if version not in {None, "1", VERSION}:
        raise api.MemoryError("Unsupported derived search index version; no rebuild was attempted")
    if version is None and owned.intersection(objects):
        raise api.MemoryError("Reserved search index objects already exist without an ownership marker")
    if version == "1" and RECORDS in objects:
        raise api.MemoryError("Reserved record-map object already exists outside the version 1 index")
    conn.execute("SAVEPOINT richi_search_build")
    try:
        measurements.index_inline_sources(conn)
        _build_annotation_index(conn, objects, api)
        for table in TABLES:
            for name in _trigger_names(table):
                conn.execute("DROP TRIGGER IF EXISTS " + name)
        for name in FILTER_INDEXES:
            conn.execute("DROP INDEX IF EXISTS " + name)
        conn.execute("DROP TABLE IF EXISTS " + KEYS)
        conn.execute("DROP TABLE IF EXISTS " + RECORDS)
        conn.execute("DROP TABLE IF EXISTS " + DIRTY)
        # Named INTEGER PRIMARY KEY refs belong to this derived map, not the
        # authoritative tables' implicit rowids (which can change on VACUUM).
        conn.execute("CREATE TABLE " + RECORDS + " (record INTEGER PRIMARY KEY, family INTEGER NOT NULL, "
                     "id TEXT NOT NULL, UNIQUE (family, id))")
        conn.execute("CREATE TABLE " + KEYS + " (field INTEGER NOT NULL, key TEXT COLLATE BINARY NOT NULL, "
                     "record INTEGER NOT NULL REFERENCES " + RECORDS + " (record), "
                     "PRIMARY KEY (field, key, record)) WITHOUT ROWID")
        conn.execute("CREATE TABLE " + DIRTY + " (family TEXT NOT NULL, id TEXT NOT NULL, "
                     "reason TEXT NOT NULL DEFAULT 'write', PRIMARY KEY (family, id)) WITHOUT ROWID")
        for table, family in families.items():
            for event, name in zip(("INSERT", "UPDATE", "DELETE"), _trigger_names(table)):
                refs = ["OLD", "NEW"] if event == "UPDATE" else ["OLD" if event == "DELETE" else "NEW"]
                # An outer INSERT ... ON CONFLICT DO UPDATE can override a
                # trigger's OR REPLACE conflict policy. Avoid a conflicting
                # INSERT entirely so old dirty rows cannot break normal upsert.
                statements = " ".join(
                    "UPDATE " + DIRTY + " SET reason = 'write' WHERE family = '" + family +
                    "' AND id = " + ref + ".id; INSERT INTO " + DIRTY +
                    " (family, id, reason) SELECT '" + family + "', " + ref + ".id, 'write' "
                    "WHERE NOT EXISTS (SELECT 1 FROM " + DIRTY + " WHERE family = '" + family +
                    "' AND id = " + ref + ".id);" for ref in refs)
                condition = (" WHEN " + " OR ".join("OLD." + column + " IS NOT NEW." + column
                             for column in INDEXED_COLUMNS[table])) if event == "UPDATE" else ""
                conn.execute("CREATE TRIGGER " + name + " AFTER " + event + " ON " + table +
                             condition + " BEGIN " + statements + " END")
            for row in conn.execute("SELECT " + _fields(table) + " FROM " + table):
                _replace(conn, family, row["id"], dict(row))
        # Fresh maps have no old keys to delete. Bulk-constructing the secondary
        # index packs it once instead of maintaining it for every inserted key.
        conn.execute("CREATE INDEX " + KEYS + "_record ON " + KEYS + " (record)")
        for name, (table, columns) in FILTER_INDEXES.items():
            if table in families:
                conn.execute("CREATE INDEX " + name + " ON " + table + " (" + columns + ")")
        conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", (VERSION_KEY, VERSION))
        result = status(conn)
        conn.execute("RELEASE richi_search_build")
        return result
    except BaseException:
        conn.execute("ROLLBACK TO richi_search_build")
        conn.execute("RELEASE richi_search_build")
        raise


def _query_parts(query):
    from . import recall

    if len(query) > MAX_QUERY_CHARS:
        return None
    words = recall.phrase(query).split()
    if len(words) > MAX_QUERY_WORDS:
        return None
    return recall.normalize(query).strip(), words


def _batches(values):
    # Stay below the older SQLite 999-variable limit even with many alias spans.
    values = sorted(set(values))
    return [values[start:start + 400] for start in range(0, len(values), 400)]


def _equal_ids(conn, family, field, values):
    result = set()
    for batch in _batches(values):
        result.update(row[0] for row in conn.execute(
            "SELECT r.id FROM " + KEYS + " k JOIN " + RECORDS + " r ON r.record = k.record "
            "WHERE k.field = ? AND k.key IN (" + ",".join("?" for _ in batch) + ")",
            [FIELDS[family][field], *batch]))
    return result


def _prefix_ids(conn, family, prefix):
    if not prefix:
        return set()
    # SQLite's BINARY collation follows UTF-8/codepoint order. Find the smallest
    # string above every extension of prefix; LIKE would have different case
    # semantics and can disable this index range lookup.
    upper = None
    for index in range(len(prefix) - 1, -1, -1):
        if ord(prefix[index]) < 0x10ffff:
            upper = prefix[:index] + chr(ord(prefix[index]) + 1)
            break
    clause = " AND key < ?" if upper is not None else ""
    params = [FIELDS[family]["token"], prefix] + ([upper] if upper is not None else [])
    return {row[0] for row in conn.execute(
        "SELECT r.id FROM " + KEYS + " k JOIN " + RECORDS + " r ON r.record = k.record "
        "WHERE k.field = ? AND key >= ?" + clause, params)}


def _routing_ids(conn, query, keys, family, words):
    from . import recall

    variants = {variant for key in keys for variant in recall.key_variants(key)}
    identities = {query}
    if query.startswith(family + ":"):
        identities.add(query[len(family) + 1:])
    identities.update(value for key in variants for value in (key, "task:" + key, "jira:" + key))
    result = _equal_ids(conn, family, "id", identities)
    if family == "entry":
        result.update(_equal_ids(conn, family, "jira", variants))
    # Concepts can have deliberate single-symbol/stopword aliases. Include all
    # spans for entities; lexical() decides which of them really add a score.
    spans = {" ".join(words)}
    for start in range(len(words)):
        for stop in range(start + (1 if family == "entity" else 2), len(words) + 1):
            spans.add(" ".join(words[start:stop]))
    result.update(_equal_ids(conn, family, "alias", spans))
    result.update(row[0] for row in conn.execute("SELECT id FROM " + DIRTY + " WHERE family = ?", (family,)))
    return result


def entry_ids(conn, query, keys):
    """Conservative FTS-exception IDs, or None to use the existing metadata scan."""
    parts = _query_parts(query)
    if parts is None or not _schema(conn)[3]:
        return None
    normalized_query, words = parts
    return _routing_ids(conn, normalized_query, keys, "entry", words)


def entity_ids(conn, query, terms, keys):
    """All possible positive lexical candidates, never limited or ranked here."""
    from . import recall

    parts = _query_parts(query)
    if parts is None or not _schema(conn)[3]:
        return None
    normalized_query, words = parts
    result = _routing_ids(conn, normalized_query, keys, "entity", words)
    prefixes = set(terms)
    prefixes.update(variant for key in keys for variant in recall.key_variants(key))
    for prefix in prefixes:
        result.update(_prefix_ids(conn, "entity", prefix))
    # A key need not be one lexical token: AB#123 is indexed as "ab" and "123",
    # and a record that holds the key holds every one of them. Only the last
    # token can continue in the text (123-rc); the others are whole tokens there.
    for key in keys:
        for variant in recall.key_variants(key):
            tokens = recall.WORDS.findall(variant)
            if tokens and tokens != [variant]:
                whole = [_equal_ids(conn, "entity", "token", [token]) for token in tokens[:-1]]
                result.update(_prefix_ids(conn, "entity", tokens[-1]).intersection(*whole))
    return result


def indexed_issue_keys(conn, variants, series):
    """Recorded issue keys among candidate spellings and series, or None to scan.

    The answer equals recall's scan of every record. Keys of a dirty record may
    be stale, so the index speaks for clean records only and dirty ones are
    read directly. A series lookup is a range of keys that continue the prefix
    with ASCII digits alone.
    """
    from . import recall

    _, families, _, ready = _schema(conn)
    if not ready or len(variants) > MAX_ISSUE_KEYS:
        return None
    source = " FROM " + KEYS + " k JOIN " + RECORDS + " r ON r.record = k.record WHERE k.field = ? AND "
    clean = " AND NOT EXISTS (SELECT 1 FROM " + DIRTY + " d WHERE d.family = ? AND d.id = r.id)"
    # A key is recorded as the jira_key of an entry or as a task:/jira: ID.
    places = [("entry", "jira", ("",))] + [(family, "id", ("task:", "jira:")) for family in families.values()]
    found = set()
    for family, field, leads in places:
        wanted = {lead + variant: variant for lead in leads for variant in variants}
        for batch in _batches(wanted):
            found.update(wanted[row[0]] for row in conn.execute(
                "SELECT DISTINCT k.key" + source + "k.key IN (" + ",".join("?" for _ in batch) + ")" + clean,
                [FIELDS[family][field], *batch, family]))
    # DEMO42 belongs to the series "demo-" too: a series has the spellings of its keys.
    probes = [(prefix, family, field, lead + spelling[:-1])
              for prefix in series for spelling in recall.key_variants(prefix + "0")
              for family, field, leads in places for lead in leads]
    exists = ("EXISTS (SELECT 1" + source + "k.key >= ? AND k.key < ? AND substr(k.key, ?) NOT GLOB '*[^0-9]*'" +
              clean + ")")
    found_series = set()
    for start in range(0, len(probes), 100):
        batch = probes[start:start + 100]
        row = conn.execute("SELECT " + ", ".join(exists for _ in batch), [
            value for _, family, field, first in batch
            for value in (FIELDS[family][field], first + "0", first + ":", len(first) + 1, family)]).fetchone()
        found_series.update(probe[0] for probe, hit in zip(batch, row) if hit)
    identities, jira_keys = [], []
    for table, family in families.items():
        dirty = [row[0] for row in conn.execute("SELECT id FROM " + DIRTY + " WHERE family = ?", (family,))]
        for batch in _batches(dirty):
            for row in conn.execute("SELECT id, " + ("jira_key" if family == "entry" else "NULL") + " FROM " + table +
                                    " WHERE id IN (" + ",".join("?" for _ in batch) + ")", batch):
                identities.append(row[0])
                jira_keys.append(row[1])
    dirty_variants, dirty_series = recall.issue_key_hits(
        recall.declared_issue_keys(identities, jira_keys), variants, series)
    return found | dirty_variants, found_series | dirty_series
