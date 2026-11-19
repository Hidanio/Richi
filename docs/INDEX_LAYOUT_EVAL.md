# Derived search index layout comparison

English | [Русский](INDEX_LAYOUT_EVAL.ru.md)

The v2 layout reduced added SQLite allocation from **10,387,456 to 4,857,856
bytes (53.2%)** on the public synthetic 10,000-entry/1,000-entity fixture.
Canonical knowledge rows did not change. All 38 entry/entity candidate-set
comparisons and all 14 ordinary recall responses matched; the latter also had
identical serialized JSON. This is an index-layout experiment, separate from
the installed-package and agent-workflow evaluations.

The [machine-readable report](../evals/results/index-layout-2026-10-25.json)
records module hashes, fixture hash, per-object `dbstat` allocation, query
medians, write samples and untimed ordinary-response hashes. The old module is
from `84e1b6c3684dd17ca7385d511fcd1b408d11a68a`; its bytes equal the initially
measured `c130086` module, independently checked by SHA-256. Both index modules
ran inside the same source runtime to isolate the index implementation. These
are not two installed-package measurements.

## Where the bytes went

The old `WITHOUT ROWID` primary key contained four text values: family, field,
key and native ID. Its secondary index implicitly repeated the remaining
primary-key columns, duplicating almost the whole row. V2 uses small field
codes and a stable integer record map. The map owns its integer identities;
it does not depend on authoritative tables' implicit rowids. Qualified IDs
are resolved by considering both the full query and one stripped family
prefix, preserving native/qualified ambiguity without storing duplicate keys.

| SQLite object | V1 allocated bytes | V2 allocated bytes |
|---|---:|---:|
| Lookup keys | 5,029,888 | 2,011,136 |
| Lookup by record | 4,857,856 | 1,748,992 |
| Record map | 0 | 282,624 |
| Record map identity index | 0 | 315,392 |
| Three state/kind/work-state indexes | 491,520 | 491,520 |
| Dirty records | 4,096 | 4,096 |
| Additional schema allocation | 4,096 | 4,096 |
| **Total added allocation** | **10,387,456** | **4,857,856** |

The secondary lookup index is constructed after initial population, reducing
fragmentation and per-insert maintenance. Lookup rows fell from 98,894 to
87,894; stored key text fell from 1,104,860 to 862,883 bytes. Neither number
measures full physical storage on its own. Sources and history remain excluded.

A separate dictionary/postings prototype saved only another 139,264 bytes
over the simple mapped prototype (about 3%), while introducing another join
and orphan-key cleanup. That design was not adopted. The final mapped layout
also benefited from bulk secondary-index construction, so prototype sizes
are not final implementation results.

## Timing observations and limits

| Measurement | V1 | V2 |
|---|---:|---:|
| One in-process build | 463.17 ms | 281.83 ms |
| Median of the 36 recorded candidate-lookup p50s | 0.0512 ms | 0.0556 ms |
| Entry metadata update p50, 20 writes | 0.2596 ms | 0.2456 ms |
| Entry summary update p50, 20 writes | 0.2499 ms | 0.2341 ms |

The record-map join adds a small lookup cost: broad entity cases increased
2–5%, at most 0.019 ms in this sample. The layout should therefore be assessed
using complete retrieval latency as well as disk usage. The table is not a
claim of faster lookup or stable production tail latency.

Each lookup alternated arms for 40 repeats. The 19 queries included
`hiddenprobe` twice. Both entry and entity lookups ran each time, giving 38
comparisons. The report stores p50s by family and query text, so it retains
36 distinct p50s per arm. Build ran once per arm, v1 then v2. Writes
ran 20 times per kind, v1 then v2; their individual samples are retained.
Caches were not flushed, and no confidence interval or concurrent-load
control was applied. Build includes derived status calculation but excludes
process startup; writes use normal entry APIs and include history updates.
The measured entry text edits change summaries, which are indexed by FTS but
are not routing keys. Entity text-write costs require separate measurement.
Bytes describe fresh builds on equivalent compacted synthetic copies; they
are allocated pages, not peak WAL or automatically reclaimed disk space.

Metadata/evidence-only changes no longer dirty or rebuild routing keys.
Previously dirty records still refresh, and oversized records stay on the
scan path until a relevant field changes or an explicit rebuild retries.
Tests cover both cases, rollback, old SQL writers, ID ambiguity, source-rowid
changes, missing indexes, and reserved-name collisions.

## Compatibility and reproduction

`search-index build` explicitly upgrades derived version 1 to 2. Reads never
upgrade it. Older version-1 readers see an unsupported derived version and
fall back to their scans; the frozen old reader was exercised on a v2 store.
The experiment used application `user_version` 2 before and after the index
build. The derived index has its own version. An upgrade can leave old pages on SQLite's
freelist; the measured reduction does not imply that an existing database file
immediately shrinks. No automatic VACUUM is introduced.

The public [reproduction harness](../evals/evaluate_index_layout.py) only
creates disposable synthetic databases and never accepts a user database.
Run it with a Python environment containing Richi's test dependencies and SQLite
`dbstat` support. To compare the implementations in the report, use a checkout
at `208432183f0f01b6b514475b5f1534fa09cb29ae`. Its harness and candidate
module match the report's `harness_sha256` and `candidate_source_sha256`.
Fetch the frozen v1 commit into that disposable checkout if it is missing.
The runner loads the candidate and fixture from the checkout passed to `--checkout`.

```sh
git -C /path/to/Richi-2084321 fetch --depth=1 https://github.com/Hidanio/Richi.git \
  84e1b6c3684dd17ca7385d511fcd1b408d11a68a
/path/to/python /path/to/Richi-2084321/evals/evaluate_index_layout.py \
  --checkout /path/to/Richi-2084321 \
  --old-revision 84e1b6c3684dd17ca7385d511fcd1b408d11a68a \
  --output /path/to/new-layout-report.json
```

The published numbers retain the original diagnostic timings. The reproduction
harness also saves raw lookup samples. Running a later checkout measures that
checkout's code and produces a new comparison. The original experiment checked
ordinary-response parity separately without collecting new timings. Identical serialized
responses imply identical response-token counts under a fixed tokenizer;
this change alone claims no reduction in response tokens or agent errors.
