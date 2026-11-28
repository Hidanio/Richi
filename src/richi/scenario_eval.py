"""Phase 3 scenario workflows: DAG fork (pin data), trust-bound Git export/import, and CLI Golden Eval.

* `fork_run`: creates an isolated `run_kind='fork'` run copying compatible terminal non-descendant steps
  strictly by DAG `needs` and recomputing `from_step`, its descendants, and any non-terminal/changed siblings.
* `export_scenario` / `import_scenario`: portable JSON export/import with Security Summary (including
  `instruction` steps) and SHA-256 trust bound to `(scope, project_id, sha256)` (never blind `--trust`).
* `golden_*` / `eval_start` / `eval_score`: fixed-commit (hex SHA) golden bug benchmarks with memory
  isolation modes (`clean | as_of_commit | with_fp_only`), 1-to-1 severity-agnostic matching, `extra_unjudged`,
  and Paired Bug Diff.
"""
from datetime import datetime, timezone
import json
import re

from . import rules, scenario_findings, scenario_format, scenario_registry, scenario_review, scenario_runs, scenarios


TRUST_PREFIX = "scenario-trust:"
TRUST_V2_PREFIX = "scenario-trust-v2:"
GOLDEN_PREFIX = "scenario-golden:"
MEMORY_MODES = ("clean", "as_of_commit", "with_fp_only")
MAX_GOLDEN_ITEMS = 50
MAX_GOLDEN_BYTES = 64 * 1024
_LINE_SLOP = 5
_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{7,64}\Z")


def _resolve_safe(conn, api, scenario_id, project_id, *, scope=None):
    try:
        return scenarios.resolve(conn, scenario_id, project_id, scope=scope)
    except scenario_format.UnsupportedVersion as exc:
        raise scenario_runs._failure(api, str(exc), "unsupported_scenario") from exc
    except ValueError as exc:
        raise scenario_runs._failure(api, str(exc), "invalid_scenario") from exc


def _check_project_binding(target_project, checked_project, api, subject):
    if checked_project is not None and target_project is not None and target_project != checked_project:
        raise scenario_runs._failure(
            api,
            f"{subject} belongs to project '{target_project}', but --project-path is bound to '{checked_project}'",
            "project_mismatch",
        )


# ---------------------------------------------------------------- 1. Fork (Pin Data)


def _step_rows_terminal(step_spec, rows):
    if not rows:
        return False
    allow_failed = bool(step_spec.get("continue_on_failure"))
    for r in rows:
        if r["status"] in ("done", "skipped"):
            continue
        if r["status"] == "failed" and allow_failed:
            continue
        return False
    return True


def _step_shape_compatible(step_spec, rows):
    if not rows:
        return False
    if any(r["type"] != step_spec["type"] for r in rows):
        return False
    want_matrix = "matrix" in step_spec
    has_matrix = any(r["matrix_index"] is not None for r in rows)
    if want_matrix and not has_matrix:
        # A conditional matrix step whose `if` evaluated false before expansion stays a single skipped row.
        return len(rows) == 1 and rows[0]["status"] == "skipped" and rows[0]["matrix_index"] is None
    if not want_matrix and has_matrix:
        return False
    return True


def fork_run(conn, api, database, source_run_id, from_step_id, *, use_latest=False, dry_run=False,
             checked_project=None, project_path=None, author=None, now_override=None):
    """Fork a scenario run from `from_step_id`, freezing compatible terminal non-descendant steps by DAG `needs`."""
    scenario_runs.require_v4(conn, api)
    author = author or rules.current_author()
    at_dt = scenario_runs._now_dt(api, now_override)
    at_iso = scenario_runs._format_iso(at_dt)

    src_row = scenario_runs._load_run(conn, source_run_id, api)
    scenario_runs._check_run_project(src_row, checked_project, api)
    src_steps = scenario_runs._load_steps(conn, source_run_id)
    src_def = json.loads(src_row["definition_json"])
    src_meta = scenario_runs.get_run_meta(conn, source_run_id)

    if use_latest:
        resolved = _resolve_safe(conn, api, src_row["scenario_id"], src_row["project_id"])
        if resolved is None:
            raise api.NotFoundError("Cannot resolve latest definition for scenario: " + src_row["scenario_id"])
        definition = resolved["definition"]
        scn_version = int(resolved.get("version") or src_row["scenario_version"])
        target_scope = resolved["scope"]
    else:
        definition = src_def
        scn_version = int(src_row["scenario_version"])
        target_scope = src_row["scope"]

    by_def = {s["id"]: s for s in definition["steps"]}
    if from_step_id not in by_def:
        raise api.NotFoundError(f"Unknown step '{from_step_id}' in scenario {src_row['scenario_id']}")

    if src_row["project_id"] is None and any(
        c["kind"] in scenario_registry.PROJECT_BOUND_CHECKS
        for step in definition.get("steps") or []
        for c in step.get("checks") or []
    ):
        raise scenario_runs._failure(
            api,
            f"Scenario {src_row['scenario_id']} uses project-bound checks and requires a project-scoped run",
            "invalid_input",
        )

    # Re-resolve inputs when --use-latest so new defaults are populated and missing newly required inputs fail fast
    try:
        stored_inputs = json.loads(src_row["inputs_json"] or "{}")
    except (ValueError, TypeError):
        stored_inputs = {}
    if use_latest:
        old_specs = src_def.get("inputs") or {}
        new_specs = definition.get("inputs") or {}
        cleaned_inputs = {}
        for k, v in (stored_inputs.items() if isinstance(stored_inputs, dict) else ()):
            old_is_sec = isinstance(old_specs.get(k), dict) and old_specs[k].get("type") == "secret"
            new_is_sec = isinstance(new_specs.get(k), dict) and new_specs[k].get("type") == "secret"
            # Drop inputs whose secret/non-secret classification changed so "<secret>" is never persisted as literal text.
            if old_is_sec != new_is_sec:
                continue
            cleaned_inputs[k] = v
        try:
            _, redacted_inputs, missing_inputs, _ = scenarios.resolve_inputs(definition, cleaned_inputs)
        except scenario_format.FormatError as exc:
            raise scenario_runs._failure(api, str(exc), "invalid_input") from exc
        if missing_inputs:
            raise scenario_runs._failure(
                api, "Latest definition requires new input(s): " + ", ".join(missing_inputs), "missing_input"
            )
        inputs_json_str = json.dumps(redacted_inputs, ensure_ascii=False, separators=(",", ":"))
    else:
        inputs_json_str = src_row["inputs_json"]

    ancestry = scenario_format.ancestors(definition)
    src_by_step_id = {}
    for row in src_steps:
        src_by_step_id.setdefault(row["step_id"], []).append(row)

    # Verify every prerequisite ancestor of from_step_id is terminal and compatible in source run
    for anc_id in sorted(ancestry[from_step_id]):
        anc_rows = src_by_step_id.get(anc_id) or []
        anc_spec = by_def[anc_id]
        if not anc_rows:
            raise scenario_runs._failure(
                api, f"Cannot fork from {from_step_id}: prerequisite step '{anc_id}' is missing in source run {source_run_id}",
                "invalid_state",
            )
        if not _step_shape_compatible(anc_spec, anc_rows):
            raise scenario_runs._failure(
                api, f"Cannot fork from {from_step_id}: prerequisite step '{anc_id}' changed type/matrix shape in latest definition",
                "invalid_state",
            )
        if not _step_rows_terminal(anc_spec, anc_rows):
            raise scenario_runs._failure(
                api, f"Cannot fork from {from_step_id}: prerequisite step '{anc_id}' is not terminal in source run",
                "invalid_state",
            )

    # Determine recompute set: from_step, its descendants, plus any non-terminal / missing / shape-changed siblings
    # and their descendants — so --dry-run and actual execution agree 100% and never create zombie claimed steps!
    base_recompute = {from_step_id}
    for s in definition["steps"]:
        sid = s["id"]
        rows = src_by_step_id.get(sid) or []
        if not rows or not _step_shape_compatible(s, rows) or not _step_rows_terminal(s, rows):
            base_recompute.add(sid)

    recompute_ids = set(base_recompute)
    for sid, anc in ancestry.items():
        if anc & base_recompute:
            recompute_ids.add(sid)

    frozen_ids = [s["id"] for s in definition["steps"] if s["id"] not in recompute_ids]
    recompute_ordered = [s["id"] for s in definition["steps"] if s["id"] in recompute_ids]

    if dry_run:
        return {
            "view": "scenario-fork-preview",
            "dry_run": True,
            "source_run_id": source_run_id,
            "scenario_id": src_row["scenario_id"],
            "scenario_version": scn_version,
            "use_latest": bool(use_latest),
            "from_step": from_step_id,
            "frozen_steps": frozen_ids,
            "recompute_steps": recompute_ordered,
        }

    run_id = scenario_runs._next_run_id(conn, src_row["scenario_id"], at_dt)
    conn.execute(
        "INSERT INTO scenario_runs(id, scenario_id, scope, project_id, scenario_version, scenario_sha256, definition_json, "
        "inputs_json, status, task_entry_id, created_by_chat, created_by_agent, created_at, updated_at, revision) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', NULL, ?, ?, ?, ?, 1)",
        (
            run_id, src_row["scenario_id"], target_scope, src_row["project_id"], scn_version,
            scenario_format.digest(definition),
            scenario_format.canonical_json(definition), inputs_json_str,
            author.by, author.agent, at_iso, at_iso,
        ),
    )
    src_is_eval = src_meta.get("kind") == "eval" or src_meta.get("case_id") is not None
    fork_meta = {
        "kind": "eval" if src_is_eval else "fork",
        "forked": True,
        "parent_run_id": source_run_id,
        "fork_from": from_step_id,
        "use_latest": bool(use_latest),
    }
    # Preserve eval memory isolation if forking from an eval run
    for inherit_key in ("memory_mode", "case_id", "commit_time"):
        if src_meta.get(inherit_key) is not None:
            fork_meta[inherit_key] = src_meta[inherit_key]
    scenario_runs.set_run_meta(conn, run_id, fork_meta)

    frozen_set = set(frozen_ids)
    for pos, step in enumerate(definition["steps"]):
        sid = step["id"]
        if sid in frozen_set:
            for src_r in src_by_step_id[sid]:
                conn.execute(
                    "INSERT INTO scenario_steps(run_id, step_key, step_id, position, matrix_index, matrix_json, "
                    "type, executor_role, executor_agent, status, claimed_by_chat, claimed_by_agent, lease_until, "
                    "attempts, rejections, result_json, reason, started_at, finished_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id, src_r["step_key"], src_r["step_id"], pos, src_r["matrix_index"], src_r["matrix_json"],
                        src_r["type"], src_r["executor_role"], src_r["executor_agent"], src_r["status"],
                        src_r["attempts"], src_r["rejections"], src_r["result_json"], src_r["reason"],
                        src_r["started_at"], src_r["finished_at"],
                    ),
                )
        else:
            role, pinned = scenario_format.executor(step)
            conn.execute(
                "INSERT INTO scenario_steps(run_id, step_key, step_id, position, matrix_index, matrix_json, "
                "type, executor_role, executor_agent, status) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, 'pending')",
                (run_id, sid, sid, pos, step["type"], role, pinned),
            )

    scenario_runs._record_event(
        conn, at_iso, src_row["scenario_id"], run_id, None, "run_forked",
        author.by, author.agent,
        {"source_run_id": source_run_id, "from_step": from_step_id, "frozen": frozen_ids, "recompute": recompute_ordered},
    )
    run_row, step_rows, _ = scenario_runs._advance(
        conn, api, database, run_id, project_path=project_path, agent=author.agent, now_override=at_dt,
    )
    out = scenario_runs._format_status(
        run_row, step_rows, definition, view="scenario-fork", run_meta=scenario_runs.get_run_meta(conn, run_id),
    )
    out["source_run_id"] = source_run_id
    out["from_step"] = from_step_id
    out["frozen_steps"] = frozen_ids
    out["recompute_steps"] = recompute_ordered
    return out


# ---------------------------------------------------------------- 2. Export / Import with SHA-256 Trust


def security_summary(definition):
    """Inspect a normalized scenario definition for all agent-executable, prompt, and memory surfaces."""
    commands = []
    skills = []
    instructions = []
    memory_records = []
    memory_reads = []
    human_prompts = []
    other_steps = []
    checks = set()
    for step in definition.get("steps") or ():
        stype = step.get("type")
        params = step.get("params") or {}
        if stype == "command":
            cmd_item = {"step_id": step["id"], "command": params.get("command", "")}
            if params.get("cwd"):
                cmd_item["cwd"] = params["cwd"]
            commands.append(cmd_item)
        elif stype == "skill":
            sk_item = {"step_id": step["id"], "skill": params.get("skill")}
            sk_inst = str(params.get("instruction") or "").strip()
            if sk_inst:
                sk_item["instruction_preview"] = sk_inst[:140]
                sk_item["instruction_chars"] = len(sk_inst)
                sk_item["instruction_truncated"] = len(sk_inst) > 140
            skills.append(sk_item)
        elif stype == "instruction":
            txt = str(params.get("text") or "").strip()
            instructions.append({
                "step_id": step["id"],
                "preview": txt[:140],
                "chars": len(txt),
                "truncated": len(txt) > 140,
            })
        elif stype == "memory.record":
            sum_txt = str(params.get("summary") or "").strip()
            rec_item = {
                "step_id": step["id"],
                "kind": params.get("kind"),
                "knowledge_state": params.get("knowledge_state", "confirmed"),
                "title": str(params.get("title") or "")[:140],
                "summary_preview": sum_txt[:140],
                "summary_chars": len(sum_txt),
                "summary_truncated": len(sum_txt) > 140,
            }
            if params.get("entry_id"):
                rec_item["entry_id"] = params["entry_id"]
            if params.get("tags"):
                rec_item["tags"] = list(params["tags"])
            memory_records.append(rec_item)
        elif stype == "memory.recall":
            q_txt = str(params.get("query") or "").strip()
            rd_item = {
                "step_id": step["id"],
                "type": stype,
                "query_preview": q_txt[:140],
                "query_chars": len(q_txt),
                "query_truncated": len(q_txt) > 140,
            }
            if params.get("kinds"):
                rd_item["kinds"] = list(params["kinds"])
            memory_reads.append(rd_item)
        elif stype == "memory.brief":
            q_txt = str(params.get("question") or "").strip()
            memory_reads.append({
                "step_id": step["id"],
                "type": stype,
                "question_preview": q_txt[:140],
                "question_chars": len(q_txt),
                "question_truncated": len(q_txt) > 140,
                "include_past_runs": params.get("include_past_runs", True),
                "include_learnings": bool(params.get("include_learnings", False)),
            })
        elif stype == "gate":
            q_txt = str(params.get("question") or "").strip()
            hp_item = {
                "step_id": step["id"],
                "type": stype,
                "preview": q_txt[:140],
                "chars": len(q_txt),
                "truncated": len(q_txt) > 140,
            }
            if params.get("options"):
                hp_item["options"] = [str(o)[:80] for o in list(params["options"])[:10]]
            human_prompts.append(hp_item)
        elif stype == "checklist":
            item_texts = [
                str(it.get("text") if isinstance(it, dict) else it or "").strip()
                for it in (params.get("items") or ())
            ]
            joined = " | ".join(t for t in item_texts if t)
            human_prompts.append({
                "step_id": step["id"],
                "type": stype,
                "preview": joined[:140],
                "chars": len(joined),
                "truncated": len(joined) > 140,
                "item_count": len(item_texts),
            })
        elif stype != "condition":
            # Fail-closed for compare (reads historical runs) and any future/unknown step type.
            other_steps.append({"step_id": step["id"], "type": str(stype or "")})
        for chk in step.get("checks") or ():
            if chk.get("kind"):
                checks.add(chk["kind"])
    secrets = sorted(
        name for name, spec in (definition.get("inputs") or {}).items()
        if isinstance(spec, dict) and spec.get("type") == "secret"
    )
    wtu = str(definition.get("when_to_use") or "").strip()
    out = {
        "commands": commands,
        "skills": skills,
        "instructions": instructions,
        "memory_records": memory_records,
        "memory_reads": memory_reads,
        "human_prompts": human_prompts,
        "other_steps": other_steps,
        "secrets": secrets,
        "checks": sorted(checks),
        "requires_trust": bool(
            commands or skills or instructions or memory_records
            or memory_reads or human_prompts or other_steps or secrets or wtu
        ),
    }
    if wtu:
        out["when_to_use"] = wtu[:200]
        out["when_to_use_chars"] = len(wtu)
        out["when_to_use_truncated"] = len(wtu) > 200
    return out


def _trust_key(project_id, sha256):
    if project_id is None:
        return f"{TRUST_V2_PREFIX}global:{sha256}"
    return f"{TRUST_V2_PREFIX}project:{len(project_id)}:{project_id}:{sha256}"


def _r4_trust_key(project_id, sha256):
    if project_id is None:
        return f"{TRUST_PREFIX}global:{sha256}"
    return f"{TRUST_PREFIX}project:{len(project_id)}:{project_id}:{sha256}"


def _legacy_trust_key(project_id, sha256):
    return f"{TRUST_PREFIX}{project_id or '_global'}:{sha256}"


def _is_trusted(conn, eff_project, sha):
    """Check trust across v2 and legacy keys while strictly verifying stored JSON project_id and sha256."""
    keys = [_trust_key(eff_project, sha), _r4_trust_key(eff_project, sha)]
    if eff_project != "_global":
        keys.append(_legacy_trust_key(eff_project, sha))
    for k in keys:
        row = conn.execute("SELECT value FROM metadata WHERE key = ?", (k,)).fetchone()
        if row is None or not row[0]:
            continue
        try:
            parsed = json.loads(row[0])
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and parsed.get("project_id") == eff_project and parsed.get("sha256") == sha:
            return True
    return False


def export_scenario(conn, api, scenario_id, *, project_id=None, scope=None, checked_project=None):
    """Export one stored scenario or built-in template with its canonical SHA-256 and security summary."""
    eff_project = project_id if project_id is not None else checked_project
    if project_id is not None:
        api.require_project(conn, project_id)
    if scope == "builtin":
        rec = scenarios.builtin_templates().get(scenario_id)
    elif scope == "global":
        rec = _resolve_safe(conn, api, scenario_id, None, scope="global")
    else:
        rec = _resolve_safe(conn, api, scenario_id, eff_project)
    if rec is None:
        raise api.NotFoundError("Unknown scenario: " + scenario_id)
    defn = rec["definition"]
    sha = scenario_format.digest(defn)
    return {
        "view": "scenario-export",
        "id": defn["id"],
        "scope": rec["scope"],
        "project_id": rec.get("project_id"),
        "sha256": sha,
        "security_summary": security_summary(defn),
        "definition": defn,
    }


def import_scenario(conn, api, raw_definition, *, scope="project", project_id=None, checked_project=None,
                    trust_sha256=None, replace=False, author=None):
    """Import a scenario JSON definition with canonical SHA-256 trust bound to (scope, project_id, sha256)."""
    author = author or rules.current_author()
    if scope == "project":
        eff_project = project_id if project_id is not None else checked_project
        if not eff_project:
            raise scenario_runs._failure(api, "Project import requires --project ID or --project-path", "invalid_input")
        api.require_project(conn, eff_project)
        _check_project_binding(eff_project, checked_project, api, "Scenario import")
    else:
        eff_project = None

    try:
        defn = scenario_format.normalize(raw_definition)
    except scenario_format.FormatError as exc:
        raise scenario_runs._failure(api, str(exc), "invalid_scenario") from exc

    sha = scenario_format.digest(defn)
    sec = security_summary(defn)
    tkey = _trust_key(eff_project, sha)
    already_trusted = _is_trusted(conn, eff_project, sha)

    # Always verify an explicitly supplied --trust-sha256 pin even if already trusted or trust-exempt.
    if trust_sha256 is not None:
        provided = str(trust_sha256).strip().lower()
        if not provided or provided != sha:
            raise scenario_runs._failure(
                api, f"Digest mismatch: scenario SHA-256 is {sha}, but --trust-sha256 was {provided[:80] or '<empty>'}",
                "digest_mismatch",
            )

    should_record_trust = False
    if sec["requires_trust"] and not already_trusted:
        if trust_sha256 is None:
            return {
                "view": "scenario-import",
                "status": "trust_required",
                "id": defn["id"],
                "scope": scope,
                "project_id": eff_project,
                "sha256": sha,
                "security_summary": sec,
                "note": "Review security_summary and re-run with --trust-sha256 " + sha,
            }
        should_record_trust = True

    existing_key = scenarios.storage_key(scope, defn["id"], eff_project)
    existing_row = conn.execute("SELECT CAST(value AS BLOB) FROM metadata WHERE key = ?", (existing_key,)).fetchone()
    if existing_row is not None:
        if not replace:
            raise scenario_runs._failure(
                api, f"Scenario '{defn['id']}' already exists in {scope} scope; pass --replace to update it",
                "conflict",
            )
        expected_updated_at = scenarios._stored_version(existing_row[0])
        req = {
            "action": "update",
            "id": defn["id"],
            "scope": scope,
            "definition": defn,
            "expected_updated_at": expected_updated_at,
            "source": "import:" + sha[:12],
        }
    else:
        req = {
            "action": "add",
            "scope": scope,
            "definition": defn,
            "source": "import:" + sha[:12],
        }
    if scope == "project":
        req["project"] = eff_project

    res = scenarios.change(conn, req, api, checked_project=checked_project, author=author)
    if should_record_trust:
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
            (tkey, json.dumps({"sha256": sha, "project_id": eff_project, "scenario_id": defn["id"], "by": author.by})),
        )
    res["view"] = "scenario-import"
    res["sha256"] = sha
    res["security_summary"] = sec
    return res


# ---------------------------------------------------------------- 3. Golden Cases & CLI Eval


def _golden_key(case_id):
    return GOLDEN_PREFIX + str(case_id)


def _validate_golden_bug_list(items, label, api, *, required=False):
    if items is None:
        if required:
            raise scenario_runs._failure(api, f"Golden case requires non-empty {label}", "invalid_input")
        return []
    if not isinstance(items, list) or len(items) > MAX_GOLDEN_ITEMS or (required and not items):
        raise scenario_runs._failure(api, f"{label} must be a list of 1..{MAX_GOLDEN_ITEMS} objects", "invalid_input")
    cleaned = []
    seen_ids = set()
    for idx, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise scenario_runs._failure(api, f"{label}[{idx}] must be an object", "invalid_input")
        bid = str(raw.get("id") or f"{label}-{idx + 1}").strip()
        if not bid or len(bid) > 80 or bid in seen_ids:
            raise scenario_runs._failure(api, f"{label}[{idx}].id must be unique non-empty text <= 80 chars", "invalid_input")
        seen_ids.add(bid)
        rel, err = scenario_findings._safe_rel_repo_path(raw.get("file"))
        if err:
            raise scenario_runs._failure(api, f"{label}[{idx}].file: {err}", "invalid_input")
        lr = raw.get("line_range")
        if lr is not None:
            if (not isinstance(lr, list) or len(lr) != 2
                    or any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in lr) or lr[1] < lr[0]):
                raise scenario_runs._failure(api, f"{label}[{idx}].line_range must be [start, end] positive ints", "invalid_input")
            lr = [lr[0], lr[1]]
        syms = raw.get("symbols") or []
        if not isinstance(syms, list) or len(syms) > 20 or not all(isinstance(s, str) and s.strip() for s in syms):
            raise scenario_runs._failure(api, f"{label}[{idx}].symbols must be a list of <= 20 strings", "invalid_input")
        syms = [s.strip().lower() for s in syms]
        if lr is None and not syms:
            raise scenario_runs._failure(api, f"{label}[{idx}] needs line_range and/or symbols", "invalid_input")
        desc = str(raw.get("description") or "").strip()[:500]
        cleaned.append({"id": bid, "file": rel, "line_range": lr, "symbols": syms, "description": desc})
    return cleaned


def golden_add(conn, api, case_obj, *, checked_project=None, author=None, now_override=None):
    """Add or update a fixed-commit (hex SHA) golden bug benchmark case."""
    author = author or rules.current_author()
    at_iso = scenario_runs._format_iso(scenario_runs._now_dt(api, now_override))
    if not isinstance(case_obj, dict):
        raise scenario_runs._failure(api, "Golden case must be a JSON object", "invalid_input")
    cid = case_obj.get("id")
    if not isinstance(cid, str) or not scenario_format.SCENARIO_ID.fullmatch(cid):
        raise scenario_runs._failure(api, scenario_format.SCENARIO_ID_HELP, "invalid_input")
    title = str(case_obj.get("title") or cid).strip()[:200]
    project_id = case_obj.get("project_id") or checked_project
    if project_id is not None:
        api.require_project(conn, project_id)
        _check_project_binding(project_id, checked_project, api, f"Golden case '{cid}'")
    existing_row = conn.execute("SELECT value FROM metadata WHERE key = ?", (_golden_key(cid),)).fetchone()
    if existing_row is not None and existing_row[0]:
        try:
            existing_case = json.loads(existing_row[0])
        except (ValueError, TypeError):
            existing_case = None
        if isinstance(existing_case, dict):
            existing_proj = existing_case.get("project_id")
            _check_project_binding(existing_proj, checked_project, api, f"Existing golden case '{cid}'")
            if existing_proj != project_id:
                raise scenario_runs._failure(
                    api,
                    f"Golden case '{cid}' belongs to project '{existing_proj or 'global'}' and cannot be reassigned to '{project_id or 'global'}'",
                    "project_mismatch",
                )
    base_rev = str(case_obj.get("base_rev") or "").strip()
    head_rev = str(case_obj.get("head_rev") or "").strip()
    if not _COMMIT_SHA.fullmatch(base_rev) or not _COMMIT_SHA.fullmatch(head_rev):
        raise scenario_runs._failure(
            api, "Golden case base_rev and head_rev must be fixed Git commit SHAs (7-64 hex characters, not floating branch names)",
            "invalid_input",
        )
    expected_bugs = _validate_golden_bug_list(case_obj.get("expected_bugs"), "expected_bugs", api, required=True)
    known_traps = _validate_golden_bug_list(case_obj.get("known_traps"), "known_traps", api, required=False)
    as_of_iso = case_obj.get("commit_time")
    if as_of_iso is not None:
        if not isinstance(as_of_iso, str) or not as_of_iso.strip():
            raise scenario_runs._failure(api, "commit_time must be an ISO-8601 UTC timestamp with timezone", "invalid_input")
        try:
            parsed_dt = datetime.fromisoformat(as_of_iso.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise scenario_runs._failure(api, "commit_time must be an ISO-8601 timestamp", "invalid_input") from exc
        if parsed_dt.tzinfo is None:
            raise scenario_runs._failure(api, "commit_time must include a timezone offset or 'Z'", "invalid_input")
        as_of_iso = scenario_runs._format_iso(parsed_dt.astimezone(timezone.utc))

    stored = {
        "id": cid,
        "title": title,
        "project_id": project_id,
        "base_rev": base_rev,
        "head_rev": head_rev,
        "commit_time": as_of_iso,
        "expected_bugs": expected_bugs,
        "known_traps": known_traps,
        "updated_at": at_iso,
        "updated_by": author.by,
    }
    encoded = json.dumps(stored, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_GOLDEN_BYTES:
        raise scenario_runs._failure(api, f"Golden case exceeds {MAX_GOLDEN_BYTES} bytes", "limit")
    conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (_golden_key(cid), encoded))
    return {"view": "scenario-golden", "status": "saved", "case": stored}


def golden_get(conn, api, case_id):
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (_golden_key(case_id),)).fetchone()
    if row is None or not row[0]:
        raise api.NotFoundError("Unknown golden case: " + str(case_id))
    try:
        parsed = json.loads(row[0])
    except (ValueError, TypeError) as exc:
        raise scenario_runs._failure(api, f"Corrupt golden case '{case_id}'", "invalid_scenario") from exc
    if not isinstance(parsed, dict):
        raise scenario_runs._failure(api, f"Corrupt golden case '{case_id}'", "invalid_scenario")
    return parsed


def golden_list(conn, *, project_id=None, api=None):
    if project_id is not None and api is not None:
        api.require_project(conn, project_id)
    rows = conn.execute(
        "SELECT value FROM metadata WHERE key >= ? AND key < ? ORDER BY key",
        (GOLDEN_PREFIX, GOLDEN_PREFIX[:-1] + ";"),
    ).fetchall()
    cases = []
    for (raw,) in rows:
        try:
            c = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(c, dict):
            continue
        if project_id is not None and c.get("project_id") != project_id:
            continue
        cases.append(c)
    return {"view": "scenario-golden-list", "cases": cases}


def golden_remove(conn, api, case_id, *, checked_project=None):
    case = golden_get(conn, api, case_id)
    _check_project_binding(case.get("project_id"), checked_project, api, f"Golden case '{case_id}'")
    conn.execute("DELETE FROM metadata WHERE key = ?", (_golden_key(case_id),))
    return {"view": "scenario-golden", "status": "removed", "id": case_id}


def _match_quality(finding, spec):
    """Return match priority tuple (lower is better) or None if finding does not match spec.

    Severity-agnostic: requires same normalized file AND (line/end_line overlap within range±slop OR word-boundary symbol hit).
    """
    if scenario_findings._normalize_file(finding.get("file") or "") != spec["file"]:
        return None
    lr = spec.get("line_range")
    line = finding.get("line")
    end_line = finding.get("end_line")
    exact_line = False
    line_hit = False
    if lr is not None and isinstance(line, int) and not isinstance(line, bool):
        f_end = end_line if (isinstance(end_line, int) and not isinstance(end_line, bool) and end_line >= line) else line
        exact_line = line <= lr[1] and f_end >= lr[0]
        line_hit = line <= (lr[1] + _LINE_SLOP) and f_end >= (lr[0] - _LINE_SLOP)
    syms = set(spec.get("symbols") or ())
    sym_hit = False
    if syms:
        hay_text = (finding.get("title") or "") + " " + (finding.get("evidence") or "")
        hay_ids = scenario_findings._extract_identifiers(hay_text)
        hay_low = hay_text.lower()
        sym_hit = any(
            s in hay_ids or bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(s) + r"(?![A-Za-z0-9_])", hay_low))
            for s in syms
        )
    if lr is not None and syms:
        if not (line_hit or sym_hit):
            return None
        return (0 if (exact_line and sym_hit) else (1 if (line_hit and sym_hit) else (2 if line_hit else 3)),)
    if lr is not None:
        return (0 if exact_line else 1,) if line_hit else None
    return (0,) if sym_hit else None


def _min_cost_max_matching(specs, candidates):
    """Exact maximum-cardinality, minimum-cost bipartite matching (SPFA successive shortest paths, stdlib only)."""
    n_left = len(specs)
    ordered = sorted(candidates, key=lambda f: f["finding_id"])
    n_right = len(ordered)
    if not n_left or not n_right:
        return {}
    src = 0
    sink = n_left + n_right + 1
    n_nodes = sink + 1
    adj = [[] for _ in range(n_nodes)]

    def add_edge(u, v, cap, cost):
        adj[u].append([v, cap, cost, len(adj[v])])
        adj[v].append([u, 0, -cost, len(adj[u]) - 1])

    for i in range(n_left):
        add_edge(src, 1 + i, 1, 0)
    for j in range(n_right):
        add_edge(1 + n_left + j, sink, 1, 0)

    stride = max(n_right, 1)
    i_stride = n_left * stride + 1
    q_stride = n_left * n_left * i_stride + 1
    has_any = False
    for i, spec in enumerate(specs):
        for j, finding in enumerate(ordered):
            q = _match_quality(finding, spec)
            if q is not None:
                has_any = True
                weight = q[0] * q_stride + i * i_stride + j
                add_edge(1 + i, 1 + n_left + j, 1, weight)
    if not has_any:
        return {}

    inf = max(10**18, (4 * n_left + 1) * q_stride)
    while True:
        dist = [inf] * n_nodes
        parent_v = [-1] * n_nodes
        parent_e = [-1] * n_nodes
        in_q = [False] * n_nodes
        dist[src] = 0
        queue = [src]
        in_q[src] = True
        head = 0
        while head < len(queue):
            u = queue[head]
            head += 1
            in_q[u] = False
            du = dist[u]
            for idx, (v, cap, cost, _rev) in enumerate(adj[u]):
                if cap > 0 and du + cost < dist[v]:
                    dist[v] = du + cost
                    parent_v[v] = u
                    parent_e[v] = idx
                    if not in_q[v]:
                        queue.append(v)
                        in_q[v] = True
        if dist[sink] == inf:
            break
        cur = sink
        while cur != src:
            pv = parent_v[cur]
            pe = parent_e[cur]
            edge = adj[pv][pe]
            edge[1] -= 1
            adj[cur][edge[3]][1] += 1
            cur = pv

    matches = {}
    for i in range(n_left):
        for v, cap, _cost, _rev in adj[1 + i]:
            if 1 + n_left <= v < sink and cap == 0:
                matches[i] = ordered[v - 1 - n_left]
                break
    return matches


def _score_single_run(conn, api, case, run_id):
    run_row = scenario_runs._load_run(conn, run_id, api)
    defn = json.loads(run_row["definition_json"])
    step_rows = scenario_runs._load_steps(conn, run_id)
    deduped, _ = scenario_review._extract_run_findings(defn, step_rows)
    active = [f for f in deduped if f["status"] != "disproved"]

    # Strict 1-to-1 exact maximum-cardinality, minimum-cost bipartite matching
    matched_bug_indices = _min_cost_max_matching(case["expected_bugs"], active)
    matched_finding_ids = {f["finding_id"] for f in matched_bug_indices.values()}

    detected = []
    missed = []
    for exp_idx, exp in enumerate(case["expected_bugs"]):
        hit = matched_bug_indices.get(exp_idx)
        if hit is not None:
            detected.append({
                "bug_id": exp["id"],
                "file": exp["file"],
                "matched_finding_id": hit["finding_id"],
                "reported_severity": hit["severity"],
                "title": hit["title"],
            })
        else:
            missed.append({"bug_id": exp["id"], "file": exp["file"], "description": exp.get("description", "")})

    traps_list = list(case.get("known_traps") or ())
    remaining_active = [f for f in active if f["finding_id"] not in matched_finding_ids]
    matched_trap_indices = _min_cost_max_matching(traps_list, remaining_active)
    for f in matched_trap_indices.values():
        matched_finding_ids.add(f["finding_id"])

    traps_triggered = []
    for trap_idx, trap in enumerate(traps_list):
        hit = matched_trap_indices.get(trap_idx)
        if hit is not None:
            traps_triggered.append({
                "trap_id": trap["id"],
                "file": trap["file"],
                "matched_finding_id": hit["finding_id"],
                "title": hit["title"],
            })

    extra_unjudged = [
        {"finding_id": f["finding_id"], "file": f["file"], "line": f["line"], "severity": f["severity"], "title": f["title"]}
        for f in active if f["finding_id"] not in matched_finding_ids
    ]
    total_exp = len(case["expected_bugs"])
    recall = round(len(detected) / total_exp, 4) if total_exp else None
    result = {
        "run_id": run_id,
        "scenario_id": run_row["scenario_id"],
        "scenario_version": run_row["scenario_version"],
        "run_status": run_row["status"],
        "memory_mode": scenario_runs.get_run_meta(conn, run_id).get("memory_mode"),
        "expected_total": total_exp,
        "detected_count": len(detected),
        "missed_count": len(missed),
        "recall": recall,
        "traps_triggered_count": len(traps_triggered),
        "extra_unjudged_count": len(extra_unjudged),
        "detected": detected,
        "missed": missed,
        "traps_triggered": traps_triggered,
        "extra_unjudged": extra_unjudged,
    }
    if run_row["status"] != "done":
        result["warning"] = f"Run {run_id} has status '{run_row['status']}' (not 'done'); score may be incomplete"
    return result


def _validate_eval_score_run(conn, api, case_id, case, run_row, *, expected_scenario_id=None, checked_project=None):
    scenario_runs._check_run_project(run_row, checked_project, api)
    if case.get("project_id") and run_row["project_id"] != case["project_id"]:
        run_scope = f"project '{run_row['project_id']}'" if run_row["project_id"] else "workspace-wide (no project)"
        raise scenario_runs._failure(
            api, f"Run {run_row['id']} is {run_scope}, not case project '{case['project_id']}'",
            "project_mismatch",
        )
    if expected_scenario_id and run_row["scenario_id"] != expected_scenario_id:
        raise scenario_runs._failure(
            api, f"Run {run_row['id']} is for scenario '{run_row['scenario_id']}', not '{expected_scenario_id}'",
            "invalid_input",
        )
    meta = scenario_runs.get_run_meta(conn, run_row["id"])
    if meta.get("case_id") and meta["case_id"] != case_id:
        raise scenario_runs._failure(
            api, f"Run {run_row['id']} was evaluated on case '{meta['case_id']}', not '{case_id}'",
            "invalid_input",
        )
    if run_row["status"] in ("failed", "cancelled"):
        raise scenario_runs._failure(
            api, f"Cannot score {run_row['status']} run {run_row['id']}", "invalid_state"
        )


def eval_score(conn, api, case_id, run_id, *, compare_run_id=None, expected_scenario_id=None, checked_project=None):
    """Score a completed (or gate-waiting) eval run against a golden case, optionally with Paired Bug Diff."""
    scenario_runs.require_v4(conn, api)
    case = golden_get(conn, api, case_id)
    _check_project_binding(case.get("project_id"), checked_project, api, f"Golden case '{case_id}'")
    run_row = scenario_runs._load_run(conn, run_id, api)
    _validate_eval_score_run(
        conn, api, case_id, case, run_row,
        expected_scenario_id=expected_scenario_id, checked_project=checked_project,
    )
    current = _score_single_run(conn, api, case, run_id)
    out = {
        "view": "scenario-eval-score",
        "case_id": case_id,
        "case_title": case["title"],
        "result": current,
    }
    if compare_run_id:
        prev_row = scenario_runs._load_run(conn, compare_run_id, api)
        _validate_eval_score_run(
            conn, api, case_id, case, prev_row, checked_project=checked_project,
        )
        prev = _score_single_run(conn, api, case, compare_run_id)
        cur_caught = {d["bug_id"] for d in current["detected"]}
        prev_caught = {d["bug_id"] for d in prev["detected"]}
        all_bugs = [b["id"] for b in case["expected_bugs"]]
        out["baseline_run_id"] = compare_run_id
        out["baseline_recall"] = prev["recall"]
        out["paired_bug_diff"] = {
            "newly_caught": [b for b in all_bugs if b in cur_caught and b not in prev_caught],
            "regressed_missed": [b for b in all_bugs if b not in cur_caught and b in prev_caught],
            "still_caught": [b for b in all_bugs if b in cur_caught and b in prev_caught],
            "still_missed": [b for b in all_bugs if b not in cur_caught and b not in prev_caught],
            "delta_recall": round((current["recall"] or 0.0) - (prev["recall"] or 0.0), 4),
            "delta_traps": current["traps_triggered_count"] - prev["traps_triggered_count"],
        }
    return out


def eval_start(conn, api, database, scenario_id, case_id, *, memory_mode="clean",
               project_id=None, raw_inputs=None, inputs_json=None,
               checked_project=None, project_path=None, author=None, now_override=None):
    """Start an isolated `run_kind='eval'` run of `scenario_id` on a fixed-commit golden case."""
    scenario_runs.require_v4(conn, api)
    if memory_mode not in MEMORY_MODES:
        raise scenario_runs._failure(api, "memory_mode must be one of " + ", ".join(MEMORY_MODES), "invalid_input")
    case = golden_get(conn, api, case_id)
    if memory_mode == "as_of_commit" and not case.get("commit_time"):
        raise scenario_runs._failure(
            api, f"memory_mode=as_of_commit requires commit_time on golden case '{case_id}'", "invalid_input"
        )
    _check_project_binding(case.get("project_id"), checked_project, api, f"Golden case '{case_id}'")
    if project_id is not None and case.get("project_id") is not None and project_id != case["project_id"]:
        raise scenario_runs._failure(
            api,
            f"Golden case '{case_id}' belongs to project '{case['project_id']}', not --project '{project_id}'",
            "project_mismatch",
        )
    eff_project = project_id or case.get("project_id") or checked_project
    _check_project_binding(eff_project, checked_project, api, "Scenario eval")

    resolved = _resolve_safe(conn, api, scenario_id, eff_project)
    if resolved is None:
        raise api.NotFoundError("Unknown scenario: " + scenario_id)
    declared_inputs = resolved["definition"].get("inputs") or {}
    eval_inputs = {}
    defaults_map = {
        "base": case["base_rev"],
        "branch": case["head_rev"],
        "target": f"{case['base_rev']}...{case['head_rev']}",
        "focus": f"eval:{case_id}",
    }
    for key, val in defaults_map.items():
        spec = declared_inputs.get(key)
        if isinstance(spec, dict) and spec.get("type") in ("string", "text"):
            eval_inputs[key] = val
    if raw_inputs or inputs_json is not None:
        try:
            extra = scenarios.parse_input_options(resolved["definition"], raw_inputs, inputs_json)
        except scenario_format.FormatError as exc:
            raise scenario_runs._failure(api, str(exc), "invalid_input") from exc
        eval_inputs.update(extra)

    eval_meta = {
        "kind": "eval",
        "case_id": case_id,
        "memory_mode": memory_mode,
        "base_rev": case["base_rev"],
        "head_rev": case["head_rev"],
        "commit_time": case.get("commit_time"),
    }
    res = scenario_runs.start_run(
        conn, api, database, scenario_id,
        project_id=eff_project, project_path=project_path,
        inputs_json=eval_inputs, author=author, now_override=now_override,
        initial_run_meta=eval_meta,
    )
    res["view"] = "scenario-eval"
    res["case_id"] = case_id
    res["memory_mode"] = memory_mode
    res["base_rev"] = case["base_rev"]
    res["head_rev"] = case["head_rev"]
    return res
