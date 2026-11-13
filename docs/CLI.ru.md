# Команды Richi

[English](CLI.md) | Русский

Глобальная интеграция с Codex: `richi setup --dry-run`, `richi setup --yes`,
`richi setup --status`. Настройка действует на всю установку, не требует привязки
чата, отклоняет селекторы workspace/хранилища и сохраняет существующие инструкции.
Установка, флаги принятия существующих файлов и восстановление описаны в
[SETUP.md](SETUP.ru.md).

После установки используйте `richi` из любой рабочей папки. Команда
`richi config show` показывает выбранные настройки и runtime. Глобальные параметры базы
и конфигурации ставятся перед подкомандой:

```sh
richi --db /path/to/memory.sqlite3 recall 'локальный кеш'
richi --config /path/to/config.json check
```

<a id="справка"></a>
## Встроенная справка

`richi` без аргументов выводит краткую памятку с самыми частыми командами.
Полный справочник встроен в CLI и никогда не открывает хранилище знаний:

```sh
richi help                    # все команды, сгруппированные по назначению
richi help recall             # одна команда: описание, параметры, примеры
richi help sources capture    # подкоманда; то же, что richi sources capture --help
richi help topics             # темы, общие для многих команд
richi help filters            # грамматика --where
```

Темы: `richi help start` (первые шаги), `richi help records` (типы записей,
ссылки, состояния знаний, безопасное обновление), `richi help filters`,
`richi help output` (JSON, ошибки, коды завершения, бюджеты) и `richi help
environment` (переменные и расположение по умолчанию). Каждая страница команды
заканчивается примерами, связанными страницами и ссылкой на подробное
руководство, а если копия руководства установлена локально, то и путём к ней,
чтобы справку можно было читать без сети. Справка выводится на английском, как
и имена команд.

При опечатке в имени команды, параметра или темы, а также в значении из
фиксированного списка вариантов CLI предлагает исправление. Ошибка, которую
находит разбор аргументов (неизвестная команда или параметр, отсутствующее или
неверно записанное значение, параметры, которые строка usage разделяет знаком
`|`, например `[--target REF | --worktree]`, `--workspace` вместе с
`--config`, аргумент, без которого нельзя обойтись при запуске без терминала),
имеет `"code": "usage_error"` и называет в `"help"` страницу справки этой
команды:

```json
{"error": "'stauts' is not a richi command. Did you mean 'status'? See 'richi help'.", "code": "usage_error", "help": "richi help"}
```

Глобальный параметр, который команда не принимает, тоже даёт `usage_error`,
например `--project-path` с `init`, `--compact-json` с `map` или параметры
хранилища с `chat` или `agents`; тогда `"help"` называет страницу этой
команды. Любая другая ошибка сохраняет собственный `"code"` или не имеет его,
например ссылка, имя поля, дата или выражение `--where`, которые команда
проверяет сама, `--check-limit` без `--check-sources`, параметр хранилища,
который команда принимает, но отклоняет выбранная область (`--db` вместе с
областью, у которой собственное хранилище), или неверно записанный ID чата
(`"code": "invalid_chat_id"`).

В ещё не привязанном чате агента ошибка, которую runtime разбирает вне
справки, например опечатка в имени команды работы со знаниями, неизвестный
параметр или отсутствующее либо неверно записанное значение, выдаётся как
отсутствие привязки (`"code": "chat_workspace_required"`): пока чат не выбрал
область, лаунчер не может передать аргументы выбранному runtime, который их
разбирает. Некоторые ошибки лаунчер проверяет сам ещё до привязки и выдаёт как
`usage_error` и в этом случае: ошибки в его собственных командах (например,
`workspace`, `chat` и `config`), опечатку в имени команды лаунчера или `help`
и ошибки в глобальных параметрах перед командой (опечатка в имени,
отсутствующее значение, форма `-w`, `--db` и `--config`, `--workspace` вместе
с `--config`, `--project-path` с `map`) или в параметрах `--port`, `--db` и
`--config` команды `map`.

Справке не нужна привязка чата. В чате агента её выводит установленная версия,
поэтому новая справка из development checkout появляется там после
переустановки пакета. В терминале справку выводит выбранный runtime; если сам
выбор сломан, например неизвестной `RICHI_WORKSPACE`, её выводит установленная
версия, так что `richi help environment` по-прежнему объясняет переменные.
Страницы команд лаунчера (`agents`, `setup`, `update`, `status`, `workspace`,
`use`, `chat` и `config`) всегда выводит установленный лаунчер, который и
выполняет эти команды, поэтому их изменения появляются только после
переустановки пакета.

Для агента можно добавить глобальный `--compact-json` перед подкомандой:
`richi --compact-json recall 'локальный кеш'`. Успешный JSON в stdout выводится
без отступов; формат по умолчанию сохраняется. Бюджет `--max-chars` считается
по выбранному формату, поэтому в тесный лимит могут поместиться дополнительные
данные. Ошибки, help, файлы `--output` и карта не меняются; сочетание с `map`
отклоняется. Контракт и измерения — в [COMPACT_OUTPUT.md](COMPACT_OUTPUT.ru.md).

Режим выбирается в настройках текущего workspace:

```sh
richi config set development.source /path/to/Richi
richi config set dev true
richi config show
richi map
# Сервер в текущем терминале; Ctrl+C завершает его:
richi map serve --open
# Возврат к установленной версии:
richi config set dev false
```

В обычном режиме CLI использует фиксированный установленный пакет. В dev все
команды используют текущий код выбранного checkout, а карта перезагружает сервер
после изменения исходников. Работающая карта также подхватывает переключение
режима после проверки нового runtime. Для изменений интерфейса обновите страницу
браузера. База и история знаний общие; миграция SQLite автоматически не выполняется.
Команды `config show` и отключения dev доступны даже при неработающих исходниках.
`config set dev` также переводит управляемые skills агентов на checkout и обратно
([что переключает dev](INSTALLATION.ru.md#что-переключает-dev)).
Подробности установки, обновления и диагностики — в
[инструкции](INSTALLATION.ru.md).

Рабочая область выбирается перед подкомандой:

```sh
richi workspace list
richi workspace create work
richi -w work init
richi -w work project add /path/to/service-example
richi use work
richi use
richi -w work recall 'локальный кеш' --project service-example
```

`richi use` открывает меню только в обычном терминале. В новом чате агент
спрашивает область один раз, затем сохраняет выбор:

```sh
richi chat current
richi chat bind --current
# Или другая область только для чата:
richi chat bind work
richi project check /path/to/service-example --id service-example
richi --project-path /path/to/service-example recall 'локальный кеш' --project service-example
# Просмотр и последующий импорт папки репозиториев:
richi project scan /path/to/repositories
richi project scan /path/to/repositories --apply
```

Варианты `bind` взаимоисключающие: выполните только выбранный пользователем.
В привязанном чате `-w` может только повторять сохранённую область. Прямые
`--db`, `--config` и соответствующие переменные хранилища отклоняются.
Для другого клиента передавайте устойчивый ID через `--chat ID` или `RICHI_CHAT_ID`;
Codex предоставляет `CODEX_THREAD_ID`. [Workspace и изоляция знаний](WORKSPACES.ru.md).

Единая проверка контекста и путей:

```sh
richi status
richi --project-path /path/to/service-example status --project service-example
richi status --projects --limit 50
```

Отчёт только читает метаданные выбранного workspace. Код 0 означает сформированный
отчёт; проверяйте поле `status` и покрытие `diagnostics.projects`. Непривязанный
чат получает `selection_required` без доступа к базе. При неисправном runtime
возвращается `unavailable` с доступными настройками и причиной. Подробности и
границы проверки — в [руководстве workspaces](WORKSPACES.ru.md#состояние-workspace-и-путей-проектов).

Полные справочники находятся в `skills/project-memory/references`. Это единый
источник документации CLI и установленного skill: папка skill переносится целиком,
без ссылок на файлы вне неё.

| Задача | Справочник |
|---|---|
| Создать базу, зарегистрировать проект, сделать backup | [CLI](../skills/project-memory/references/CLI.ru.md) |
| Создать или полностью обновить запись, сохранить task-карточку | [Запись и защита от конфликтов](../skills/project-memory/references/ENTRY_WRITE.ru.md) |
| Найти прошлую работу и разобрать ограничения выдачи | [Recall](../skills/project-memory/references/RECALL.ru.md) |
| Выбрать кандидатов и пакетно прочитать полные записи | [Discover и fetch](../skills/project-memory/references/RETRIEVAL_TOOLS.ru.md) |
| Собрать контекст вопроса и изменения кода | [Brief](../skills/project-memory/references/BRIEF.ru.md) |
| Связать знания с версиями кода, открыть историю и diff | [Git-источники](../skills/project-memory/references/GIT.ru.md) |
| Захватить проверенные байты из checkout/worktree и прикрепить источник | [Capture и attach](../skills/project-memory/references/GIT_CAPTURE.ru.md) |
| Найти знания по изменённым файлам | [Impact](../skills/project-memory/references/IMPACT.ru.md) |
| Создать сущности и связи | [Граф](../skills/project-memory/references/GRAPH.ru.md) |
| Прочитать соседей известной записи | [Соседи и границы выдачи](../skills/project-memory/references/NEIGHBORS.ru.md) |
| Описать термины с разными значениями и версиями | [Энциклопедия](../skills/project-memory/references/ENCYCLOPEDIA.ru.md) |
| Найти инструменты, настройки и задачи с их применением | [Инструменты](../skills/project-memory/references/TOOLS.ru.md) |
| Разобрать пропущенный известный результат | [Диагностика recall](../skills/project-memory/references/RECALL_DEBUG.ru.md) |
| Проверить состояние workspace, кандидатов или группу источников | [Диагностика](../skills/project-memory/references/DIAGNOSTICS.ru.md) |
| Оценить дубли и необходимость объединения без изменений | [Ограниченный аудит](../skills/project-memory/references/MEMORY_AUDIT.ru.md) |
| Вручную объединить знания и разобрать кандидатов punisher | [Обслуживание](../skills/project-memory/references/MAINTENANCE.ru.md) |
| Проверить изменение поиска по воспроизводимым примерам | [Оценка качества](../skills/project-memory/references/EVALUATION.ru.md) |
| Измерить рост хранения и обслужить поисковый индекс | [Storage](STORAGE.ru.md) |

Примеры содержат вымышленные проекты `service-example`, `service-consumer`,
задачу `ABC-123` и ссылки `example.com`. Заменяйте их проверенными данными
своего проекта. Даты, SHA и пути в примерах не являются доказательствами.
