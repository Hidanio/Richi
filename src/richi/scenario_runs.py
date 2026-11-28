"""Scenario runs: scheduler, leases, report validation, run task entries and unified journal.

Richi never executes shell commands or spawns agents itself. Instead it owns
the deterministic state machine for every run in schema 4 tables
(`scenario_runs`, `scenario_steps`, `scenario_events`):

* `start_run` freezes a snapshot of the scenario definition, stores redacted
  inputs (secret values are never persisted), creates the run's `task` entry
  in the "scenario-run" category, and advances ready Richi-executed steps.
* `next_step` and `claim_step` return rendered step payloads for the calling
  agent (resolving skills with missing-skill warnings, rendering templates
  with field line/length profiles, checking `cwd` containment and reporting
  substituted command values) and acquire 30-minute leases.
* `report_step` validates the agent's report against `outputs` and `checks`
  (including symlink-safe `file_exists` and hardened `evidence_git`); a
  rejected report records reasons, renews the claiming chat's lease, and
  leaves the step claimed.
* `answer_step` records the human's answer to a `gate` or `checklist` step
  from the chat (enforcing gate `options`, `allow_comment` and required
  checklist items).
* `skip_step`, `retry_step`, `pause_run`, `resume_run`, `cancel_run`,
  `release_step`, `run_status`, and `list_runs` manage the lifecycle with
  optional `expected_revision` optimistic concurrency.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shlex
import time
from types import SimpleNamespace
import unicodedata

from . import agents, git_evidence, rules, scenario_findings, scenario_format, scenario_registry, scenarios, templating


LEASE_SECONDS = 30 * 60
MAX_REPORT_BYTES = 1024 * 1024
MAX_REASON = 4000
MAX_EVIDENCE_ITEMS = 100
MAX_GIT_EVIDENCE = 20
ACTIVE_RUN_STATUSES = ("running", "waiting", "paused")
TERMINAL_RUN_STATUSES = ("done", "failed", "cancelled")
_GIT_REV = re.compile(r"[0-9A-Za-z][0-9A-Za-z._/@~^-]{0,199}\Z")
_ARTIFACT_URI = re.compile(r"^richi://artifact/[A-Za-z0-9_.:-]+@[A-Za-z0-9_-]+(?:/[^?#\x00]+)?\Z")
MAX_ARTIFACTS = 50
RUN_META_PREFIX = "scenario-run-meta:"
_UNSET = object()


def require_v4(conn, api):
    if not scenario_registry.has_scenario_tables(conn):
        error = api.MemoryError("Scenario runs require schema 4; run richi migrate, which backs up the store first")
        error.code = "schema_upgrade_required"
        raise error


def _run_meta_key(run_id):
    return RUN_META_PREFIX + str(run_id)


def set_run_meta(conn, run_id, meta):
    """Store non-default run metadata (run_kind='eval'|'fork', forked_from, case_id) in metadata."""
    if not meta:
        return
    conn.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        (_run_meta_key(run_id), json.dumps(meta, ensure_ascii=False, separators=(",", ":"))),
    )


def get_run_meta(conn, run_id):
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (_run_meta_key(run_id),)).fetchone()
    if row is None or not row[0]:
        return {}
    try:
        val = json.loads(row[0])
        return val if isinstance(val, dict) else {}
    except (ValueError, TypeError):
        return {}


def all_run_meta(conn):
    rows = conn.execute(
        "SELECT key, value FROM metadata WHERE key >= ? AND key < ?",
        (RUN_META_PREFIX, RUN_META_PREFIX[:-1] + ";"),
    ).fetchall()
    out = {}
    for k, v in rows:
        rid = k[len(RUN_META_PREFIX):]
        try:
            parsed = json.loads(v)
            if isinstance(parsed, dict):
                out[rid] = parsed
        except (ValueError, TypeError):
            continue
    return out


def _valid_artifact_item(item):
    if isinstance(item, str):
        return _valid_artifact_uri(item)
    if not isinstance(item, dict):
        return False
    if not set(item).issubset({"uri", "kind", "title"}) or not _valid_artifact_uri(item.get("uri")):
        return False
    for opt in ("kind", "title"):
        if opt in item and (not isinstance(item[opt], str) or len(item[opt]) > 200 or "\x00" in item[opt]):
            return False
    return True


def _valid_artifact_uri(uri):
    if not isinstance(uri, str) or len(uri) > 1000 or not _ARTIFACT_URI.fullmatch(uri):
        return False
    _, _, tail = uri[len("richi://artifact/"):].partition("/")
    if tail and any(seg in ("", ".", "..") for seg in tail.split("/")):
        return False
    return True


def _failure(api, message, code, **details):
    error = api.MemoryError(message)
    error.code = code
    if details:
        error.details = details
    return error


def _parse_iso(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _format_iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now_dt(api, now_override=None):
    if now_override is not None:
        return now_override if isinstance(now_override, datetime) else _parse_iso(now_override)
    return _parse_iso(api.now())


def _no_nul(value, name, api, limit=None):
    if value is None:
        return None
    if not isinstance(value, str):
        raise _failure(api, name + " must be text", "invalid_input")
    if "\x00" in value:
        raise _failure(api, name + " must not contain NUL bytes", "invalid_input")
    if limit is not None and len(value) > limit:
        raise _failure(api, "%s is longer than %d characters" % (name, limit), "invalid_input")
    return value


def _render_field(template_str, context, *, line=False, limit=None, field="field", empty=True, strict_format=False):
    """Render a template and enforce one-line / non-empty / length profiles on the rendered text (C3)."""
    parsed = templating.parse_template(template_str)
    rendered = templating.render(parsed, context)
    if not empty and not rendered.strip():
        raise ValueError("%s rendered to empty text" % field)
    if limit is not None and len(rendered) > limit:
        raise ValueError("%s rendered longer than %d characters" % (field, limit))
    if line:
        if scenario_format._BAD_LINE_CHARS.search(rendered):
            raise ValueError("%s rendered with forbidden line breaks or control characters" % field)
        if strict_format and any(unicodedata.category(ch) == "Cf" for ch in rendered):
            raise ValueError("%s rendered with invisible format characters" % field)
    return rendered


def _substituted_values(template_str, context, secrets):
    """List of {path, value} substituted into a command template (secrets masked)."""
    parsed = templating.parse_template(template_str)
    shown = []
    for expr in parsed.expressions():
        path = expr.path
        is_secret = len(path.parts) >= 2 and path.parts[0] == "inputs" and path.parts[1] in secrets
        if is_secret:
            shown.append({"path": path.text, "value": "<secret>"})
            continue
        try:
            raw = templating.lookup(context, path)
            shown.append({"path": path.text, "value": raw})
        except templating.Unresolved:
            shown.append({"path": path.text, "value": None})
    return shown


def _record_event(conn, at, scenario_id, run_id, step_key, action, chat, agent, detail=None):
    conn.execute(
        "INSERT INTO scenario_events(at, scenario_id, run_id, step_key, action, chat, agent, detail_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (at, scenario_id, run_id, step_key, action, chat, agent,
         json.dumps(detail or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
    )


def _load_run(conn, run_id, api):
    row = conn.execute("SELECT * FROM scenario_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise api.NotFoundError("Unknown scenario run: " + run_id)
    return dict(row)


def _check_run_project(run_row, checked_project, api):
    """Verify --project-path matches the run's project scope (R2-J04)."""
    if checked_project is None:
        return
    if run_row["project_id"] is None:
        raise _failure(api, "Run %s is workspace-wide and not bound to project %s; omit --project-path" % (
            run_row["id"], checked_project), "project_mismatch")
    if run_row["project_id"] != checked_project:
        raise _failure(api, "Run %s belongs to project %s, not --project-path project %s" % (
            run_row["id"], run_row["project_id"], checked_project), "project_mismatch")


def _check_expected_revision(run_row, expected_revision, api):
    """Optimistic concurrency check on run revision (R2-J22)."""
    if expected_revision is not None and run_row["revision"] != expected_revision:
        raise _failure(api, "Conflict: run %s changed (stored revision %d, expected %d)" % (
            run_row["id"], run_row["revision"], expected_revision), "conflict")


def _load_steps(conn, run_id):
    rows = conn.execute(
        "SELECT * FROM scenario_steps WHERE run_id = ? ORDER BY position, COALESCE(matrix_index, -1), step_key",
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _step_by_id_map(definition):
    return {step["id"]: step for step in definition["steps"]}


def _validate_runtime_secrets(secrets_runtime, definition, api):
    """Validate --secret keys/values against declared secret inputs (R2-J19)."""
    if not secrets_runtime:
        return {}
    specs = definition.get("inputs") or {}
    secret_names = {name for name, spec in specs.items() if spec["type"] == "secret"}
    validated = {}
    for key, val in secrets_runtime.items():
        if key not in secret_names:
            raise _failure(api, "Unknown secret input: " + str(key), "invalid_input")
        try:
            validated[key] = scenario_format.check_input(key, specs[key], val)
        except scenario_format.FormatError as exc:
            raise _failure(api, str(exc), "invalid_input") from exc
    return validated


def _build_steps_context(step_rows, definition):
    """Build the `steps` dict for templating/conditions, with aggregate shape for matrix steps (A2, R2-J16)."""
    by_def = _step_by_id_map(definition)
    grouped = {}
    for row in step_rows:
        grouped.setdefault(row["step_id"], []).append(row)

    result = {}
    for step_id, rows in grouped.items():
        spec = by_def.get(step_id, {})
        if "matrix" not in spec:
            row = rows[0]
            if row["status"] in ("done", "failed") and row["result_json"]:
                payload = json.loads(row["result_json"])
                payload["status"] = row["status"]
                result[step_id] = payload
            else:
                result[step_id] = {"status": row["status"]}
            continue

        # Matrix step: placeholder row before expansion or N instance rows after expansion.
        if len(rows) == 1 and rows[0]["matrix_index"] is None:
            result[step_id] = {"status": rows[0]["status"], "items": [], "data": {}, "files": []}
            continue

        items, files = [], []
        data_buckets = {}
        statuses = []
        for row in rows:
            statuses.append(row["status"])
            parsed = json.loads(row["result_json"]) if row["result_json"] else {}
            matrix_vals = json.loads(row["matrix_json"]) if row["matrix_json"] else {}
            item = dict(parsed)
            item["key"] = row["step_key"]
            item["matrix"] = matrix_vals
            item["status"] = row["status"]
            items.append(item)
            if row["status"] == "done":
                for file_path in parsed.get("files") or []:
                    files.append(file_path)
                for key, val in (parsed.get("data") or {}).items():
                    data_buckets.setdefault(key, []).append(val)

        aggregated_data = {}
        for key, vals in data_buckets.items():
            if all(isinstance(v, list) for v in vals):
                merged = []
                for v in vals:
                    merged.extend(v)
                aggregated_data[key] = merged
            else:
                aggregated_data[key] = vals

        if all(st == "skipped" for st in statuses):
            agg_status = "skipped"
        elif any(st == "failed" for st in statuses):
            agg_status = "failed"
        elif all(st in ("done", "skipped") for st in statuses):
            agg_status = "done"
        else:
            agg_status = "running"

        result[step_id] = {"status": agg_status, "items": items, "data": aggregated_data, "files": files}
    return result


def _context(run_row, step_rows, definition, *, project_path=None, agent=None, now_dt=None, secrets_runtime=None):
    stored_inputs = json.loads(run_row["inputs_json"])
    input_specs = definition.get("inputs") or {}
    secret_names = {name for name, spec in input_specs.items() if spec["type"] == "secret"}
    inputs = {}
    for key, val in stored_inputs.items():
        if key in secret_names:
            if secrets_runtime and key in secrets_runtime:
                inputs[key] = secrets_runtime[key]
            elif secrets_runtime is None:
                # For presence/null checks in conditions: non-empty redacted secret is truthy, empty is falsey.
                inputs[key] = "<present>" if val == "<secret>" else ""
        else:
            inputs[key] = val
    if secrets_runtime:
        for key, val in secrets_runtime.items():
            if key in secret_names:
                inputs[key] = val
    now_date = (now_dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return {
        "inputs": inputs,
        "steps": _build_steps_context(step_rows, definition),
        "run": {"id": run_row["id"]},
        "project": {"path": str(project_path) if project_path else ""},
        "agent": {"id": agent or ""},
        "now": {"date": now_date},
    }


def _dependency_satisfied(dep_id, step_rows, by_def):
    """True when dep_id has finished (done, skipped, or failed with continue_on_failure) — C4 no skip cascade."""
    rows = [row for row in step_rows if row["step_id"] == dep_id]
    if not rows:
        return False
    spec = by_def[dep_id]
    allowed = {"done", "skipped"}
    if spec.get("continue_on_failure"):
        allowed.add("failed")
    return all(row["status"] in allowed for row in rows)


def _expire_leases(conn, run_row, step_rows, at_dt, at_iso):
    expired = False
    for row in step_rows:
        if row["status"] == "claimed" and row["lease_until"]:
            if _parse_iso(row["lease_until"]) <= at_dt:
                conn.execute(
                    "UPDATE scenario_steps SET status = 'ready', claimed_by_chat = NULL, claimed_by_agent = NULL, "
                    "lease_until = NULL, stop_continuations = 0 WHERE run_id = ? AND step_key = ?",
                    (run_row["id"], row["step_key"]),
                )
                _record_event(conn, at_iso, run_row["scenario_id"], run_row["id"], row["step_key"],
                              "lease_expired", row["claimed_by_chat"], row["claimed_by_agent"],
                              {"expired_at": row["lease_until"]})
                expired = True
    return expired


def _release_all_claims(conn, run_id):
    """Clear active leases and move claimed steps back to ready when a run stops (cancel/failed)."""
    conn.execute(
        "UPDATE scenario_steps SET status = 'ready', claimed_by_chat = NULL, claimed_by_agent = NULL, "
        "lease_until = NULL, stop_continuations = 0 WHERE run_id = ? AND status = 'claimed'",
        (run_id,),
    )


BASELINE_PREFIX = "scenario-baseline:"


def _baseline_key(scenario_id, project_id):
    return BASELINE_PREFIX + scenario_id + ":" + (project_id if project_id else "_global")


def get_baseline_run_id(conn, scenario_id, project_id):
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (_baseline_key(scenario_id, project_id),)).fetchone()
    if row is None or not row[0]:
        return None
    rid = str(row[0]).strip()
    exists = conn.execute("SELECT 1 FROM scenario_runs WHERE id = ?", (rid,)).fetchone()
    return rid if exists else None


def _all_baseline_ids(conn):
    rows = conn.execute("SELECT value FROM metadata WHERE key >= ? AND key < ?",
                        (BASELINE_PREFIX, BASELINE_PREFIX[:-1] + ";")).fetchall()
    return {str(r[0]).strip() for r in rows if r and r[0]}


def set_baseline(conn, api, run_id, *, clear=False, checked_project=None, author=None, now_override=None):
    """Pin or clear a completed run as the baseline for compare steps of its scenario/project."""
    require_v4(conn, api)
    author = author or rules.current_author()
    at_iso = _format_iso(_now_dt(api, now_override))
    run_row = _load_run(conn, run_id, api)
    _check_run_project(run_row, checked_project, api)
    key = _baseline_key(run_row["scenario_id"], run_row["project_id"])
    if clear:
        conn.execute("DELETE FROM metadata WHERE key = ?", (key,))
        _record_event(conn, at_iso, run_row["scenario_id"], run_id, None, "baseline_cleared",
                      author.by, author.agent, {})
        return {"status": "cleared", "run_id": run_id, "scenario_id": run_row["scenario_id"],
                "project_id": run_row["project_id"], "is_baseline": False}
    if run_row["status"] != "done":
        raise _failure(api, "Only completed (done) runs can be marked as baseline (run is %s)" % run_row["status"],
                       "invalid_state")
    run_kind = get_run_meta(conn, run_id).get("kind", "prod")
    if run_kind != "prod":
        raise _failure(api, "Only prod runs can be marked as baseline (run kind is %s)" % run_kind,
                       "invalid_state")
    conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (key, run_id))
    _record_event(conn, at_iso, run_row["scenario_id"], run_id, None, "baseline_set",
                  author.by, author.agent, {})
    return {"status": "set", "run_id": run_id, "scenario_id": run_row["scenario_id"],
            "project_id": run_row["project_id"], "is_baseline": True}


def _past_runs_summary(conn, scenario_id, project_id, exclude_run_id, limit=3):
    """Load recent completed/failed prod runs of the same scenario+project for memory.brief process memory."""
    fetch_limit = max(limit * 4, 20)
    if project_id is None:
        rows = conn.execute(
            "SELECT id, status, finished.finished_at, created_at, updated_at, inputs_json "
            "FROM scenario_runs LEFT JOIN ("
            "  SELECT run_id, MAX(finished_at) AS finished_at FROM scenario_steps GROUP BY run_id"
            ") AS finished ON finished.run_id = scenario_runs.id "
            "WHERE scenario_id = ? AND project_id IS NULL AND id <> ? AND status IN ('done', 'failed') "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (scenario_id, exclude_run_id, fetch_limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, status, finished.finished_at, created_at, updated_at, inputs_json "
            "FROM scenario_runs LEFT JOIN ("
            "  SELECT run_id, MAX(finished_at) AS finished_at FROM scenario_steps GROUP BY run_id"
            ") AS finished ON finished.run_id = scenario_runs.id "
            "WHERE scenario_id = ? AND project_id = ? AND id <> ? AND status IN ('done', 'failed') "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (scenario_id, project_id, exclude_run_id, fetch_limit),
        ).fetchall()
    meta_map = all_run_meta(conn)
    prod_rows = [r for r in rows if (meta_map.get(r["id"]) or {}).get("kind", "prod") == "prod"][:limit]
    out = []
    for row in prod_rows:
        rid = row["id"]
        steps = conn.execute(
            "SELECT step_key, status, result_json, reason FROM scenario_steps "
            "WHERE run_id = ? AND status IN ('done', 'failed') ORDER BY position, step_key",
            (rid,),
        ).fetchall()
        highlights = []
        for s in steps:
            if s["result_json"]:
                try:
                    sm = (json.loads(s["result_json"]).get("summary") or "").strip()
                except (ValueError, TypeError):
                    sm = ""
                if sm and not sm.startswith("[untrusted memory evidence:"):
                    highlights.append(f"{s['step_key']}={sm[:120]}")
            elif s["reason"]:
                highlights.append(f"{s['step_key']}=failed({s['reason'][:80]})")
        out.append({
            "run_id": rid,
            "status": row["status"],
            "created_at": row["created_at"],
            "highlights": highlights[:4],
        })
    return out


def _safe_link_entries(conn, api, edge_id, from_entry_id, to_entry_id, kind, description, sources, knowledge_state="confirmed"):
    """Link two entry nodes via graph.put, ignoring if endpoints are absent."""
    from . import graph
    if not from_entry_id or not to_entry_id or from_entry_id == to_entry_id:
        return
    if api.entry_record(conn, from_entry_id) is None or api.entry_record(conn, to_entry_id) is None:
        return
    safe_id = re.sub(r"[^A-Za-z0-9_.:-]", "-", edge_id)[:95]
    graph.put(conn, {
        "id": safe_id,
        "from_ref": "entry:" + from_entry_id,
        "to_ref": "entry:" + to_entry_id,
        "kind": kind,
        "description": description[:500],
        "sources": sources,
        "knowledge_state": knowledge_state,
    }, "edge", api)


def _step_data_for_compare(conn, run_id, step_id):
    """Extract comparable data dict for step_id from a historical run (handles matrix and scalar steps)."""
    rows = conn.execute(
        "SELECT status, matrix_index, result_json FROM scenario_steps WHERE run_id = ? AND step_id = ? ORDER BY position, step_key",
        (run_id, step_id),
    ).fetchall()
    if not rows:
        return {}
    if len(rows) == 1 and rows[0]["matrix_index"] is None:
        if rows[0]["result_json"]:
            try:
                return json.loads(rows[0]["result_json"]).get("data") or {}
            except (ValueError, TypeError):
                return {}
        return {}
    buckets = {}
    for row in rows:
        if row["status"] == "done" and row["result_json"]:
            try:
                data = json.loads(row["result_json"]).get("data") or {}
            except (ValueError, TypeError):
                continue
            for k, v in data.items():
                buckets.setdefault(k, []).append(v)
    merged = {}
    for k, vals in buckets.items():
        if all(isinstance(v, list) for v in vals):
            flat = []
            for v in vals:
                flat.extend(v)
            merged[k] = flat
        else:
            merged[k] = vals
    return merged


def _run_richi_step(conn, api, database, run_row, step_spec, ctx):
    """Execute a Richi-owned step (`condition`, `memory.recall`, `memory.brief`, `memory.record`, `compare`)."""
    kind = step_spec["type"]
    params = step_spec.get("params") or {}
    if kind == "condition":
        parsed = templating.parse_condition(params["expr"])
        outcome = templating.evaluate(parsed, ctx)
        return {
            "status": "done",
            "summary": "true" if outcome else "false",
            "data": {"value": bool(outcome)},
        }
    if kind == "memory.recall":
        run_meta = get_run_meta(conn, run_row["id"])
        mem_mode = run_meta.get("memory_mode")
        if mem_mode in ("clean", "with_fp_only"):
            return {"status": "done", "summary": f"Eval memory_mode={mem_mode}: recall omitted.", "data": {"refs": [], "count": 0}}
        from . import recall
        query = _render_field(params["query"], ctx, line=True, limit=2000, field="params.query", empty=False)
        limit = params.get("limit", 8)
        where = None
        if params.get("kinds"):
            quoted = ", ".join("'" + item + "'" for item in params["kinds"])
            where = "kind IN (" + quoted + ")"
        args = SimpleNamespace(
            query=query, project=run_row["project_id"], where=where, limit=limit,
            max_chars=16000, include_hypotheses=False, include_superseded=False,
            explain=False, expect=None,
        )
        res = recall.recall(conn, args, api)
        matches = res.get("results") or []
        if mem_mode == "as_of_commit" and run_meta.get("commit_time"):
            cutoff = str(run_meta["commit_time"])
            matches = [m for m in matches if not m.get("updated_at") or str(m["updated_at"]) <= cutoff]
        refs = [item.get("ref") for item in matches if item.get("ref")]
        lines = ["%s: %s" % (item.get("ref"), item.get("title", "")) for item in matches[:8]]
        summary = ("[untrusted memory evidence: %s]" % "; ".join(lines)[:1460]) if lines else "No matching records found."
        return {"status": "done", "summary": summary, "data": {"refs": refs, "count": len(refs)}}
    if kind == "memory.brief":
        run_meta = get_run_meta(conn, run_row["id"])
        mem_mode = run_meta.get("memory_mode")
        question = _render_field(params["question"], ctx, line=False, limit=2000, field="params.question", empty=False)
        if mem_mode == "clean":
            return {
                "status": "done",
                "summary": "Eval memory_mode=clean: prior memory, past runs and FP learnings omitted.",
                "data": {"refs": [], "count": 0, "past_runs": [], "learnings": []},
            }
        refs = []
        lines = []
        past_runs = []
        if mem_mode != "with_fp_only":
            from . import task_brief
            limit = params.get("limit", 12)
            args = SimpleNamespace(
                query=question, project=run_row["project_id"], limit=limit, max_chars=16000,
                include_hypotheses=False, include_superseded=False,
                base=None, target=None, worktree=False, repo=None, check_sources=False,
                where=None, explain=False, expect=None,
            )
            res = task_brief.brief(conn, args, api, database)
            cards = res.get("results") or []
            if mem_mode == "as_of_commit" and run_meta.get("commit_time"):
                cutoff = str(run_meta["commit_time"])
                cards = [c for c in cards if not c.get("updated_at") or str(c["updated_at"]) <= cutoff]
            refs = [item.get("ref") for item in cards if item.get("ref")]
            lines = ["%s: %s" % (item.get("ref"), item.get("title", "")) for item in cards[:8]]
            if params.get("include_past_runs", True) and mem_mode != "as_of_commit":
                past_runs = _past_runs_summary(conn, run_row["scenario_id"], run_row["project_id"], run_row["id"], limit=3)
                for pr in past_runs:
                    hl = ", ".join(pr["highlights"][:2])
                    lines.append("past-run %s (%s)%s" % (pr["run_id"], pr["status"], (": " + hl) if hl else ""))
        learnings = []
        if params.get("include_learnings") or mem_mode == "with_fp_only":
            from . import scenario_review
            learnings, fp_hints = scenario_review.load_scoped_fp_learnings(
                conn, project_id=run_row["project_id"], query=question,
            )
            lines.extend(fp_hints)
        summary = ("[untrusted memory evidence: %s]" % "; ".join(lines)[:1460]) if lines else "No prior records found for this context."
        return {
            "status": "done",
            "summary": summary,
            "data": {"refs": refs, "count": len(refs), "past_runs": past_runs, "learnings": learnings},
        }
    if kind == "memory.record":
        entry_kind = params["kind"]
        title = _render_field(params["title"], ctx, line=True, limit=scenario_format.MAX_TITLE, field="params.title", empty=False)
        body = _render_field(params["summary"], ctx, line=False, limit=4000, field="params.summary", empty=False)
        if params.get("entry_id"):
            entry_id = _render_field(params["entry_id"], ctx, line=True, limit=100, field="params.entry_id", empty=False)
        else:
            raw_id = "%s:scenario-%s-%s" % (entry_kind, run_row["id"], step_spec["id"])
            entry_id = re.sub(r"[^A-Za-z0-9_.:-]", "-", raw_id)[:100]
        run_meta = get_run_meta(conn, run_row["id"])
        ks = params.get("knowledge_state") or "confirmed"
        # Fork/eval runs must never write confirmed memory entries.
        if run_meta.get("kind", "prod") != "prod":
            ks = "hypothesis"
        # Forbid automated memory.record from setting false-positive tags (only explicit human feedback may).
        extra_tags = [
            t for t in (params.get("tags") or [])
            if isinstance(t, str) and t.strip() and t.strip().lower() != "false-positive"
        ]
        base_sources = [
            {"reference": "scenario:%s@v%d" % (run_row["scenario_id"], run_row["scenario_version"]),
             "type": "scenario", "label": run_row["scenario_id"]},
            {"reference": "scenario-run:" + run_row["id"], "type": "scenario-run", "label": run_row["id"]},
        ]
        existing = api.entry_record(conn, entry_id)
        if existing is not None:
            current = api.entry_get(conn, entry_id, history=False)
            updated = {k: v for k, v in current.items() if k not in ("created_at", "updated_at", "history")}
            updated["summary"] = (current.get("summary") or "") + "\n\n--- Run %s ---\n%s" % (run_row["id"], body)
            seen_refs = {s.get("reference") for s in (updated.get("sources") or []) if isinstance(s, dict)}
            updated["sources"] = list(updated.get("sources") or []) + [s for s in base_sources if s["reference"] not in seen_refs]
            merged_tags = list(dict.fromkeys(list(updated.get("tags") or []) + ["scenario:" + run_row["scenario_id"]] + extra_tags))
            updated["tags"] = merged_tags[:20]
            updated["expected_updated_at"] = current["updated_at"]
            put_res = api.entry_put(conn, updated)
        else:
            record = {
                "id": entry_id,
                "kind": entry_kind,
                "title": title,
                "summary": body,
                "project_ids": [run_row["project_id"]] if run_row["project_id"] else [],
                "knowledge_state": ks,
                "sources": base_sources,
                "tags": list(dict.fromkeys(["scenario-record", "scenario:" + run_row["scenario_id"]] + extra_tags))[:20],
                "expected_updated_at": None,
            }
            put_res = api.entry_put(conn, record)
        if params.get("link_task", True) and run_row["task_entry_id"]:
            _safe_link_entries(
                conn, api,
                f"edge:scn-{run_row['id']}-{step_spec['id']}",
                run_row["task_entry_id"], entry_id,
                "produces",
                f"Produced by scenario run {run_row['id']} step {step_spec['id']}",
                base_sources,
                knowledge_state=ks,
            )
        return {
            "status": "done",
            "summary": "Recorded %s (%s): %s" % (entry_id, put_res["status"], title[:160]),
            "data": {"entry_id": entry_id, "ref": "entry:" + entry_id, "kind": entry_kind, "status": put_res["status"]},
        }
    if kind == "compare":
        target_step = params["step"]
        current_data = (ctx["steps"].get(target_step) or {}).get("data") or {}
        base_run_id = None
        if params.get("baseline_run"):
            cand = _render_field(params["baseline_run"], ctx, line=True, limit=100, field="params.baseline_run", empty=False)
            if cand != run_row["id"] and conn.execute("SELECT 1 FROM scenario_runs WHERE id = ?", (cand,)).fetchone():
                base_run_id = cand
        if base_run_id is None:
            pinned = get_baseline_run_id(conn, run_row["scenario_id"], run_row["project_id"])
            if pinned and pinned != run_row["id"]:
                base_run_id = pinned
        if base_run_id is None:
            meta_map = all_run_meta(conn)
            if run_row["project_id"] is None:
                cands = conn.execute(
                    "SELECT id FROM scenario_runs WHERE scenario_id = ? AND project_id IS NULL AND id <> ? AND status = 'done' "
                    "ORDER BY created_at DESC, id DESC LIMIT 20",
                    (run_row["scenario_id"], run_row["id"]),
                ).fetchall()
            else:
                cands = conn.execute(
                    "SELECT id FROM scenario_runs WHERE scenario_id = ? AND project_id = ? AND id <> ? AND status = 'done' "
                    "ORDER BY created_at DESC, id DESC LIMIT 20",
                    (run_row["scenario_id"], run_row["project_id"], run_row["id"]),
                ).fetchall()
            for cand in cands:
                if (meta_map.get(cand[0]) or {}).get("kind", "prod") == "prod":
                    base_run_id = cand[0]
                    break
        if base_run_id is None:
            return {
                "status": "done",
                "summary": "No prior baseline run found; current run establishes initial reference.",
                "data": {"has_baseline": False, "baseline_run_id": None, "diffs": {}, "changed_count": 0},
            }
        base_data = _step_data_for_compare(conn, base_run_id, target_step)
        metric_paths = list(params["metrics"]) if params.get("metrics") else sorted(set(base_data) | set(current_data))
        diffs = {}
        summary_parts = []
        changed_count = 0
        for path in metric_paths:
            b_ok, b_val = _lookup_dotted(base_data, path)
            c_ok, c_val = _lookup_dotted(current_data, path)
            if not b_ok and not c_ok:
                continue
            if (isinstance(b_val, (int, float)) and not isinstance(b_val, bool)
                    and isinstance(c_val, (int, float)) and not isinstance(c_val, bool)):
                delta = round(c_val - b_val, 6)
                pct = round((delta / b_val) * 100.0, 2) if b_val != 0 else None
                changed = delta != 0
                if changed:
                    changed_count += 1
                diffs[path] = {"baseline": b_val, "current": c_val, "delta": delta, "pct": pct, "changed": changed}
                sign = "+" if delta > 0 else ""
                pct_str = f" ({sign}{pct}%)" if pct is not None else ""
                summary_parts.append(f"{path}: {b_val} -> {c_val} [{sign}{delta}{pct_str}]")
            elif isinstance(b_val, list) or isinstance(c_val, list):
                b_n = len(b_val) if isinstance(b_val, list) else 0
                c_n = len(c_val) if isinstance(c_val, list) else 0
                delta = c_n - b_n
                changed = delta != 0
                if changed:
                    changed_count += 1
                diffs[path] = {"baseline_count": b_n, "current_count": c_n, "delta": delta, "changed": changed}
                sign = "+" if delta > 0 else ""
                summary_parts.append(f"{path}: {b_n} -> {c_n} items [{sign}{delta}]")
            else:
                changed = b_val != c_val
                if changed:
                    changed_count += 1
                diffs[path] = {"baseline": b_val, "current": c_val, "changed": changed}
                summary_parts.append(f"{path}: {'changed' if changed else 'unchanged'}")
        summary = ("Compared with %s: %s" % (base_run_id, "; ".join(summary_parts[:6]))) if summary_parts else ("Compared with %s: no comparable metrics." % base_run_id)
        return {
            "status": "done",
            "summary": summary[:1000],
            "data": {"has_baseline": True, "baseline_run_id": base_run_id, "diffs": diffs, "changed_count": changed_count},
        }
    raise ValueError("Unsupported Richi step type: " + kind)


def _compute_run_status(run_row, step_rows, by_def):
    if run_row["status"] in ("paused", "cancelled"):
        return run_row["status"]
    for row in step_rows:
        if row["status"] == "failed" and not by_def[row["step_id"]].get("continue_on_failure"):
            return "failed"
    terminal = {"done", "skipped"}
    if all(row["status"] in terminal or (row["status"] == "failed" and by_def[row["step_id"]].get("continue_on_failure"))
           for row in step_rows):
        return "done"
    if any(row["status"] in ("ready", "claimed") for row in step_rows):
        return "running"
    return "waiting"


def _advance(conn, api, database, run_id, *, project_path=None, agent=None, now_override=None,
             force_bump=False, previous_status=None):
    """Expire stale leases, unlock ready steps, expand matrices, execute Richi steps, update run status."""
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)
    run_row = _load_run(conn, run_id, api)
    if run_row["status"] in ("paused", "cancelled", "done", "failed"):
        if force_bump:
            conn.execute(
                "UPDATE scenario_runs SET updated_at = ?, revision = revision + 1 WHERE id = ?",
                (at_iso, run_id),
            )
            run_row = _load_run(conn, run_id, api)
        return run_row, _load_steps(conn, run_id), False

    definition = json.loads(run_row["definition_json"])
    by_def = _step_by_id_map(definition)
    prev = previous_status if previous_status is not None else run_row["status"]
    changed = False

    step_rows = _load_steps(conn, run_id)
    if _expire_leases(conn, run_row, step_rows, at_dt, at_iso):
        changed = True
        step_rows = _load_steps(conn, run_id)

    for _ in range(scenario_format.MAX_STEPS + 5):
        progressed = False
        step_rows = _load_steps(conn, run_id)
        ctx = _context(run_row, step_rows, definition, project_path=project_path, agent=agent, now_dt=at_dt)

        for row in step_rows:
            if row["status"] != "pending":
                continue
            spec = by_def[row["step_id"]]
            if not all(_dependency_satisfied(dep, step_rows, by_def) for dep in spec["needs"]):
                continue

            if "if" in spec:
                try:
                    should_run = templating.evaluate(templating.parse_condition(spec["if"]), ctx)
                except ValueError as exc:
                    conn.execute(
                        "UPDATE scenario_steps SET status = 'blocked', reason = ? WHERE run_id = ? AND step_key = ?",
                        ("if condition error: " + str(exc), run_id, row["step_key"]),
                    )
                    _record_event(conn, at_iso, run_row["scenario_id"], run_id, row["step_key"],
                                  "step_blocked", None, None, {"reason": str(exc)})
                    progressed = changed = True
                    continue
                if not should_run:
                    conn.execute(
                        "UPDATE scenario_steps SET status = 'skipped', reason = 'if evaluated to false', "
                        "finished_at = ? WHERE run_id = ? AND step_key = ?",
                        (at_iso, run_id, row["step_key"]),
                    )
                    _record_event(conn, at_iso, run_row["scenario_id"], run_id, row["step_key"],
                                  "step_skipped", None, None, {"reason": "if evaluated to false"})
                    progressed = changed = True
                    continue

            if "matrix" in spec and row["matrix_index"] is None:
                try:
                    combos = scenario_format.expand_matrix(spec, ctx)
                except (templating.Unresolved, ValueError) as exc:
                    conn.execute(
                        "UPDATE scenario_steps SET status = 'blocked', reason = ? WHERE run_id = ? AND step_key = ?",
                        ("matrix error: " + str(exc), run_id, row["step_key"]),
                    )
                    _record_event(conn, at_iso, run_row["scenario_id"], run_id, row["step_key"],
                                  "step_blocked", None, None, {"reason": str(exc)})
                    progressed = changed = True
                    continue
                role, pinned = scenario_format.executor(spec)
                init_status = "waiting_human" if role == "human" else "ready"
                conn.execute("DELETE FROM scenario_steps WHERE run_id = ? AND step_key = ?", (run_id, row["step_key"]))
                for idx, combo in enumerate(combos):
                    inst_key = "%s.%d" % (spec["id"], idx)
                    conn.execute(
                        "INSERT INTO scenario_steps(run_id, step_key, step_id, position, matrix_index, matrix_json, "
                        "type, status, executor_role, executor_agent, started_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (run_id, inst_key, spec["id"], row["position"], idx,
                         scenario_format.canonical_json(combo), spec["type"], init_status, role, pinned,
                         at_iso if init_status == "waiting_human" else None),
                    )
                _record_event(conn, at_iso, run_row["scenario_id"], run_id, spec["id"],
                              "matrix_expanded", None, None, {"instances": len(combos)})
                progressed = changed = True
                break

            role, _ = scenario_format.executor(spec)
            if role == "richi":
                try:
                    res = _run_richi_step(conn, api, database, run_row, spec, ctx)
                except (templating.Unresolved, ValueError, api.MemoryError) as exc:
                    conn.execute(
                        "UPDATE scenario_steps SET status = 'blocked', reason = ? WHERE run_id = ? AND step_key = ?",
                        (str(exc), run_id, row["step_key"]),
                    )
                    _record_event(conn, at_iso, run_row["scenario_id"], run_id, row["step_key"],
                                  "step_blocked", None, None, {"reason": str(exc)})
                else:
                    conn.execute(
                        "UPDATE scenario_steps SET status = 'done', started_at = ?, finished_at = ?, result_json = ? "
                        "WHERE run_id = ? AND step_key = ?",
                        (at_iso, at_iso, json.dumps(res, ensure_ascii=False, separators=(",", ":")),
                         run_id, row["step_key"]),
                    )
                    _record_event(conn, at_iso, run_row["scenario_id"], run_id, row["step_key"],
                                  "step_done", None, "richi", {"summary": res.get("summary", "")[:300]})
                progressed = changed = True
            elif role == "human":
                conn.execute(
                    "UPDATE scenario_steps SET status = 'waiting_human', started_at = ? WHERE run_id = ? AND step_key = ?",
                    (at_iso, run_id, row["step_key"]),
                )
                _record_event(conn, at_iso, run_row["scenario_id"], run_id, row["step_key"],
                              "waiting_human", None, None, {})
                progressed = changed = True
            else:
                conn.execute(
                    "UPDATE scenario_steps SET status = 'ready' WHERE run_id = ? AND step_key = ?",
                    (run_id, row["step_key"]),
                )
                progressed = changed = True
        if not progressed:
            break

    step_rows = _load_steps(conn, run_id)
    new_status = _compute_run_status(run_row, step_rows, by_def)
    if new_status == "failed":
        _release_all_claims(conn, run_id)
        step_rows = _load_steps(conn, run_id)
    if new_status != prev or changed or force_bump:
        conn.execute(
            "UPDATE scenario_runs SET status = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
            (new_status, at_iso, run_id),
        )
        if new_status != prev:
            _record_event(conn, at_iso, run_row["scenario_id"], run_id, None,
                          "run_" + new_status, None, None, {"from": prev, "to": new_status})
        run_row = _load_run(conn, run_id, api)
    return run_row, step_rows, new_status != prev


# ---------------------------------------------------------------- run task entry ("Запущенные сценарии")


_STATUS_ICONS = {"done": "✓", "failed": "✗", "waiting_human": "⏳", "skipped": "⊘",
                 "claimed": "▶", "ready": "○", "pending": "·", "blocked": "!"}


def _clean_summary_text(text):
    return "".join(ch for ch in str(text).replace("\n", " ").replace("\r", " ") if ord(ch) >= 32)


def _format_run_summary(run_row, step_rows, definition):
    inputs = json.loads(run_row["inputs_json"])
    lines = [
        "Scenario: %s (v%d, %s)" % (run_row["scenario_id"], run_row["scenario_version"], run_row["scope"]),
        "Run: %s — status: %s" % (run_row["id"], run_row["status"]),
    ]
    if inputs:
        pairs = ", ".join("%s=%s" % (k, _clean_summary_text(
            v if not isinstance(v, (list, dict)) else json.dumps(v, ensure_ascii=False)))
            for k, v in inputs.items())
        lines.append("Inputs: " + pairs)
    lines.append("Steps:")
    for row in step_rows:
        icon = _STATUS_ICONS.get(row["status"], "?")
        note = ""
        if row["result_json"]:
            parsed = json.loads(row["result_json"])
            note = parsed.get("summary") or (
                ("answer=" + str(parsed["answer"])) if "answer" in parsed else "")
        elif row["reason"]:
            note = row["reason"]
        suffix = (" — " + _clean_summary_text(note)[:180]) if note else ""
        lines.append("  %s %s (%s)%s" % (icon, row["step_key"], row["status"], suffix))
    return "\n".join(lines)


def _sync_task_entry(conn, api, run_row, step_rows, definition, *, title=None, create=False):
    """Create or update the run's task entry (tag: scenario-run) on milestone transitions (R3-A1)."""
    entry_id = run_row["task_entry_id"]
    if not entry_id:
        return
    status = run_row["status"]
    state_map = {"running": "in_progress", "waiting": "blocked", "paused": "blocked",
                 "failed": "blocked", "done": "done", "cancelled": "done"}
    work_state = state_map.get(status, "in_progress")
    summary = _format_run_summary(run_row, step_rows, definition)
    if status == "cancelled":
        target_ks = "superseded"
    else:
        # Keep routine scenario run tasks as hypothesis so repeated runs never pollute
        # default confirmed recall/brief or the Knowledge Map until explicitly promoted.
        target_ks = "hypothesis"

    if create:
        clean_title = (title or "").strip() or ("%s: %s" % (definition["title"], run_row["id"]))
        record = {
            "id": entry_id,
            "kind": "task",
            "title": clean_title[:200],
            "summary": summary,
            "project_ids": [run_row["project_id"]] if run_row["project_id"] else [],
            "work_state": work_state,
            "knowledge_state": target_ks,
            "sources": [
                {"reference": "scenario:%s@v%d" % (run_row["scenario_id"], run_row["scenario_version"]),
                 "type": "scenario", "label": definition["title"]},
                {"reference": "scenario-run:" + run_row["id"], "type": "scenario-run", "label": run_row["id"]},
            ],
            "tags": ["scenario-run", "scenario:" + run_row["scenario_id"]],
            "aliases": [run_row["id"]],
            "expected_updated_at": None,
        }
        api.entry_put(conn, record)
        return
    # R2-J12: use entry_record() so a deleted task entry never crashes run transitions.
    if api.entry_record(conn, entry_id) is None:
        return
    current = api.entry_get(conn, entry_id, history=False)
    updated = {k: v for k, v in current.items() if k not in ("created_at", "updated_at", "history")}
    updated["summary"] = summary
    updated["work_state"] = work_state
    if status == "cancelled":
        updated["knowledge_state"] = "superseded"
    elif current.get("knowledge_state") != "confirmed":
        updated["knowledge_state"] = "hypothesis"
    updated["expected_updated_at"] = current["updated_at"]
    api.entry_put(conn, updated)


def _next_run_id(conn, scenario_id, at_dt):
    """Range-bounded, numeric sequence ID avoiding LIKE wildcards, rollover collisions and existing entries."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", scenario_id)[:40].strip("-") or "run"
    day = at_dt.strftime("%Y%m%d")
    prefix = "run-%s-%s-" % (safe, day)
    upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    rows = conn.execute(
        "SELECT id FROM scenario_runs WHERE id >= ? AND id < ?",
        (prefix, upper),
    ).fetchall()
    seq = 1
    for (existing,) in rows:
        tail = existing[len(prefix):]
        if tail.isdigit():
            seq = max(seq, int(tail) + 1)
    while True:
        candidate = "%s%04d" % (prefix, seq)
        if conn.execute("SELECT 1 FROM entries WHERE id = ?", ("task:scenario-" + candidate,)).fetchone() is None:
            return candidate
        seq += 1


# ---------------------------------------------------------------- public run operations


def start_run(conn, api, database, scenario_id, *, project_id=None, project_path=None, scope=None,
              raw_inputs=None, inputs_json=None, secrets_runtime=None, task_id=None, author=None,
              now_override=None, initial_run_meta=None):
    """Start a new run: validate inputs, freeze snapshot, create task entry, advance ready steps."""
    require_v4(conn, api)
    if project_id is not None:
        api.require_project(conn, project_id)
    if task_id is not None:
        if api.entry_record(conn, task_id) is None:
            raise api.NotFoundError("Unknown task entry for --task: " + str(task_id))
    try:
        record = scenarios.resolve(conn, scenario_id, project_id, scope=scope)
    except scenario_format.UnsupportedVersion as exc:
        raise _failure(api, str(exc), "unsupported_scenario") from exc
    except ValueError as exc:
        raise _failure(api, str(exc), "invalid_scenario") from exc
    if record is None:
        raise api.NotFoundError("Unknown scenario: " + scenario_id + "; see scenario list or scenario templates")

    definition = record["definition"]
    # R3-X1: fail fast when starting a workspace-wide run for a scenario with project-bound checks.
    if project_id is None and any(
        c["kind"] in scenario_registry.PROJECT_BOUND_CHECKS
        for step in definition.get("steps") or []
        for c in step.get("checks") or []
    ):
        raise _failure(
            api,
            "Scenario %s uses project-bound checks and requires --project or --project-path"
            % record["id"],
            "invalid_input",
        )
    try:
        provided = scenarios.parse_input_options(definition, raw_inputs, inputs_json)
        if secrets_runtime:
            validated_secrets = _validate_runtime_secrets(secrets_runtime, definition, api)
            provided.update(validated_secrets)
        _, redacted, missing, secret_names = scenarios.resolve_inputs(definition, provided)
    except scenario_format.FormatError as exc:
        raise _failure(api, str(exc), "invalid_input") from exc
    if missing:
        raise _failure(api, "Missing required input(s): " + ", ".join(missing), "missing_input", missing=missing)

    author = author or rules.current_author()
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)
    run_id = _next_run_id(conn, record["id"], at_dt)
    is_eval_run = isinstance(initial_run_meta, dict) and initial_run_meta.get("kind") == "eval"
    task_entry_id = None if is_eval_run else ("task:scenario-" + run_id)

    # R2-J19 & R2-J24: filter by secret_names (not sentinel text) and require non-empty rendered title.
    title_ctx = {
        "inputs": {k: v for k, v in redacted.items() if k not in secret_names},
        "steps": {},
        "run": {"id": run_id},
        "project": {"path": str(project_path) if project_path else ""},
        "agent": {"id": author.agent or ""},
        "now": {"date": at_dt.strftime("%Y-%m-%d")},
    }
    if "run_title" in definition:
        try:
            run_title = _render_field(definition["run_title"], title_ctx, line=True,
                                      limit=scenario_format.MAX_TITLE, field="run_title", empty=False)
        except (templating.Unresolved, ValueError) as exc:
            raise _failure(api, str(exc), "invalid_input") from exc
    else:
        run_title = "%s: %s" % (definition["title"], run_id)

    conn.execute(
        "INSERT INTO scenario_runs(id, scenario_id, scope, project_id, scenario_version, scenario_sha256, "
        "definition_json, inputs_json, status, task_entry_id, created_by_chat, created_by_agent, "
        "created_at, updated_at, revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, 1)",
        (run_id, record["id"], record["scope"], project_id, record["version"], record["sha256"],
         json.dumps(definition, ensure_ascii=False, separators=(",", ":")),
         json.dumps(redacted, ensure_ascii=False, separators=(",", ":")),
         task_entry_id, author.by, author.agent, at_iso, at_iso),
    )
    if isinstance(initial_run_meta, dict) and initial_run_meta:
        set_run_meta(conn, run_id, initial_run_meta)
    for pos, step in enumerate(definition["steps"]):
        role, pinned = scenario_format.executor(step)
        conn.execute(
            "INSERT INTO scenario_steps(run_id, step_key, step_id, position, matrix_index, matrix_json, "
            "type, status, executor_role, executor_agent) VALUES (?, ?, ?, ?, NULL, NULL, ?, 'pending', ?, ?)",
            (run_id, step["id"], step["id"], pos, step["type"], role, pinned),
        )
    _record_event(conn, at_iso, record["id"], run_id, None, "run_started", author.by, author.agent,
                  {"version": record["version"], "scope": record["scope"], "inputs": redacted})

    run_row, step_rows, _ = _advance(conn, api, database, run_id, project_path=project_path,
                                     agent=author.agent, now_override=at_dt)
    if task_entry_id is not None:
        _sync_task_entry(conn, api, run_row, step_rows, definition, title=run_title, create=True)
    if task_id is not None and task_entry_id is not None:
        _safe_link_entries(
            conn, api,
            f"edge:scn-task-{run_id}",
            task_entry_id, task_id,
            "related_to",
            f"Scenario run {run_id} executed for {task_id}",
            [{"reference": "scenario-run:" + run_id, "type": "scenario-run", "label": run_id}],
            knowledge_state="hypothesis",
        )
    return _format_status(run_row, step_rows, definition, view="scenario-run", run_meta=get_run_meta(conn, run_id))


def _check_cwd_containment(rendered_cwd, project_path):
    """Ensure rendered cwd is relative without traversal, and inside project_path when given (R2-J09)."""
    if rendered_cwd.startswith("/") or "\\" in rendered_cwd or re.match(r"[A-Za-z]:", rendered_cwd):
        raise ValueError("command.cwd must be a relative POSIX path inside the project")
    segments = rendered_cwd.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError("command.cwd must not contain empty, '.' or '..' segments: " + rendered_cwd)
    if project_path is None:
        return rendered_cwd
    root = Path(project_path).resolve()
    target = (root / rendered_cwd).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("command.cwd escapes --project-path: " + rendered_cwd) from exc
    return str(target)


def _render_step_payload(step_row, step_spec, ctx, definition, *, project_path=None, agent=None,
                         secrets_names=None, installed_skills=None):
    """Render a ready/claimed/waiting step for the agent to act on, enforcing C3/K1/I2/R2-J28."""
    step_ctx = dict(ctx)
    if step_row["matrix_json"]:
        step_ctx["matrix"] = json.loads(step_row["matrix_json"])
    secrets_names = secrets_names or set()

    title_ctx = dict(step_ctx, inputs={k: v for k, v in step_ctx["inputs"].items() if k not in secrets_names})
    title = step_spec.get("title", step_spec["id"])
    if "title" in step_spec:
        title = _render_field(step_spec["title"], title_ctx, line=True,
                              limit=scenario_format.MAX_TITLE, field="title", empty=False)

    kind = step_spec["type"]
    params = step_spec.get("params") or {}
    payload = {
        "key": step_row["step_key"],
        "id": step_spec["id"],
        "type": kind,
        "title": title,
        "status": step_row["status"],
        "executor": {"role": step_row["executor_role"], "agent": step_row["executor_agent"]},
    }
    if step_row["matrix_json"]:
        payload["matrix"] = json.loads(step_row["matrix_json"])
    if step_spec.get("hints"):
        payload["hints"] = step_spec["hints"]
    if step_spec.get("outputs"):
        payload["outputs"] = step_spec["outputs"]
    if step_spec.get("checks"):
        payload["checks"] = step_spec["checks"]

    if kind == "instruction":
        payload["text"] = _render_field(params["text"], step_ctx, line=False,
                                        limit=templating.MAX_RENDERED, field="params.text", empty=False)
    elif kind == "skill":
        chosen = scenarios.skill_for(params["skill"], agent)
        payload["skill"] = chosen
        if agent:
            if not chosen:
                payload["skill_warning"] = "No skill configured for agent %s on step %s" % (agent, step_spec["id"])
            elif installed_skills is not None and chosen not in installed_skills:
                payload["skill_warning"] = "Agent %s does not have skill %s installed" % (agent, chosen)
        if "instruction" in params:
            payload["instruction"] = _render_field(params["instruction"], step_ctx, line=False,
                                                   limit=templating.MAX_RENDERED, field="params.instruction")
    elif kind == "command":
        payload["command"] = _render_field(params["command"], step_ctx, line=True, limit=4000,
                                           field="params.command", empty=False, strict_format=True)
        payload["substituted_values"] = _substituted_values(params["command"], step_ctx, secrets_names)
        if "cwd" in params:
            rendered_cwd = _render_field(params["cwd"], title_ctx, line=True, limit=1000,
                                         field="params.cwd", empty=False, strict_format=True)
            payload["cwd"] = _check_cwd_containment(rendered_cwd, project_path)
        if "expect_exit" in params:
            payload["expect_exit"] = params["expect_exit"]
        if "tail_lines" in params:
            payload["tail_lines"] = params["tail_lines"]
    elif kind == "gate":
        payload["question"] = _render_field(params["question"], title_ctx, line=False,
                                            limit=4000, field="params.question", empty=False)
        if "options" in params:
            payload["options"] = params["options"]
        if "allow_comment" in params:
            payload["allow_comment"] = params["allow_comment"]
    elif kind == "checklist":
        items = []
        for idx, item in enumerate(params["items"]):
            items.append({
                "index": idx,
                "text": _render_field(item["text"], title_ctx, line=True, limit=1000,
                                      field="params.items[%d].text" % idx, empty=False),
                "required": bool(item.get("required", False)),
            })
        payload["items"] = items
    return payload


def _block_step_and_commit(conn, api, run_row, definition, step_key, reason, author, active_agent, at_iso):
    """Persist blocked state cleanly and update run status/task entry (never raises to roll back) — R2-J02."""
    by_def = _step_by_id_map(definition)
    conn.execute(
        "UPDATE scenario_steps SET status = 'blocked', claimed_by_chat = NULL, claimed_by_agent = NULL, "
        "lease_until = NULL, reason = ? WHERE run_id = ? AND step_key = ?",
        (reason, run_row["id"], step_key),
    )
    _record_event(conn, at_iso, run_row["scenario_id"], run_row["id"], step_key,
                  "step_blocked", author.by, active_agent, {"reason": reason})
    step_rows = _load_steps(conn, run_row["id"])
    previous_status = run_row["status"]
    new_status = _compute_run_status(run_row, step_rows, by_def)
    conn.execute(
        "UPDATE scenario_runs SET status = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
        (new_status, at_iso, run_row["id"]),
    )
    if new_status != previous_status:
        _record_event(conn, at_iso, run_row["scenario_id"], run_row["id"], None,
                      "run_" + new_status, author.by, active_agent, {"from": previous_status, "to": new_status})
    updated_run = _load_run(conn, run_row["id"], api)
    if new_status != previous_status:
        _sync_task_entry(conn, api, updated_run, step_rows, definition)
    return updated_run, step_rows


def _step_has_check(spec, kind):
    return any(c.get("kind") == kind for c in (spec.get("checks") or ()))


def _latest_claimed_fingerprint(conn, run_id, step_key):
    row = conn.execute(
        "SELECT detail_json FROM scenario_events "
        "WHERE run_id = ? AND step_key = ? AND action = 'claimed' "
        "ORDER BY id DESC LIMIT 1",
        (run_id, step_key),
    ).fetchone()
    if row is None or not row[0]:
        return None
    try:
        det = json.loads(row[0])
        return det.get("worktree_fingerprint") if isinstance(det, dict) else None
    except (ValueError, TypeError):
        return None


def next_step(conn, api, database, run_id, *, claim=False, step_key=None, checked_project=None,
              project_path=None, agent=None, author=None, secrets_runtime=None, installed_skills=_UNSET,
              claim_fingerprint=None, now_override=None):
    """Return the next actionable step for this agent/chat; with claim=True, acquire a 30m lease."""
    require_v4(conn, api)
    author = author or rules.current_author()
    active_agent = agent or author.agent
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)

    run_check = _load_run(conn, run_id, api)
    _check_run_project(run_check, checked_project, api)
    definition = json.loads(run_check["definition_json"])
    validated_secrets = _validate_runtime_secrets(secrets_runtime, definition, api)

    run_row, step_rows, status_changed = _advance(conn, api, database, run_id, project_path=project_path,
                                                  agent=active_agent, now_override=at_dt)
    if status_changed:
        _sync_task_entry(conn, api, run_row, step_rows, definition)

    if run_row["status"] in ("done", "failed", "cancelled", "paused"):
        return {"view": "scenario-next", "run_id": run_id, "run_status": run_row["status"],
                "step": None, "waiting_human": [], "parallel_ready": []}

    by_def = _step_by_id_map(definition)
    input_specs = definition.get("inputs") or {}
    secret_names = {name for name, spec in input_specs.items() if spec["type"] == "secret"}

    # R2-J05 & R3-I13: exact ownership match + consistent agent filter when --agent overrides.
    mine = [
        row for row in step_rows
        if row["status"] == "claimed"
        and row["claimed_by_chat"] == author.by
        and (not active_agent or row["executor_agent"] is None or row["executor_agent"] == active_agent)
    ]
    ready_for_me = [
        row for row in step_rows
        if row["status"] == "ready"
        and row["executor_role"] == "agent"
        and (row["executor_agent"] is None or row["executor_agent"] == active_agent)
    ]
    waiting_human = [row for row in step_rows if row["status"] == "waiting_human"]
    waiting_other_agent = [
        {"key": row["step_key"], "agent": row["executor_agent"]}
        for row in step_rows
        if row["status"] == "ready" and row["executor_agent"] is not None and row["executor_agent"] != active_agent
    ]

    if step_key is not None:
        chosen = next((row for row in step_rows if row["step_key"] == step_key), None)
        if chosen is None:
            raise api.NotFoundError("Unknown step %s in run %s" % (step_key, run_id))
        if chosen["status"] == "claimed" and chosen["claimed_by_chat"] != author.by:
            raise _failure(api, "Step %s is claimed by another chat" % step_key, "conflict")
        if chosen["status"] not in ("ready", "claimed", "waiting_human"):
            raise _failure(api, "Step %s is %s, not actionable" % (step_key, chosen["status"]), "invalid_state")
        if chosen["executor_agent"] and chosen["executor_agent"] != active_agent:
            raise _failure(api, "Step %s is pinned to agent %s" % (step_key, chosen["executor_agent"]), "agent_mismatch")
    else:
        chosen = mine[0] if mine else (ready_for_me[0] if ready_for_me else (waiting_human[0] if waiting_human else None))

    if chosen is None:
        return {
            "view": "scenario-next",
            "run_id": run_id,
            "run_status": run_row["status"],
            "step": None,
            "parallel_ready": [],
            "waiting_human": [],
            "waiting_other_agent": waiting_other_agent,
            "blocked": [{"key": row["step_key"], "reason": row["reason"]} for row in step_rows if row["status"] == "blocked"],
        }

    ctx = _context(run_row, step_rows, definition, project_path=project_path, agent=active_agent,
                   now_dt=at_dt, secrets_runtime=validated_secrets)
    spec = by_def[chosen["step_id"]]
    if spec["type"] == "skill":
        installed = scenarios.installed_skills(active_agent) if installed_skills is _UNSET else installed_skills
    else:
        installed = None
    try:
        rendered = _render_step_payload(chosen, spec, ctx, definition, project_path=project_path,
                                        agent=active_agent, secrets_names=secret_names, installed_skills=installed)
    except templating.Unresolved as exc:
        if exc.path.startswith("inputs.") and exc.path.split(".", 1)[1] in secret_names:
            return {
                "view": "scenario-next",
                "run_id": run_id,
                "run_status": run_row["status"],
                "step": {"key": chosen["step_key"], "id": spec["id"], "type": spec["type"], "status": chosen["status"]},
                "missing_secret": exc.path.split(".", 1)[1],
                "note": "Prefer --secret-env %s=ENV_VAR (or --secret %s=VALUE); secret values are never stored." % (
                    exc.path.split(".", 1)[1], exc.path.split(".", 1)[1]),
            }
        # R2-J02: commit blocked status cleanly instead of raising out of the transaction.
        reason = "unresolved value: " + str(exc)
        updated_run, updated_steps = _block_step_and_commit(
            conn, api, run_row, definition, chosen["step_key"], reason, author, active_agent, at_iso)
        return {
            "view": "scenario-next",
            "run_id": run_id,
            "run_status": updated_run["status"],
            "step": None,
            "blocked_step": {"key": chosen["step_key"], "reason": reason},
            "blocked": [{"key": r["step_key"], "reason": r["reason"]} for r in updated_steps if r["status"] == "blocked"],
            "parallel_ready": [r["step_key"] for r in updated_steps if r["status"] == "ready" and r["executor_role"] == "agent"],
            "waiting_human": [r["step_key"] for r in updated_steps if r["status"] == "waiting_human"],
        }
    except ValueError as exc:
        reason = str(exc)
        updated_run, updated_steps = _block_step_and_commit(
            conn, api, run_row, definition, chosen["step_key"], reason, author, active_agent, at_iso)
        return {
            "view": "scenario-next",
            "run_id": run_id,
            "run_status": updated_run["status"],
            "step": None,
            "blocked_step": {"key": chosen["step_key"], "reason": reason},
            "blocked": [{"key": r["step_key"], "reason": r["reason"]} for r in updated_steps if r["status"] == "blocked"],
            "parallel_ready": [r["step_key"] for r in updated_steps if r["status"] == "ready" and r["executor_role"] == "agent"],
            "waiting_human": [r["step_key"] for r in updated_steps if r["status"] == "waiting_human"],
        }

    if claim and chosen["executor_role"] == "agent":
        lease_until = _format_iso(at_dt + timedelta(seconds=LEASE_SECONDS))
        if chosen["status"] == "ready":
            claim_detail = {"lease_until": lease_until}
            if _step_has_check(spec, "no_dirty_git") and project_path is not None:
                fp_info = claim_fingerprint if isinstance(claim_fingerprint, dict) else scenario_findings.compute_worktree_fingerprint(project_path)
                if fp_info and fp_info.get("fingerprint"):
                    claim_detail["worktree_fingerprint"] = fp_info["fingerprint"]
            conn.execute(
                "UPDATE scenario_steps SET status = 'claimed', claimed_by_chat = ?, claimed_by_agent = ?, "
                "lease_until = ?, stop_continuations = 0, attempts = attempts + 1, started_at = COALESCE(started_at, ?) "
                "WHERE run_id = ? AND step_key = ?",
                (author.by, active_agent, lease_until, at_iso, run_id, chosen["step_key"]),
            )
            conn.execute("UPDATE scenario_runs SET updated_at = ?, revision = revision + 1 WHERE id = ?", (at_iso, run_id))
            _record_event(conn, at_iso, run_row["scenario_id"], run_id, chosen["step_key"],
                          "claimed", author.by, active_agent, claim_detail)
        else:
            conn.execute(
                "UPDATE scenario_steps SET lease_until = ? WHERE run_id = ? AND step_key = ?",
                (lease_until, run_id, chosen["step_key"]),
            )
            conn.execute("UPDATE scenario_runs SET updated_at = ?, revision = revision + 1 WHERE id = ?", (at_iso, run_id))
        rendered["status"] = "claimed"
        rendered["lease_until"] = lease_until

    prefix = ("richi --project-path %s " % shlex.quote(str(project_path))) if project_path else "richi "
    if chosen["executor_role"] == "human":
        rendered["answer_command"] = "%sscenario answer %s %s VALUE" % (prefix, run_id, chosen["step_key"])
    else:
        rendered["report_command"] = "%sscenario report %s %s --json -" % (prefix, run_id, chosen["step_key"])

    parallel_ready = [row["step_key"] for row in ready_for_me if row["step_key"] != chosen["step_key"]]
    return {
        "view": "scenario-next",
        "run_id": run_id,
        "run_status": run_row["status"],
        "step": rendered,
        "parallel_ready": parallel_ready,
        "waiting_human": [row["step_key"] for row in waiting_human if row["step_key"] != chosen["step_key"]],
        "waiting_other_agent": waiting_other_agent,
    }


def claim_step(conn, api, database, run_id, step_key, *, checked_project=None, project_path=None,
               agent=None, author=None, secrets_runtime=None, expected_revision=None,
               installed_skills=_UNSET, claim_fingerprint=None, now_override=None):
    """Claim a specific ready step and return its rendered payload (supporting single-chat parallel fan-out, R2-J11)."""
    require_v4(conn, api)
    author = author or rules.current_author()
    active_agent = agent or author.agent
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)

    run_check = _load_run(conn, run_id, api)
    _check_run_project(run_check, checked_project, api)
    _check_expected_revision(run_check, expected_revision, api)
    definition = json.loads(run_check["definition_json"])
    validated_secrets = _validate_runtime_secrets(secrets_runtime, definition, api)

    run_row, step_rows, status_changed = _advance(conn, api, database, run_id, project_path=project_path,
                                                  agent=active_agent, now_override=at_dt)
    if status_changed:
        _sync_task_entry(conn, api, run_row, step_rows, definition)
    if run_row["status"] in ("paused", "cancelled", "done", "failed"):
        raise _failure(api, "Run %s is %s and cannot claim steps" % (run_id, run_row["status"]), "invalid_state")

    step = next((row for row in step_rows if row["step_key"] == step_key), None)
    if step is None:
        raise api.NotFoundError("Unknown step %s in run %s" % (step_key, run_id))
    if step["executor_role"] != "agent":
        raise _failure(api, "Step %s is performed by %s, not an agent" % (step_key, step["executor_role"]), "invalid_state")
    if step["executor_agent"] and step["executor_agent"] != active_agent:
        raise _failure(api, "Step %s is pinned to agent %s" % (step_key, step["executor_agent"]), "agent_mismatch")
    if step["status"] == "claimed":
        if step["claimed_by_chat"] != author.by:
            raise _failure(api, "Step %s is already claimed by %s" % (step_key, step["claimed_by_chat"] or "another caller"), "conflict")
    elif step["status"] != "ready":
        raise _failure(api, "Step %s is %s, not ready" % (step_key, step["status"]), "invalid_state")

    by_def = _step_by_id_map(definition)
    spec = by_def[step["step_id"]]
    secret_names = {name for name, s in (definition.get("inputs") or {}).items() if s["type"] == "secret"}
    ctx = _context(run_row, step_rows, definition, project_path=project_path, agent=active_agent,
                   now_dt=at_dt, secrets_runtime=validated_secrets)
    if spec["type"] == "skill":
        installed = scenarios.installed_skills(active_agent) if installed_skills is _UNSET else installed_skills
    else:
        installed = None
    try:
        rendered = _render_step_payload(step, spec, ctx, definition, project_path=project_path,
                                        agent=active_agent, secrets_names=secret_names, installed_skills=installed)
    except templating.Unresolved as exc:
        if exc.path.startswith("inputs.") and exc.path.split(".", 1)[1] in secret_names:
            return {
                "status": "missing_secret", "run_id": run_id, "step_key": step_key,
                "missing_secret": exc.path.split(".", 1)[1],
                "note": "Prefer --secret-env %s=ENV_VAR (or --secret %s=VALUE); secret values are never stored." % (
                    exc.path.split(".", 1)[1], exc.path.split(".", 1)[1]),
            }
        reason = "unresolved value: " + str(exc)
        updated_run, _ = _block_step_and_commit(conn, api, run_row, definition, step_key, reason, author, active_agent, at_iso)
        return {"status": "blocked", "run_id": run_id, "step_key": step_key,
                "run_status": updated_run["status"], "reason": reason}
    except ValueError as exc:
        reason = str(exc)
        updated_run, _ = _block_step_and_commit(conn, api, run_row, definition, step_key, reason, author, active_agent, at_iso)
        return {"status": "blocked", "run_id": run_id, "step_key": step_key,
                "run_status": updated_run["status"], "reason": reason}

    lease_until = _format_iso(at_dt + timedelta(seconds=LEASE_SECONDS))
    if step["status"] == "ready":
        claim_detail = {"lease_until": lease_until}
        if _step_has_check(spec, "no_dirty_git") and project_path is not None:
            fp_info = claim_fingerprint if isinstance(claim_fingerprint, dict) else scenario_findings.compute_worktree_fingerprint(project_path)
            if fp_info and fp_info.get("fingerprint"):
                claim_detail["worktree_fingerprint"] = fp_info["fingerprint"]
        conn.execute(
            "UPDATE scenario_steps SET status = 'claimed', claimed_by_chat = ?, claimed_by_agent = ?, "
            "lease_until = ?, stop_continuations = 0, attempts = attempts + 1, started_at = COALESCE(started_at, ?) "
            "WHERE run_id = ? AND step_key = ?",
            (author.by, active_agent, lease_until, at_iso, run_id, step_key),
        )
        conn.execute("UPDATE scenario_runs SET updated_at = ?, revision = revision + 1 WHERE id = ?", (at_iso, run_id))
        _record_event(conn, at_iso, run_row["scenario_id"], run_id, step_key, "claimed", author.by, active_agent,
                      claim_detail)
    else:
        conn.execute("UPDATE scenario_steps SET lease_until = ? WHERE run_id = ? AND step_key = ?",
                     (lease_until, run_id, step_key))
        conn.execute("UPDATE scenario_runs SET updated_at = ?, revision = revision + 1 WHERE id = ?", (at_iso, run_id))

    prefix = ("richi --project-path %s " % shlex.quote(str(project_path))) if project_path else "richi "
    rendered["status"] = "claimed"
    rendered["lease_until"] = lease_until
    rendered["report_command"] = "%sscenario report %s %s --json -" % (prefix, run_id, step_key)
    return {"status": "claimed", "run_id": run_id, "step_key": step_key, "lease_until": lease_until, "step": rendered}


def release_step(conn, api, run_id, step_key, *, checked_project=None, author=None,
                 expected_revision=None, now_override=None):
    require_v4(conn, api)
    author = author or rules.current_author()
    at_iso = _format_iso(_now_dt(api, now_override))
    run_row = _load_run(conn, run_id, api)
    _check_run_project(run_row, checked_project, api)
    _check_expected_revision(run_row, expected_revision, api)
    if run_row["status"] in ("paused", "cancelled", "done", "failed"):
        raise _failure(api, "Run %s is %s and cannot release steps" % (run_id, run_row["status"]), "invalid_state")
    step = conn.execute("SELECT * FROM scenario_steps WHERE run_id = ? AND step_key = ?", (run_id, step_key)).fetchone()
    if step is None:
        raise api.NotFoundError("Unknown step %s in run %s" % (step_key, run_id))
    if step["status"] != "claimed":
        raise _failure(api, "Step %s is %s, not claimed" % (step_key, step["status"]), "invalid_state")
    if step["claimed_by_chat"] != author.by:
        raise _failure(api, "Step %s is claimed by another chat (%s)" % (step_key, step["claimed_by_chat"] or "anonymous"), "conflict")
    conn.execute(
        "UPDATE scenario_steps SET status = 'ready', claimed_by_chat = NULL, claimed_by_agent = NULL, "
        "lease_until = NULL, stop_continuations = 0 WHERE run_id = ? AND step_key = ?",
        (run_id, step_key),
    )
    conn.execute("UPDATE scenario_runs SET updated_at = ?, revision = revision + 1 WHERE id = ?", (at_iso, run_id))
    _record_event(conn, at_iso, run_row["scenario_id"], run_id, step_key, "released", author.by, author.agent, {})
    return {"status": "released", "run_id": run_id, "step_key": step_key}


# ---------------------------------------------------------------- report validation


def _lookup_dotted(data, path):
    value = data
    for segment in path.split("."):
        if isinstance(value, dict) and segment in value:
            value = value[segment]
        else:
            return False, None
    return True, value


def _matches_output_type(value, expected):
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "list":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _verify_file_exists_io(rel_path, project_path):
    """Read-only filesystem verification for file_exists (symlink-safe inside project_path)."""
    if rel_path.startswith("/") or "\\" in rel_path or re.match(r"[A-Za-z]:", rel_path) or any(
            seg in ("", ".", "..") for seg in rel_path.split("/")):
        return "check file_exists failed: invalid relative path " + rel_path
    if project_path is None:
        return "check file_exists requires --project-path to verify " + rel_path
    root = Path(project_path).resolve()
    resolved = (root / rel_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return "check file_exists failed: path escapes project root: " + rel_path
    if not resolved.is_file():
        return "check file_exists failed: file not found: " + rel_path
    return None


def _verify_git_evidence_io(evidence, project_path):
    """Hardened read-only Git verification for ALL git evidence items using git_evidence._git (R2-J03, R3-I7)."""
    git_items = [item for item in evidence if isinstance(item, dict) and item.get("type") == "git"]
    if not git_items:
        return ["check evidence_git failed: report.evidence needs a git revision"]
    if len(git_items) > MAX_GIT_EVIDENCE:
        return ["check evidence_git failed: at most %d git evidence items allowed (got %d)" % (
            MAX_GIT_EVIDENCE, len(git_items))]
    if project_path is None:
        return ["check evidence_git requires --project-path to verify git revision(s)"]
    reasons = []
    valid_revs = []
    seen = set()
    for index, item in enumerate(git_items):
        raw_rev = item.get("revision")
        if not isinstance(raw_rev, str) or not raw_rev.strip():
            reasons.append("check evidence_git failed: evidence[%d].revision must be nonempty text" % index)
            continue
        rev = raw_rev.strip()
        if "\x00" in rev or rev.startswith("-") or not _GIT_REV.fullmatch(rev):
            reasons.append("check evidence_git failed: invalid git revision syntax: %r" % rev[:80])
            continue
        if rev not in seen:
            seen.add(rev)
            valid_revs.append(rev)
    if valid_revs:
        with git_evidence.operation_deadline(time.monotonic() + git_evidence.TIMEOUT):
            for rev in valid_revs:
                try:
                    _out, returncode = git_evidence._git(
                        Path(project_path),
                        ["rev-parse", "--verify", "--quiet", "--end-of-options", rev + "^{commit}"],
                        allowed=(0, 1),
                    )
                except (git_evidence.GitEvidenceError, OSError) as exc:
                    reasons.append("check evidence_git failed: " + str(exc))
                    continue
                if returncode != 0:
                    reasons.append("check evidence_git failed: revision not found in repository: " + rev)
    return reasons


def prepare_report_preflight(conn, api, run_id, step_key, report, *, checked_project=None, project_path=None,
                             author=None, now_dt=None):
    """Load DB context for read-only report checks inside a brief read transaction (R2-J03, R3-I2, R3-C2)."""
    if not scenario_registry.has_scenario_tables(conn):
        return None
    if not isinstance(report, dict) or report.get("status") != "done":
        return None
    row = conn.execute("SELECT * FROM scenario_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    run_row = dict(row)
    if run_row["status"] in ("paused", "cancelled", "done", "failed"):
        return None
    if checked_project is not None and run_row["project_id"] != checked_project:
        return None
    definition = json.loads(run_row["definition_json"])
    step_rows = _load_steps(conn, run_id)
    step = next((r for r in step_rows if r["step_key"] == step_key), None)
    if step is None or step["status"] != "claimed":
        return None
    at_dt = now_dt if now_dt is not None else _now_dt(api)
    if author is not None and step["claimed_by_chat"] != author.by:
        return None
    if step["lease_until"] and _parse_iso(step["lease_until"]) <= at_dt:
        return None
    spec = _step_by_id_map(definition).get(step["step_id"])
    if spec is None:
        return None
    checks = spec.get("checks") or []
    if not any(c["kind"] in ("file_exists", "evidence_git", "no_dirty_git", "findings_grounded") for c in checks):
        return None
    ctx = _context(run_row, step_rows, definition, project_path=project_path,
                   agent=author.agent if author is not None else None, now_dt=at_dt)
    step_ctx = dict(ctx)
    if step["matrix_json"]:
        step_ctx["matrix"] = json.loads(step["matrix_json"])
    return {
        "project_path": str(project_path) if project_path is not None else None,
        "checks": checks,
        "step_ctx": step_ctx,
        "evidence": report.get("evidence") if isinstance(report.get("evidence"), list) else [],
        "data": report.get("data") if isinstance(report.get("data"), dict) else {},
    }


def run_report_preflight_io(prepared):
    """Run read-only filesystem/git report checks completely outside any SQLite transaction (R2-J03, R3-I2, R3-A3)."""
    if prepared is None:
        return None
    project_path = prepared["project_path"]
    step_ctx = prepared["step_ctx"]
    evidence = prepared["evidence"]
    data = prepared.get("data") if isinstance(prepared.get("data"), dict) else {}
    io_results = {
        "project_path": project_path,
        "file_exists": {},
        "evidence": evidence,
        "evidence_git": None,
        "worktree": None,
        "data": data,
        "findings_grounded": {},
    }
    for check in prepared["checks"]:
        if check["kind"] == "file_exists":
            try:
                rel_path = _render_field(check["path"], step_ctx, line=True,
                                         limit=scenario_format.MAX_PROJECT_PATH, field="check.file_exists", empty=False)
            except (templating.Unresolved, ValueError):
                continue
            if rel_path not in io_results["file_exists"]:
                io_results["file_exists"][rel_path] = _verify_file_exists_io(rel_path, project_path)
        elif check["kind"] == "evidence_git" and io_results["evidence_git"] is None:
            if len(evidence) <= MAX_EVIDENCE_ITEMS:
                io_results["evidence_git"] = _verify_git_evidence_io(evidence, project_path)
        elif check["kind"] == "no_dirty_git" and io_results["worktree"] is None:
            io_results["worktree"] = scenario_findings.compute_worktree_fingerprint(project_path)
        elif check["kind"] == "findings_grounded":
            path = check.get("path") or "findings"
            if path not in io_results["findings_grounded"]:
                found, val = _lookup_dotted(data, path)
                if not found:
                    io_results["findings_grounded"][path] = [f"check findings_grounded failed: missing data.{path}"]
                else:
                    io_results["findings_grounded"][path] = scenario_findings.verify_findings_grounded_io(
                        val, project_path, path_label=path
                    )
    return io_results


def preflight_report_checks(conn, api, run_id, step_key, report, *, checked_project=None, project_path=None,
                            author=None, now_dt=None):
    """Convenience wrapper combining prepare_report_preflight and run_report_preflight_io."""
    return run_report_preflight_io(
        prepare_report_preflight(conn, api, run_id, step_key, report,
                                 checked_project=checked_project, project_path=project_path,
                                 author=author, now_dt=now_dt)
    )


def _validate_report(report, step_row, step_spec, ctx, project_path, preflight_io=None, claimed_fingerprint=None):
    """Return list of failure reasons (empty means report is accepted)."""
    reasons = []
    if not isinstance(report, dict):
        return ["report must be a JSON object"]
    status = report.get("status")
    if status not in ("done", "failed"):
        return ["report.status must be 'done' or 'failed'"]
    try:
        encoded = json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return ["report must be valid finite JSON"]
    if len(encoded) > MAX_REPORT_BYTES:
        return ["report is larger than %d bytes" % MAX_REPORT_BYTES]

    summary = report.get("summary")
    if status == "done" and (not isinstance(summary, str) or not summary.strip()):
        reasons.append("report.summary must be nonempty text when status is done")
    elif summary is not None and not isinstance(summary, str):
        reasons.append("report.summary must be text")
    elif isinstance(summary, str) and "\x00" in summary:
        reasons.append("report.summary must not contain NUL bytes")

    # R2-J17 & R2-J24: validate reason BEFORE the failed early return.
    reason_val = report.get("reason")
    if reason_val is not None:
        if not isinstance(reason_val, str):
            reasons.append("report.reason must be text")
        elif "\x00" in reason_val:
            reasons.append("report.reason must not contain NUL bytes")
        elif len(reason_val) > MAX_REASON:
            reasons.append("report.reason is longer than %d characters" % MAX_REASON)

    data = report.get("data")
    if data is not None and not isinstance(data, dict):
        reasons.append("report.data must be a JSON object")
        data = {}
    else:
        data = data or {}
        if data:
            try:
                scenario_format._shallow(data, "report.data")
            except scenario_format.FormatError as exc:
                reasons.append(str(exc))

    files = report.get("files")
    if files is not None and (not isinstance(files, list) or not all(isinstance(item, str) and "\x00" not in item for item in files)):
        reasons.append("report.files must be a list of paths without NUL bytes")
        files = []
    files = files or []

    artifacts = report.get("artifacts")
    if artifacts is not None:
        if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACTS:
            reasons.append("report.artifacts must be a list of at most %d richi://artifact/... URIs" % MAX_ARTIFACTS)
        elif not all(_valid_artifact_item(item) for item in artifacts):
            reasons.append("report.artifacts entries must match richi://artifact/<id>@<version_uid>/<path> without traversal")

    evidence = report.get("evidence")
    if evidence is not None and (not isinstance(evidence, list) or not all(isinstance(item, dict) for item in evidence)):
        reasons.append("report.evidence must be a list of objects")
        evidence = []
    else:
        evidence = evidence or []
        if len(evidence) > MAX_EVIDENCE_ITEMS:
            reasons.append("report.evidence may contain at most %d items" % MAX_EVIDENCE_ITEMS)
        elif evidence:
            try:
                scenario_format._shallow(evidence, "report.evidence")
            except scenario_format.FormatError as exc:
                reasons.append(str(exc))

    raw_exit = report.get("exit_code")
    if raw_exit is not None and (isinstance(raw_exit, bool) or not isinstance(raw_exit, int) or not (-255 <= raw_exit <= 65535)):
        reasons.append("report.exit_code must be an integer between -255 and 65535")

    if status == "failed":
        return reasons

    outputs = step_spec.get("outputs") or {}
    for field, spec in (outputs.get("data") or {}).items():
        if field not in data:
            if spec.get("required"):
                reasons.append("missing required data field: " + field)
        elif not _matches_output_type(data[field], spec["type"]):
            reasons.append("data.%s must be %s" % (field, spec["type"]))
    if (outputs.get("files") or {}).get("required") and not files:
        reasons.append("report.files must list at least one file")

    exit_code = report.get("exit_code")
    if exit_code is None and "exit_code" in data:
        exit_code = data["exit_code"]
    explicit_exit_checks = {c["expect"] for c in (step_spec.get("checks") or []) if c["kind"] == "exit_code"}
    if step_spec["type"] == "command" and "expect_exit" in (step_spec.get("params") or {}):
        expected = step_spec["params"]["expect_exit"]
        if expected not in explicit_exit_checks:
            if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code != expected:
                reasons.append("exit_code must be %d (got %r)" % (expected, exit_code))

    step_ctx = dict(ctx)
    if step_row["matrix_json"]:
        step_ctx["matrix"] = json.loads(step_row["matrix_json"])
    expected_root = str(project_path) if project_path is not None else None
    preflight_matches_root = (
        preflight_io is not None and preflight_io.get("project_path") == expected_root
    )

    for check in step_spec.get("checks") or []:
        kind = check["kind"]
        if kind == "data_required":
            found, val = _lookup_dotted(data, check["path"])
            if not found or val is None:
                reasons.append("check data_required failed: missing data." + check["path"])
        elif kind == "min_items":
            found, val = _lookup_dotted(data, check["path"])
            if not found or not isinstance(val, list) or len(val) < check["count"]:
                reasons.append("check min_items failed: data.%s needs at least %d item(s)" % (
                    check["path"], check["count"]))
        elif kind == "exit_code":
            if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code != check["expect"]:
                reasons.append("check exit_code failed: expected %d, got %r" % (check["expect"], exit_code))
        elif kind == "file_exists":
            try:
                rel_path = _render_field(check["path"], step_ctx, line=True,
                                         limit=scenario_format.MAX_PROJECT_PATH, field="check.file_exists", empty=False)
            except (templating.Unresolved, ValueError) as exc:
                reasons.append("check file_exists failed: " + str(exc))
            else:
                if preflight_matches_root and rel_path in (preflight_io.get("file_exists") or {}):
                    err = preflight_io["file_exists"][rel_path]
                else:
                    err = _verify_file_exists_io(rel_path, project_path)
                if err:
                    reasons.append(err)
        elif kind == "evidence_git":
            if (preflight_matches_root and preflight_io.get("evidence_git") is not None
                    and preflight_io.get("evidence") == evidence):
                reasons.extend(preflight_io["evidence_git"])
            else:
                reasons.extend(_verify_git_evidence_io(evidence, project_path))
        elif kind == "no_dirty_git":
            mode = check.get("mode") or "unchanged"
            if project_path is None:
                reasons.append("check no_dirty_git requires --project-path to verify repository worktree")
                continue
            wt = preflight_io.get("worktree") if (preflight_matches_root and preflight_io.get("worktree") is not None) else None
            if wt is None:
                wt = scenario_findings.compute_worktree_fingerprint(project_path)
            if wt.get("error"):
                reasons.append("check no_dirty_git failed: " + wt["error"])
            elif mode == "clean":
                if not wt.get("is_clean"):
                    reasons.append("check no_dirty_git (clean) failed: repository worktree has uncommitted changes")
            else:
                if claimed_fingerprint is not None and wt.get("fingerprint") != claimed_fingerprint:
                    reasons.append("check no_dirty_git (unchanged) failed: repository worktree changed during read-only step")
        elif kind == "findings_grounded":
            path = check.get("path") or "findings"
            if (preflight_matches_root and path in (preflight_io.get("findings_grounded") or {})
                    and preflight_io.get("data") == data):
                reasons.extend(preflight_io["findings_grounded"][path])
            else:
                found, val = _lookup_dotted(data, path)
                if not found:
                    reasons.append(f"check findings_grounded failed: missing data.{path}")
                else:
                    reasons.extend(scenario_findings.verify_findings_grounded_io(val, project_path, path_label=path))
    return reasons


def report_step(conn, api, database, run_id, step_key, report, *, checked_project=None, project_path=None,
                author=None, expected_revision=None, preflight_io=None, now_override=None):
    """Validate and record an agent's report for a claimed step."""
    require_v4(conn, api)
    author = author or rules.current_author()
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)
    run_row = _load_run(conn, run_id, api)
    _check_run_project(run_row, checked_project, api)
    _check_expected_revision(run_row, expected_revision, api)
    if run_row["status"] in ("paused", "cancelled", "done", "failed"):
        raise _failure(api, "Run %s is %s and cannot accept reports" % (run_id, run_row["status"]), "invalid_state")
    definition = json.loads(run_row["definition_json"])
    by_def = _step_by_id_map(definition)
    step_rows = _load_steps(conn, run_id)
    # R2-J05: expire stale leases before accepting/rejecting a report.
    if _expire_leases(conn, run_row, step_rows, at_dt, at_iso):
        step_rows = _load_steps(conn, run_id)
    step = next((row for row in step_rows if row["step_key"] == step_key), None)
    if step is None:
        raise api.NotFoundError("Unknown step %s in run %s" % (step_key, run_id))
    if step["status"] != "claimed":
        raise _failure(api, "Step %s is %s; claim it first with scenario next --claim" % (step_key, step["status"]), "invalid_state")
    if step["claimed_by_chat"] != author.by:
        raise _failure(api, "Step %s is claimed by another chat (%s)" % (step_key, step["claimed_by_chat"] or "anonymous"), "conflict")

    spec = by_def[step["step_id"]]
    ctx = _context(run_row, step_rows, definition, project_path=project_path, agent=author.agent, now_dt=at_dt)
    claimed_fp = _latest_claimed_fingerprint(conn, run_id, step_key) if _step_has_check(spec, "no_dirty_git") else None
    reasons = _validate_report(report, step, spec, ctx, project_path, preflight_io=preflight_io,
                               claimed_fingerprint=claimed_fp)
    if reasons:
        lease_until = _format_iso(at_dt + timedelta(seconds=LEASE_SECONDS))
        conn.execute(
            "UPDATE scenario_steps SET rejections = rejections + 1, lease_until = ? WHERE run_id = ? AND step_key = ?",
            (lease_until, run_id, step_key),
        )
        conn.execute("UPDATE scenario_runs SET updated_at = ?, revision = revision + 1 WHERE id = ?", (at_iso, run_id))
        _record_event(conn, at_iso, run_row["scenario_id"], run_id, step_key,
                      "report_rejected", author.by, author.agent, {"reasons": reasons})
        return {"status": "rejected", "run_id": run_id, "step_key": step_key,
                "step_status": "claimed", "lease_until": lease_until, "reasons": reasons}

    stored = {
        "status": report["status"],
        "summary": report.get("summary") or "",
        "data": report.get("data") or {},
        "files": report.get("files") or [],
        "evidence": report.get("evidence") or [],
        "agent": author.agent,
        "chat": author.by,
    }
    if report.get("artifacts"):
        stored["artifacts"] = list(report["artifacts"])
    raw_exit = report.get("exit_code")
    if raw_exit is not None and not isinstance(raw_exit, bool) and isinstance(raw_exit, int):
        stored["exit_code"] = raw_exit
    elif "exit_code" in stored["data"]:
        data_exit = stored["data"]["exit_code"]
        if not isinstance(data_exit, bool) and isinstance(data_exit, int) and -255 <= data_exit <= 65535:
            stored["exit_code"] = data_exit

    new_step_status = report["status"]
    conn.execute(
        "UPDATE scenario_steps SET status = ?, claimed_by_chat = NULL, claimed_by_agent = NULL, lease_until = NULL, "
        "stop_continuations = 0, result_json = ?, reason = ?, finished_at = ? WHERE run_id = ? AND step_key = ?",
        (new_step_status, json.dumps(stored, ensure_ascii=False, separators=(",", ":")),
         report.get("reason") if new_step_status == "failed" else None, at_iso, run_id, step_key),
    )
    event_detail = {"summary": stored["summary"][:300]}
    if new_step_status == "failed" and report.get("reason"):
        event_detail["reason"] = str(report["reason"])[:500]
    if "exit_code" in stored:
        event_detail["exit_code"] = stored["exit_code"]
    _record_event(conn, at_iso, run_row["scenario_id"], run_id, step_key,
                  "report_accepted" if new_step_status == "done" else "step_failed",
                  author.by, author.agent, event_detail)

    run_row, step_rows, status_changed = _advance(conn, api, database, run_id, project_path=project_path,
                                                  agent=author.agent, now_override=at_dt, force_bump=True)
    if status_changed:
        _sync_task_entry(conn, api, run_row, step_rows, definition)
    return {"status": "accepted", "run_id": run_id, "step_key": step_key,
            "step_status": new_step_status, "run_status": run_row["status"]}


def _parse_checklist_answer(value, items, ctx, api):
    """Parse checklist answer into canonical 0-based indices, rejecting ambiguous texts and enforcing required items."""
    if value in ("done", "all", "ok"):
        return list(range(len(items)))
    rendered_texts = {}
    for idx, item in enumerate(items):
        texts = {item["text"]}
        try:
            texts.add(_render_field(item["text"], ctx, line=True, limit=1000, field="item"))
        except (templating.Unresolved, ValueError):
            pass
        for text in texts:
            rendered_texts.setdefault(text, []).append(idx)

    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        raise _failure(api, "Checklist answer must list checked items or 'all'/'done'", "invalid_answer")
    selected = set()
    for part in parts:
        if part.isdigit():
            idx = int(part)
            if not 0 <= idx < len(items):
                raise _failure(api, "Checklist index out of range: " + part, "invalid_answer")
            selected.add(idx)
        elif part in rendered_texts:
            matches = rendered_texts[part]
            if len(matches) > 1:
                raise _failure(
                    api,
                    "Ambiguous checklist item %r (matches indices %s); answer with 0-based indices instead"
                    % (part, ", ".join(str(i) for i in matches)),
                    "invalid_answer",
                )
            selected.add(matches[0])
        else:
            raise _failure(api, "Unknown checklist item: " + part, "invalid_answer")
    missing_required = [idx for idx, item in enumerate(items) if item.get("required") and idx not in selected]
    if missing_required:
        raise _failure(api, "Required checklist item(s) not checked: " + ", ".join(str(i) for i in missing_required),
                       "invalid_answer")
    return sorted(selected)


def answer_step(conn, api, database, run_id, step_key, value, *, comment=None, checked_project=None,
                project_path=None, author=None, expected_revision=None, now_override=None):
    """Record the human's answer to a waiting gate or checklist step (always from the chat)."""
    require_v4(conn, api)
    _no_nul(value, "answer value", api, limit=4000)
    _no_nul(comment, "comment", api, limit=4000)
    author = author or rules.current_author()
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)
    run_row = _load_run(conn, run_id, api)
    _check_run_project(run_row, checked_project, api)
    _check_expected_revision(run_row, expected_revision, api)
    if run_row["status"] in ("paused", "cancelled", "done", "failed"):
        raise _failure(api, "Run %s is %s and cannot accept answers" % (run_id, run_row["status"]), "invalid_state")
    definition = json.loads(run_row["definition_json"])
    by_def = _step_by_id_map(definition)
    step_rows = _load_steps(conn, run_id)
    step = next((row for row in step_rows if row["step_key"] == step_key), None)
    if step is None:
        raise api.NotFoundError("Unknown step %s in run %s" % (step_key, run_id))
    if step["status"] != "waiting_human":
        raise _failure(api, "Step %s is %s, not waiting for a human answer" % (step_key, step["status"]), "invalid_state")

    spec = by_def[step["step_id"]]
    params = spec.get("params") or {}
    if spec["type"] == "gate":
        if not value.strip():
            raise _failure(api, "Gate answer must be nonempty text", "invalid_answer")
        options = params.get("options")
        if options and value not in options:
            raise _failure(api, "Answer must be one of: " + ", ".join(options), "invalid_answer")
        if params.get("allow_comment") is False and comment:
            raise _failure(api, "This gate does not allow comments", "invalid_answer")
        stored = {"status": "done", "answer": value, "comment": comment or "",
                  "summary": value + (": " + comment if comment else "")}
    else:
        ctx = _context(run_row, step_rows, definition, project_path=project_path, agent=author.agent, now_dt=at_dt)
        step_ctx = dict(ctx)
        if step["matrix_json"]:
            step_ctx["matrix"] = json.loads(step["matrix_json"])
        checked = _parse_checklist_answer(value, params["items"], step_ctx, api)
        stored = {"status": "done", "checked": checked, "summary": "checked %d/%d" % (len(checked), len(params["items"]))}

    conn.execute(
        "UPDATE scenario_steps SET status = 'done', result_json = ?, finished_at = ? WHERE run_id = ? AND step_key = ?",
        (json.dumps(stored, ensure_ascii=False, separators=(",", ":")), at_iso, run_id, step_key),
    )
    event_detail = {"value": value}
    if comment is not None:
        event_detail["comment"] = comment
    _record_event(conn, at_iso, run_row["scenario_id"], run_id, step_key,
                  "answered", author.by, author.agent, event_detail)

    run_row, step_rows, status_changed = _advance(conn, api, database, run_id, project_path=project_path,
                                                  agent=author.agent, now_override=at_dt, force_bump=True)
    if status_changed:
        _sync_task_entry(conn, api, run_row, step_rows, definition)
    return {"status": "answered", "run_id": run_id, "step_key": step_key, "run_status": run_row["status"]}


def _has_other_blocking_failure(step_rows, by_def, exclude_key):
    return any(
        row["step_key"] != exclude_key
        and row["status"] == "failed"
        and not by_def[row["step_id"]].get("continue_on_failure")
        for row in step_rows
    )


def skip_step(conn, api, database, run_id, step_key, reason, *, checked_project=None, project_path=None,
              author=None, expected_revision=None, now_override=None):
    require_v4(conn, api)
    _no_nul(reason, "reason", api, limit=MAX_REASON)
    if not isinstance(reason, str) or not reason.strip():
        raise _failure(api, "skip requires a nonempty --reason", "usage_error")
    author = author or rules.current_author()
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)
    run_row = _load_run(conn, run_id, api)
    _check_run_project(run_row, checked_project, api)
    _check_expected_revision(run_row, expected_revision, api)
    if run_row["status"] in ("paused", "cancelled", "done"):
        raise _failure(api, "Run %s is %s and cannot skip steps" % (run_id, run_row["status"]), "invalid_state")
    step_rows = _load_steps(conn, run_id)
    if _expire_leases(conn, run_row, step_rows, at_dt, at_iso):
        step_rows = _load_steps(conn, run_id)
    step = next((row for row in step_rows if row["step_key"] == step_key), None)
    if step is None:
        raise api.NotFoundError("Unknown step %s in run %s" % (step_key, run_id))
    if step["status"] in ("pending", "done", "skipped"):
        raise _failure(api, "Step %s is %s and cannot be skipped" % (step_key, step["status"]), "invalid_state")
    if step["status"] == "claimed" and step["claimed_by_chat"] != author.by:
        raise _failure(api, "Step %s is claimed by another chat (%s)" % (step_key, step["claimed_by_chat"] or "anonymous"), "conflict")

    definition = json.loads(run_row["definition_json"])
    by_def = _step_by_id_map(definition)
    previous_status = run_row["status"]
    conn.execute(
        "UPDATE scenario_steps SET status = 'skipped', claimed_by_chat = NULL, claimed_by_agent = NULL, "
        "lease_until = NULL, stop_continuations = 0, reason = ?, finished_at = ? WHERE run_id = ? AND step_key = ?",
        (reason.strip(), at_iso, run_id, step_key),
    )
    _record_event(conn, at_iso, run_row["scenario_id"], run_id, step_key,
                  "step_skipped", author.by, author.agent, {"reason": reason.strip()})
    if previous_status == "failed" and not _has_other_blocking_failure(step_rows, by_def, step_key):
        conn.execute("UPDATE scenario_runs SET status = 'running' WHERE id = ?", (run_id,))
    run_row, step_rows, status_changed = _advance(
        conn, api, database, run_id, project_path=project_path, agent=author.agent,
        now_override=at_dt, force_bump=True, previous_status=previous_status,
    )
    if status_changed:
        _sync_task_entry(conn, api, run_row, step_rows, definition)
    return {"status": "skipped", "run_id": run_id, "step_key": step_key, "run_status": run_row["status"]}


def retry_step(conn, api, database, run_id, step_key=None, *, checked_project=None, project_path=None,
               author=None, expected_revision=None, now_override=None):
    """Retry a failed or blocked step, enforcing retry.max and downstream invariants (R2-J13, R3-S3, R3-C1)."""
    require_v4(conn, api)
    author = author or rules.current_author()
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)
    run_row = _load_run(conn, run_id, api)
    _check_run_project(run_row, checked_project, api)
    _check_expected_revision(run_row, expected_revision, api)
    if run_row["status"] in ("paused", "cancelled", "done"):
        raise _failure(api, "Run %s is %s and cannot be retried" % (run_id, run_row["status"]), "invalid_state")
    step_rows = _load_steps(conn, run_id)
    if step_key is None:
        if run_row["status"] == "failed":
            target = next((row for row in step_rows if row["status"] == "failed"), None) or next(
                (row for row in step_rows if row["status"] == "blocked"), None)
        else:
            target = next((row for row in step_rows if row["status"] in ("failed", "blocked")), None)
        if target is None:
            raise _failure(api, "No failed or blocked step to retry in run " + run_id, "invalid_state")
    else:
        target = next((row for row in step_rows if row["step_key"] == step_key), None)
        if target is None:
            raise api.NotFoundError("Unknown step %s in run %s" % (step_key, run_id))
        if target["status"] not in ("failed", "blocked"):
            raise _failure(api, "Step %s is %s, not failed or blocked" % (step_key, target["status"]), "invalid_state")

    definition = json.loads(run_row["definition_json"])
    by_def = _step_by_id_map(definition)
    spec = by_def[target["step_id"]]
    # R3-S3 / R3-I9: refuse retrying a failed step once any downstream dependent has already left pending.
    if target["status"] == "failed" or spec.get("continue_on_failure"):
        ancestry = scenario_format.ancestors(definition)
        for row in step_rows:
            if target["step_id"] in ancestry.get(row["step_id"], set()) and row["status"] != "pending":
                raise _failure(
                    api,
                    "Cannot retry step %s because downstream step %s is already %s"
                    % (target["step_key"], row["step_key"], row["status"]),
                    "invalid_state",
                )
    retry_cfg = spec.get("retry")
    if retry_cfg is not None and target["status"] == "failed":
        max_attempts = 1 + retry_cfg["max"]
        if target["attempts"] >= max_attempts:
            raise _failure(api, "Step %s reached its retry limit (%d retries)" % (
                target["step_key"], retry_cfg["max"]), "retry_limit")

    previous_status = run_row["status"]
    reset_status = "pending" if target["status"] == "blocked" and target["matrix_index"] is None else "ready"
    if spec["type"] in ("gate", "checklist") and reset_status == "ready":
        reset_status = "waiting_human"
    conn.execute(
        "UPDATE scenario_steps SET status = ?, claimed_by_chat = NULL, claimed_by_agent = NULL, lease_until = NULL, "
        "stop_continuations = 0, result_json = NULL, reason = NULL, finished_at = NULL WHERE run_id = ? AND step_key = ?",
        (reset_status, run_id, target["step_key"]),
    )
    if previous_status == "failed" and not _has_other_blocking_failure(step_rows, by_def, target["step_key"]):
        conn.execute("UPDATE scenario_runs SET status = 'running' WHERE id = ?", (run_id,))
    _record_event(conn, at_iso, run_row["scenario_id"], run_id, target["step_key"],
                  "retried", author.by, author.agent, {})
    run_row, step_rows, status_changed = _advance(
        conn, api, database, run_id, project_path=project_path, agent=author.agent,
        now_override=at_dt, force_bump=True, previous_status=previous_status,
    )
    if status_changed:
        _sync_task_entry(conn, api, run_row, step_rows, definition)
    return {"status": "retried", "run_id": run_id, "step_key": target["step_key"], "run_status": run_row["status"]}


def set_run_state(conn, api, database, run_id, action, *, checked_project=None, project_path=None,
                  author=None, expected_revision=None, now_override=None):
    """pause, resume or cancel a run."""
    require_v4(conn, api)
    author = author or rules.current_author()
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)
    run_row = _load_run(conn, run_id, api)
    _check_run_project(run_row, checked_project, api)
    _check_expected_revision(run_row, expected_revision, api)
    definition = json.loads(run_row["definition_json"])

    if action == "pause":
        if run_row["status"] not in ("running", "waiting"):
            raise _failure(api, "Cannot pause run in status " + run_row["status"], "invalid_state")
        conn.execute("UPDATE scenario_runs SET status = 'paused', updated_at = ?, revision = revision + 1 WHERE id = ?",
                     (at_iso, run_id))
        _record_event(conn, at_iso, run_row["scenario_id"], run_id, None, "paused", author.by, author.agent, {})
    elif action == "resume":
        if run_row["status"] != "paused":
            raise _failure(api, "Cannot resume run in status " + run_row["status"], "invalid_state")
        conn.execute("UPDATE scenario_runs SET status = 'running', updated_at = ?, revision = revision + 1 WHERE id = ?",
                     (at_iso, run_id))
        _record_event(conn, at_iso, run_row["scenario_id"], run_id, None, "resumed", author.by, author.agent, {})
        run_row, step_rows, _ = _advance(conn, api, database, run_id, project_path=project_path,
                                         agent=author.agent, now_override=at_dt)
        _sync_task_entry(conn, api, run_row, step_rows, definition)
        return _format_status(run_row, step_rows, definition, view="scenario-run")
    elif action == "cancel":
        if run_row["status"] in ("done", "cancelled"):
            raise _failure(api, "Run %s is already %s" % (run_id, run_row["status"]), "invalid_state")
        conn.execute("UPDATE scenario_runs SET status = 'cancelled', updated_at = ?, revision = revision + 1 WHERE id = ?",
                     (at_iso, run_id))
        _release_all_claims(conn, run_id)
        _record_event(conn, at_iso, run_row["scenario_id"], run_id, None, "cancelled", author.by, author.agent, {})
    else:
        raise ValueError("unknown action " + action)

    run_row = _load_run(conn, run_id, api)
    step_rows = _load_steps(conn, run_id)
    _sync_task_entry(conn, api, run_row, step_rows, definition)
    return _format_status(run_row, step_rows, definition, view="scenario-run")


def _format_status(run_row, step_rows, definition, *, view="scenario-status", is_baseline=False, run_meta=None):
    steps = []
    for row in step_rows:
        item = {
            "key": row["step_key"],
            "id": row["step_id"],
            "type": row["type"],
            "status": row["status"],
            "executor": {"role": row["executor_role"], "agent": row["executor_agent"]},
            "attempts": row["attempts"],
            "rejections": row["rejections"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }
        if row["matrix_json"]:
            item["matrix"] = json.loads(row["matrix_json"])
        if row["claimed_by_chat"] is not None or row["status"] == "claimed":
            item["claimed_by_chat"] = row["claimed_by_chat"]
            item["lease_until"] = row["lease_until"]
        if row["reason"]:
            item["reason"] = row["reason"]
        if row["result_json"]:
            parsed = json.loads(row["result_json"])
            item["summary"] = parsed.get("summary", "")
            if "answer" in parsed:
                item["answer"] = parsed["answer"]
            if parsed.get("artifacts"):
                item["artifacts"] = parsed["artifacts"]
        steps.append(item)
    meta = run_meta if isinstance(run_meta, dict) else {}
    return {
        "view": view,
        "run_id": run_row["id"],
        "scenario_id": run_row["scenario_id"],
        "scenario_version": run_row["scenario_version"],
        "scope": run_row["scope"],
        "project_id": run_row["project_id"],
        "status": run_row["status"],
        "run_kind": meta.get("kind", "prod"),
        "is_baseline": bool(is_baseline),
        "task_entry_id": run_row["task_entry_id"],
        "inputs": json.loads(run_row["inputs_json"]),
        "created_by_chat": run_row["created_by_chat"],
        "created_by_agent": run_row["created_by_agent"],
        "created_at": run_row["created_at"],
        "updated_at": run_row["updated_at"],
        "revision": run_row["revision"],
        "steps": steps,
    }


def run_status(conn, api, run_id, *, checked_project=None):
    require_v4(conn, api)
    run_row = _load_run(conn, run_id, api)
    _check_run_project(run_row, checked_project, api)
    step_rows = _load_steps(conn, run_id)
    definition = json.loads(run_row["definition_json"])
    is_base = get_baseline_run_id(conn, run_row["scenario_id"], run_row["project_id"]) == run_id
    return _format_status(run_row, step_rows, definition, is_baseline=is_base, run_meta=get_run_meta(conn, run_id))


def list_runs(conn, api, *, active=False, chat=None, project_id=None, scenario_id=None, limit=20):
    require_v4(conn, api)
    if project_id is not None:
        api.require_project(conn, project_id)
    clauses, params = [], []
    if active:
        clauses.append("status IN ('running', 'waiting', 'paused')")
    if chat is not None:
        clauses.append("(created_by_chat = ? OR id IN (SELECT run_id FROM scenario_steps WHERE claimed_by_chat = ?))")
        params.extend([chat, chat])
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if scenario_id is not None:
        clauses.append("scenario_id = ?")
        params.append(scenario_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        "SELECT id, scenario_id, scope, project_id, scenario_version, status, task_entry_id, "
        "created_by_chat, created_by_agent, created_at, updated_at, revision FROM scenario_runs"
        + where + " ORDER BY updated_at DESC, id DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    baselines = _all_baseline_ids(conn)
    meta_map = all_run_meta(conn)
    return {
        "view": "scenario-runs",
        "runs": [
            dict(
                row,
                is_baseline=row["id"] in baselines,
                run_kind=(meta_map.get(row["id"]) or {}).get("kind", "prod"),
            )
            for row in rows
        ],
    }


def _duration_sec(start_iso, end_iso):
    if not start_iso or not end_iso:
        return None
    try:
        dt = (_parse_iso(end_iso) - _parse_iso(start_iso)).total_seconds()
        return round(max(0.0, dt), 2)
    except (ValueError, TypeError):
        return None


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return round(float(ordered[mid]), 2)
    return round((float(ordered[mid - 1]) + float(ordered[mid])) / 2.0, 2)


def scenario_stats(conn, api, *, scenario_id=None, project_id=None, version=None, last_n=None, run_kind="prod"):
    """Aggregate execution metrics across runs, scenarios, and steps (SQL JOIN, p50, run_kind isolation)."""
    schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
    empty = {
        "view": "scenario-stats",
        "schema_version": schema_version,
        "run_kind": run_kind,
        "total_runs": 0,
        "by_status": {},
        "completion_rate": None,
        "abort_rate": None,
        "first_pass_rate": None,
        "agent_active_p50_sec": None,
        "human_wait_p50_sec": None,
        "per_scenario": [],
        "per_step": [],
    }
    if not scenario_registry.has_scenario_tables(conn):
        return empty
    if project_id is not None:
        api.require_project(conn, project_id)
    clauses, params = [], []
    if scenario_id is not None:
        clauses.append("r.scenario_id = ?")
        params.append(scenario_id)
    if project_id is not None:
        clauses.append("r.project_id = ?")
        params.append(project_id)
    if version is not None:
        clauses.append("r.scenario_version = ?")
        params.append(int(version))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    all_runs = conn.execute(
        "SELECT r.id, r.scenario_id, r.scenario_version, r.project_id, r.status, r.created_at, r.updated_at "
        "FROM scenario_runs r" + where + " ORDER BY r.created_at DESC, r.id DESC",
        params,
    ).fetchall()
    if not all_runs:
        return empty
    meta_map = all_run_meta(conn)
    if run_kind and run_kind != "all":
        run_rows = [r for r in all_runs if (meta_map.get(r["id"]) or {}).get("kind", "prod") == run_kind]
    else:
        run_rows = list(all_runs)
    if last_n is not None and int(last_n) > 0:
        run_rows = run_rows[: int(last_n)]
    if not run_rows:
        return empty

    selected_ids = {r["id"] for r in run_rows}
    step_rows = conn.execute(
        "SELECT s.run_id, s.step_id, s.type, s.status, s.attempts, s.rejections, s.reason, s.started_at, s.finished_at "
        "FROM scenario_steps s JOIN scenario_runs r ON r.id = s.run_id" + where,
        params,
    ).fetchall()

    by_status = {}
    scn_buckets = {}
    run_rejections = {rid: 0 for rid in selected_ids}
    run_retries = {rid: 0 for rid in selected_ids}
    run_agent_sec = {rid: 0.0 for rid in selected_ids}
    run_human_sec = {rid: 0.0 for rid in selected_ids}
    run_has_agent = set()
    run_has_human = set()
    step_buckets = {}

    for sr in step_rows:
        rid = sr["run_id"]
        if rid not in selected_ids:
            continue
        run_rejections[rid] = run_rejections.get(rid, 0) + int(sr["rejections"] or 0)
        if int(sr["attempts"] or 0) > 1:
            run_retries[rid] = run_retries.get(rid, 0) + (int(sr["attempts"]) - 1)
        sid = sr["step_id"]
        sb = step_buckets.setdefault(sid, {
            "step_id": sid, "type": sr["type"], "executions": 0, "done": 0, "failed": 0,
            "skipped": 0, "blocked": 0, "rejections": 0, "_durs": [], "_reasons": {},
        })
        sb["executions"] += 1
        st = sr["status"]
        if st in ("done", "failed", "skipped", "blocked"):
            sb[st] += 1
        sb["rejections"] += int(sr["rejections"] or 0)
        dur = _duration_sec(sr["started_at"], sr["finished_at"])
        if dur is not None:
            sb["_durs"].append(dur)
            if sr["type"] in ("gate", "checklist"):
                run_human_sec[rid] = run_human_sec.get(rid, 0.0) + dur
                run_has_human.add(rid)
            else:
                run_agent_sec[rid] = run_agent_sec.get(rid, 0.0) + dur
                run_has_agent.add(rid)
        if sr["reason"] and st in ("failed", "blocked"):
            key = str(sr["reason"])[:120]
            sb["_reasons"][key] = sb["_reasons"].get(key, 0) + 1

    # Also collect historical step_failed reasons from scenario_events (survives retries!) via SQL JOIN.
    ev_clauses = clauses + ["e.action = 'step_failed'"]
    ev_where = " WHERE " + " AND ".join(ev_clauses)
    ev_rows = conn.execute(
        "SELECT e.run_id, e.step_key, e.detail_json "
        "FROM scenario_events e JOIN scenario_runs r ON r.id = e.run_id" + ev_where,
        params,
    ).fetchall()
    for ev in ev_rows:
        if ev["run_id"] not in selected_ids:
            continue
        sk = (ev["step_key"] or "").split(".", 1)[0]
        if sk not in step_buckets:
            continue
        try:
            det = json.loads(ev["detail_json"] or "{}")
        except (ValueError, TypeError):
            continue
        reason = det.get("reason")
        if reason:
            key = str(reason)[:120]
            step_buckets[sk]["_reasons"][key] = step_buckets[sk]["_reasons"].get(key, 0) + 1

    terminal = 0
    done_total = 0
    cancelled_total = 0
    first_pass_done = 0
    done_agent_durs = []
    done_human_durs = []
    for r in run_rows:
        st = r["status"]
        by_status[st] = by_status.get(st, 0) + 1
        sc = scn_buckets.setdefault(r["scenario_id"], {
            "scenario_id": r["scenario_id"], "total": 0, "done": 0, "failed": 0,
            "active": 0, "cancelled": 0, "_durs": [],
        })
        sc["total"] += 1
        if st == "done":
            sc["done"] += 1
            done_total += 1
            terminal += 1
            if run_rejections.get(r["id"], 0) == 0 and run_retries.get(r["id"], 0) == 0:
                first_pass_done += 1
            dur = _duration_sec(r["created_at"], r["updated_at"])
            if dur is not None:
                sc["_durs"].append(dur)
            if r["id"] in run_has_agent:
                done_agent_durs.append(run_agent_sec[r["id"]])
            if r["id"] in run_has_human:
                done_human_durs.append(run_human_sec[r["id"]])
        elif st == "failed":
            sc["failed"] += 1
            terminal += 1
        elif st == "cancelled":
            sc["cancelled"] += 1
            cancelled_total += 1
            terminal += 1
        else:
            sc["active"] += 1

    per_scenario = []
    for sc in sorted(scn_buckets.values(), key=lambda x: (-x["total"], x["scenario_id"])):
        durs = sc.pop("_durs")
        sc["avg_duration_sec"] = round(sum(durs) / len(durs), 2) if durs else None
        sc["p50_duration_sec"] = _median(durs)
        per_scenario.append(sc)

    per_step = []
    for sb in sorted(step_buckets.values(), key=lambda x: (-(x["failed"] + x["rejections"]), -x["executions"], x["step_id"])):
        durs = sb.pop("_durs")
        reasons = sb.pop("_reasons")
        sb["avg_duration_sec"] = round(sum(durs) / len(durs), 2) if durs else None
        sb["p50_duration_sec"] = _median(durs)
        sb["top_failure_reasons"] = [
            {"reason": k, "count": v}
            for k, v in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        ]
        per_step.append(sb)

    return {
        "view": "scenario-stats",
        "schema_version": schema_version,
        "run_kind": run_kind,
        "total_runs": len(run_rows),
        "by_status": by_status,
        "completion_rate": round(done_total / terminal, 4) if terminal else None,
        "abort_rate": round(cancelled_total / terminal, 4) if terminal else None,
        "first_pass_rate": round(first_pass_done / done_total, 4) if done_total else None,
        "agent_active_p50_sec": _median(done_agent_durs),
        "human_wait_p50_sec": _median(done_human_durs),
        "per_scenario": per_scenario,
        "per_step": per_step[:30],
    }


def waiting_gates_queue(conn, *, limit=30):
    """Unified 'Waiting for me' queue of all active human gate/checklist steps across projects."""
    if not scenario_registry.has_scenario_tables(conn):
        return []
    rows = conn.execute(
        "SELECT s.run_id, s.step_key, s.step_id, s.type, s.matrix_json, s.started_at, "
        "r.scenario_id, r.project_id, r.definition_json, r.inputs_json, r.created_by_chat, r.created_by_agent "
        "FROM scenario_steps s JOIN scenario_runs r ON r.id = s.run_id "
        "WHERE r.status IN ('running', 'waiting') AND s.status = 'waiting_human' "
        "ORDER BY s.started_at ASC, s.run_id ASC LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        return []
    repo_cache = {}
    steps_cache = {}
    queue = []
    for row in rows:
        rid = row["run_id"]
        try:
            defn = json.loads(row["definition_json"])
        except (ValueError, TypeError):
            continue
        spec = _step_by_id_map(defn).get(row["step_id"])
        if not spec:
            continue
        pid = row["project_id"]
        if pid and pid not in repo_cache:
            prow = conn.execute("SELECT repo_path FROM projects WHERE id = ?", (pid,)).fetchone()
            repo_cache[pid] = prow[0] if prow else None
        if rid not in steps_cache:
            steps_cache[rid] = _load_steps(conn, rid)
        run_stub = {"id": rid, "inputs_json": row["inputs_json"], "project_id": pid}
        ctx = _context(run_stub, steps_cache[rid], defn, project_path=repo_cache.get(pid),
                       agent=row["created_by_agent"])
        if row["matrix_json"]:
            try:
                ctx["matrix"] = json.loads(row["matrix_json"])
            except (ValueError, TypeError):
                pass
        params = spec.get("params") or {}
        title = spec.get("title") or row["step_id"]
        try:
            title = _render_field(title, ctx, line=True, limit=scenario_format.MAX_TITLE, empty=False)
        except (templating.Unresolved, ValueError):
            pass
        item = {
            "run_id": rid,
            "scenario_id": row["scenario_id"],
            "scenario_title": defn.get("title") or row["scenario_id"],
            "project_id": pid,
            "step_key": row["step_key"],
            "step_id": row["step_id"],
            "type": row["type"],
            "title": title,
            "created_by_chat": row["created_by_chat"],
            "started_at": row["started_at"],
        }
        if row["type"] == "gate":
            q = params.get("question") or ""
            try:
                q = _render_field(q, ctx, line=False, limit=4000, empty=False)
            except (templating.Unresolved, ValueError):
                pass
            item["question"] = q
            item["options"] = list(params.get("options") or [])
            item["allow_comment"] = params.get("allow_comment", True)
        elif row["type"] == "checklist":
            rendered_items = []
            for it in (params.get("items") or []):
                txt = it.get("text", "") if isinstance(it, dict) else str(it)
                try:
                    txt = _render_field(txt, ctx, line=True, limit=1000, empty=False)
                except (templating.Unresolved, ValueError):
                    pass
                rendered_items.append({"text": txt, "required": bool(it.get("required")) if isinstance(it, dict) else False})
            item["items"] = rendered_items
        queue.append(item)
    return queue
