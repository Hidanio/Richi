# Diagnose a missing known record

English | [Русский](RECALL_DEBUG.ru.md)

Use the same bound workspace, project/path, query, state flags, result limit and
reasonable output budget as the failing search. A different successful query is
a control, not proof that the original query works. If the exact ID is known,
full `entry get ID` or `entity get ID` establishes whether the record exists;
no preliminary search is required.

```sh
richi --compact-json --project-path /work/example recall 'why pooling was rejected' \
  --project service-example --expect entry:experiment:pooling --limit 8 --max-chars 16000
```

`--expect` accepts one full `entry:<id>` or `entity:<id>` ref and enables
`--explain`. Without an expected ID, `--explain` gives the general stage report.
Read the actual recorded reason: absent record, state filter, lexical guard,
candidate selection, rank/result limit or output budget. Direct lexical
rejection and final outcome differ: a sourced confirmed graph link can add a
record later. An empty bounded result need not mean `no_match`.

Diagnostics share `--max-chars` with results. Inspect omissions, coverage and
budget; missing details/counters do not mean zero. Keep an ordinary recall
separately when comparing unchanged retrieval behavior, because adding
diagnostics can reduce the displayed cards. These flags explain the algorithm;
they do not change rankings, verify the knowledge or call an LLM.

For a lexical miss, try observed wording or RU/EN terms as controls. Preserve the
original query/expected ref and observed failure. Do not invent aliases, change
states, narrow the test corpus or rewrite knowledge to make a test pass.
No match is not evidence that work never happened. Read full relevant current
records before using their conclusions, and retain experiment conditions.

For `entries_scanned`, `entries_scored`, routing prefilter and FTS internals,
read only [DIAGNOSTICS.md](DIAGNOSTICS.md#счётчики-предварительного-отбора-entries).
Fewer scored payloads alone do not prove constant-time retrieval or better recall.
For a regression/quality evaluation use [EVALUATION.md](EVALUATION.md).

Known FTS limitation: query normalization maps Cyrillic `ё` to `е`, but the
current index stores the original spelling. A title containing `берёза` can be
missed by both `береза` and `берёза` in recall. An exact ID lookup remains
available. Preserve this as a retrieval failure; do not rewrite the record,
invent aliases or rebuild a live index merely to hide the mismatch.
