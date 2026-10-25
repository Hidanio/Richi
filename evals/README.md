# Public recall acceptance cases

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

## Tracks and shared research

`evaluate_tracks.py` compares fixed installed baseline/candidate revisions on the
frozen `tests/fixtures/tracks.json` corpus, with a real CLI postprocessor in
`track_baseline.py`. It checks evidence parity and measures 30 alternating fresh
processes per scenario at 40/1,000/10,000 entries, query cores, sufficient-context
workflows, visible tokens, migration, indexes and result-write growth. Prepared
summary and standalone-task controls are separate. No model-quality improvement
is inferred from these tool measurements.

See [the complete protocol and commands](../docs/TRACKS_EVAL.md). Full runs require
committed runtime hashes and a cached tokenizer; `--smoke` is diagnostic only.
The [2026-10-25 measurements](../docs/TRACKS_EVAL_RESULTS.md) and
[machine samples](results/tracks-2026-10-25.json) record the completed run against
`f1ade89`, including its latency/token regressions and stronger write contract.

## Retrieval tools and storage growth

After installing the candidate package in a disposable environment:

```sh
python evals/evaluate_search_storage.py --output /tmp/richi-search-storage.json
# Optional: a separate Python with tiktoken and cached o200k_base/cl100k_base encodings.
python evals/evaluate_search_storage.py --output /tmp/richi-search-storage-tokens.json \
  --tokenizer-python /path/to/tokenizer-venv/bin/python
```

`TIKTOKEN_CACHE_DIR` can point to an existing encoding cache. No tokenizer is a
runtime dependency. The experiment fails before running if the explicitly
requested tokenizer cannot initialize. Without one, token counts are unmeasured.

The runner creates only synthetic temporary stores. It compares recall plus
three individual reads with discover plus one batch fetch, checks whole-record
equality and unchanged databases, and measures local fresh-process read latency
over five alternating repetitions. Follow-up IDs are oracle-selected; this is
not a blind test of agent selection or answer quality. Token counts cover tool
text only, excluding instructions, reasoning and billing effects.

A separate graph fixture checks the rendered output cap, root retention and
edge endpoints. A growth fixture keeps 100 current cards while adding 0, 10 and
100 metadata revisions, then checks that explicit index maintenance preserves
all logical rows, rowids and recall results. Reported physical sizes distinguish
current text, history, FTS and free pages when SQLite supports `dbstat`.
Reports include runtime code hashes, raw-output hashes and coverage limitations;
they refuse to overwrite an existing output. Timing is observational, not a CI
threshold, and operating-system caches are shared between processes.

See the [2026-10-23 measured results](../docs/RETRIEVAL_STORAGE_EVAL.md), including
the deliberately incomplete graph view and the absence of disk shrink after
index maintenance.
