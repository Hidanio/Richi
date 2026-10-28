# Richi installation and update audit — 28 October 2026

English | [Русский](INSTALL_UPDATE_AUDIT-2026-10-28.ru.md)

Base: `main` at `6729d4c` plus the audit fixes. The local branch was four merged
changes behind; it was fast-forwarded and all working-copy edits restored.
SHA256 checks confirmed that 131 pre-existing untracked documents were unchanged.
A backup of edits was retained outside the sources. The personal installation,
OS schedules and published releases were not changed.

Artifact hashes and measurements below describe the installation audit before
the bilingual documentation follow-up; they are not hashes of a published release.

## Observed GitHub state

- The [repository](https://github.com/Hidanio/Richi) is public; its API returns HTTP 200.
- [Installer PR #15](https://github.com/Hidanio/Richi/pull/15) merged on 28 October
  at 12:11 Moscow time (`09:11:02Z`), merge commit `6729d4c`.
- The [releases API](https://api.github.com/repos/Hidanio/Richi/releases?per_page=5)
  returns HTTP 200 and an empty list. Releases are absent; the repository is
  accessible.
- The actual command `richi --compact-json update check` returned `no_release`.
- `https://github.com/Hidanio/Richi/releases/latest/download/install.sh` returned
  HTTP 404: merging code does not publish installer assets.

A real published update download therefore remains unverified. Network access
was checked through the official feed; the remaining lifecycle used actual wheel
and installer artifacts with synthetic transport.

## Release lifecycle

1. `update check` fetches `releases/latest` or an exact `tags/vX.Y.Z` from
   `Hidanio/Richi`. Drafts and prereleases are rejected.
2. It validates the manifest, version/tag, expected asset URLs/names, protocol,
   Python and schema contract. Workspace databases are not opened.
3. `apply` checks wheel size, SHA256, metadata and structure, then creates a
   separate candidate venv. Installation uses `--no-index --no-deps`.
4. After self-check, authorization and lock checks, the runtime is switched.
   A recoverable setup journal coordinates the managed skill with the runtime.
5. Current and previous versions remain available; leases retain versions used
   by running processes. `rollback` is offline and cleanup is repeatable.

| Check response | Meaning |
| --- | --- |
| `no_release` | No stable release exists. |
| `update_available` | A newer compatible version is available. |
| `release_available` | The version matches an unverified checkout build; its first transition to a release is allowed. |
| `up_to_date` | Version and verified SHA match. |
| `older_release` | Apply refuses a downgrade; explicit rollback remains available. |
| `incompatible_release` | Python or the schema contract is incompatible; `can_apply: false`. Unknown protocols are rejected while reading the manifest. |
| `release_changed` | An installed verified version was republished with a different SHA; a new version number is required. |

`auto enable --interval 6h` enables checks. Automatic installation requires
separate `--install` authorization. The OS job wakes every 15 minutes; Python
checks due time, backoff, policy and locks. Enabling again without `--install`
revokes installation permission; a manual rollback suspends automatic installation.
There are no OS notifications yet; read results through `auto status`.

The inspected personal installation had `enabled=false`, `auto_install=false`
and no registered OS job. The audit did not enable it.

## Fixed defects

| Priority | Reproducer | Fix |
| --- | --- | --- |
| P2 | `check` offered installation with Python `[99,0]` or an incompatible schema. | Shared `assess_release` for check/apply and check-only auto; installation is not offered. |
| P2 | Verified current `0.2.0` could be replaced by another `0.2.0` wheel with a different SHA. | `release_changed` rejects it before activation. The first transition from an unverified seed remains possible. |
| P2 | Interruption between seed package removal and stub creation broke `python -m richi`. | The permanent `__main__.py` is installed atomically before removal and survives partial cleanup. |
| P2 | On Linux, a missing timer skipped service checks, allowing a foreign service to start before verification. | Service fragment/drop-ins and ownership are checked even without a timer, before write/enable/start. |
| P2 | Removing a timer could leave an owned service; control without its ownership file was ambiguous. | A verified owned service is stopped; an unverified service is not touched. |

Integration retained setup recovery before apply/rollback/cleanup, the pending
journal guard, skill/runtime coordination and setup-protocol checking.

## Practical checks

- A real pyproject-built wheel was installed into a new venv with a space in its
  path. From a foreign cwd, checks covered CLI and `python -I -m richi`,
  config/init/project/entry/recall/status/check, and temporary map HTTP health/graph.
- Merged installer code passed fresh/repeat installation, three real-wheel
  upgrades, bundled-skill switching, rollback and preservation of workspace
  bytes, chat bindings and personal instructions.
- Exact `.pyz`, wheel and manifest artifacts passed `installer_release_smoke.py`.
  The shell wrapper was also checked with transport stubs: a correct checksum
  installs; an incorrect checksum stops before Richi/Codex changes.
- PATH was tested with real `zsh -ic` and `zsh -lic`. Noninteractive `zsh -lc`
  does not read `.zshrc`; the absolute command in the AGENTS block works.
- Tests cover corrupt checksums/manifests, incompatibility, pre-activation
  candidate failure, no-op, rollback, leases, interrupted cleanup, revoked auto
  consent, retry/backoff and quarantine.
- The normally installed combined build matched 47 runtime/launcher/bootstrap
  files and 22 bundled skill files byte for byte.
- A real old wheel lacking the setup protocol was updated to the tested wheel.
  The runtime works and permanent bootstrap bytes remain unchanged. `setup --yes`
  explains that a new dedicated environment is required. Old skill, instructions
  and database remain intact; new integration metadata is not created.

The exact combined-checkout artifacts were verified. All 69 runtime,
launcher/bootstrap and bundled skill files in the wheel matched the installed
copy used for the full tests:

| Artifact | SHA256 |
| --- | --- |
| `richi-0.1.0-py3-none-any.whl` | `1d2908e2cd8f0662775b29e55f29e0f3663857b9f93f9fbe785de204d2e771bc` |
| `richi-installer.pyz` | `e45cd0e9897a443a38cfe8332fb2c3321e5ddeff17727747b00f801ccd927317` |
| `richi-release.json` | `76ca53567be1553d425b422c700a79393504f33cd15966be35ebe39adccad7d2` |

Full `python -m unittest discover -s tests -v`: 756 tests in 711.862 seconds.
744 passed, one platform skip; 11 `test_graph_view` tests produced 31 errors
including subtests. All shared one cause: after CLI parser extraction, their
factory passed `memory` instead of `memory.limit`. Only that test call changed;
runtime and tested artifacts stayed unchanged. Repeating all of `test_graph_view.py`
passed 11/11 in 1.277 seconds. Every applicable test therefore had a successful
result on the final runtime; a second full run was not made during this audit
phase after fixing the factory.

Public recall evaluation: 7/7, with the synthetic database unchanged.
`git diff --check` passed. The known `е/ё` Unicode miss was reproduced on a new
schema 2 database of the current build; it remains in the main audit report.

OS scheduler calls were stubbed in tests. A real Linux systemd user manager and
a live LaunchAgent firing were not checked. The local platform was macOS/Python
3.9; Linux/Python 3.12 must pass release CI.

## PR follow-up: bilingual documentation and final checks

The follow-up reran the entire runtime suite: 756 tests in 703.144 seconds,
755 passed and one platform skip, with no errors or failures. A rebuilt wheel
with the bilingual documentation then passed 61 package/CLI/setup tests and the
exact-artifact offline installer smoke test. All 47 runtime files and 43 skill
files, including 21 Russian skill pages, matched the installed copy byte for byte.
The final installed build passed the recall evaluation: 7/7, database unchanged.

Documentation has 44 complete English/Russian pairs. All 484 local links and
anchors passed validation. Command examples are preserved; the README shell
comment is localized. `git diff --check` passed. These local checks do not replace
the Linux/macOS and Python 3.9/3.12 CI matrix.

The bilingual build is a test artifact, not a published release:

| Artifact | SHA256 |
| --- | --- |
| `richi-0.1.0-py3-none-any.whl` | `6ec9261bd346b567991ed67fc0e9b39e6025a3f641a8ca466925006305b3b5f0` |
| `richi-installer.pyz` | `309771d44c890b5f296f753da32a124f1f77b3ebd7a78d09a4f347931bf76504` |
| `richi-release.json` | `362ee77e85945eae32e77a08f9a14b298895226a5626c3c78c6bc80d43df823e` |

## Public installation

After including the fixes in a release, align version/tag and run the release
workflow. It tests exact artifacts in its matrix before publishing the wheel,
manifest, installer `.pyz`, checksum and `install.sh`. Until publication, the
one-command URL is unavailable and the updater's correct response is `no_release`.

An old bootstrap installation without the setup protocol needs the separate
transition described in [SETUP.md](SETUP.md), not `pip install -U` over managed
generations. This audit did not replace the personal installation or integration.
