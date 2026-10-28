<a id="запуск-cli-гипотеза-и-протокол-проверки"></a>
# CLI startup: hypothesis and validation protocol

English | [Русский](CLI_STARTUP.ru.md)

The protocol was fixed on October 16, 2026, before comparing the candidate with
main baseline `3b8d380` after PR #8. The change defers `tempfile` and `subprocess`
imports in three `richi_launcher` modules: `config`, `runtime` and `workspaces`.
The original protocol and comparison results are preserved below. The baseline
installation comes from `39fb95eb406fe8f5024423e222743717a977cd96`: its production
code matches squash merge `3b8d380098b9e809ead8a0cce566c66abf93e10f`.
The measurement tool checks installed-file and source hashes.

<a id="наблюдаемая-проблема-и-гипотеза"></a>
## Observed problem and hypothesis

After recall candidate selection, normal CLI execution still takes about 300 ms.
In the original local profile, medians of five normal invocations were 289–342 ms.
With 10000 background entries, a separate instrumented `recall` call took
22–39 ms inclusive; substantial work remained in imports, source compilation and
argparse construction. Release-hash validation took about 1.3 ms in one profile:
that measurement does not justify removing it.

Hypothesis: ordinary reads need not eagerly import libraries required for
configuration/registry writes or preflight. Moving imports to use sites will
reduce unnecessary startup work while preserving command behavior. In dev,
`tempfile` remains necessary for a fresh pycache prefix; this protection cannot
be removed for speed. Runtime validation, workspace selection, locks, atomic
configuration writes and the recall algorithm remain unchanged.

The profile motivates the hypothesis rather than measuring speedup. cProfile
changes timing; cumulative calls are nested and cannot be added. Its wrapper
preloads its own infrastructure, `json` and `pathlib`; process startup, exec and
shutdown are also not fully covered. Profile times therefore cannot be subtracted
from normal wall time or used to infer an exact startup share. Recall's linear
metadata scan remains separate work.

<a id="парное-сравнение"></a>
## Paired comparison

1. Use the unchanged
   [runner](https://github.com/Hidanio/Richi/blob/ff966896ec9c28497430ec18b2c39706ee86d5ab/evals/evaluate_memory_growth.py)
   and [PR #7 fixture](https://github.com/Hidanio/Richi/blob/ff966896ec9c28497430ec18b2c39706ee86d5ab/evals/memory_growth_cases.json)
   as external sources of synthetic corpora and fixed queries. Do not rewrite
   original expectations or raw baseline; merging PR #7 into main is not required.
2. Install baseline and candidate normally in separate venvs of the same Python.
   Record launcher/runtime, runner and fixture versions/hashes. Dev must point
   to its corresponding frozen checkout. Launcher changes require reinstalling
   the fixed package even with `dev=true`: `git pull` or reloading `src/richi`
   alone does not apply them.
3. Matrix: background 100/10000 × `current-id`/`crowded-lexical` × release/dev,
   eight conditions. Details/history are 0; total current entries 106/10006,
   entities 0. Use identical closed seed copies, corpus hashes, `limit=8` and
   normal budget 16000. Isolate HOME/config/chat environment from the user;
   do not switch live memory or settings.
4. After one untimed warmup per variant/condition pair, collect nine pairs per
   condition: 144 normal CLI invocations, sequentially. Rotate condition order
   each round and alternate baseline→candidate with reverse order. Retain raw
   times, variant median/p95 and paired deltas. Generation, seeding, JSON parsing
   and checks are outside command wall time.
5. Compare complete normal JSON: refs, order, sources, states, read-more, limits
   and budget. Run `--expect` diagnostics with budget 100000 separately from
   timing; compare counters and outcomes. Check unchanged database and nonempty
   WAL; empty/missing WAL are equivalent, SHM excluded. Retain the previous
   broad-query target miss as a quality miss; do not change the oracle or label
   other matches irrelevant.
6. Use a separate profile to verify removal of target imports from the normal
   path, retaining original `.prof` files. Run launcher/config/workspaces tests,
   the full unittest suite and recall acceptance under
   [EVALUATION.md](../skills/project-memory/references/EVALUATION.md).
   Pay particular attention to write/preflight paths that now import locally
   and to dev code freshness.

Acceptance requires unchanged behavior and an actual reduction in unnecessary
imports; timing effects are published for all eight conditions, including
slowdowns. Sequential paired ordering reduces drift but does not control host
load. Caches are not flushed and cold startup is not measured; p95 of nine
repeats is the maximum and does not reliably estimate the distribution tail.
These measurements define no SLA or timing CI gate, do not measure growth of
entities/history/details for one Jira, and promise no speedup for every command
or Python version.

<a id="проверки"></a>
## Checks

Full unittest suite: 565 tests in 580.125 seconds, `OK (skipped=1)` — 564 passed
and one skipped. Recall acceptance: 7/7, database unchanged. These validate
behavioral scenarios; timing is checked separately by the paired experiment above.

<a id="результаты-парного-эксперимента"></a>
## Paired experiment results

[Raw JSON](../evals/results/cli-startup-before-after-2026-10-16.json) was obtained
on October 16, 2026, 20:32:19–20:33:36 Moscow time, on macOS arm64, Python 3.9.6,
SQLite 3.43.2. The runner made 144 normal calls and checked full normal and
diagnostic JSON equality plus unchanged code and database: no violations.
Four `crowded-lexical` conditions retained their previous quality miss; success
here means preserved behavior, not perfect search quality.

| Mode | Background | Query | Median before, ms | Median after, ms | Median paired Δ, ms |
| --- | ---: | --- | ---: | ---: | ---: |
| release | 100 | current-id | 347.07 | 261.50 | −85.85 |
| release | 100 | crowded-lexical | 289.04 | 265.20 | −20.45 |
| dev | 100 | current-id | 307.61 | 275.72 | −34.56 |
| dev | 100 | crowded-lexical | 284.96 | 286.47 | +2.19 |
| release | 10000 | current-id | 364.16 | 281.28 | −37.61 |
| release | 10000 | crowded-lexical | 310.97 | 287.82 | −21.58 |
| dev | 10000 | current-id | 370.28 | 294.88 | −13.79 |
| dev | 10000 | crowded-lexical | 305.04 | 304.51 | −3.82 |

Paired Δ is `candidate_ms − baseline_ms` within each pair; its median differs
from the difference of variant medians. For example, release/current-id/10000
has paired −37.61 ms versus a median difference of −82.88 ms;
dev/current-id/10000 has −13.79 versus −75.40 ms. Host load was uncontrolled
and variation large. There is no uniform speedup: dev/crowded-lexical/100's
median rose 0.5%, and at 10000 it barely changed (−0.2%). These data do not
establish a fixed speedup percentage or SLA.

Separate profiles at 10000 confirmed fewer target imports: release launcher
no longer imported `tempfile`, `shutil` or `subprocess`; dev removed `subprocess`
but retained `tempfile`/`shutil` for a fresh pycache prefix. The child runtime
removed `tempfile` in both modes; `shutil` remained needed by argparse.
This confirms changed work, but profile times do not replace normal measurements.

The first attempt stopped during preparation, before timed calls: local SQLite/VFS
could not open a fresh WAL-mode database copy via `mode=ro` without sidecars.
The harness now runs one setup `mode=rw SELECT` on the synthetic copy, explicitly
closes the connection and checks that the main database SHA matches the seed
copy; only then does it fix the read-only digest. This repairs experiment setup,
not production behavior or performance; invariants were not weakened.

<a id="воспроизведение"></a>
## Reproduction

The [runner](../evals/evaluate_cli_startup.py) requires external helper/fixture
files specifically from `ff966896ec9c28497430ec18b2c39706ee86d5ab` and refuses to
overwrite output. SHA-256 of the files used:

| File | SHA-256 |
| --- | --- |
| evaluate_cli_startup.py | `9d16603ec58d46873354c5f7616ef21bacbf5c216f3849489ac3afa4feaf7762` |
| evaluate_memory_growth.py | `013ea13bef2a127ed0c631454cc0615e744a333d153a633ddcd2c4895cccc908` |
| memory_growth_cases.json | `a38d6fdad925a1f2045b13faad83a611bbe01ed274fba42b0888772df4b60e31` |

Full installed/source launcher and runtime hashes, venv paths, mode selection,
raw pairs and profile tables are saved in JSON. Prepare normal baseline and
candidate installations using the same Python from their respective checkouts,
then supply absolute paths and a new output. Example from the candidate checkout:

```sh
/path/to/baseline-venv/bin/python -I -B evals/evaluate_cli_startup.py \
  --baseline-python /path/to/baseline-venv/bin/python \
  --candidate-python /path/to/candidate-venv/bin/python \
  --baseline-source /path/to/baseline-checkout \
  --candidate-source /path/to/candidate-checkout \
  --growth-evals /path/to/ff96689-checkout/evals \
  --repeats 9 --output /tmp/cli-startup-new.json
```
