# Indexed routing and composite-filter evaluation

This benchmark compares two **normally installed fixed packages** against the
same disposable synthetic SQLite contents. It does not read a personal workspace,
switch development settings, update installed skills or accept a user database.
The baseline is commit `b8a2a9aa889a7d65d433e985ca3f6a9d4cec4c60`; the candidate is
an independently installed build of this change. Installed Python-module hashes
are recorded, so the unchanged package version alone is not treated as provenance.
The report also records the declared baseline source revision; this label does
not replace checking installed module hashes against that revision.

The experiment asks separate questions:

1. Does indexed routing preserve the complete ordinary unfiltered JSON response?
2. Does it reduce engine and fresh-CLI latency at increasing corpus sizes?
3. Do strict composed predicates apply before the FTS shortlist cap, including
   when eligible records would otherwise rank beyond that cap?
4. Can a scoped workflow return the same required full records with less irrelevant
   text, and what are index build, storage and write costs?

## Corpus and predetermined expectations

The main sizes are 100, 1,000 and 10,000 **total entries**, plus entities at 10% of
each size. Summaries are approximately 500 characters with source references,
conditions and limitations. Background records vary project, tags, aliases and
knowledge state. Existing public acceptance fixtures are included unchanged, with
additional exact-entity, Unicode/case-folded alias, phrase, hypothesis, superseded
and broad-query cases. The 14 cases are fixed in the runner; known no-match and
ambiguous-concept cases are included.

The baseline package seeds each corpus through normal APIs. A SQLite backup copy
becomes the candidate database, preserving timestamps, rowids, sources and history
exactly. Candidate `search-index build` runs explicitly on that copy. No implicit
index build is assumed. Canonical fingerprints include full rows and rowids in
knowledge/history/link/event tables, application schema version and metadata,
except the explicitly derived `richi_search_index_version` metadata key.

For unfiltered queries **every ordinary response field must agree**, including
ordered refs, scores, sources, knowledge/work states, recheck flags, ambiguity,
links, selection metadata and budgets. Diagnostic traces are measured separately
and may differ because their counters describe changed execution. A faster query
with a different ordinary response does not pass parity.

## Historical misses stay visible

The runner also loads the exact frozen PR #7 fixture from commit
`ff966896ec9c28497430ec18b2c39706ee86d5ab`, checks its original SHA-256, and reproduces
all seven corpus configurations: backgrounds 100/1,000/10,000, extra task details
0/10/100, and extra revisions 0/10/100. This gives **77 old query/configuration
pairs per arm**, outside the new scale fixture.

The old crowded lexical question previously missed its designated target in all
seven configurations. These are genuine bounded-discoverability observations:
other matching notes are not labelled irrelevant merely because they rank above
that target. Missing targets remain explicit `quality_misses`, separately for each
arm. They are not erased, renamed successes or silently excluded from averages.
Legacy queries are repeated once for parity, not used for new latency estimates.

The new positive/negative expectations must pass. Historical misses do not alone
make the runner exit nonzero; ordinary-response differences, unexpected new-fixture
misses, failed strict-filter sets or mutation invariants do. Always inspect the
quality counts, not only the process exit code.

## Strict-filter and token comparison

A separate crowded corpus contains 360 eligible/irrelevant-by-filter records,
above the FTS candidate cap. Predetermined target records have weaker lexical
rank and must still be returned under project/kind, nested AND/OR/NOT, tag/date,
and IN/IS NULL filters. State predicates do not silently override the command's
knowledge-state selection. Filter expectations are compared with fixed IDs, not
with an oracle produced by the candidate's parser.

A smaller 24-record corpus tests a realistic overfetch workflow. Both arms use
`search QUERY --project atlas --limit 200`, compact JSON, and full-record output.
The existing strict project scope is therefore used in the baseline too. The
candidate adds a composed kind/state/tag/date predicate; the baseline receives
all project-scoped matches and a fixed client-side oracle selects the required
records. The required **entire records, including sources**, must agree exactly.
This is not a blind agent selecting its own evidence and does not prove every
workflow will use fewer tokens. Full relevant content receives no truncation
credit. The crowded recall comparison is reported separately; a capped baseline
that missed the required targets is not assigned a comparable token-savings win.

Token counts sum independent tool responses using both `o200k_base` and
`cl100k_base`. They exclude prompts, tool-call framing, reasoning, generated
answers, caching and billing. Unfiltered output should be identical, so its token
counts should also be identical; indexing is a speed optimization, not a reason
to claim unfiltered token savings. Character counts include each final newline.
Tokenization requires an already populated cache; no network download is part of
the measurement.

## Timing and cost measurements

For each main query, one untimed read precedes 20 timed in-process calls. Timing
includes retrieval and normal response fitting but excludes Python startup,
argument parsing and the final stdout write. The connection remains open and
queries own read transactions. Sample values, p50 and nearest-rank p95 are retained.
The small sample does not establish stable production tail latency. The engine
arms run in separate blocks; which arm runs first alternates across corpus sizes
and the actual order is recorded. This does not eliminate all temporal noise.

Three main queries also run five fresh CLI processes per arm with alternating arm
order. This captures startup and serialization overhead. Fresh processes share OS
caches; the benchmark does not claim cold filesystem caches. Run final timings
without concurrent test suites or other benchmark loads. Timings are observations,
not flaky CI pass/fail thresholds.

Index costs include complete build CLI wall time, including process startup,
status retrieval and serialization. Main/WAL bytes are measured before build and
after the process closes; this is not a measurement of peak WAL during build.
Separate copied databases measure 20 metadata edits and 20 text edits via normal
APIs, including their history and index writes. Both arms must finish with the
same current semantic payloads; timestamps are excluded only from this post-write
comparison because the calls occur at different times. Write-time WAL sizes and
post-close file sizes are reported separately. These measurements expose the
tradeoff rather than assuming that faster lookup is free.

Engine reads, subsequent fresh CLI reads, and both arms of the filter workflow
must preserve canonical fingerprints and physical main/WAL fingerprints. SHM lock state and filesystem timestamps are not part of physical
identity. Index build may add derived objects but must preserve canonical rows.

## Reproduction

Install baseline and candidate into different temporary virtual environments using
normal package installation. Do not run a benchmark against editable installs.
The runner's source checkout must contain the frozen fixture commit, or provide
its exact file via `--legacy-fixture`.

```sh
TIKTOKEN_CACHE_DIR=/path/to/prepopulated-cache \
  /path/to/driver-python evals/evaluate_indexed_filters.py \
  --baseline-python /path/to/baseline-venv/bin/python \
  --candidate-python /path/to/candidate-venv/bin/python \
  --tokenizer-python /path/to/tokenizer-venv/bin/python \
  --output /path/to/new-report.json
```

For an explicitly labelled smoke run, use `--sizes 100 --engine-repeats 1
--cli-repeats 1 --skip-legacy`. Smoke results are not the full evaluation. Reports
are created exclusively and never overwritten. Public fixtures and reports must
contain synthetic data only.

## Measured results: 2026-10-25

The [complete synthetic report](../evals/results/indexed-filters-2026-10-25.json)
compares current main `b8a2a9aa889a7d65d433e985ca3f6a9d4cec4c60` with the rebased
candidate. Both fixed installations used Python 3.9.6 and SQLite 3.43.2;
tokenization used tiktoken 0.14.0. Every installed baseline Python module matched
that archived revision, and every candidate module hash matched the reviewed
source in every dataset. The final run had no concurrent test suite or benchmark
load. Only this current-main run is reported below.

**All 119 ordinary response pairs agree exactly.** All 42 main query/configuration
pairs and all eight strict-filter expectations pass. Canonical/physical read
fingerprints, canonical rows across index builds, and post-write semantics pass.
The old crowded-lexical target is still missed in **7/77 legacy query/configuration
pairs per arm**; this change preserves that existing limitation. The separate
360-record strict-filter test returns all three predetermined records under each
of four predicates, although unfiltered baseline recall's 160-candidate shortlist
misses them.

Observed warm engine p50, milliseconds (baseline → candidate):

| Entries + entities | Exact Jira | Unicode alias | Broad lexical query |
| --- | ---: | ---: | ---: |
| 100 + 10 | 0.905 → 0.261 | 0.771 → 0.271 | 2.753 → 2.845 |
| 1,000 + 100 | 6.816 → 0.306 | 5.233 → 0.292 | 11.069 → 8.652 |
| 10,000 + 1,000 | 69.075 → 0.284 | 51.959 → 0.286 | 62.931 → 39.648 |

At 10,000 entries, exact Jira routing visits 1 entry and 0 entities instead of
7,998 entries and 1,000 entities. These are application routing visits, not SQLite
page-read counters. Broad queries still visit 160 shortlisted entries and 998
matching entities: the speedup depends on selectivity. The phrase case improves
p50 only from 80.917 → 60.880 ms, while its p95 regresses 86.651 → 123.262 ms.
The small-corpus broad-query p50 also regresses slightly. This run does not
establish improved latency for every query or percentile.

Fresh CLI at 10,000 entries, milliseconds (baseline → candidate):

| Query | p50 | p95 |
| --- | ---: | ---: |
| Exact Jira | 343.7 → 249.3 | 468.9 → 255.0 |
| Unicode alias | 313.3 → 251.3 | 444.2 → 362.0 |
| Broad lexical query | 303.9 → 407.9 | 333.9 → 509.4 |

Startup and process-level noise materially affect these five-sample measurements.
Jira and alias CLI p50 improve, but **broad-query CLI p50 regresses** despite its
faster warm engine. The complete samples remain in the report; no better-looking
rerun replaces them. Large engine ratios must not be presented as equivalent
end-to-end CLI speedups or a universal CLI improvement. At 100 entries CLI p50
is approximately 238–269 ms and moves in both directions across queries.

Unfiltered token counts are identical at every scale: at 10,000 entries the 14
responses total 8,438 tokens per arm with `o200k_base`, or 8,373 with `cl100k_base`.
In the explicitly scoped full-record workflow, baseline search returns 18 records
and filtered search returns the same three required records, including all fields
and sources. Response tokens fall **3,760 → 625 (83.4%)** with `o200k_base` and
**3,773 → 623 (83.5%)** with `cl100k_base`. This is one predetermined scoped
workflow, not a general reduction in model context or billed tokens.

| Entries + entities | Main DB before → after build, bytes | Complete build CLI, ms |
| --- | ---: | ---: |
| 100 + 10 | 438,272 → 569,344 | 250.3 |
| 1,000 + 100 | 2,940,928 → 4,001,792 | 272.3 |
| 10,000 + 1,000 | 27,668,480 → 38,055,936 | 748.5 |

The largest fixture therefore pays **10,387,456 extra bytes (37.5%)** for derived
indexes. On its 20+20 update sample, metadata-write p50 increases 0.188 → 0.295 ms
and text-write p50 0.183 → 0.288 ms; text-write p95 increases 0.303 → 2.097 ms.
The report's active WAL snapshots are not cumulative write volume: automatic
checkpoint timing differs between arms, so a smaller sampled WAL must not be
claimed as reduced write amplification. Indexing trades storage and write cost
for selective lookup speed; it neither shrinks canonical history nor fixes the
old broad-query quality misses.

## Predeclared CLI follow-up: 30 samples per arm

The initial full run's broad-query CLI regression triggered exactly one additional
measurement, declared before execution: only the 10,000-entry + 1,000-entity
corpus, the same unchanged fixed installations, 30 fresh CLI repetitions per arm
for **all three** CLI queries, and 20 warm engine repetitions. Arm order still
alternates. Historical fixtures were deliberately skipped because this is a
partial performance follow-up, not a replacement for the full 119-pair evaluation.
There were no concurrent benchmark or test-suite loads.

The [separate follow-up report](../evals/results/indexed-filters-cli-followup-2026-10-25.json)
preserves every sample. Its 14 ordinary parity pairs, all strict-filter cases and
all mutation/write invariants pass. The original complete report and its numbers
above remain unchanged.

Fresh CLI, milliseconds (baseline → candidate):

| Query | p50 | p95 |
| --- | ---: | ---: |
| Exact Jira | 316.4 → 249.8 | 458.3 → 384.1 |
| Unicode alias | 298.6 → 246.7 | 433.1 → 379.0 |
| Broad lexical query | 305.6 → 297.6 | 433.7 → 440.5 |

The initial broad-query median regression **does not persist**: the follow-up p50
is 2.6% lower, while p95 is still slightly higher. That small median difference
against substantial process-level variation does not establish a dependable
broad-query CLI improvement. Selective queries improve p50 by 21.1% for Jira and
17.4% for the Unicode alias in this follow-up. These are observations on this
fixture and host, not production guarantees. Both runs remain visible; no further
measurement was used to select a more favorable result.
