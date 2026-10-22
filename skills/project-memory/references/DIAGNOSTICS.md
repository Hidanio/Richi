# Диагностика поиска, знаний и источников

Все команды запускаются явно, работают локально и не вызывают модели или сеть.
SQLite остаётся каноническим хранилищем. Схема 2 не меняется; для записи знаний
продолжают использоваться `entry/entity/edge put` с историей и CAS.
Команды чтения открывают базу через `mode=ro` и `query_only=ON`.

Примеры ниже используют установленную команду `richi`. ID демонстрационные;
реальные проекты можно получить через `project list`.

## Почему запись не найдена

Порядок проверки известного пропуска и семантика `--expect`/`--explain`:
[RECALL_DEBUG.md](RECALL_DEBUG.md). Ниже — дополнительные счётчики реализации.

### Счётчики предварительного отбора entries

При наличии FTS прямой отбор проходит курсором по метаданным допустимых
по состоянию entries (`id`, `jira_key`, `aliases`). Полный payload читается
по первичному ключу для записи из FTS shortlist или совпадения прежних
ID/Jira/alias-правил. Unicode-нормализация этих правил сохранена; обход
метаданных остаётся линейным. Без FTS сохраняется полный lexical fallback.
Entities проходят прежний полный обход и учитываются в `entities_scanned`.

В полной `explain.counts` различаются:

| Поле | Что считается |
|---|---|
| `entries_scanned` | Посещённые entries после state-фильтра: метаданные при FTS, полные записи без FTS. Это по-прежнему может быть N |
| `entries_scored` | Полные записи, обработанные lexical scoring прямого поиска, включая отклонённые последующими guards |
| `entries_prefiltered` | Entries, исключённые предварительным отбором, включая исключённую ожидаемую запись |
| `expected_entry_probes` | Отдельные полные чтения и lexical-проверки исключённой entry из `--expect`; максимум одно |

`entries_scored + entries_prefiltered = entries_scanned`. Диагностическая
проверка ожидаемой записи не входит в `entries_scored`: она уже учтена среди
`entries_prefiltered`. Это не число результатов, физических чтений SQLite
или всех SQL-строк: графовое дополнение, проверка состояния ожидаемого узла
и работа FTS имеют отдельные пути чтения. Меньшее `entries_scored` не означает,
что исчез O(N) или что число физических чтений стало постоянным.

Для прочих исключённых записей `direct_rejected` содержит `routing_prefilter`:
их полный lexical guard не выполнялся, поэтому причина не угадывается.
Если исключённая entry явно задана через `--expect`, одно дополнительное
чтение по ID позволяет показать фактически проверенный `weak_lexical`,
`subject_guard`, `short_subject_guard`, `jira_guard` либо `fts_not_shortlisted`
относительно уже выполненного FTS shortlist. Такая проверка не добавляет
запись в кандидаты и не меняет ранжирование; возможное попадание через граф
показывается отдельно. Для отсутствующей или отфильтрованной по состоянию
цели дополнительной полной проверки нет.

Новые диагностические поля расходуют общий `--max-chars`. При тесном бюджете
они могут быть опущены с `details_omitted_for_budget`, либо может измениться
число показанных карточек. Отсутствующий счётчик не означает ноль. Сравнивайте
обычную выдачу отдельно от `--explain`/`--expect`; сохранение её поведения
не обещает побайтно прежний диагностический JSON при том же бюджете.

## Кандидаты для ручного обслуживания

Команда диагностики — `punisher`.

```sh
richi punisher --project service-example --limit 20 --max-chars 16000
richi --project-path /actual/checkout punisher --project service-example \
  --summary-warn-chars 6000
richi punisher --stale-days 180 --as-of 2026-10-01
```

`health` содержит состояние SQLite и счётчики, `findings` — короткие кандидаты
для просмотра. Проверяются отсутствие источников/дат, давность проверки,
изолированные сущности, точные повторы текста, общие aliases и длина summary. Гипотеза,
одинаковый alias или старая дата сами по себе не означают ошибку. Даже точный
повтор текста может относиться к разным задачам или контрактам.

`--project` задаёт строгую область: memberships записей и прямые подтверждённые
`used_in` с источниками от entity к проекту. Это отличается от повышения
приоритета в recall и связной компоненты `graph export --project`.
Проверка целостности SQLite относится ко всей базе, а смысловые кандидаты —
к выбранной области. `--as-of` фиксирует дату для воспроизводимого отчёта.

Проверяйте `coverage`, `truncated` и `budget`: ограниченный отчёт не доказывает
отсутствие остальных кандидатов. `candidate_count` и `finding_counts` учитывают
все находки внутри фактически проверенной области до ограничения выдачи;
`budget.omitted_findings` учитывает находки, не попавшие в вывод из-за `--limit`
или `--max-chars`. При сокращении самих счётчиков есть `finding_counts_omitted`.
Текущее число кандидатов не расширяет scan caps: до 5000 записей на таблицу
и 10000 явных рёбер. Неполное сканирование отмечено в `coverage.complete`;
это отдельное ограничение от числа показанных findings. Сортировка стабильна:
уровень (`error`, `review`, `info`), код, refs. Ради бюджета finding
может быть опущен целиком; идентификаторы не обрезаются.

Punisher ничего не объединяет, не удаляет, не изменяет записи, историю,
связи, состояния, даты проверки, FTS или schema version. Он поддерживает
схемы 1 и 2, не читает entry_history, файлы источников, Git или сеть.
Данные другого workspace не используются. Решения по кандидатам — в [MEMORY_AUDIT.md](MEMORY_AUDIT.md);
процедура изменений — в [MAINTENANCE.md](MAINTENANCE.md).

### Длинная summary

`--summary-warn-chars` — целое число от 1 до 1000000 включительно,
по умолчанию 6000. Неверное значение отклоняется; внутренний Python API
также отклоняет `bool`. Это начальный настраиваемый ориентир удобства чтения,
не измеренная граница качества и не лимит записи.

Проверяются только summary уже выбранных entries и scoped entities. Длина —
`len(summary)` сохранённой Unicode-строки без нормализации или `strip`;
это символы Python, не байты, токены или визуальные графемы. При длине,
равной порогу, finding отсутствует; при строго большей длине появляется
ровно один `long_summary` на запись. Для `confirmed` уровень `review`, для
`hypothesis` и `superseded` — `info`. Ни одно значение не означает ошибку
данных или необходимость удалить/разделить запись. Оно не меняет `health`:
состояние целостности SQLite и кандидаты для редакторской проверки различаются.

Применённый порог всегда возвращается в metadata отчёта как
`summary_warn_chars`. Пример одного finding для демонстрационной задачи:

```json
{
  "code": "long_summary",
  "level": "review",
  "reason": "Read the full record and review whether current state and details would benefit from separation. Length alone does not establish mixed history, outdated knowledge, or reduced quality.",
  "refs": ["entry:task:EXAMPLE-42"],
  "details": {
    "summary_chars": 6001,
    "threshold_chars": 6000,
    "kind": "task",
    "knowledge_state": "confirmed",
    "read_arguments": ["entry", "get", "task:EXAMPLE-42"]
  }
}
```

Для entity `read_arguments` равен `["entity", "get", "<полный ID>"]`.
Если ID начинается с дефиса, перед ним добавляется отдельный аргумент `--`,
чтобы CLI прочитал его как ID, а не опцию; сам ID сохраняется без изменений.
Это точные аргументы после `richi`, а не shell-строка: не склеивайте
неэкранированный ID в исполняемую команду. Сам длинный summary в finding
не включается. У показанного finding сохраняются все обязательные details,
даже при малом бюджете; если он не помещается целиком, увеличивается
`omitted_findings`. Отсутствие `long_summary` в усечённом отчёте не доказывает,
что длинных карточек нет. Сначала прочитайте полный `get`, затем решите,
полезно ли отделить текущее состояние от подробностей.

## Снимок проверяемой группы и повторная проверка

```sh
richi sources snapshot \
  --ref entry:experiment:pooling --ref entity:connection-pool \
  --output work/pooling-review-01.json

richi sources snapshot --project service-example \
  --output work/service-example-review-01.json

richi sources check --manifest work/pooling-review-01.json \
  --max-chars 16000
```

Снимок создаёт отдельный JSON manifest, не изменяя знания. Он фиксирует полное
содержание выбранных карточек, их проектные привязки, состав и ревизии
инцидентных явных рёбер, источники карточек/рёбер и хеши доступных локальных
файлов. Все состояния знаний учитываются. Проектный scope строгий; соседние
карточки не включаются автоматически через связанность графа.

Повторная проверка обнаруживает изменение карточки, появление/исчезновение
карточек в выбранном проекте, изменения связей и дрейф локальных файлов.
Появление нового ребра определяется даже при неизменном `updated_at` узла.
Это важно при подготовке плана объединения в базе, которую пополняют другие задачи.

Снимок не является backup: он не содержит все данные для восстановления.
Он также не является блокировкой. Перед записью всё ещё нужны полное чтение
актуальной карточки и `expected_updated_at`. Изменения после проверки возможны;
несколько команд записи не становятся общей транзакцией благодаря manifest.

Существующий manifest никогда не перезаписывается. Для новой базы сравнения
нужен новый путь. Подробности неподдерживаемых/недоступных источников и
ограничений указываются в JSON. URL не загружаются; ссылки на Jira, commit и
разговоры остаются указателями, которые при необходимости проверяет агент.
Обычные локальные файлы читаются с ограничениями по размеру и суммарному объёму;
устройства, FIFO и другие специальные файлы не хешируются.

Лимиты по умолчанию: 200 выбранных узлов, 1000 инцидентных рёбер и 1000 уникальных
ссылок; файл до 8 MiB, суммарно до 64 MiB за запуск, manifest до 4 MiB.
Превышение размера выбранного графа требует более узкой группы; пропуски
файлов отражаются как недоступность. `sources check` выдаёт общий `status`,
`has_changes`, счётчики и покрытие даже при сокращении подробностей по бюджету.
`unsupported` означает, что оставшиеся URL не проверялись, а не что они сломаны.
В этой версии отслеживаются рёбра `graph_edges`; старые связи сервисов из
таблицы `relations` отдельно не снимаются, что явно указано в `coverage`.
Manifest привязан к пути базы и отказывается сравниваться с другой базой.

Неизменный SHA-256 означает совпадение байтов с manifest. Он не подтверждает
содержание утверждения, актуальное состояние сервиса или production-релиз.
Недоступный источник означает пробел проверки, а не автоматическое опровержение
исторического знания. Никакая из этих команд не продвигает `verified_at`.

Для Git-источников manifest также сохраняет объект `source.git`, но сравнение
локального файла по `reference` остаётся сравнением текущих байтов. Сохранённый
коммит или снимок рабочей копии проверяется отдельной командой
`sources git-check`. Навигация по коду и обратный поиск описаны в [GIT.md](GIT.md).
Punisher проверяет структуру Git-метаданных и выдаёт подсказку проверки для
соответствующей карточки, не читая репозиторий автоматически.

## Точные источники в записях

У `sources` для entries, entities и рёбер появились необязательные поля:

```json
{
  "reference": "/absolute/repo/module.py",
  "type": "code",
  "label": "Контракт поля в проверенной версии",
  "observed_at": "2026-10-01",
  "revision": "git:<actual-commit-sha>",
  "locator": "FunctionName, L42-L58",
  "sha256": "<actual-64-hex-whole-file-sha256>"
}
```

Значения в угловых скобках — placeholders; не копируйте их как доказательства.
`revision` и `locator` — непустые строки до 1024 символов без управляющих
символов. `sha256` — 64 шестнадцатеричных символа, нормализуемых к нижнему
регистру; это хеш всего файла, не строки или выдержки. Заполнять поля нужно
по фактически проверенным источникам. Они сохраняются в исходном JSON и истории;
модуль не исполняет locator и не делает checkout указанной ревизии.

Прежние записи без новых полей остаются валидными. Миграция схемы не нужна.

## Workspace context and project paths

Use `richi status` for chat/CLI selection, workspace storage, runtime and cwd membership.
Use `richi --project-path PATH status --project ID` for an expected repository.
Only the selected workspace is read; the same path in another store cannot satisfy the check.
Before chat binding, the report is `selection_required` and reads no runtime settings or database.
Broken selected runtime produces `unavailable`; no installed-runtime fallback occurs.

`richi status --projects --limit 50` also reports registered path problems. Output limit is
1–200; problems precede healthy paths. Check `total`, `checked`, `omitted`, `counts` and
`status` before claiming coverage. The audit has a 10-second/10000-registration budget
and a 30-second worker timeout. A remote-only project with no path is informational.
Missing, inaccessible, duplicate or invalid Git paths require review; no paths are changed.
Current-path matching can inspect other registered paths even without `--projects`.

Top-level status is `ok`, `attention`, `selection_required` or `unavailable`. Exit 0 means
a report was emitted, not a health assertion. Argument, selector and configuration errors
exit 1. `status` reads projects/schema metadata only; it does not replace knowledge `check`
or requested `punisher` maintenance. Continue using `--project-path` on knowledge operations
to recheck membership in their transaction.
