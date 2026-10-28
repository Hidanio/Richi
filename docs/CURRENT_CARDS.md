<a id="актуальные-карточки-и-диагностика-длинных-summary"></a>
# Current cards and diagnostics for long summaries

English | [Русский](CURRENT_CARDS.ru.md)

First stage, October 15, 2026: current task summaries, manually preserving details
in existing record types, and the read-only `long_summary` finding.
Baseline: `eb1cb35b20fe9f3d64ef23de7201346336cc3e6e` (main after PR #5).
This is a synthetic demonstration; accumulated user memory is neither a fixture
nor subject to cleanup. The separate Templates pilot is unchanged.

<a id="что-изменилось"></a>
## What changed

`summary` remains the card's sole current body. A stable task record describes
the decision, scope, reasons, active limitations, checks, unknowns and next step.
Independent investigations, decisions and experiments remain ordinary current
entries with sources and `part_of` links from detail to task. Rules are in the
[skill](../skills/project-memory/SKILL.md) and
[maintenance procedure](../skills/project-memory/references/MAINTENANCE.md).

New flag `punisher --summary-warn-chars N` accepts an integer `1..1000000`,
default `6000`; the Python API also rejects `bool`. Length is exact
`len(summary)` of the stored Unicode string, without stripping or normalization.
When `summary_chars > N`, one finding is emitted per selected entry or scoped
entity: `review` for confirmed, `info` for hypothesis/superseded. Length alone
does not establish mixed history, stale knowledge or poor quality.

```json
{
  "code": "long_summary",
  "level": "review",
  "reason": "Read the full record and review whether current state and details would benefit from separation. Length alone does not establish mixed history, outdated knowledge, or reduced quality.",
  "refs": ["entry:task:DEMO-84"],
  "details": {
    "summary_chars": 27368,
    "threshold_chars": 6000,
    "kind": "task",
    "knowledge_state": "confirmed",
    "read_arguments": ["entry", "get", "task:DEMO-84"]
  }
}
```

The threshold is returned as `summary_warn_chars`. For entities the command is
`entity get`; a separate `--` precedes an ID starting with a hyphen. Arguments
are not a shell string. Diagnostics do not include the full summary. A budget
may omit an entire finding, but cannot truncate mandatory details or IDs.
`candidate_count`, `finding_counts`, `omitted_findings` and coverage retain
their meanings; scan caps and integrity `health` are unchanged. Full contract:
[DIAGNOSTICS](../skills/project-memory/references/DIAGNOSTICS.md).

<a id="воспроизводимый-пример"></a>
## Reproducible example

The [fixture](../tests/fixtures/card_maintenance.json) describes fictional
Relay/Ledger and Jira `DEMO-84`. All `example.invalid` links are synthetic;
external sources are not read. The large input card is reproducibly assembled
from substantive blocks and eight editorial copies of those blocks. Copies are
explicitly labeled: they are not new measurements or independent evidence.
Initial and final content is predetermined; the test does not assess a model's
ability to independently shorten unfamiliar text.

Before: one `task:DEMO-84` with a 27,368-character summary mixing the current
decision, receipt race, rejection of cross-shard retry, an old measurement and
an unverified alternative. After: the same ID, 1486 characters and four
self-contained detail records. No new field or heading format is required.

| Current record | Summary characters | States | Purpose |
|---|---:|---|---|
| `task:DEMO-84` | 1486 | confirmed / implemented | Current decision, limits, unknowns, refs |
| `investigation:DEMO-84-receipt-race` | 662 | confirmed / done | Question, trace, method and conclusion limits |
| `decision:DEMO-84-reject-cross-shard` | 621 | confirmed / done | Rejection reason and decision applicability |
| `experiment:DEMO-84-replay-20260910` | 692 | confirmed / done | Confirmed failure and historical measurement |
| `investigation:DEMO-84-epoch-fence` | 638 | hypothesis / planned | Unverified alternative and future check |

Total current summaries after splitting: 4099 characters. The complete original
text also remains in the parent's history; normal search reads current records.
All five retain `jira_key: DEMO-84` and memberships `ledger`, `relay`. Four
sourced edges point from children to task, without reverse duplicates.
The investigation's task membership is confirmed; its hypothesis remains
unconfirmed and requires `--include-hypotheses` for search and traversal.

The main card after splitting still explicitly states:

> The cursor advances only after durable-receipt. The cold-restore exception
> requires manual receipt reconciliation before replay. Implementation is
> implemented; merged and released are unconfirmed. There is no new measurement
> after implementation; the effect of epoch-fence is unknown.

This is an excerpt describing the example, not the full payload.

<a id="карта-сохранения-значимых-фактов"></a>
## Significant-fact preservation map

The fixture's `preservation_map` contains 17 machine-checkable mappings
`fact_id → text → destination → source`. Grouped compactly:

| Facts | Preserved in | Synthetic source |
|---|---|---|
| implemented; merge/release unknown; next step | task | `relay/pull/84` |
| durable-receipt condition; cold-restore exception | task and receipt-race investigation | `relay/spec/receipt-v3` |
| Duplicate receipt on ownership change causes rejection | reject-cross-shard decision | `relay/traces/receipt-race` |
| receipt-window-17 window; comparison of ACK, fsync and receipt-index | receipt-race investigation | `relay/traces/receipt-race` |
| p99 452 ms versus baseline 310 ms; date 2026-09-10 | replay-20260910 experiment | `relay/runs/2026-09-10` |
| 8 shards, 64 workers, TTL 30 s, seed 17; 10,000 receipts; one run | replay-20260910 experiment | `relay/runs/2026-09-10` |
| Failure confirmed only under these conditions; not current production performance | replay-20260910 experiment | `relay/runs/2026-09-10` |
| Unverified epoch-fence, unknown effects/costs, proposed validation method | epoch-fence investigation | `relay/notes/epoch-fence` |

The parent's historical sources are fully preserved, including labels and dates.
Its `verified_at` remains unchanged. The test also verifies every child field,
not only the listed keywords.

Queries are fixed in the fixture before transformation: `DEMO-84`, `двойная
квитанция`, `receipt-window-17`, `durable-receipt`, `cold-restore`, `452`,
`epoch-fence`. All use `limit=8`; only the last explicitly includes hypotheses.
The Jira query must contain the task without requiring top-1; exact
`entry get task:DEMO-84` is tested separately. After moving details, the unique
investigation window is also found through FTS `search`, despite no longer
appearing in the task summary.

Full-read volume is measured as Unicode characters of `entry get` JSON stdout,
including formatting and the final newline. Each needed record is read once per
scenario; search, graph and history auditing are excluded.

| Scenario | Before: one full task | After: needed full records |
|---|---:|---:|
| Current state | 28,978 | 2990 — task |
| Rejection reason with current context | 28,978 | 4699 — task + decision |
| All preserved facts | 28,978 | 8906 — task + four detail records |

<a id="проверки-и-повторение"></a>
## Checks and reproduction

From the checkout root, install normally in a disposable venv:

```sh
CARDS_VENV="$(mktemp -d /tmp/richi-cards-venv.XXXXXX)"
python3 -m venv "$CARDS_VENV"
"$CARDS_VENV/bin/python" -m pip install --upgrade pip
"$CARDS_VENV/bin/python" -m pip install .
"$CARDS_VENV/bin/python" -m unittest discover -s tests -v
"$CARDS_VENV/bin/python" evals/evaluate_recall.py
"$CARDS_VENV/bin/python" tests/test_card_maintenance.py --report
```

For focused runs, pass `-p test_punisher.py` or `-p test_card_maintenance.py`
to unittest discover. Test subprocesses use existing `cli_environment`:
temporary home/registry/store and no ambient chat IDs. User runtime and dev
config are not switched. The [CI](../.github/workflows/tests.yml) matrix covers
macOS/Linux, Python 3.9 and 3.12.

The workflow creates children with create-only CAS and verifies them, then edges
and search; the parent is shortened last with the original `expected_updated_at`.
Tests cover failures after some children and before parent replacement,
resuming with the same IDs without extra revisions, rejecting a changed child,
and a CAS conflict from a concurrent parent edit. Multiple commands are not
one transaction.

<a id="границы-результата"></a>
## Result boundaries

`entry read`, `recall` and `brief` can truncate even the resulting short task
summary. The test demonstrates specific omissions of significant facts, checks
excerpt/truncated/omissions/read_more flags and restores conditions through full
`get`. In this fixture, `entry read --max-chars 2000` (1957 stdout characters)
and `brief --max-chars 4000` (3529) omit cold-restore and the next step.
`recall --max-chars 2000` omits the whole card: a 414-character report with a
positive candidate count, `no_match=false`, omissions and exact `read_more`.
All three responses explicitly mark `truncated`. The test does not require
every qualification to always fit an excerpt. Before a condition-dependent
action, read the complete relevant records.

Character counts illustrate reading volume in this example. Time/token savings
and improved LLM quality on real tasks were not measured. Schema, retrieval,
output truncation, launcher/updater and map are unchanged. Actual manual cleanup
of user-selected cards and guarantees of complete semantic units remain
separate stages.
