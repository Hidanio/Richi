# Сценарии

[English](SCENARIOS.md) | Русский

Сценарий — это повторяемая многошаговая процедура, которую агент выполняет в
чате: типизированные поля ввода, универсальные этапы (`instruction`, `skill`,
`command`, `gate`, `checklist`, `memory.recall`, `memory.brief`, `condition`),
явные зависимости (`needs`), матрицы и проверки отчётов. Richi хранит
определение, состояние запуска и аренду этапов, создаёт для каждого запуска
запись `task` с тегом `scenario-run` и ведёт общий журнал правок и событий.
Richi сам не запускает команды и не поднимает агентов.

## Когда запускать сценарий

1. **Явная просьба**: пользователь просит запустить конкретный сценарий.
2. **Постоянное правило**: правило указывает использовать сценарий.
3. **Предложение**: если подходящий сценарий или встроенный шаблон описан в
   `when_to_use`, предложи его и спроси пользователя перед запуском. Молча
   сценарий не запускай.

## Поиск, предпросмотр и управление определениями

```sh
richi --compact-json scenario list
richi --compact-json scenario templates
richi --compact-json scenario show review --agent codex
richi --compact-json scenario plan review --input branch=feature_x --agent codex
```

`scenario list` показывает сценарии уровня workspace и проектов, а также
встроенные шаблоны. `scenario show` ищет сначала в проекте, затем в workspace,
затем среди встроенных шаблонов и возвращает `resolved_skills` и
`missing_skills` для активного агента. `scenario plan` показывает предпросмотр
без записи в базу.

Чтобы сделать редактируемую копию или изменить сохранённый сценарий (только по
просьбе пользователя):

```sh
richi --compact-json scenario copy review --project service-example --id service-review
richi --compact-json scenario apply --json change.json
```

`scenario apply` принимает один JSON-объект с `action` (`add`, `update` или
`remove`), `scope` (`global` или `project`), `definition` и
`expected_updated_at` для обновлений и удаления. Определения хранятся в
`metadata` и никогда не попадают в `recall` или `search`.

## Пошаговое выполнение запуска

Запуски требуют схему 4 (`richi migrate` предварительно делает резервную копию
базы).

```sh
richi --compact-json --project-path /path/to/service scenario run review --input branch=feature_x
richi --compact-json --project-path /path/to/service scenario next run-review-20261127-0001 --claim
richi --compact-json --project-path /path/to/service scenario report run-review-20261127-0001 angle.0 --json report.json
```

Повторяй цикл, пока `scenario next RUN --claim` возвращает этап для агента:

1. Прочитай подставленные `text` / `instruction` / `command`, выбранный `skill`,
   подсказки `hints` (например `subagent`), требования `outputs` и проверки
   `checks`.
2. Выполни этап. Для `command` проверь `substituted_values` и выполняй команду
   в рамках своей песочницы и подтверждений.
3. Отправь JSON-отчёт с `status` (`done` или `failed`), `summary` и нужными
   `data`, `files`, `exit_code` или `evidence`. Если Richi вернул
   `status: "rejected"`, исправь указанные `reasons` и отправь отчёт снова:
   отклонённый отчёт не закрывает этап.

Для параллельных этапов и явной передачи аренды (`claim` возвращает полный отрендеренный payload этапа и принимает `--secret`, а `next` принимает `--step`):

```sh
richi --compact-json scenario claim run-review-20261127-0001 angle.1
richi --compact-json scenario next run-review-20261127-0001 --step angle.1 --claim
richi --compact-json scenario release run-review-20261127-0001 angle.1
```

## Шлюзы, секреты и продолжение в другом чате

- **Шлюзы и чек-листы**: когда `scenario next` возвращает этап `gate` или
  `checklist` (`status: "waiting_human"`), спроси пользователя в этом чате и
  запиши ответ командой `scenario answer`:

```sh
richi --compact-json scenario answer run-review-20261127-0001 approve all --comment 'Принято'
```

- **Секретные поля**: значения `type: "secret"` никогда не сохраняются в
  SQLite, записях задач или журнале. Если `scenario next` вернул
  `missing_secret`, запроси значение у пользователя и передай
  `--secret KEY=VALUE` в `scenario next`.
- **Продолжение после сжатия контекста или в новом чате**: найди активный
  запуск и явно продолжи его:

```sh
richi --compact-json scenario runs --active --mine
richi --compact-json scenario status run-review-20261127-0001
```

## Управление запусками и общий журнал

```sh
richi --compact-json scenario skip run-review-20261127-0001 angle.1 --reason 'Тесты не менялись'
richi --compact-json scenario retry run-review-20261127-0001 merge
richi --compact-json scenario pause run-review-20261127-0001
richi --compact-json scenario resume run-review-20261127-0001
richi --compact-json scenario cancel run-review-20261127-0001
richi --compact-json scenario log --run run-review-20261127-0001 --entries 50
```

Пропуск этапа требует `--reason` и не блокирует следующие этапы по порядку
`needs`, если их `if` не проверяет статус пропущенного этапа. `scenario log`
показывает и правки определений, и события запусков от новых к старым. Для
каждого запуска создаётся запись `task` с тегом `scenario-run`, обновляемая на
ключевых переходах (`in_progress`, `blocked`, `done`).
