# Try Richi with colleagues

English | [Русский](TESTING.ru.md)

Check whether a new chat can read a synthetic note saved by another chat in the
same workspace. Use local Codex, Claude Code or Cursor Agent on macOS or Linux.

## Prepare your chosen agent

Follow [installation or connection to an existing installation](SETUP.md). Choose
a stable release that supports your agent. To test a change before publication,
install the candidate checkout in its own environment using the
[source installation guide](INSTALLATION.md), then connect the client through `richi agents`.

Open a new terminal and check:

```sh
command -v richi
richi --version
richi setup --help
```

If help offers `--agent`, run `richi agents --status` and look for
`configured` next to your chosen agent. On 1.0.1, which supports only Codex,
use `richi setup --status` and look for the same status. Both checks are read-only. If PATH has not refreshed, use
`~/.local/bin/richi`. In Cursor the `sessionStart` hook gives the agent the
command prefix for its current chat.

## Check memory between two chats

Open a new chat in the selected agent and send this message:

> Use only workspace `richi_test` for this chat. Create it if needed, bind this
> chat to it and initialize its store if necessary. Report the runtime mode from
> `richi config show`. Do not read other workspaces. Create
> synthetic project `test_demo` without a repository path. Save a confirmed note
> `note:orion-test` titled “Orion test”: “Synthetic test: Orion uses a 30-second
> timeout. This is not production.” Assign it to `test_demo` and cite the source
> as manually supplied synthetic test data. Show the workspace and saved ID.

If `richi_test` already contains unrelated work, choose another test name and use
it consistently. Explicit workspace selection is required in each new chat.
Then, in an ordinary terminal:

```sh
richi -w richi_test check
richi -w richi_test entry get note:orion-test
richi -w richi_test recall Orion --project test_demo
richi -w richi_test map
```

Expect a valid database and the note in both recall and the map. In a **second
new chat**, explicitly choose `richi_test` and ask the agent to find and read the
Orion note, report its timeout and whether it describes production, without
changing memory. Expect 30 seconds and a clear statement that it is synthetic.
The test checks persistence; Richi does not import conversation history.

## Send feedback

Include OS, Python version, `richi --version`, setup status, the failing step,
expected/actual behavior and a minimal synthetic reproduction. Mention whether
the second chat successfully used the saved note. Remove private paths and
content; do not send databases, setup backups or personal logs.
