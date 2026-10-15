# Public synthetic evaluations

## Recall acceptance cases

Run from the repository after installing Richi (or setting `PYTHONPATH=src`):

```sh
python evals/evaluate_recall.py
python evals/evaluate_recall.py --output /tmp/richi-recall-report.json
```

Each run creates and deletes its own temporary SQLite database. Every CLI invocation
passes `--db` and an empty temporary `--config` explicitly, with `RICHI_*`
environment settings cleared. User memory, configured repositories, external services,
and private evaluation snapshots are never consulted. `example.test` references
are fictional evidence and are not fetched.

The fixed synthetic cases exercise exact task identifiers, Unicode aliases,
explicit cross-project links, historical exclusions, concept ambiguity and scope,
unknown queries, and the distinction between implementation and release. The
runner also checks source provenance, knowledge/work states, serialized output
budgets, and that recall leaves the seeded database unchanged. It exits nonzero
when a case fails and refuses to overwrite reports.

`recall_cases.json` contains all seeds and expectations. These are acceptance
checks, not a blind benchmark or a claim about production recall accuracy.
Historical private datasets remain outside this repository; this fixture does
not replace them or reproduce their results.

## Memory growth measurements

`evaluate_memory_growth.py` measures the effect of growing a synthetic corpus
through normal Richi CLI reads. Its fixture, `memory_growth_cases.json`, fixes
the corpus parameters, queries and expected useful records before measurement.
It runs separately from `evaluate_recall.py`; a growth measurement does not
replace the existing acceptance cases.

Run it with the Python interpreter of a disposable virtual environment where
the current checkout has been installed normally. Its subprocesses use `-I`
and clear inherited Python settings, so `PYTHONPATH=src` is not sufficient.

```sh
python evals/evaluate_memory_growth.py --quick --repeats 1
python evals/evaluate_memory_growth.py --repeats 3 --output /tmp/richi-memory-growth-report.json
```

Use a new `--output` path for each report: an existing file is never overwritten.
`--repeats` accepts 1–20 and defaults to 3: each normal query has one separately
reported first call, followed by N timed repeats. `--timeout` accepts a positive
finite value up to 600 seconds and defaults to 60. `--cases PATH` selects a fixture.
Use the full run to compare these independent axes:

| Axis | Full-run values | Question |
|---|---|---|
| Background current entries | 100, 1000, 10000 | How does additional current knowledge affect retrieval and diagnostic coverage? |
| Additional children with the same Jira key | 0, 10, 100 | Do competing details displace the task or its useful evidence within a fixed result limit? |
| Additional task history revisions | 0, 10, 100 | How does audit history grow when the final current content stays fixed? |

These are separate experiments, not a Cartesian product. The shared full-run
baseline is background 100 / children 0 / revisions 0, giving seven distinct
datasets. `--quick` uses baseline 8 / 0 / 0 and values background 8/24,
children 0/3/12, revisions 0/2/5: six small datasets for a smoke run, not a
measurement of the full matrix. Within each axis, keep the other dimensions
fixed and compare the same queries, limits, budgets and expectations.
With the supplied fixture, total current entries are six focal records plus
the background and additional children: the largest background dataset has
10006 entries, not 10000. The fixture has `entity: []`; entity growth is not
measured. Counts of current records and audit revisions answer different
questions; a larger SQLite file alone cannot identify the cause of a retrieval
change or attribute physical storage to one table.

Measure normal CLI output and latency at `--max-chars 16000` separately from
a diagnostic pass using `recall --expect` with `--max-chars 100000`.
The normal pass determines returned refs,
target positions, omissions and serialized output size. Diagnostic metadata
explains whether a target was filtered, not selected, or omitted by the budget;
it must not replace the normal pass's actual results or latency. Keep quality
misses visible in the report instead of changing the expected refs to fit the
observed output. In particular, a successful process exit is not a statement
that every useful record was retrieved. The broad `crowded-lexical` query has
many legitimate lexical matches: missing its designated target measures bounded
discoverability, not proof that ranking or the other returned records are wrong.

By default the runner exits 0 when its harness and invariant checks pass,
while reporting `quality_misses` separately as a count of dataset/case pairs
with unmet expectations, not a count of missing records. It exits nonzero for
execution or invariant failures. Add `--fail-on-miss` to make measured quality misses also
produce a nonzero exit. This option does not add a timing threshold or change
the expectations.

Timing covers process startup, CLI work and stdout capture. JSON parsing,
validation, corpus generation and seeding are outside that timer. The report
keeps the first call separately and individual repeat observations, their median
and nearest-rank p95. With the default three repeats, p95 is their maximum and
does not estimate a reliable latency tail. Preserve the repeat count;
do not call the first invocation a cold-cache measurement. A fresh process does
not clear filesystem or SQLite caches. Latency is descriptive and is not an
absolute CI pass/fail threshold. Punisher timing includes its database-wide
integrity check even when maintenance findings have strict project scope.

For reproducibility, the report records fixture and generated-corpus hashes,
installed module hashes, Python/platform/SQLite versions, the actual diagnostic
counters and record/history/current-summary storage statistics. It also checks
full target records, source provenance, states, output budgets and unchanged
database/WAL byte digests (SHM is transient coordination state). These checks
distinguish a valid measurement from a useful record being present in a bounded result.

Each scenario uses disposable synthetic storage with an explicit temporary
configuration and home, without inherited `RICHI_*`, `PYTHON*` settings or
`CODEX_THREAD_ID`. This isolation is confined to the test subprocesses; it does not switch the user's
runtime, chat binding or workspace. Production memory, private corpora and
referenced source contents are not read; fictional sources are never fetched.
The runner measures existing behavior; it neither implements archival/cleanup
nor changes production retrieval.

See [MEMORY_GROWTH.md](../docs/MEMORY_GROWTH.md) for a recorded full run, its
environment, measurements and limitations. Synthetic misses and local latency
do not establish production accuracy, saved tokens or agent decision quality.
