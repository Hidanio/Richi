"""Declarative platform registry for Richi Scenarios: capabilities, step blocks, checks, and domain views.

This module is the single source of truth for both agents (`richi scenario catalog`) and the
Map UI (`map_snapshot["platform"]`). New subsystems (such as Artifact Storage) and new domain
views plug into this registry without hardcoding schema version numbers or duplicating lists.
"""

SCENARIO_TABLES = frozenset({"scenario_runs", "scenario_steps", "scenario_events"})
ARTIFACT_TABLES = frozenset({"artifacts", "artifact_revisions", "artifact_files", "artifact_blobs"})


def existing_tables(conn):
    """Return the set of table names currently present in SQLite."""
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def has_scenario_tables(conn):
    """True when all scenario execution tables exist (safe across schema v4/v5 merges)."""
    return SCENARIO_TABLES.issubset(existing_tables(conn))


def has_artifact_tables(conn):
    """True when the Richi Artifact Storage tables exist in this database."""
    return ARTIFACT_TABLES.issubset(existing_tables(conn))


def detect_capabilities(conn):
    """Detect active Richi platform capabilities from live database tables."""
    tables = existing_tables(conn)
    has_scn = SCENARIO_TABLES.issubset(tables)
    has_art = ARTIFACT_TABLES.issubset(tables)
    return {
        "scenarios": has_scn,
        "review_hub": has_scn,
        "process_memory": has_scn,
        "artifacts": has_art,
    }


# Declarative catalog of built-in checks (single source of truth for validation, preflight, UI, agents).
CHECK_REGISTRY = {
    "data_required": {
        "required_fields": ("path",),
        "optional_fields": (),
        "project_bound": False,
        "description": "Require a non-null field at report.data.<path>.",
    },
    "min_items": {
        "required_fields": ("path", "count"),
        "optional_fields": (),
        "project_bound": False,
        "description": "Require report.data.<path> to be a list with at least <count> items.",
    },
    "exit_code": {
        "required_fields": ("expect",),
        "optional_fields": (),
        "project_bound": False,
        "description": "Require command report.exit_code to match <expect>.",
    },
    "file_exists": {
        "required_fields": ("path",),
        "optional_fields": (),
        "project_bound": True,
        "description": "Verify relative <path> exists as a regular file inside --project-path.",
    },
    "evidence_git": {
        "required_fields": (),
        "optional_fields": (),
        "project_bound": True,
        "description": "Verify report.evidence includes valid Git commit revision(s) present in --project-path.",
    },
    "no_dirty_git": {
        "required_fields": (),
        "optional_fields": ("mode",),
        "project_bound": True,
        "description": "Verify read-only analysis did not modify the Git worktree (mode: unchanged|clean).",
    },
    "findings_grounded": {
        "required_fields": (),
        "optional_fields": ("path",),
        "project_bound": True,
        "description": "Verify every finding in data.<path> (default: findings) cites an existing repo file, valid line, severity (P0/P1/P2/nit) and non-empty evidence.",
    },
}

PROJECT_BOUND_CHECKS = frozenset(
    kind for kind, spec in CHECK_REGISTRY.items() if spec["project_bound"]
)


# Declarative catalog of step blocks (agents and UI read availability directly from here).
STEP_BLOCK_CATALOG = (
    {
        "type": "instruction", "category": "agent", "role": "agent",
        "label": "Инструкция", "requires_capability": None,
        "description": "Free-form task executed by the chat agent in its sandbox.",
        "exposes": ["summary", "status", "data", "files", "artifacts"],
    },
    {
        "type": "skill", "category": "agent", "role": "agent",
        "label": "Скилл", "requires_capability": None,
        "description": "Invoke an installed agent skill (shared default or per_agent mapping).",
        "exposes": ["summary", "status", "data", "files", "artifacts"],
    },
    {
        "type": "command", "category": "agent", "role": "agent",
        "label": "Команда", "requires_capability": None,
        "description": "Run a shell command inside --project-path (templates require | shell).",
        "exposes": ["summary", "status", "data", "files", "artifacts", "exit_code"],
    },
    {
        "type": "gate", "category": "human", "role": "human",
        "label": "Вопрос человеку", "requires_capability": None,
        "description": "Pause execution until the human answers in chat.",
        "exposes": ["summary", "status", "answer", "comment"],
    },
    {
        "type": "checklist", "category": "human", "role": "human",
        "label": "Чек-лист", "requires_capability": None,
        "description": "Human verification checklist with required and optional items.",
        "exposes": ["summary", "status", "checked"],
    },
    {
        "type": "memory.recall", "category": "memory", "role": "richi",
        "label": "Поиск в памяти", "requires_capability": None,
        "description": "Automatic lexical/graph recall from Richi memory before agent steps.",
        "exposes": ["summary", "status", "data"],
    },
    {
        "type": "memory.brief", "category": "memory", "role": "richi",
        "label": "Сводка из памяти", "requires_capability": None,
        "description": "Automatic context brief + past runs of this scenario + optional scoped false-positive learnings.",
        "exposes": ["summary", "status", "data"],
    },
    {
        "type": "memory.record", "category": "memory", "role": "richi",
        "label": "Записать в память", "requires_capability": None,
        "description": "Persist confirmed outcomes/decisions to Richi memory linked to the run task.",
        "exposes": ["summary", "status", "data"],
    },
    {
        "type": "compare", "category": "analytics", "role": "richi",
        "label": "Сравнить с baseline", "requires_capability": "process_memory",
        "description": "Compare numeric/list metrics of an ancestor step against a pinned baseline or latest done run.",
        "exposes": ["summary", "status", "data"],
    },
    {
        "type": "condition", "category": "flow", "role": "richi",
        "label": "Условие", "requires_capability": None,
        "description": "Evaluate a safe boolean expression for downstream branching.",
        "exposes": ["summary", "status", "data"],
    },
    # Reserved extension slots implemented in Phase 4 once Artifact Storage is merged and executors ship.
    {
        "type": "artifact.fetch", "category": "storage", "role": "richi",
        "label": "Получить из хранилища", "requires_capability": "artifacts",
        "planned": True,
        "description": "Fetch versioned specs, reports or measurements from Richi Artifact Storage by task/project/tag.",
        "exposes": ["summary", "status", "data", "artifacts"],
    },
    {
        "type": "artifact.publish", "category": "storage", "role": "richi",
        "label": "Сохранить в хранилище", "requires_capability": "artifacts",
        "planned": True,
        "description": "Publish full reports or findings.json into Richi Artifact Storage as an immutable version_uid.",
        "exposes": ["summary", "status", "data", "artifacts"],
    },
)


# Declarative registry of built-in domain views inside the Scenarios tab.
# Adding a future domain (QA, Incidents, Releases) registers one entry here without rewriting tab routing.
DOMAIN_VIEWS = (
    {
        "id": "review",
        "title": "Ревью",
        "match_tags": ["review", "bug-hunt", "pre-commit"],
        "endpoint": "/api/scenario-review",
        "requires_capability": "review_hub",
        "widgets": [
            "signal_mix",
            "open_blockers",
            "step_signal_table",
            "disprove_yield",
            "fp_health",
            "findings_inbox",
            "fp_patterns",
        ],
        "empty_cta_templates": ["pre-commit-review", "bug-hunt"],
    },
)


def platform_catalog(conn):
    """Build a self-describing platform snapshot for agents (`scenario catalog`) and Map UI."""
    caps = detect_capabilities(conn)
    blocks = []
    for block in STEP_BLOCK_CATALOG:
        req = block["requires_capability"]
        active = (req is None or bool(caps.get(req))) and not block.get("planned")
        entry = {k: v for k, v in block.items() if k != "planned"}
        entry["available"] = active
        if block.get("planned"):
            entry["status"] = "activates_with_" + str(req)
        blocks.append(entry)
    checks = [
        {
            "kind": kind,
            "required_fields": list(spec["required_fields"]),
            "optional_fields": list(spec["optional_fields"]),
            "project_bound": spec["project_bound"],
            "description": spec["description"],
        }
        for kind, spec in CHECK_REGISTRY.items()
    ]
    views = [
        dict(view, available=(view["requires_capability"] is None or bool(caps.get(view["requires_capability"]))))
        for view in DOMAIN_VIEWS
    ]
    return {
        "view": "scenario-catalog",
        "capabilities": caps,
        "step_blocks": blocks,
        "checks": checks,
        "domain_views": views,
        "artifact_uri_contract": "richi://artifact/<id>@<version_uid>/<path>",
    }
