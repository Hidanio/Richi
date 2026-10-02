"""Explicit, read-only source observations stored outside the knowledge database.

Content hashes detect drift, not truth or current deployment state. No URL is
fetched, no unselected repository is scanned, and no memory timestamp is updated.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile


FORMAT = "project-memory-source-watch"
VERSION = 1
MAX_REFS = 200
MAX_EDGES = 1000
MAX_SOURCES = 1000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_REFERENCE_CHARS = 4096
LOCATOR = re.compile(r"(?P<locator>:[1-9][0-9]*(?::[1-9][0-9]*)?|#L[1-9][0-9]*(?:-L?[1-9][0-9]*)?)$")
SHA = re.compile(r"[a-f0-9]{64}\Z")
NOTICE = "Unchanged bytes do not establish that a fact or deployment is current. No verified_at fields were updated."


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(value):
    data = _canonical(value)
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError("Selected record exceeds the source-watch record size limit")
    return hashlib.sha256(data).hexdigest()


def _text(value, name, maximum=MAX_REFERENCE_CHARS):
    if not isinstance(value, str) or not value or len(value) > maximum or "\0" in value:
        raise ValueError(f"Invalid {name}")
    return value


def _ref(value):
    _text(value, "full record reference")
    family, separator, identity = value.partition(":")
    if not separator or not identity or family not in ("entry", "entity", "project"):
        raise ValueError("Use a full entry:, entity:, or project: reference")
    return family, identity


def _database(conn, db_path):
    path = str(Path(db_path).expanduser().resolve())
    attached = conn.execute("PRAGMA database_list").fetchall()
    actual = next((row[2] for row in attached if row[1] == "main"), None)
    if not actual or str(Path(actual).resolve()) != path:
        raise ValueError("Source-watch database path does not match the open connection")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in (1, 2):
        raise ValueError("Source-watch supports database schema 1 or 2 only")
    return path, version


@contextmanager
def _read_snapshot(conn):
    # Do not commit or roll back a transaction owned by the CLI/caller.
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if own_transaction:
            conn.rollback()


def _rows(conn, query, params=()):
    cursor = conn.execute(query, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _decode(row):
    result = dict(row)
    for key in ("sources", "tags", "aliases"):
        if key in result:
            result[key] = json.loads(result[key])
    return result


def _get_record(conn, ref, version):
    family, identity = _ref(ref)
    if family == "entity" and version == 1:
        return None
    table = {"entry": "entries", "entity": "entities", "project": "projects"}[family]
    rows = _rows(conn, f"""
SELECT
    *
FROM {table}
WHERE 1=1
    AND id = ?
;
""", (identity,))
    if not rows:
        return None
    record = _decode(rows[0])
    if family == "entry":
        record["project_ids"] = [row[0] for row in conn.execute("""
SELECT
    project_id
FROM entry_projects
WHERE 1=1
    AND entry_id = ?
ORDER BY project_id
;
""", (identity,))]
    return record


def _source_objects(record):
    sources = record.get("sources", [])
    if not isinstance(sources, list):
        raise ValueError("Selected record has an invalid sources array")
    result = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Selected record has an invalid source object")
        _text(source.get("reference"), "source reference")
        result.append(dict(source))
    if record.get("source"):
        result.append({"reference": _text(record["source"], "project source")})
    return result


def _source_references(record):
    return [source["reference"] for source in _source_objects(record)]


def _register_sources(owners, record, owner, evidence_bytes):
    for source in _source_objects(record):
        declaration = {"owner": owner, "source": source}
        declarations = owners.setdefault(source["reference"], [])
        if declaration not in declarations:
            evidence_bytes[0] += len(_canonical(declaration))
            if evidence_bytes[0] > MAX_MANIFEST_BYTES or len(owners) > MAX_SOURCES:
                raise ValueError("Source declarations exceed source-watch limits; select narrower refs")
            declarations.append(declaration)


def _selection(refs, project):
    refs = list(refs or [])
    if bool(refs) == bool(project):
        raise ValueError("Specify either one or more --ref values or one --project")
    if len(refs) > MAX_REFS:
        raise ValueError(f"At most {MAX_REFS} references may be selected")
    for ref in refs:
        _ref(ref)
    if project is not None:
        _text(project, "project ID")
    return {"refs": sorted(set(refs)), "project": project}


def _project_refs(conn, project, version):
    refs = {"project:" + project}
    entries = conn.execute("""
SELECT
    entry_id
FROM entry_projects
WHERE 1=1
    AND project_id = ?
LIMIT ?
;
""", (project, MAX_REFS + 1)).fetchall()
    refs.update("entry:" + row[0] for row in entries)
    if version == 2:
        scope_edges = _rows(conn, """
SELECT
    from_ref
    , sources
FROM graph_edges
WHERE 1=1
    AND to_ref = ?
    AND kind = 'used_in'
    AND knowledge_state = 'confirmed'
    AND from_ref LIKE 'entity:%'
LIMIT ?
;
""", ("project:" + project, MAX_EDGES + 1))
        if len(scope_edges) > MAX_EDGES:
            raise ValueError("Project scope exceeds source-watch edge limit; select narrower refs")
        for edge in scope_edges:
            if _source_references(_decode(edge)):
                refs.add(edge["from_ref"])
    if len(refs) > MAX_REFS:
        raise ValueError("Project scope exceeds source-watch record limit; select narrower refs")
    return sorted(refs)


def _collect(conn, selection, version, *, require_all):
    """Capture card revisions and incident explicit-edge membership atomically."""
    requested = selection["refs"]
    if selection["project"] is not None:
        requested = _project_refs(conn, selection["project"], version)
    records, owners, evidence_bytes = [], {}, [0]
    for ref in requested:
        record = _get_record(conn, ref, version)
        if record is None:
            if require_all:
                raise ValueError(f"Selected record does not exist: {ref}")
            continue
        records.append({"ref": ref, "sha256": _hash(record)})
        _register_sources(owners, record, ref, evidence_bytes)
    edges = []
    if version == 2 and requested:
        marks = ",".join("?" for _ in requested)
        rows = _rows(conn, f"""
SELECT
    *
FROM graph_edges
WHERE 1=1
    AND (from_ref IN ({marks}) OR to_ref IN ({marks}))
ORDER BY id
LIMIT ?
;
""", (*requested, *requested, MAX_EDGES + 1))
        if len(rows) > MAX_EDGES:
            raise ValueError("Incident edge set exceeds source-watch limit; select narrower refs")
        for row in rows:
            edge = _decode(row)
            edges.append({"id": edge["id"], "from_ref": edge["from_ref"],
                          "to_ref": edge["to_ref"], "sha256": _hash(edge)})
            _register_sources(owners, edge, "edge:" + edge["id"], evidence_bytes)
    if len(owners) > MAX_SOURCES:
        raise ValueError("Source count exceeds source-watch limit; select narrower refs")
    return records, edges, owners


def _local_path(reference):
    if not reference.startswith("/"):
        return None, None
    # A real filename ending in :12 or #L12 wins over locator interpretation.
    if os.path.lexists(reference):
        return reference, ""
    match = LOCATOR.search(reference)
    if match:
        return reference[:match.start()], match.group("locator")
    return reference, ""


def _observe(reference, declarations, max_file_bytes, remaining):
    path, locator = _local_path(reference)
    result = {"reference": reference, "owners": sorted({item["owner"] for item in declarations}),
              "evidence": sorted(declarations, key=_canonical), "path": path, "locator": locator}
    if path is None:
        result.update(status="unsupported", reason="Only absolute local files are hashed; no network fetch")
        return result, 0
    fd = None
    read_bytes = 0
    try:
        # O_NONBLOCK ensures a FIFO cannot stall even if the path changes while opening.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            result.update(status="unavailable", reason="not_regular_file")
            return result, 0
        if before.st_size > max_file_bytes:
            result.update(status="unavailable", reason="file_size_limit")
            return result, 0
        if before.st_size > remaining:
            result.update(status="unavailable", reason="total_bytes_limit")
            return result, 0
        digest = hashlib.sha256()
        while read_bytes < before.st_size:
            block = os.read(fd, min(64 * 1024, before.st_size - read_bytes))
            if not block:
                break
            digest.update(block)
            read_bytes += len(block)
        after = os.fstat(fd)
        path_after = os.stat(path)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if read_bytes != before.st_size or any(
                getattr(before, key) != getattr(after, key) or getattr(after, key) != getattr(path_after, key)
                for key in fields):
            result.update(status="unavailable", reason="file_changed_during_read")
            return result, read_bytes
        result.update(status="hashed", sha256=digest.hexdigest(), size_bytes=read_bytes)
        return result, read_bytes
    except OSError as exc:
        result.update(status="unavailable", reason="local_io_error", errno=exc.errno)
        return result, read_bytes
    finally:
        if fd is not None:
            os.close(fd)


def _observe_sources(owners, max_file_bytes):
    sources, consumed = [], 0
    for reference, refs in sorted(owners.items()):
        observation, count = _observe(reference, refs, max_file_bytes, MAX_TOTAL_BYTES - consumed)
        sources.append(observation)
        consumed += count
    return sources, consumed


def _output_target(output, db_path):
    target = Path(output).expanduser().absolute()
    if target.resolve() == Path(db_path).resolve():
        raise ValueError("Manifest output must not be the knowledge database")
    if os.path.lexists(target):
        raise ValueError("Manifest output already exists; snapshots never overwrite files")
    if not target.parent.is_dir():
        raise ValueError("Manifest output parent directory must already exist")
    return target


def _atomic_create(output, data, db_path):
    target = _output_target(output, db_path)
    encoded = _canonical(data) + b"\n"
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("Manifest exceeds the size limit; select fewer records")
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".source-watch-", dir=target.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic publication with no replacement, including a competing creator.
        os.link(temporary, target)
    except FileExistsError as exc:
        raise ValueError("Manifest output appeared concurrently; nothing was overwritten") from exc
    finally:
        if temporary is not None:
            os.unlink(temporary)
    return str(target)


def _file_limit(value):
    if type(value) is not int or not 1 <= value <= MAX_TOTAL_BYTES:
        raise ValueError(f"max_file_bytes must be between 1 and {MAX_TOTAL_BYTES}")
    return value


def _counts(sources):
    return {status: sum(source["status"] == status for source in sources)
            for status in ("hashed", "unchanged", "changed", "unavailable", "unsupported")}


def snapshot(conn, *, db_path, refs=None, project=None, output, max_file_bytes=MAX_FILE_BYTES):
    """Write one immutable sidecar. Does not write to or migrate the database."""
    selection = _selection(refs, project)
    max_file_bytes = _file_limit(max_file_bytes)
    with _read_snapshot(conn):
        database, version = _database(conn, db_path)
        _output_target(output, database)
        records, edges, owners = _collect(conn, selection, version, require_all=True)
    sources, consumed = _observe_sources(owners, max_file_bytes)
    manifest = {
        "format": FORMAT, "version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": database, "database_schema_version": version,
        "selection": selection, "records": records, "edges": edges, "sources": sources,
        "limits": {"max_file_bytes": max_file_bytes, "max_total_bytes": MAX_TOTAL_BYTES},
        "notice": NOTICE,
    }
    path = _atomic_create(output, manifest, database)
    return {"status": "created", "manifest": path, "records": len(records), "incident_edges": len(edges),
            "sources": _counts(sources), "bytes_read": consumed,
            "all_sources_hashed": all(s["status"] == "hashed" for s in sources), "notice": NOTICE}


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Manifest contains duplicate JSON keys")
        result[key] = value
    return result


def _read_manifest(path):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
            raise ValueError("Manifest must be a bounded regular file")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            data = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(data) > MAX_MANIFEST_BYTES:
            raise ValueError("Manifest exceeds size limit")
        return json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite manifest number")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Malformed source-watch manifest") from exc
    finally:
        if fd is not None:
            os.close(fd)


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError("Malformed manifest object fields")


def _list(value, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("Malformed or oversized manifest collection")
    return value


def _validate_manifest(value):
    _keys(value, ("format", "version", "created_at", "database", "database_schema_version",
                  "selection", "records", "edges", "sources", "limits", "notice"))
    if value["format"] != FORMAT or type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError("Unsupported source-watch manifest format/version")
    if type(value["database_schema_version"]) is not int or value["database_schema_version"] not in (1, 2):
        raise ValueError("Invalid manifest database schema")
    _text(value["created_at"], "manifest timestamp", 100)
    _text(value["notice"], "manifest notice", 1000)
    if not _text(value["database"], "database path").startswith("/"):
        raise ValueError("Manifest database path must be absolute")
    _keys(value["selection"], ("refs", "project"))
    _list(value["selection"]["refs"], MAX_REFS)
    normalized = _selection(value["selection"]["refs"], value["selection"]["project"])
    if normalized != value["selection"]:
        raise ValueError("Manifest selection must be unique and sorted")
    _keys(value["limits"], ("max_file_bytes", "max_total_bytes"))
    _file_limit(value["limits"]["max_file_bytes"])
    if type(value["limits"]["max_total_bytes"]) is not int or value["limits"]["max_total_bytes"] != MAX_TOTAL_BYTES:
        raise ValueError("Invalid manifest total byte limit")
    record_refs, edge_ids = set(), set()
    for record in _list(value["records"], MAX_REFS):
        _keys(record, ("ref", "sha256"))
        _ref(record["ref"])
        if not isinstance(record["sha256"], str) or not SHA.fullmatch(record["sha256"]):
            raise ValueError("Invalid record fingerprint")
        if record["ref"] in record_refs:
            raise ValueError("Duplicate manifest record")
        record_refs.add(record["ref"])
    if normalized["refs"] and record_refs != set(normalized["refs"]):
        raise ValueError("Manifest records do not cover selected references")
    if normalized["project"] and "project:" + normalized["project"] not in record_refs:
        raise ValueError("Manifest does not cover selected project")
    for edge in _list(value["edges"], MAX_EDGES):
        _keys(edge, ("id", "from_ref", "to_ref", "sha256"))
        _text(edge["id"], "edge ID")
        _ref(edge["from_ref"])
        _ref(edge["to_ref"])
        if edge["from_ref"] not in record_refs and edge["to_ref"] not in record_refs:
            raise ValueError("Manifest contains a non-incident edge")
        if edge["id"] in edge_ids or not isinstance(edge["sha256"], str) or not SHA.fullmatch(edge["sha256"]):
            raise ValueError("Invalid or duplicate edge fingerprint")
        edge_ids.add(edge["id"])
    seen_sources = set()
    for source in _list(value["sources"], MAX_SOURCES):
        _keys(source, ("reference", "owners", "evidence", "path", "locator", "status"), ("reason", "errno", "sha256", "size_bytes"))
        reference = _text(source["reference"], "source reference")
        if reference in seen_sources:
            raise ValueError("Duplicate manifest source")
        seen_sources.add(reference)
        owners = _list(source["owners"], MAX_REFS + MAX_EDGES)
        if not owners or any(not isinstance(owner, str) for owner in owners):
            raise ValueError("Invalid source owners")
        if owners != sorted(set(owners)) or not set(owners) <= record_refs | {"edge:" + identity for identity in edge_ids}:
            raise ValueError("Manifest source has invalid owners")
        evidence = _list(source["evidence"], (MAX_REFS + MAX_EDGES) * 100)
        for declaration in evidence:
            _keys(declaration, ("owner", "source"))
            if declaration["owner"] not in owners or not isinstance(declaration["source"], dict):
                raise ValueError("Invalid source declaration")
            declared = declaration["source"]
            _keys(declared, ("reference",), ("label", "type", "observed_at", "revision", "locator", "sha256", "git"))
            if declared["reference"] != reference:
                raise ValueError("Inconsistent source declaration")
            for key, item in declared.items():
                if key == "git":
                    from . import git_evidence
                    git_evidence.validate_anchor(declared)
                    continue
                if key == "observed_at" and item is None:
                    continue
                _text(item, "declared source " + key, MAX_MANIFEST_BYTES)
            if "sha256" in declared and not SHA.fullmatch(declared["sha256"]):
                raise ValueError("Invalid declared source digest")
        if {item["owner"] for item in evidence} != set(owners):
            raise ValueError("Source declarations do not cover source owners")
        if source["status"] not in ("hashed", "unavailable", "unsupported"):
            raise ValueError("Invalid baseline source status")
        path, locator = source["path"], source["locator"]
        if reference.startswith("/"):
            if not isinstance(path, str) or not path.startswith("/") or not isinstance(locator, str):
                raise ValueError("Invalid local source path/locator")
            if reference != path + locator or (locator and not LOCATOR.fullmatch(locator)) or source["status"] == "unsupported":
                raise ValueError("Inconsistent local source locator")
        elif path is not None or locator is not None or source["status"] != "unsupported":
            raise ValueError("Nonlocal sources must be unsupported")
        if source["status"] == "hashed":
            if not isinstance(source.get("sha256"), str) or not SHA.fullmatch(source["sha256"]):
                raise ValueError("Invalid source digest")
            if type(source.get("size_bytes")) is not int or not 0 <= source["size_bytes"] <= value["limits"]["max_file_bytes"]:
                raise ValueError("Invalid source size")
        elif "sha256" in source or "size_bytes" in source:
            raise ValueError("Unhashed source cannot contain a digest")
        if "reason" in source:
            _text(source["reason"], "source reason", 1000)
        if "errno" in source and source["errno"] is not None and type(source["errno"]) is not int:
            raise ValueError("Invalid source errno")
    return value


def _differences(before, after, identity):
    old, new = ({item[identity]: item for item in collection} for collection in (before, after))
    return {"added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()),
            "changed": sorted(key for key in old.keys() & new.keys() if old[key] != new[key]),
            "unchanged_count": sum(old[key] == new[key] for key in old.keys() & new.keys())}


def _bound_report(result, max_chars):
    result["truncated"] = False
    if max_chars is None:
        return result
    if type(max_chars) is not int or not 2000 <= max_chars <= 200000:
        raise ValueError("max_chars must be between 2000 and 200000")
    result["budget"] = {"max_chars": max_chars, "truncated": False, "sources_omitted": 0,
                        "record_refs_omitted": 0, "edge_ids_omitted": 0, "output_chars": 0}
    # Match the CLI's JSON encoding, including whitespace and its final newline.
    def size():
        for _ in range(10):
            actual = len(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            if actual == result["budget"]["output_chars"]:
                return actual
            result["budget"]["output_chars"] = actual
        raise ValueError("Could not settle source-check output budget")
    collections = [(result["sources"], "sources_omitted")]
    for key, count_key in (("records", "record_refs_omitted"), ("incident_edges", "edge_ids_omitted")):
        for change in ("added", "removed", "changed"):
            result[key][change + "_count"] = len(result[key][change])
            collections.append((result[key][change], count_key))
    while size() > max_chars:
        candidates = [(items, key) for items, key in collections if items]
        if not candidates:
            raise ValueError("max_chars is too small for source-check metadata")
        items, key = max(candidates, key=lambda candidate: len(_canonical(candidate[0])))
        removed = max(1, len(items) // 4)
        del items[-removed:]
        result["budget"][key] += removed
        result["budget"]["truncated"] = True
        result["truncated"] = True
    return result


def check(conn, *, db_path, manifest, max_chars=None):
    """Compare database revisions and current local bytes with an explicit sidecar."""
    if max_chars is not None and (type(max_chars) is not int or not 2000 <= max_chars <= 200000):
        raise ValueError("max_chars must be between 2000 and 200000")
    baseline = _validate_manifest(_read_manifest(manifest))
    with _read_snapshot(conn):
        database, version = _database(conn, db_path)
        if database != baseline["database"]:
            raise ValueError("Manifest belongs to a different database path")
        records, edges, owners = _collect(conn, baseline["selection"], version, require_all=False)
    current_sources, consumed = _observe_sources(owners, baseline["limits"]["max_file_bytes"])
    record_changes = _differences(baseline["records"], records, "ref")
    edge_changes = _differences(baseline["edges"], edges, "id")
    old = {source["reference"]: source for source in baseline["sources"]}
    current = {source["reference"]: source for source in current_sources}
    results = []
    for reference in sorted(old.keys() | current.keys()):
        previous, observed = old.get(reference), current.get(reference)
        result = {"reference": reference, "before": previous, "current": observed}
        if previous is None or observed is None:
            result.update(status="changed", reason="source_added" if previous is None else "source_no_longer_referenced")
        elif observed["status"] != "hashed":
            result.update(status=observed["status"], reason=observed.get("reason"))
        elif previous["status"] != "hashed":
            result.update(status="changed", reason="source_now_available")
        elif any(previous.get(key) != observed.get(key) for key in ("sha256", "path", "locator", "owners", "evidence")):
            result.update(status="changed", reason="content_or_reference_scope_changed")
        else:
            result.update(status="unchanged")
        results.append(result)
    changed = (version != baseline["database_schema_version"] or
               any(record_changes[k] or edge_changes[k] for k in ("added", "removed", "changed")) or
               any(result["status"] == "changed" for result in results))
    statuses = {result["status"] for result in results}
    status = "changed" if changed else "unavailable" if "unavailable" in statuses else "unsupported" if "unsupported" in statuses else "unchanged"
    return _bound_report({
        "status": status, "has_changes": bool(changed), "manifest": str(Path(manifest).absolute()),
        "records": record_changes, "incident_edges": edge_changes,
        "database_schema_changed": version != baseline["database_schema_version"],
        "sources": results, "counts": _counts(results), "bytes_read": consumed,
        "coverage": {"database_selection_complete": True,
                     "legacy_project_relations_checked": False,
                     "all_current_local_sources_hashed": all(s["status"] == "hashed" for s in current_sources if s["path"] is not None),
                     "all_current_sources_supported": all(s["status"] != "unsupported" for s in current_sources)},
        "notice": NOTICE,
    }, max_chars)
