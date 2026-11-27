"""Scenario runs: scheduler, leases, report validation, run task entries and unified journal.

Richi never executes shell commands or spawns agents itself. Instead it owns
the deterministic state machine for every run in schema 4 tables
(`scenario_runs`, `scenario_steps`, `scenario_events`):

* `start_run` freezes a snapshot of the scenario definition, stores redacted
  inputs (secret values are never persisted), creates the run's `task` entry
  in the "scenario-run" category, and advances ready Richi-executed steps.
* `next_step` returns the next ready step for the calling agent (resolving
  skills, rendering templates with field line/length profiles, checking `cwd`
  containment and reporting substituted command values) and optionally claims
  it with a 30-minute lease.
* `report_step` validates the agent's report against `outputs` and `checks`
  (including symlink-safe `file_exists` and `evidence_git`); a rejected report
  records reasons without closing the step.
* `answer_step` records the human's answer to a `gate` or `checklist` step
  from the chat.
* `skip_step`, `retry_step`, `pause_run`, `resume_run`, `cancel_run`,
  `release_step`, `run_status`, and `list_runs` manage the lifecycle.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
import unicodedata

from . import agents, rules, scenario_format, scenarios, templating


LEASE_SECONDS = 30 * 60
MAX_REPORT_BYTES = 1024 * 1024
ACTIVE_RUN_STATUSES = ("running", "waiting", "paused")
TERMINAL_RUN_STATUSES = ("done", "failed", "cancelled")


def require_v4(conn, api):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 4:
        error = api.MemoryError("Scenario runs require schema 4; run richi migrate, which backs up the store first")
        error.code = "schema_upgrade_required"
        raise error


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


def _render_field(template_str, context, *, line=False, limit=None, field="field", strict_format=False):
    """Render a template and enforce one-line / length profiles on the rendered text (C3)."""
    parsed = templating.parse_template(template_str)
    rendered = templating.render(parsed, context)
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


def _load_steps(conn, run_id):
    rows = conn.execute(
        "SELECT * FROM scenario_steps WHERE run_id = ? ORDER BY position, COALESCE(matrix_index, -1), step_key",
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _step_by_id_map(definition):
    return {step["id"]: step for step in definition["steps"]}


def _build_steps_context(step_rows, definition):
    """Build the `steps` dict for templating/conditions, with aggregate shape for matrix steps (A2)."""
    by_def = _step_by_id_map(definition)
    grouped = {}
    for row in step_rows:
        grouped.setdefault(row["step_id"], []).append(row)

    result = {}
    for step_id, rows in grouped.items():
        spec = by_def.get(step_id, {})
        if "matrix" not in spec:
            row = rows[0]
            if row["status"] == "done" and row["result_json"]:
                payload = json.loads(row["result_json"])
                payload["status"] = "done"
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
            # Never expose the "<secret>" placeholder as a real value; supply only from secrets_runtime if provided.
            if secrets_runtime and key in secrets_runtime:
                inputs[key] = secrets_runtime[key]
            elif secrets_runtime is None:
                # For presence/null checks in conditions, preserve truthiness without exposing any secret value.
                inputs[key] = "<present>" if val == "<secret>" else None
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
                    "lease_until = NULL WHERE run_id = ? AND step_key = ?",
                    (run_row["id"], row["step_key"]),
                )
                _record_event(conn, at_iso, run_row["scenario_id"], run_row["id"], row["step_key"],
                              "lease_expired", row["claimed_by_chat"], row["claimed_by_agent"],
                              {"expired_at": row["lease_until"]})
                expired = True
    return expired


def _run_richi_step(conn, api, database, run_row, step_spec, ctx):
    """Execute a Richi-owned step (`condition`, `memory.recall`, `memory.brief`) inside the current transaction."""
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
        from . import recall
        query = _render_field(params["query"], ctx, line=True, limit=2000, field="params.query")
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
        matches = res.get("matches") or []
        refs = [item.get("ref") for item in matches if item.get("ref")]
        lines = ["%s: %s" % (item.get("ref"), item.get("title", "")) for item in matches[:8]]
        summary = ("; ".join(lines)) if lines else "No matching records found."
        return {"status": "done", "summary": summary[:1500], "data": {"refs": refs, "count": len(refs)}}
    if kind == "memory.brief":
        from . import task_brief
        question = _render_field(params["question"], ctx, line=False, limit=4000, field="params.question")
        limit = params.get("limit", 12)
        args = SimpleNamespace(
            query=question, project=run_row["project_id"], limit=limit, max_chars=16000,
            include_hypotheses=False, include_superseded=False,
            base=None, target=None, worktree=False, repo=None, check_sources=False,
            where=None, explain=False, expect=None,
        )
        res = task_brief.brief(conn, args, api, database)
        cards = res.get("cards") or []
        refs = [item.get("ref") for item in cards if item.get("ref")]
        lines = ["%s: %s" % (item.get("ref"), item.get("title", "")) for item in cards[:8]]
        summary = ("; ".join(lines)) if lines else "No prior records found for this context."
        return {"status": "done", "summary": summary[:1500], "data": {"refs": refs, "count": len(refs)}}
    raise ValueError("Unsupported Richi step type: " + kind)


def _compute_run_status(run_row, step_rows, by_def):
    if run_row["status"] in ("paused", "cancelled"):
        return run_row["status"]
    # Fatal failure on any step without continue_on_failure.
    for row in step_rows:
        if row["status"] == "failed" and not by_def[row["step_id"]].get("continue_on_failure"):
            return "failed"
    # Every step closed?
    terminal = {"done", "skipped"}
    if all(row["status"] in terminal or (row["status"] == "failed" and by_def[row["step_id"]].get("continue_on_failure"))
           for row in step_rows):
        return "done"
    # Any ready or claimed agent step -> running.
    if any(row["status"] in ("ready", "claimed") for row in step_rows):
        return "running"
    # Otherwise waiting on human gate/checklist or blocked step.
    return "waiting"


def _advance(conn, api, database, run_id, *, project_path=None, agent=None, now_override=None):
    """Expire stale leases, unlock ready steps, expand matrices, execute Richi steps, update run status."""
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)
    run_row = _load_run(conn, run_id, api)
    if run_row["status"] in ("paused", "cancelled", "done", "failed"):
        return run_row, _load_steps(conn, run_id), False

    definition = json.loads(run_row["definition_json"])
    by_def = _step_by_id_map(definition)
    previous_status = run_row["status"]
    changed = False

    step_rows = _load_steps(conn, run_id)
    if _expire_leases(conn, run_row, step_rows, at_dt, at_iso):
        changed = True
        step_rows = _load_steps(conn, run_id)

    # Fixed-point loop: advancing one step (skip, condition, recall, brief) may unlock downstream steps.
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

            # Evaluate `if` before matrix expansion (A2).
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

            # Matrix expansion with shell-safe keys `<step_id>.<idx>` (A2).
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
                break  # Re-read step_rows cleanly after replacing placeholder.

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
    if new_status != previous_status or changed:
        conn.execute(
            "UPDATE scenario_runs SET status = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
            (new_status, at_iso, run_id),
        )
        if new_status != previous_status:
            _record_event(conn, at_iso, run_row["scenario_id"], run_id, None,
                          "run_" + new_status, None, None, {"from": previous_status, "to": new_status})
        run_row = _load_run(conn, run_id, api)
    status_changed = new_status != previous_status
    return run_row, step_rows, status_changed


# ---------------------------------------------------------------- run task entry ("Запущенные сценарии")


_STATUS_ICONS = {"done": "✓", "failed": "✗", "waiting_human": "⏳", "skipped": "⊘",
                 "claimed": "▶", "ready": "○", "pending": "·", "blocked": "!"}


def _format_run_summary(run_row, step_rows, definition):
    inputs = json.loads(run_row["inputs_json"])
    lines = [
        "Scenario: %s (v%d, %s)" % (run_row["scenario_id"], run_row["scenario_version"], run_row["scope"]),
        "Run: %s — status: %s" % (run_row["id"], run_row["status"]),
    ]
    if inputs:
        pairs = ", ".join("%s=%s" % (k, v if not isinstance(v, (list, dict)) else json.dumps(v, ensure_ascii=False))
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
        suffix = (" — " + note.replace("\n", " ")[:180]) if note else ""
        lines.append("  %s %s (%s)%s" % (icon, row["step_key"], row["status"], suffix))
    return "\n".join(lines)


def _sync_task_entry(conn, api, run_row, step_rows, definition, *, title=None, create=False):
    """Create or update the run's task entry (tag: scenario-run) only on milestone transitions."""
    entry_id = run_row["task_entry_id"]
    if not entry_id:
        return
    state_map = {"running": "in_progress", "waiting": "blocked", "paused": "blocked",
                 "failed": "blocked", "done": "done", "cancelled": "done"}
    work_state = state_map.get(run_row["status"], "in_progress")
    summary = _format_run_summary(run_row, step_rows, definition)
    if create:
        record = {
            "id": entry_id,
            "kind": "task",
            "title": (title or ("%s: %s" % (definition["title"], run_row["id"])))[:200],
            "summary": summary,
            "project_ids": [run_row["project_id"]] if run_row["project_id"] else [],
            "work_state": work_state,
            "knowledge_state": "hypothesis",
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
    try:
        current = api.entry_get(conn, entry_id, history=False)
    except api.NotFoundError:
        return
    updated = {k: v for k, v in current.items() if k not in ("created_at", "updated_at", "history")}
    updated["summary"] = summary
    updated["work_state"] = work_state
    updated["expected_updated_at"] = current["updated_at"]
    api.entry_put(conn, updated)


def _next_run_id(conn, scenario_id, at_dt):
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", scenario_id)[:40].strip("-") or "run"
    day = at_dt.strftime("%Y%m%d")
    prefix = "run-%s-%s-" % (safe, day)
    rows = conn.execute("SELECT id FROM scenario_runs WHERE id LIKE ? ORDER BY id DESC LIMIT 1", (prefix + "%",)).fetchall()
    seq = 1
    if rows:
        tail = rows[0][0][len(prefix):]
        if tail.isdigit():
            seq = int(tail) + 1
    return "%s%04d" % (prefix, seq)


# ---------------------------------------------------------------- public run operations


def start_run(conn, api, database, scenario_id, *, project_id=None, project_path=None, scope=None,
              raw_inputs=None, inputs_json=None, author=None, now_override=None):
    """Start a new run: validate inputs, freeze snapshot, create task entry, advance ready steps."""
    require_v4(conn, api)
    if project_id is not None:
        api.require_project(conn, project_id)
    try:
        record = scenarios.resolve(conn, scenario_id, project_id, scope=scope)
    except scenario_format.UnsupportedVersion as exc:
        raise _failure(api, str(exc), "unsupported_scenario") from exc
    except ValueError as exc:
        raise _failure(api, str(exc), "invalid_scenario") from exc
    if record is None:
        raise api.NotFoundError("Unknown scenario: " + scenario_id + "; see scenario list or scenario templates")

    definition = record["definition"]
    try:
        provided = scenarios.parse_input_options(definition, raw_inputs, inputs_json)
        _, redacted, missing, _ = scenarios.resolve_inputs(definition, provided)
    except scenario_format.FormatError as exc:
        raise _failure(api, str(exc), "invalid_input") from exc
    if missing:
        raise _failure(api, "Missing required input(s): " + ", ".join(missing), "missing_input", missing=missing)

    author = author or rules.current_author()
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)
    run_id = _next_run_id(conn, record["id"], at_dt)
    task_entry_id = "task:scenario-" + run_id

    # Render run_title using redacted context (never touches secret values) and enforce C3 one-line limit.
    title_ctx = {
        "inputs": {k: v for k, v in redacted.items() if v != "<secret>"},
        "steps": {},
        "run": {"id": run_id},
        "project": {"path": str(project_path) if project_path else ""},
        "agent": {"id": author.agent or ""},
        "now": {"date": at_dt.strftime("%Y-%m-%d")},
    }
    if "run_title" in definition:
        try:
            run_title = _render_field(definition["run_title"], title_ctx, line=True,
                                      limit=scenario_format.MAX_TITLE, field="run_title")
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
    _sync_task_entry(conn, api, run_row, step_rows, definition, title=run_title, create=True)
    return _format_status(run_row, step_rows, definition, view="scenario-run")


def _check_cwd_containment(rendered_cwd, project_path):
    """Ensure rendered cwd stays inside project_path (no '..' or symlink escape) — K1."""
    if rendered_cwd.startswith("/") or "\\" in rendered_cwd:
        raise ValueError("command.cwd must be a relative path inside the project")
    if project_path is None:
        return rendered_cwd
    root = Path(project_path).resolve()
    target = (root / rendered_cwd).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("command.cwd escapes --project-path: " + rendered_cwd) from exc
    return str(target)


def _render_step_payload(step_row, step_spec, ctx, definition, *, project_path=None, agent=None, secrets_names=None):
    """Render a ready/claimed/waiting step for the agent to act on, enforcing C3/K1/I2."""
    step_ctx = dict(ctx)
    if step_row["matrix_json"]:
        step_ctx["matrix"] = json.loads(step_row["matrix_json"])
    secrets_names = secrets_names or set()

    # Check if any secret referenced by this step was not supplied for this call.
    title_ctx = dict(step_ctx, inputs={k: v for k, v in step_ctx["inputs"].items() if k not in secrets_names})
    title = step_spec.get("title", step_spec["id"])
    if "title" in step_spec:
        title = _render_field(step_spec["title"], title_ctx, line=True, limit=scenario_format.MAX_TITLE, field="title")

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
        payload["text"] = _render_field(params["text"], step_ctx, line=False, limit=templating.MAX_RENDERED, field="params.text")
    elif kind == "skill":
        chosen = scenarios.skill_for(params["skill"], agent)
        payload["skill"] = chosen
        if "instruction" in params:
            payload["instruction"] = _render_field(params["instruction"], step_ctx, line=False,
                                                   limit=templating.MAX_RENDERED, field="params.instruction")
    elif kind == "command":
        payload["command"] = _render_field(params["command"], step_ctx, line=True, limit=4000,
                                           field="params.command", strict_format=True)
        payload["substituted_values"] = _substituted_values(params["command"], step_ctx, secrets_names)
        if "cwd" in params:
            rendered_cwd = _render_field(params["cwd"], title_ctx, line=True, limit=1000,
                                         field="params.cwd", strict_format=True)
            payload["cwd"] = _check_cwd_containment(rendered_cwd, project_path)
        if "expect_exit" in params:
            payload["expect_exit"] = params["expect_exit"]
        if "tail_lines" in params:
            payload["tail_lines"] = params["tail_lines"]
    elif kind == "gate":
        payload["question"] = _render_field(params["question"], title_ctx, line=False, limit=4000, field="params.question")
        if "options" in params:
            payload["options"] = params["options"]
        if "allow_comment" in params:
            payload["allow_comment"] = params["allow_comment"]
    elif kind == "checklist":
        items = []
        for idx, item in enumerate(params["items"]):
            items.append({
                "text": _render_field(item["text"], title_ctx, line=True, limit=1000, field="params.items[%d].text" % idx),
                "required": bool(item.get("required", False)),
            })
        payload["items"] = items
    return payload


def next_step(conn, api, database, run_id, *, claim=False, project_path=None, agent=None, author=None,
              secrets_runtime=None, now_override=None):
    """Return the next actionable step for this agent/chat; with claim=True, acquire a 30m lease."""
    require_v4(conn, api)
    author = author or rules.current_author()
    active_agent = agent or author.agent
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)

    run_row, step_rows, status_changed = _advance(conn, api, database, run_id, project_path=project_path,
                                                  agent=active_agent, now_override=at_dt)
    definition = json.loads(run_row["definition_json"])
    if status_changed:
        _sync_task_entry(conn, api, run_row, step_rows, definition)

    if run_row["status"] in ("done", "failed", "cancelled", "paused"):
        return {"view": "scenario-next", "run_id": run_id, "run_status": run_row["status"],
                "step": None, "waiting_human": [], "parallel_ready": []}

    by_def = _step_by_id_map(definition)
    input_specs = definition.get("inputs") or {}
    secret_names = {name for name, spec in input_specs.items() if spec["type"] == "secret"}

    # Prefer a step already claimed by this chat (so resuming after compaction returns the same step).
    mine = [row for row in step_rows if row["status"] == "claimed" and author.by and row["claimed_by_chat"] == author.by]
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

    ctx = _context(run_row, step_rows, definition, project_path=project_path, agent=active_agent,
                   now_dt=at_dt, secrets_runtime=secrets_runtime if secrets_runtime is not None else {})

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

    spec = by_def[chosen["step_id"]]
    try:
        rendered = _render_step_payload(chosen, spec, ctx, definition, project_path=project_path,
                                        agent=active_agent, secrets_names=secret_names)
    except templating.Unresolved as exc:
        # Missing runtime secret? Report cleanly without blocking the step.
        if exc.path.startswith("inputs.") and exc.path.split(".", 1)[1] in secret_names:
            return {
                "view": "scenario-next",
                "run_id": run_id,
                "run_status": run_row["status"],
                "step": {"key": chosen["step_key"], "id": spec["id"], "type": spec["type"], "status": chosen["status"]},
                "missing_secret": exc.path.split(".", 1)[1],
                "note": "Pass --secret %s=VALUE or ask the user in this chat; secret values are never stored." % exc.path.split(".", 1)[1],
            }
        conn.execute(
            "UPDATE scenario_steps SET status = 'blocked', reason = ? WHERE run_id = ? AND step_key = ?",
            ("unresolved value: " + str(exc), run_id, chosen["step_key"]),
        )
        _record_event(conn, at_iso, run_row["scenario_id"], run_id, chosen["step_key"],
                      "step_blocked", author.by, active_agent, {"reason": str(exc)})
        raise _failure(api, "Step %s blocked: %s" % (chosen["step_key"], exc), "step_blocked") from exc
    except ValueError as exc:
        conn.execute(
            "UPDATE scenario_steps SET status = 'blocked', reason = ? WHERE run_id = ? AND step_key = ?",
            (str(exc), run_id, chosen["step_key"]),
        )
        _record_event(conn, at_iso, run_row["scenario_id"], run_id, chosen["step_key"],
                      "step_blocked", author.by, active_agent, {"reason": str(exc)})
        raise _failure(api, "Step %s blocked: %s" % (chosen["step_key"], exc), "step_blocked") from exc

    # Claim or renew lease on agent steps when requested.
    if claim and chosen["executor_role"] == "agent":
        lease_until = _format_iso(at_dt + timedelta(seconds=LEASE_SECONDS))
        if chosen["status"] == "ready":
            conn.execute(
                "UPDATE scenario_steps SET status = 'claimed', claimed_by_chat = ?, claimed_by_agent = ?, "
                "lease_until = ?, attempts = attempts + 1, started_at = COALESCE(started_at, ?) "
                "WHERE run_id = ? AND step_key = ?",
                (author.by, active_agent, lease_until, at_iso, run_id, chosen["step_key"]),
            )
            conn.execute("UPDATE scenario_runs SET updated_at = ?, revision = revision + 1 WHERE id = ?", (at_iso, run_id))
            _record_event(conn, at_iso, run_row["scenario_id"], run_id, chosen["step_key"],
                          "claimed", author.by, active_agent, {"lease_until": lease_until})
        else:
            conn.execute(
                "UPDATE scenario_steps SET lease_until = ? WHERE run_id = ? AND step_key = ?",
                (lease_until, run_id, chosen["step_key"]),
            )
        rendered["status"] = "claimed"
        rendered["lease_until"] = lease_until

    if chosen["executor_role"] == "human":
        rendered["answer_command"] = "richi scenario answer %s %s VALUE" % (run_id, chosen["step_key"])
    else:
        rendered["report_command"] = "richi scenario report %s %s --json -" % (run_id, chosen["step_key"])

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


def claim_step(conn, api, database, run_id, step_key, *, project_path=None, agent=None, author=None, now_override=None):
    require_v4(conn, api)
    author = author or rules.current_author()
    active_agent = agent or author.agent
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)
    run_row, step_rows, _ = _advance(conn, api, database, run_id, project_path=project_path,
                                     agent=active_agent, now_override=at_dt)
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
        if author.by and step["claimed_by_chat"] == author.by:
            lease_until = _format_iso(at_dt + timedelta(seconds=LEASE_SECONDS))
            conn.execute("UPDATE scenario_steps SET lease_until = ? WHERE run_id = ? AND step_key = ?",
                         (lease_until, run_id, step_key))
            return {"status": "claimed", "run_id": run_id, "step_key": step_key, "lease_until": lease_until}
        raise _failure(api, "Step %s is already claimed by %s" % (step_key, step["claimed_by_chat"] or "another chat"), "conflict")
    if step["status"] != "ready":
        raise _failure(api, "Step %s is %s, not ready" % (step_key, step["status"]), "invalid_state")
    lease_until = _format_iso(at_dt + timedelta(seconds=LEASE_SECONDS))
    conn.execute(
        "UPDATE scenario_steps SET status = 'claimed', claimed_by_chat = ?, claimed_by_agent = ?, "
        "lease_until = ?, attempts = attempts + 1, started_at = COALESCE(started_at, ?) "
        "WHERE run_id = ? AND step_key = ?",
        (author.by, active_agent, lease_until, at_iso, run_id, step_key),
    )
    conn.execute("UPDATE scenario_runs SET updated_at = ?, revision = revision + 1 WHERE id = ?", (at_iso, run_id))
    _record_event(conn, at_iso, run_row["scenario_id"], run_id, step_key, "claimed", author.by, active_agent,
                  {"lease_until": lease_until})
    return {"status": "claimed", "run_id": run_id, "step_key": step_key, "lease_until": lease_until}


def release_step(conn, api, run_id, step_key, *, author=None, now_override=None):
    require_v4(conn, api)
    author = author or rules.current_author()
    at_iso = _format_iso(_now_dt(api, now_override))
    run_row = _load_run(conn, run_id, api)
    step = conn.execute("SELECT * FROM scenario_steps WHERE run_id = ? AND step_key = ?", (run_id, step_key)).fetchone()
    if step is None:
        raise api.NotFoundError("Unknown step %s in run %s" % (step_key, run_id))
    if step["status"] != "claimed":
        raise _failure(api, "Step %s is %s, not claimed" % (step_key, step["status"]), "invalid_state")
    if step["claimed_by_chat"] and author.by and step["claimed_by_chat"] != author.by:
        raise _failure(api, "Step %s is claimed by another chat (%s)" % (step_key, step["claimed_by_chat"]), "conflict")
    conn.execute(
        "UPDATE scenario_steps SET status = 'ready', claimed_by_chat = NULL, claimed_by_agent = NULL, "
        "lease_until = NULL WHERE run_id = ? AND step_key = ?",
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


def _validate_report(report, step_row, step_spec, ctx, project_path):
    """Return list of failure reasons (empty means report is accepted)."""
    reasons = []
    if not isinstance(report, dict):
        return ["report must be a JSON object"]
    status = report.get("status")
    if status not in ("done", "failed"):
        return ["report.status must be 'done' or 'failed'"]
    if len(json.dumps(report, ensure_ascii=False).encode("utf-8")) > MAX_REPORT_BYTES:
        return ["report is larger than %d bytes" % MAX_REPORT_BYTES]

    summary = report.get("summary")
    if status == "done" and (not isinstance(summary, str) or not summary.strip()):
        reasons.append("report.summary must be nonempty text when status is done")
    elif summary is not None and not isinstance(summary, str):
        reasons.append("report.summary must be text")

    data = report.get("data")
    if data is not None and not isinstance(data, dict):
        reasons.append("report.data must be a JSON object")
        data = {}
    data = data or {}

    files = report.get("files")
    if files is not None and (not isinstance(files, list) or not all(isinstance(item, str) for item in files)):
        reasons.append("report.files must be a list of paths")
        files = []
    files = files or []

    evidence = report.get("evidence")
    if evidence is not None and (not isinstance(evidence, list) or not all(isinstance(item, dict) for item in evidence)):
        reasons.append("report.evidence must be a list of objects")
        evidence = []
    evidence = evidence or []

    if status == "failed":
        return reasons

    # 1. outputs schema check (A10: outputs implies data_schema).
    outputs = step_spec.get("outputs") or {}
    for field, spec in (outputs.get("data") or {}).items():
        if field not in data:
            if spec.get("required"):
                reasons.append("missing required data field: " + field)
        elif not _matches_output_type(data[field], spec["type"]):
            reasons.append("data.%s must be %s" % (field, spec["type"]))
    if (outputs.get("files") or {}).get("required") and not files:
        reasons.append("report.files must list at least one file")

    # 2. command.params.expect_exit check.
    exit_code = report.get("exit_code")
    if exit_code is None and "exit_code" in data:
        exit_code = data["exit_code"]
    if step_spec["type"] == "command" and "expect_exit" in (step_spec.get("params") or {}):
        expected = step_spec["params"]["expect_exit"]
        if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code != expected:
            reasons.append("exit_code must be %d (got %r)" % (expected, exit_code))

    # 3. explicit checks.
    step_ctx = dict(ctx)
    if step_row["matrix_json"]:
        step_ctx["matrix"] = json.loads(step_row["matrix_json"])

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
                                         limit=scenario_format.MAX_PROJECT_PATH, field="check.file_exists")
            except (templating.Unresolved, ValueError) as exc:
                reasons.append("check file_exists failed: " + str(exc))
                continue
            if rel_path.startswith("/") or "\\" in rel_path or ".." in rel_path.split("/"):
                reasons.append("check file_exists failed: invalid relative path " + rel_path)
                continue
            if project_path is None:
                reasons.append("check file_exists requires --project-path to verify " + rel_path)
                continue
            root = Path(project_path).resolve()
            resolved = (root / rel_path).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                reasons.append("check file_exists failed: path escapes project root: " + rel_path)
                continue
            if not resolved.is_file():
                reasons.append("check file_exists failed: file not found: " + rel_path)
        elif kind == "evidence_git":
            git_items = [item for item in evidence if item.get("type") == "git" and isinstance(item.get("revision"), str) and item["revision"].strip()]
            if not git_items:
                reasons.append("check evidence_git failed: report.evidence needs a git revision")
            elif project_path is not None:
                rev = git_items[0]["revision"].strip()
                proc = subprocess.run(
                    ["git", "-C", str(project_path), "rev-parse", "--verify", "--quiet", rev + "^{commit}"],
                    capture_output=True, text=True, check=False,
                )
                if proc.returncode != 0:
                    reasons.append("check evidence_git failed: revision not found in repository: " + rev)
    return reasons


def report_step(conn, api, database, run_id, step_key, report, *, project_path=None, author=None, now_override=None):
    """Validate and record an agent's report for a claimed step."""
    require_v4(conn, api)
    author = author or rules.current_author()
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)
    run_row = _load_run(conn, run_id, api)
    if run_row["status"] in ("paused", "cancelled", "done", "failed"):
        raise _failure(api, "Run %s is %s and cannot accept reports" % (run_id, run_row["status"]), "invalid_state")
    definition = json.loads(run_row["definition_json"])
    by_def = _step_by_id_map(definition)
    step_rows = _load_steps(conn, run_id)
    step = next((row for row in step_rows if row["step_key"] == step_key), None)
    if step is None:
        raise api.NotFoundError("Unknown step %s in run %s" % (step_key, run_id))
    if step["status"] != "claimed":
        raise _failure(api, "Step %s is %s; claim it first with scenario next --claim" % (step_key, step["status"]), "invalid_state")
    if step["claimed_by_chat"] and author.by and step["claimed_by_chat"] != author.by:
        raise _failure(api, "Step %s is claimed by another chat (%s)" % (step_key, step["claimed_by_chat"]), "conflict")

    spec = by_def[step["step_id"]]
    ctx = _context(run_row, step_rows, definition, project_path=project_path, agent=author.agent, now_dt=at_dt)
    reasons = _validate_report(report, step, spec, ctx, project_path)
    if reasons:
        conn.execute(
            "UPDATE scenario_steps SET rejections = rejections + 1 WHERE run_id = ? AND step_key = ?",
            (run_id, step_key),
        )
        _record_event(conn, at_iso, run_row["scenario_id"], run_id, step_key,
                      "report_rejected", author.by, author.agent, {"reasons": reasons})
        return {"status": "rejected", "run_id": run_id, "step_key": step_key,
                "step_status": "claimed", "reasons": reasons}

    stored = {
        "status": report["status"],
        "summary": report.get("summary") or "",
        "data": report.get("data") or {},
        "files": report.get("files") or [],
        "evidence": report.get("evidence") or [],
        "agent": author.agent,
        "chat": author.by,
    }
    if "exit_code" in report:
        stored["exit_code"] = report["exit_code"]
    elif "exit_code" in stored["data"]:
        stored["exit_code"] = stored["data"]["exit_code"]

    new_step_status = report["status"]
    conn.execute(
        "UPDATE scenario_steps SET status = ?, claimed_by_chat = NULL, claimed_by_agent = NULL, lease_until = NULL, "
        "result_json = ?, reason = ?, finished_at = ? WHERE run_id = ? AND step_key = ?",
        (new_step_status, json.dumps(stored, ensure_ascii=False, separators=(",", ":")),
         report.get("reason") if new_step_status == "failed" else None, at_iso, run_id, step_key),
    )
    _record_event(conn, at_iso, run_row["scenario_id"], run_id, step_key,
                  "report_accepted" if new_step_status == "done" else "step_failed",
                  author.by, author.agent, {"summary": stored["summary"][:300]})

    run_row, step_rows, status_changed = _advance(conn, api, database, run_id, project_path=project_path,
                                                  agent=author.agent, now_override=at_dt)
    if status_changed:
        _sync_task_entry(conn, api, run_row, step_rows, definition)
    return {"status": "accepted", "run_id": run_id, "step_key": step_key,
            "step_status": new_step_status, "run_status": run_row["status"]}


def answer_step(conn, api, database, run_id, step_key, value, *, comment=None, project_path=None,
                author=None, now_override=None):
    """Record the human's answer to a waiting gate or checklist step (always from the chat)."""
    require_v4(conn, api)
    author = author or rules.current_author()
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)
    run_row = _load_run(conn, run_id, api)
    if run_row["status"] in ("paused", "cancelled", "done", "failed"):
        raise _failure(api, "Run %s is %s and cannot accept answers" % (run_id, run_row["status"]), "invalid_state")
    definition = json.loads(run_row["definition_json"])
    by_def = _step_by_id_map(definition)
    step = conn.execute("SELECT * FROM scenario_steps WHERE run_id = ? AND step_key = ?", (run_id, step_key)).fetchone()
    if step is None:
        raise api.NotFoundError("Unknown step %s in run %s" % (step_key, run_id))
    if step["status"] != "waiting_human":
        raise _failure(api, "Step %s is %s, not waiting for a human answer" % (step_key, step["status"]), "invalid_state")

    spec = by_def[step["step_id"]]
    params = spec.get("params") or {}
    if spec["type"] == "gate":
        options = params.get("options")
        if options and value not in options:
            raise _failure(api, "Answer must be one of: " + ", ".join(options), "invalid_answer")
        stored = {"status": "done", "answer": value, "comment": comment or "",
                  "summary": value + (": " + comment if comment else "")}
    else:
        # checklist: accept "done"/"all" or comma-separated item indices/texts.
        items = params["items"]
        checked = list(range(len(items))) if value in ("done", "all", "ok") else [
            part.strip() for part in value.split(",") if part.strip()
        ]
        stored = {"status": "done", "checked": checked, "summary": "checked (%s)" % value}

    conn.execute(
        "UPDATE scenario_steps SET status = 'done', result_json = ?, finished_at = ? WHERE run_id = ? AND step_key = ?",
        (json.dumps(stored, ensure_ascii=False, separators=(",", ":")), at_iso, run_id, step_key),
    )
    _record_event(conn, at_iso, run_row["scenario_id"], run_id, step_key,
                  "answered", author.by, author.agent, {"value": value, "comment": comment})

    run_row, step_rows, status_changed = _advance(conn, api, database, run_id, project_path=project_path,
                                                  agent=author.agent, now_override=at_dt)
    if status_changed:
        _sync_task_entry(conn, api, run_row, step_rows, definition)
    return {"status": "answered", "run_id": run_id, "step_key": step_key, "run_status": run_row["status"]}


def skip_step(conn, api, database, run_id, step_key, reason, *, project_path=None, author=None, now_override=None):
    require_v4(conn, api)
    if not isinstance(reason, str) or not reason.strip():
        raise _failure(api, "skip requires a nonempty --reason", "usage_error")
    author = author or rules.current_author()
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)
    run_row = _load_run(conn, run_id, api)
    if run_row["status"] in ("cancelled", "done"):
        raise _failure(api, "Run %s is %s" % (run_id, run_row["status"]), "invalid_state")
    step = conn.execute("SELECT * FROM scenario_steps WHERE run_id = ? AND step_key = ?", (run_id, step_key)).fetchone()
    if step is None:
        raise api.NotFoundError("Unknown step %s in run %s" % (step_key, run_id))
    if step["status"] in ("done", "skipped"):
        raise _failure(api, "Step %s is already %s" % (step_key, step["status"]), "invalid_state")
    conn.execute(
        "UPDATE scenario_steps SET status = 'skipped', claimed_by_chat = NULL, claimed_by_agent = NULL, "
        "lease_until = NULL, reason = ?, finished_at = ? WHERE run_id = ? AND step_key = ?",
        (reason.strip(), at_iso, run_id, step_key),
    )
    _record_event(conn, at_iso, run_row["scenario_id"], run_id, step_key,
                  "step_skipped", author.by, author.agent, {"reason": reason.strip()})
    if run_row["status"] == "failed":
        conn.execute("UPDATE scenario_runs SET status = 'running', updated_at = ?, revision = revision + 1 WHERE id = ?",
                     (at_iso, run_id))
    definition = json.loads(run_row["definition_json"])
    run_row, step_rows, status_changed = _advance(conn, api, database, run_id, project_path=project_path,
                                                  agent=author.agent, now_override=at_dt)
    if status_changed:
        _sync_task_entry(conn, api, run_row, step_rows, definition)
    return {"status": "skipped", "run_id": run_id, "step_key": step_key, "run_status": run_row["status"]}


def retry_step(conn, api, database, run_id, step_key=None, *, project_path=None, author=None, now_override=None):
    """Retry a failed or blocked step (defaults to the first failed/blocked step of the run)."""
    require_v4(conn, api)
    author = author or rules.current_author()
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)
    run_row = _load_run(conn, run_id, api)
    if run_row["status"] in ("cancelled", "done"):
        raise _failure(api, "Run %s is %s and cannot be retried" % (run_id, run_row["status"]), "invalid_state")
    step_rows = _load_steps(conn, run_id)
    if step_key is None:
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
    spec = _step_by_id_map(definition)[target["step_id"]]
    reset_status = "pending" if target["status"] == "blocked" and target["matrix_index"] is None else "ready"
    if spec["type"] in ("gate", "checklist") and reset_status == "ready":
        reset_status = "waiting_human"
    conn.execute(
        "UPDATE scenario_steps SET status = ?, claimed_by_chat = NULL, claimed_by_agent = NULL, lease_until = NULL, "
        "result_json = NULL, reason = NULL, finished_at = NULL WHERE run_id = ? AND step_key = ?",
        (reset_status, run_id, target["step_key"]),
    )
    conn.execute("UPDATE scenario_runs SET status = 'running', updated_at = ?, revision = revision + 1 WHERE id = ?",
                 (at_iso, run_id))
    _record_event(conn, at_iso, run_row["scenario_id"], run_id, target["step_key"],
                  "retried", author.by, author.agent, {})
    run_row, step_rows, _ = _advance(conn, api, database, run_id, project_path=project_path,
                                     agent=author.agent, now_override=at_dt)
    _sync_task_entry(conn, api, run_row, step_rows, definition)
    return {"status": "retried", "run_id": run_id, "step_key": target["step_key"], "run_status": run_row["status"]}


def set_run_state(conn, api, database, run_id, action, *, project_path=None, author=None, now_override=None):
    """pause, resume or cancel a run."""
    require_v4(conn, api)
    author = author or rules.current_author()
    at_dt = _now_dt(api, now_override)
    at_iso = _format_iso(at_dt)
    run_row = _load_run(conn, run_id, api)
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
        conn.execute(
            "UPDATE scenario_steps SET claimed_by_chat = NULL, claimed_by_agent = NULL, lease_until = NULL "
            "WHERE run_id = ? AND status = 'claimed'",
            (run_id,),
        )
        _record_event(conn, at_iso, run_row["scenario_id"], run_id, None, "cancelled", author.by, author.agent, {})
    else:
        raise ValueError("unknown action " + action)

    run_row = _load_run(conn, run_id, api)
    step_rows = _load_steps(conn, run_id)
    _sync_task_entry(conn, api, run_row, step_rows, definition)
    return _format_status(run_row, step_rows, definition, view="scenario-run")


def _format_status(run_row, step_rows, definition, *, view="scenario-status"):
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
        }
        if row["matrix_json"]:
            item["matrix"] = json.loads(row["matrix_json"])
        if row["claimed_by_chat"]:
            item["claimed_by_chat"] = row["claimed_by_chat"]
            item["lease_until"] = row["lease_until"]
        if row["reason"]:
            item["reason"] = row["reason"]
        if row["result_json"]:
            parsed = json.loads(row["result_json"])
            item["summary"] = parsed.get("summary", "")
            if "answer" in parsed:
                item["answer"] = parsed["answer"]
        steps.append(item)
    return {
        "view": view,
        "run_id": run_row["id"],
        "scenario_id": run_row["scenario_id"],
        "scenario_version": run_row["scenario_version"],
        "scope": run_row["scope"],
        "project_id": run_row["project_id"],
        "status": run_row["status"],
        "task_entry_id": run_row["task_entry_id"],
        "inputs": json.loads(run_row["inputs_json"]),
        "created_by_chat": run_row["created_by_chat"],
        "created_by_agent": run_row["created_by_agent"],
        "created_at": run_row["created_at"],
        "updated_at": run_row["updated_at"],
        "revision": run_row["revision"],
        "steps": steps,
    }


def run_status(conn, api, run_id):
    require_v4(conn, api)
    run_row = _load_run(conn, run_id, api)
    step_rows = _load_steps(conn, run_id)
    definition = json.loads(run_row["definition_json"])
    return _format_status(run_row, step_rows, definition)


def list_runs(conn, api, *, active=False, chat=None, project_id=None, scenario_id=None, limit=20):
    require_v4(conn, api)
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
        "created_by_chat, created_by_agent, created_at, updated_at FROM scenario_runs"
        + where + " ORDER BY updated_at DESC, id DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    return {
        "view": "scenario-runs",
        "runs": [dict(row) for row in rows],
    }
