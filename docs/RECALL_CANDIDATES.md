<a id="отбор-кандидатов-recall-проблема-гипотезы-и-проверка"></a>
# Recall candidate selection: problem, hypotheses and validation

English | [Русский](RECALL_CANDIDATES.ru.md)

The protocol was fixed before implementation, on October 16, 2026. The separate
PR is based on `e1265bb82674e0c7cb9f16763ab6f67919d9ad20`. At the start of work,
PR #7 was not yet merged into main: its recorded measurements are evidence,
but its branch is not an implementation dependency.

<a id="наблюдаемая-проблема"></a>
## Observed problem

In the [immutable PR #7 report](https://github.com/Hidanio/Richi/blob/ff966896ec9c28497430ec18b2c39706ee86d5ab/evals/results/memory-growth-2026-10-15.json),
normal recall for `task:DEMO-84` returned 6354 characters with the task first,
but the diagnostic pass examined 105/1005/10005 entries as the corpus grew to
106/1006/10006 records. CLI medians were 280.6/321.1/696.2 ms. These are three
repeats on one host, not a proven scaling law or SLA.

The original `candidates` supplements the FTS shortlist by fully reading entries
in allowed states: it decodes fields and computes a lexical score for each,
although many records are then discarded. The scan also preserves existing
ID/Jira/alias matches that FTS tokenization can miss. The problem under test is
unnecessary loading and processing of full cards.

<a id="гипотезы-до-изменения"></a>
## Hypotheses before the change

1. FTS/metadata preselection that preserves every existing route into the
   candidate set will reduce full loads and lexical-scoring calls. The gain
   must appear in work counters and then be checked by normal CLI timing
   without diagnostic overhead.
2. Preserving Unicode ID/alias semantics may require a linear metadata scan.
   Python normalization cannot be replaced with SQL `lower` or string comparison
   without validation. This stage does not promise to eliminate O(N), SQLite
   physical reads or guarantee index use.
3. Preselection optimization may preserve the existing result: refs and order,
   sources, states, project boost and graph expansion. Full cards are still
   needed for actual selected candidates; FTS-off retains the existing fallback.

The selected target missing from broad `receipt calibration` is a separate
discoverability issue. In PR #7 it occurred in all seven datasets while a unique
query found the same record. This neither proves other matches irrelevant nor
justifies changing ranking, limits, states or the oracle for speed. Archives,
storage schema, knowledge deletion and user memory are outside this stage.

<a id="контракт-выбранной-реализации"></a>
## Contract of the selected implementation

With FTS, candidate selection reads entry metadata through a cursor, then loads
full records by primary key for FTS/ID/Jira/alias candidates. Thus
`entries_scanned` still counts every visited entry in allowed states, while
`entries_scored` counts only full lexical scoring for direct search. Metadata
traversal remains O(N); the optimization reduces full-card processing.
`entries_prefiltered` counts excluded records; scored plus prefiltered equals
scanned. This partitions work rather than promising a constant number of SQL
rows or physical reads. Entities and the full non-FTS fallback are preserved.
For direct API calls, public `recall` opens a read transaction if none exists
and preserves the caller's transaction; CLI already read within a transaction.
This holds one snapshot during concurrent writes, including after Python has
prefetched the cursor's last row, when a cursor alone is insufficient.

Without a full read, the exclusion reason is `routing_prefilter`. Only an excluded
entry selected by `--expect` may receive one separate full probe:
`expected_entry_probes <= 1`. It clarifies the previous lexical guard against
the executed shortlist but neither adds a candidate nor counts as scored;
the record remains prefiltered. The full counter contract is in
[DIAGNOSTICS.md](../skills/project-memory/references/DIAGNOSTICS.md).
Extra diagnostic fields consume budget and can change diagnostic truncation;
normal-output parity is checked separately from diagnostic JSON.

<a id="протокол-сравнения-и-приёмка"></a>
## Comparison protocol and acceptance

1. Preserve original expectations. Use the
   [fixture](https://github.com/Hidanio/Richi/blob/ff966896ec9c28497430ec18b2c39706ee86d5ab/evals/memory_growth_cases.json)
   and [PR #7 runner](https://github.com/Hidanio/Richi/blob/ff966896ec9c28497430ec18b2c39706ee86d5ab/evals/evaluate_memory_growth.py)
   unchanged. Do not rewrite original raw JSON; save rerun baseline and new
   implementation results separately. Measurement scripts may be read from a
   separate PR #7 checkout without including its changes in this implementation PR.
2. Install baseline and candidate normally in separate disposable venvs.
   Compare identical synthetic corpora, queries, flags, `limit=8` and normal
   budget 16000. Retain fixture/runtime hashes, Python/SQLite versions, backend,
   parameters and raw repeats. Do not use or switch live memory or user runtime,
   configuration or chat bindings.
3. Before implementation, fix additional contrastive and negative cases:
   neighboring Jira/ID, empty results, Unicode and ambiguous aliases,
   confirmed/hypothesis/superseded with explicit flags, FTS-off, graph expansion
   and its limits, entries and entities/concepts. PR #7's fixture contains no
   entities, so it alone cannot validate that search branch.
4. Compare normal baseline/candidate output: ordered refs and positions,
   sources, knowledge/work states, applicability, graph links and no-match.
   Preserve budgets and omission flags. Exact get and complete current records
   remain available. Speed does not explain a difference: investigate each.
   Existing misses remain visible; their presence is not a quality success,
   and their disappearance must not mask an unintended semantic change.
5. Run `--expect` diagnostics separately with budget 100000. Distinguish metadata
   inspection, full loading and lexical scoring; counters must describe actual
   work. Explicitly map old and new methods if counter names or meanings change.
   Do not call fewer full loads fewer physical reads without a separate measurement.
6. Check unchanged data, history, graph and states; run
   `python -m unittest discover -s tests -v` and `python evals/evaluate_recall.py`.
   Time CLI separately from seeding, JSON parsing and checks; keep the first
   call separate from repeats. Caches are not cold; p95 of three repeats is the
   maximum. An absolute time threshold does not become a CI gate.

Acceptance requires preserved behavior and a confirmed reduction in unnecessary
full-record processing for the selected scenarios. Publish timing gains only
from measurements, including no improvement and remaining linear scans.
Metadata scans, latency and search quality are distinct outcomes; none replaces
the others.

<a id="результаты-и-воспроизведение"></a>
## Results and reproduction

The rerun [baseline](../evals/results/recall-candidates-before-2026-10-16.json)
and [candidate](../evals/results/recall-candidates-after-2026-10-16.json) were
measured sequentially on October 16, 2026, Moscow time, on macOS arm64,
Python 3.9.6 and SQLite 3.43.2. Below are medians of three normal `current-id`
repeats; the first call is recorded separately. Full lexical scoring before
the change equals old `entries_scanned`; afterward it equals new `entries_scored`.

| Background entries | Full scoring before → after | `entries_scanned` after | Median before → after, ms | Change |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 105 → 3 | 105 | 293.434500 → 286.808500 | −2.3% |
| 1000 | 1005 → 3 | 1005 | 335.029292 → 295.540000 | −11.8% |
| 10000 | 10005 → 3 | 10005 | 598.241916 → 313.238500 | −47.6% |

In these three cases `expected_entry_probes=0`; the metadata scan remains linear.
All recorded normal fields except timing matched in all 77 pairs, including
refs, positions and budgets; corpora and full reads with sources and states also
matched. Both runs completed without harness errors or invariant violations,
and data stayed unchanged. The seven broad-query target misses remained:
`quality_expectations_met=false`. This validates behavior preservation rather
than improving broad-search quality.

Speedups were not uniform: for example, `b100-d0-h0/crowded-lexical` rose from
291.4 to 372.6 ms (+27.9%), and `b1000-d0-h0/shared-jira` from 316.0 to 359.2 ms
(+13.7%). All 11 `b10000-d0-h0` queries reduced their median by 24.7–57.2%, but
three repeats on an uncontrolled host do not separate noise from effect or
establish an SLA. Benefits decline with many exact Jira/alias or shortlist
matches; if all N records match, N separate primary-key full loads are possible.
No speedup is promised for arbitrary alias distributions; entity growth was
not measured here, and the non-FTS fallback retains a full scan.

Implementation validation: 14 new tests; total suite 564 passed and 1 skipped
out of 565. The initial 29 sandbox `PermissionError` failures on `socket.bind`
were rerun with localhost permission: all 29 passed. Acceptance: 7/7, database
unchanged.

Both measurements used unchanged runner/fixture from `ff96689`. The baseline
package was installed from the same commit: its production runtime matches
`e1265bb`. Of seven retained runtime hashes, only `recall.py` changed; JSON
contains full hashes, installed-venv paths, parameters and raw repeats.
The original PR #7 report is preserved. To reproduce from the candidate checkout
when commit `ff96689` is available locally, extract it into a temporary directory;
merging PR #7 into main is unnecessary. Every run writes a new file.

```sh
RECALL_COMPARE_ROOT="$(mktemp -d /tmp/richi-recall-compare.XXXXXX)"
mkdir "$RECALL_COMPARE_ROOT/baseline"
git archive ff966896ec9c28497430ec18b2c39706ee86d5ab | tar -x -C "$RECALL_COMPARE_ROOT/baseline"
python3 -m venv "$RECALL_COMPARE_ROOT/before"
python3 -m venv "$RECALL_COMPARE_ROOT/after"
"$RECALL_COMPARE_ROOT/before/bin/python" -m pip install --upgrade pip
"$RECALL_COMPARE_ROOT/after/bin/python" -m pip install --upgrade pip
"$RECALL_COMPARE_ROOT/before/bin/python" -m pip install "$RECALL_COMPARE_ROOT/baseline"
"$RECALL_COMPARE_ROOT/after/bin/python" -m pip install .
"$RECALL_COMPARE_ROOT/before/bin/python" "$RECALL_COMPARE_ROOT/baseline/evals/evaluate_memory_growth.py" --repeats 3 --output "$RECALL_COMPARE_ROOT/before.json"
"$RECALL_COMPARE_ROOT/after/bin/python" "$RECALL_COMPARE_ROOT/baseline/evals/evaluate_memory_growth.py" --repeats 3 --output "$RECALL_COMPARE_ROOT/after.json"
```
