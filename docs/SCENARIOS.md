# Scenarios in Richi

English | [Русский](SCENARIOS.ru.md)

Scenarios turn repeatable multi-step agent work—branch reviews, load tests,
regression checks, reports, incident triage—into structured procedures backed
by Richi memory and verified step reports.

## How M1 works

1. **Agent executes in chat; Richi owns state and checks.** Richi never spawns
   subprocesses or agents. An agent starts a run (`scenario run`), pulls steps
   one by one (`scenario next --claim`), performs each step inside its own
   permissions and sandbox, and reports results (`scenario report`).
2. **Generic building blocks.** Format v1 defines typed inputs (`string`,
   `text`, `number`, `boolean`, `select`, `multiselect`, `secret`), explicit
   dependency graphs (`needs`), conditions (`if`), bounded matrices (up to 50
   instances with shell-safe keys `<step>.<idx>`), per-agent skill selection,
   and report validation (`outputs`, `data_required`, `min_items`, `exit_code`,
   `file_exists`, `evidence_git`).
3. **Safe templates.** `{{path | filter}}` templates and conditions are parsed
   without `eval`. Every expression inside `command.params.command` must end
   with `| shell` (`shlex.quote`). Secret inputs are allowed only in
   agent-facing execution params and presence checks, and are never persisted
   in SQLite.
4. **Run records and journal.** Every run creates a `task` entry tagged
   `scenario-run` (updated on milestone transitions) and logs every edit and
   run event in `scenario_events` (`scenario log`).
5. **Built-in Review template.** Richi ships a synthetic `review` template
   (`memory.brief` → parallel `skill` angles → `instruction` merge → human
   `gate`) usable out of the box or copied with `scenario copy`.

## Quick example

```sh
richi migrate
richi scenario plan review --input branch=feature_x
richi --project-path /path/to/service scenario run review --input branch=feature_x
richi --project-path /path/to/service scenario next run-review-20261127-0001 --claim
```

For the full agent command contract, see
[SCENARIOS.md](../skills/richi/references/SCENARIOS.md).

## Roadmap

- **M1 (this slice)**: format v1, safe templating, workspace/project storage,
  schema v4 run tables, scheduler, leases, report validation, run task entries,
  unified journal, built-in `review` template, CLI and EN/RU documentation.
- **M2**: agent lifecycle hooks (`SessionStart`, `Stop`, `PreCompact`) with
  Richi-side continuation counting.
- **M3**: process memory (`compare` against prior run baselines, `memory.record`,
  prior run findings in `memory.brief`).
- **M4**: Scenarios tab in `richi map` (library, drag-and-drop list editor with
  wiring, inspector, run view, journal).
- **M5–M9**: presets & gallery, repository export/import with trust-by-hash,
  free Blueprints-style canvas over the same explicit graph format,
  retrospectives, cross-agent handoff, forks and triggers.
