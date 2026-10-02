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
