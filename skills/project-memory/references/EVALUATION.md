<a id="проверка-полезности-поиска"></a>

# Evaluating retrieval usefulness

English | [Русский](EVALUATION.ru.md)

The public distribution uses synthetic regression tests and temporary databases.
Private databases, work questions, old reports and expected IDs from real projects
are not included in the repository. Their results must not be presented as a publicly reproducible
benchmark. The existence of tests does not prove retrieval quality on every new database.

<a id="изменение-алгоритма"></a>

## Changing the algorithm

Save a minimal reproducible example and record expectations before editing.
Compare relevant cases before and after; add a nearby negative example
if false matches are a risk. Do not change an old snapshot, expected answers
or stored baseline to improve a metric. For new knowledge or questions,
create a separate dataset version and a corresponding database snapshot.

Run the repository's synthetic tests from its root:

```sh
python -m unittest discover -s tests -v
python evals/evaluate_recall.py
```

For a separate dataset of your own, record hashes of the database snapshot and question file,
code version and command settings. Use a consistent read-only copy
created with the SQLite backup API, rather than a changing live database. Store
private datasets and reports outside the public checkout. This distribution does not promise
a standalone universal benchmark runner.

Check retrieved refs and their positions, misses, false matches,
sources, states and the limits of the full JSON. For a question about current
state, a relevant historical record may require `needs_recheck`;
that flag does not establish the current release. Response sources must
belong to the corresponding record, and ranking must not change
knowledge state.

`budget.output_chars`, when present, must equal the actual output size
including the final newline. Empty `results` with `no_match=false`
are allowed when a budget omission is explicit: candidates existed but did not fit.
This differs from a negative example that expects no matches.
Do not count a flag on an unrelated card as satisfying a requirement
for the intended record.

Questions selected in advance but seen by the algorithm's author are a fixed
regression check; they are no longer a fully blind holdout.
Public reports must state the protocol and limitations. Top-k hits,
context volume and local command duration do not measure tokens saved,
knowledge truth or the quality of all subsequent agent decisions.

<a id="проверка-разделения-task-карточки"></a>

## Checking task-card splitting

The synthetic example and measurements are described in
[CURRENT_CARDS.md](../../../docs/CURRENT_CARDS.md). The fixture is in
[card_maintenance.json](../../../tests/fixtures/card_maintenance.json),
and [test_card_maintenance.py](../../../tests/test_card_maintenance.py) checks the scenario
through existing CLI/API operations in a temporary database. This checks preservation
of access to knowledge, sources and states; retrieval algorithms and Templates
are unchanged in this stage.

Before transformation, fix the queries, flags, budget and a reasonable `limit`:
task Jira/ID, a unique failure reason, an investigation detail, an active
constraint, a historical measurement and an unverified alternative. Map
significant facts, conditions, numbers and unknowns to new current records and
sources. After transfer, useful detail must be present in full current
content; a parent snapshot in history alone is insufficient for ordinary retrieval.

After transfer, separately check exact `entry get task:KEY` and task presence
in a general Jira query at the predetermined `limit`, without requiring top-1.
Unique-detail queries must reach the intended related records;
check their full content, sources, `project_ids`, Jira, historical
dates, scope, knowledge/work states and relationship directions. A hypothesis remains available
with explicit `--include-hypotheses` and does not become confirmed. Failed experience
preserves a confirmed outcome under its conditions; implemented does not
become merged/released after an editorial change.

Check the previous text in history, rerunning the scenario without new duplicates/revisions,
a CAS conflict when the parent is edited concurrently, and continuation using known IDs
after a failure before shortening the parent. These checks do not make a sequence
of commands atomic. Live memory is not used for fixtures or tests.

For `entry read`, `recall` and `brief`, check existing excerpt,
truncated, omissions and `read_more` indicators. Do not introduce an expectation that “every caveat is always
in the excerpt”: this stage does not guarantee that. Show a case where
a condition requires full `get`, and check meaning in the complete record. Compare
the actual main-summary length and the volume of necessary reads in the selected scenario;
this does not measure time, tokens or LLM quality on real memory.

For `long_summary`, separately check Unicode-length boundaries, the new threshold
and default, severity by state, strict scope, unchanged data/history/
FTS/graph on schemas 1 and 2, absence of source/Git/network I/O and actual
JSON size under a tight budget. An omitted finding is counted, while a displayed finding
retains mandatory details and the full ID. Incomplete scanning is not reported
as full coverage, and candidates do not change integrity health. The contract is in
[DIAGNOSTICS.md](DIAGNOSTICS.md).

<a id="измерение-роста-памяти"></a>

## Measuring memory growth

The synthetic runner measures existing behavior separately from acceptance
evaluation. The matrix and parameters are in [evals/README.md](../../../evals/README.md),
and run results are in [MEMORY_GROWTH.md](../../../docs/MEMORY_GROWTH.md).

```sh
python evals/evaluate_memory_growth.py --quick --repeats 1
python evals/evaluate_memory_growth.py --repeats 3 --output /tmp/richi-memory-growth-report.json
```

Queries and expected useful refs are fixed in the fixture before a run; preserve
actual misses for each query without adjusting the oracle to the output. `--quick`
does not replace the full matrix; each report needs a new `--output` path.
Missing a broad query's target does not prove incorrect ranking of other
matches. The fixture contains no entities; their growth is not measured here.

Ordinary CLI output determines found refs, positions, omissions, volume and time.
A separate `recall --expect` with a large budget explains the target's outcome;
diagnostic JSON consumes budget and does not replace ordinary results.
A successful harness execution does not imply fully useful output: `quality_misses`
are stored separately; `--fail-on-miss` includes them in a nonzero exit code.

CLI time includes startup and stdout, excluding JSON parsing and corpus generation.
The first call is measured separately from N repeats and is not considered cold;
p95 with three repeats equals the maximum and does not reliably estimate the latency tail.
Time is not an absolute CI gate.
For punisher, it includes the global integrity check. Show repeats
and variation; these measurements do not prove token savings or LLM quality.

Scenarios use disposable stores and separate configuration; test
processes do not inherit `RICHI_*`/`CODEX_THREAD_ID`. Production memory is not read,
and user runtimes and workspaces are not switched. Test isolation does not
authorize bypassing chat binding for live memory. Archiving/cleanup is not implemented.

<a id="пропуски-в-повседневной-работе"></a>

## Misses in everyday work

Record a gap selectively when it caused repeated investigation or may
change a conclusion. Distinguish missing knowledge, an intentional state/scope filter,
a budget omission, term ambiguity, an unavailable source and a retrieval defect
for an existing relevant record.

In an appropriate `investigation` with the `retrieval-gap` tag, save a sanitized
query, project, flags, budget, observed result, expected ref
and evidence that relevant content, scope and state already existed in
the database at search time. Use history or a snapshot when needed.
State the correction or remaining uncertainty. Do not create a record
for every query or count an ordinary successful read as a system improvement.

A confirmed missing synonym may be added to the correct scoped concept
and checked by rerunning the query. This does not require a new benchmark for every
data edit. Expansion of indexing and automation should be justified
by recurring needs and observed results.
