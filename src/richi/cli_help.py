"""Human help for the Richi CLI: overview, grouped commands, pages and topics.

The live argparse tree stays the single source of syntax and one-line
summaries. This module adds ordering, longer descriptions, examples and
cross-cutting topics. It is loaded only to print help, describe a capability
or report an argument error; rendering never opens a knowledge store.
Without a compatible richi_launcher.help_format (an older installed launcher)
help falls back to plain argparse pages.
"""
import argparse

# The richi_launcher.help_format API this module is written for, and every
# name of it that the runtime uses (tests keep the list complete). A launcher
# that differs gets plain argparse help instead of a failure.
API = 1
NEEDS = ("HelpFormatter", "TEXT_WIDTH", "choice_error", "complete", "did_you_mean", "document", "find",
         "finish", "guide_url", "known_args", "local_guide", "selected", "sentence", "subcommands",
         "suggestions", "summaries", "syntax", "tidy", "unrecognized", "usage_message", "walk")
try:
    from richi_launcher import help_format as style
except ImportError:  # development sources run by an older installed launcher
    style = None
if getattr(style, "API", None) != API or not all(hasattr(style, name) for name in NEEDS):
    style = None


# How `richi help` presents commands. A command missing here is still listed,
# under "other commands", so the overview can never hide one.
GROUPS = (
    ("getting started", ("setup", "agents", "init", "status")),
    ("find knowledge", ("recall", "brief", "discover", "fetch", "search", "context")),
    ("read and write records", ("entry", "entity", "edge", "relation")),
    ("explore and analyze", ("map", "graph", "tools", "query", "punisher")),
    ("code evidence (Git)", ("sources",)),
    ("projects, workspaces and chats", ("project", "workspace", "use", "chat", "rules", "scenario", "skill-sources")),
    ("configuration and updates", ("config", "update")),
    ("maintenance", ("check", "backup", "storage", "search-index", "migrate")),
    ("help", ("help", "capabilities")),
)

# Examples of `richi help`; scripts/check_cli_docs.py parses every line.
EXAMPLES = (
    ("Ask what is already known", 'richi recall "why did we choose a local cache" --project service-example'),
    ("Prepare for a change on this branch", 'richi brief "cache rework" --project service-example --base main'),
    ("Read whole records", "richi fetch --ref entry:task:ABC-123 --ref entity:component:cache"),
    ("Count decisions in a project", "richi query count --project service-example --where \"kind = 'decision'\""),
    ("Use another workspace for one command", "richi -w work status"),
)

# Commands that a bare `richi` suggests; each is described by its own summary.
OVERVIEW_COMMANDS = ("richi status", 'richi recall "QUESTION"', 'richi brief "TASK"', "richi project list", "richi map")
# How a bare `richi` shows that every command has a page.
HELP_EXAMPLE = "richi recall --help"

TOPICS = {
    "start": ("First steps: connect an agent, create a store, register projects", """
First steps with Richi

1. Connect an AI agent. Preview first; existing instructions are preserved.
     richi agents --status
     richi setup --dry-run
     richi setup --yes                     # Codex
     richi setup --agent claude --yes      # Claude Code
     richi setup --agent cursor --yes      # Cursor
     richi setup --agent antigravity --yes # Antigravity

2. Create the knowledge store of the current workspace (safe to repeat).
     richi init
   Keep unrelated work apart in its own workspace if you like:
     richi workspace create work
     richi -w work init

3. Register repositories so knowledge can be scoped to them.
     richi project add /path/to/service
     richi project scan /path/to/repositories            # preview
     richi project scan /path/to/repositories --apply    # register all found

4. Check the result.
     richi status --projects

5. Use it.
     richi recall "how do we deploy the service" --project service
     richi map

In an agent chat the agent asks once which workspace to use and remembers the
choice with richi chat bind. Agents usually write records through the Richi
skill. See richi help records for the contents of each record type.
"""),
    "records": ("Record types, references, knowledge states and safe updates", """
Records, references and knowledge states

entries (richi entry)
  kind: {entry_kinds}
  work_state: {work_states}
entities (richi entity)
  kind: {entity_kinds}
edges (richi edge)
  explicit, sourced links between records; the kind names the meaning
projects (richi project) and relations between projects (richi relation)

references, used by fetch, graph neighbors, sources and read_more:
  entry:<id>   entity:<id>   edge:<id>   project:<id>
  Keep everything after the first colon: entry:task:ABC-123 has the ID
  task:ABC-123. Pass an ID that starts with a dash after --:
  richi entry get -- -odd-id

knowledge_state:
  confirmed    established knowledge, normally backed by sources
  hypothesis   an unverified assumption (the default for new records)
  superseded   replaced by newer knowledge
  recall, discover, query, graph and tools show only confirmed records unless
  you add --include-hypotheses or --include-superseded; search and context
  show hypotheses too; fetch and get return any state you ask for.

safe updates:
  put replaces the whole record. Read it in full first (richi entry get ID),
  keep every field you do not mean to change, drop created_at, updated_at and
  history, and pass the read updated_at as expected_updated_at. A concurrent
  change is then rejected instead of silently overwritten; null means
  "create only".
"""),
    "filters": ("Grammar of --where expressions for recall, discover, search, query", """
Filter expressions (--where)

recall, discover, search and every query command accept --where EXPR: a
strict condition applied before ranking and limits. A record that fails it
never appears, not even through graph links.

  richi recall "pool sizing" --where "kind IN ('decision', 'experiment')"
  richi search cache --where "project = 'service-example' AND tags HAS 'performance'"
  richi query count --where "work_state = 'blocked' AND verified_at < '2026-01-01'"

operators (precedence: NOT, then AND, then OR; group with parentheses):
  =  !=  <  <=  >  >=            compare a field with a 'quoted' value
  IN ('a', 'b')   NOT IN (...)    membership in a literal list
  IS NULL   IS NOT NULL           missing values
  LIKE 'Cache%'   NOT LIKE        % matches any run, _ one character
  tags HAS 'x'   aliases HAS 'x'  exact array membership; NOT HAS too

fields (family is entry or entity):
  {filter_fields}

notes:
  Values are single-quoted strings; double an embedded quote: 'team''s'.
  Dates compare as ISO dates or timestamps; a date alone means midnight UTC.
  Entities have no work_state or jira_key, so NOT work_state = 'x' skips them;
  write work_state IS NULL OR work_state != 'x' to keep them.
  --project on recall and discover only boosts ranking; project = 'x' filters.
  Raw SQL, functions, subqueries and comments are rejected.
"""),
    "output": ("JSON output, errors, exit codes and response budgets", """
Output, errors and exit codes

  stdout   results as JSON, indented; --compact-json removes the whitespace
  stderr   errors as one JSON object: {"error": "...", "code": "..."}
           code is optional. A mistake the argument parser catches (an
           unknown command or option, a missing or malformed value,
           options that a usage line separates with |, --workspace with
           --config, an argument that a run without a terminal needs)
           uses "usage_error" and adds "help" with the page that explains
           the command. So does a global option the command does not take
           (--project-path with init, --compact-json with map, storage
           options with chat or agents); "help" then names that command's
           page. Every other error keeps its own code or has none, for
           example a reference, field name, date or --where expression
           that a command checks itself, --check-limit without
           --check-sources, a storage option the command takes but the
           selected workspace refuses (--db with a workspace that keeps
           its own storage), or a malformed chat ID (invalid_chat_id). In
           a chat that has no workspace yet, a mistake the runtime parses
           outside help is chat_workspace_required: that runtime is not
           chosen yet. The launcher checks some mistakes first and reports
           them as usage_error even then: those in its own commands
           (such as workspace, chat and config), a mistyped launcher
           command or help, and mistakes in the global options before the
           command (a mistyped one, a missing value, the form of -w, --db
           and --config, --workspace with --config, --project-path with
           map) or in map's --port, --db and --config
  help     plain text: richi help, COMMAND --help; --version: Richi X.Y.Z

exit codes:
  0  success, including help and reports that describe problems
     (status, punisher, storage stats)
  1  any error: invalid arguments, a missing record, a failed check, an
     unavailable store or runtime

bounded responses:
  Commands with --max-chars (recall, brief, discover, fetch, query, ...) fit
  their JSON into that many characters. Look at truncated, is_excerpt and
  sources_omitted, and follow read_more for whole records before relying on
  an excerpt. An empty result that is not no_match only means nothing fit.

input:
  put and upsert commands read --json FILE, or --json - for stdin: one object
  or an array of up to 1000 objects, applied atomically.
"""),
    "environment": ("Environment variables and default locations", """
Environment variables

selection (command-line options take precedence):
  RICHI_WORKSPACE   workspace to use when -w is not given
  RICHI_CONFIG      configuration file; takes precedence over RICHI_WORKSPACE
  RICHI_DB          database file (rejected for a named workspace)
  RICHI_DATA_DIR    directory of the default database file memory.sqlite3
                    (rejected for a named workspace); backups, captured Git
                    artifacts and runtime files live next to the database
  RICHI_PORT        loopback port of the map (default 8765)

agent chats:
  RICHI_CHAT_ID     stable chat identity for workspace binding; Claude Code
                    setup provides it through its SessionStart hook, and
                    CLAUDECODE=1 marks its shells
  CODEX_THREAD_ID   chat identity supplied by Codex
  ANTIGRAVITY_CONVERSATION_ID
                    chat identity supplied by Antigravity (used as
                    antigravity:<id>); ANTIGRAVITY_AGENT marks its shells
  CURSOR_AGENT      marks a Cursor Agent shell. Use the sessionStart hook's
                    --chat cursor:<id> on every command.
  In a chat, RICHI_CONFIG, RICHI_DB and RICHI_DATA_DIR are rejected: the
  chat's binding selects the store.

default locations:
  macOS   ~/Library/Application Support/Richi
  Linux   configuration in $XDG_CONFIG_HOME/richi (~/.config/richi),
          data in $XDG_DATA_HOME/richi (~/.local/share/richi)
  Named workspaces live under workspaces/NAME in the data directory.

set by Richi for its own processes; do not set them yourself:
  RICHI_ACTIVE_RUNTIME, RICHI_ACTIVE_CONFIG, RICHI_ACTIVE_WORKSPACE,
  RICHI_INSTALLATION_ROOT, RICHI_INSTALLATION_GENERATION,
  RICHI_INSTALLATION_LEASE_FD, RICHI_MAPS_PASSIVE, RICHI_MAP_PROBE,
  RICHI_MAP_LISTEN_FD
"""),
}

# Indented like the DETAILS texts it starts: help_format.paragraphs()
# dedents a text as a whole and keeps an indented block as it is.
CHANGE_RECORD = """
        Reads one object or an array of up to 1000 from --json FILE or --json -
        (stdin) and applies them atomically: one invalid object rolls back the
        whole batch. put replaces the whole record, so start from a full get, keep
        every field, and pass its updated_at as expected_updated_at; a concurrent
        change is then rejected instead of lost (null means create only)."""

# Longer pages for runtime commands, keyed by command path. The one-line
# summary is the `help=` of the declaration and heads every page.
DETAILS = {
    "init": dict(about="""
        Creates the SQLite database with the current schema and full-text search
        when SQLite provides it. Running it again only reports the existing store.
        A store with an older schema must be upgraded with migrate, which backs it
        up first.""",
        examples=(("", "richi init"), ("In another workspace", "richi -w work init"),
                  ("An explicit database file", "richi --db /path/to/memory.sqlite3 init")),
        see=("workspace create", "check", "migrate"), guide="CLI.md"),
    "recall": dict(about="""
        Searches IDs, titles, summaries, issue keys, tags and aliases of entries and
        entities, then adds one hop of explicit graph links. Matching is lexical:
        try other words or the other language when a paraphrase misses. --project
        boosts that project without hiding others; use --where for a strict scope.

        The JSON response fits --max-chars and contains excerpts. Check
        is_excerpt, truncated and sources_omitted, and follow read_more for whole
        records before relying on details. No match does not prove that work
        never happened.""",
        examples=(("Ask within a project", 'richi recall "why did we choose a local cache" --project service-example'),
                  ("Only decisions and experiments",
                   "richi recall \"connection pool\" --where \"kind IN ('decision', 'experiment')\""),
                  ("Why is a known record not found?", 'richi recall "cache" --expect entry:decision:local-cache')),
        see=("discover", "fetch", "brief", "filters")),
    "brief": dict(about="""
        Starts from recall and, when asked, adds knowledge attached to files changed
        between --base and --target (or the working tree), plus one hop of stored
        links. Everything shares one budget and keeps states, sources and selection
        reasons. --check-sources compares the Git evidence of selected cards with
        the target. Without --base and --check-sources, Git is not run.""",
        examples=(("Before changing something", 'richi brief "what to know before changing the cache" --project service-example'),
                  ("Add knowledge about files changed on this branch",
                   'richi brief "cache review" --project service-example --base main'),
                  ("Uncommitted changes, with source checks",
                   'richi brief "cache" --project service-example --base HEAD --worktree --check-sources')),
        see=("recall", "sources impact")),
    "discover": dict(about="""
        Ranks like recall but returns compact cards (at most 300 characters of
        excerpt, no sources), so many candidates fit a small budget. Cards are
        routing hints, not evidence: read the chosen records with fetch.""",
        examples=(("", 'richi discover "pool sizing" --project service-example'),
                  ("Then read the chosen ones",
                   "richi fetch --ref entry:experiment:pool-sizing --ref entity:concept:connection-pool")),
        see=("fetch", "recall", "filters")),
    "fetch": dict(about="""
        Reads one consistent snapshot and returns whole current records without
        history: entries, entities, edges and projects, in any knowledge state.
        Missing, oversize and budget-limited references are reported explicitly;
        continue with --offset when the response says so.""",
        examples=(("", "richi fetch --ref entry:task:ABC-123 --ref entity:component:cache"),
                  ("More room for long records", "richi fetch --ref entry:task:ABC-123 --max-chars 40000")),
        see=("discover", "entry get", "records")),
    "search": dict(about="""
        Matches any query word as a prefix in IDs, titles, summaries, issue keys,
        tags and aliases and returns whole entries, best match first. --project
        and --where are strict. Hypotheses are included; superseded entries need
        --include-superseded. Prefer recall for questions; search suits short
        keywords and exact scopes.""",
        examples=(("", "richi search 'query optimization'"),
                  ("", "richi search ABC-123 --project service-example"),
                  ("", "richi search cache --where \"title LIKE 'Cache%'\"")),
        see=("recall", "filters")),
    "context": dict(about="""
        Returns the project, its relations in both directions, the related projects
        and a bounded list of entries: the project's own first, then its
        neighbors', most recently updated first. It does not go beyond one hop.""",
        examples=(("", "richi context --project service-example --limit 30"),),
        see=("relation", "graph neighbors"), guide="CLI.md"),
    "entry": dict(about="""
        Entries are the main records: tasks, decisions, facts, notes,
        investigations, experiments and procedures. Each keeps its sources,
        projects, states and a full revision history.""",
        examples=(("Read a whole entry", "richi entry get task:ABC-123"),
                  ("With its revision history", "richi entry get task:ABC-123 --history"),
                  ("Create or replace from a file", "richi entry put --json entry.json")),
        see=("records", "fetch", "sources attach"), guide="ENTRY_WRITE.md"),
    "entry put": dict(about=CHANGE_RECORD + """

        Required: id, kind, title, summary. Optional: project_ids, work_state,
        knowledge_state (default hypothesis), sources, jira_key, tags, aliases,
        verified_at, expected_updated_at. Tasks can carry a decision_card with a
        question, observations, recommendation, recorded outcome and review
        condition. Confirmed facts and decisions need at least one source.""",
        examples=(("", "richi entry put --json entry.json"), ("From stdin", "richi entry put --json - < entry.json")),
        see=("records", "entry get")),
    "entry get": dict(about="""
        Returns the full current record; --history adds every revision. For several
        records at once use fetch.""",
        examples=(("", "richi entry get task:ABC-123"), ("", "richi entry get task:ABC-123 --history"),
                  ("An ID that starts with a dash", "richi entry get -- -odd-id"))),
    "entry read": dict(about="""
        A size-limited card for quick reading: long summaries and sources may be
        cut. Use entry get before updating a record or relying on details.""",
        examples=(("", "richi entry read task:ABC-123 --max-chars 4000"),), guide="RECALL.md"),
    "entity": dict(about="""
        Entities are graph nodes besides entries: components, concepts, documents,
        Jira issues, metrics, pull requests and tools. Concepts form the
        encyclopedia of scoped project terms; tools form the tools catalog.""",
        examples=(("", "richi entity get concept:connection-pool"),
                  ("", "richi entity get concept:connection-pool --history"),
                  ("", "richi entity put --json entity.json")),
        see=("edge", "graph", "tools", "records"), guide="GRAPH.md"),
    "entity put": dict(about=CHANGE_RECORD + """

        Fields: id, kind, title, summary, sources, knowledge_state, verified_at,
        tags, aliases. Confirmed entities need at least one source; tool entities
        need schema 3 or newer.""",
        examples=(("", "richi entity put --json entity.json"),)),
    "entity get": dict(examples=(("", "richi entity get concept:connection-pool --history"),)),
    "edge": dict(about="""
        An edge connects two records (entry:, entity: or project: references) with
        a kind such as depends_on, used_in or uses_tool, plus its sources and
        knowledge state. Direction and kind carry the meaning; a shared project or
        a path in the graph alone proves nothing.""",
        examples=(("", "richi edge get cache-uses-redis"), ("", "richi edge put --json edge.json")),
        see=("graph neighbors", "entity", "records"), guide="GRAPH.md"),
    "edge put": dict(about=CHANGE_RECORD + """

        Fields: id, from_ref, to_ref, kind, description, sources, knowledge_state,
        verified_at. Confirmed edges need at least one source. Entry membership in
        projects comes from the entry's project_ids, not from edges.""",
        examples=(("", "richi edge put --json edge.json"),)),
    "edge get": dict(about="""
        Pass the edge's native ID, without the edge: prefix that graph output adds.""",
        examples=(("", "richi edge get cache-uses-redis --history"),)),
    "relation": dict(about="""
        A relation links two different registered projects with a free-form kind
        such as calls_api, publishes_to or shares_storage. Read relations with
        context; finer-grained links between records are edges.""",
        examples=(("", "richi relation put --json relation.json"),), see=("context", "edge"), guide="CLI.md"),
    "relation put": dict(about="""
        Reads one object or an array from --json FILE or --json - and applies them
        atomically. Required: id, from_project, to_project, kind. Optional:
        description, source, knowledge_state (default hypothesis), verified_at. A
        confirmed relation requires a source. Writing an existing ID replaces all
        of its fields; relations keep no history.""",
        examples=(("", "richi relation put --json relation.json"),)),
    "map": dict(about="""
        Starts a local server on 127.0.0.1 in the background, or reuses a
        compatible one, and opens the browser. map serve runs the server in this
        terminal instead; Ctrl+C stops it. map stop stops this workspace's map,
        and map restart starts its current version on the same URL, or starts one
        if none is running. The map reads knowledge and stored Git sources and
        never edits them.""",
        examples=(("", "richi map"), ("Foreground server", "richi map serve --open"),
                  ("Another port, no browser", "richi map --port 8766 --no-open"),
                  ("Stop the map, or restart it without a browser",
                   ("richi map stop", "richi map restart --no-open"))),
        see=("graph", "config show", "update"), guide="CLI.md"),
    "graph": dict(about="""
        neighbors answers local questions around one record; export returns the
        whole explicit graph that the map also uses.""",
        examples=(("", "richi graph neighbors entry:task:ABC-123 --view compact --max-chars 12000"),
                  ("", "richi graph export --project service-example")),
        see=("edge", "map"), guide="NEIGHBORS.md"),
    "graph neighbors": dict(about="""
        Follows incoming and outgoing links from REF (project:, entry: or entity:).
        --view compact fits the answer into --max-chars; the default full view
        returns whole records. Only confirmed nodes and edges appear unless you
        include other states. Absence beyond --depth is not evidence of no link.""",
        examples=(("", "richi graph neighbors entry:task:ABC-123 --depth 1 --view compact --max-chars 12000"),
                  ("", "richi graph neighbors project:service-example --depth 2 --limit 50 --view compact"))),
    "graph export": dict(about="""
        Returns nodes and edges as JSON, or only those connected to one project.
        Large graphs are capped at 2000 nodes and 10000 edges and report
        truncation; prefer graph neighbors for a local question.""",
        examples=(("", "richi graph export"), ("", "richi graph export --project service-example"))),
    "tools": dict(about="""
        A tool is an entity with recorded applications: the projects it is used in,
        scenarios, configurations and the tasks that used it.""",
        examples=(("", "richi tools list"), ("", "richi tools get tool:example:tracekit")),
        see=("entity",), guide="TOOLS.md"),
    "tools list": dict(examples=(("", "richi tools list"), ("", "richi tools list --limit 20"))),
    "tools get": dict(examples=(("", "richi tools get tool:example:tracekit"),)),
    "query": dict(about="""
        Exact metadata operations inside SQLite, without a lexical shortlist. They
        cover every matching current record but never read summaries or sources;
        use fetch for evidence. --project is strict here, unlike on recall.""",
        examples=(("How many decisions?", "richi query count --project service-example --where \"kind = 'decision'\""),
                  ("Is there a record for a Jira issue?", "richi query exists --where \"jira_key = 'ABC-123'\""),
                  ("Records by kind and work state", "richi query facets --project service-example --by kind --by work_state"),
                  ("A page of titles; continue with --after", "richi query select --project service-example --fields ref,title,work_state --limit 50")),
        see=("filters", "fetch"), guide="QUERY_TOOLS.md"),
    "query count": dict(examples=(("", "richi query count --project service-example --where \"kind = 'decision'\""),)),
    "query exists": dict(about="Stops at the first match.",
                         examples=(("", "richi query exists --where \"jira_key = 'ABC-123'\""),)),
    "query facets": dict(about="""
        Groups by one to three --by fields; repeat --by for combined groups. NULL
        is its own group. has_more means groups were omitted, not that their
        counts are zero.""",
        examples=(("", "richi query facets --project service-example --by kind --by work_state"),)),
    "query select": dict(about="""
        Returns ref plus the chosen scalar fields: {select_fields}. Pass the last
        ref of a page as --after to continue; each page reads a new snapshot.""",
        examples=(("", "richi query select --project service-example --fields ref,title,verified_at --limit 50"),
                  ("Next page", "richi query select --project service-example --after entry:task:ABC-123"))),
    "punisher": dict(about="""
        A read-only report: database health plus candidates for manual review, such
        as old verification dates (--stale-days), missing sources, duplicate-looking
        content, shared aliases, orphan entities and summaries longer than
        --summary-warn-chars. Candidates are prompts to look, not verdicts; nothing
        is changed.""",
        examples=(("", "richi punisher --limit 20"),
                  ("", "richi punisher --project service-example --stale-days 90"),
                  ("Reproducible ages", "richi punisher --as-of 2026-11-01")),
        see=("check", "storage")),
    "sources": dict(about="""
        Links knowledge to the code that supports it. capture records a file's exact
        bytes and revision, attach appends that evidence to a record, and show,
        history and diff read it later without a checkout; commit lists the changed
        files of a legacy whole-commit reference (git:FULL_SHA). git-check,
        related and impact go the other way, from code to knowledge. snapshot and
        check track drift of selected records and local files. Nothing here uses
        the network.""",
        examples=(("Capture a file and attach it to an entry",
                   ("richi sources capture --project service-example --path internal/cache.go --output /tmp/cache.json",
                    "richi sources attach --ref entry:task:ABC-123 --json /tmp/cache.json --expected-updated-at UPDATED_AT")),
                  ("Has the code changed since?", "richi sources git-check --ref entry:task:ABC-123"),
                  ("Knowledge about files changed on this branch", "richi sources impact --project service-example --base main")),
        see=("brief", "records"), guide="GIT.md"),
    "sources capture": dict(about="""
        Captures working-copy bytes by default (including uncommitted changes) or
        committed bytes with --rev. The checkout is --repo, else the Git root of
        --project-path, else the project's registered repo_path; the current
        directory alone never selects it. Capturing creates evidence only; attach
        it to a record with sources attach.""",
        examples=(("", "richi sources capture --project service-example --path internal/cache.go --output /tmp/cache.json"),
                  ("Committed bytes", "richi sources capture --project service-example --path internal/cache.go --rev HEAD")),
        see=("sources attach",)),
    "sources attach": dict(about="""
        Appends the captured source to the record and keeps its fields, projects,
        other sources and history. Pass the updated_at from a fresh read; a
        concurrent change is rejected.""",
        examples=(("", "richi sources attach --ref entry:task:ABC-123 --json /tmp/cache.json --expected-updated-at UPDATED_AT"),)),
    "sources git-check": dict(about="""
        Compares attached Git evidence with a local revision (default HEAD) or the
        working copy of the registered checkout; --repo names a linked worktree
        instead. --project selects every record with Git evidence from that
        repository. It never fetches and never updates knowledge; matching bytes
        do not prove a conclusion is still right.""",
        examples=(("", "richi sources git-check --ref entry:task:ABC-123"),
                  ("All evidence from one repository against its working copy",
                   "richi sources git-check --project service-example --worktree"),
                  ("Against a linked worktree", "richi sources git-check --project service-example --worktree --repo /path/to/worktree"))),
    "sources show": dict(examples=(("", "richi sources show --ref entry:task:ABC-123 --source 1"),)),
    "sources history": dict(examples=(("", "richi sources history --ref entry:task:ABC-123 --limit 10"),)),
    "sources diff": dict(examples=(("Captured version against HEAD", "richi sources diff --ref entry:task:ABC-123"),
                                   ("Against the working copy", "richi sources diff --ref entry:task:ABC-123 --worktree"))),
    "sources commit": dict(about="""
        Only for a legacy whole-commit reference (git:FULL_SHA) in a record's
        sources: it lists the files changed by that commit. Captured file sources
        are refused; read them with sources show, history or diff. To read a listed
        file, pass it with --path to sources show, history, diff or git-check.""",
        examples=(("A legacy whole-commit reference: list its files, then read one",
                   ("richi sources commit --ref entry:task:ABC-123 --source 1",
                    "richi sources show --ref entry:task:ABC-123 --source 1 --path internal/cache.go")),)),
    "sources related": dict(about="""
        Reverse lookup: which records cite this repository path (and commit).""",
        examples=(("", "richi sources related --project service-example --path internal/cache.go"),)),
    "sources impact": dict(about="""
        Matches files changed between --base and --target (or the working tree)
        against record sources. These are direct source associations, not an
        inferred semantic impact.""",
        examples=(("", "richi sources impact --project service-example --base main"),
                  ("Uncommitted work", "richi sources impact --project service-example --base HEAD --worktree"))),
    "sources snapshot": dict(about="""
        Writes a new manifest of the selected records, edges and local source files
        without changing knowledge; compare later with sources check.""",
        examples=(("", "richi sources snapshot --project service-example --output /tmp/service-sources.json"),),
        guide="DIAGNOSTICS.md"),
    "sources check": dict(examples=(("", "richi sources check --manifest /tmp/service-sources.json"),), guide="DIAGNOSTICS.md"),
    "project": dict(about="""
        A project is usually one repository. Registering it lets knowledge be scoped
        to it and lets --project-path verify that a checkout belongs to the
        workspace.""",
        examples=(("", "richi project add /path/to/service"),
                  ("Preview, then register all repositories found",
                   ("richi project scan ~/src", "richi project scan ~/src --apply")),
                  ("Which project is this checkout?", "richi project check . --id service")),
        see=("workspace", "context"), guide="WORKSPACES.md"),
    "project add": dict(about="""
        The ID and name default to the folder name. Registering is explicit;
        Richi never scans repositories in the background.""",
        examples=(("", "richi project add /path/to/service --id service --name 'Example service'"),)),
    "project scan": dict(about="""
        Without --apply only previews. --apply registers the complete discovery
        plan atomically, and only when it has no conflicts.""",
        examples=(("", "richi project scan ~/src"), ("", "richi project scan ~/src --apply --max-depth 2"))),
    "project check": dict(about="""
        Looks only in the selected workspace; linked Git worktrees resolve to their
        registered repository. With --id, it also confirms the path is that
        project. Run it before repository work.""",
        examples=(("", "richi project check /path/to/service --id service"),)),
    "project list": dict(examples=(("", "richi project list"),), guide="CLI.md"),
    "project upsert": dict(about="""
        Fields: id and name (required), repo_path, description, source,
        verified_at. Writing an existing ID replaces its fields.""",
        examples=(("", "richi project upsert --json projects.json"),), guide="CLI.md"),
    "rules": dict(about="""
        Standing rules are the user's own instructions for every relevant chat of a
        workspace: global ones for all its chats, project ones for work in one
        registered project. They are not knowledge, so search never returns them;
        agents load them with rules show and change them only when the user asks.
        A rule may name a skill, for every agent or a different one per agent.""",
        examples=(("Rules that apply here", "richi rules show"),
                  ("Plus one project's rules", "richi --project-path /path/to/service rules show"),
                  ("Keep a rule the user asked for",
                   'richi rules add branch-naming --project service --text "Name branches SE-<number>_<topic>."')),
        see=("skill-sources", "project", "map"), guide="RULES.md"),
    "rules show": dict(about="""
        Prints every rule that applies in full: global rules, then the project's
        with --project-path or --project. When the agent running the command is
        known (detected from its shell, or given with --agent), each rule's
        use_skill is the skill that agent uses whenever the rule applies. A shell
        that carries the signs of two agents names neither: pass --agent. An ID
        that no agent and no rule has is answered with agent_problem. In a
        repository the workspace does not know, the global rules still come back,
        with project_path_problem saying why the project's rules do not.
        invalid_rules names stored rules that cannot be read, and
        unsupported_rules those that need a newer Richi; agents get neither.
        agent_settings_problem says that Richi cannot use the agent settings, all
        or some own agents' (code: damaged, unreadable, newer, large or agents): the
        user's own agents described there are not recognized from their shells
        (pass --agent) and cannot be named in new rules, and next says what to
        tell the user.""",
        examples=(("", "richi rules show"), ("", "richi --project-path /path/to/service rules show"),
                  ("As a particular agent", "richi rules show --agent codex"))),
    "rules review-preferences": dict(about="""
        Shows active preference: entries that still need a decision in this workspace.
        Each candidate includes wording, entry version and all pending scopes in view.
        Continue with --after next_after. For truncated wording, read the original via read_more.
        With --project-path, includes only workspace-wide entries and that project's entries.""",
        examples=(("", "richi rules review-preferences"),)),
    "rules promote": dict(about="""
        After the user reviews a candidate and confirms its wording and scope,
        creates a standing rule and links it to the original entry. --dismiss keeps
        the preference as knowledge. Each linked project is reviewed independently.
        A different entry version requires a fresh review. A previously dismissed scope
        requires new explicit approval and --reconsider-dismissed. A missing or replaced
        rule produces a conflict on retry.""",
        examples=(("", "richi rules promote preference:branch-naming --project example "
                       "--expected-updated-at 2026-01-02T03:04:05.000000Z"),)),
    "rules check": dict(about="""
        Checks a proposed branch name against the global and selected project's explicit
        checks. A violated check exits with code 1 and gives the rule text and expected
        format or forbidden prefix. manual_rules identifies rules to review yourself.
        An unregistered repository uses the workspace's global checks and reports
        project_path_problem. Ambiguous paths and explicit project mismatches are errors.
        An action is never performed by this command.""",
        examples=(("", "richi --project-path /work/example rules check branch-name EX-42_fix"),)),
    "rules list": dict(about="""
        Lists every stored rule of the workspace as it is stored, with where it
        came from, who changed it last and the total size of all rules (also with
        --global or --project); agents load rules in full, so the total is
        limited. agents lists the
        agents a rule may name a skill for, projects the registered projects, and
        invalid_rules the stored rules that cannot be read, with next, the command
        that removes one, where a command can name the row (otherwise an SQLite
        tool removes it).""",
        examples=(("", "richi rules list"), ("", "richi rules list --global"),
                  ("", "richi rules list --project service"))),
    "rules add": dict(about="""
        Creates a rule only when the user explicitly asks for a standing rule, in
        the user's words, one instruction per rule. --skill NAME names a skill for
        every agent, agents added later included; AGENT=NAME replaces it for one
        agent. The same skill given to every agent Richi knows is the skill of the
        action and is stored as the skill for every agent, unless the agent
        settings cannot be used now; an agent named beyond the known ones keeps its
        own skill or none (AGENT=). Write a text that begins with a dash as
        --text="- ...".""",
        examples=(("", 'richi rules add no-agent-prefix --global --text "Never prefix branch names with the agent name."'),
                  ("A skill for every agent",
                   'richi rules add pr-skill --global --text "Use the pr-description skill for pull requests." '
                   "--skill pr-description"),
                  ("A different skill per agent",
                   'richi rules add review-skill --global --text "Use the code review skill for reviews." '
                   "--skill claude=engineering:code-review --skill codex=code-review"))),
    "rules update": dict(about="""
        Changes the text, the skills or the source of a rule; --skill options
        replace all its skills and an empty --skill removes them. Pass the
        updated_at last read: a newer version is a conflict and nothing is
        written.""",
        examples=(("", 'richi rules update branch-naming --expected-updated-at 2026-01-02T03:04:05.000000Z '
                       '--text "Name branches SE-<number>_<topic>."'),
                  ("Remove the rule's skills",
                   "richi rules update pr-skill --expected-updated-at 2026-01-02T03:04:05.000000Z --skill ''"))),
    "rules remove": dict(about="""
        Deletes a rule; agents stop receiving it when they next load the rules.
        Pass the updated_at last read.""",
        examples=(("", "richi rules remove branch-naming --expected-updated-at 2026-01-02T03:04:05.000000Z"),)),
    "rules log": dict(about="""
        Shows the latest changes of rules, newest first: when, which rule, the
        action, who made it (the chat identity of the shell that ran the command,
        or richi map), and the rule before and after. The workspace keeps the
        latest 200 changes; more says whether older ones exist than those shown.
        It is a record for review, not proof of who acted.""",
        examples=(("", "richi rules log"),
                  ("The last changes of one rule", "richi rules log --id branch-naming --entries 5"))),
    "rules clear-log": dict(about="""
        Deletes the recorded rule changes, all of them or those of one rule. The
        log keeps rule texts, removed rules included, so clear it when a rule
        held something that should not stay in the store. The rules themselves
        are not touched. Omit --project-path even with --id because a deleted
        rule ID can be reused in a different project.""",
        examples=(("Everything", "richi rules clear-log"),
                  ("The changes of one rule", "richi rules clear-log --id branch-naming"))),
    "rules apply": dict(about="""
        Takes the same change as rules add, update and remove, as one JSON object:
        action, id and that action's fields (scope, project, text, skill, skills,
        source, check, expected_updated_at). skills maps an agent ID to its skill. Useful
        for text with quotes or line breaks and for programs; the map saves rules
        through it.""",
        examples=(("", "richi rules apply --json rule-change.json"),
                  ("From a program", "richi --compact-json rules apply --json -"))),
    "scenario": dict(about="""
        Reusable multi-step procedures an agent follows in a chat. Richi stores
        the scenario definition (inputs, steps, explicit dependencies, checks),
        tracks run state and leases in schema 4 tables, records each run as a
        task entry tagged scenario-run, and logs every edit and run event in a
        unified journal. Agents propose a matching scenario and ask before
        starting it unless the user requested it explicitly.""",
        examples=(("List scenarios and built-in templates", "richi scenario list"),
                  ("Preview the built-in branch review", "richi scenario plan review --input branch=feature_x"),
                  ("Start a review run in a project",
                   "richi --project-path /path/to/service scenario run review --input branch=feature_x")),
        see=("rules", "brief", "migrate"), guide="SCENARIOS.md"),
    "scenario list": dict(about="""
        Lists stored workspace-wide and project-scoped scenarios along with
        built-in templates. Stored definitions live in metadata and never appear
        in recall or search; corrupt or newer-format definitions are reported
        separately without hiding healthy ones.""",
        examples=(("", "richi scenario list"), ("", "richi scenario list --global"),
                  ("", "richi scenario list --project service"))),
    "scenario templates": dict(about="""
        Lists the read-only synthetic templates bundled with Richi, such as the
        built-in review scenario. Use scenario copy to save an editable copy in
        workspace or project scope.""",
        examples=(("", "richi scenario templates"),)),
    "scenario show": dict(about="""
        Shows one scenario definition in full, resolving project scope first,
        then workspace-wide scope, then built-in templates. When an agent is
        known or passed with --agent, resolved_skills shows the skill chosen for
        each skill step and missing_skills flags any skill the agent lacks.""",
        examples=(("", "richi scenario show review"),
                  ("With explicit agent", "richi scenario show review --agent codex"))),
    "scenario copy": dict(about="""
        Copies a built-in template or existing scenario into workspace-wide or
        project scope so it can be edited with scenario apply. Keeping the same
        ID shadows the broader scope.""",
        examples=(("Into workspace scope", "richi scenario copy review --global"),
                  ("Into one project with a custom ID",
                   "richi scenario copy review --project service --id service-review"))),
    "scenario apply": dict(about="""
        Creates, updates or removes a stored scenario from one JSON change
        object (action: add, update or remove). Updates and removals require
        expected_updated_at for optimistic concurrency.""",
        examples=(("", "richi scenario apply --json change.json"),
                  ("From stdin", "richi --compact-json scenario apply --json -"))),
    "scenario plan": dict(about="""
        Dry-run preview of a scenario: validates supplied inputs, resolves
        skills for the active agent, expands static/known matrices, and renders
        the planned run title without writing to the store.""",
        examples=(("", "richi scenario plan review --input branch=feature_x"),
                  ("", "richi scenario plan review --input branch=feature_x --agent codex"))),
    "scenario run": dict(about="""
        Starts a new run on schema 4: freezes a snapshot of the scenario
        definition, redacts secret inputs so raw secrets are never written to
        SQLite, creates a task entry tagged scenario-run, and executes any
        immediately ready Richi steps. Prefer --secret-env KEY=ENV_VAR for
        secret inputs so secret values stay out of argv.""",
        examples=(("", "richi --project-path /path/to/service scenario run review --input branch=feature_x"),
                  ("With JSON inputs", "richi scenario run review --inputs-json inputs.json"))),
    "scenario next": dict(about="""
        Returns the next ready or already-claimed step for this agent/chat,
        rendering templates and resolving skills. Pass --step STEP to target a
        specific parallel step. With --claim, acquires or renews a 30-minute
        lease on the returned agent step. Prefer --secret-env KEY=ENV_VAR (or
        --secret KEY=VALUE) when a step needs a runtime-only secret input.""",
        examples=(("", "richi scenario next run-review-20261127-0001 --claim"),
                  ("With explicit agent", "richi scenario next run-review-20261127-0001 --agent codex"))),
    "scenario claim": dict(about="""
        Claims one specific ready agent step for this chat with a 30-minute
        lease and returns its rendered step payload, preventing another chat
        from working on the same step at once. Prefer --secret-env KEY=ENV_VAR
        (or --secret KEY=VALUE) when the claimed step needs a runtime-only
        secret input.""",
        examples=(("", "richi scenario claim run-review-20261127-0001 angle.0"),)),
    "scenario release": dict(about="""
        Releases a step lease held by this chat so another chat can claim it.""",
        examples=(("", "richi scenario release run-review-20261127-0001 angle.0"),)),
    "scenario report": dict(about="""
        Submits a JSON report for a step claimed by this chat. Richi validates
        declared outputs and checks (data_required, min_items, exit_code,
        file_exists inside --project-path, evidence_git). A rejected report
        returns reasons and leaves the step claimed.""",
        examples=(("", "richi scenario report run-review-20261127-0001 angle.0 --json report.json"),
                  ("From stdin", "richi --compact-json scenario report run-review-20261127-0001 angle.0 --json -"))),
    "scenario answer": dict(about="""
        Records the human's answer from the chat to a waiting gate or checklist
        step and advances the run.""",
        examples=(("", "richi scenario answer run-review-20261127-0001 approve all --comment 'Looks good'"),)),
    "scenario skip": dict(about="""
        Skips a step with a mandatory reason and advances downstream steps
        (a skipped dependency still satisfies ordering unless downstream `if`
        tests its status).""",
        examples=(("", "richi scenario skip run-review-20261127-0001 angle.1 --reason 'No test changes'"),)),
    "scenario retry": dict(about="""
        Resets a failed or blocked step (or the first failed/blocked step of the
        run) so it can be executed again.""",
        examples=(("", "richi scenario retry run-review-20261127-0001"),
                  ("One specific step", "richi scenario retry run-review-20261127-0001 merge"))),
    "scenario pause": dict(about="""
        Pauses an active scenario run without losing step progress.""",
        examples=(("", "richi scenario pause run-review-20261127-0001"),)),
    "scenario resume": dict(about="""
        Resumes a paused scenario run and advances any ready steps.""",
        examples=(("", "richi scenario resume run-review-20261127-0001"),)),
    "scenario cancel": dict(about="""
        Cancels an active scenario run and releases any claimed step leases.""",
        examples=(("", "richi scenario cancel run-review-20261127-0001"),)),
    "scenario status": dict(about="""
        Prints the full state of one run: status, inputs (secrets redacted),
        linked task entry ID, and every step with its status, lease and summary.""",
        examples=(("", "richi scenario status run-review-20261127-0001"),)),
    "scenario runs": dict(about="""
        Lists runs in this workspace, optionally filtered to active runs, a chat,
        a project or one scenario ID.""",
        examples=(("", "richi scenario runs --active"),
                  ("Runs of this chat", "richi scenario runs --mine --active"))),
    "scenario log": dict(about="""
        Shows the unified scenario journal (newest first): definition edits
        (created, updated, removed) and run events (started, claimed, rejected,
        accepted, answered, skipped, finished).""",
        examples=(("", "richi scenario log"),
                  ("For one run", "richi scenario log --run run-review-20261127-0001 --entries 50"))),
    "scenario hook": dict(about="""
        Handles an agent lifecycle hook for active scenario runs:
        - session-start: summarizes claimed steps, pending gates and agent-pinned ready steps;
        - stop: blocks stopping while this chat holds a claimed step, capped by a Richi-side
          continuation counter (default 5) so hooks never loop forever;
        - pre-compact: prints a compact handoff summary to preserve across context compaction.
        Use --format claude|codex|cursor|antigravity for native hook JSON contracts.""",
        examples=(("Session start context", "richi scenario hook session-start --agent claude"),
                  ("Stop enforcement for Cursor", "richi scenario hook stop --agent cursor --format cursor"),
                  ("Pre-compact handoff", "richi scenario hook pre-compact"))),
    "skill-sources": dict(about="""
        Richi knows which AI agents work on this computer and where each finds
        skills, so that a rule can name a skill for every agent or one per agent.
        You can switch skill sources and add folders for the built-in agents
        (Claude Code, Codex, Cursor and Antigravity) or describe another agent. These settings
        are yours and shared by every workspace. To connect Richi to an agent, see
        richi agents and richi setup.""",
        examples=(("Agents, their skill sources and skill counts", "richi skill-sources list"),
                  ("Describe another agent", 'richi skill-sources add gemini-cli --name "Gemini CLI" --skills ~/.gemini/skills'),
                  ("Stop using one skill source of a built-in agent", "richi skill-sources update claude --disable claude-app")),
        see=("rules", "agents", "map"), guide="AGENT_SKILLS.md"),
    "skill-sources list": dict(about="""
        For each agent: whether it is built in, whether it looks installed, its
        skill sources with their folders and skill counts, and updated_at, the
        version of your settings for it (null when you changed nothing). presets
        lists well-known agents you could add; unknown_agents, settings this
        version keeps as they are and ignores: of an agent that only a newer Richi
        has, or of one agent it cannot use (error says why); it names the first
        20, and unknown_agents_more counts the rest. problem says why the settings
        file as a whole cannot be used, problem_code in one word: damaged,
        unreadable, newer (update Richi) or large (update Richi, or else make
        the file smaller).""",
        examples=(("", "richi skill-sources list"),)),
    "skill-sources add": dict(about="""
        Describes an agent Richi does not have built in. The ID is permanent: rules
        use it in --skill ID=NAME. A skills folder holds one skill per subfolder,
        each with a SKILL.md. The variable lets rules show recognize the agent's
        shells; without one the agent passes --agent ID.""",
        examples=(("", 'richi skill-sources add gemini-cli --name "Gemini CLI" --skills ~/.gemini/skills'),
                  ("Recognize its shells and hide skills only the user invokes",
                   'richi skill-sources add gemini-cli --name "Gemini CLI" --skills ~/.gemini/skills '
                   "--variable GEMINI_CLI=1 --manual-skills front-matter"))),
    "skill-sources update": dict(about="""
        Switches skill sources by their key from skill-sources list, and adds or removes
        skill folders. An own agent may also get a new name, variable or
        manual-skills rule; a built-in agent keeps the definition Richi gives it.
        Pass updated_at as last read; omit it while it is null.""",
        examples=(("Adjust a built-in agent for the first time",
                   "richi skill-sources update claude --disable claude-app --add-skills ~/work/team-skills"),
                  ("Change an own agent",
                   'richi skill-sources update gemini-cli --expected-updated-at 2026-01-02T03:04:05.000000Z --name "Gemini"'),
                  ("Use a source again",
                   "richi skill-sources update claude --expected-updated-at 2026-01-02T03:04:05.000000Z --enable claude-app"))),
    "skill-sources remove": dict(about="""
        Deletes an own agent; rules that name a skill for it keep that name. For a
        built-in agent it drops your adjustments and restores the agent as Richi
        defines it. Skill folders on disk are not touched.""",
        examples=(("", "richi skill-sources remove gemini-cli --expected-updated-at 2026-01-02T03:04:05.000000Z"),)),
    "check": dict(about="""
        Runs SQLite integrity and foreign key checks and reports the schema version
        and search backend. Exits with an error when anything is wrong.""",
        examples=(("", "richi check"), ("Verify a backup", "richi -w default --db '/path/to/backup.sqlite3' check")),
        see=("backup", "punisher")),
    "backup": dict(about="""
        Uses SQLite's backup API, so committed WAL data is included, then checks the
        copy's integrity. Without --output the copy goes to backups/ next to the
        database with a UTC timestamp; existing files are never overwritten, and a
        destination with leftover -wal, -shm or -journal files beside it is refused.
        Captured Git artifacts in artifacts/git/ are not included: copy them
        separately.""",
        examples=(("", "richi backup"), ("", "richi backup --output /path/to/memory-backup.sqlite3"),
                  ("Check the copy", "richi -w default --db '/path/to/memory-backup.sqlite3' check"),
                  ("Put a copy back at a cleared database path",
                   "richi --config '/path/to/config.json' --db '/path/to/memory-backup.sqlite3' backup --output '/path/to/memory.sqlite3'")),
        see=("check", "migrate")),
    "storage": dict(about="""
        stats is read-only. optimize shows a plan; with --apply it makes a verified
        backup first and then maintains indexes for the whole workspace.""",
        examples=(("", "richi storage stats"), ("", "richi storage stats --deep --files"),
                  ("", ("richi storage optimize", "richi storage optimize --apply"))),
        see=("search-index", "backup"), guide="STORAGE.md"),
    "storage stats": dict(about="""
        Measures data, history, indexes, WAL and files of the workspace and reports
        index coverage. It only reads.""",
        examples=(("", "richi storage stats"),
                  ("Text bytes, page attribution and workspace directories", "richi storage stats --deep --files"))),
    "storage optimize": dict(about="""
        Without --apply it only shows the maintenance plan. --apply writes to the
        whole workspace after a verified backup.""",
        examples=(("Preview", "richi storage optimize"), ("Apply after reading the preview", "richi storage optimize --apply"))),
    "search-index": dict(about="""
        An optional index of lookup keys and filter fields that speeds up recall and
        filters. It is derived data: building it never changes knowledge or
        history, and every command also works without it.""",
        examples=(("", "richi search-index status"), ("", "richi search-index build")),
        see=("filters", "storage"), guide="FILTERS.md"),
    "search-index status": dict(examples=(("", "richi search-index status"),)),
    "search-index build": dict(about="""
        Rebuilds lookup keys and filter indexes in one transaction; knowledge and
        history stay unchanged.""",
        examples=(("", "richi search-index build"), ("Then confirm coverage", "richi search-index status"))),
    "migrate": dict(about="""
        Writes a verified copy of the store to backups/ beside it, then upgrades
        schema 1, 2 or 3 to the current schema in one transaction, keeping records,
        links, history and indexes. Before upgrading, migrate checks integrity and foreign keys.
        The result names the copy. On the current schema it returns existing;
        run richi check for a full integrity and foreign key check.

        Upgrades go forward only: older releases may not support the upgraded schema.
        The way back is that copy, which this release still reads.""",
        examples=(("", ("richi migrate", "richi check")),), see=("backup", "check"), guide="GRAPH.md"),
    "help": dict(about="""
        Without arguments lists all commands. With a command and optional
        subcommands it shows the same page as COMMAND --help. Topics explain rules
        shared by many commands; richi help topics lists them.""",
        examples=(("", "richi help"), ("", "richi help recall"), ("", "richi help sources capture"),
                  ("", "richi help filters")), guide="docs/CLI.md"),
    "capabilities": dict(about="""
        Agents use it to find an operation without loading every manual: list shows
        small groups, list --group GROUP their operations with read/write effects,
        and describe ID the current syntax and guide. People usually prefer
        richi help.""",
        examples=(("", "richi capabilities list"), ("", "richi capabilities list --group search"),
                  ("", 'richi capabilities describe "query count"')), guide="CLI.md"),
    "capabilities list": dict(examples=(("Small groups first", "richi capabilities list"),
                                        ("Operations of one group", "richi capabilities list --group search"))),
    "capabilities describe": dict(examples=(("", 'richi capabilities describe "query count"'),)),
}

# Options that mean the same everywhere; an explicit help= always wins.
# {range} becomes the bounds of the option's argument type.
COMMON_HELP = {
    "--include-hypotheses": "Also return hypothesis records (only confirmed by default)",
    "--include-superseded": "Also return superseded records (hidden by default)",
    "--limit": "Maximum number of results, {range} (default: %(default)s)",
    "--max-chars": "Response size budget in characters, {range} (default: %(default)s)",
    "--history": "Include the full revision history",
    "--json": "UTF-8 JSON file, or - for stdin: one object or an array of up to 1000",
}

# Short placeholders instead of argparse's upper-cased option names.
METAVARS = {
    "--project": "ID", "--where": "EXPR", "--limit": "N", "--max-chars": "N", "--offset": "N",
    "--stale-days": "N", "--summary-warn-chars": "N", "--check-limit": "N", "--depth": "N",
    "--max-depth": "N", "--base": "REF", "--target": "REF", "--rev": "REV", "--ref": "REF",
    "--commit": "SHA", "--fields": "LIST", "--after": "REF", "--id": "ID", "--name": "NAME",
    "--description": "TEXT", "--path": "PATH", "--port": "PORT",
}

# Pages of the commands that the installed launcher runs: the runtime only
# lists them, so a direct runtime call points to the launcher's page.
LAUNCHER_PAGE = """
    The installed richi launcher parses and runs this command. Its page lists
    the options and examples:

      richi help %s"""

# The order in which help lists work states; the states come from the code.
WORK_STATE_ORDER = ("planned", "in_progress", "implemented", "merged", "released", "blocked", "done")


def _members(values, order=()):
    """Values in the preferred order, then any others alphabetically."""
    values = set(values)
    return ", ".join([value for value in order if value in values] + sorted(values - set(order)))


def _lists():
    """Enumerations that help text names, read from the code that checks them."""
    from . import graph, memory, query_filters, query_tools
    return {"entry_kinds": _members(memory.KINDS), "work_states": _members(memory.STATES, WORK_STATE_ORDER),
            "entity_kinds": _members(graph.ENTITY_KINDS), "filter_fields": _members(query_filters.FIELDS),
            "select_fields": ", ".join(query_tools.SCALARS)}


def _fill(text, lists):
    """Replace {name} placeholders of _lists(); a line that ends with one is
    wrapped and indented under its label."""
    import re
    import textwrap
    names = "|".join(lists)

    def line(match):
        prefix = match.group(1)
        return textwrap.fill(lists[match.group(2)], style.TEXT_WIDTH if style is not None else 78,
                             initial_indent=prefix, subsequent_indent=" " * len(prefix), break_on_hyphens=False)
    text = re.sub(r"(?m)^([^{}\n]*)\{(%s)\}$" % names, line, text)
    return re.sub(r"\{(%s)\}" % names, lambda match: lists[match.group(1)], text)


def topic(name, topics=None):
    """The text of a help topic of TOPICS, or of another such table."""
    return _fill((TOPICS if topics is None else topics)[name][1], _lists()).lstrip("\n")


def decorate(root):
    """Attach pages, examples and option texts to the parser tree. It runs
    once per tree, when help is printed, a capability is described or the
    documentation is validated; ordinary commands never pay for it."""
    if style is None or root.__dict__.get("_documented"):
        return root
    root._documented = True
    from . import capabilities
    from .cli_parser import LAUNCHER_COMMANDS
    catalog = {row[0]: row[4] for row in capabilities.COMMANDS}
    lists = _lists()
    style.tidy(root)
    for path, parser, summary in style.walk(root):
        for action in parser._actions:
            if not action.option_strings:
                continue
            if action.help is None:
                action.help = next((COMMON_HELP[option] for option in action.option_strings
                                    if option in COMMON_HELP), None)
            if action.help and "{range}" in action.help:
                bounds = getattr(action.type, "bounds", None)
                action.help = (action.help.replace("{range}", "%d-%d" % bounds) if bounds
                               else action.help.replace(", {range}", "").replace("{range}", ""))
            # Choices stay visible as {a,b}; root options name their own values.
            if action.metavar is None and action.nargs != 0 and action.choices is None and parser is not root:
                action.metavar = next((METAVARS[option] for option in action.option_strings
                                       if option in METAVARS), None)
        if not path:
            continue
        key = " ".join(path)
        if path[0] in LAUNCHER_COMMANDS:
            style.document(parser, style.sentence(summary), LAUNCHER_PAGE % key)
            continue
        detail = DETAILS.get(key, {})
        # A page keeps an authored description when there is no summary.
        style.document(parser, style.sentence(summary) or parser.description,
                       about=_fill(detail["about"], lists) if detail.get("about") else None,
                       examples=detail.get("examples", ()), see=detail.get("see", ()),
                       # A cataloged command links the guide the catalog names.
                       guide=detail.get("guide") or catalog.get(key))
    # Subcommands without a guide of their own link their group's.
    style.complete(root)
    return root


def _rows(rows, width=30):
    """Names and descriptions in two columns; a long description wraps
    under its column."""
    import textwrap
    lines = []
    for name, text in rows:
        wrapped = textwrap.wrap(text, (style.TEXT_WIDTH if style is not None else 78) - width,
                                break_on_hyphens=False) or [""]
        head = "  " + (name.ljust(width - 2) if len(name) < width - 2 else name + "\n" + " " * width)
        lines.append(head + wrapped[0])
        lines.extend(" " * width + rest for rest in wrapped[1:])
    return "\n".join(lines)


def _plain(parser):
    """Plain argparse help, used without a compatible help_format."""
    return argparse.ArgumentParser.format_help(parser)


def _summary(parser, line):
    """List summary of the command that a `richi ...` line runs."""
    summary = ""
    for word in line.split()[1:]:
        names = style.summaries(parser)
        if word not in names:
            break
        summary, parser = names[word], style.subcommands(parser).choices[word]
    return summary


def overview(parser):
    """Short orientation printed by a bare `richi`."""
    if style is None:
        return _plain(parser)
    name, (summary, _) = next(iter(TOPICS.items()))
    help_rows = (("richi help", "List all commands with a short explanation"),
                 ("richi help COMMAND", "Explain a command: options, examples, related pages"),
                 ("richi help " + name, summary),
                 ("richi help topics", "Shared rules: " + ", ".join(TOPICS)))
    return "\n".join((
        parser.description,
        "",
        "usage: richi [GLOBAL OPTIONS] COMMAND [ARGS...]",
        "",
        "common commands:",
        _rows([(line, _summary(parser, line)) for line in OVERVIEW_COMMANDS]),
        "",
        "get help:",
        _rows(help_rows),
        "",
        "Every command also accepts --help, for example: " + HELP_EXAMPLE,
        "")) + "\n"


def topics_help():
    return "help topics (richi help TOPIC):\n" + _rows([(name, TOPICS[name][0]) for name in TOPICS], 16) + "\n"


def root_help(parser):
    """`richi help` and `richi --help`: every command, grouped by purpose."""
    if style is None:
        return _plain(parser)
    decorate(parser)
    summaries = style.summaries(parser)
    sections = []
    listed = set()
    for title, names in GROUPS:
        rows = [(name, summaries[name]) for name in names if name in summaries]
        listed.update(name for name, _ in rows)
        if rows:
            sections.append(title + ":\n" + _rows(rows, 16))
    rest = [(name, summaries[name]) for name in summaries if name not in listed]
    if rest:
        sections.append("other commands:\n" + _rows(rest, 16))
    formatter = parser._get_formatter()
    formatter.start_section("global options (place them before COMMAND)")
    formatter.add_arguments(parser._optionals._group_actions)
    formatter.end_section()
    examples = ["examples:"]
    for comment, command in EXAMPLES:
        examples += ["  # " + comment, "  " + command, ""]
    return "\n\n".join([
        parser.description + "\n\n"
        "Richi keeps decisions, investigations, tasks and their evidence in a local\n"
        "SQLite store, links them to projects, code and Git history, and helps people\n"
        "and agents find them again.",
        "usage:\n  richi [GLOBAL OPTIONS] COMMAND [ARGS...]\n  richi help [COMMAND [SUBCOMMAND] | TOPIC]",
        *sections,
        formatter.format_help().strip("\n"),
        topics_help().rstrip("\n"),
        "\n".join(examples).rstrip(),
        "Run 'richi help COMMAND' or 'richi COMMAND --help' for details on a command.\n"
        "Documentation: " + style.guide_url("docs/CLI.md") + _local("docs/CLI.md"),
    ]) + "\n"


def _local(guide):
    """A line with this machine's copy of a guide, or nothing."""
    local = style.local_guide(guide)
    return "\nLocal copy: " + str(local) if local is not None else ""


def correction(value, choices):
    """'. Did you mean ...?' for a mistyped VALUE from the fixed list CHOICES,
    or '', also when no compatible help_format is installed."""
    if style is None:
        return ""
    close = style.did_you_mean(style.suggestions(value, choices))
    return "." + close if close else ""


def page(root, words, error):
    """Help text for `richi help WORDS...`; error(message, target) raises and
    ends the message with the help command to read. -h and --help are
    ignored, as on the launcher's pages."""
    words = [word for word in words if word not in ("-h", "--help")]
    if not words:
        return root_help(root)
    name = words[0]
    if name.startswith("-"):
        error("'richi help' takes command and topic names, not options such as %s" % name, "richi help")
    if name == "topics" or name in TOPICS:
        if len(words) > 1:
            error("'richi help %s' takes no further words" % name, "richi help " + name)
        return topics_help() if name == "topics" else topic(name)
    if style is None:
        return _plain(_plain_find(root, words, error))
    commands = style.subcommands(root)
    if name not in commands.choices:
        close = style.did_you_mean(style.suggestions(name, list(commands.choices) + list(TOPICS) + ["topics"]))
        error("'%s' is not a richi command or help topic.%s" % (name, close), "richi help")
    return style.find(commands.choices[name], words[1:], error).format_help()


def _plain_find(parser, words, error):
    """The parser that WORDS name below PARSER, without help_format."""
    for word in words:
        commands = next((action for action in parser._actions
                         if isinstance(action, argparse._SubParsersAction)), None)
        target = " ".join(["richi", "help"] + parser.prog.split()[1:])
        if word.startswith("-"):
            error("'richi help' takes command and topic names, not options such as %s" % word, target)
        if commands is None:
            error("'%s' has no subcommands" % parser.prog, target)
        if word not in commands.choices:
            error("'%s' is not a %s command" % (word, parser.prog), target)
        parser = commands.choices[word]
    return parser
