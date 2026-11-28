"""Agent lifecycle hooks for scenarios (M2): session-start, stop enforcement, and pre-compact handoff.

Richi keeps the stop continuation counter in SQLite (`scenario_steps.stop_continuations`)
so enforcement never relies on an agent's own loop-guard flag (e.g. Antigravity's
`continuationActive` does not flip on repeated Stop hooks).
"""
from datetime import datetime, timezone
import json
import shlex

from . import rules, scenario_registry, scenario_runs, scenarios


DEFAULT_MAX_CONTINUATIONS = 5
MAX_CONTINUATIONS_LIMIT = 20
ENFORCE_KEY = "scenarios:enforce"


def _has_tables(conn):
    return scenario_registry.has_scenario_tables(conn)


def _now_iso(api, now_override=None):
    if now_override is not None:
        return now_override if isinstance(now_override, str) else now_override.strftime("%Y-%m-%dT%H:%M:%SZ")
    now = getattr(api, "now_utc", None)
    if callable(now):
        return now()
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _enforcement_enabled(conn):
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (ENFORCE_KEY,)).fetchone()
    if row is None:
        return True
    val = str(row[0]).strip().lower()
    return val not in {"false", "0", "off", "no"}


def _step_title(definition, step_id):
    for step in (definition.get("steps") or []):
        if step.get("id") == step_id:
            return step.get("title") or step_id
    return step_id


def _load_active_context(conn, *, chat=None, agent=None, project_id=None, now_dt=None):
    """Collect active claimed steps (excluding expired leases), waiting human gates, and agent-pinned ready steps."""
    if not _has_tables(conn):
        return {
            "active_runs": [],
            "claimed_steps": [],
            "waiting_human": [],
            "agent_ready_steps": [],
        }

    rk_sql = scenario_runs.RUN_KIND_SQL
    clauses = ["r.status IN ('running', 'waiting')"]
    params = []
    if project_id is not None:
        clauses.append("(r.project_id IS NULL OR r.project_id = ?)")
        params.append(project_id)
    if chat:
        clauses.append(
            f"({rk_sql} = 'prod' OR r.created_by_chat = ? "
            "OR EXISTS (SELECT 1 FROM scenario_steps s WHERE s.run_id = r.id AND s.status = 'claimed' AND s.claimed_by_chat = ?))"
        )
        params.extend([chat, chat])
    else:
        clauses.append(f"{rk_sql} = 'prod'")

    runs_rows = conn.execute(
        f"SELECT r.id, r.scenario_id, r.scope, r.project_id, r.status, r.definition_json, "
        f"r.created_by_chat, r.created_by_agent, r.updated_at, {rk_sql} AS run_kind "
        "FROM scenario_runs r "
        "LEFT JOIN metadata m ON m.key = 'scenario-run-meta:' || r.id "
        "WHERE " + " AND ".join(clauses) + " ORDER BY r.updated_at DESC, r.id DESC LIMIT 50",
        params,
    ).fetchall()
    if not runs_rows:
        return {
            "active_runs": [],
            "claimed_steps": [],
            "waiting_human": [],
            "agent_ready_steps": [],
        }

    runs_by_id = {}
    defs_by_id = {}
    for row in runs_rows:
        r = dict(row)
        r["run_kind"] = r.get("run_kind") or "prod"
        runs_by_id[r["id"]] = r
        try:
            defs_by_id[r["id"]] = json.loads(r["definition_json"])
        except (ValueError, TypeError):
            defs_by_id[r["id"]] = {}

    if not runs_by_id:
        return {
            "active_runs": [],
            "claimed_steps": [],
            "waiting_human": [],
            "agent_ready_steps": [],
        }

    placeholders = ",".join("?" for _ in runs_by_id)
    step_rows = conn.execute(
        "SELECT run_id, step_key, step_id, position, type, status, executor_role, executor_agent, "
        "claimed_by_chat, claimed_by_agent, lease_until, stop_continuations "
        f"FROM scenario_steps WHERE run_id IN ({placeholders}) "
        "AND status IN ('claimed', 'waiting_human', 'ready') ORDER BY run_id, position, COALESCE(matrix_index, -1), step_key",
        tuple(runs_by_id.keys()),
    ).fetchall()

    now_dt = now_dt or datetime.now(timezone.utc)
    claimed_steps = []
    waiting_human = []
    agent_ready_steps = []
    touched_runs = set()

    for srow in step_rows:
        s = dict(srow)
        run = runs_by_id[s["run_id"]]
        title = _step_title(defs_by_id[s["run_id"]], s["step_id"])
        item = {
            "run_id": s["run_id"],
            "scenario_id": run["scenario_id"],
            "project_id": run["project_id"],
            "run_kind": run["run_kind"],
            "step_key": s["step_key"],
            "step_id": s["step_id"],
            "title": title,
            "type": s["type"],
            "status": s["status"],
        }
        if s["status"] == "claimed" and chat and s["claimed_by_chat"] == chat:
            # Skip already-expired leases so Stop hooks never loop on dead claims.
            if s["lease_until"]:
                try:
                    if scenario_runs._parse_iso(s["lease_until"]) <= now_dt:
                        continue
                except (ValueError, TypeError):
                    pass
            item["lease_until"] = s["lease_until"]
            item["stop_continuations"] = int(s["stop_continuations"] or 0)
            claimed_steps.append(item)
            touched_runs.add(s["run_id"])
        elif s["status"] == "waiting_human" and (
            (chat and run["created_by_chat"] == chat)
            or (project_id is not None and run["project_id"] == project_id and run["run_kind"] == "prod")
        ):
            waiting_human.append(item)
            touched_runs.add(s["run_id"])
        elif s["status"] == "ready" and s["executor_role"] == "agent":
            if agent and s["executor_agent"] == agent and (
                run["run_kind"] == "prod" or (chat and run["created_by_chat"] == chat)
            ):
                item["executor_agent"] = s["executor_agent"]
                agent_ready_steps.append(item)
                touched_runs.add(s["run_id"])
            elif chat and run["created_by_chat"] == chat and (s["executor_agent"] is None or s["executor_agent"] == agent):
                if s["executor_agent"]:
                    item["executor_agent"] = s["executor_agent"]
                agent_ready_steps.append(item)
                touched_runs.add(s["run_id"])

    active_runs = [
        {
            "run_id": rid,
            "scenario_id": runs_by_id[rid]["scenario_id"],
            "project_id": runs_by_id[rid]["project_id"],
            "run_kind": runs_by_id[rid]["run_kind"],
            "status": runs_by_id[rid]["status"],
            "updated_at": runs_by_id[rid]["updated_at"],
        }
        for rid in runs_by_id
        if rid in touched_runs
    ]
    return {
        "active_runs": active_runs,
        "claimed_steps": claimed_steps,
        "waiting_human": waiting_human,
        "agent_ready_steps": agent_ready_steps,
    }


def session_start(conn, api, *, chat=None, agent=None, project_id=None, entries=None, now_override=None):
    """Build SessionStart context for active scenario runs relevant to this chat or agent."""
    author = rules.current_author(entries=entries) if (chat is None or agent is None) else rules.Author(chat, agent)
    chat_id = chat or author.by
    agent_id = agent or author.agent
    now_dt = scenario_runs._now_dt(api, now_override)
    ctx = _load_active_context(conn, chat=chat_id, agent=agent_id, project_id=project_id, now_dt=now_dt)

    lines = []
    if ctx["claimed_steps"]:
        parts = [f"{s['run_id']} step {s['step_key']} ({s['title']})" for s in ctx["claimed_steps"][:5]]
        lines.append("Claimed scenario step(s) for this chat: " + "; ".join(parts)
                     + ". Finish and submit `richi scenario report <RUN> <STEP> --json -` or pause with `richi scenario pause <RUN>`.")
    if ctx["agent_ready_steps"]:
        parts = [f"{s['run_id']} step {s['step_key']} ({s['title']})" for s in ctx["agent_ready_steps"][:5]]
        lines.append("Ready scenario step(s) available: " + "; ".join(parts)
                     + ". Continue with `richi scenario next <RUN> --claim`.")
    if ctx["waiting_human"]:
        parts = [f"{s['run_id']} gate {s['step_key']} ({s['title']})" for s in ctx["waiting_human"][:5]]
        lines.append("Waiting for human answer: " + "; ".join(parts)
                     + ". Ask the user and record with `richi scenario answer <RUN> <STEP> VALUE`.")

    return {
        "view": "scenario-hook",
        "event": "session-start",
        "chat": chat_id,
        "agent": agent_id,
        "project": project_id,
        **ctx,
        "context_note": "\n".join(lines),
    }


def stop_check(conn, api, *, chat=None, agent=None, project_id=None, project_path=None,
               max_continuations=DEFAULT_MAX_CONTINUATIONS, now_override=None):
    """Check whether this chat may stop its turn, enforcing claimed steps with a Richi-side counter."""
    author = rules.current_author() if (chat is None or agent is None) else rules.Author(chat, agent)
    chat_id = chat or author.by
    agent_id = agent or author.agent
    if not isinstance(max_continuations, int) or not 1 <= max_continuations <= MAX_CONTINUATIONS_LIMIT:
        raise api.MemoryError(f"--max-continuations must be between 1 and {MAX_CONTINUATIONS_LIMIT}")

    base = {
        "view": "scenario-hook",
        "event": "stop",
        "chat": chat_id,
        "agent": agent_id,
        "max_continuations": max_continuations,
    }
    if not _has_tables(conn):
        return {**base, "decision": "allow", "enforced": False}
    if not _enforcement_enabled(conn):
        return {**base, "decision": "allow", "enforced": False}

    now_dt = scenario_runs._now_dt(api, now_override)
    ctx = _load_active_context(conn, chat=chat_id, agent=agent_id, project_id=project_id, now_dt=now_dt)
    if ctx["claimed_steps"]:
        step = ctx["claimed_steps"][0]
        count = step["stop_continuations"]
        at_iso = _now_iso(api, now_override)
        if count >= max_continuations:
            scenario_runs._record_event(
                conn, at_iso, step["scenario_id"], step["run_id"], step["step_key"],
                "stop_limit_reached", chat_id, agent_id,
                {"continuations": count, "max_continuations": max_continuations},
            )
            return {
                **base,
                "decision": "allow",
                "enforced": True,
                "limit_reached": True,
                "run_id": step["run_id"],
                "step_key": step["step_key"],
                "continuations": count,
                "note": (
                    f"Stop enforcement limit ({max_continuations}) reached for {step['run_id']} step {step['step_key']}. "
                    "Allowing stop; tell the user the current status or pause the run."
                ),
            }
        new_count = count + 1
        conn.execute(
            "UPDATE scenario_steps SET stop_continuations = ? WHERE run_id = ? AND step_key = ?",
            (new_count, step["run_id"], step["step_key"]),
        )
        scenario_runs._record_event(
            conn, at_iso, step["scenario_id"], step["run_id"], step["step_key"],
            "stop_enforced", chat_id, agent_id,
            {"continuations": new_count, "max_continuations": max_continuations},
        )
        prefix = f"richi --project-path {shlex.quote(str(project_path))} " if (project_path and step.get("project_id")) else "richi "
        next_cmd = f"{prefix}scenario report {step['run_id']} {step['step_key']} --json -"
        reason = (
            f"Scenario run {step['run_id']} still has claimed step '{step['step_key']}' ({step['title']}). "
            f"Finish it and submit `{next_cmd}`, or pause explicitly with `{prefix}scenario pause {step['run_id']}` "
            f"(continuation {new_count}/{max_continuations})."
        )
        return {
            **base,
            "decision": "block",
            "enforced": True,
            "run_id": step["run_id"],
            "step_key": step["step_key"],
            "continuations": new_count,
            "next_command": next_cmd,
            "reason": reason,
        }

    if ctx["waiting_human"]:
        gates = [f"{g['run_id']} {g['step_key']} ({g['title']})" for g in ctx["waiting_human"][:5]]
        return {
            **base,
            "decision": "allow",
            "enforced": True,
            "waiting_human": ctx["waiting_human"],
            "note": "Waiting for human gate answer(s): " + "; ".join(gates)
                    + ". Present the question(s) clearly to the user and wait for their reply.",
        }

    return {**base, "decision": "allow", "enforced": True}


def pre_compact(conn, api, *, chat=None, agent=None, project_id=None, now_override=None):
    """Produce a compact state handoff block to preserve active scenario progress across compaction."""
    author = rules.current_author() if (chat is None or agent is None) else rules.Author(chat, agent)
    chat_id = chat or author.by
    agent_id = agent or author.agent
    now_dt = scenario_runs._now_dt(api, now_override)
    ctx = _load_active_context(conn, chat=chat_id, agent=agent_id, project_id=project_id, now_dt=now_dt)

    lines = []
    for run in ctx["active_runs"]:
        rid = run["run_id"]
        claimed = [s["step_key"] for s in ctx["claimed_steps"] if s["run_id"] == rid]
        ready = [s["step_key"] for s in ctx["agent_ready_steps"] if s["run_id"] == rid]
        waiting = [s["step_key"] for s in ctx["waiting_human"] if s["run_id"] == rid]
        summary_bits = [f"scenario={run['scenario_id']}", f"status={run['status']}"]
        if claimed:
            summary_bits.append("claimed=" + ",".join(claimed))
        if ready:
            summary_bits.append("ready=" + ",".join(ready))
        if waiting:
            summary_bits.append("waiting_human=" + ",".join(waiting))
        lines.append(f"- {rid} ({', '.join(summary_bits)}): resume with `richi scenario status {rid}` and `richi scenario next {rid} --claim`.")

    handoff = ("Active Richi scenario run(s) to preserve after context compaction:\n" + "\n".join(lines)) if lines else ""
    return {
        "view": "scenario-hook",
        "event": "pre-compact",
        "chat": chat_id,
        "agent": agent_id,
        "project": project_id,
        **ctx,
        "handoff_summary": handoff,
    }


def format_agent_output(event, result, agent_format):
    """Translate a Richi hook result into an agent's native hook JSON contract."""
    if agent_format == "richi":
        return result
    if event == "stop":
        if result.get("decision") == "block":
            if agent_format == "cursor":
                return {"followup_message": result["reason"]}
            return {"decision": "block", "reason": result["reason"]}
        return {}
    note = result.get("context_note") if event == "session-start" else result.get("handoff_summary")
    if not note:
        return {}
    if agent_format == "cursor":
        return {"additional_context": note}
    hook_name = "SessionStart" if event == "session-start" else "PreCompact"
    return {"hookSpecificOutput": {"hookEventName": hook_name, "additionalContext": note}}
