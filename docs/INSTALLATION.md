# Установка и конфигурация

## Установка из исходников

Нужны macOS или Linux и Python 3.9+ с модулем `sqlite3`. Для Git-источников нужен
Git в PATH. Runtime использует стандартную библиотеку Python. Поддержка Windows
в этой поставке не заявлена.

```sh
git clone https://github.com/Hidanio/Richi.git
cd Richi
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
richi config show
```

Установка `-e` связывает команду с этим checkout: изменения кода доступны без
повторной установки. Не удаляйте и не перемещайте его, пока используете эту
установку. В новом терминале снова активируйте окружение или вызывайте
`/path/to/Richi/.venv/bin/richi`. Для агента без активации оболочки настройте PATH
с каталогом `.venv/bin` либо укажите абсолютный путь к этому исполняемому файлу
в его локальных инструкциях. Личный путь не нужно включать в переносимый skill.

## Выбор хранилища

`richi config show` показывает итоговую конфигурацию; само чтение настроек не
создаёт базу. Для новой установки выполните `richi init`, затем `richi check`.
Для существующей базы укажите её путь и выполните `check` до записи данных.

Настройки можно передать JSON-файлом:

```json
{
  "database": "/path/to/existing/memory.sqlite3",
  "data_dir": "/path/to/richi-data",
  "port": 8765
}
```

Это синтетические абсолютные пути. При подключении существующей памяти
`database` должен указывать на фактическую базу. Если поле `database` задано,
связанные Git-снимки, runtime и резервные копии остаются рядом с этой базой.
`data_dir` определяет размещение базы по умолчанию, когда отдельный путь базы
не задан. Конфигурация не перемещает файлы.

```sh
richi --config /path/to/config.json config show
richi --config /path/to/config.json check
```

Параметры выбираются по приоритету: явный аргумент CLI, переменная окружения,
JSON-конфигурация, платформенное значение по умолчанию. Для каталога данных
используйте `RICHI_DATA_DIR`, для базы — `RICHI_DB`, для порта — `RICHI_PORT`.
`RICHI_CONFIG` выбирает JSON-файл; `--config PATH` имеет приоритет над ним.
`--db PATH` ставится перед подкомандой; `--port` применяется к командам карты.

```sh
export RICHI_CONFIG=/path/to/config.json
richi config show
richi project list
# Разовая работа с другой базой без изменения конфигурации:
richi --db /path/to/another.sqlite3 check
```

Платформенные значения по умолчанию:

| Система | Конфигурация | Каталог данных |
|---|---|---|
| macOS | `~/Library/Application Support/Richi/config.json` | `~/Library/Application Support/Richi/` |
| Linux | `${XDG_CONFIG_HOME:-~/.config}/richi/config.json` | `${XDG_DATA_HOME:-~/.local/share}/richi/` |

При отсутствии явного `database` файл называется `memory.sqlite3` внутри
выбранного каталога данных. Относительные пути внутри JSON разрешаются от
каталога этого JSON-файла; относительные пути CLI и переменных окружения — от
текущей рабочей папки. Для общей памяти из разных папок удобнее абсолютные пути.
Знак `~` в путях поддерживается. Явно выбранный несуществующий или некорректный
конфиг приводит к ошибке, а не к незаметному выбору другой базы.

По умолчанию Richi выбирает пользовательский каталог данных операционной
системы, отдельно от checkout. Проверяйте фактические пути через `config show`,
особенно когда терминал, IDE и агент имеют разные переменные окружения.
SQLite, `artifacts/git/`, `backups/` и `runtime/` остаются локальными.
Не помещайте их в Git. Изменение пути конфигурации не копирует данные и не
обновляет зарегистрированные пути проектов автоматически.

## Карта

```sh
richi map
# Сервер в текущем терминале; Ctrl+C завершает его:
richi map serve --open
richi map serve --port 8766 --open
```

`richi map` запускает или переиспользует совместимый сервер выбранной базы
и открывает браузер. Карта доступна только на loopback, показывает граф,
определения, источники и Git-навигацию. Запись знаний выполняется через CLI.
Карта не импортирует разговоры и не выполняет процедуры из записей.

## Подключение агента

Скопируйте всю папку `skills/project-memory` в каталог skills своего агента.
Для Codex это обычно `${CODEX_HOME:-$HOME/.codex}/skills/project-memory`.
Если skill уже установлен, сначала сохраните его отдельную копию и проверьте,
нет ли в нём локальных дополнений. Все ссылки внутри переносимого skill
ведут в его собственную папку `references/`.

Убедитесь, что процесс агента видит исполняемый файл `richi` и выбранную
конфигурацию. Проверьте из его среды `richi config show` и `richi check`.
Настройка в терминале не гарантирует те же переменные в IDE или desktop-приложении.

Пример инструкции проекта для явно разрешённых локальных обновлений:

```markdown
Use the project-memory skill to recall prior engineering work and save reusable
findings, decisions, failed experiments, and task outcomes. Use the configured
Richi store; inspect `richi config show` if its location is unclear. Local memory
updates are authorized during this project's work. Search before creating a new
record, preserve sources and scope, and use expected_updated_at for updates.
Code, task trackers, and pull requests remain authoritative for current state.
This authorization does not permit external messages or unrelated changes.
```

Эта инструкция — образец выбора режима владельцем проекта. Сам skill не
предполагает разрешения записывать произвольные базы или менять внешние системы.
MCP не обязателен: агент вызывает установленный CLI через инструменты запуска
процессов. GUI-меню CLI и отдельный MCP можно добавить позднее.
