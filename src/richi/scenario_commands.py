"""CLI declarations and dispatch for `richi scenario ...`."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from . import agents, rules, scenario_eval, scenario_format, scenario_hooks, scenario_registry, scenario_review, scenario_runs, scenarios


WRITE_ACTIONS = frozenset({
    "copy", "apply", "run", "next", "claim", "release", "report", "answer",
    "skip", "retry", "pause", "resume", "cancel", "baseline", "feedback",
    "fork", "import", "golden", "eval",
})


def _argument_id(value):
    if not isinstance(value, str) or not scenario_format.SCENARIO_ID.fullmatch(value):
        raise argparse.ArgumentTypeError(scenario_format.SCENARIO_ID_HELP)
    return value


def _argument_agent(value):
    if not agents.valid_id(value):
        raise argparse.ArgumentTypeError(agents.ID_HELP)
    return value


def _argument_entries(value):
    if not value.isdigit() or not 1 <= int(value) <= scenarios.MAX_LOG:
        raise argparse.ArgumentTypeError("a number from 1 to %d" % scenarios.MAX_LOG)
    return int(value)


def add_commands(commands, api):
    known = ", ".join(agents.builtin_ids()) + " or one from skill-sources list"
    parser = commands.add_parser("scenario", help="Plan, run and manage multi-step agent scenarios")
    actions = parser.add_subparsers(dest="action", required=True)

    listing = actions.add_parser("list", help="List stored scenarios of this workspace and built-in templates")
    scope = listing.add_mutually_exclusive_group()
    scope.add_argument("--global", dest="global_scope", action="store_true", help="Only workspace-wide scenarios")
    scope.add_argument("--project", metavar="ID", help="Only scenarios of this registered project")

    actions.add_parser("templates", help="List built-in scenario templates shipped with Richi")

    show = actions.add_parser("show", help="Show one scenario definition with per-agent skill resolution")
    show.add_argument("id", type=_argument_id, help="Scenario or built-in template ID")
    show_scope = show.add_mutually_exclusive_group()
    show_scope.add_argument("--global", dest="global_scope", action="store_true", help="Read from workspace-wide scope only")
    show_scope.add_argument("--project", metavar="ID", help="Project scope to resolve first")
    show_scope.add_argument("--builtin", action="store_true", help="Read the built-in template even when shadowed")
    show.add_argument("--agent", metavar="ID", type=_argument_agent,
                      help="Agent ID for skill resolution, such as " + known + " (default: detected from shell)")

    copy_cmd = actions.add_parser("copy", help="Copy a built-in template or scenario into workspace or project scope")
    copy_cmd.add_argument("template", type=_argument_id, help="Source template or scenario ID")
    copy_target = copy_cmd.add_mutually_exclusive_group(required=True)
    copy_target.add_argument("--global", dest="global_scope", action="store_true", help="Store in workspace-wide scope")
    copy_target.add_argument("--project", metavar="ID", help="Store in this registered project")
    copy_cmd.add_argument("--id", dest="new_id", type=_argument_id,
                          help="New scenario ID (default: keep the source ID)")

    apply_cmd = actions.add_parser("apply", help="Add, update or remove a stored scenario from one JSON change object")
    apply_cmd.add_argument("--json", required=True, metavar="FILE",
                           help="File with one change object (action: add, update or remove); - reads from stdin")

    plan_cmd = actions.add_parser("plan", help="Preview a scenario run without writing to the store")
    plan_cmd.add_argument("id", type=_argument_id, help="Scenario or built-in template ID")
    plan_cmd.add_argument("--input", dest="inputs", action="append", metavar="KEY=VALUE",
                          help="Input value for the scenario (repeat for multiple inputs)")
    plan_cmd.add_argument("--inputs-json", metavar="FILE",
                          help="JSON object of input values; - reads from stdin")
    plan_cmd.add_argument("--project", metavar="ID", help="Project whose scope to resolve first")
    plan_cmd.add_argument("--agent", metavar="ID", type=_argument_agent,
                          help="Agent ID for skill resolution (default: detected from shell)")

    run_cmd = actions.add_parser("run", help="Start a new run of a scenario and create its run task entry")
    run_cmd.add_argument("id", type=_argument_id, help="Scenario or built-in template ID")
    run_cmd.add_argument("--input", dest="inputs", action="append", metavar="KEY=VALUE",
                         help="Input value for the run (repeat for multiple inputs; secrets are never stored)")
    run_cmd.add_argument("--inputs-json", metavar="FILE",
                         help="JSON object of input values; - reads from stdin")
    run_cmd.add_argument("--secret-env", dest="secret_envs", action="append", metavar="KEY=ENV_VAR",
                         help="Read a secret input from an environment variable (never stored; keeps value out of argv)")
    run_cmd.add_argument("--task", dest="task_id", metavar="ENTRY_ID",
                         help="Optional existing task/entry ID to link this run to in the knowledge graph")
    run_cmd.add_argument("--project", metavar="ID", help="Registered project for this run (default: --project-path project)")

    next_cmd = actions.add_parser("next", help="Get the next ready step of a run (and optionally claim it)")
    next_cmd.add_argument("run_id", metavar="RUN", help="Run ID from scenario run or scenario runs")
    next_cmd.add_argument("--claim", action="store_true", help="Claim the returned agent step with a 30-minute lease")
    next_cmd.add_argument("--step", dest="step_key", metavar="STEP",
                          help="Inspect or claim one specific actionable step (useful for parallel fan-out)")
    next_cmd.add_argument("--agent", metavar="ID", type=_argument_agent,
                          help="Agent ID filtering pinned steps and skills (default: detected from shell)")
    next_cmd.add_argument("--secret", dest="secrets", action="append", metavar="KEY=VALUE",
                          help="Runtime-only secret input for rendering this step (never stored)")
    next_cmd.add_argument("--secret-env", dest="secret_envs", action="append", metavar="KEY=ENV_VAR",
                          help="Read runtime-only secret input from an environment variable (keeps value out of argv)")

    claim_cmd = actions.add_parser("claim", help="Claim a specific ready agent step and return its rendered payload")
    claim_cmd.add_argument("run_id", metavar="RUN", help="Run ID")
    claim_cmd.add_argument("step_key", metavar="STEP", help="Step key, such as angle.0 or merge")
    claim_cmd.add_argument("--agent", metavar="ID", type=_argument_agent, help="Agent ID (default: detected from shell)")
    claim_cmd.add_argument("--secret", dest="secrets", action="append", metavar="KEY=VALUE",
                           help="Runtime-only secret input for rendering this step (never stored)")
    claim_cmd.add_argument("--secret-env", dest="secret_envs", action="append", metavar="KEY=ENV_VAR",
                           help="Read runtime-only secret input from an environment variable (keeps value out of argv)")

    release_cmd = actions.add_parser("release", help="Release a step lease held by this chat")
    release_cmd.add_argument("run_id", metavar="RUN", help="Run ID")
    release_cmd.add_argument("step_key", metavar="STEP", help="Claimed step key to release")

    report_cmd = actions.add_parser("report", help="Submit a step result JSON and validate it against outputs and checks")
    report_cmd.add_argument("run_id", metavar="RUN", help="Run ID")
    report_cmd.add_argument("step_key", metavar="STEP", help="Claimed step key to report")
    report_cmd.add_argument("--json", required=True, metavar="FILE",
                            help="JSON report object with status, summary, optional data/files/evidence; - reads stdin")

    answer_cmd = actions.add_parser("answer", help="Record the human's answer to a waiting gate or checklist step")
    answer_cmd.add_argument("run_id", metavar="RUN", help="Run ID")
    answer_cmd.add_argument("step_key", metavar="STEP", help="Waiting gate or checklist step key")
    answer_cmd.add_argument("value", help="Chosen option or answer value from the chat")
    answer_cmd.add_argument("--comment", help="Optional human comment stored with the answer")

    skip_cmd = actions.add_parser("skip", help="Skip a step with a recorded reason")
    skip_cmd.add_argument("run_id", metavar="RUN", help="Run ID")
    skip_cmd.add_argument("step_key", metavar="STEP", help="Step key to skip")
    skip_cmd.add_argument("--reason", required=True, help="Why this step was skipped")

    retry_cmd = actions.add_parser("retry", help="Retry a failed or blocked step")
    retry_cmd.add_argument("run_id", metavar="RUN", help="Run ID")
    retry_cmd.add_argument("step_key", nargs="?", metavar="STEP",
                           help="Failed or blocked step key (default: first failed/blocked step)")

    pause_cmd = actions.add_parser("pause", help="Pause an active scenario run")
    pause_cmd.add_argument("run_id", metavar="RUN", help="Run ID")

    resume_cmd = actions.add_parser("resume", help="Resume a paused scenario run")
    resume_cmd.add_argument("run_id", metavar="RUN", help="Run ID")

    cancel_cmd = actions.add_parser("cancel", help="Cancel an active scenario run")
    cancel_cmd.add_argument("run_id", metavar="RUN", help="Run ID")

    status_cmd = actions.add_parser("status", help="Show the full status and steps of one scenario run")
    status_cmd.add_argument("run_id", metavar="RUN", help="Run ID")

    runs_cmd = actions.add_parser("runs", help="List scenario runs in this workspace")
    runs_cmd.add_argument("--active", action="store_true", help="Only running, waiting or paused runs")
    chat_group = runs_cmd.add_mutually_exclusive_group()
    chat_group.add_argument("--chat", dest="filter_chat", metavar="ID", help="Only runs created or claimed by this chat")
    chat_group.add_argument("--mine", action="store_true", help="Only runs belonging to the current chat identity")
    runs_cmd.add_argument("--project", metavar="ID", help="Only runs of this registered project")
    runs_cmd.add_argument("--scenario", metavar="ID", type=_argument_id, help="Only runs of this scenario ID")
    runs_cmd.add_argument("--limit", type=api.limit, default=20, help="Maximum runs to list, 1-200 (default: %(default)s)")

    log_cmd = actions.add_parser("log", help="Show the unified scenario journal of definition changes and run events")
    log_cmd.add_argument("--id", dest="scenario_id", type=_argument_id, help="Only events for this scenario ID")
    log_cmd.add_argument("--run", dest="run_id", metavar="RUN", help="Only events for this run ID")
    log_cmd.add_argument("--entries", metavar="N", type=_argument_entries, default=20,
                         help="How many latest events to show, 1-%d (default: %%(default)s)" % scenarios.MAX_LOG)

    hook_cmd = actions.add_parser("hook", help="Handle an agent lifecycle hook (session-start, stop, pre-compact)")
    hook_cmd.add_argument("hook_event", choices=("session-start", "stop", "pre-compact"), metavar="EVENT",
                          help="Lifecycle event: session-start, stop or pre-compact")
    hook_cmd.add_argument("--agent", metavar="ID", type=_argument_agent,
                          help="Agent ID, such as " + known + " (default: detected from shell)")
    hook_cmd.add_argument("--project", metavar="ID", help="Project scope filter (default: --project-path project)")
    hook_cmd.add_argument("--format", dest="hook_format", choices=("richi", "claude", "codex", "cursor", "antigravity"),
                          default="richi", help="Output JSON schema: richi (default) or native agent hook contract")
    hook_cmd.add_argument("--max-continuations", type=int, default=scenario_hooks.DEFAULT_MAX_CONTINUATIONS,
                          metavar="N", help="Maximum automatic Stop continuations per claimed step, 1-20 (default: %(default)s)")

    base_cmd = actions.add_parser("baseline", help="Pin or clear a completed run as the baseline for compare steps")
    base_cmd.add_argument("run_id", metavar="RUN", help="Completed run ID to pin or unpin as baseline")
    base_cmd.add_argument("--clear", action="store_true", help="Clear the pinned baseline for this run's scenario and project")

    stats_cmd = actions.add_parser("stats", help="Show execution statistics across runs, scenarios and steps")
    stats_cmd.add_argument("--scenario", metavar="ID", type=_argument_id, help="Filter metrics to one scenario ID")
    stats_cmd.add_argument("--project", metavar="ID", help="Filter metrics to one registered project")
    stats_cmd.add_argument("--version", type=int, metavar="N", help="Filter metrics to one scenario version")
    stats_cmd.add_argument("--last", dest="last_n", type=api.limit, metavar="N",
                           help="Analyze only the latest N matching runs (1-200)")
    stats_cmd.add_argument("--kind", dest="run_kind", choices=("prod", "eval", "fork", "all"), default="prod",
                           help="Which run kinds to include (default: prod)")

    actions.add_parser("catalog", help="Describe active platform capabilities, step blocks, checks and domain views")

    fb_cmd = actions.add_parser("feedback", help="Record finding verdicts, resolutions and scoped FP patterns for a run")
    fb_cmd.add_argument("run_id", metavar="RUN", help="Scenario run ID containing the reviewed findings")
    fb_cmd.add_argument("--finding", metavar="ID", help="Finding ID (for example f:123456abcdef) for single-item feedback")
    fb_cmd.add_argument("--verdict", choices=scenario_review.VERDICTS, metavar="VERDICT",
                        help="Quality verdict: actionable_bug, valid_nit or false_positive")
    fb_cmd.add_argument("--resolution", choices=scenario_review.RESOLUTIONS, metavar="STATE",
                        help="Lifecycle resolution: open, fixed, wont_fix or unreviewed_dismissed")
    fb_cmd.add_argument("--note", metavar="TEXT", help="Optional short rationale or fix note (<= 1000 chars)")
    fb_cmd.add_argument("--remember-pattern", action="store_true",
                        help="Store a scoped confirmed false-positive fact entry (requires --verdict false_positive)")
    fb_cmd.add_argument("--scope-path", dest="scope_paths", action="append", metavar="PATH",
                        help="Relative repo path prefix where the FP pattern applies (repeatable)")
    fb_cmd.add_argument("--safety-condition", metavar="TEXT",
                        help="Condition proving the code is safe (10-500 chars; required with --remember-pattern)")
    fb_cmd.add_argument("--rev", metavar="REV", help="Optional Git revision where the FP pattern was verified")
    fb_cmd.add_argument("--title", metavar="TEXT", help="Optional custom title for the promoted FP fact entry")
    fb_cmd.add_argument("--batch", metavar="FILE",
                        help="Read a JSON array (or object with items) of feedback records from FILE or '-' for stdin")

    fork_cmd = actions.add_parser("fork", help="Fork a run from one step, freezing non-descendant steps by DAG needs")
    fork_cmd.add_argument("run_id", metavar="RUN", help="Source scenario run ID to fork")
    fork_cmd.add_argument("--from", dest="from_step", required=True, metavar="STEP",
                          help="Step ID to recompute along with all its downstream descendants")
    fork_cmd.add_argument("--use-latest", action="store_true",
                          help="Use the latest saved scenario definition instead of the source run's frozen snapshot")
    fork_cmd.add_argument("--dry-run", action="store_true",
                          help="Preview which steps stay frozen and which will recompute without writing")

    exp_cmd = actions.add_parser("export", help="Export a scenario definition as portable JSON with SHA-256 and security summary")
    exp_cmd.add_argument("id", type=_argument_id, help="Scenario or built-in template ID to export")
    exp_scope = exp_cmd.add_mutually_exclusive_group()
    exp_scope.add_argument("--global", dest="global_scope", action="store_true", help="Export from workspace-wide scope only")
    exp_scope.add_argument("--project", metavar="ID", help="Project scope to resolve first")
    exp_scope.add_argument("--builtin", action="store_true", help="Export the built-in template")
    exp_cmd.add_argument("--out", metavar="FILE", help="Write portable scenario JSON directly to FILE")
    exp_cmd.add_argument("--force", action="store_true", help="Overwrite an existing regular --out file (symlinks are still refused)")

    imp_cmd = actions.add_parser("import", help="Import a scenario JSON file with SHA-256 trust bound to project")
    imp_cmd.add_argument("file", metavar="FILE", help="Scenario JSON file path (or '-' for stdin)")
    imp_scope = imp_cmd.add_mutually_exclusive_group()
    imp_scope.add_argument("--global", dest="global_scope", action="store_true", help="Import into workspace-wide scope")
    imp_scope.add_argument("--project", metavar="ID", help="Import into this registered project (default: --project-path)")
    imp_cmd.add_argument("--trust-sha256", metavar="HEX",
                         help="Approve instruction/command/skill/memory.record/secret surfaces for this exact SHA-256")
    imp_cmd.add_argument("--replace", action="store_true", help="Update an existing scenario with the same ID")

    gold_cmd = actions.add_parser("golden", help="Manage fixed-commit golden bug cases for CLI scenario evaluation")
    gold_sub = gold_cmd.add_subparsers(dest="golden_action", required=True)
    gold_add = gold_sub.add_parser("add", help="Add or update a golden bug case from JSON")
    gold_add.add_argument("--json", required=True, metavar="FILE", help="Golden case JSON file path or '-' for stdin")
    gold_list = gold_sub.add_parser("list", help="List stored golden bug cases")
    gold_list.add_argument("--project", metavar="ID", help="Filter golden cases to one registered project")
    gold_rm = gold_sub.add_parser("remove", help="Remove a stored golden bug case")
    gold_rm.add_argument("case_id", metavar="ID", type=_argument_id, help="Golden case ID to remove")

    eval_cmd = actions.add_parser("eval", help="Run or score a scenario on a fixed-commit golden bug case")
    eval_cmd.add_argument("id", nargs="?", type=_argument_id,
                          help="Scenario ID to evaluate (optional with --score; verified when passed)")
    eval_cmd.add_argument("--case", dest="case_id", required=True, metavar="ID", type=_argument_id,
                          help="Golden case ID to evaluate against")
    eval_cmd.add_argument("--memory-mode", choices=scenario_eval.MEMORY_MODES, default="clean", metavar="MODE",
                          help="Memory isolation: clean (default), as_of_commit or with_fp_only")
    eval_cmd.add_argument("--score", dest="score_run", metavar="RUN",
                          help="Score an existing eval run instead of starting a new run")
    eval_cmd.add_argument("--compare-run", metavar="PREV",
                          help="Compare --score RUN against a previous run to show Paired Bug Diff")
    eval_cmd.add_argument("--project", metavar="ID", help="Project ID override (default: golden case or --project-path)")
    eval_cmd.add_argument("--input", dest="inputs", action="append", metavar="KEY=VALUE",
                          help="Optional scenario input override when starting an eval run (repeatable)")
    eval_cmd.add_argument("--inputs-json", metavar="FILE",
                          help="Optional JSON object of scenario inputs when starting an eval run; - reads stdin")


def writes(args):
    if args.command != "scenario":
        return False
    if args.action == "hook":
        return getattr(args, "hook_event", None) == "stop"
    if args.action == "fork":
        return not bool(getattr(args, "dry_run", False))
    if args.action == "golden":
        return getattr(args, "golden_action", None) != "list"
    if args.action == "eval":
        return getattr(args, "score_run", None) is None
    return args.action in WRITE_ACTIONS


def _validate_feedback_args(args, api):
    if getattr(args, "batch", None) is not None:
        if not isinstance(args.batch, str) or not args.batch.strip():
            err = api.MemoryError("--batch requires a nonempty file path or '-'")
            err.code = "usage_error"
            raise err
        conflicting = [
            flag for flag, val in (
                ("--finding", args.finding),
                ("--verdict", args.verdict),
                ("--resolution", args.resolution),
                ("--note", args.note),
                ("--remember-pattern", args.remember_pattern),
                ("--scope-path", args.scope_paths),
                ("--safety-condition", args.safety_condition),
                ("--rev", args.rev),
                ("--title", args.title),
            )
            if val is not None and val is not False
        ]
        if conflicting:
            err = api.MemoryError("--batch cannot be combined with " + ", ".join(conflicting))
            err.code = "usage_error"
            raise err
        return
    if not args.finding:
        err = api.MemoryError("Pass --finding ID (or --batch FILE/-)")
        err.code = "usage_error"
        raise err
    if not args.remember_pattern:
        pat_flags = [
            flag for flag, val in (
                ("--scope-path", args.scope_paths),
                ("--safety-condition", args.safety_condition),
                ("--rev", args.rev),
                ("--title", args.title),
            )
            if val is not None
        ]
        if pat_flags:
            err = api.MemoryError(", ".join(pat_flags) + " require --remember-pattern")
            err.code = "usage_error"
            raise err


def _validate_eval_args(args, api):
    if getattr(args, "score_run", None) is not None:
        if not args.score_run.strip():
            err = api.MemoryError("--score requires a nonempty run ID")
            err.code = "usage_error"
            raise err
        if args.compare_run is not None and not args.compare_run.strip():
            err = api.MemoryError("--compare-run requires a nonempty run ID")
            err.code = "usage_error"
            raise err
        extra_start_flags = [
            flag for flag, val in (
                ("--project", args.project),
                ("--input", args.inputs),
                ("--inputs-json", args.inputs_json),
            )
            if val is not None
        ]
        if extra_start_flags:
            err = api.MemoryError(", ".join(extra_start_flags) + " cannot be used with --score")
            err.code = "usage_error"
            raise err
        return
    if args.compare_run is not None:
        err = api.MemoryError("--compare-run requires --score RUN")
        err.code = "usage_error"
        raise err
    if not args.id:
        err = api.MemoryError("Pass a scenario ID to start an eval run, or pass --score RUN")
        err.code = "usage_error"
        raise err
    if getattr(args, "inputs_json", None) is not None and not str(args.inputs_json).strip():
        err = api.MemoryError("--inputs-json requires a nonempty file path or '-'")
        err.code = "usage_error"
        raise err


def read_request(args, api):
    """Read any file/stdin JSON payload BEFORE acquiring a SQLite write lock."""
    if args.command != "scenario":
        return None
    if args.action == "apply":
        return scenarios.read_json_input(args.json, api, limit=scenarios.MAX_CHANGE)
    if args.action == "report":
        return scenarios.read_json_input(args.json, api, limit=scenario_runs.MAX_REPORT_BYTES)
    if args.action == "eval":
        _validate_eval_args(args, api)
        if getattr(args, "score_run", None) is None and getattr(args, "inputs_json", None) is not None:
            return scenarios.read_json_input(args.inputs_json, api, limit=scenarios.MAX_CHANGE)
        return None
    if args.action in ("plan", "run") and getattr(args, "inputs_json", None) is not None:
        if not str(args.inputs_json).strip():
            err = api.MemoryError("--inputs-json requires a nonempty file path or '-'")
            err.code = "usage_error"
            raise err
        return scenarios.read_json_input(args.inputs_json, api, limit=scenarios.MAX_CHANGE)
    if args.action == "feedback":
        _validate_feedback_args(args, api)
        if getattr(args, "batch", None) is not None:
            return scenarios.read_json_input(args.batch, api, limit=scenario_review.MAX_FEEDBACK_BYTES, allow_list=True)
        return None
    if args.action == "import":
        return scenarios.read_json_input(args.file, api, limit=scenario_format.MAX_BYTES * 2)
    if args.action == "golden" and getattr(args, "golden_action", None) == "add":
        return scenarios.read_json_input(args.json, api, limit=scenario_eval.MAX_GOLDEN_BYTES)
    return None


def preflight(conn, args, api, *, request=None, project_path=None):
    """Run read-only settings/skill-catalog/filesystem/git work completely outside SQLite write locks (R2-J03, R3-I2, R3-A3)."""
    if args.command != "scenario":
        return None
    settings = agents.read_settings()
    author = rules.current_author(entries=settings.entries)
    out = {"settings": settings, "author": author}
    if args.action in ("next", "claim"):
        active_agent = getattr(args, "agent", None) or author.agent
        out["installed_skills"] = scenarios.installed_skills(active_agent, settings)
        will_claim = args.action == "claim" or bool(getattr(args, "claim", False))
        if will_claim and project_path is not None and scenario_registry.has_scenario_tables(conn):
            run_id = getattr(args, "run_id", None)
            needs_fp = False
            if run_id:
                with api.transaction(conn, write=False):
                    rrow = conn.execute("SELECT definition_json FROM scenario_runs WHERE id = ?", (run_id,)).fetchone()
                    if rrow and rrow[0]:
                        try:
                            defn = json.loads(rrow[0])
                        except (ValueError, TypeError):
                            defn = None
                        if isinstance(defn, dict) and isinstance(defn.get("steps"), list):
                            target_key = getattr(args, "step_key", None)
                            target_id = target_key.split(".", 1)[0] if isinstance(target_key, str) and target_key else None
                            for step in defn["steps"]:
                                if not isinstance(step, dict):
                                    continue
                                if target_id is not None and step.get("id") != target_id:
                                    continue
                                if any(isinstance(c, dict) and c.get("kind") == "no_dirty_git" for c in (step.get("checks") or ())):
                                    needs_fp = True
                                    break
                        if needs_fp:
                            from .projects import checkout_root, ensure_project_match
                            matched = ensure_project_match(conn, project_path)
                            effective_root = checkout_root(matched, project_path)
            if needs_fp:
                from . import scenario_findings
                out["claim_fingerprint"] = scenario_findings.compute_worktree_fingerprint(effective_root)
        return out
    if args.action != "report" or request is None:
        return out
    at_dt = scenario_runs._now_dt(api)
    prepared = None
    if project_path is not None:
        from .projects import checkout_root, ensure_project_match
        with api.transaction(conn, write=False):
            matched = ensure_project_match(conn, project_path)
            checked_project = matched["project"]["id"]
            effective_root = checkout_root(matched, project_path)
            prepared = scenario_runs.prepare_report_preflight(
                conn, api, args.run_id, args.step_key, request,
                checked_project=checked_project, project_path=effective_root,
                author=author, now_dt=at_dt,
            )
    else:
        with api.transaction(conn, write=False):
            prepared = scenario_runs.prepare_report_preflight(
                conn, api, args.run_id, args.step_key, request,
                checked_project=None, project_path=None,
                author=author, now_dt=at_dt,
            )
    out["report_io"] = scenario_runs.run_report_preflight_io(prepared)
    return out


def _parse_secrets(raw_pairs, api, raw_env_pairs=None):
    if not raw_pairs and not raw_env_pairs:
        return None
    import os
    result = {}
    for pair in raw_pairs or ():
        if "=" not in pair:
            err = api.MemoryError("secret must have the form KEY=VALUE")
            err.code = "invalid_input"
            raise err
        key, val = pair.split("=", 1)
        result[key] = val
    for pair in raw_env_pairs or ():
        if "=" not in pair:
            err = api.MemoryError("secret-env must have the form KEY=ENV_VAR")
            err.code = "invalid_input"
            raise err
        key, env_name = pair.split("=", 1)
        if not env_name or env_name not in os.environ:
            err = api.MemoryError("environment variable %r is not set for secret %r" % (env_name, key))
            err.code = "invalid_input"
            raise err
        result[key] = os.environ[env_name]
    return result


def run(conn, args, api, database, *, workspace=None, checked_project=None, project_path=None,
        request=None, preflight_io=None):
    action = args.action
    pf = preflight_io if isinstance(preflight_io, dict) else {}
    settings = pf.get("settings") if "settings" in pf else agents.read_settings()
    author = pf.get("author") if "author" in pf else rules.current_author(entries=settings.entries)
    installed_kw = {"installed_skills": pf["installed_skills"]} if "installed_skills" in pf else {}
    report_io = pf.get("report_io")

    if action == "templates":
        return scenarios.list_templates()
    if action == "list":
        return scenarios.listing(conn, api, workspace=workspace, global_scope=args.global_scope, project=args.project)
    if action == "show":
        # R2-J21: --project resolves project-first and falls back to global/builtin as documented.
        scope = "builtin" if args.builtin else ("global" if args.global_scope else None)
        project_id = args.project if args.project is not None else checked_project
        return scenarios.show(conn, api, args.id, project_id=project_id, scope=scope, agent=args.agent, settings=settings)
    if action == "copy":
        scope = "global" if args.global_scope else "project"
        return scenarios.copy_template(conn, api, args.template, new_id=args.new_id, scope=scope,
                                       project_id=args.project, checked_project=checked_project, author=author)
    if action == "apply":
        return scenarios.change(conn, request, api, checked_project=checked_project, author=author)
    if action == "plan":
        project_id = args.project if args.project is not None else checked_project
        return scenarios.plan(conn, api, args.id, project_id=project_id, project_path=project_path,
                              agent=args.agent, raw_inputs=args.inputs, inputs_json=request, settings=settings)
    if action == "log":
        return scenarios.log(conn, scenario_id=args.scenario_id, run_id=args.run_id, entries=args.entries)
    if action == "hook":
        project_id = args.project if args.project is not None else checked_project
        event = args.hook_event
        if event == "session-start":
            res = scenario_hooks.session_start(conn, api, chat=author.by, agent=args.agent or author.agent, project_id=project_id)
        elif event == "stop":
            res = scenario_hooks.stop_check(conn, api, chat=author.by, agent=args.agent or author.agent,
                                            project_id=project_id, project_path=project_path,
                                            max_continuations=args.max_continuations)
        else:
            res = scenario_hooks.pre_compact(conn, api, chat=author.by, agent=args.agent or author.agent, project_id=project_id)
        return scenario_hooks.format_agent_output(event, res, args.hook_format)
    if action == "run":
        project_id = args.project if args.project is not None else checked_project
        secrets_runtime = _parse_secrets(None, api, getattr(args, "secret_envs", None))
        return scenario_runs.start_run(conn, api, database, args.id, project_id=project_id,
                                       project_path=project_path, raw_inputs=args.inputs,
                                       inputs_json=request, secrets_runtime=secrets_runtime,
                                       task_id=getattr(args, "task_id", None), author=author)
    if action == "baseline":
        return scenario_runs.set_baseline(conn, api, args.run_id, clear=args.clear,
                                          checked_project=checked_project, author=author)
    if action == "stats":
        project_id = args.project if args.project is not None else checked_project
        return scenario_runs.scenario_stats(
            conn, api, scenario_id=args.scenario, project_id=project_id,
            version=getattr(args, "version", None), last_n=getattr(args, "last_n", None),
            run_kind=getattr(args, "run_kind", "prod"),
        )
    if action == "catalog":
        return scenario_registry.platform_catalog(conn)
    if action == "feedback":
        _validate_feedback_args(args, api)
        if getattr(args, "batch", None) is not None:
            items = request.get("items") if isinstance(request, dict) else request
            if not isinstance(items, list):
                err = api.MemoryError("--batch JSON must be a list of items or an object with 'items'")
                err.code = "invalid_input"
                raise err
        else:
            item = {"finding_id": args.finding}
            if args.verdict is not None:
                item["verdict"] = args.verdict
            if args.resolution is not None:
                item["resolution"] = args.resolution
            if args.note is not None:
                item["note"] = args.note
            if args.remember_pattern:
                item["remember_pattern"] = True
                item["scope_paths"] = args.scope_paths or []
                item["safety_condition"] = args.safety_condition
                if args.rev:
                    item["created_rev"] = args.rev
                if args.title:
                    item["title"] = args.title
            items = [item]
        return scenario_review.record_feedback(
            conn, api, args.run_id, items,
            checked_project=checked_project, author=author, allow_remember_pattern=True,
        )
    if action == "fork":
        return scenario_eval.fork_run(
            conn, api, database, args.run_id, args.from_step,
            use_latest=args.use_latest, dry_run=args.dry_run,
            checked_project=checked_project, project_path=project_path, author=author,
        )
    if action == "export":
        if args.force and args.out is None:
            err = api.MemoryError("--force requires --out FILE")
            err.code = "usage_error"
            raise err
        scope = "builtin" if args.builtin else ("global" if args.global_scope else None)
        return scenario_eval.export_scenario(
            conn, api, args.id, project_id=args.project, scope=scope, checked_project=checked_project,
        )
    if action == "import":
        scope = "global" if args.global_scope else "project"
        return scenario_eval.import_scenario(
            conn, api, request, scope=scope, project_id=args.project, checked_project=checked_project,
            trust_sha256=args.trust_sha256, replace=args.replace, author=author,
        )
    if action == "golden":
        ga = args.golden_action
        if ga == "add":
            return scenario_eval.golden_add(conn, api, request, checked_project=checked_project, author=author)
        if ga == "list":
            project_id = args.project if args.project is not None else checked_project
            return scenario_eval.golden_list(conn, api=api, project_id=project_id)
        if ga == "remove":
            return scenario_eval.golden_remove(conn, api, args.case_id, checked_project=checked_project)
    if action == "eval":
        _validate_eval_args(args, api)
        if args.score_run is not None:
            return scenario_eval.eval_score(
                conn, api, args.case_id, args.score_run.strip(),
                compare_run_id=args.compare_run.strip() if args.compare_run is not None else None,
                checked_project=checked_project,
                expected_scenario_id=args.id,
            )
        return scenario_eval.eval_start(
            conn, api, database, args.id, args.case_id,
            memory_mode=args.memory_mode, project_id=args.project,
            raw_inputs=args.inputs, inputs_json=request,
            checked_project=checked_project, project_path=project_path, author=author,
        )
    if action == "next":
        secrets_runtime = _parse_secrets(args.secrets, api, getattr(args, "secret_envs", None))
        return scenario_runs.next_step(conn, api, database, args.run_id, claim=args.claim, step_key=args.step_key,
                                       checked_project=checked_project, project_path=project_path,
                                       agent=args.agent, author=author, secrets_runtime=secrets_runtime,
                                       claim_fingerprint=pf.get("claim_fingerprint"), **installed_kw)
    if action == "claim":
        secrets_runtime = _parse_secrets(args.secrets, api, getattr(args, "secret_envs", None))
        return scenario_runs.claim_step(conn, api, database, args.run_id, args.step_key,
                                        checked_project=checked_project, project_path=project_path,
                                        agent=args.agent, author=author, secrets_runtime=secrets_runtime,
                                        claim_fingerprint=pf.get("claim_fingerprint"), **installed_kw)
    if action == "release":
        return scenario_runs.release_step(conn, api, args.run_id, args.step_key,
                                          checked_project=checked_project, author=author)
    if action == "report":
        return scenario_runs.report_step(conn, api, database, args.run_id, args.step_key, request,
                                         checked_project=checked_project, project_path=project_path,
                                         author=author, preflight_io=report_io)
    if action == "answer":
        return scenario_runs.answer_step(conn, api, database, args.run_id, args.step_key, args.value,
                                         comment=args.comment, checked_project=checked_project,
                                         project_path=project_path, author=author)
    if action == "skip":
        return scenario_runs.skip_step(conn, api, database, args.run_id, args.step_key, args.reason,
                                       checked_project=checked_project, project_path=project_path, author=author)
    if action == "retry":
        return scenario_runs.retry_step(conn, api, database, args.run_id, args.step_key,
                                        checked_project=checked_project, project_path=project_path, author=author)
    if action in ("pause", "resume", "cancel"):
        return scenario_runs.set_run_state(conn, api, database, args.run_id, action,
                                           checked_project=checked_project, project_path=project_path, author=author)
    if action == "status":
        return scenario_runs.run_status(conn, api, args.run_id, checked_project=checked_project)
    if action == "runs":
        if args.mine:
            if not author.by:
                err = api.MemoryError("--mine requires a chat identity (--chat or RICHI_CHAT_ID)")
                err.code = "usage_error"
                raise err
            chat_filter = author.by
        else:
            chat_filter = args.filter_chat
        project_id = args.project if args.project is not None else checked_project
        return scenario_runs.list_runs(conn, api, active=args.active, chat=chat_filter,
                                       project_id=project_id, scenario_id=args.scenario, limit=args.limit)
    raise api.MemoryError("Unknown scenario action: " + str(action))


def write_export_file(args, res, api, database):
    """Write exported scenario JSON atomically outside any SQLite transaction (no symlinks, no DB clobber)."""
    raw_out = getattr(args, "out", None)
    if not isinstance(raw_out, str) or not raw_out.strip() or "\x00" in raw_out:
        err = api.MemoryError("--out must be a valid file path")
        err.code = "invalid_input"
        raise err
    out_path = Path(raw_out).expanduser()
    if out_path.is_symlink():
        err = api.MemoryError("Refusing to write export to a symlink: " + str(out_path))
        err.code = "invalid_input"
        raise err
    resolved_out = out_path.resolve(strict=False)
    resolved_db = Path(database).expanduser().resolve(strict=False)
    db_files = {resolved_db.name, resolved_db.name + "-wal", resolved_db.name + "-shm", resolved_db.name + "-journal"}
    db_files_folded = {n.casefold() for n in db_files}
    db_candidates = [resolved_db.parent / n for n in db_files]
    is_db_target = False
    if resolved_out.parent == resolved_db.parent and resolved_out.name.casefold() in db_files_folded:
        is_db_target = True
    if not is_db_target and out_path.exists():
        for cand in db_candidates:
            try:
                if cand.exists() and os.path.samefile(str(out_path), str(cand)):
                    is_db_target = True
                    break
            except OSError:
                continue
    if not is_db_target and out_path.parent.exists() and resolved_db.parent.exists():
        try:
            if os.path.samefile(str(out_path.parent), str(resolved_db.parent)) and out_path.name.casefold() in db_files_folded:
                is_db_target = True
        except OSError:
            pass
    if is_db_target:
        err = api.MemoryError("Refusing to overwrite the store database file: " + str(out_path))
        err.code = "invalid_input"
        raise err
    if out_path.exists() and out_path.is_dir():
        err = api.MemoryError("Export --out path is a directory: " + str(out_path))
        err.code = "invalid_input"
        raise err
    force = bool(getattr(args, "force", False))
    if out_path.exists() and not force:
        err = api.MemoryError("Output file already exists (pass --force to overwrite): " + str(out_path))
        err.code = "exists"
        raise err
    text = json.dumps(res["definition"], ensure_ascii=False, indent=2) + "\n"
    tmp_name = None
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(out_path.parent),
            prefix=".richi-export-", suffix=".tmp", delete=False,
        ) as handle:
            tmp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if out_path.is_symlink():
            err = api.MemoryError("Refusing to write export to a symlink: " + str(out_path))
            err.code = "invalid_input"
            raise err
        if not force:
            try:
                os.link(tmp_name, str(out_path))
            except FileExistsError as exc:
                err = api.MemoryError("Output file already exists (pass --force to overwrite): " + str(out_path))
                err.code = "exists"
                raise err from exc
            os.unlink(tmp_name)
            tmp_name = None
        else:
            os.replace(tmp_name, str(out_path))
            tmp_name = None
    except OSError as exc:
        err = api.MemoryError("Cannot write export file: " + str(exc))
        err.code = "io_error"
        raise err from exc
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    res["written_to"] = str(out_path)
    return res
