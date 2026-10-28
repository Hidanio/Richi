<a id="компактный-json-и-короткий-входной-skill"></a>
# Compact JSON and a shorter entry skill

English | [Русский](COMPACT_OUTPUT.ru.md)

`richi --compact-json …` removes indentation and spaces between JSON fields
in successful responses. The flag is global: place it before the command,
alongside `--project-path` or other permitted selectors. The default format
is unchanged.

```sh
richi --compact-json --project-path /work/example recall 'почему выбран кеш' --project example
richi --compact-json --project-path /work/example entry get task:ABC-123
richi --compact-json config show
```

Compact JSON preserves field values, Unicode and the final newline. It does not
shorten record content, introduce a ranker or change the storage schema.
Budgeted responses use one renderer for fitting and printing;
`budget.output_chars` equals actual stdout Unicode length including the newline.
Characters, UTF-8 bytes and tokens are different units.

With ample budget, only presentation and `budget.output_chars` change. Under the
same tight `--max-chars`, additional sentences, sources, cards or edges may fit;
such output need not contain fewer tokens. To deliberately limit context,
choose the budget separately and inspect omissions. Budget-empty output does
not become `no_match`.

The flag applies to successful runtime and launcher stdout JSON, including
`config`, `workspace`, `chat`, `status` and `update`. It is not a storage selector
and does not weaken chat binding or the project guard. Errors/stderr, help and
version retain their format. `--compact-json map` is rejected before map startup;
HTTP and process logging are unchanged. `--output` files, canonical hashing JSON
and internal work limits retain their existing representation. A literal after
the command or `--` is not reinterpreted by the launcher as this flag.

Runtime mode is held in a `ContextVar` only for the CLI invocation and resets
even after an exception. APIs/server still default to pretty JSON. The launcher
formats its responses without importing the selected runtime, so settings
recovery remains available with a broken dev checkout. Updating the launcher
requires reinstalling the fixed package; a dev reload alone is insufficient.

<a id="skill-и-маршруты-к-подробностям"></a>
## Skill and routes to details

The main [SKILL.md](../skills/project-memory/SKILL.md) covers workspace selection,
chat/project guards, search or direct get for a known ID, incomplete excerpts,
write authorization, full get/CAS, sources and states. References are loaded
according to the task. Update an installed skill by copying the entire
`skills/project-memory` directory, including all `references/` files.

Frequent operations have focused, self-contained instructions:
[writing](../skills/project-memory/references/ENTRY_WRITE.md),
[search diagnostics](../skills/project-memory/references/RECALL_DEBUG.md),
[reading neighbors](../skills/project-memory/references/NEIGHBORS.md),
[read-only audit](../skills/project-memory/references/MEMORY_AUDIT.md) and
[capture/attach](../skills/project-memory/references/GIT_CAPTURE.md).
This material moved from broader guides; their remaining sections cover advanced
operations. A known ID can be read directly under the core instructions without
loading the writing guide.

| Previous section | Where rules are preserved |
| --- | --- |
| Workspace/runtime/project selection | Required core guards; procedures in WORKSPACES |
| Recall and incomplete output | Core, RECALL; missing ID in RECALL_DEBUG |
| Recording results and current task cards | Core and ENTRY_WRITE; splitting in MAINTENANCE |
| Memory maintenance | MEMORY_AUDIT for decisions; MAINTENANCE for backups and mutation order |
| Search misses and experiments | EVALUATION |
| Sources, snapshots, code and diffs | GIT_CAPTURE, DIAGNOSTICS, GIT, BRIEF, IMPACT |
| Graph, concepts and ambiguity | NEIGHBORS for reads; GRAPH, ENCYCLOPEDIA for other operations |

When an ID is known and the full record is needed, call `get` directly. `read`
remains optional; do not pay for recall/read before the same get. Request history
separately. Compact serialization does not remove the need to read conditions
and exceptions or verify the current release. For old installations that reject
the flag, the skill permits retrying without it while preserving selectors;
changing runtime merely for formatting is unnecessary.

<a id="измерение-22-октября-2026"></a>
## Measurement on October 22, 2026

Baseline: `748fc91e2308d04787d32c15db36e259a7af58f4`. Both versions were installed
normally in separate temporary venvs, Python 3.9.6 / SQLite 3.43.2. One synthetic
corpus was created through baseline CLI and then only read by both versions.
Unchanged `tests/fixtures/card_maintenance.json` contains 7 questions, 5 current
records and 17 atomic sourced facts. Personal memory was not used.

The [runner](../evals/evaluate_compact_output.py) measures 31 scenarios × 3 variants:
baseline pretty, candidate pretty, candidate compact. These are 7 recall calls
at budgets 100000/2000, 5 reads at the same budgets, 5 full gets and 2 explains.
Raw stdout, exact code/fixture/runner hashes and results are in the
[report](../evals/results/compact-output-2026-10-22.json).

- Pretty stdout matched baseline in all 31 scenarios, without removing timestamps.
- Parsed JSON matched in all 19 ample/full scenarios except `budget.output_chars`.
- All 17 sourced facts remained in full get. Code and database/WAL were unchanged.
- Payload changed in 7 of 12 tight scenarios because more content fitted.
- Violations of checked invariants: 0.

Sum of seven recall calls with ample budget: a fixed query collection, not a
claim that every real task runs all seven:

| Format | o200k_base | cl100k_base |
| --- | ---: | ---: |
| Pretty | 17717 | 20339 |
| Compact | 13860 | 16362 |
| Reduction | 21.8% | 19.6% |

At 2000 characters, missing required facts across all questions fell from 19 to
11, but the compact package remained incomplete in 6 of 7 questions. Total
cl100k tokens rose from 3483 to 3558 due to additional content; o200k fell from
3434 to 3253. This demonstrates a completeness/length tradeoff rather than an
encoding regression. Full-record reading remains required.

The skill was measured separately for initial version `1eb690b`, before splitting
references by operation. This historical table does not describe today's skill
structure. References count only in the named routes, each file once; this is
not a measurement of actual client behavior:

| Loaded instructions | Before, o200k / cl100k | After, o200k / cl100k |
| --- | ---: | ---: |
| SKILL.md only | 6638 / 6690 | 1474 / 1475 |
| Core + RECALL | 8563 / 9702 | 3399 / 4487 |
| Core + CLI for writing | 12187 / 14634 | 7702 / 10425 |
| Core + MAINTENANCE + DIAGNOSTICS | 14418 / 18319 | 9254 / 13104 |
| Same + CLI + GRAPH for changes | 22913 / 30411 | 18428 / 26202 |

An approximately 78% reduction in the entry file does not imply equivalent
whole-task savings. Some operations need additional references. Individual sizes
are retained separately in the report; their sum is not treated as permanent
overhead for every call. Rule relocation is also checked through action-selection
scenarios, but that is not a statistical assessment of model quality.

Token counts use optional evaluation dependency `tiktoken==0.14.0`.
`o200k_base`/`cl100k_base` are text proxies, not claims about the current Codex
tokenizer. Counts cover stdout or named skill files; they exclude prompts,
tool framing, replayed history, reasoning/output and cache billing. Latency,
money, model selection of the right record and final-answer quality were not
measured. Exact fact/source auditing is not a semantic proof. This corpus has
no active concept ambiguity; a separate CLI test covers it. Old evaluation
expectations were unchanged.

<a id="повторение-и-проверки"></a>
## Reproduction and checks

Create a baseline checkout at the specified commit and normal installations of
both versions in disposable venvs. The runner's Python may have tiktoken installed;
the production package remains stdlib-only. Without `--tokenizer-cache`, the
runner checks length and behavior without counting tokens. Output must be a new file.

```sh
python evals/evaluate_compact_output.py \
  --baseline-source /tmp/richi-baseline \
  --baseline-python /tmp/richi-baseline-venv/bin/python \
  --candidate-source /tmp/richi-candidate \
  --candidate-python /tmp/richi-candidate-venv/bin/python \
  --tokenizer-cache /tmp/tokenizer-cache \
  --output /tmp/compact-output-new.json
```

The runner checks installed code against source hashes, the fixed baseline,
unchanged fixture and code/skill/database immutability during the run. Synthetic
subprocesses are isolated from user chat IDs, Richi/Python selectors and HOME.
This is a test mechanism, not permission to bypass chat binding for real memory.
New fixture-creation timestamps can change bytes/tokens; retained stdout allows
exact recounting of this run.

Focused CLI tests cover both formats, tight budgets, ambiguity/no-match,
full get/history, sources, Git commands/files, nested contexts and reset after
errors, early launcher commands, release/dev, broken dev and flag-like payloads.
General checks: `python -m unittest discover -s tests -v`,
`python evals/evaluate_recall.py` and the standard skill validator.

October 22 validation: 586 tests passed, 1 Linux-only test skipped on macOS.
The first full run had 29 sandbox errors opening loopback ports; exactly those
29 tests were rerun with local-server permission and passed. This is the combined
result of two runs, not a single fully green run. Acceptance cases: 7/7, database
unchanged. Skill validation and Markdown links passed. Final independent code
review found no blockers.

A separate agent additionally checked the shortened skill without the old skill,
implementation or previous conclusions. It read the new core and required
references, then proposed actions for seven synthetic situations. It ran no real
Richi commands and changed no memory in this check.

| Situation | Checked decision |
| --- | --- |
| New unbound chat, only one workspace | Ask for selection before memory access; independent code work may continue |
| Chat bound to alpha, global selection beta | Work in alpha without choosing again or searching beta |
| Known task ID, incomplete excerpt and old implemented state | Direct full get; verify current sources and deployment before concluding |
| Updating after a CAS conflict | Reread the full record, reconcile changes, retry with fresh CAS |
| Consolidating records with a shared alias but different experiments | Preserve differences; back up before justified changes; do not absorb the failure |
| Preserving a historical failure with an unavailable report | Distinguish event and verification dates; invent no sources, freshness or release |
| Known fact ID and old CLI without compact support | Retry get without the flag and with the same selectors; do not switch workspace/runtime |

No loss of mandatory rules was found in these seven answers. This is a qualitative
review of one agent's proposed actions, not a repeatable statistical benchmark
or validation of actual command execution or tool-call counts. Core/reference
versions are anchored by hashes in the measurement report.

<a id="инструкции-по-операциям-проверка-всей-работы"></a>
## Operation guides: evaluating the whole workflow

The first private A/B comparison of long and short core skills showed no stable
saving in complete reading: −2.6% o200k and +0.6% cl100k. Agents loaded large
reference guides. The next change therefore extracts ENTRY_WRITE, GIT_CAPTURE,
MEMORY_AUDIT, NEIGHBORS and RECALL_DEBUG. Material was moved, condensed and written
in English like the core; those individual effects were not measured separately.

The repeated comparison uses baseline `1eb690b`, already a short skill but before
reference splitting. Two fresh sessions per version receive six identical
questions in forward and reverse order; context can be reused within a session.
Runtime `1eb690b`, compact JSON, selected-memory snapshot and primary documents
are identical. Predetermined acceptance: at least 10% reduction with both
tokenizers, a win in each pair, no material regressions, exact update drafts and
unchanged data.

| All tool-returned text, two sessions | Before | After | Reduction |
| --- | ---: | ---: | ---: |
| o200k_base | 76939 | 56870 | 26.1% |
| cl100k_base | 103920 | 69172 | 33.4% |
| Read/CLI operations | 40 | 40 | 0% |

Individual repeats reduced o200k by 25.3% and 26.9%, cl100k by 33.0% and 33.9%.
All four full drafts preserved fields and exact CAS. Independent validation of
the new version passed 12/12 main answers. One old-version answer had a minor
inaccuracy about `sources related` and `--repo`. The reviewer did not know the
version mapping or metrics; guide names quoted in answers remained visible,
so masking was incomplete.

A separate new task about two graph edges was hidden from the change author
until sources were frozen. Both versions answered correctly in fresh sessions;
volume fell 30.1% / 31.1%. This result was not added to the main total. However,
one new-version graph response still contained 242850 characters: a node-count
limit does not bound full-card volume. A compact graph representation with its
own budget remained a candidate for the next change.

[Aggregate metrics and skill hashes](../evals/results/skill-operations-2026-10-22.json)
contain no private questions, cards or database snapshot. The full report and raw
traces remain local outside the repository. This is a private evaluation of a
known sample, not a publicly reproducible benchmark: one model, two repeats and
one new task establish neither general quality nor statistical significance.

Accounting includes complete stdout/stderr before possible outer-shell truncation,
repeated reads and denied file access outside the fixed allowlist. Full model
input with replayed history, reasoning, caching, billing and latency were not
measured. Compact JSON's effect is excluded. Quality and savings are checked
for the combined instruction change, not merely a smaller entry file.

After this change, the full suite had 587 tests, 586 passed and one Linux-only
skip on macOS in one run with permitted loopback. Recall acceptance 7/7, skill
validation and local links passed. Review found a missing common entity/edge
update contract; it was added to core before freezing the candidate. Original
first-experiment results were not replaced. The installed skill does not update
itself: after accepting the change, copy the whole directory with new references.
