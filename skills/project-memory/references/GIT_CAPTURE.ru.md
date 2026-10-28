<a id="capture-and-attach-the-code-actually-checked"></a>

# Сохранить и прикрепить действительно проверенный код

[English](GIT_CAPTURE.md) | Русский

Capture создаёт свидетельство, а не утверждение о знании и не обновление даты проверки.
Используй выбранный workspace и проверку членства репозитория из [SKILL.ru.md](../SKILL.ru.md).
У зарегистрированного проекта должен быть `repo_path`; все примеры синтетические.

```sh
richi project check /work/example --id service-example
richi --compact-json --project-path /work/example sources capture \
  --project service-example --path internal/cache.go --output /tmp/cache-source-01.json
# For committed bytes, add --rev HEAD (or a local tag, branch or SHA).
richi --compact-json --project-path /work/example entry get task:EXAMPLE-42
richi --compact-json --project-path /work/example sources attach \
  --ref entry:task:EXAMPLE-42 --json /tmp/cache-source-01.json \
  --expected-updated-at 'UPDATED_AT_FROM_GET'
```

<a id="checkout-and-version"></a>

## Checkout и версия

Для `sources capture` порядок выбора: явный `--repo`, корень Git для
проверенного `--project-path`, затем зарегистрированный `repo_path`. Один лишь cwd оболочки
никогда не выбирает checkout. `--project-path` по-прежнему проверяет членство workspace;
`--repo` должен использовать общий Git-каталог зарегистрированного репозитория: связанные
worktrees допустимы, отдельный clone не считается тем же репозиторием автоматически.
`--path` задаётся относительно выбранного корня Git, даже если project-path указывает подкаталог.

Перед прикреплением проверь `capture_context.repo_root`, `selected_by` (`repo`, `project_path`,
`registered`) и `resolved_commit`: они должны описывать действительно изученные
checkout и версию. `--output` создаёт новый файл только с
`source`; контекст остаётся в stdout, существующие выходные файлы никогда не перезаписываются.

`--rev` сохраняет байты из commit. По умолчанию / с `--worktree` сохраняются реальные
рабочие байты, включая untracked-изменения, в `artifacts/git/<sha256>.blob`
рядом с базой. Базовый commit — HEAD; `dirty` сравнивает этот файл с данным
commit. Незакоммиченные байты не являются содержимым commit. Репозитории
без первого HEAD не поддерживаются; удалённый файл можно сохранить с более старым `--rev`.

Созданная привязка `git` версии 1 содержит `repo_id`, `path`, полный `commit`, `blob`,
`mode`, `dirty` и `snapshot` для worktree-свидетельства. Blob принадлежит базовому
commit и равен null для нового файла. `revision`, `sha256`, `observed_at` заполняются
по реальным свидетельствам. Старые источники без `git` остаются допустимыми и не
привязываются молча к текущему HEAD; миграция SQLite не нужна.

<a id="attach-without-replacing-the-record"></a>

## Прикрепить без замены записи

Для новой entry/entity/edge включи созданный объект в `sources`.
Для существующей записи `sources attach` добавляет источник, сохраняя поля,
проекты, существующие источники и историю. Требуется свежий `updated_at`, даже
если источник уже прикреплён; также проверяется доступность сохранённых байтов.
При конфликте перечитай и согласуй данные, затем проверь полную запись. Не прикрепляй источник
в задаче только на черновик или чтение. Здесь `--json` принимает один файл источника, не stdin.

Операции Git используют локальные объекты без checkout, изменения index или fetch.
Capture поддерживает обычные файлы до 8 MiB, но не symlinks, submodules и пути
вне репозитория. Partial/promisor-репозитории отклоняются, чтобы избежать lazy fetch.
Храни артефакты вне исходного checkout; SQLite backup сам по себе не содержит
`artifacts/git/` или объекты репозитория, нужные для повторного чтения исторических свидетельств.

Для исторического просмотра, `git-check`, diff и связанных знаний прочитай [GIT.ru.md](GIT.ru.md).
Выбор по project-path специфичен для capture: команды чтения Git,
например `git-check`, `diff` и `impact`, требуют явный `--repo` для выбора
связанного worktree. `sources related` ищет только сохранённые привязки, не выбирает
checkout и не принимает `--repo`.
