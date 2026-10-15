# Актуальные карточки и диагностика длинных summary

Первый этап от 15 октября 2026: текущая task summary, ручное сохранение
подробностей в существующих записях и read-only finding `long_summary`.
Baseline: `eb1cb35b20fe9f3d64ef23de7201346336cc3e6e` (main после PR #5).
Это синтетическая демонстрация; накопленная пользовательская память не служит
fixture и не подвергается уборке. Отдельный пилот Templates не меняется.

## Что изменилось

`summary` остаётся единственным текущим содержанием карточки. Стабильная
task-запись описывает решение, область, причины, действующие ограничения,
проверки, неизвестное и следующий шаг. Самостоятельные исследования, решения
и эксперименты доступны как обычные текущие entries с источниками и связями
`part_of` от подробной записи к задаче. Правила — в
[skill](../skills/project-memory/SKILL.md) и
[порядке обслуживания](../skills/project-memory/references/MAINTENANCE.md).

Новый флаг `punisher --summary-warn-chars N` принимает целое `1..1000000`,
по умолчанию `6000`; Python API также отклоняет `bool`. Длина — точный
`len(summary)` сохранённой Unicode-строки, без strip/нормализации.
При `summary_chars > N` выдаётся один finding на выбранную entry или scoped
entity: `review` для confirmed, `info` для hypothesis/superseded.
Длина сама по себе не доказывает смешение истории, устаревание или плохое качество.

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

Порог возвращается как `summary_warn_chars` в отчёте. Для entity команда —
`entity get`; перед ID с начальным дефисом добавляется отдельный `--`.
Аргументы не являются shell-строкой. Полный summary не включается в диагностику.
Бюджет может исключить весь finding, но не обрезать его обязательные details
или ID. `candidate_count`, `finding_counts`, `omitted_findings` и coverage
сохраняют свой смысл; scan caps и integrity `health` не меняются.
Подробный контракт — в [DIAGNOSTICS](../skills/project-memory/references/DIAGNOSTICS.md).

## Воспроизводимый пример

[Fixture](../tests/fixtures/card_maintenance.json) описывает вымышленные
Relay/Ledger и Jira `DEMO-84`. Все ссылки `example.invalid` синтетические,
внешние источники не читаются. Большая входная карточка воспроизводимо
собирается из содержательных блоков и восьми редакторских копий этих блоков.
Копии явно обозначены: они не представляют новые измерения или независимые
свидетельства. Исходное и итоговое содержание заранее заданы; тест не оценивает
способность модели самостоятельно сокращать неизвестный текст.

До: одна `task:DEMO-84`, 27 368 символов summary. В ней смешаны текущее
решение, гонка квитанций, отказ от межсекционного retry, старый замер и
непроверенная альтернатива. После: тот же ID, 1486 символов и четыре
самодостаточные подробные записи. Новый формат полей/заголовков не требуется.

| Текущая запись | Символов summary | Состояния | Назначение |
|---|---:|---|---|
| `task:DEMO-84` | 1486 | confirmed / implemented | Текущее решение, ограничения, неизвестное, refs |
| `investigation:DEMO-84-receipt-race` | 662 | confirmed / done | Вопрос, трасса, метод и границы вывода |
| `decision:DEMO-84-reject-cross-shard` | 621 | confirmed / done | Причина отказа и применимость решения |
| `experiment:DEMO-84-replay-20260910` | 692 | confirmed / done | Подтверждённая неудача и историческое измерение |
| `investigation:DEMO-84-epoch-fence` | 638 | hypothesis / planned | Непроверенная альтернатива и будущая проверка |

Итого текущие summary после разделения — 4099 символов. Полный исходный текст
дополнительно остаётся в истории родителя; обычный поиск читает текущие записи.
Все пять записей сохраняют `jira_key: DEMO-84` и memberships `ledger`, `relay`.
Четыре sourced-ребра направлены от детей к задаче, без обратных дублей.
Принадлежность исследования задаче подтверждена; его гипотеза остаётся
неподтверждённой и требует `--include-hypotheses` для поиска и обхода.

Основная карточка после разделения всё ещё явно сообщает:

> Cursor продвигается только после durable-receipt. Исключение cold-restore
> требует ручного сопоставления квитанций до replay. Реализация implemented;
> merged и released не подтверждены. Нового замера после реализации нет,
> эффект epoch-fence неизвестен.

Это выдержка для описания примера, а не полный payload.

## Карта сохранения значимых фактов

Машинно проверяемые 17 соответствий `fact_id → text → destination → source`
находятся в `preservation_map` fixture. Их компактная группировка:

| Факты | Куда сохранены | Синтетический источник |
|---|---|---|
| implemented; merge/release неизвестны; следующий шаг | task | `relay/pull/84` |
| Условие durable-receipt; исключение cold-restore | task и investigation receipt-race | `relay/spec/receipt-v3` |
| Двойная квитанция при смене владельца — причина отказа | decision reject-cross-shard | `relay/traces/receipt-race` |
| Окно receipt-window-17; сравнение ACK, fsync и receipt-index | investigation receipt-race | `relay/traces/receipt-race` |
| p99 452 ms против baseline 310 ms; дата 2026-09-10 | experiment replay-20260910 | `relay/runs/2026-09-10` |
| 8 секций, 64 worker, TTL 30 s, seed 17; 10 000 квитанций; один прогон | experiment replay-20260910 | `relay/runs/2026-09-10` |
| Неудача подтверждена только в этих условиях; не текущая production-производительность | experiment replay-20260910 | `relay/runs/2026-09-10` |
| Непроверенный epoch-fence, неизвестные эффект/расходы, предложенный метод проверки | investigation epoch-fence | `relay/notes/epoch-fence` |

Исторические источники родителя сохранены полностью, вместе с labels и датами.
`verified_at` родителя остаётся прежним. Тест проверяет также все поля детей,
а не только перечисленные ключевые слова.

Запросы зафиксированы в fixture до преобразования: `DEMO-84`, `двойная
квитанция`, `receipt-window-17`, `durable-receipt`, `cold-restore`, `452`,
`epoch-fence`. У всех `limit=8`; только последний использует явный флаг
гипотез. Jira-запрос должен содержать задачу без требования top-1; точный
`entry get task:DEMO-84` проверяется отдельно. Уникальное окно расследования
после переноса находится и через FTS `search`, хотя его больше нет в task summary.

Объём полного чтения измерен как Unicode-символы JSON stdout команды `entry get`,
включая форматирование и завершающий перевод строки. В каждом сценарии каждая
нужная запись читается один раз; поиск, граф и аудит истории в это число не входят.

| Сценарий | До: одна полная task | После: нужные полные записи |
|---|---:|---:|
| Текущее состояние | 28 978 | 2990 — task |
| Причина отказа с текущим контекстом | 28 978 | 4699 — task + decision |
| Все сохранённые факты | 28 978 | 8906 — task + четыре подробные записи |

## Проверки и повторение

Из корня checkout установите пакет обычным способом в одноразовый venv:

```sh
CARDS_VENV="$(mktemp -d /tmp/richi-cards-venv.XXXXXX)"
python3 -m venv "$CARDS_VENV"
"$CARDS_VENV/bin/python" -m pip install --upgrade pip
"$CARDS_VENV/bin/python" -m pip install .
"$CARDS_VENV/bin/python" -m unittest discover -s tests -v
"$CARDS_VENV/bin/python" evals/evaluate_recall.py
"$CARDS_VENV/bin/python" tests/test_card_maintenance.py --report
```

Для focused-прогонов используйте `-p test_punisher.py` или
`-p test_card_maintenance.py` у unittest discover. Тестовые subprocess используют
существующий `cli_environment`: временные home/registry/store, без ambient chat
IDs. Пользовательский runtime и dev config не переключаются. Матрица
[CI](../.github/workflows/tests.yml): macOS/Linux, Python 3.9 и 3.12.

Workflow создаёт детей с create-only CAS, проверяет их, затем рёбра и поиск;
родитель сокращается последним с исходным `expected_updated_at`. Тесты
проверяют сбои после части детей и перед заменой родителя, продолжение по тем
же ID без лишних ревизий, отказ при изменённом ребёнке и CAS-конфликт
параллельной правки родителя. Несколько команд не являются одной транзакцией.

## Границы результата

`entry read`, `recall` и `brief` могут усечь даже итоговую короткую task summary.
Тест показывает конкретные пропуски значимых фактов, проверяет признаки
excerpt/truncated/omissions/read_more и восстанавливает условия полным `get`.
В этом fixture `entry read --max-chars 2000` (1957 символов stdout) и
`brief --max-chars 4000` (3529) опускают исключение cold-restore и следующий
шаг. `recall --max-chars 2000` опускает карточку целиком: 414 символов
отчёта с положительным candidate count, `no_match=false`, omissions и
точным `read_more`. Все три ответа явно отмечают `truncated`.
Он не требует, чтобы все оговорки всегда попадали в excerpt. Перед действием,
зависящим от условий, нужно читать полные релевантные записи.

Число символов иллюстрирует объём чтения в этом примере. Экономия времени,
токенов и рост качества LLM на реальных задачах не измерены. Schema, retrieval,
сокращение выдачи, launcher/updater и карта не меняются. Реальная ручная уборка
выбранных пользователем карточек и гарантия полноты смысловых блоков остаются
отдельными этапами.
