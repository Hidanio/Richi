# Agent workflow payload and context evaluation

This evaluation measures prerecorded local tool workflows against synthetic data.
It does not run a language model, judge generated answers or measure reasoning,
production accuracy, billing, cache effectiveness or a complete real conversation.
It compares fixed installed baseline `b8a2a9aa889a7d65d433e985ca3f6a9d4cec4c60`
with the candidate. Earlier search/index benchmark reports remain unchanged.

## What is compared

The fixture contains 60 Atlas entries and 20 unrelated Beacon entries, with
mixed knowledge/work states, two sources per record, and conditions near the end
of each roughly 1,000-character summary. Three confirmed Atlas decisions are the
required full evidence for the pool-sizing question. Fifty current confirmed
Atlas entries form the independent count/distribution/projection oracle. There
are no inter-project relations; baseline `context --project atlas --limit 200`
therefore enumerates the complete relevant scope. Superseded records are not
needed for these explicitly confirmed-only tasks.

The expected answers come from the fixture generator, not the candidate's filter
parser or query output. Baseline APIs seed a disposable database; SQLite backup
creates the candidate copy. Index construction is explicit setup, outside agent
read workflows. Required whole records must equal the baseline full-get payload,
including sources, conditions, states and timestamps. Read operations preserve
canonical rows and physical main/WAL fingerprints.

| Workflow | Existing baseline tools | Candidate tools | Required outcome |
| --- | --- | --- | --- |
| Scoped evidence | Strict-project search; also discover + fetch | Direct filtered full search; also filtered discover + fetch | Same three entire records and sources |
| Count and state distribution | Complete project context + selection/grouping | Exact count + facets | Same total and every group, including null |
| Positive and negative existence | Complete context + explicit predicates | Two scoped exists queries | True and false within stored confirmed scope |
| Metadata inventory | Complete context + projection | Select requested fields | Same complete ordered rows; bodies intentionally omitted |
| Known/missing references | Fetch exact refs | Same fetch | One identical full record and one explicit missing outcome |

Each relevant baseline has both a raw-output variant and a **working local Python
postprocessor**. Its entire actual program appears in the recorded command
arguments. It consumes CLI JSON locally and exposes only its final result to the
model: hidden intermediate records are measured as off-model bytes, never charged
as visible context. This is a stronger baseline than dumping all records into the
model. A native tool is not presumed cheaper than that baseline.

Raw-baseline filtering/counting uses the deterministic oracle to model the
necessary selection/arithmetic; it is not an observed agent successfully doing
those steps. The program baseline actually executes its recorded code. The
program's existence and size are measured; the number of reasoning tokens or
engineering attempts needed to write it is unknown.

The main projection requests up to 200 rows and follows continuation if required
by its character budget. A separate seven-row cursor check verifies exact order,
no duplicates or gaps, and complete union. Its calls have their own transcript and
metrics; they are not silently charged to or removed from the main workflow.

## What the token totals include

For each arm, the runner counts:

- The identical task prompt and that arm's complete core skill.
- Only the operation documentation actually loaded: a focused baseline retrieval
  document or the search/context section of the CLI guide; candidate retrieval
  or query contract, plus the filter guide prefix through string-predicate
  semantics when filters are used. Full query/pagination semantics are loaded;
  parser help alone is insufficient.
- Every normalized command argument and exact visible response, including JSON
  formatting and the final newline.
- Handcrafted program source encoded separately for inspection; that code is
  already included in the command arguments and is **never added twice**.
- Logical tool calls and declared model decision rounds separately.

Two candidate startup paths use identical progressive/metadata data operations.
`candidate_native` loads the known operation guides directly, as the core skill permits;
`candidate_catalog` first lists the relevant capability group and describes the
selected commands, then loads the same semantic guides. Optional discovery
overhead is therefore visible rather than forced into every native-tool use.
Independent guide reads share a decision round. A separate direct filtered-search
variant handles the precisely scoped evidence task using the same full-search
limit/output as the baseline and charges the CLI search section plus the filter
contract. It preserves the exact full-record oracle.

Tool arguments use a documented normalized envelope: `{"cmd":"richi ..."}`.
Harness-only temporary `--db`, configuration and home-directory selectors are not
agent-visible arguments. Provider tool framing and orchestration transport are
not modeled. `read_document` is a normalized documentation-read adapter, not a
new Richi command. Independent known schema descriptions and independent query
operations may share one decision round; dependent calls remain separate.

Workflows are independent task scenarios; summing their cold totals does not
represent one measured conversation. The separately encoded unescaped program
source is descriptive: its tokenization need not match shell/JSON-escaped argument
substrings exactly.

Both `o200k_base` and `cl100k_base` tokenize each visible message independently.
The tokenizer must use an already populated cache; network downloads are disabled.
Reports retain exact command arguments, response hashes/lengths, answers and
per-step token counts rather than duplicating every large response body.

Two views are reported:

1. **Cold bound-chat workflow**: the workspace and repository have already been
   selected/validated. The task starts with its prompt and loaded core skill,
   then loads the relevant operation contracts and executes the recorded calls.
2. **Operation-only increment**: the new prompt and data-operation calls, with
   previously known skill/contracts excluded. This is not a claim that an
   existing conversation or loaded documentation consumes zero context.

`terminal_visible_tokens` is the sum of independently encoded visible messages.
`context_at_model_boundaries` contains that accumulated visible input before the
first decision and after each batch of tool responses, including the final
answer opportunity. Their sum is an illustrative cumulative-input model that
assumes the visible history is sent again without compaction or cache discounts.
It excludes unobserved reasoning and final-answer text. An additional prior chat
prefix of P tokens adds P to terminal context and P times the number of decision
boundaries to cumulative input. These values are **not measured billed tokens**.

Known-ref data payloads should not shrink merely because new commands exist.
Aggregate/projection results legitimately omit record bodies because those tasks
ask for metadata only; they are not evidence for a factual conclusion or an update.
The evidence workflow receives no credit for truncating required records.

## Reproduce

Install both versions normally in separate temporary environments, and provide
matching source trees for their skill documents. Do not use editable installs or
personal workspaces. The script accepts no existing database.

```sh
TIKTOKEN_CACHE_DIR=/path/to/prepopulated-cache \
  python3 evals/evaluate_agent_workflows.py \
  --baseline-python /path/to/baseline/bin/python \
  --candidate-python /path/to/candidate/bin/python \
  --baseline-source /path/to/frozen-b8a2a9a-source \
  --candidate-source /path/to/candidate-source \
  --tokenizer-python /path/to/tokenizer/bin/python \
  --output /path/to/new-report.json
```

`--baseline-only` is a contract smoke, not a comparative result. Output files must
be new. No latency claims are made; capability contracts and token counts, not
wall-clock timing, are the subject of this evaluation.

## Results

Published synthetic run: 2026-10-25, Python 3.9.6, SQLite 3.43.2,
tiktoken 0.14.0. The [complete machine-readable report](../evals/results/agent-workflows-2026-10-25.json)
records the fixture/script/runtime hashes, exact normalized arguments, answer
and response hashes, component counts, both encodings, and every variant.
All 21 main workflow variants passed their predetermined oracles. The separate
eight-page projection check returned the exact 50-row union/order. Index setup
preserved canonical rows; every recorded read preserved canonical and physical
main/WAL fingerprints in both arms.

In every paired token cell below, numbers are **o200k / cl100k**. The strong
baseline is the executed Python pipeline, except unchanged native fetch for
known refs. The native evidence route uses direct full search with strict
filters; the progressive route remains reported separately.

### Operation-only visible context

| Workflow | Raw baseline | Strong baseline | Native tools | Reduction vs strong baseline |
| --- | ---: | ---: | ---: | ---: |
| Full evidence | 3,341 / 3,370 | 1,125 / 1,130 | 1,006 / 1,008 | 10.6% / 10.8% |
| Count + distribution | 16,467 / 16,597 | 291 / 292 | 222 / 218 | 23.7% / 25.3% |
| Positive + negative existence | 16,471 / 16,602 | 272 / 275 | 223 / 218 | 18.0% / 20.7% |
| Complete metadata inventory | 16,463 / 16,593 | 1,044 / 1,066 | 941 / 959 | 9.9% / 10.0% |
| Known + missing refs | 506 / 510 | 506 / 510 | 506 / 510 | 0.0% / 0.0% |

The metadata-tool reductions against the strong baseline are about 10–25%,
not the roughly 94–99% reductions visible against raw context dumping. Direct
filtered search saves about 11% against the Python evidence pipeline while
preserving all three required full records and their two sources each. Existing
exact-ref fetch has identical data operations and no operation-only saving.

Progressive evidence discovery is a different tradeoff: existing discover + fetch
costs 5,102 / 5,111 tokens; filtered discover + fetch
costs 1,965 / 1,974. It beats that same progressive baseline,
but costs more than either the full-search Python pipeline or direct filtered
search on this small fixture. It is useful when selecting references first is
necessary; it is not a universally cheaper entry-search route.

### Cold bound-chat visible context

| Workflow | Strong baseline | Known native operation + semantic guides | Optional catalog + semantic guides |
| --- | ---: | ---: | ---: |
| Full evidence | 3,705 / 4,154 | 4,355 / 4,805 | 5,958 / 5,945 |
| Count + distribution | 2,871 / 3,316 | 3,058 / 3,054 | 3,722 / 3,690 |
| Positive + negative existence | 2,852 / 3,299 | 3,654 / 3,654 | 4,037 / 4,022 |
| Complete metadata inventory | 3,624 / 4,090 | 3,777 / 3,795 | 4,229 / 4,229 |
| Known + missing refs | 3,004 / 3,006 | 3,303 / 3,304 | 3,690 / 3,679 |

The evidence catalog column uses progressive discover + fetch, the route whose
descriptions were requested. Its direct-known progressive counterpart costs
5,357 / 5,368 tokens; the direct-search column loads the CLI search section
and filter semantics. Other catalog variants use exactly the same data operations
as their direct-known counterparts.

For o200k, every measured cold native workflow costs more than the strong
baseline. For cl100k, count/distribution and projection have small cold terminal
savings, while evidence, existence and known-ref fetch regress. The difference
comes partly from the encodings treating the Russian baseline CLI section
differently. On-demand catalog discovery adds cost rather than producing an
immediate token saving in these already-defined tasks.

### Accumulated visible input

The following o200k numbers sum visible prefixes at declared model decision
boundaries. They are an illustrative replay model, not billing measurements;
both encodings and every boundary are available in the report.

| Workflow | Cold baseline → native | Operation-only baseline → native |
| --- | ---: | ---: |
| Full evidence | 8,037 → 9,630 | 1,154 → 1,035 |
| Count + distribution | 7,203 → 7,820 | 320 → 251 |
| Positive + negative existence | 7,192 → 9,019 | 305 → 256 |
| Complete metadata inventory | 7,948 → 8,531 | 1,069 → 966 |
| Known + missing refs | 7,248 → 8,020 | 532 → 532 |

Cold cumulative input increases in every o200k strong-baseline comparison.
For cl100k, count/distribution and projection decrease; the other cases increase.
Optional catalog rounds add input in every case. Reusing already loaded
contracts improves the incremental comparison, but the existing conversation
still occupies context and may be replayed; the operation-only view excludes that
unknown prefix explicitly.

### Calls, code and limits

| Workflow | Data calls: baseline → native | Executed baseline analysis code, characters → native |
| --- | ---: | ---: |
| Full evidence | 1 → 1 | 291 → 0 |
| Count + distribution | 1 → 2 | 400 → 0 |
| Positive + negative existence | 1 → 2 | 348 → 0 |
| Complete metadata inventory | 1 → 1 | 308 → 0 |
| Known + missing refs | 1 → 1 | 0 → 0 |

Independent count/facets and the two existence calls share one model decision
round in the declared plans, although each remains a separate logical tool call.
Native tools remove these handwritten postprocessors; they do not remove the
need to choose a scope, fields, predicates or a continuation strategy. The cost
of authoring/debugging code was not measured. Aggregate/projection tasks
intentionally omit source arrays and evidence bodies; full-evidence tasks do not.

This fixture covers entry metadata and a small complete project scope. It does
not demonstrate an autonomous agent choosing the correct workflow, an entity
analysis workload, a cross-page consistent snapshot, or behavior above the
baseline context limit. No speed claim follows from this token evaluation.
Neither favorable raw-output percentages nor these small synthetic tasks
establish a universal token, context, cost or answer-quality improvement.
