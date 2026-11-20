"""Human review of historical preference entries, scoped to the selected store."""
import hashlib
import itertools
import json
import sqlite3

from . import rules


def add_commands(actions):
    review = actions.add_parser("review-preferences", help="Read a page of pending preferences with their wording and scopes")
    review.add_argument("--limit", type=int, default=5, metavar="N", help="Maximum entries on one page, 1–10 (default: 5)")
    review.add_argument("--after", metavar="ENTRY_ID", help="Continue after the next_after entry ID from the preceding page")
    promote = actions.add_parser("promote", help="Confirm one reviewed preference as a standing rule or dismiss it")
    promote.add_argument("entry_id", help="Exact preference: entry ID from rules review-preferences")
    scope = promote.add_mutually_exclusive_group(required=True)
    scope.add_argument("--global", dest="global_scope", action="store_true", help="Review the workspace-wide preference")
    scope.add_argument("--project", metavar="ID", help="Review the preference for this linked project")
    promote.add_argument("--expected-updated-at", required=True, metavar="TIMESTAMP",
                         help="Version of the original entry whose wording the user reviewed")
    promote.add_argument("--rule-id", type=rules._argument_id, help="ID for the new rule (default: the suggested rule_id)")
    promote.add_argument("--text", help="Exact alternative wording approved by the user (default: the entry summary)")
    promote.add_argument("--dismiss", action="store_true", help="Keep the entry as knowledge and stop suggesting this scope")
    promote.add_argument("--reconsider-dismissed", action="store_true",
                         help="After a new explicit confirmation, promote a previously dismissed scope")
    rules.add_check_options(promote)


def _marker(identifier, project):
    # Entry and project IDs can contain punctuation. The length makes the pair unambiguous.
    return "preference-review:%d:%s%s" % (len(identifier), identifier, project or "")


def _rule_id(identifier, project, several):
    name = identifier[len("preference:"):]
    if several:
        name += ":" + project
    if rules._NAME.fullmatch(name):
        return name
    return "preference-" + hashlib.sha256(_marker(identifier, project).encode()).hexdigest()[:20]


def pending(conn, project=None, *, all_projects=False, preview=False, limit=3, after=""):
    # Stream scope IDs in order. Fetch wording once per candidate, even for 100 linked projects.
    sql = """SELECT e.id, ep.project_id
                FROM entries e LEFT JOIN entry_projects ep ON ep.entry_id = e.id
                LEFT JOIN metadata m ON m.key = 'preference-review:' || length(e.id) || ':' ||
                                               e.id || coalesce(ep.project_id, '')
               WHERE e.id >= 'preference:' AND e.id < 'preference;' AND e.id > ? AND e.knowledge_state <> 'superseded'
                 AND m.key IS NULL"""
    params = [after]
    if not all_projects:
        sql += " AND (ep.project_id IS NULL OR ep.project_id = ?)"
        params.append(project)
    sql += " ORDER BY e.id, ep.project_id"
    title_limit, text_limit = (160, 240) if preview else (500, 16384)
    candidates = []
    more = False
    for entry_id, group in itertools.groupby(conn.execute(sql, params), key=lambda row: row[0]):
        if len(candidates) == limit:
            more = True
            break
        title, wording, version, state, title_length, text_length, several = conn.execute(
            """SELECT substr(title, 1, ?), substr(summary, 1, ?), updated_at, knowledge_state,
                      length(title), length(summary), (SELECT count(*) FROM entry_projects WHERE entry_id = e.id) > 1
                 FROM entries e WHERE id = ?""", (title_limit, text_limit, entry_id)).fetchone()
        # SQLite permits BLOBs in TEXT columns. Reject them while the rules
        # preview can still report the error and return the standing rules.
        if not all(isinstance(value, str) for value in (entry_id, title, wording, version, state)):
            raise ValueError("Historical preference has a non-text field: " + str(entry_id)[:200])
        item = {"entry_id": entry_id, "title": title, "text": wording, "updated_at": version,
                "knowledge_state": state}
        scopes = []
        for _, project_id in group:
            if project_id is not None and not isinstance(project_id, str):
                raise ValueError("Historical preference has a non-text project ID: " + entry_id[:200])
            scopes.append({"scope": "project" if project_id else "global", "project_id": project_id,
                           "rule_id": _rule_id(entry_id, project_id, several)})
        if len(scopes) == 1:
            item.update(scopes[0])
        else:
            item["scopes"] = scopes
        if title_length > title_limit:
            item["title_truncated"] = True
        if text_length > text_limit:
            item["text_truncated"] = True
        if title_length > title_limit or text_length > text_limit:
            item["read_more"] = {"command": "entry get", "id": entry_id}
        candidates.append(item)
    return {"candidates": candidates, "more": more, "next_after": candidates[-1]["entry_id"] if more else None}


def notice(conn, project):
    next_command = "richi --project-path PATH rules review-preferences" if project else "richi rules review-preferences"
    try:
        page = pending(conn, project, preview=True)
    except (sqlite3.Error, ValueError, TypeError) as exc:
        # A damaged knowledge entry must not prevent delivery of standing instructions.
        return {"error": "Cannot preview historical preferences: " + str(exc)[:300], "next": next_command}
    if page["candidates"]:
        return {**page,
                "note": "Show these historical preferences to the user for review. Only an explicitly confirmed "
                        "preference becomes a standing rule.",
                "next": next_command}


def _previous_promotion(conn, stored, entry, project, args, check, api):
    # Earlier development versions stored only rule:<id>. They cannot prove the identity
    # of a rule that later reused the ID. Do not claim a successful retry on that evidence.
    previous = {}
    if stored.startswith("rule:"):
        rule_id, created_at = stored[len("rule:"):], None
    else:
        try:
            previous = json.loads(stored)
            rule_id, created_at = previous["rule_id"], previous["created_at"]
            rules.rule_id(rule_id)
            if not isinstance(created_at, str) or not created_at:
                raise ValueError
        except (TypeError, ValueError, KeyError):
            raise api.MemoryError("Unreadable preference review; inspect the standing rules")
    row = rules._row(conn, rule_id, False, api)
    if row is None:
        raise rules._failure(api, "Previously promoted rule was removed: " + rule_id, "conflict")
    try:
        rule = rules._readable_row(row[1], row[2])
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise rules._failure(api, "Cannot verify previously promoted rule: " + rule_id, "conflict") from exc
    if (rule["created_at"] != created_at or rule["project_id"] != project or rule["source"] != "entry:" + entry["id"]):
        raise rules._failure(api, "Cannot verify the original promoted rule at this ID: " + rule_id, "conflict")
    if (previous.get("reviewed_version") != args.expected_updated_at or rule["updated_at"] != created_at):
        raise rules._failure(api, "The preference or its promoted rule changed since confirmation; "
                             "inspect rules list and use rules update for a new decision", "conflict")
    wording = args.text if args.text is not None else entry["summary"]
    if (args.dismiss or (args.rule_id is not None and args.rule_id != rule_id)
            or rules._text(wording, api) != rule["text"]
            or ("check" in check and check["check"] != rule["check"])):
        raise rules._failure(api, "A different promotion decision is already recorded; inspect rule " + rule_id, "conflict")
    return {"status": "already_promoted", "rule_id": rule_id, "entry_id": entry["id"]}


def decide(conn, args, api, checked_project, author, settings):
    entry = api.entry_record(conn, args.entry_id)
    if not entry or not entry["id"].startswith("preference:") or entry["knowledge_state"] == "superseded":
        raise api.NotFoundError("No active preference entry with this ID")
    if entry["updated_at"] != args.expected_updated_at:
        raise rules._failure(api, "Preference changed; review its wording and scope again", "conflict")
    project = args.project
    if (project is None and entry["project_ids"]) or (project is not None and project not in entry["project_ids"]):
        raise api.MemoryError("Choose the scope shown for this preference by rules review-preferences")
    if project is not None and checked_project is not None and project != checked_project:
        raise api.MemoryError("Preference scope differs from the checked project")
    marker = _marker(entry["id"], project)
    found = conn.execute("SELECT value FROM metadata WHERE key = ?", (marker,)).fetchone()
    check = rules.check_options(args, api)
    if args.dismiss and (args.rule_id or args.text is not None or check or args.reconsider_dismissed):
        raise api.MemoryError("--dismiss takes no options for changing a rule")
    if found and found[0] != "dismissed":
        return _previous_promotion(conn, found[0], entry, project, args, check, api)
    if found and not args.dismiss and not args.reconsider_dismissed:
        raise rules._failure(api, "This scope was dismissed. Review the entry and obtain a new confirmation "
                             "before using --reconsider-dismissed.", "conflict")
    if not found and args.reconsider_dismissed:
        raise rules._failure(api, "This scope has no dismissal to reconsider", "conflict")
    if args.dismiss:
        decision = "dismissed"
        result = {"status": "dismissed", "entry_id": entry["id"], "project_id": project}
    else:
        rule_id = args.rule_id or _rule_id(entry["id"], project, len(entry["project_ids"]) > 1)
        request = {"action": "add", "id": rule_id, "scope": "project" if project else "global", "project": project,
                   "text": args.text if args.text is not None else entry["summary"], "source": "entry:" + entry["id"],
                   **check}
        result = rules.change(conn, request, api, checked_project, author, settings)
        decision = json.dumps({"rule_id": rule_id, "created_at": result["rule"]["created_at"],
                               "reviewed_version": entry["updated_at"]})
    conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (marker, decision))
    return result
