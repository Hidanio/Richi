"""Review Hub aggregation, structured finding feedback, and scoped false-positive learnings.

Feedback is stored per run under metadata key `scenario-feedback:<run_id>` (bounded to 64 KiB / 250 items)
with an immutable audit event in `scenario_events`. Promoted false-positive patterns are stored as confirmed
`fact` entries ONLY via explicit human CLI invocation (`allow_remember_pattern=True`), never from the map UI
or automated `memory.record` steps.
"""
from datetime import datetime, timezone
import hashlib
import json
import re

from . import rules, scenario_findings, scenario_format, scenario_registry, scenario_runs


FEEDBACK_PREFIX = "scenario-feedback:"
MAX_FEEDBACK_BYTES = 64 * 1024
MAX_FEEDBACK_ITEMS = 250
MAX_BATCH_ITEMS = 100
MAX_NOTE_CHARS = 1000
MAX_SCOPE_PATHS = 10
MAX_ENTRY_SOURCES = 100
FP_TAG = "false-positive"
FP_STALE_DAYS = 90
FP_MAX_INJECT = 5

VERDICTS = ("actionable_bug", "valid_nit", "false_positive")
RESOLUTIONS = ("open", "fixed", "wont_fix", "unreviewed_dismissed")


def _review_domain_spec():
    for dv in scenario_registry.DOMAIN_VIEWS:
        if dv.get("id") == "review":
            return dv
    return {"match_tags": ["review", "bug-hunt", "pre-commit"], "empty_cta_templates": ["pre-commit-review", "bug-hunt"]}


REVIEW_TAGS = frozenset(str(t).lower() for t in _review_domain_spec().get("match_tags") or ("review", "bug-hunt", "pre-commit"))
_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _feedback_key(run_id):
    return FEEDBACK_PREFIX + str(run_id)


def get_run_feedback(conn, run_id):
    """Return latest-wins feedback map `{finding_id: {...}}` for one run."""
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (_feedback_key(run_id),)).fetchone()
    if row is None or not row[0]:
        return {}
    try:
        data = json.loads(row[0])
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _feedback_for_runs(conn, run_ids):
    """Load feedback blobs ONLY for the specified run IDs (bounded, never full-table scan)."""
    if not run_ids:
        return {}
    out = {}
    keys = [_feedback_key(rid) for rid in run_ids]
    for i in range(0, len(keys), 500):
        chunk = keys[i : i + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT key, value FROM metadata WHERE key IN ({placeholders})",
            chunk,
        ).fetchall()
        for k, v in rows:
            rid = k[len(FEEDBACK_PREFIX):]
            try:
                parsed = json.loads(v)
                if isinstance(parsed, dict):
                    out[rid] = parsed
            except (ValueError, TypeError):
                continue
    return out


def _clean_single_line(value, field, api, *, min_len=0, max_len=500):
    if not isinstance(value, str) or _CTRL_CHARS.search(value):
        raise scenario_runs._failure(api, f"{field} must be valid text without control characters", "invalid_input")
    collapsed = " ".join(value.split()).strip()
    if len(collapsed) < min_len or len(collapsed) > max_len:
        raise scenario_runs._failure(
            api, f"{field} must be {min_len}-{max_len} characters", "invalid_input"
        )
    return collapsed


def _clean_scope_paths(paths, api):
    if not isinstance(paths, list) or not paths:
        raise scenario_runs._failure(api, "remember_pattern requires at least one --scope-path", "invalid_input")
    if len(paths) > MAX_SCOPE_PATHS:
        raise scenario_runs._failure(api, f"At most {MAX_SCOPE_PATHS} scope paths allowed", "invalid_input")
    cleaned = []
    for raw in paths:
        if not isinstance(raw, str):
            raise scenario_runs._failure(api, "Invalid scope_path: must be text", "invalid_input")
        trimmed = raw.strip().rstrip("/")
        p, err = scenario_findings._safe_rel_repo_path(trimmed)
        if err:
            raise scenario_runs._failure(api, "Invalid scope_path: " + err, "invalid_input")
        if p not in cleaned:
            cleaned.append(p)
    return cleaned


def _remember_fp_pattern(conn, api, run_row, item, author, at_iso):
    """Persist a scoped false-positive learning as a confirmed fact entry."""
    scope_paths = _clean_scope_paths(item.get("scope_paths"), api)
    cond = _clean_single_line(
        item.get("safety_condition"), "safety_condition", api, min_len=10, max_len=500
    )
    created_rev = item.get("created_rev")
    if created_rev is not None:
        created_rev = _clean_single_line(created_rev, "created_rev", api, min_len=0, max_len=120) or None
    fid = item["finding_id"]
    raw_title = item.get("title")
    if raw_title is not None:
        raw_title = _clean_single_line(raw_title, "title", api, min_len=0, max_len=180)
    title = raw_title if raw_title else f"FP pattern ({scope_paths[0]}): {cond[:90]}"
    note_raw = item.get("note")
    note = _clean_single_line(note_raw, "note", api, min_len=0, max_len=500) if note_raw else ""

    sig_payload = json.dumps(
        [run_row["project_id"], sorted(scope_paths), cond.lower()],
        ensure_ascii=False, separators=(",", ":"),
    )
    entry_id = "fact:fp-" + hashlib.sha256(sig_payload.encode("utf-8")).hexdigest()[:12]

    lines = [
        "Scope paths: " + json.dumps(scope_paths, ensure_ascii=False),
        "Safety condition: " + cond,
    ]
    if created_rev:
        lines.append("Created rev: " + created_rev)
    if note:
        lines.append("Rationale: " + note[:500])
    summary = "\n".join(lines)

    sources = [
        {"reference": "scenario-run:" + run_row["id"], "type": "scenario-run", "label": run_row["id"]},
        {"reference": "finding:" + fid, "type": "finding", "label": fid},
    ]
    tags = [FP_TAG, "review-learning", "scenario:" + run_row["scenario_id"]]
    existing = api.entry_record(conn, entry_id)
    if existing is not None:
        current = api.entry_get(conn, entry_id, history=False)
        updated = {k: v for k, v in current.items() if k not in ("created_at", "updated_at", "history")}
        updated["title"] = title
        updated["summary"] = summary
        updated["knowledge_state"] = "confirmed"
        seen_refs = {s.get("reference") for s in (updated.get("sources") or []) if isinstance(s, dict)}
        merged_sources = list(updated.get("sources") or []) + [s for s in sources if s["reference"] not in seen_refs]
        updated["sources"] = merged_sources[-MAX_ENTRY_SOURCES:]
        updated["tags"] = list(dict.fromkeys(list(updated.get("tags") or []) + tags))[:20]
        updated["expected_updated_at"] = current["updated_at"]
        put_res = api.entry_put(conn, updated)
    else:
        record = {
            "id": entry_id,
            "kind": "fact",
            "title": title,
            "summary": summary,
            "project_ids": [run_row["project_id"]] if run_row["project_id"] else [],
            "knowledge_state": "confirmed",
            "sources": sources,
            "tags": tags,
            "expected_updated_at": None,
        }
        put_res = api.entry_put(conn, record)
    return {"entry_id": entry_id, "status": put_res["status"], "scope_paths": scope_paths}


def record_feedback(conn, api, run_id, items, *, checked_project=None, author=None,
                    allow_remember_pattern=True, now_override=None):
    """Record one or more finding feedback items (verdict + resolution) for a scenario run."""
    scenario_runs.require_v4(conn, api)
    author = author or rules.current_author()
    at_iso = scenario_runs._format_iso(scenario_runs._now_dt(api, now_override))
    run_row = scenario_runs._load_run(conn, run_id, api)
    scenario_runs._check_run_project(run_row, checked_project, api)

    if not isinstance(items, list) or not items:
        raise scenario_runs._failure(api, "At least one feedback item is required", "invalid_input")
    if len(items) > MAX_BATCH_ITEMS:
        raise scenario_runs._failure(api, f"At most {MAX_BATCH_ITEMS} feedback items per request", "invalid_input")

    current = get_run_feedback(conn, run_id)
    recorded_patterns = []
    audit_items = []

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise scenario_runs._failure(api, f"items[{idx}] must be an object", "invalid_input")
        fid = item.get("finding_id")
        if not isinstance(fid, str) or not fid.strip() or len(fid) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in fid):
            raise scenario_runs._failure(api, f"items[{idx}].finding_id must be valid nonempty text", "invalid_input")
        fid = fid.strip()
        verdict = item.get("verdict")
        resolution = item.get("resolution")
        if verdict is None and resolution is None:
            raise scenario_runs._failure(api, f"items[{idx}] must set verdict and/or resolution", "invalid_input")
        if verdict is not None and verdict not in VERDICTS:
            raise scenario_runs._failure(api, f"items[{idx}].verdict must be one of {', '.join(VERDICTS)}", "invalid_input")
        if resolution is not None and resolution not in RESOLUTIONS:
            raise scenario_runs._failure(api, f"items[{idx}].resolution must be one of {', '.join(RESOLUTIONS)}", "invalid_input")
        note = item.get("note")
        if note is not None:
            if not isinstance(note, str) or len(note) > MAX_NOTE_CHARS or "\x00" in note:
                raise scenario_runs._failure(api, f"items[{idx}].note must be text <= {MAX_NOTE_CHARS} chars", "invalid_input")
            note = note.strip()

        prev = current.get(fid) if isinstance(current.get(fid), dict) else {}
        entry = {
            "verdict": verdict if verdict is not None else prev.get("verdict"),
            "resolution": resolution if resolution is not None else (prev.get("resolution") or "open"),
            "updated_at": at_iso,
            "by": author.by,
        }
        if note is not None:
            if note:
                entry["note"] = note
        elif prev.get("note"):
            entry["note"] = prev["note"]
        if prev.get("pattern_entry_id"):
            entry["pattern_entry_id"] = prev["pattern_entry_id"]

        if item.get("remember_pattern"):
            if not allow_remember_pattern:
                raise scenario_runs._failure(
                    api, "Promoting FP patterns to memory is only allowed via CLI (richi scenario feedback --remember-pattern)",
                    "forbidden",
                )
            if entry["verdict"] != "false_positive":
                raise scenario_runs._failure(
                    api, "remember_pattern requires verdict='false_positive'", "invalid_input"
                )
            pat = _remember_fp_pattern(conn, api, run_row, item, author, at_iso)
            entry["pattern_entry_id"] = pat["entry_id"]
            recorded_patterns.append(pat)

        current[fid] = entry
        audit_items.append({
            "finding_id": fid,
            "verdict": entry["verdict"],
            "resolution": entry["resolution"],
        })

    if len(current) > MAX_FEEDBACK_ITEMS:
        raise scenario_runs._failure(api, f"Feedback store limit exceeded ({MAX_FEEDBACK_ITEMS} items per run)", "limit")
    encoded = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_FEEDBACK_BYTES:
        raise scenario_runs._failure(api, f"Feedback payload exceeds {MAX_FEEDBACK_BYTES} bytes", "limit")

    conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (_feedback_key(run_id), encoded))
    scenario_runs._record_event(
        conn, at_iso, run_row["scenario_id"], run_id, None, "finding_feedback",
        author.by, author.agent, {"items": audit_items[:30], "count": len(audit_items)},
    )
    return {
        "view": "scenario-feedback",
        "status": "recorded",
        "run_id": run_id,
        "count": len(audit_items),
        "feedback": current,
        "recorded_patterns": recorded_patterns,
    }


# ---------------------------------------------------------------- Scoped FP learnings


def _parse_fp_summary(summary):
    """Parse structured FP header fields (first-match-wins before Rationale:)."""
    scope_paths = None
    safety_condition = None
    created_rev = None
    for line in str(summary or "").splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("rationale:"):
            break
        if low.startswith("scope paths:") and scope_paths is None:
            raw = stripped.split(":", 1)[1].strip()
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        scope_paths = [str(p).strip() for p in parsed if isinstance(p, str) and p.strip()]
                except (ValueError, TypeError):
                    scope_paths = None
            if scope_paths is None:
                scope_paths = [p.strip() for p in raw.split(",") if p.strip()]
        elif low.startswith("safety condition:") and safety_condition is None:
            safety_condition = stripped.split(":", 1)[1].strip()
        elif low.startswith("created rev:") and created_rev is None:
            created_rev = stripped.split(":", 1)[1].strip() or None
    return scope_paths or [], safety_condition or "", created_rev


def _parse_utc(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _is_stale(updated_at, ref_dt):
    dt = _parse_utc(updated_at)
    if dt is None:
        return False
    return (ref_dt - dt).days > FP_STALE_DAYS


def load_scoped_fp_learnings(conn, project_id=None, *, query="", changed_files=None,
                             now_dt=None, as_of_dt=None, limit=FP_MAX_INJECT):
    """Load confirmed `false-positive` fact entries scoped to project/query, with staleness & as_of cutoff."""
    ref_dt = as_of_dt or now_dt or datetime.now(timezone.utc)
    if project_id is not None:
        rows = conn.execute(
            "SELECT e.id, e.title, e.summary, e.updated_at, e.tags "
            "FROM entries e LEFT JOIN entry_projects ep ON ep.entry_id = e.id "
            "WHERE e.kind = 'fact' AND e.knowledge_state = 'confirmed' AND e.tags LIKE ? "
            "AND (ep.project_id = ? OR ep.project_id IS NULL) "
            "ORDER BY e.updated_at DESC LIMIT 120",
            ('%"false-positive"%', project_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT e.id, e.title, e.summary, e.updated_at, e.tags "
            "FROM entries e "
            "WHERE e.kind = 'fact' AND e.knowledge_state = 'confirmed' AND e.tags LIKE ? "
            "ORDER BY e.updated_at DESC LIMIT 120",
            ('%"false-positive"%',),
        ).fetchall()

    seen = set()
    q_tokens = set(re.findall(r"[A-Za-zА-Яа-яЁё0-9_./-]{3,}", str(query or "").lower()))
    changed = [str(f).strip().replace("\\", "/").lstrip("./") for f in (changed_files or ()) if f]

    scored = []
    for row in rows:
        eid = row[0]
        if eid in seen:
            continue
        seen.add(eid)
        if as_of_dt is not None:
            upd_dt = _parse_utc(row[3])
            if upd_dt is not None and upd_dt > as_of_dt:
                continue
        try:
            tags = json.loads(row[4] or "[]")
        except (ValueError, TypeError):
            tags = []
        if FP_TAG not in tags:
            continue
        scope_paths, cond, created_rev = _parse_fp_summary(row[2])
        stale = _is_stale(row[3], ref_dt)
        score = 0
        if not stale:
            score += 10
        if changed and scope_paths:
            for cf in changed:
                if any(cf == sp or cf.startswith(sp.rstrip("/") + "/") for sp in scope_paths):
                    score += 50
        hay = (str(row[1] or "") + " " + str(row[2] or "")).lower()
        for tok in q_tokens:
            if tok in hay:
                score += 5
        scored.append({
            "score": score,
            "updated_at": str(row[3] or ""),
            "item": {
                "entry_id": eid,
                "title": row[1],
                "scope_paths": scope_paths,
                "safety_condition": cond or str(row[1] or ""),
                "created_rev": created_rev,
                "updated_at": row[3],
                "stale": stale,
            },
        })

    # Sort deterministically: highest score first, then newest updated_at DESC, then entry_id ASC.
    scored.sort(key=lambda x: x["item"]["entry_id"])
    scored.sort(key=lambda x: x["updated_at"], reverse=True)
    scored.sort(key=lambda x: -x["score"])
    items = [x["item"] for x in scored[:max(0, int(limit))]]
    hints = []
    for it in items:
        scope_str = ", ".join(it["scope_paths"][:3]) if it["scope_paths"] else "project"
        stale_tag = " [stale]" if it["stale"] else ""
        safe_cond = str(it["safety_condition"]).replace("]", ")").replace("[", "(")[:180]
        hints.append(
            f"FP-check{stale_tag} ({it['entry_id']}, {scope_str}): verify whether safety condition holds before flagging — {safe_cond}"
        )
    return items, hints


# ---------------------------------------------------------------- Review Hub Snapshot


def _is_review_definition(defn):
    tags = {str(t).lower() for t in (defn.get("tags") or ())}
    if tags & REVIEW_TAGS:
        return True
    for step in defn.get("steps") or ():
        for chk in step.get("checks") or ():
            if chk.get("kind") == "findings_grounded":
                return True
        out_data = (step.get("outputs") or {}).get("data") or {}
        if "findings" in out_data:
            return True
    return False


def _extract_run_findings(defn, step_rows):
    """Extract findings + disprove metrics using per-branch DAG dominance (no hardcoded step IDs).

    If a completed (`done`) step B is a transitive DAG descendant of step A and B emits `findings` or
    `disproved`, B supersedes A's raw candidates along that branch while inheriting `consensus_ratio`,
    `corroborated_by`, and `severity_votes` from fuzzy-matched upstream candidates of A.
    """
    steps_list = defn.get("steps") or []
    by_def = {s["id"]: s for s in steps_list if isinstance(s, dict) and "id" in s}
    try:
        ancestry = scenario_format.ancestors(defn) if steps_list else {}
    except Exception:
        ancestry = {}

    replicated_totals = {}
    for row in step_rows:
        if row["status"] != "done":
            continue
        spec = by_def.get(row["step_id"]) or {}
        if spec.get("matrix_mode") == "replicated":
            replicated_totals[row["step_id"]] = replicated_totals.get(row["step_id"], 0) + 1

    per_step_active = {}
    per_step_disproved = {}
    critic_step_ids = set()

    for row in step_rows:
        if row["status"] != "done" or not row["result_json"]:
            continue
        try:
            res = json.loads(row["result_json"])
        except (ValueError, TypeError):
            continue
        data = res.get("data") if isinstance(res, dict) else None
        if not isinstance(data, dict):
            continue
        sid = row["step_id"]
        spec = by_def.get(sid) or {}
        matrix_label = ""
        if row["matrix_json"]:
            try:
                m = json.loads(row["matrix_json"])
                if isinstance(m, dict) and m:
                    matrix_label = ",".join(str(v) for v in m.values())
            except (ValueError, TypeError):
                pass
        matrix_mode = spec.get("matrix_mode") or "orthogonal"

        f_list = data.get("findings")
        d_list = data.get("disproved")
        if isinstance(d_list, list):
            critic_step_ids.add(sid)
            per_step_disproved.setdefault(sid, [])
            for item in d_list:
                if not isinstance(item, dict):
                    continue
                per_step_disproved[sid].append({
                    "file": item.get("file", ""),
                    "line": item.get("line"),
                    "end_line": item.get("end_line"),
                    "severity": item.get("severity", "P2"),
                    "title": item.get("title", ""),
                    "evidence": item.get("evidence", ""),
                    "status": "disproved",
                    "step_id": sid,
                    "step_key": row["step_key"],
                    "matrix_label": matrix_label or sid,
                    "matrix_mode": "orthogonal",
                })
        if isinstance(f_list, list):
            per_step_active.setdefault(sid, [])
            for item in f_list:
                if not isinstance(item, dict):
                    continue
                per_step_active[sid].append({
                    "file": item.get("file", ""),
                    "line": item.get("line"),
                    "end_line": item.get("end_line"),
                    "severity": item.get("severity", "P2"),
                    "title": item.get("title", ""),
                    "evidence": item.get("evidence", ""),
                    "status": item.get("status") or ("confirmed" if sid in critic_step_ids else "candidate"),
                    "step_id": sid,
                    "step_key": row["step_key"],
                    "matrix_label": matrix_label or sid,
                    "matrix_mode": matrix_mode,
                })

    # Per-branch DAG dominance: a step A is superseded iff some completed descendant B emitted findings or disproved.
    done_emitting = set(per_step_active) | set(per_step_disproved)
    superseded_steps = set()
    for step_b in done_emitting:
        for anc_a in ancestry.get(step_b, ()):
            if anc_a in done_emitting:
                superseded_steps.add(anc_a)

    # Mark findings emitted by steps that dominate an upstream findings step as confirmed by default
    dominating_steps = {
        step_b for step_b in done_emitting
        if any(anc_a in done_emitting for anc_a in ancestry.get(step_b, ()))
    }
    for sid in dominating_steps:
        for item in per_step_active.get(sid, ()):
            if item["status"] == "candidate":
                item["status"] = "confirmed"

    terminal_active_raw = []
    superseded_raw = []
    for sid, items in per_step_active.items():
        if sid in superseded_steps:
            superseded_raw.extend(items)
        else:
            terminal_active_raw.extend(items)

    all_disproved_raw = []
    for items in per_step_disproved.values():
        all_disproved_raw.extend(items)

    terminal_deduped = scenario_findings.dedupe_findings(terminal_active_raw, replicated_totals=replicated_totals)
    disproved_deduped = scenario_findings.dedupe_findings(all_disproved_raw, replicated_totals=replicated_totals)
    upstream_deduped = scenario_findings.dedupe_findings(superseded_raw, replicated_totals=replicated_totals) if superseded_raw else []

    # Propagate upstream replicated consensus & orthogonal corroboration onto surviving terminal findings
    if upstream_deduped and terminal_deduped:
        for tf in terminal_deduped:
            matched_up = [uf for uf in upstream_deduped if scenario_findings.findings_similar(tf, uf)]
            if not matched_up:
                continue
            for uf in matched_up:
                if tf["consensus_ratio"] is None and uf.get("consensus_ratio") is not None:
                    tf["consensus_ratio"] = uf["consensus_ratio"]
                for lbl in uf.get("corroborated_by") or ():
                    if lbl not in tf["corroborated_by"]:
                        tf["corroborated_by"].append(lbl)
                for sk in uf.get("source_steps") or ():
                    if sk not in tf["source_steps"]:
                        tf["source_steps"].append(sk)

    # Disprove yield strictly on deduped sets so 0.0 <= disprove_rate <= 1.0 ALWAYS holds
    has_disprove = bool(critic_step_ids)
    disprove_removed = len(disproved_deduped)
    disprove_candidates = 0
    if has_disprove:
        critic_ancestors = set()
        for cid in critic_step_ids:
            critic_ancestors.update(ancestry.get(cid, ()))
        upstream_of_critics_raw = [it for sid in critic_ancestors for it in per_step_active.get(sid, ())]
        upstream_cnt = len(scenario_findings.dedupe_findings(upstream_of_critics_raw, replicated_totals=replicated_totals)) if upstream_of_critics_raw else 0
        critic_survived = len([f for f in terminal_deduped if f["step_id"] in critic_step_ids])
        disprove_candidates = max(upstream_cnt, critic_survived + disprove_removed)

    combined = terminal_deduped + disproved_deduped
    combined.sort(key=lambda x: (
        1 if x["status"] == "disproved" else 0,
        scenario_findings.SEVERITY_RANK.get(x["severity"], 9),
        x["file"],
        x["line"] if x["line"] is not None else 10**9,
        x["finding_id"],
    ))
    return combined, {
        "has_disprove": has_disprove,
        "candidates": disprove_candidates,
        "disproved": disprove_removed,
        "has_final_step": bool(dominating_steps or critic_step_ids),
    }


def _scope_target_key(inputs_json):
    """Extract normalized branch/target scope for cross-run likely_fixed grouping (ignoring free-text focus)."""
    try:
        inp = json.loads(inputs_json or "{}")
    except (ValueError, TypeError):
        return (None, None)
    if not isinstance(inp, dict):
        return (None, None)
    branch = str(inp.get("branch")).strip() if inp.get("branch") else None
    target = str(inp.get("target")).strip() if inp.get("target") else None
    return (branch, target)


def review_hub_snapshot(conn, api=None, *, project_id=None, limit=30):
    """Aggregate Review Hub KPIs, deduped Findings Inbox, and FP health for the Scenarios > Review tab."""
    caps = scenario_registry.detect_capabilities(conn)
    domain_spec = _review_domain_spec()
    empty_ctas = list(domain_spec.get("empty_cta_templates") or ["pre-commit-review", "bug-hunt"])
    projects = [{"id": r[0], "name": r[1]} for r in conn.execute("SELECT id, name FROM projects ORDER BY name, id")]
    now_dt = datetime.now(timezone.utc)
    fp_all, _ = load_scoped_fp_learnings(conn, project_id=project_id, now_dt=now_dt, limit=50)
    stale_fp = sum(1 for p in fp_all if p["stale"])
    fresh_fp = len(fp_all) - stale_fp

    empty_kpis = {
        "signal_mix": {
            "actionable_bugs": 0, "valid_nits": 0, "false_positives": 0, "unlabeled": 0,
            "labeled_total": 0, "total_findings": 0, "label_coverage": None,
            "actionable_share": None, "actionable_bug_share": None, "sufficient_sample": False,
        },
        "open_blockers": {"p0": 0, "p1": 0, "total": 0, "likely_fixed": 0},
        "step_signal_table": [],
        "disprove_yield": {"candidates": 0, "disproved": 0, "survived": 0, "disprove_rate": None},
        "fp_health": {"total": len(fp_all), "fresh": fresh_fp, "stale": stale_fp},
    }

    if not caps["review_hub"]:
        return {
            "view": "scenario-review-hub",
            "capabilities": caps,
            "project_id": project_id,
            "projects": projects,
            "runs_analyzed": 0,
            "kpis": empty_kpis,
            "findings": [],
            "fp_patterns": fp_all,
            "empty_cta_templates": empty_ctas,
        }

    # Filter prod runs in SQL via LEFT JOIN metadata and paginate until `limit` review runs are collected.
    clauses = ["(m.value IS NULL OR COALESCE(json_extract(m.value, '$.kind'), 'prod') = 'prod')"]
    params = []
    if project_id is not None:
        clauses.append("r.project_id = ?")
        params.append(project_id)
    where = " WHERE " + " AND ".join(clauses)

    review_runs = []
    offset = 0
    batch_size = max(limit * 3, 60)
    max_scan = 2000
    while len(review_runs) < limit and offset < max_scan:
        rows = conn.execute(
            "SELECT r.id, r.scenario_id, r.scenario_version, r.project_id, r.status, r.definition_json, "
            "r.inputs_json, r.created_at, r.updated_at "
            "FROM scenario_runs r "
            "LEFT JOIN metadata m ON m.key = 'scenario-run-meta:' || r.id"
            + where + " ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?",
            params + [batch_size, offset],
        ).fetchall()
        if not rows:
            break
        offset += len(rows)
        for r in rows:
            try:
                defn = json.loads(r["definition_json"])
            except (ValueError, TypeError):
                continue
            if not isinstance(defn, dict) or not _is_review_definition(defn):
                continue
            review_runs.append((r, defn))
            if len(review_runs) >= limit:
                break

    fb_map = _feedback_for_runs(conn, [r["id"] for r, _ in review_runs])

    # Track newest done run per (scenario_id, project_id, branch, target) with its chronological index
    latest_done_findings = {}
    per_run_extracted = []
    for run_idx, (r, defn) in enumerate(review_runs):
        step_rows = scenario_runs._load_steps(conn, r["id"])
        deduped, dmeta = _extract_run_findings(defn, step_rows)
        per_run_extracted.append((run_idx, r, defn, deduped, dmeta))
        group_key = (r["scenario_id"], r["project_id"], _scope_target_key(r["inputs_json"]))
        if r["status"] == "done" and group_key not in latest_done_findings:
            active_in_done = [f for f in deduped if f["status"] != "disproved"]
            latest_done_findings[group_key] = (
                run_idx,
                r["id"],
                {f["finding_id"] for f in active_in_done},
                active_in_done,
            )

    inbox = []
    actionable = valid_nit = false_pos = unlabeled = 0
    open_p0 = open_p1 = likely_fixed_cnt = 0
    step_signal = {}
    disp_cand = disp_rem = 0

    for run_idx, r, defn, deduped, dmeta in per_run_extracted:
        rid = r["id"]
        run_fb = fb_map.get(rid) or {}
        if dmeta["has_disprove"]:
            disp_cand += dmeta["candidates"]
            disp_rem += dmeta["disproved"]
        group_key = (r["scenario_id"], r["project_id"], _scope_target_key(r["inputs_json"]))
        newest_done = latest_done_findings.get(group_key)

        for f in deduped:
            fb = run_fb.get(f["finding_id"]) or {}
            verdict = fb.get("verdict")
            resolution = fb.get("resolution") or ("unreviewed_dismissed" if f["status"] == "disproved" else "open")
            likely_fixed = False
            # Only flag likely_fixed for runs STRICTLY OLDER than newest_done (run_idx > newest_done_idx)
            if (f["status"] != "disproved" and resolution == "open" and verdict != "false_positive"
                    and newest_done is not None and run_idx > newest_done[0]):
                still_present = (
                    f["finding_id"] in newest_done[2]
                    or any(scenario_findings.findings_similar(f, nf) for nf in newest_done[3])
                )
                if not still_present:
                    likely_fixed = True

            enriched = dict(f)
            enriched.update({
                "run_id": rid,
                "scenario_id": r["scenario_id"],
                "scenario_title": defn.get("title") or r["scenario_id"],
                "project_id": r["project_id"],
                "run_created_at": r["created_at"],
                "verdict": verdict,
                "resolution": resolution,
                "note": fb.get("note") or "",
                "pattern_entry_id": fb.get("pattern_entry_id"),
                "likely_fixed": likely_fixed,
            })
            inbox.append(enriched)

            # Metrics only count active (non-disproved) findings in signal_mix
            if f["status"] != "disproved":
                if verdict == "actionable_bug":
                    actionable += 1
                elif verdict == "valid_nit":
                    valid_nit += 1
                elif verdict == "false_positive":
                    false_pos += 1
                else:
                    unlabeled += 1

                if resolution == "open" and verdict != "false_positive":
                    if f["severity"] == "P0":
                        open_p0 += 1
                        if likely_fixed:
                            likely_fixed_cnt += 1
                    elif f["severity"] == "P1":
                        open_p1 += 1
                        if likely_fixed:
                            likely_fixed_cnt += 1

                sid = f["step_id"] or "unknown"
                scn_id = r["scenario_id"]
                sb_key = (scn_id, sid)
                sb = step_signal.setdefault(sb_key, {
                    "scenario_id": scn_id, "step_id": sid, "total": 0, "actionable": 0,
                    "valid_nit": 0, "false_positive": 0, "unlabeled": 0,
                })
                sb["total"] += 1
                if verdict == "actionable_bug":
                    sb["actionable"] += 1
                elif verdict == "valid_nit":
                    sb["valid_nit"] += 1
                elif verdict == "false_positive":
                    sb["false_positive"] += 1
                else:
                    sb["unlabeled"] += 1

    labeled = actionable + valid_nit + false_pos
    total_active = labeled + unlabeled
    step_table = []
    for sb in sorted(step_signal.values(), key=lambda x: (-x["total"], x["scenario_id"], x["step_id"])):
        lab = sb["actionable"] + sb["valid_nit"] + sb["false_positive"]
        sb["signal_rate"] = round((sb["actionable"] + sb["valid_nit"]) / lab, 4) if lab else None
        step_table.append(sb)

    disp_survived = max(0, disp_cand - disp_rem)
    kpis = {
        "signal_mix": {
            "actionable_bugs": actionable,
            "valid_nits": valid_nit,
            "false_positives": false_pos,
            "unlabeled": unlabeled,
            "labeled_total": labeled,
            "total_findings": total_active,
            "label_coverage": round(labeled / total_active, 4) if total_active else None,
            "actionable_share": round((actionable + valid_nit) / labeled, 4) if labeled else None,
            "actionable_bug_share": round(actionable / labeled, 4) if labeled else None,
            "sufficient_sample": labeled >= 15,
        },
        "open_blockers": {
            "p0": open_p0,
            "p1": open_p1,
            "total": open_p0 + open_p1,
            "likely_fixed": likely_fixed_cnt,
        },
        "step_signal_table": step_table[:20],
        "disprove_yield": {
            "candidates": disp_cand,
            "disproved": disp_rem,
            "survived": disp_survived,
            "disprove_rate": min(1.0, round(disp_rem / disp_cand, 4)) if disp_cand else None,
        },
        "fp_health": {
            "total": len(fp_all),
            "fresh": fresh_fp,
            "stale": stale_fp,
        },
    }

    # Sort inbox: open blockers first, then severity, then newest run
    inbox.sort(key=lambda x: (
        1 if x["status"] == "disproved" else 0,
        1 if (x["resolution"] != "open" or x["verdict"] == "false_positive") else 0,
        scenario_findings.SEVERITY_RANK.get(x["severity"], 9),
        x["file"],
        x["line"] if x["line"] is not None else 10**9,
    ))

    return {
        "view": "scenario-review-hub",
        "capabilities": caps,
        "project_id": project_id,
        "projects": projects,
        "runs_analyzed": len(review_runs),
        "kpis": kpis,
        "findings": inbox[:200],
        "fp_patterns": fp_all,
        "empty_cta_templates": empty_ctas,
    }
