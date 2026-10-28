<a id="энциклопедия-понятий"></a>

# Concept encyclopedia

English | [Русский](ENCYCLOPEDIA.ru.md)

The encyclopedia is a view of existing `entities` with `kind: concept`.
It uses the same SQLite database, history, sources and graph. No schema migration
or separate MCP is required. On the website, the “Encyclopedia” switch opens
the definitions catalog; a card leads to sources and related map nodes.

<a id="одно-название-несколько-значений"></a>

## One name, several meanings

The unit of knowledge is a **concept in a specific context**, not a unique word.
Different cards may share aliases. Search does not merge them.
For example, `Y — a coordinate in a graphics editor` and `Y — a model's target variable`
could be separate concepts if both meanings are supported by sources.
This illustrates how meanings are distinguished, rather than claiming that a second meaning
exists in any real system.

Distinguish these situations:

| Situation | How to store it |
|---|---|
| Russian/English name, abbreviation or alternative spelling of the same meaning | `aliases` on one card |
| Different meaning, contract, component or scope | A separate `concept` with context in its ID and title |
| More precise wording with the same meaning | Update the card with `expected_updated_at`; history is preserved |
| Meaning/contract changes over time | A separate variant with version/period in its context; a `supersedes` edge only when replacement is proven |
| Two incompatible explanations without sufficient evidence | Explicit `hypothesis` alternatives; do not choose one as fact |

Versions used in parallel remain `confirmed` within their own scopes.
Mark an old card `superseded` only when its replacement
in the stated context has been confirmed. `verified_at` is the date a source was checked,
not when the definition appeared or the version was released. State known validity periods
and versions in the context; do not invent unknown dates.

<a id="карточка"></a>

## Card

Recommended stable ID: `concept:<project-or-domain>:<term>:<scope>`.
The title should contain the term and distinguishing context. For example:
`Y — a coordinate in a graphics editor`.

Use short semantic sections in `summary`:

```text
Определение: 1–3 предложения о смысле понятия.

Контекст: проект, компонент/API, версия или период, если известны.

Пример: небольшой пример использования, подтверждённый источником.

Ограничения: существенные особенности и отличия от похожих понятий.
```

The first paragraph serves as the short definition in the catalog. Do not add
empty sections or artificial examples just to fill a template. Research and task history
remain in their own records; the definition links to them rather than copying
the entire analysis. In `sources`, name verified code/documentation locations, using
stable commit links where possible. Locally checked code does not prove
production state. For `confirmed`, the CLI checks source presence;
the agent checks source content.

Entity fields and write commands are described in [GRAPH.md](GRAPH.md). An update fully
replaces the payload: first run `entity get ID`, preserve sources and other
fields, and pass `expected_updated_at`. Write a new card with
`expected_updated_at: null` to avoid overwriting a concurrent record.

<a id="область-применения-и-связи"></a>

## Scope and relationships

Project scope is defined by an existing explicit edge:

```text
entity:concept:... --used_in--> project:<project-id>
```

Only direct confirmed `used_in` relationships with a source in this direction
are considered for scope display and priority boosting in `recall`.
Graph connectivity, shared tags and project mentions do not define concept scope.
If a card applies to several projects, add several such
relationships. If the contracts differ, create separate cards. A missing relationship
means “project scope is unspecified,” not “universal definition.”

Create additional relationships only from evidence:

- `variant_of`: specific definition → more general concept;
- `supersedes`: new meaning/contract → replaced old meaning/contract;
- `references`: definition → analysis or task with details;
- `related_to`: semantic relationship explained in the edge description.

A matching word does not prove `variant_of` or `related_to`. A
`supersedes` relationship alone does not change the old card's `knowledge_state`.
Records for the same word may have no semantic relationship at all.

<a id="чтение"></a>

## Reading

```sh
richi recall 'Что такое Y?' --project service-example
richi recall 'BIT-vector' --max-chars 16000
richi entity get concept:example:term:scope
richi graph neighbors entity:concept:example:term:scope --depth 1
```

The ID `concept:example:term:scope` is illustrative. The actual ID is returned by
`recall`. Short terms are searched through explicit concept aliases, not by matching
one letter within arbitrary text. Titles/aliases should reflect
actual spelling; full morphology and embeddings are not added.

When several concepts match, `recall` reports `ambiguity` with alternatives.
`--project` raises contextual priority and does not turn a match into the only
possible meaning. Limits and budgets may hide some alternatives; check
omission counters. Read the full relevant card before drawing a conclusion.
If task context distinguishes the meanings, use it and name the selected
scope; otherwise, briefly show alternatives and clarify the intended meaning.

Hypotheses and superseded definitions are hidden by default. For a historical
question, use the required flags and, if necessary, card history.
The encyclopedia shows the loaded graph export; the interface warns
when that export is truncated. Absence from the catalog does not prove absence
of knowledge from the entire database.

<a id="как-пополнять"></a>

## Adding concepts

During real work, extract a concept when its explanation will be needed
again, its meaning is system-specific or misinterpretation affects a decision.
First search existing meanings, then verify sources and context.
Choose synonyms and tags independently from actual usage. Do not create
a bulk dictionary of general terms unrelated to project work. Do not turn
an unverified guess into a definition. There is no background conversation import.
