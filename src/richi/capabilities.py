"""Small on-demand command catalog; the detailed syntax comes from the live parser."""

import argparse


GROUPS = {
    'search': 'Find relevant knowledge and fetch selected full evidence.',
    'analysis': 'Compute counts, existence, distributions and metadata in SQLite.',
    'research': 'Read task context, explicit tracks, experiments and changes without inferred conclusions.',
    'graph': 'Inspect explicit dependencies and scoped definitions.',
    'git': 'Inspect recorded code, history, changes and attached evidence.',
    'write': 'Save knowledge with provenance and optimistic concurrency.',
    'maintenance': 'Inspect storage and explicitly maintain derived structures.',
}
# id, group, effect, task-oriented summary, canonical detailed guide.
COMMANDS = (
    ('discover', 'search', 'read', 'Find small candidate cards; supports composed --where.', 'RETRIEVAL_TOOLS.md'),
    ('fetch', 'search', 'read', 'Fetch up to 32 exact refs; whole records, explicit missing/oversize/continuation.', 'RETRIEVAL_TOOLS.md'),
    ('recall', 'search', 'read', 'Rank bounded excerpts with graph evidence and optional retrieval diagnostics.', 'RECALL.md'),
    ('search', 'search', 'read', 'Lexical entry search with strict project/metadata filters; full entry payloads.', 'FILTERS.md'),
    ('brief', 'search', 'read', 'Assemble a task overview with optional explicit Git checks and diffs.', 'BRIEF.md'),
    ('task context', 'research', 'read', 'Read bounded context for an exact task, including standalone tasks and shared track evidence.', 'TRACKS.md'),
    ('track summary', 'research', 'read', 'Read a bounded track overview with exact counts, scoped evidence and unknowns.', 'TRACKS.md'),
    ('track changes', 'research', 'read', 'Read ordered membership and research changes using a scope-bound cursor.', 'TRACKS.md'),
    ('track attach', 'research', 'write', 'Attach an existing entry to a track with explicit contribution, sources and revision guard.', 'TRACKS.md'),
    ('track detach', 'research', 'write', 'End explicit track membership with a revision guard; preserve history.', 'TRACKS.md'),
    ('research put', 'research', 'write', 'Atomically save research entries and explicit links with CAS and stable request replay.', 'TRACKS.md'),
    ('query count', 'analysis', 'read', 'Count every matching current record; no lexical shortlist.', 'QUERY_TOOLS.md'),
    ('query exists', 'analysis', 'read', 'Answer whether a matching current record exists; stop at first hit.', 'QUERY_TOOLS.md'),
    ('query facets', 'analysis', 'read', 'Compute exact grouped counts; bounded top groups, NULL preserved.', 'QUERY_TOOLS.md'),
    ('query select', 'analysis', 'read', 'Return selected scalar metadata with stable ref pagination; fetch for evidence.', 'QUERY_TOOLS.md'),
    ('punisher', 'analysis', 'read', 'Inspect stale, conflicting or oversized knowledge; report review candidates.', 'MEMORY_AUDIT.md'),
    ('graph neighbors', 'graph', 'read', 'Read explicit neighbors; compact/full views and bounded graph context.', 'NEIGHBORS.md'),
    ('graph export', 'graph', 'read', 'Export the explicit knowledge graph.', 'GRAPH.md'),
    ('entity get', 'graph', 'read', 'Read an exact entity, optionally its revision history.', 'GRAPH.md'),
    ('edge get', 'graph', 'read', 'Read an exact edge and its provenance.', 'GRAPH.md'),
    ('sources show', 'git', 'read', 'Read recorded file content at its source revision without checkout.', 'GIT.md'),
    ('sources history', 'git', 'read', 'Read bounded file commit history without checkout.', 'GIT.md'),
    ('sources diff', 'git', 'read', 'Compare source bytes with an explicit revision or working tree.', 'GIT.md'),
    ('sources commit', 'git', 'read', 'Read commit metadata and recorded changes.', 'GIT.md'),
    ('sources related', 'git', 'read', 'Find knowledge attached to an exact repository path or commit.', 'GIT.md'),
    ('sources impact', 'git', 'read', 'Find recorded source associations for changed files; not inferred impact.', 'IMPACT.md'),
    ('sources git-check', 'git', 'read', 'Check attached Git evidence against a chosen local state.', 'GIT_CAPTURE.md'),
    ('entry get', 'search', 'read', 'Read one exact whole entry, optionally its revision history.', 'ENTRY_WRITE.md'),
    ('entry put', 'write', 'write', 'Create/replace an entry with full sources and expected_updated_at.', 'ENTRY_WRITE.md'),
    ('entity put', 'write', 'write', 'Create/replace an entity with provenance and optimistic concurrency.', 'GRAPH.md'),
    ('edge put', 'write', 'write', 'Create/replace an explicit sourced edge with optimistic concurrency.', 'GRAPH.md'),
    ('sources capture', 'git', 'write', 'Capture Git-backed evidence into local content-addressed artifacts.', 'GIT_CAPTURE.md'),
    ('sources attach', 'write', 'write', 'Attach captured evidence using the current record version.', 'GIT_CAPTURE.md'),
    ('storage stats', 'maintenance', 'read', 'Measure data/history/index/WAL/file footprint and coverage.', 'STORAGE.md'),
    ('storage optimize', 'maintenance', 'conditional', 'Preview maintenance; --apply writes workspace-wide after a verified backup.', 'STORAGE.md'),
    ('search-index status', 'maintenance', 'read', 'Inspect optional index availability, coverage and fallback records.', 'FILTERS.md'),
    ('search-index build', 'maintenance', 'write', 'Explicitly rebuild derived search indexes; canonical knowledge is preserved.', 'FILTERS.md'),
    ('check', 'maintenance', 'read', 'Check SQLite integrity and foreign keys.', 'DIAGNOSTICS.md'),
    ('backup', 'maintenance', 'write', 'Write a consistent database backup; copy captured Git artifacts separately.', 'CLI.md'),
)


def add_commands(commands, api):
    actions = commands.add_parser('capabilities', help='Discover built-in operations without reading all manuals').add_subparsers(dest='action', required=True)
    listing = actions.add_parser('list', help='List small capability groups; select a group for its commands')
    listing.add_argument('--group', choices=tuple(GROUPS) + ('all',))
    detail = actions.add_parser('describe', help='Read the current syntax for one exact command ID')
    detail.add_argument('id', help='Exact ID from list, quoted when it contains spaces')


def _parser(root, identity, api):
    parser = root
    for token in identity.split():
        actions = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
        if len(actions) != 1 or token not in actions[0].choices:
            raise api.MemoryError('Capability references an unavailable command: ' + identity)
        parser = actions[0].choices[token]
    return parser


def run(args, api):
    if args.action == 'list':
        group = args.group
        if group is None:
            return {'view': 'capabilities', 'groups': [
                {'id': name, 'operations': sum(row[1] == name for row in COMMANDS), 'summary': summary}
                for name, summary in GROUPS.items()],
                'next': ['capabilities list --group GROUP', 'capabilities describe "COMMAND ID"']}
        if group not in (*GROUPS, 'all'):
            raise api.MemoryError('Unknown capability group')
        rows = [row for row in COMMANDS if group == 'all' or row[1] == group]
        return {'view': 'capabilities', 'group': group,
                'operations': [{'id': row[0], 'effect': row[2], 'summary': row[3]} for row in rows],
                'next': 'capabilities describe "COMMAND ID"'}
    if args.action == 'describe':
        row = next((row for row in COMMANDS if row[0] == args.id), None)
        if row is None:
            raise api.MemoryError('Unknown capability ID; use capabilities list --group GROUP')
        parser = _parser(api.parser(), row[0], api)
        return {'view': 'capability', 'id': row[0], 'group': row[1], 'effect': row[2],
                'summary': row[3], 'syntax': parser.format_help(),
                'guide': 'references/' + row[4],
                'context': 'Use the bound workspace and --project-path guard for repository operations. Writes require task authorization.'}
    raise api.MemoryError('Unknown capabilities action')
