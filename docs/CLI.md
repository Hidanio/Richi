# Команды Richi

После установки используйте `richi` из любой рабочей папки. Команда
`richi config show` показывает выбранные настройки и runtime. Глобальные параметры базы
и конфигурации ставятся перед подкомандой:

```sh
richi --db /path/to/memory.sqlite3 recall 'локальный кеш'
richi --config /path/to/config.json check
richi --help
richi sources --help
```

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
Подробности установки, обновления и диагностики — в
[инструкции](INSTALLATION.md).

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

`richi use` открывает меню только в терминале; агенты передают `-w NAME`
в каждом вызове. [Workspace и изоляция знаний](WORKSPACES.md).

Полные справочники находятся в `skills/project-memory/references`. Это единый
источник документации CLI и установленного skill: папка skill переносится целиком,
без ссылок на файлы вне неё.

| Задача | Справочник |
|---|---|
| Создать базу, зарегистрировать проект, сохранить результат, сделать backup | [CLI и формат записей](../skills/project-memory/references/CLI.md) |
| Найти прошлую работу и разобрать ограничения выдачи | [Recall](../skills/project-memory/references/RECALL.md) |
| Собрать контекст вопроса и изменения кода | [Brief](../skills/project-memory/references/BRIEF.md) |
| Связать знания с версиями кода, открыть историю и diff | [Git-источники](../skills/project-memory/references/GIT.md) |
| Найти знания по изменённым файлам | [Impact](../skills/project-memory/references/IMPACT.md) |
| Создать сущности, связи и посмотреть соседей | [Граф](../skills/project-memory/references/GRAPH.md) |
| Описать термины с разными значениями и версиями | [Энциклопедия](../skills/project-memory/references/ENCYCLOPEDIA.md) |
| Разобрать пропущенный результат, проверить группу источников | [Диагностика](../skills/project-memory/references/DIAGNOSTICS.md) |
| Вручную объединить знания и разобрать кандидатов punisher | [Обслуживание](../skills/project-memory/references/MAINTENANCE.md) |
| Проверить изменение поиска по воспроизводимым примерам | [Оценка качества](../skills/project-memory/references/EVALUATION.md) |

Примеры содержат вымышленные проекты `service-example`, `service-consumer`,
задачу `ABC-123` и ссылки `example.com`. Заменяйте их проверенными данными
своего проекта. Даты, SHA и пути в примерах не являются доказательствами.
