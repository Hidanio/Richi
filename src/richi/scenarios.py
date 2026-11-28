"""Scenario definitions: workspace/project storage, built-in templates, dry-run plan and change log.

Like standing rules, scenario definitions are not knowledge: recall, search,
discovery and the graph never return them. Each stored definition is one row in
the store's `metadata` table:

* global (workspace-wide): `scenario:global:<id>`
* project-scoped:          `scenario:project:<len(project_id)>:<project_id>:<id>`

Keeping definitions in `metadata` means backups and workspace copies include
them automatically, and older Richi versions ignore them. A row written for a
newer scenario format raises `scenario_format.UnsupportedVersion` and is
reported in `unsupported_scenarios` with `"next": "richi update"` rather than
hiding healthy definitions; a damaged row is reported in `invalid_scenarios`.

Built-in templates ship under `src/richi/scenario_templates/*.json` (synthetic,
read-only). Resolving a scenario ID checks the project scope first, then global,
then built-in templates, so a fresh workspace can run or preview `review` right
away and shadow it later with `scenario copy`.

Every definition change is recorded in `scenario_events` on schema 4+ so the
unified journal shows definition edits alongside run events; on pre-v4 stores
it falls back to `scenario-log:` metadata rows (latest 200), which
`richi migrate` backfills into `scenario_events`.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from . import agents, rules, scenario_format, skill_catalog, templating
from .output import cut_name
from .revisions import recovery_version


PREFIX = "scenario:"
GLOBAL_PREFIX = "scenario:global:"
PROJECT_PREFIX = "scenario:project:"
LOG_PREFIX = "scenario-log:"
CLOCK_KEY = "scenario_clock"
TEMPLATES_DIR = Path(__file__).resolve().parent / "scenario_templates"
MAX_STORED = 300 * 1024
MAX_CHANGE = 300 * 1024
MAX_LOG = 200
MAX_NAMED = 200
MAX_SOURCE = 500
CHANGE_FIELDS = {
    "add": {"action", "scope", "project", "definition", "source"},
    "update": {"action", "id", "scope", "project", "expected_updated_at", "definition", "source"},
    "remove": {"action", "id", "scope", "project", "expected_updated_at"},
}


def _failure(api, message, code, subject=None):
    error = api.MemoryError(message)
    error.code = code
    if subject is not None:
        error.details = {"subject": subject}
    return error


def storage_key(scope, scenario_id, project_id=None):
    if scope == "global":
        return GLOBAL_PREFIX + scenario_id
    if scope == "project" and project_id:
        return "%s%d:%s:%s" % (PROJECT_PREFIX, len(project_id), project_id, scenario_id)
    raise ValueError("scope must be global or project with project_id")


def _parse_key(key):
    """(scope, project_id, scenario_id) from a metadata key, or None."""
    if key.startswith(GLOBAL_PREFIX):
        identifier = key[len(GLOBAL_PREFIX):]
        return ("global", None, identifier) if identifier else None
    if key.startswith(PROJECT_PREFIX):
        rest = key[len(PROJECT_PREFIX):]
        length_str, sep, tail = rest.partition(":")
        if not sep or not length_str.isdigit():
            return None
        length = int(length_str)
        if length < 1 or len(tail) <= length + 1 or tail[length] != ":":
            return None
        project_id = tail[:length]
        identifier = tail[length + 1:]
        return ("project", project_id, identifier) if identifier else None
    return None


def _text_of(raw):
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return raw if isinstance(raw, str) else None


def _valid_version(value):
    return isinstance(value, str) and 1 <= len(value) <= 64 and "\n" not in value and "\r" not in value


def _stored_version(raw):
    text = _text_of(raw)
    if text is not None and len(text.encode("utf-8")) <= MAX_STORED:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and _valid_version(parsed.get("updated_at")):
                return parsed["updated_at"]
        except (ValueError, TypeError, RecursionError):
            pass
    return recovery_version(raw)


def _optional_utf8_text(value, name):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("stored scenario %s must be text" % name)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("stored scenario %s is not UTF-8 text" % name) from exc
    return value


def _decode(key, raw):
    """Decode one stored scenario row. Raises UnsupportedVersion or ValueError."""
    text = _text_of(raw)
    if text is None:
        raise ValueError("stored scenario is not UTF-8 text")
    parsed_key = _parse_key(key)
    if parsed_key is None:
        raise ValueError("stored scenario key is malformed")
    key_scope, key_project, key_id = parsed_key
    record = scenario_format.parse(text, max_bytes=MAX_STORED, what="stored scenario")
    if record.get("id") != key_id or record.get("scope") != key_scope or record.get("project_id") != key_project:
        raise ValueError("stored scenario scope or ID does not match its key")
    raw_def = record.get("definition")
    if not isinstance(raw_def, dict):
        raise ValueError("stored scenario is missing its definition")
    definition = scenario_format.normalize(raw_def)
    if definition["id"] != key_id:
        raise ValueError("stored scenario definition ID does not match its key")
    version = record.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("stored scenario version must be a positive integer")
    created_at, updated_at = record.get("created_at"), record.get("updated_at")
    if not _valid_version(created_at) or not _valid_version(updated_at):
        raise ValueError("stored scenario is missing timestamps")
    return {
        "id": key_id,
        "scope": key_scope,
        "project_id": key_project,
        "version": version,
        "sha256": scenario_format.digest(definition),
        "definition": definition,
        "source": _optional_utf8_text(record.get("source"), "source"),
        "changed_by": _optional_utf8_text(record.get("changed_by"), "changed_by"),
        "changed_by_agent": _optional_utf8_text(record.get("changed_by_agent"), "changed_by_agent"),
        "created_at": _optional_utf8_text(created_at, "created_at"),
        "updated_at": _optional_utf8_text(updated_at, "updated_at"),
    }


def _summary(record):
    definition = record["definition"]
    return {
        "id": record["id"],
        "title": definition["title"],
        "description": definition.get("description", ""),
        "when_to_use": definition.get("when_to_use", ""),
        "tags": definition.get("tags", []),
        "scope": record["scope"],
        "project_id": record["project_id"],
        "version": record["version"],
        "sha256": record["sha256"],
        "steps": len(definition["steps"]),
        "inputs": list((definition.get("inputs") or {}).keys()),
        "updated_at": record.get("updated_at"),
    }


# ---------------------------------------------------------------- built-in templates


def builtin_templates():
    """Built-in synthetic templates shipped with Richi, keyed by template ID."""
    found = {}
    if not TEMPLATES_DIR.is_dir():
        return found
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        definition = scenario_format.load(path.read_text(encoding="utf-8"))
        found[definition["id"]] = {
            "id": definition["id"],
            "scope": "builtin",
            "project_id": None,
            "version": 1,
            "sha256": scenario_format.digest(definition),
            "definition": definition,
            "source": "builtin:" + path.name,
            "changed_by": None,
            "changed_by_agent": None,
            "created_at": None,
            "updated_at": None,
        }
    return found


def list_templates():
    templates = builtin_templates()
    return {"view": "scenario-templates", "templates": [_summary(item) for item in templates.values()]}


# ---------------------------------------------------------------- store scan and resolve


def scan(conn):
    """(valid records, invalid reports, unsupported reports) from metadata."""
    rows = conn.execute(
        "SELECT CAST(key AS BLOB), CAST(value AS BLOB) FROM metadata "
        "WHERE 1=1 AND key >= 'scenario:' AND key < 'scenario;' ORDER BY key"
    ).fetchall()
    valid, invalid, unsupported = [], [], []
    for raw_key, raw_value in rows:
        key = _text_of(raw_key)
        if key is None:
            invalid.append({"key": cut_name(repr(raw_key), MAX_NAMED), "updated_at": recovery_version(raw_value),
                            "error": "scenario key is not UTF-8 text"})
            continue
        try:
            valid.append(_decode(key, raw_value))
        except scenario_format.UnsupportedVersion as exc:
            unsupported.append({"key": cut_name(key, MAX_NAMED), "requires": exc.version, "error": str(exc),
                                "updated_at": _stored_version(raw_value), "next": "richi update"})
        except ValueError as exc:
            invalid.append({"key": cut_name(key, MAX_NAMED), "error": str(exc),
                            "updated_at": _stored_version(raw_value)})
    valid.sort(key=lambda item: (item["scope"] != "global", item["project_id"] or "", item["id"]))
    return valid, invalid, unsupported


def resolve(conn, scenario_id, project_id=None, *, scope=None, include_builtin=True):
    """Resolve one scenario by ID: project -> global -> built-in (or exact scope when requested)."""
    if not isinstance(scenario_id, str) or not scenario_format.SCENARIO_ID.fullmatch(scenario_id):
        raise ValueError(scenario_format.SCENARIO_ID_HELP)
    candidates = []
    if scope == "global":
        candidates.append(storage_key("global", scenario_id))
    elif scope == "project":
        if not project_id:
            raise ValueError("project scope requires a project ID")
        candidates.append(storage_key("project", scenario_id, project_id))
    elif scope == "builtin":
        return builtin_templates().get(scenario_id)
    else:
        if project_id:
            candidates.append(storage_key("project", scenario_id, project_id))
        candidates.append(storage_key("global", scenario_id))
    for key in candidates:
        row = conn.execute("SELECT CAST(value AS BLOB) FROM metadata WHERE 1=1 AND key = ?", (key,)).fetchone()
        if row is not None:
            return _decode(key, row[0])
    if scope is None and include_builtin:
        return builtin_templates().get(scenario_id)
    return None


def listing(conn, api, workspace=None, *, global_scope=False, project=None, include_builtin=True):
    if project is not None:
        api.require_project(conn, project)
    valid, invalid, unsupported = scan(conn)
    if global_scope:
        valid = [item for item in valid if item["scope"] == "global"]
    elif project is not None:
        valid = [item for item in valid if item["scope"] == "project" and item["project_id"] == project]
    result = {
        "view": "scenarios",
        "workspace": workspace,
        "scenarios": [_summary(item) for item in valid],
    }
    if include_builtin and not global_scope and project is None:
        stored_ids = {item["id"] for item in valid}
        result["builtin_templates"] = [
            dict(_summary(item), shadowed=item["id"] in stored_ids)
            for item in builtin_templates().values()
        ]
    if invalid:
        result["invalid_scenarios"] = invalid
    if unsupported:
        result["unsupported_scenarios"] = unsupported
    return result


# ---------------------------------------------------------------- skills & dry-run plan


def skill_for(skill_spec, agent_id):
    """Resolved skill name for agent_id from a skill step's params.skill object, or None."""
    per_agent = skill_spec.get("per_agent") or {}
    if agent_id and agent_id in per_agent:
        return per_agent[agent_id]
    return skill_spec.get("default")


def installed_skills(agent_id, settings=None):
    if not agent_id:
        return None
    try:
        reg = agents.registry(entries=settings.entries if settings else None)
        target = [item for item in reg if item.id == agent_id]
        if not target:
            return None
        data = skill_catalog.catalog(registry=target)
    except Exception:
        return None
    totals = data.get("totals")
    if not isinstance(totals, dict) or agent_id not in totals:
        return None
    return {
        item.get("name")
        for item in data.get("skills") or []
        if isinstance(item, dict) and item.get("name") and agent_id in (item.get("agents") or [])
    }


_installed_skills = installed_skills


def _resolve_agent(explicit_agent, settings=None):
    entries = settings.entries if settings else None
    if explicit_agent is not None:
        return explicit_agent
    return agents.detect(entries=entries)


def show(conn, api, scenario_id, *, project_id=None, scope=None, agent=None, settings=None):
    if project_id is not None:
        api.require_project(conn, project_id)
    settings = settings if settings is not None else agents.read_settings()
    try:
        record = resolve(conn, scenario_id, project_id, scope=scope)
    except scenario_format.UnsupportedVersion as exc:
        raise _failure(api, str(exc), "unsupported_scenario") from exc
    except ValueError as exc:
        raise _failure(api, str(exc), "invalid_scenario") from exc
    if record is None:
        raise api.NotFoundError("Unknown scenario: " + scenario_id + "; see scenario list or scenario templates")
    detected = _resolve_agent(agent, settings)
    installed_by_agent = {}

    def _get_installed(target_agent):
        if target_agent not in installed_by_agent:
            installed_by_agent[target_agent] = _installed_skills(target_agent, settings)
        return installed_by_agent[target_agent]

    resolved_skills = {}
    missing_skills = []
    for step in record["definition"]["steps"]:
        if step["type"] == "skill":
            _, pinned_agent = scenario_format.executor(step)
            target_agent = pinned_agent or detected
            chosen = skill_for(step["params"]["skill"], target_agent)
            resolved_skills[step["id"]] = chosen
            if target_agent and chosen:
                installed = _get_installed(target_agent)
                if installed is not None and chosen not in installed:
                    missing_skills.append({"step": step["id"], "agent": target_agent, "skill": chosen})
    result = dict(record)
    result["view"] = "scenario"
    result["agent"] = detected
    if resolved_skills:
        result["resolved_skills"] = resolved_skills
    if missing_skills:
        result["missing_skills"] = missing_skills
    return result


def parse_input_options(definition, raw_pairs=None, inputs_json=None):
    """Parse --input key=value strings and/or --inputs-json values according to definition['inputs']."""
    specs = definition.get("inputs") or {}
    provided = {}
    if inputs_json is not None:
        if not isinstance(inputs_json, dict):
            raise scenario_format.FormatError("inputs JSON must be an object", "inputs")
        for key, val in inputs_json.items():
            if key not in specs:
                raise scenario_format.FormatError("unknown input " + str(key), "inputs." + str(key))
            provided[key] = scenario_format.check_input(key, specs[key], val)
    for pair in raw_pairs or ():
        if "=" not in pair:
            raise scenario_format.FormatError("input must have the form KEY=VALUE: " + pair, "inputs")
        key, raw_value = pair.split("=", 1)
        if key not in specs:
            raise scenario_format.FormatError("unknown input " + key, "inputs." + key)
        provided[key] = _coerce_cli_input(key, specs[key], raw_value)
    return provided


def _coerce_cli_input(name, spec, raw):
    kind = spec["type"]
    if kind in ("string", "text", "secret", "select"):
        return scenario_format.check_input(name, spec, raw)
    if kind == "boolean":
        low = raw.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
        raise scenario_format.FormatError("must be true or false", "inputs." + name)
    if kind == "number":
        cleaned = raw.strip()
        try:
            if "." in cleaned or "e" in cleaned.lower():
                val = float(cleaned)
            else:
                digits = cleaned.lstrip("+-")
                if not digits or len(digits) > scenario_format.MAX_DIGITS:
                    raise scenario_format.FormatError(
                        "number is longer than %d digits" % scenario_format.MAX_DIGITS, "inputs." + name
                    )
                val = int(cleaned)
        except ValueError as exc:
            raise scenario_format.FormatError("must be a number", "inputs." + name) from exc
        return scenario_format.check_input(name, spec, val)
    if kind == "multiselect":
        items = [part.strip() for part in raw.split(",") if part.strip()] if raw.strip() else []
        return scenario_format.check_input(name, spec, items)
    raise scenario_format.FormatError("unsupported input type " + kind, "inputs." + name)


def resolve_inputs(definition, provided):
    """(resolved_values, redacted_values, missing_required, secret_names)."""
    specs = definition.get("inputs") or {}
    values, redacted, missing = {}, {}, []
    secrets = {name for name, spec in specs.items() if spec["type"] == "secret"}
    for name, spec in specs.items():
        if name in provided:
            val = scenario_format.check_input(name, spec, provided[name])
            values[name] = val
            if name in secrets:
                redacted[name] = "<secret>" if val else ""
            else:
                redacted[name] = val
        elif "default" in spec:
            values[name] = spec["default"]
            redacted[name] = spec["default"]
        elif spec.get("required"):
            missing.append(name)
    return values, redacted, missing, secrets


def _topological_steps(steps):
    by_id = {step["id"]: step for step in steps}
    in_degree = {step["id"]: len(step["needs"]) for step in steps}
    children = {step["id"]: [] for step in steps}
    for step in steps:
        for need in step["needs"]:
            children[need].append(step["id"])
    ready = [step["id"] for step in steps if in_degree[step["id"]] == 0]
    order = []
    while ready:
        current = ready.pop(0)
        order.append(by_id[current])
        for nxt in children[current]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                ready.append(nxt)
    return order


def plan(conn, api, scenario_id, *, project_id=None, project_path=None, scope=None, agent=None,
         raw_inputs=None, inputs_json=None, settings=None):
    """Dry-run preview: resolve skills, inputs, run_title and step plan without writing."""
    if project_id is not None:
        api.require_project(conn, project_id)
    settings = settings if settings is not None else agents.read_settings()
    try:
        record = resolve(conn, scenario_id, project_id, scope=scope)
    except scenario_format.UnsupportedVersion as exc:
        raise _failure(api, str(exc), "unsupported_scenario") from exc
    except ValueError as exc:
        raise _failure(api, str(exc), "invalid_scenario") from exc
    if record is None:
        raise api.NotFoundError("Unknown scenario: " + scenario_id + "; see scenario list or scenario templates")
    definition = record["definition"]
    try:
        provided = parse_input_options(definition, raw_inputs, inputs_json)
        values, redacted, missing, secrets = resolve_inputs(definition, provided)
    except scenario_format.FormatError as exc:
        raise _failure(api, str(exc), "invalid_input") from exc
    detected = _resolve_agent(agent, settings)
    installed_by_agent = {}

    def _get_installed(target_agent):
        if target_agent not in installed_by_agent:
            installed_by_agent[target_agent] = _installed_skills(target_agent, settings)
        return installed_by_agent[target_agent]

    now_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    preview_inputs = {k: v for k, v in values.items() if k not in secrets}
    context = {
        "inputs": preview_inputs,
        "steps": {},
        "run": {"id": "<preview>"},
        "project": {"path": str(project_path) if project_path else "<project-path>"},
        "agent": {"id": detected or "<agent>"},
        "now": {"date": now_date},
    }
    run_title = definition["title"]
    if "run_title" in definition:
        try:
            run_title = templating.render(templating.parse_template(definition["run_title"]), context)
        except templating.Unresolved:
            run_title = definition["run_title"]

    planned_steps = []
    warnings = []
    needs_project_checks = False
    for step in _topological_steps(definition["steps"]):
        role, pinned_agent = scenario_format.executor(step)
        entry = {
            "id": step["id"],
            "type": step["type"],
            "title": step.get("title", step["id"]),
            "needs": step["needs"],
            "executor": {"role": role, "agent": pinned_agent},
        }
        if "if" in step:
            entry["if"] = step["if"]
        if "matrix" in step:
            try:
                combos = scenario_format.expand_matrix(step, context)
                entry["matrix_instances"] = combos
            except (templating.Unresolved, ValueError) as exc:
                entry["matrix_pending"] = str(exc)
        if step["type"] == "skill":
            target_agent = pinned_agent or detected
            chosen = skill_for(step["params"]["skill"], target_agent)
            entry["skill"] = chosen
            if target_agent:
                if chosen is None:
                    warnings.append("No skill configured for agent %s (step %s)" % (target_agent, step["id"]))
                else:
                    installed = _get_installed(target_agent)
                    if installed is not None and chosen not in installed:
                        warnings.append("Agent %s does not have skill %s (step %s)" % (target_agent, chosen, step["id"]))
        if step.get("hints"):
            entry["hints"] = step["hints"]
        if step.get("outputs"):
            entry["outputs"] = step["outputs"]
        if step.get("checks"):
            entry["checks"] = step["checks"]
            if any(c["kind"] in ("file_exists", "evidence_git") for c in step["checks"]):
                needs_project_checks = True
        planned_steps.append(entry)
    if project_id is None and needs_project_checks:
        warnings.append(
            "Scenario uses project-bound checks (file_exists/evidence_git) and requires --project or --project-path to run"
        )

    return {
        "view": "scenario-plan",
        "scenario": _summary(record),
        "agent": detected,
        "run_title": run_title,
        "inputs": redacted,
        "missing_required_inputs": missing,
        "ready_to_run": not missing,
        "steps": planned_steps,
        "warnings": warnings,
    }


# ---------------------------------------------------------------- writes and change log


def _change_time(conn, api, previous=None):
    row = conn.execute("SELECT CAST(value AS BLOB) FROM metadata WHERE 1=1 AND key = ?", (CLOCK_KEY,)).fetchone()
    log_times = conn.execute(
        "SELECT CAST(substr(key, 14, 27) AS BLOB) FROM metadata WHERE key >= 'scenario-log:' AND key < 'scenario-log;'"
    )
    at = api.next_updated_at(_text_of(row[0]) if row else None, previous, *(_text_of(raw) for raw, in log_times))
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (CLOCK_KEY, at),
    )
    return at


def _has_events_table(conn):
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scenario_events'").fetchone()
    return row is not None


def _record_log(conn, at, action, scope, project_id, scenario_id, before, after, author):
    action_name = action if action.startswith("scenario_") else "scenario_" + action
    before_summary = _summary(before) if before else None
    after_summary = _summary(after) if after else None
    if _has_events_table(conn):
        detail = {
            "scope": scope,
            "project_id": project_id,
            "before": before_summary,
            "after": after_summary,
        }
        conn.execute(
            "INSERT INTO scenario_events(at, scenario_id, run_id, step_key, action, chat, agent, detail_json) "
            "VALUES (?, ?, NULL, NULL, ?, ?, ?, ?)",
            (at, scenario_id, action_name, author.by, author.agent,
             json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )
        return
    scope_key = "global:" + scenario_id if scope == "global" else "project:%d:%s:%s" % (len(project_id), project_id, scenario_id)
    entry = {
        "at": at,
        "id": scenario_id,
        "scope": scope,
        "project_id": project_id,
        "action": action_name,
        "by": author.by,
        "agent": author.agent,
        "before": before_summary,
        "after": after_summary,
    }
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        (LOG_PREFIX + at + ":" + scope_key, json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
    )
    conn.execute(
        "DELETE FROM metadata WHERE 1=1 AND key >= 'scenario-log:' AND key < 'scenario-log;' AND key NOT IN "
        "(SELECT key FROM metadata WHERE 1=1 AND key >= 'scenario-log:' AND key < 'scenario-log;' "
        "ORDER BY key DESC LIMIT ?)",
        (MAX_LOG,),
    )


def log(conn, *, scenario_id=None, run_id=None, entries=20):
    """Unified journal: on schema 4 reads scenario_events; on earlier schemas reads definition changes."""
    if _has_events_table(conn):
        clauses, params = [], []
        if scenario_id is not None:
            clauses.append("scenario_id = ?")
            params.append(scenario_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            "SELECT id, at, scenario_id, run_id, step_key, action, chat, agent, detail_json "
            "FROM scenario_events" + where + " ORDER BY id DESC LIMIT ?",
            params + [entries + 1],
        ).fetchall()
        more = len(rows) > entries
        found = []
        for row in rows[:entries]:
            try:
                detail = json.loads(row[8]) if row[8] else {}
            except ValueError:
                detail = {}
            found.append({
                "event_id": row[0], "at": row[1], "scenario_id": row[2], "run_id": row[3],
                "step_key": row[4], "action": row[5], "by": row[6], "agent": row[7], "detail": detail,
            })
        return {"view": "scenario-log", "source": "events", "entries": found, "more": more}

    if run_id is not None:
        return {"view": "scenario-log", "source": "metadata", "entries": [], "more": False}
    rows = conn.execute(
        "SELECT CAST(value AS BLOB) FROM metadata WHERE 1=1 AND key >= 'scenario-log:' AND key < 'scenario-log;' "
        "ORDER BY key DESC"
    ).fetchall()
    found, more = [], False
    for (raw,) in rows:
        text = _text_of(raw)
        if text is None:
            continue
        try:
            entry = json.loads(text)
        except (ValueError, RecursionError):
            continue
        if isinstance(entry, dict) and (scenario_id is None or entry.get("id") == scenario_id):
            if len(found) == entries:
                more = True
                break
            action = entry.get("action") or ""
            if action and not action.startswith("scenario_"):
                action = "scenario_" + action
            found.append({
                "event_id": None,
                "at": entry.get("at"),
                "scenario_id": entry.get("id"),
                "run_id": None,
                "step_key": None,
                "action": action,
                "by": entry.get("by"),
                "agent": entry.get("agent"),
                "detail": {
                    "scope": entry.get("scope"),
                    "project_id": entry.get("project_id"),
                    "before": entry.get("before"),
                    "after": entry.get("after"),
                },
            })
    return {"view": "scenario-log", "source": "metadata", "entries": found, "more": more, "kept": MAX_LOG}


def read_json_input(path_arg, api, limit=MAX_CHANGE):
    """Read a bounded JSON object from a file path or '-' (stdin) before acquiring a write lock."""
    try:
        if path_arg == "-":
            buf = getattr(sys.stdin, "buffer", None)
            if buf is not None:
                raw_bytes = buf.read(limit + 1)
            else:
                try:
                    raw_text = sys.stdin.read(limit + 1)
                    raw_bytes = raw_text.encode("utf-8")
                except (UnicodeDecodeError, UnicodeEncodeError) as exc:
                    raise _failure(api, "JSON input must be UTF-8 text", "invalid_json") from exc
        else:
            with Path(path_arg).expanduser().open("rb") as handle:
                raw_bytes = handle.read(limit + 1)
    except OSError as exc:
        raise _failure(api, "Cannot read JSON file: " + str(exc), "usage_error") from exc
    if len(raw_bytes) > limit:
        raise _failure(api, "JSON input is larger than %d bytes" % limit, "limit")
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _failure(api, "JSON input must be UTF-8 text", "invalid_json") from exc
    try:
        data = scenario_format.parse(raw_text, max_bytes=limit, what="JSON input")
    except scenario_format.FormatError as exc:
        raise _failure(api, str(exc), "invalid_json") from exc
    return data


def _source(value, api):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_SOURCE or "\n" in value or "\r" in value:
        raise _failure(api, "source must be one line of at most %d characters" % MAX_SOURCE, "invalid_scenario")
    return value.strip()


def change(conn, request, api, *, checked_project=None, author=None):
    """Apply an add, update or remove change object atomically inside the caller's transaction."""
    if not isinstance(request, dict):
        raise _failure(api, "Scenario change must be a JSON object", "invalid_scenario")
    action = request.get("action")
    if action not in CHANGE_FIELDS:
        raise _failure(api, "action must be add, update or remove", "invalid_scenario")
    unknown = sorted(set(request) - CHANGE_FIELDS[action])
    if unknown:
        raise _failure(api, "Unknown change fields: " + ", ".join(unknown), "invalid_scenario")

    scope = request.get("scope", "global")
    if scope not in ("global", "project"):
        raise _failure(api, "scope must be global or project", "invalid_scenario")
    project_id = request.get("project")
    if scope == "project":
        if not project_id:
            project_id = checked_project
        if not project_id:
            raise _failure(api, "project scope requires project or --project-path", "invalid_scenario")
        api.require_project(conn, project_id)
        if checked_project and project_id != checked_project:
            raise _failure(api, "Scenario project %s does not match --project-path project %s" % (
                project_id, checked_project), "project_mismatch")
    elif project_id is not None:
        raise _failure(api, "global scope cannot name a project", "invalid_scenario")

    author = author or rules.current_author()

    if action == "add":
        if "definition" not in request:
            raise _failure(api, "add requires definition", "invalid_scenario")
        try:
            definition = scenario_format.normalize(request["definition"])
        except scenario_format.FormatError as exc:
            raise _failure(api, str(exc), "invalid_scenario") from exc
        scenario_id = definition["id"]
        key = storage_key(scope, scenario_id, project_id)
        existing = conn.execute("SELECT 1 FROM metadata WHERE 1=1 AND key = ?", (key,)).fetchone()
        if existing is not None:
            raise _failure(api, "Scenario %s already exists in %s scope" % (scenario_id, scope), "conflict")
        at = _change_time(conn, api)
        record = {
            "id": scenario_id,
            "scope": scope,
            "project_id": project_id,
            "version": 1,
            "sha256": scenario_format.digest(definition),
            "definition": definition,
            "source": _source(request.get("source"), api),
            "changed_by": author.by,
            "changed_by_agent": author.agent,
            "created_at": at,
            "updated_at": at,
        }
        conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)",
                     (key, json.dumps(record, ensure_ascii=False, separators=(",", ":"))))
        _record_log(conn, at, "created", scope, project_id, scenario_id, None, record, author)
        return {"status": "created", "scenario": record}

    if action == "update":
        if "definition" not in request:
            raise _failure(api, "update requires definition", "invalid_scenario")
        expected = request.get("expected_updated_at")
        if not isinstance(expected, str) or not expected:
            raise _failure(api, "update requires expected_updated_at", "invalid_scenario")
        try:
            definition = scenario_format.normalize(request["definition"])
        except scenario_format.FormatError as exc:
            raise _failure(api, str(exc), "invalid_scenario") from exc
        scenario_id = request.get("id", definition["id"])
        if scenario_id != definition["id"]:
            raise _failure(api, "id does not match definition.id", "invalid_scenario")
        key = storage_key(scope, scenario_id, project_id)
        row = conn.execute("SELECT CAST(value AS BLOB) FROM metadata WHERE 1=1 AND key = ?", (key,)).fetchone()
        if row is None:
            raise api.NotFoundError("Unknown scenario: " + scenario_id)
        try:
            current = _decode(key, row[0])
        except scenario_format.UnsupportedVersion as exc:
            raise _failure(api, str(exc), "unsupported_scenario") from exc
        except ValueError as exc:
            raise _failure(api, str(exc), "invalid_scenario") from exc
        if current["updated_at"] != expected:
            raise _failure(api, "Conflict: scenario %s changed (stored %s, expected %s)" % (
                scenario_id, current["updated_at"], expected), "conflict")
        new_source = current["source"] if "source" not in request else _source(request.get("source"), api)
        if definition == current["definition"] and new_source == current["source"]:
            return {"status": "unchanged", "scenario": current}
        at = _change_time(conn, api, previous=current["updated_at"])
        updated = {
            "id": scenario_id,
            "scope": scope,
            "project_id": project_id,
            "version": current["version"] + 1,
            "sha256": scenario_format.digest(definition),
            "definition": definition,
            "source": new_source,
            "changed_by": author.by,
            "changed_by_agent": author.agent,
            "created_at": current["created_at"],
            "updated_at": at,
        }
        conn.execute("UPDATE metadata SET value = ? WHERE 1=1 AND key = ?",
                     (json.dumps(updated, ensure_ascii=False, separators=(",", ":")), key))
        _record_log(conn, at, "updated", scope, project_id, scenario_id, current, updated, author)
        return {"status": "updated", "scenario": updated}

    # remove
    scenario_id = request.get("id")
    expected = request.get("expected_updated_at")
    if not isinstance(scenario_id, str) or not scenario_id:
        raise _failure(api, "remove requires id", "invalid_scenario")
    if not isinstance(expected, str) or not expected:
        raise _failure(api, "remove requires expected_updated_at", "invalid_scenario")
    key = storage_key(scope, scenario_id, project_id)
    row = conn.execute("SELECT CAST(value AS BLOB) FROM metadata WHERE 1=1 AND key = ?", (key,)).fetchone()
    if row is None:
        raise api.NotFoundError("Unknown scenario: " + scenario_id)
    stored_version = _stored_version(row[0])
    if stored_version != expected:
        raise _failure(api, "Conflict: scenario %s changed (stored %s, expected %s)" % (
            scenario_id, stored_version, expected), "conflict")
    try:
        before = _decode(key, row[0])
    except ValueError:
        before = None
    at = _change_time(conn, api, previous=stored_version if _valid_version(stored_version) else None)
    conn.execute("DELETE FROM metadata WHERE 1=1 AND key = ?", (key,))
    _record_log(conn, at, "removed", scope, project_id, scenario_id, before, None, author)
    return {"status": "removed", "id": scenario_id, "scope": scope, "project_id": project_id}


def copy_template(conn, api, template_id, *, new_id=None, scope="global", project_id=None,
                  checked_project=None, author=None):
    """Copy a built-in template (or stored scenario) into workspace or project scope."""
    try:
        source = resolve(conn, template_id, checked_project or project_id)
    except scenario_format.UnsupportedVersion as exc:
        raise _failure(api, str(exc), "unsupported_scenario") from exc
    except ValueError as exc:
        raise _failure(api, str(exc), "invalid_scenario") from exc
    if source is None:
        raise api.NotFoundError("Unknown scenario or template: " + template_id + "; see scenario templates")
    definition = json.loads(json.dumps(source["definition"]))
    if new_id is not None:
        definition["id"] = new_id
    request = {
        "action": "add",
        "scope": scope,
        "definition": definition,
        "source": "copy:" + source["scope"] + ":" + source["id"],
    }
    if scope == "project":
        request["project"] = project_id or checked_project
    return change(conn, request, api, checked_project=checked_project, author=author)


def map_snapshot(conn, api, workspace=None, *, run_limit=50, log_limit=100):
    """Complete read-only snapshot for the map's Scenarios tab (definitions, templates, runs, log)."""
    from . import scenario_runs

    schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
    projects = [{"id": row[0], "name": row[1]} for row in conn.execute(
        "SELECT id, name FROM projects ORDER BY name, id")]
    stored, invalid, unsupported = scan(conn)
    stored_ids = {item["id"] for item in stored}

    builtins = []
    for record in builtin_templates().values():
        entry = dict(record)
        entry["builtin"] = True
        entry["shadowed"] = record["id"] in stored_ids
        builtins.append(entry)
    builtins.sort(key=lambda item: item["id"])

    runs = []
    runs_total = 0
    runs_waiting = 0
    waiting_queue = []
    stats = None
    if schema_version >= 4:
        runs_total = conn.execute("SELECT count(*) FROM scenario_runs").fetchone()[0]
        runs_waiting = conn.execute(
            "SELECT count(DISTINCT r.id) FROM scenario_runs r "
            "JOIN scenario_steps s ON s.run_id = r.id "
            "WHERE r.status = 'waiting' AND s.status = 'waiting_human'"
        ).fetchone()[0]
        runs = scenario_runs.list_runs(conn, api, limit=run_limit)["runs"]
        waiting_queue = scenario_runs.waiting_gates_queue(conn, limit=30)
        stats = scenario_runs.scenario_stats(conn, api)
        if runs:
            run_ids = [item["id"] for item in runs]
            placeholders = ",".join("?" for _ in run_ids)
            counts_by_run = {}
            for row in conn.execute(
                "SELECT run_id, status, count(*) FROM scenario_steps "
                "WHERE run_id IN (" + placeholders + ") GROUP BY run_id, status",
                run_ids,
            ):
                bucket = counts_by_run.setdefault(row[0], {"total": 0})
                bucket[row[1]] = row[2]
                bucket["total"] += row[2]
            for item in runs:
                counts = counts_by_run.get(item["id"], {"total": 0})
                item["step_counts"] = counts

    journal = log(conn, entries=log_limit)
    return {
        "view": "scenario-map",
        "workspace": workspace,
        "schema_version": schema_version,
        "projects": projects,
        "scenarios": stored,
        "builtin_templates": builtins,
        "invalid_scenarios": invalid,
        "unsupported_scenarios": unsupported,
        "runs": runs,
        "runs_total": runs_total,
        "runs_waiting": runs_waiting,
        "waiting_queue": waiting_queue,
        "stats": stats,
        "runs_more": runs_total > len(runs),
        "log": journal["entries"],
        "log_more": journal["more"],
        "log_source": journal["source"],
        "limit_bytes": scenario_format.MAX_BYTES,
    }
