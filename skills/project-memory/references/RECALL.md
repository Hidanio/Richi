<a id="компактное-чтение-и-recall"></a>

# Compact reading and recall

English | [Русский](RECALL.ru.md)

Diagnose a known miss using [RECALL_DEBUG.md](RECALL_DEBUG.md).
For composed strict `--where` filters and explicit index building, see
[FILTERS.md](FILTERS.md).

If several records need to be read in full after searching, use
[`discover` and `fetch`](RETRIEVAL_TOOLS.md): first small cards for selection,
then complete selected records in one call. This is a separate presentation mode,
not a new ranking model; discovery does not replace evidence.

The new commands read one consistent database snapshot, do not change knowledge and do not access the network. No schema migration is required. Python 3.9+, standard library.

<a id="команды"></a>

## Commands

```sh
richi recall 'Почему выбрали локальный кеш?' --project service-example
richi recall 'ABC-123' --limit 8 --max-chars 16000
richi entry read task:ABC-123 --max-chars 6000
richi entry get task:ABC-123
richi entry get task:ABC-123 --history
```

`recall` accepts a question or technical keywords. `--limit` is 1–200, default 8; `--max-chars` is 2000–100000, default 16000. Characters in **the entire JSON on stdout** are counted, including indentation and the final newline; these are not tokens or UTF-8 bytes. The query is limited to 2000 characters.

`entry read` returns a compact current card; the default budget is 6000 characters. `entry get` returns the full current record **without history**. `entry get --history` also returns all previous revisions. For `entity get` and `edge get`, history is likewise explicitly enabled through `--history`. Only reading has changed: historical data is preserved. Older `search` and `context` remain available with their previous full output; `recall` is now the preferred first entry point for an agent.

Before writing changes, read full `get`, preserve original sources and fields, and pass `expected_updated_at`. A compact card must not be used as an update payload: it represents only part of a record.

<a id="как-отбирается-контекст"></a>

## How context is selected

1. Exact IDs, Jira keys and aliases receive high priority. A task number is not split into a general `REC` token and a number.
2. Text retrieval uses FTS5 with weights for ID, title, Jira, aliases and tags. Additional lexical scoring explains matching terms. Without FTS5, Unicode scan is used. There is a small list of common Russian and English question words.
3. `--project` raises priority for project records while preserving the ability to find experience from neighboring services. It is **not a strict filter**. `search --project` remains available for strict entry filtering.
4. One hop along explicit sourced relationships is allowed from the top three results. Up to two result slots may go to related records. Automatic `belongs_to_project` relationships do not expand into all project records. Exact matches are protected from displacement by the graph.
5. Entities are searched by ID, title, summary, aliases and tags. Content is not duplicated into entries for indexing. An explicitly built index narrows lexical candidates; without it, scan remains available.

Only `confirmed` is available by default. `--include-hypotheses` and `--include-superseded` enable their respective states separately. A filtered endpoint cannot enter results through a relationship. On schema 1, recall works without new entities or the graph.

<a id="что-означает-выдача"></a>

## What the output means

`results` contains the original `id`, full `ref` (`entry:...` or `entity:...`), type, title, text excerpt, states, verification date, several sources, `match_reasons` and `read_more`. In `read_more`, command and ID are structurally separated: `{"command":"entry get","id":"task:ABC-123"}`. When launching from a shell, quote the ID correctly.

`summary_excerpt` consists of complete sentences from the original text. Under a limited budget, the beginning, sentences with caveats and the conclusion receive priority. This is a heuristic excerpt, not an automatically written new conclusion. `[…]` marks an omission. If a sentence itself exceeds the budget, it may be omitted entirely.

`is_excerpt` and `excerpt_note` warn that important conditions may be missing. **Before making a technical conclusion, read the full relevant record.** `sources_omitted` reports the number of sources outside the card; remaining links and their dates are not truncated. `source_count` refers to the whole record. `edges` contains available evidence for graph additions.

A card selected only through the graph is retained together with a direct match
and the connecting edge. If the budget cannot preserve this group, the neighboring
card is omitted. `truncated` also accounts for query and edge shortening and
reaching internal retrieval limits.

`budget` reports actual size, the limit and omitted result/edge counts. Under a tight budget, the excerpt is shortened and source count reduced to one first, then edges and card count. If even a whole source with a minimal card cannot fit, the card is omitted with a further-read command. In that case, `no_match=false` with empty `results` means a match was found but did not fit.

`candidate_count` is the number of selected candidates, not the total database size. `no_match=true` means no record was found, not proof that the work never happened. `retrieval_score` ranks matches; it is not a truth probability or answer confidence.

<a id="понятия-и-неоднозначные-термины"></a>

## Concepts and ambiguous terms

For a definition, use `recall 'Что такое Y?' --project PROJECT_ID`.
Short letter aliases are matched exactly among concepts; a letter inside another
word is not a match. Concepts sharing a name remain separate cards.
Their `project_ids` and project priority come only from direct confirmed
sourced `used_in` edges directed from the concept to the project.

If an alias matches several available concepts, the response includes
`ambiguity`: `detected`, `matched_aliases`, `concept_count`, `candidates` and
`omitted_candidates`. Alternatives show refs, titles, scopes and states.
The ambiguity flag survives even with `--limit 1`; the budget may shorten
the alternative list but does not turn the remaining alternative into the only meaning.
The project raises relevance without resolving ambiguity automatically.
Absence of `ambiguity` does not prove that meanings unknown to the database do not exist.

Rules for separating synonyms, contexts, versions and superseded definitions are
in [ENCYCLOPEDIA.md](ENCYCLOPEDIA.md).

<a id="актуальность-и-ограничения"></a>

## Currency and limits

`needs_recheck` with `recheck_reasons` marks a current-state question or missing verification date. Current-state detection is heuristic; absence of the flag does not prove freshness. `verified_at` does not replace a measurement, merge or release date. The agent checks current state in the source system; recall itself does not read issue trackers, repository hosting or production.

The system uses text and already stored relationships. Embeddings, automatic synonyms and full Russian morphology have not been added. For a miss, try another formulation or technical term. Knowledge age should not automatically be treated as proof of staleness.

The FTS shortlist is limited to 160 candidates, and edge reads to 100 per anchor node. Relevant limits and truncation indicators are in `selection`. After explicit `search-index build`, exact routes and entity lexical candidates are selected through the index. Without the index or for unindexed records, scan is retained. A composed filter is applied before the shortlist and to graph additions; output limits do not bound all internal reading.

<a id="проверка-качества"></a>

## Quality checks

Public tests use synthetic records and temporary databases. Private
snapshots, real work questions and their expected outcomes are not distributed.
The protocol for reproducible comparisons and handling discovered misses is described
in [EVALUATION.md](EVALUATION.md).

This checks retrieval and context format. It does not confirm the truth of every record or current release status. Dataset questions are not hardcoded into the algorithm.
