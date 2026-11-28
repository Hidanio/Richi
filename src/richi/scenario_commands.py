"""CLI declarations and dispatch for `richi scenario ...`."""
import argparse

from . import agents, rules, scenario_format, scenario_runs, scenarios


WRITE_ACTIONS = frozenset({
    "copy", "apply", "run", "next", "claim", "release", "report", "answer",
    "skip", "retry", "pause", "resume", "cancel",
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


def writes(args):
    return args.command == "scenario" and args.action in WRITE_ACTIONS


def read_request(args, api):
    """Read any file/stdin JSON payload BEFORE acquiring a SQLite write lock."""
    if args.command != "scenario":
        return None
    if args.action == "apply":
        return scenarios.read_json_input(args.json, api, limit=scenarios.MAX_CHANGE)
    if args.action == "report":
        return scenarios.read_json_input(args.json, api, limit=scenario_runs.MAX_REPORT_BYTES)
    if args.action in ("plan", "run") and getattr(args, "inputs_json", None):
        return scenarios.read_json_input(args.inputs_json, api, limit=scenarios.MAX_CHANGE)
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
    if action == "run":
        project_id = args.project if args.project is not None else checked_project
        secrets_runtime = _parse_secrets(None, api, getattr(args, "secret_envs", None))
        return scenario_runs.start_run(conn, api, database, args.id, project_id=project_id,
                                       project_path=project_path, raw_inputs=args.inputs,
                                       inputs_json=request, secrets_runtime=secrets_runtime, author=author)
    if action == "next":
        secrets_runtime = _parse_secrets(args.secrets, api, getattr(args, "secret_envs", None))
        return scenario_runs.next_step(conn, api, database, args.run_id, claim=args.claim, step_key=args.step_key,
                                       checked_project=checked_project, project_path=project_path,
                                       agent=args.agent, author=author, secrets_runtime=secrets_runtime, **installed_kw)
    if action == "claim":
        secrets_runtime = _parse_secrets(args.secrets, api, getattr(args, "secret_envs", None))
        return scenario_runs.claim_step(conn, api, database, args.run_id, args.step_key,
                                        checked_project=checked_project, project_path=project_path,
                                        agent=args.agent, author=author, secrets_runtime=secrets_runtime, **installed_kw)
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
