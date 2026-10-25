# Reproducible tracks evaluation

This protocol and `tests/fixtures/tracks.json` are frozen before timings. The
baseline is merged main `2084321` (resolved to a full SHA in the result file).
The candidate SHA must be passed explicitly. The harness compares the actual
installed runtime, launcher and SQL/HTML resource hashes against each declared commit and refuses stale
installations. Python and SQLite versions, seed, fixture/script hashes and raw
samples are saved. No user store or private corpus is accepted as input.

The completed run on runtime `f1ade897da23bb1f1514677387e3ac44379385d9` is in
[the measured results report](TRACKS_EVAL_RESULTS.md), with
[all machine samples](../evals/results/tracks-2026-10-25.json). It shows mixed
results: faster change polling and atomic writes, larger/slower native task
context, and no measured improvement in model solution quality.

## Corpus and acceptance

The seed is `20261025`; scales are **40, 1,000 and 10,000 total entries**. The
33 core entries contain 20 tasks, two tracks, two hypotheses, one canonical shared
experiment, five runs, two decisions and an outside-track fact. Remaining entries
are unrelated notes in the same projects. The technical track contains 19 tasks
and has no business goal. Two tasks also belong to the product track, which
pursues two independent document goal sources. One task is fully standalone.
All tasks are completed, while goal effect is unknown.

The fixture includes supporting, negative and infrastructure-failed runs. Small
and large dataset results remain separate; a second hypothesis explicitly records
conflicting observations under the same conditions. The old rejection is
`superseded`, while its negative evidence remains useful. Original task motivation
and a dated, sourced track direction change are both present.

| Case | Frozen expected behavior | Verification |
| --- | --- | --- |
| A01 | Standalone task has its own useful context and no fabricated track | Fixture invariants and installed context tests |
| A02 | Technical track needs no goal | Zero `pursues_goal` links; runtime creation |
| A03 | Two memberships, two independent goal sources, unchanged projects | Exact fixture sets and context deduplication tests |
| A04 | Same-project outsiders remain outside strict selection | Exact query result tests |
| A05 | Original motivation and dated revised direction coexist | Source and structured-field assertions |
| A06 | Historical rejection and negative evidence remain discoverable | Full-context required evidence set; bounded omissions tests |
| A07 | Shared experiment and each run exist once | Unique IDs, shared `informs` and canonical `run_of` links |
| A08 | Execution, outcome, knowledge state and applicability remain distinct | Structured outcome/condition assertions; no inferred assessment |
| A09 | Strict scope and workspace expansion are distinguishable | Scope metadata and strict-filter tests |
| A10 | Renaming preserves observed time and unknown applicability | Installed title-update test and benchmark rename |
| A11 | Completed tasks do not imply measured goal effect | Full counts and explicit unknown |
| A12 | Journal order/cursors handle ties, edits, detach and filter exit | Installed mutation and pagination tests |
| A13 | CAS, request replay and rollback preserve foreign edits | Installed transaction tests |
| A14 | Serialized Unicode/escaped/huge outputs obey budget and expose omissions | Installed output-budget tests |
| A15 | Wrong scope/store cursor cannot disclose another workspace | Installed cursor isolation tests |
| A16 | Explicit migration preserves data/history and old operations | Migration tests plus measured synthetic schema migration |

`test_track_eval.py` proves corpus properties, not runtime correctness. The
installed test suite must pass separately; the evaluation does not call a fixture
description a passed behavioral test. Full-context evidence checks are exact set
checks; latency has no CI pass threshold.

## Comparison arms

1. **Baseline CLI plus executed local Python.** `evals/track_baseline.py` uses
   existing `query`, `fetch`, graph neighbors and per-object history. Metadata
   scenarios use existing CLI commands directly, without an unnecessary wrapper.
   Context/history dispatch the installed public CLI `memory.main(argv)` inside
   one fresh postprocessor process, capturing stdout. Every call still executes
   the real parser, config resolution, guards and handlers; there are no raw SQL
   reads or reimplemented query functions. This strong baseline avoids repeated
   interpreter/launcher startups. `--dispatch subprocess` retains the slower
   full-launcher-per-command control. Dispatch counts and process counts are
   explicitly separate. All invocations use the harness's isolated synthetic
   home/config; the script is not a way to access user stores.
   Script stdout contains only the answer; optional `--metrics-file` keeps
   instrumentation local, so token replay does not silently discard visible
   diagnostic output.
   Explicit
   membership tags and JSON summaries are documented authoring conventions. Both
   stores retain identical summary facts, sources, tags, projects and graph links;
   the candidate also has validated structured research fields. SQL metadata
   selection and local processing avoid sending whole workspaces to a model.
2. **Native operations.** Exact task context, strict `--track` metadata selection,
   whole-scope aggregates, scoped event cursors and atomic research writes.
3. **Prepared compact summary control.** The actual baseline postprocessor output
   is saved once and reread in fresh processes. Preparation cost is reported;
   relevant edits invalidate this control. It is not a hand-selected model answer.
4. **Standalone control.** Both baseline and native context operate on task 20,
   which has no useful epic context. Prepared standalone output is also measured.

The first 16,000-character diagnostic returned all required research details for
the baseline but only four of ten evidence details natively; native counts and
continuation pointers remained explicit. Provenance/link/role output is larger.
The chosen context default is therefore **32,000 serialized JSON characters**,
and both arms receive this same budget in the measured run. This is not a token
limit or a claim of token savings. An explicit 16,000-character stress check is
retained in each scale's quality report.

Per scenario, 30 fresh-process observations are alternated baseline/native. Core
queries are measured separately in an open installed-runtime connection, with
parser construction and imports excluded by one untimed warm-up. OS caches
are shared; “fresh process” does not mean cold disk. Context, strict query, broad
project query, aggregate and changes operations are measured at every scale.
Result creation has 30 distinct IDs and includes its links: baseline uses two
sequential CLI writes (entry plus batched edges), candidate uses one atomic request. The weaker baseline
write contract is explicit. There is no weighted headline saving.

Baseline changes polls the bounded direct neighborhood's object histories with
per-object revision positions, saved in a local cursor file rather than all
included in model context. It has no global snapshot across CLI calls. Native
events are revision notifications; clients read current cards separately. These
are not falsely labeled identical history-replay contracts.

Storage measurements checkpoint both disposable databases consistently and report
freelist pages, allocated sizes, index/page attribution when SQLite `dbstat` is
available, build time, explicit migration time and growth after the write series.
Neither database is silently VACUUMed. Build timing includes fixture insertion and
index maintenance, not merely the final index DDL.

## Running

Install archived baseline and candidate normally into separate disposable virtual
environments, with no development redirect. Use a prepopulated tokenizer cache;
the benchmark refuses to download tokenization data during measurement.

```sh
TIKTOKEN_CACHE_DIR=/private/tmp/richi-tokenizer-cache \
  /private/tmp/richi-tracks-candidate-venv/bin/python evals/evaluate_tracks.py \
  --baseline-python /private/tmp/richi-tracks-baseline-venv/bin/python \
  --candidate-python /private/tmp/richi-tracks-candidate-venv/bin/python \
  --candidate-revision FULL_RUNTIME_COMMIT \
  --tokenizer-python /path/to/python-with-tiktoken \
  --output /private/tmp/tracks-results.json
```

`--smoke --repeats 1 --core-repeats 1` checks one small corpus and is marked
`smoke_only`; it cannot substantiate performance claims. A full run requires the
tokenizer and at least 30 repeats. The output path must not already exist.

The recorded run used normally installed wheels in separate disposable Python
3.9.6 virtual environments; neither environment redirected to development
source. The baseline wheel was built from the archived `2084321` source; the
candidate wheel was rebuilt from frozen `f1ade89`. Its exact evaluation command
was:

```sh
TIKTOKEN_CACHE_DIR=/private/tmp/richi-token-research/tokenizer-cache \
  /private/tmp/richi-tracks-candidate-venv/bin/python evals/evaluate_tracks.py \
  --baseline-python /private/tmp/richi-tracks-baseline-venv/bin/python \
  --candidate-python /private/tmp/richi-tracks-candidate-venv/bin/python \
  --candidate-revision f1ade897da23bb1f1514677387e3ac44379385d9 \
  --tokenizer-python /private/tmp/richi-token-research-venv/bin/python \
  --output /private/tmp/tracks-results-final.json
```

The linked machine-results file preserves all measurements. Only three disposable
migration database paths had their OS temporary-directory prefix replaced with
`<synthetic-temp>`; this path-only sanitization changes no samples or token counts.

Visible token accounting uses named/versioned `o200k_base`: skill/instructions,
task, argument text, baseline script and final output. Local intermediate CLI
bytes/calls are measured separately because they do not need to enter model
context. Cold replay includes the instruction prefix and script once. Warm replay
includes task/arguments/response; a complete session still owes the cold prefix.
This is an explicit independent-text replay, not actual billing, cache accounting,
hidden reasoning tokens or observed model behavior.

For context, the report separates first-response latency from time to sufficient
evidence. A deterministic replay follows advertised context continuation cursors
and then fetches explicitly omitted whole cards. It never consults the expected
reference set to decide what to read. Every response and argument from that
workflow enters the visible-token total. The frozen expected set is used only to
verify the completed workflow. A fast incomplete first page is never scored as
equivalent to a complete baseline answer.

## Model-quality stage: not run without an authorized runner

Before any model trials, freeze task prompts and a rubric requiring: correct
success criterion, durable-receipt constraint, prior negative result with its
conditions, scoped applicability, explicit unknown goal effect, no unsupported
causal claim, and no repeated experiment already answered under the same
conditions. Include task 01, task 02 and standalone task 20. Use the same named
model/version, system instructions, sampling settings, tools and token budget in
every arm. Randomize arm order; run at least 10 independent trials per task/arm.
Score anonymized outputs with an independent evaluator against the frozen facts;
record errors, unnecessary reruns, rework, calls and time to sufficient evidence.
Report every trial and confidence intervals, including failures. Authoring the
baseline script counts toward its cold session.

Without those real trials, only data availability, contract correctness, visible
context and operation latency/storage are measured. Improvement in engineering
solution quality, reasoning quality or business goal attainment remains untested.
