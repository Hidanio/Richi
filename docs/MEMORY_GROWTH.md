<a id="рост-инженерной-памяти-синтетические-замеры"></a>
# Engineering memory growth: synthetic measurements

English | [Русский](MEMORY_GROWTH.ru.md)

This measures Richi after PR #6, baseline
`e1265bb82674e0c7cb9f16763ab6f67919d9ad20`. This stage does not change
production retrieval, the schema, archives or user memory.

<a id="протокол"></a>
## Protocol

The [runner](../evals/evaluate_memory_growth.py) uses a normally installed package,
separate temporary home/config/SQLite storage and a fixed
[fixture](../evals/memory_growth_cases.json). The fixture specifies 11 queries
and useful target records in advance. Generation and seeding are outside recall
timing. The corpus contains six focal records: a current task, a rejected
approach, an investigation with a historical measurement, a hypothesis,
knowledge from another project and a control record for broad topical search.

Three axes vary independently: 100/1000/10000 background entries;
0/10/100 detail records for one Jira task; 0/10/100 additional revisions.
The common baseline is 100/0/0, giving seven datasets. History scenarios end
with identical current content. All knowledge is synthetic; `example.invalid`
references are never opened.

Normal recall uses `limit=8` and a 16000-character budget. A separate diagnostic
pass with `--expect` and budget 100000 shows the records actually examined and
the target's selection stage. Its results and timing do not replace the normal
pass: the extra diagnostics consume budget themselves.

The first call is measured separately, followed by three repeats. CLI timing
includes process startup and stdout capture; JSON parsing and checks are outside
the timer. Caches are not cleared. With three repeats, p95 is the maximum and
does not estimate a stable latency tail. Each dataset also checks full target
`get` responses, sources, states, budgets and byte checksums of the SQLite main
file and every nonempty WAL before and after reads. A missing WAL and a zero-byte
file are equivalent; auxiliary SHM is excluded. The statistics connection is
explicitly closed before subsequent CLI reads and the final comparison.

<a id="результаты"></a>
## Results

The [unchanged run JSON](../evals/results/memory-growth-2026-10-15.json) retains
fixture/generator/runtime hashes, raw repeat timings, target positions and
diagnostics. Run: October 15, 2026, 20:50:24–20:53:48 UTC (204.2 seconds);
macOS 15.1 arm64, Python 3.9.6, SQLite 3.43.2, FTS5, normal Richi 0.1.0 installation
in a disposable venv. Parallel test runs had finished; other host load was
not controlled.

All seven datasets and 77 dataset/query pairs completed. There were no harness
errors or invariant violations; data checksum checks passed. **Seven expected
target misses** were recorded: one `crowded-lexical` miss per dataset. The other
ten queries met expectations at every size, including the unique control for
the same record. This is not 100% search recall.

<a id="рост-числа-текущих-записей"></a>
### Growth in current records

`current-id` is normal recall for `task:DEMO-84`, not a direct `entry get`.
The task ranked first in all three datasets and the response stayed at 6354
characters. Examined entries were counted by a separate diagnostic call;
the single hypothesis is excluded by default.

| Background entries | Total entries | Examined | CLI median, ms | p95 of 3, ms | SQLite, KiB |
|---:|---:|---:|---:|---:|---:|
| 100 | 106 | 105 | 280.6 | 427.9 | 364 |
| 1000 | 1006 | 1005 | 321.1 | 504.6 | 2112 |
| 10000 | 10006 | 10005 | 696.2 | 831.8 | 19196 |

Bounded output does not imply bounded search work. Even an ID query visits all
current entries in allowed states; the FTS shortlist does not eliminate this
scan. The local median rose about 2.5-fold, but three repeats do not establish
a scaling law or operational SLA. The full scan itself is confirmed by counters
and the [candidate implementation](../src/richi/recall.py).

For broad `receipt calibration` with 100 background records, the target was a
candidate but lost its place in the result (`rank_or_limit`). At 1000 and 10000
it was already absent from the 160-record FTS shortlist (`fts_not_shortlisted`).
`calibration-target-evidence` ranked the same record first in all seven datasets;
full `get` also succeeded. This distinguishes losing a result slot, losing a
candidate slot and actual record unavailability.

<a id="подробности-одной-задачи"></a>
### Details of one task

| Additional children | Total entries / edges | `current-id` median, ms | Shared Jira response characters | Jira results omitted | Graph truncation |
|---:|---:|---:|---:|---:|---|
| 0 | 106 / 4 | 280.6 | 6731 | 0 | no |
| 10 | 116 / 14 | 282.5 | 15910 | 5 | no |
| 100 | 206 / 104 | 289.4 | 15446 | 95 | yes |

The stable task retained first place in the shared Jira query. With 10 and 100
additional children, however, generated investigations occupied the other seven
slots: the original rejection reason and original investigation were absent.
Their individual queries still found the required records. The fixed shared-Jira
oracle requires the task, not every detail, so this effect is described separately
rather than hidden by the overall scenario pass rate. `omitted_results` includes
the result-count limit, not only insufficient characters.

At 104 edges, the graph traversal limit was reached. A `part_of` edge does not
guarantee automatically reading every linked record. Exact refs in the current
task summary and targeted detail reads remain necessary.

<a id="история-при-неизменном-текущем-содержимом"></a>
### History with unchanged current content

Every row has 106 current entries, 24304 current-summary characters, the same
`current_corpus_sha256`, and identical returned-ref sets for each query.
The added revisions belong to one task.

| Additional revisions | entry_history rows | Total before/after JSON, bytes | SQLite, KiB | `current-id` median, ms |
|---:|---:|---:|---:|---:|
| 0 | 106 | 68423 | 364 | 280.6 |
| 10 | 116 | 86243 | 388 | 283.8 |
| 100 | 206 | 246803 | 572 | 277.2 |

History increased the file size, but not the number of current records examined
or the results in this scenario. A small experiment with 100 revisions does not
prove unlimited history scalability or model thousands of edits to a
28,000-character summary.

<a id="покрытие-диагностики"></a>
### Diagnostic coverage

In the dataset with 10000 background entries, 7005 records belonged to project
Relay. Punisher examined 5000 and explicitly returned `coverage.complete=false`;
global integrity health remained `ok`. The output occupied only 2298 of 16000
characters, with `omitted_findings=0`. Increasing the response budget therefore
does not widen examination coverage: an unexamined tail is not verified merely
because no findings were returned.

<a id="что-следует-делать-дальше"></a>
## Next steps

The first justified candidate for a separate PR is candidate-selection
optimization, especially ID/alias lookup without a full scan of current entries,
with behavior parity checked against these and previous scenarios. Controlled
task-detail expansion and diagnostics beyond the scan cap should be designed
separately. Simply raising output limits does not remove full scans or candidate
competition.

This run does not justify moving knowledge to separate physical storage.
It shows the need to distinguish current records, history and the context of a
particular read. A possible logical archive needs its own discoverability and
old-ID access contract; rejection reasons or completed experiments must not be
automatically hidden merely because of age or work state.

<a id="воспроизведение"></a>
## Reproduction

From the checkout root, install the package in a disposable venv:

```sh
GROWTH_VENV="$(mktemp -d /tmp/richi-growth-venv.XXXXXX)"
python3 -m venv "$GROWTH_VENV"
"$GROWTH_VENV/bin/python" -m pip install --upgrade pip
"$GROWTH_VENV/bin/python" -m pip install .
"$GROWTH_VENV/bin/python" evals/evaluate_memory_growth.py --quick --repeats 1
"$GROWTH_VENV/bin/python" evals/evaluate_memory_growth.py --repeats 3 --output /tmp/richi-memory-growth-new.json
```

`--output` requires a new path. Exit code 0 means measurement and invariants
passed; `quality_misses` separately counts dataset/query pairs with unmet
expectations. `--fail-on-miss` makes such misses cause a nonzero exit. Run the
normal acceptance evaluation separately:

```sh
"$GROWTH_VENV/bin/python" -m unittest discover -s tests -v
"$GROWTH_VENV/bin/python" evals/evaluate_recall.py
```

<a id="пределы-выводов"></a>
## Limits of the conclusions

Broad `receipt calibration` intentionally has many legitimate lexical matches.
A predetermined target missing from eight results demonstrates bounded
discoverability, not objective irrelevance of the other answers. The
`calibration-target-evidence` control checks the same record's availability by
a unique detail. Sources and full `get` are checked independently of inclusion
in bounded output.

The dataset grows entries and task links, not entities. It does not test long
multi-year corpora, arbitrary paraphrases, production query distributions or
LLM decision quality. Local latency depends on the host and background load.
SQLite size includes indexes, history and free pages; total history JSON is not
a measurement of one table's physical size. Shortening summaries and moving
details does not remove edit history.

Automatic archiving and separate physical storage do not follow from record
count growth alone. That decision needs measured search losses or operational
limits and an archive availability contract.
