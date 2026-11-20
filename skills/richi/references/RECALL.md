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

<a id="ключи-задач"></a>

## Issue keys

A token is an issue key only when the store records it; its form decides nothing. `python3`, `sha256` and `gemma-4` are ordinary words until a record declares them. A record declares a key in two ways:

- `jira_key` of an entry. Despite its name, the field takes a key of any tracker in any format: `ABC-123`, `AB#123`, `#123`, `CU-8669abc`, `ПРОЕКТ-12`, `APP.WEB-12`.
- An entry or entity ID `task:<key>` or `jira:<key>`. Any such ID with a number counts, also a descriptive one: `task:phase-0` makes `phase-0` a key and `phase-1` an unknown issue.

A key always contains a digit and at least one other character. `jira_key: "N/A"`, the ID `task:backup` and a bare number such as `17` declare nothing and stay ordinary words; record a numeric issue the way the tracker shows it, for example `#17`. Records in every knowledge state count, and `--where` does not change which tokens are keys.

In a query, a key is a run without whitespace, at most 128 characters long, with no letter, digit or underscore next to it: `(ABC-123).`, a tracker URL and `feature/ABC-123-fix-login` all contain `ABC-123`, while `x_ABC-123` and `ABC-123x` do not. Case does not matter, and `ё` equals `е`; a mention spelled with `ё` in a text still falls under the FTS limitation described in [RECALL_DEBUG.md](RECALL_DEBUG.md). A key of two or more letters and a number has two equal spellings, `ABC-123` and `ABC123`; other formats match as recorded. At a position the longest run wins that is a recorded key or another number of a recorded series: with `WEB-12` and `APP.WEB-7` recorded, `APP.WEB-12` is an unknown issue of the `APP.WEB` series, not `WEB-12`.

A recorded key switches recall to whole-key mode: `selection.jira_mode` is `required_whole_key` and `selection.jira_keys` lists the keys.

- A record is a candidate only when its ID, `jira_key`, title, summary or an alias contains a whole key. `ABC-17` never returns `ABC-18` or `ABC-170`.
- The other words of the question rank these records and add no candidates.
- A record whose `jira_key` is the key gets the reason `exact_jira_key`. A record whose ID is the key, `task:<key>` or `jira:<key>` also gets `canonical_issue_record`.

Another number of a recorded series is an unknown issue, not a set of words. With `ABC-123` recorded, `ABC-999999` returns only records that contain `ABC-999999`; when there are none, the answer is `no_match` with `selection.abstention_reason: no_matching_issue_key`. `AB#999` beside a recorded `AB#123` and `#999` beside `#77` behave the same way. A series is a recorded key without its final number, so a key that does not end in a number, such as `CU-8669abc`, starts none.

The same rule covers a technical token that continues a recorded series. In a store that records `HTTP-12`, `http2` and `http-404` are issues of the `HTTP` series, and only records that contain the whole token are returned. A store without recorded keys never enters whole-key mode. There a key that appears only in titles or texts is an ordinary word: it matches as a whole token, but `ABC123` and `ABC-123` are different words. Record it in `jira_key` to make it a key.

A word with a digit that is not a key matches as a whole token: `sha256` does not find `sha2560`, `gemma-4` does not find `gemma-40`, and `python3` still finds `python3.13`. It remains a technical subject of the question: a record that contains none of the question's identifiers (Latin words with a capital letter, a digit or `-_.:/`) is rejected, which `--explain` reports as `subject_guard`.

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
