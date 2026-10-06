# Установка и конфигурация

## Обычная установка

Нужны macOS или Linux и Python 3.9+ с модулем `sqlite3`. Для Git-источников нужен
Git в PATH. Runtime использует стандартную библиотеку Python. Поддержка Windows
в этой поставке не заявлена.

```sh
git clone https://github.com/Hidanio/Richi.git
cd Richi
python3 -m venv "$HOME/.local/share/richi/venv"
"$HOME/.local/share/richi/venv/bin/python" -m pip install --upgrade pip
"$HOME/.local/share/richi/venv/bin/python" -m pip install .
mkdir -p "$HOME/.local/bin"
ln -s "$HOME/.local/share/richi/venv/bin/richi" "$HOME/.local/bin/richi"
export PATH="$HOME/.local/bin:$PATH"
richi config show
```

Команда устанавливает фиксированную копию пакета в отдельное окружение Python.
Обычный режим работает из этой копии: правки, удаление или переключение веток
исходного checkout не меняют установленную версию. Установка не требует Codex.
Обновление pip обеспечивает поддержку современной упаковки `pyproject.toml`,
в том числе в новых окружениях старых поставок Python.

`ln -s` выше создаёт новую команду: если `~/.local/bin/richi` уже существует,
проверьте его назначение и замените только прежнюю ссылку Richi. Для новых
терминалов добавьте `~/.local/bin` в PATH своей оболочки. Активация окружения после
этого не нужна. Если агент не наследует PATH оболочки, настройте этот каталог
или укажите полный путь к команде в локальных инструкциях. Личный путь не нужно
включать в переносимый skill.

Для запуска через модуль используйте Python установленного окружения и
изолированный режим, исключающий влияние текущей папки и `PYTHONPATH`:

```sh
"$HOME/.local/share/richi/venv/bin/python" -I -m richi config show
```

Для обновлений из опубликованных стабильных GitHub Releases используйте:

```sh
richi update check
richi update apply
# Возврат к предыдущей установленной версии:
richi update rollback
```

Сохраняются текущая и предыдущая версии; работающие процессы удерживают свой
код до завершения. После обновления перезапустите карту. Dev-настройки и базы
workspace остаются прежними. Подробнее о проверках, очистке и первоначальном
переходе со старой установки — в [UPDATES.md](UPDATES.md).

## Режим разработки

Установка остаётся обычной. Для работы непосредственно с исходниками выберите
корень checkout, содержащий `src/richi/`, и включите dev в конфигурации:

```sh
richi config set development.source /path/to/Richi
richi config set dev true
richi config show
richi map
```

JSON-настройки этого режима:

```json
{
  "dev": true,
  "development": {"source": "/path/to/Richi"}
}
```

По умолчанию `dev` равен `false`. Настройка действует на все команды, агентов
и карту, использующих этот конфиг выбранного workspace. Каждый вызов CLI в dev использует текущие
исходники; правки и переключение веток checkout сразу влияют на дальнейшую
работу. Карта автоматически перезагружает сервер при изменении Python- и
SQL-файлов. Для изменений интерфейса обновите страницу браузера.

```sh
richi config set dev false
richi config show
```

Эта команда возвращает установленную версию. База, Git-снимки, история и остальные
настройки остаются прежними; два режима не создают две копии знаний. Команды
`config show` и `config set dev false` обслуживает установленный запускатель,
поэтому они доступны и при удалённых или неработающих dev-исходниках.
Новые CLI-вызовы при сломанном dev сообщают ошибку и не переключаются молча
на установленную версию.

Обычные изменения в `src/richi/` не требуют переустановки. Изменения установленного
запускателя в `launcher/` требуют пересборки и установки пакета.
Для разработки самого запускателя используйте отдельное тестовое окружение;
управляемую релизную установку обновляйте через `richi update apply`. Dev использует тот же Python и установленные зависимости; сейчас runtime
использует стандартную библиотеку, а при добавлении зависимостей их нужно
установить в это окружение.

Команда `config set` меняет настройки и не открывает базу. Совместимость схемы
проверяют выполняемая CLI-операция и карта перед заменой runtime; автоматических
миграций нет. Для экспериментов со схемой используйте отдельную копию базы
и связанного каталога `artifacts/git/`.

## Workspaces

Для независимых наборов связанных проектов используйте
[рабочие области](WORKSPACES.md). `richi use NAME` быстро меняет область по
умолчанию, `richi use` открывает меню, а `richi -w NAME ...` закрепляет область
для конкретной команды. Текущая база остаётся в `default`; новые области
получают отдельные базы и каталоги Git-снимков. Dev-настройки принадлежат
выбранной области и копируются при её создании.

Настройки текущего CLI служат предложением для нового чата. После ответа
пользователя агент сохраняет выбор через `richi chat bind --current` или
`richi chat bind NAME`. Чат продолжает использовать свою область после
переключения CLI. ID Codex определяется автоматически; другие клиенты передают
устойчивый `RICHI_CHAT_ID` либо `--chat ID`. Подробности — в [руководстве](WORKSPACES.md).

## Выбор хранилища

Приведённые ниже прямые настройки базы предназначены для обычного терминала.
Обнаруженный чат использует зарегистрированную область и отклоняет прямые
переопределения `--config`, `--db`, `RICHI_CONFIG`, `RICHI_DB`, `RICHI_DATA_DIR`.

`richi config show` показывает итоговые настройки и выбранный runtime; само
чтение настроек не создаёт базу. Для новой установки выполните `richi init`, затем `richi check`.
Для существующей базы укажите её путь и выполните `check` до записи данных.

Настройки можно передать JSON-файлом:

```json
{
  "database": "/path/to/existing/memory.sqlite3",
  "data_dir": "/path/to/richi-data",
  "port": 8765,
  "dev": false,
  "development": {"source": "/path/to/Richi"}
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

Обе команды следуют общему параметру `dev` в конфигурации; отдельный флаг режима
у карты не нужен. `richi map` запускает или переиспользует сервер выбранной базы
и открывает браузер. Карта доступна только на loopback, показывает граф,
определения, источники и Git-навигацию. Запись знаний выполняется через CLI.
Карта не импортирует разговоры и не выполняет процедуры из записей.

Работающая карта замечает переключение `dev` и после проверки нового runtime
переходит на него с сохранением PID и URL. В dev она также отслеживает изменения
Python- и SQL-файлов исходников. Изменение SQL-файла не применяет миграцию базы.
Для загрузки изменённого интерфейса обновите страницу браузера.

Если проверка новых исходников обнаружила ошибку синтаксиса, импорта или
совместимости схемы, карта сохраняет предыдущий работающий процесс и пишет
диагностику. Дочерние операции графа и Git читают исходники заново, поэтому
при повреждённом dev могут временно не работать и в сохранившемся сервере.
Исправьте исходники или отключите dev; CLI-команды управления конфигурацией
остаются доступны. При фоновом запуске лог находится в
`runtime/map-server.log` рядом с выбранной базой; при `map serve` — в терминале.

## Подключение агента

Скопируйте всю папку `skills/project-memory` в каталог skills своего агента.
Для Codex это обычно `${CODEX_HOME:-$HOME/.codex}/skills/project-memory`.
Если skill уже установлен, сначала сохраните его отдельную копию и проверьте,
нет ли в нём локальных дополнений. Все ссылки внутри переносимого skill
ведут в его собственную папку `references/`.

Убедитесь, что процесс агента видит исполняемый файл `richi` и выбранную
конфигурацию. Сначала вызовите `richi chat current` и `richi workspace list`.
Если чат ещё не привязан, спросите пользователя: текущая область CLI или другая
для этого чата; сохраните ответ через `chat bind --current` либо `chat bind NAME`.
Затем проверьте `richi config show` и `richi check`: режим,
исходники и база должны совпадать с ожидаемыми.
Настройка в терминале не гарантирует те же переменные в IDE или desktop-приложении.

Пример инструкции проекта для явно разрешённых локальных обновлений:

```markdown
At the start of a new chat, inspect `richi chat current` and `richi workspace list`.
If unbound, ask once whether to use the current CLI workspace or another one for
this chat. An explicit workspace already named by the user is sufficient.
Save the answer with `richi chat bind --current` or `richi chat bind NAME`.
Reuse the binding in later turns and after compaction; do not change the global
CLI default. Rebinding requires the user's explicit request and `--replace`.
Before repository work, verify `richi project check PATH --id PROJECT_ID`, then
use `--project-path PATH` on knowledge operations. Do not infer a workspace from cwd.
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
процессов. Для переключения workspace есть CLI-меню `richi use`; отдельный MCP можно добавить позднее.
