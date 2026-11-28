"""Phase 3 scenario workflows: DAG fork (pin data), trust-bound Git export/import, and CLI Golden Eval.

* `fork_run`: creates an isolated `run_kind='fork'` run copying non-descendant steps strictly by DAG `needs`
  from a prior run and recomputing `from_step` and its transitive descendants.
* `export_scenario` / `import_scenario`: portable JSON export/import with Security Summary and SHA-256 trust
  bound to `(sha256, project_id)` (never blind `--trust`).
* `golden_*` / `eval_start` / `eval_score`: fixed-commit golden bug benchmarks with memory isolation modes
  (`clean | as_of_commit | with_fp_only`), severity-agnostic matching, `extra_unjudged`, and Paired Bug Diff.
"""
from datetime import datetime, timezone
import json
import re

from . import rules, scenario_findings, scenario_format, scenario_review, scenario_runs, scenarios


TRUST_PREFIX = "scenario-trust:"
GOLDEN_PREFIX = "scenario-golden:"
MEMORY_MODES = ("clean", "as_of_commit", "with_fp_only")
MAX_GOLDEN_ITEMS = 50
MAX_GOLDEN_BYTES = 64 * 1024
_LINE_SLOP = 5


# ---------------------------------------------------------------- 1. Fork (Pin Data)


def fork_run(conn, api, database, source_run_id, from_step_id, *, use_latest=False, dry_run=False,
             checked_project=None, project_path=None, author=None, now_override=None):
    """Fork a scenario run from `from_step_id`, freezing non-descendant steps strictly by DAG `needs`."""
    scenario_runs.require_v4(conn, api)
    author = author or rules.current_author()
    at_dt = scenario_runs._now_dt(api, now_override)
    at_iso = scenario_runs._format_iso(at_dt)

    src_row = scenario_runs._load_run(conn, source_run_id, api)
    scenario_runs._check_run_project(src_row, checked_project, api)
    src_steps = scenario_runs._load_steps(conn, source_run_id)
    src_def = json.loads(src_row["definition_json"])

    if use_latest:
        resolved = scenarios.resolve(conn, src_row["scenario_id"], src_row["project_id"])
        if resolved is None:
            raise api.NotFoundError("Cannot resolve latest definition for scenario: " + src_row["scenario_id"])
        definition = resolved["definition"]
        scn_version = int(resolved.get("version") or src_row["scenario_version"])
    else:
        definition = src_def
        scn_version = int(src_row["scenario_version"])

    by_def = {s["id"]: s for s in definition["steps"]}
    if from_step_id not in by_def:
        raise api.NotFoundError(f"Unknown step '{from_step_id}' in scenario {src_row['scenario_id']}")

    ancestry = scenario_format.ancestors(definition)
    recompute_ids = {from_step_id} | {sid for sid, anc in ancestry.items() if from_step_id in anc}
    frozen_ids = [s["id"] for s in definition["steps"] if s["id"] not in recompute_ids]
    recompute_ordered = [s["id"] for s in definition["steps"] if s["id"] in recompute_ids]

    # Verify every prerequisite ancestor of from_step_id is completed/skipped in source run
    src_by_step_id = {}
    for row in src_steps:
        src_by_step_id.setdefault(row["step_id"], []).append(row)

    for anc_id in sorted(ancestry[from_step_id]):
        anc_rows = src_by_step_id.get(anc_id) or []
        if not anc_rows:
            raise scenario_runs._failure(
                api, f"Cannot fork from {from_step_id}: prerequisite step '{anc_id}' is missing in source run {source_run_id}",
                "invalid_state",
            )
        if not all(r["status"] in ("done", "skipped") for r in anc_rows):
            raise scenario_runs._failure(
                api, f"Cannot fork from {from_step_id}: prerequisite step '{anc_id}' is not done/skipped in source run",
                "invalid_state",
            )

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
            run_id, src_row["scenario_id"], src_row["scope"], src_row["project_id"], scn_version,
            scenario_format.digest(definition),
            scenario_format.canonical_json(definition), src_row["inputs_json"],
            author.by, author.agent, at_iso, at_iso,
        ),
    )
    scenario_runs.set_run_meta(conn, run_id, {
        "kind": "fork",
        "parent_run_id": source_run_id,
        "fork_from": from_step_id,
        "use_latest": bool(use_latest),
    })

    for pos, step in enumerate(definition["steps"]):
        sid = step["id"]
        if sid in frozen_ids and sid in src_by_step_id:
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
    """Inspect a normalized scenario definition for executable or privileged surfaces."""
    commands = []
    skills = []
    memory_records = []
    checks = set()
    for step in definition.get("steps") or ():
        stype = step.get("type")
        params = step.get("params") or {}
        if stype == "command":
            commands.append({"step_id": step["id"], "command": params.get("command", "")})
        elif stype == "skill":
            skills.append({"step_id": step["id"], "skill": params.get("skill")})
        elif stype == "memory.record":
            memory_records.append({
                "step_id": step["id"],
                "kind": params.get("kind"),
                "knowledge_state": params.get("knowledge_state", "confirmed"),
            })
        for chk in step.get("checks") or ():
            if chk.get("kind"):
                checks.add(chk["kind"])
    secrets = sorted(
        name for name, spec in (definition.get("inputs") or {}).items()
        if isinstance(spec, dict) and spec.get("type") == "secret"
    )
    return {
        "commands": commands,
        "skills": skills,
        "memory_records": memory_records,
        "secrets": secrets,
        "checks": sorted(checks),
        "requires_trust": bool(commands or skills or memory_records or secrets),
    }


def _trust_key(project_id, sha256):
    return f"{TRUST_PREFIX}{project_id or '_global'}:{sha256}"


def export_scenario(conn, api, scenario_id, *, project_id=None, scope=None, checked_project=None):
    """Export one stored scenario or built-in template with its canonical SHA-256 and security summary."""
    eff_project = project_id if project_id is not None else checked_project
    if scope == "builtin":
        rec = scenarios.builtin_templates().get(scenario_id)
    elif scope == "global":
        rec = scenarios.resolve(conn, scenario_id, None)
        if rec and rec["scope"] != "global":
            rec = None
    else:
        rec = scenarios.resolve(conn, scenario_id, eff_project)
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
    """Import a scenario JSON definition with canonical SHA-256 trust bound to (sha256, project_id)."""
    author = author or rules.current_author()
    if scope == "project":
        eff_project = project_id if project_id is not None else checked_project
        if not eff_project:
            raise scenario_runs._failure(api, "Project import requires --project ID or --project-path", "invalid_input")
        api.require_project(conn, eff_project)
    else:
        eff_project = None

    try:
        defn = scenario_format.normalize(raw_definition)
    except scenario_format.FormatError as exc:
        raise scenario_runs._failure(api, str(exc), "invalid_scenario") from exc

    sha = scenario_format.digest(defn)
    sec = security_summary(defn)
    tkey = _trust_key(eff_project, sha)
    already_trusted = conn.execute("SELECT 1 FROM metadata WHERE key = ?", (tkey,)).fetchone() is not None

    if sec["requires_trust"] and not already_trusted:
        if not trust_sha256:
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
        provided = str(trust_sha256).strip().lower()
        if provided != sha:
            raise scenario_runs._failure(
                api, f"Digest mismatch: scenario SHA-256 is {sha}, but --trust-sha256 was {provided[:80]}",
                "digest_mismatch",
            )
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
            (tkey, json.dumps({"sha256": sha, "project_id": eff_project, "scenario_id": defn["id"], "by": author.by})),
        )

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
    """Add or update a fixed-commit golden bug benchmark case."""
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
    base_rev = str(case_obj.get("base_rev") or "").strip()
    head_rev = str(case_obj.get("head_rev") or "").strip()
    if not base_rev or not head_rev or not scenario_runs._GIT_REV.fullmatch(base_rev) or not scenario_runs._GIT_REV.fullmatch(head_rev):
        raise scenario_runs._failure(api, "Golden case requires valid base_rev and head_rev Git revisions", "invalid_input")
    expected_bugs = _validate_golden_bug_list(case_obj.get("expected_bugs"), "expected_bugs", api, required=True)
    known_traps = _validate_golden_bug_list(case_obj.get("known_traps"), "known_traps", api, required=False)
    as_of_iso = case_obj.get("commit_time")
    if as_of_iso is not None:
        try:
            datetime.fromisoformat(str(as_of_iso).replace("Z", "+00:00"))
        except ValueError as exc:
            raise scenario_runs._failure(api, "commit_time must be ISO-8601 timestamp", "invalid_input") from exc

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
    return json.loads(row[0])


def golden_list(conn, *, project_id=None):
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
        if project_id is not None and c.get("project_id") != project_id:
            continue
        cases.append(c)
    return {"view": "scenario-golden-list", "cases": cases}


def golden_remove(conn, api, case_id):
    cur = conn.execute("DELETE FROM metadata WHERE key = ?", (_golden_key(case_id),))
    if cur.rowcount == 0:
        raise api.NotFoundError("Unknown golden case: " + str(case_id))
    return {"view": "scenario-golden", "status": "removed", "id": case_id}


def _finding_matches_spec(finding, spec):
    """Severity-agnostic matcher: same file AND (line within range±slop OR symbol overlap)."""
    if scenario_findings._normalize_file(finding.get("file") or "") != spec["file"]:
        return False
    lr = spec.get("line_range")
    line = finding.get("line")
    line_hit = False
    if lr is not None and isinstance(line, int) and not isinstance(line, bool):
        line_hit = (lr[0] - _LINE_SLOP) <= line <= (lr[1] + _LINE_SLOP)
    syms = set(spec.get("symbols") or ())
    sym_hit = False
    if syms:
        hay_ids = scenario_findings._extract_identifiers((finding.get("title") or "") + " " + (finding.get("evidence") or ""))
        hay_low = ((finding.get("title") or "") + " " + (finding.get("evidence") or "")).lower()
        sym_hit = any(s in hay_ids or s in hay_low for s in syms)
    if lr is not None and syms:
        return line_hit or sym_hit
    return line_hit if lr is not None else sym_hit


def _score_single_run(conn, api, case, run_id):
    run_row = scenario_runs._load_run(conn, run_id, api)
    defn = json.loads(run_row["definition_json"])
    step_rows = scenario_runs._load_steps(conn, run_id)
    deduped, _ = scenario_review._extract_run_findings(defn, step_rows)
    active = [f for f in deduped if f["status"] != "disproved"]

    matched_finding_ids = set()
    detected = []
    missed = []
    for exp in case["expected_bugs"]:
        hit = next((f for f in active if _finding_matches_spec(f, exp)), None)
        if hit is not None:
            matched_finding_ids.add(hit["finding_id"])
            detected.append({
                "bug_id": exp["id"],
                "file": exp["file"],
                "matched_finding_id": hit["finding_id"],
                "reported_severity": hit["severity"],
                "title": hit["title"],
            })
        else:
            missed.append({"bug_id": exp["id"], "file": exp["file"], "description": exp.get("description", "")})

    traps_triggered = []
    for trap in case.get("known_traps") or ():
        hit = next((f for f in active if f["finding_id"] not in matched_finding_ids and _finding_matches_spec(f, trap)), None)
        if hit is not None:
            matched_finding_ids.add(hit["finding_id"])
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
    return {
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


def eval_score(conn, api, case_id, run_id, *, compare_run_id=None, checked_project=None):
    """Score a completed eval run against a golden case, optionally with Paired Bug Diff."""
    scenario_runs.require_v4(conn, api)
    case = golden_get(conn, api, case_id)
    run_row = scenario_runs._load_run(conn, run_id, api)
    scenario_runs._check_run_project(run_row, checked_project, api)
    current = _score_single_run(conn, api, case, run_id)
    out = {
        "view": "scenario-eval-score",
        "case_id": case_id,
        "case_title": case["title"],
        "result": current,
    }
    if compare_run_id:
        prev_row = scenario_runs._load_run(conn, compare_run_id, api)
        scenario_runs._check_run_project(prev_row, checked_project, api)
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
               project_id=None, checked_project=None, project_path=None, author=None, now_override=None):
    """Start an isolated `run_kind='eval'` run of `scenario_id` on a fixed-commit golden case."""
    scenario_runs.require_v4(conn, api)
    if memory_mode not in MEMORY_MODES:
        raise scenario_runs._failure(api, "memory_mode must be one of " + ", ".join(MEMORY_MODES), "invalid_input")
    case = golden_get(conn, api, case_id)
    eff_project = project_id or case.get("project_id") or checked_project

    resolved = scenarios.resolve(conn, scenario_id, eff_project)
    if resolved is None:
        raise api.NotFoundError("Unknown scenario: " + scenario_id)
    declared_inputs = resolved["definition"].get("inputs") or {}
    eval_inputs = {}
    if "base" in declared_inputs:
        eval_inputs["base"] = case["base_rev"]
    if "branch" in declared_inputs:
        eval_inputs["branch"] = case["head_rev"]
    if "target" in declared_inputs:
        eval_inputs["target"] = f"{case['base_rev']}...{case['head_rev']}"
    if "focus" in declared_inputs:
        eval_inputs["focus"] = f"eval:{case_id}"

    # Pre-register run_meta during start_run via hook parameter so memory.brief honors memory_mode on step 1!
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
