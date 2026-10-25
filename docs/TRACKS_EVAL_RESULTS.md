# Track context measurements — 2026-10-25

These are synthetic data/CLI measurements, not model-quality trials. The executed baseline uses existing CLI operations and a local Python postprocessor; no engineering-answer improvement is claimed.

Both arms expose all required research facts at the measured 32k budget. The native result combines typed validation, explicit scope, provenance and an event cursor, with the following measured costs and benefits:

- At 10,000 entries, context p50 is 210 ms for the baseline and 294 ms natively; warm visible tokens are 2,739 and 6,871. Standalone context is also slower natively, 289 ms versus 154 ms.
- Native context uses one CLI dispatch; the baseline composes five dispatches inside one fresh process. Changes use one versus 60 dispatches, with p50 265 ms versus 786 ms. Both read arms use one fresh process per observation.
- Result writes take 286 ms natively versus 540 ms baseline p50. The native single request is atomic; the baseline's two sequential writes have a weaker contract.
- Native strict-query core p50 is 0.50 ms versus 3.83 ms at 10,000 entries. CLI startup dominates the end-to-end strict-query timing, 265 ms versus 261 ms.
- Native pre-write storage is 20,348,928 bytes versus 18,452,480 bytes at 10,000 entries. Smaller-corpus overhead is proportionally larger. The fixture's duplicated summary/typed representation is described below.

Baseline runtime: `208432183f0f01b6b514475b5f1534fa09cb29ae`. Candidate runtime: `f1ade897da23bb1f1514677387e3ac44379385d9`.

Python 3.9.6; SQLite 3.43.2; tokenizer `o200k_base` via `tiktoken` 0.14.0. Three frozen corpus sizes, 30 fresh processes per arm/scenario; p95 uses nearest rank. OS caches are shared.

[Protocol and reproduction commands](TRACKS_EVAL.md). [Machine results with all samples](../evals/results/tracks-2026-10-25.json).

The published JSON normalizes only the OS-specific prefix of three disposable migration database paths to `<synthetic-temp>`. Measurements, hashes, samples and token counts are unchanged.

## Validation of the measured runtime

Before measurements, the installed candidate passed 747 local tests in 619.721 seconds, with one Linux-only systemd test skipped on macOS. The required recall evaluation passed 7/7 and preserved its database digest. Skill validation and 104 local reference links passed. The [GitHub Actions runtime check](https://github.com/Hidanio/Richi/actions/runs/40217306762) passed all four Ubuntu/macOS × Python 3.9/3.12 jobs for `f1ade89`.

The benchmark then verified installed Python modules, launcher files and SQL/HTML resources against the declared source commits before measuring each scale. No heavy test suite ran concurrently with the official measurements. All parity, migration and sufficient-evidence assertions passed. Raw observations include the slower samples; none were discarded.

## Evidence and scope

Both stores retain the same authored summary facts, sources and explicit graph links. Native structured fields add validation. Full context is checked against the frozen structured research facts; string-list sets are compared after the API’s normalization. Ordered observations and direction changes retain order. The track remains 19 tasks plus shared evidence while unrelated same-project entries grow. This is not a benchmark of a 10,000-member track.

| Entries | Baseline missing/mismatched full facts | Native missing/mismatched full facts | 16k baseline missing details | 16k native missing details | Migration preserves legacy rows |
| ---: | ---: | ---: | ---: | ---: | --- |
| 40 | 0 | 0 | 0 | 6 | True |
| 1,000 | 0 | 0 | 0 | 6 | True |
| 10,000 | 0 | 0 | 0 | 6 | True |

The measured common budget is 32,000 serialized JSON characters. The 16,000-character rows are explicit stress diagnostics. Continuations and omitted-card fetches are included in sufficient-evidence workflows; a fast incomplete first response is not counted as a complete answer. Exact metadata pages and whole-scope aggregate rows match in every scale. Changes expose the title update in both arms, but baseline history snapshots and native current-state notifications have different payload contracts.

## First-response latency

Milliseconds: p50 / p95. Baseline context/history embed the installed CLI entry point in one fresh postprocessor process; metadata operations use the installed launcher directly. This avoids artificially charging one interpreter startup for every local composition step. Native commands use the installed launcher.

| Entries | Scenario | Baseline | Native | Prepared control |
| ---: | --- | ---: | ---: | ---: |
| 40 | context | 196.44 / 326.47 | 299.88 / 451.70 | 61.79 / 145.72 |
| 40 | standalone | 164.20 / 287.28 | 293.36 / 453.68 | 58.63 / 70.63 |
| 40 | strict | 256.83 / 411.26 | 266.47 / 406.89 | — |
| 40 | broad | 255.86 / 391.01 | 266.80 / 398.14 | — |
| 40 | aggregates | 264.76 / 394.02 | 272.25 / 427.12 | — |
| 40 | changes | 798.42 / 920.57 | 272.79 / 422.57 | — |
| 1,000 | context | 196.66 / 331.99 | 298.55 / 479.98 | 58.91 / 71.70 |
| 1,000 | standalone | 156.38 / 263.58 | 287.59 / 430.79 | 62.06 / 155.52 |
| 1,000 | strict | 264.28 / 460.07 | 270.15 / 430.62 | — |
| 1,000 | broad | 267.19 / 441.05 | 275.04 / 423.56 | — |
| 1,000 | aggregates | 275.68 / 468.92 | 297.36 / 553.84 | — |
| 1,000 | changes | 798.93 / 1283.54 | 278.96 / 455.88 | — |
| 10,000 | context | 210.29 / 344.67 | 293.62 / 490.54 | 58.87 / 103.37 |
| 10,000 | standalone | 153.76 / 265.36 | 289.10 / 432.28 | 58.38 / 123.38 |
| 10,000 | strict | 260.66 / 416.27 | 265.12 / 521.34 | — |
| 10,000 | broad | 280.73 / 421.04 | 277.15 / 399.48 | — |
| 10,000 | aggregates | 264.64 / 419.00 | 262.39 / 416.16 | — |
| 10,000 | changes | 786.25 / 889.10 | 265.40 / 415.69 | — |

The prepared control is the baseline’s actually collected compact output, not a hand-picked answer. It is invalidated by relevant edits. There is no weighted headline saving.

| Entries | Scenario | Preparation ms | Prepared cold / warm tokens | Preparation visible tokens |
| ---: | --- | ---: | ---: | ---: |
| 40 | context | 192.99 | 4,864 / 2,657 | 7,211 |
| 40 | standalone | 168.77 | 2,421 / 214 | 4,768 |
| 1,000 | context | 206.55 | 4,864 / 2,657 | 7,211 |
| 1,000 | standalone | 148.00 | 2,421 / 214 | 4,768 |
| 10,000 | context | 327.60 | 4,864 / 2,657 | 7,211 |
| 10,000 | standalone | 147.89 | 2,421 / 214 | 4,768 |

## Time to sufficient evidence

| Entries | Scenario | Baseline p50/p95 ms | Native p50/p95 ms | Native processes | Native response chars |
| ---: | --- | ---: | ---: | ---: | ---: |
| 40 | context | 196.44 / 326.47 | 299.88 / 451.70 | 1 | 26,428 |
| 40 | standalone | 164.20 / 287.28 | 293.36 / 453.68 | 1 | 1,990 |
| 1,000 | context | 196.66 / 331.99 | 298.55 / 479.98 | 1 | 26,428 |
| 1,000 | standalone | 156.38 / 263.58 | 287.59 / 430.79 | 1 | 1,990 |
| 10,000 | context | 210.29 / 344.67 | 293.62 / 490.54 | 1 | 26,428 |
| 10,000 | standalone | 153.76 / 265.36 | 289.10 / 432.28 | 1 | 1,990 |

## Query core

Milliseconds: p50 / p95. Open connection, parser/import warm-up excluded; these are separate from CLI startup.

| Entries | Scenario | Baseline | Native |
| ---: | --- | ---: | ---: |
| 40 | context | 2.64 / 5.42 | 4.59 / 9.24 |
| 40 | standalone | 0.11 / 0.16 | 0.28 / 0.46 |
| 40 | strict | 0.45 / 0.52 | 0.51 / 0.56 |
| 40 | broad | 0.45 / 0.48 | 0.47 / 0.51 |
| 40 | aggregates | 0.11 / 0.17 | 0.23 / 0.47 |
| 40 | changes | 5.04 / 86.44 | 0.08 / 0.12 |
| 1,000 | context | 3.40 / 3.60 | 4.71 / 12.82 |
| 1,000 | standalone | 0.10 / 0.13 | 0.27 / 0.31 |
| 1,000 | strict | 0.72 / 0.79 | 0.53 / 0.58 |
| 1,000 | broad | 0.45 / 0.52 | 0.48 / 0.56 |
| 1,000 | aggregates | 0.35 / 0.47 | 0.13 / 0.16 |
| 1,000 | changes | 4.90 / 5.27 | 0.08 / 0.09 |
| 10,000 | context | 12.02 / 12.61 | 4.40 / 5.00 |
| 10,000 | standalone | 0.10 / 0.13 | 0.28 / 0.33 |
| 10,000 | strict | 3.83 / 6.03 | 0.50 / 0.54 |
| 10,000 | broad | 0.50 / 5.08 | 0.45 / 0.49 |
| 10,000 | aggregates | 2.82 / 3.30 | 0.14 / 0.16 |
| 10,000 | changes | 15.29 / 19.16 | 0.08 / 0.10 |

## Visible tokens

Cold includes the full chosen instruction/catalog prefix and, where used, the generated postprocessor script. Warm includes task, arguments and all actual sufficient-evidence workflow responses. These are independent-text replay estimates, not billing or hidden reasoning tokens. Do not sum cold prefixes repeatedly when modeling one session.

| Entries | Scenario | Baseline cold / warm | Native cold / warm |
| ---: | --- | ---: | ---: |
| 40 | context | 7,281 / 2,738 | 12,810 / 6,871 |
| 40 | standalone | 4,839 / 296 | 6,470 / 531 |
| 40 | strict | 2,896 / 689 | 6,668 / 729 |
| 40 | broad | 2,909 / 702 | 6,641 / 702 |
| 40 | aggregates | 2,391 / 184 | 6,163 / 224 |
| 40 | changes | 5,099 / 556 | 6,334 / 395 |
| 40 | write_result | 2,648 / 441 | 6,543 / 604 |
| 1,000 | context | 7,282 / 2,739 | 12,810 / 6,871 |
| 1,000 | standalone | 4,840 / 297 | 6,470 / 531 |
| 1,000 | strict | 2,896 / 689 | 6,668 / 729 |
| 1,000 | broad | 2,964 / 757 | 6,696 / 757 |
| 1,000 | aggregates | 2,391 / 184 | 6,163 / 224 |
| 1,000 | changes | 5,103 / 560 | 6,344 / 405 |
| 1,000 | write_result | 2,648 / 441 | 6,543 / 604 |
| 10,000 | context | 7,282 / 2,739 | 12,810 / 6,871 |
| 10,000 | standalone | 4,840 / 297 | 6,470 / 531 |
| 10,000 | strict | 2,896 / 689 | 6,668 / 729 |
| 10,000 | broad | 2,964 / 757 | 6,696 / 757 |
| 10,000 | aggregates | 2,391 / 184 | 6,163 / 224 |
| 10,000 | changes | 5,103 / 560 | 6,348 / 409 |
| 10,000 | write_result | 2,648 / 441 | 6,543 / 604 |

## Storage, migration and writes

Both databases were checkpointed consistently; neither was VACUUMed. Freelist and per-index/page attribution are included in the machine results. Build is fixture insertion plus index maintenance. Migration is a disposable baseline copy and includes canonical legacy-row preservation checks.

| Entries | Baseline DB before / after writes | Native DB before / after writes | Migration ms | Baseline write p50/p95 ms | Native write p50/p95 ms |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 40 | 323,584 / 434,176 | 458,752 / 675,840 | 279.48 | 536.53 / 728.02 | 288.91 / 502.70 |
| 1,000 | 2,076,672 / 2,211,840 | 2,392,064 / 2,621,440 | 313.40 | 576.01 / 687.08 | 287.47 / 438.51 |
| 10,000 | 18,452,480 / 18,583,552 | 20,348,928 / 20,557,824 | 498.48 | 540.19 / 675.54 | 286.31 / 460.05 |

The write series creates 30 distinct results per arm. Native uses one atomic result-and-links request with an idempotency receipt; baseline uses an entry write followed by a batched edge write, with weaker atomicity across the two calls. The native fixture retains the same JSON-in-summary facts and adds a typed representation in the same card, so these storage ratios include that intentional duplication. They are fixture-specific, not a universal schema overhead estimate.

| Entries | Baseline fixture build ms | Native fixture build ms | Baseline search-index build ms / added bytes | Native search-index build ms / added bytes |
| ---: | ---: | ---: | ---: | ---: |
| 40 | 19.49 | 60.38 | 362.53 / 36,864 | 289.04 / 36,864 |
| 1,000 | 239.44 | 315.87 | 310.46 / 200,704 | 501.69 / 200,704 |
| 10,000 | 1217.06 | 2972.82 | 346.62 / 1,556,480 | 449.04 / 1,548,288 |

Build and migration each have one observation per scale; do not interpret them as latency distributions. Search-index construction is measured on independent disposable copies. Native typed/semantic index maintenance is included in fixture build and the result-write series. Actual strict-selection, context-scope and full-count `EXPLAIN QUERY PLAN` output, table sizes, index names and unused pages are saved in the machine report.

The captured strict native plan uses `research_semantic_links`, `entry_research_role` and entry primary-key lookups; the baseline tag convention scans entries and JSON tags. Context counts join selected native IDs through the entry primary-key index. Context construction also contains scans of intermediate sets and the goal-source entity branch. This corpus grows unrelated entries, not goal sources or experiment-link fan-out, so it does not establish constant cost for every graph shape.

## Limits

No authorized model runner was used. Constraint-following, unsupported causal assertions, repeated investigations, rework and engineering solution quality have not been measured in model trials. The frozen runner protocol remains in TRACKS_EVAL.md. All facts and measurements are synthetic; no private stores were migrated or published. Runtime selection, user skill installation, merge and deployment remain separate actions.
