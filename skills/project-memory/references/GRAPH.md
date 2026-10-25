# Knowledge graph: schemas 2 and 3

Граф дополняет существующую память. Проекты и записи остаются в своих таблицах, а `graph_nodes` хранит только ссылки на них. Новая сущность нужна, если объекту — PR, метрике, компоненту, понятию, документу или Jira-задаче — требуется отдельная карточка. Если задача уже сохранена как entry, используйте её узел; дублировать её сущностью `jira` не требуется.

Ссылки на узлы имеют строгий вид:

| Узел | Ссылка |
|---|---|
| Проект `service-example` | `project:service-example` |
| Запись `task:ABC-123` | `entry:task:ABC-123` |
| Сущность `pr:example-42` | `entity:pr:example-42` |

Идентификатор после первого двоеточия сохраняется целиком. Содержание проекта или записи не копируется в граф. INSERT-триггеры создают ссылки для новых записей, включая записи, добавляемые прежними командами CLI. Внешние ключи не позволяют записать ребро с отсутствующим узлом.

## Переход с версии 1

После установки Richi, из любой рабочей папки:

```sh
richi backup
richi migrate
richi check
```

`migrate` explicitly and atomically upgrades 1→2→3 or 2→3: it validates the original store, adds the required tables/indexes/triggers, creates graph references for existing projects and entries, then updates `user_version`. Existing tables, records, history and FTS remain. Errors roll back the transaction. Unknown versions are rejected; repeating migration on schema 3 returns `existing`.

Legacy read/write commands support schemas 1, 2 and 3. Graph operations require at least schema 2 and never migrate automatically. `init` creates schema 3; existing schema 1/2 stores require `migrate`. Back up before migration. Migration holds a SQLite write lock, so concurrent writers wait within their configured timeout. Tracks and typed research require schema 3; deployment: [TRACKS_DEPLOYMENT.md](TRACKS_DEPLOYMENT.md).

`schema.sql` defines schema 1; `graph_schema.sql` extends the graph; `research_schema.sql` adds typed roles, revisions and events. `init` and `migrate` apply the extensions in one transaction. Do not run these SQL files directly against a live store.

## Сущности

```sh
richi entity put --json - <<'JSON'
{
  "id": "pr:example-42",
  "kind": "pull_request",
  "title": "PR 42: кеш повторяющихся запросов",
  "summary": "Изменение ожидает ревью; это ещё не означает выпуск в production.",
  "sources": [{"reference": "https://example.com/repo/pull/42", "type": "pull_request"}],
  "knowledge_state": "confirmed",
  "verified_at": "2026-09-30",
  "tags": ["cache"],
  "aliases": ["кеширование", "кэширование"]
}
JSON

richi entity get pr:example-42
```

Обязательны `id`, `kind`, `title`, `summary`. Допустимые kind: `pull_request`, `metric`, `component`, `concept`, `document`, `jira`. Остальные поля: `sources`, `knowledge_state` (по умолчанию `hypothesis`), `verified_at`, `tags`, `aliases`, `expected_updated_at`.

Источники имеют тот же формат, что у entries: массив объектов с обязательным `reference` и необязательными `label`, `type`, `observed_at`. Для любой новой сущности `confirmed` обязателен хотя бы один источник. Это проверка наличия ссылки, а не автоматическая верификация утверждений.

## Рёбра

Понятия `kind: concept` также показаны в разделе «Энциклопедия». Их прямую
область проекта задаёт подтверждённое ребро `used_in` от понятия к проекту
с источником. Оно не выводится из связности графа. Формат определений,
одноимённые значения и версии описаны в [ENCYCLOPEDIA.md](ENCYCLOPEDIA.md).

```sh
richi edge put --json - <<'JSON'
{
  "id": "pr42-implements-abc123",
  "from_ref": "entity:pr:example-42",
  "to_ref": "entry:task:ABC-123",
  "kind": "implements",
  "description": "PR содержит реализацию задачи; состояние выпуска проверяется отдельно.",
  "sources": [{"reference": "https://example.com/repo/pull/42"}],
  "knowledge_state": "confirmed",
  "verified_at": "2026-09-30"
}
JSON

richi edge get pr42-implements-abc123
```

Example objects and links are synthetic; create endpoints first. Required fields: `id`, `from_ref`, `to_ref`, `kind`. `kind` remains a free-form string such as `implements`, `measures`, `depends_on`, `documents` or `supersedes`; schema 3 also validates endpoints for typed research relationships from [TRACKS.md](TRACKS.md). Optional fields: `description`, `sources`, `knowledge_state`, `verified_at`, `expected_updated_at`, `expected_revision`. Confirmed edges require a source. Direction conveys meaning; neighbor reads inspect both directions.

In schema 3, `get` for a typed relationship or linked business-goal document
also returns read-only `revision`. Ordinary legacy objects keep their previous
response shape and timestamp CAS. Remove `revision` from the payload and pass
it as `expected_revision` when changing a typed relationship.
Creation may assert 0. This complements timestamp CAS and protects edits with
identical timestamps. Track operations, roles and exact scope are documented in [TRACKS.md](TRACKS.md).
Legacy free-form edge names do not become research assertions merely because
migration introduces a matching typed name. Native endpoints opt into the new
contract; promoting a legacy card validates its incident edges to avoid
creating invalid typed assertions.

`entity put` и `edge put` принимают одиночный объект либо массив и полностью заменяют поля по ID, как `entry put`. Пакет атомарен. `get` возвращает текущую полную карточку; `get ID --history` дополнительно возвращает неизменяемую историю `before`/`after`. При обновлении передавайте `expected_updated_at` из последнего чтения: устаревшее время отклоняет изменение. `null` означает ожидание отсутствующей карточки. Одинаковая нормализованная запись даёт `unchanged` без изменения времени и истории. Новые entity/edge команды не принимают `event_id`; идемпотентность повтора обеспечивается содержимым, а защита от устаревшего обновления — `expected_updated_at`.

Существующие `entry_projects` и `relations` отображаются автоматически. Связь записи с проектом называется `belongs_to_project`: это принадлежность записи, а не утверждение о техническом или бизнес-влиянии. Такие рёбра редактируются через `entry put project_ids`; связи проектов — через `relation put`. API отклоняет ручной дубль уже существующей производной связи. Если производная связь была создана позже ручной, экспорт сохраняет обе записи с разными `id` и `origin`: они могут иметь разные источники и уверенность, поэтому скрывать одну по совпадению направления и kind нельзя.

## Задача и подробные записи

Стабильная task-запись содержит актуальное состояние. Самостоятельные разборы,
эксперименты и основания решений используют существующие entry kinds
`investigation`, `experiment`, `decision`; отдельной сущности для расследования
не нужно. Поиск индексирует их текущий текст, а не старые ревизии родителя.
Каждая запись сохраняет собственные источники, knowledge/work state, реальные
`project_ids` и фактический `jira_key`, если это часть Jira-задачи.

Предпочтительное направление связи — подробная запись → задача:

```json
{
  "id": "pooling-experiment-part-of-abc123",
  "from_ref": "entry:experiment:pooling-abc123",
  "to_ref": "entry:task:ABC-123",
  "kind": "part_of",
  "description": "Эксперимент проверял вариант решения задачи ABC-123; отказ и условия сохранены в его карточке.",
  "sources": [{"reference": "https://example.com/reports/abc123-pooling", "type": "report"}],
  "knowledge_state": "confirmed",
  "expected_updated_at": null
}
```

Это синтетический пример payload для `edge put`; оба endpoint должны уже
существовать. Новый ID с `expected_updated_at: null` создаётся только при
отсутствии записи. На повторе сначала прочитайте и проверьте существующий
объект, переиспользуйте его или внесите обоснованную правку с его текущим CAS.
Для обновления не отправляйте старый payload поверх параллельной правки.

`part_of` утверждает принадлежность материала задаче, а не успешность подхода.
Неудачный результат может быть confirmed при подтверждённых условиях;
непроверенный подход остаётся hypothesis. `supports`/`informs` подходят лишь
при источнике, подтверждающем соответствующий смысл. Не создавайте обратное
ребро только для навигации: `neighbors` читает оба направления. Membership
по-прежнему задаётся `project_ids`; графовое соседство не расширяет строгий
scope `punisher --project`.

До сокращения родителя создайте и проверьте все нужные подробные записи,
рёбра и доступность через исходные запросы. Последним обновите родителя с
его исходным `expected_updated_at`. Несколько entry/edge put не образуют
общую транзакцию; CAS-конфликт требует перечитать и согласовать новые сведения.
Подробный порядок и сохранение provenance — в [MAINTENANCE.md](MAINTENANCE.md).

## Экспорт графа

Для одного известного узла используйте [NEIGHBORS.md](NEIGHBORS.md).

```sh
richi graph export
richi graph export --project service-example
richi graph export --include-hypotheses
```

По умолчанию экспорт включает только `confirmed` записи, сущности и рёбра. Проекты отображаются как подтверждённые элементы локального каталога; это не дополнительная проверка их описания. `--include-hypotheses` добавляет гипотезы; `--include-superseded` добавляет устаревшие знания. Флаги независимы. Если endpoint исключён фильтром, его рёбра тоже скрываются.

Без project-фильтра экспорт охватывает все доступные узлы, начиная с проектов. `--project ID` показывает компонент связности выбранного проекта: ближайшие узлы добавляются раньше дальних. Это может охватить несколько связанных проектов.

Каждый экспорт читает один согласованный снимок в транзакции. Максимум — 2000 узлов и 10000 рёбер. При достижении лимита выставляются `truncated` и соответствующие поля `limits`; рёбра с endpoint за пределами возвращённых nodes не выдаются.

Результат JSON:

```json
{
  "schema_version": 2,
  "generated_at": "2026-09-30T00:00:00Z",
  "nodes": [],
  "edges": [],
  "truncated": false,
  "limits": {
    "max_nodes": 2000,
    "max_edges": 10000,
    "nodes_truncated": false,
    "edges_truncated": false
  },
  "scope": {
    "root": null,
    "depth": null,
    "selection": "all",
    "knowledge_states": ["confirmed"]
  }
}
```

Узел содержит `id` (полную ссылку), `native_id`, `node_type` (`project`, `entry`, `entity`), `kind`, `title`, `summary`, `knowledge_state`, `work_state`, `project_ids`, `sources`, `tags`, `aliases`, `verified_at`. Entry/Entity также имеют время создания и обновления. Для сущностей без прямой канонической привязки `project_ids` пуст: связи доступны через edges, дополнительные привязки не выдумываются.

Ребро содержит `id`, `from_ref`, `to_ref`, `kind`, `description`, `knowledge_state`, `sources`, `verified_at`, `origin`, `provenance`. `origin` равен `explicit`, `project_relation` или `entry_project`; `provenance` указывает исходную таблицу и ID. Идентификаторы экспорта имеют префиксы `edge:`, `relation:`, `membership:` для исключения коллизий. В `edge get` передаётся исходный ID, без добавленного экспортом `edge:`.

Интерактивная карта — отдельное представление этих данных: она рисует nodes/edges и показывает источники при выборе элемента. Экспорт сам не открывает сервер и не изменяет знания. Визуальная близость и путь между узлами не доказывают причинность: смысл определяет kind ребра и его источник. Старый текстовый `search` продолжает искать entries; `recall` ищет и entries, и entities и выбирает ограниченное окружение по явным связям. Отдельный FTS-индекс для entities пока не добавлен. Подробнее — в [RECALL.md](RECALL.md).
