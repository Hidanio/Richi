# Retrieval and storage experiment, 2026-10-23

English | [Русский](RETRIEVAL_STORAGE_EVAL.ru.md)

The new tools reduce returned context and repeated process starts while preserving
whole records on demand. They do not change recall ranking or remove its current
candidate scans. Index maintenance preserves all historical knowledge; it does
not implement retention or promise a smaller database.

Reproduce with [the synthetic runner](../evals/evaluate_search_storage.py) and
[instructions](../evals/README.md#retrieval-tools-and-storage-growth). The
[machine-readable result](../evals/results/search-storage-2026-10-23.json) records
installed code hashes, response hashes, token counts and individual timing samples.
This observation used macOS, Python 3.9.6, SQLite 3.43.2 and tiktoken 0.14.0.
No personal database, knowledge or evaluation corpus was used.

## Find candidates, then read three whole records

Eight synthetic cards share a lexical alias. Baseline is `recall --limit 8` plus
three `entry get` calls. Candidate is `discover --limit 8` plus one `fetch` with
the same three exact references and a 32,000-character budget. Selection is an
oracle: the experiment does not test whether an agent chooses the right cards.

| Measurement | Baseline | Candidate | Difference |
|---|---:|---:|---:|
| Tool calls | 4 | 2 | −50% |
| Returned characters | 23,404 | 15,668 | −33.1% |
| Tool text tokens, o200k_base | 4,701 | 3,425 | −27.1% |
| Tool text tokens, cl100k_base | 4,558 | 3,393 | −25.6% |
| Read three records, median milliseconds | 819 | 233 | −71.5% |

Each response is tokenized separately, then summed. All fetched records equal
their full individual-get payloads, including sources, states and timestamps.
Reads leave SQLite main/WAL content and canonical rows unchanged.

The batch envelope itself uses slightly more text than three naked records
(1,556 versus 1,407 o200k tokens); the workflow text saving comes from discovery.
The timing saving comes from doing three reads in one fresh CLI process. Timings
are five alternating repetitions sharing OS caches, not cold-cache results or a
claim that search execution is faster. Instruction loading, reasoning, repeated
follow-ups and model billing are outside these token totals. New guide loading
can offset savings on a first use; this is not an end-to-end agent-cost evaluation.

## Bounded graph overview

Thirty large cards form a star with membership and explicit evidence links.
At depth 2 and a 30-node cap, the full response is 276,932 characters and 47,147
o200k tokens. Opt-in compact output with a 16,000-character budget is 14,905
characters and 3,916 tokens: 94.6% fewer characters and 91.7% fewer tokens.

This is deliberately incomplete: 11 nodes remain, while 19 nodes and 19 edges
are declared omitted. The root remains and edges have visible endpoints. Exact
read pointers allow follow-up. This result demonstrates output containment, not
equivalent information in fewer tokens, bounded peak memory, or answer quality.

## Growth and maintenance

The fixture holds 100 current cards and alternates one card's work state without
changing indexed text. Even checkpoints return to the original work state.

| Additional revisions | Allocated SQLite bytes | Logical history JSON bytes |
|---:|---:|---:|
| 0 | 1,052,672 | 258,970 |
| 10 | 1,114,112 | 310,780 |
| 100 | 1,601,536 | 777,070 |

Logical current text remains 229,480 bytes. The extra 100 revisions add 518,100
history bytes and 548,864 allocated bytes. This illustrates full before/after
payload amplification; it is not a prediction of a real workspace's growth rate.

Explicit index maintenance creates a verified backup and preserves all canonical
rows, rowids and recall results. Allocated size **increases** to 1,646,592 bytes,
with 49,152 free bytes available for SQLite reuse. No disk shrink or search-speed
gain was measured. This is why `storage optimize` promises index maintenance,
not garbage collection. See [storage contracts and next steps](STORAGE.md).

The next search experiment should target indexed ID/alias candidate lookup and
defer loading large payloads. The next storage experiment should compare skipping
unchanged-text FTS updates and deduplicating revision payloads. A cold archive
needs separate retention, evidence reachability and restoration checks.

## Validation

The installed candidate passed the full 623-test run (one Linux-only systemd
parser test skipped on macOS). After the final project-relation read-pointer fix,
all 37 retrieval/graph/storage tests passed against a newly installed wheel,
including the added relation regression. Seven existing recall acceptance cases
passed. All 26 installed runtime files matched the checkout. Skill validation
and Markdown links passed. The experiment above passed every invariant.
