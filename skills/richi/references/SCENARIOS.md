# Scenarios

English | [Русский](SCENARIOS.ru.md)

A scenario is a reusable multi-step procedure that an agent follows in a chat:
typed input fields, generic steps (`instruction`, `skill`, `command`, `gate`,
`checklist`, `memory.recall`, `memory.brief`, `condition`), explicit
dependencies (`needs`), optional matrices, and report checks. Richi stores the
definition, tracks run state and step leases, creates a `task` entry tagged
`scenario-run` for each run, and writes every edit and run event to a unified
journal. Richi never executes shell commands or spawns agents itself.

## When to start a scenario

1. **Explicit request**: the user asks to run a scenario by name.
2. **Standing rule**: a rule directs the chat to use a scenario.
3. **Proposal**: when a stored scenario or built-in template fits the task
   (`when_to_use`), propose it and ask the user before starting it. Never start
   a scenario silently without consent or a standing rule.

## Discover, preview and manage definitions

```sh
richi --compact-json scenario list
richi --compact-json scenario templates
richi --compact-json scenario show review --agent codex
richi --compact-json scenario plan review --input branch=feature_x --agent codex
```

`scenario list` shows workspace-wide and project-scoped scenarios plus built-in
templates. `scenario show` resolves project scope first, then workspace-wide,
then built-in templates, and reports `resolved_skills` and `missing_skills` for
the active agent. `scenario plan` previews inputs, matrix expansion and skills
without writing.

To create an editable copy or change a stored scenario (only when the user asks):

```sh
richi --compact-json scenario copy review --project service-example --id service-review
richi --compact-json scenario apply --json change.json
```

`scenario apply` takes one JSON object with `action` (`add`, `update` or
`remove`), `scope` (`global` or `project`), `definition`, and
`expected_updated_at` for updates and removals. Definitions live in workspace
`metadata` and never appear in `recall` or `search`.

## Authoring a scenario from a user request

When the user asks you to create or modify a scenario (for example, *"create a
multi-stage review / release / triage scenario"*), author it directly via
`richi scenario apply --json -` and verify it immediately:

1. **Avoid unnecessary manual `inputs`**: do not ask the user to fill `branch`,
   `diff`, or `project_path` manually when the agent running in chat can inspect
   them automatically. Add `inputs` only for genuine user choices.
2. **Use the exact v1 step shapes**:
   - `instruction`: `params: {"text": "..."}` (add `hints: {"subagent": true}`
     for independent reviewer/critic steps).
   - `skill`: `params: {"skill": {"default": "skill-name"}, "instruction": "..."}`
     (check installed skills first or verify with `scenario plan`).
   - `command`: `params: {"command": "pytest -q"}` (every `{{...}}` in `command`
     must end with `| shell`).
   - `gate`: `params: {"question": "...", "options": ["Approve", "Revise"], "allow_comment": true}`.
   - `checklist`: `params: {"items": [{"text": "Tests pass", "required": true}]}`.
   - `memory.recall`: `params: {"query": "...", "limit": 10}` (runs automatically
     inside Richi before the agent).
   - `memory.brief`: `params: {"question": "...", "limit": 10}`.
   - `condition`: `params: {"expr": "steps.gate.answer == 'Approve'"}`.
3. **Wire explicit `needs` and verify**: set `needs: []` on parallel root steps
   and `needs: ["step_a", "step_b"]` on fan-in steps. Optionally include
   `layout.positions` (the map also auto-lays out omitted positions). After
   `scenario apply`, run `richi --compact-json scenario plan <id>` to confirm
   valid references and installed skills, then tell the user the scenario is
   ready in `richi map` → **Scenarios**.

```sh
richi --compact-json scenario apply --json - <<'EOF'
{"action":"add","scope":"global","definition":{"format":"richi-scenario","format_version":1,"id":"quick-check","title":"Quick verification","steps":[{"id":"review","type":"instruction","title":"Self-review","needs":[],"params":{"text":"Review current changes and list concrete issues."}},{"id":"confirm","type":"gate","title":"Confirm fixes","needs":["review"],"params":{"question":"Apply the listed fixes?","options":["Yes","No"],"allow_comment":true}}]}}
EOF
richi --compact-json scenario plan quick-check
```

## Run a scenario step by step

Runs require schema 4 (`richi migrate` backs up the store before upgrading).

```sh
richi --compact-json --project-path /path/to/service scenario run review --input branch=feature_x
richi --compact-json --project-path /path/to/service scenario next run-review-20261127-0001 --claim
richi --compact-json --project-path /path/to/service scenario report run-review-20261127-0001 angle.0 --json report.json
```

Loop while `scenario next RUN --claim` returns an agent step:

1. Read the step's rendered `text` / `instruction` / `command`, resolved
   `skill`, `hints` (such as `subagent`), `outputs` and `checks`.
2. Perform the step. For `command` steps, inspect `substituted_values` and run
   the command within your own sandbox and approvals.
3. Submit a JSON report with `status` (`done` or `failed`), `summary`, and any
   required `data`, `files`, `exit_code` or `evidence`. If Richi returns
   `status: "rejected"`, fix the listed `reasons` and report again; a rejected
   report never closes the step.

For parallel steps or explicit lease handoff (`claim` returns the full rendered step payload and accepts `--secret`, and `next` accepts `--step`):

```sh
richi --compact-json scenario claim run-review-20261127-0001 angle.1
richi --compact-json scenario next run-review-20261127-0001 --step angle.1 --claim
richi --compact-json scenario release run-review-20261127-0001 angle.1
```

## Gates, secrets and resuming across chats

- **Human gates and checklists**: when `scenario next` returns a `gate` or
  `checklist` step (`status: "waiting_human"`), ask the user in this chat and
  record their reply with `scenario answer`:

```sh
richi --compact-json scenario answer run-review-20261127-0001 approve all --comment 'Accepted'
```

- **Secret inputs**: `type: "secret"` values are never stored in SQLite, task
  entries or logs. If `scenario next` returns `missing_secret`, ask the user
  and pass `--secret KEY=VALUE` on `scenario next`.
- **Resume after compaction or in a new chat**: find the active run and
  continue explicitly:

```sh
richi --compact-json scenario runs --active --mine
richi --compact-json scenario status run-review-20261127-0001
```

## Control runs and inspect the journal

```sh
richi --compact-json scenario skip run-review-20261127-0001 angle.1 --reason 'No test changes'
richi --compact-json scenario retry run-review-20261127-0001 merge
richi --compact-json scenario pause run-review-20261127-0001
richi --compact-json scenario resume run-review-20261127-0001
richi --compact-json scenario cancel run-review-20261127-0001
richi --compact-json scenario log --run run-review-20261127-0001 --entries 50
```

Skipping a step requires `--reason` and still satisfies downstream `needs`
ordering unless a downstream step's `if` tests its status. `scenario log`
shows both definition edits and run events newest first. Each run also creates
a `task` entry tagged `scenario-run` that updates on milestone transitions
(`in_progress`, `blocked`, `done`).
