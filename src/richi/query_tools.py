"""Exact metadata operations with bounded results; no raw SQL or lexical shortlist."""

from . import query_filters


SCALARS = ('id', 'kind', 'title', 'knowledge_state', 'work_state', 'jira_key',
           'created_at', 'updated_at', 'verified_at')
GROUP_FIELDS = ('family', 'kind', 'knowledge_state', 'work_state', 'jira_key')
DEFAULT_FIELDS = 'ref,kind,title,knowledge_state,work_state,verified_at'


def add_commands(commands, api):
    actions = commands.add_parser('query', help='Exact counts, existence, groups and metadata pages; no raw SQL').add_subparsers(dest='action', required=True)
    for name in ('count', 'exists', 'facets', 'select'):
        command = actions.add_parser(name)
        command.add_argument('--family', choices=('entry', 'entity', 'all'), default='entry')
        command.add_argument('--project', help='Strict justified project membership')
        command.add_argument('--where', help='Composed metadata predicate; same grammar as recall --where')
        command.add_argument('--include-hypotheses', action='store_true')
        command.add_argument('--include-superseded', action='store_true')
        command.add_argument('--max-chars', type=api.compact.max_chars, default=8000)
        if name in ('facets', 'select'):
            command.add_argument('--limit', type=api.limit, default=20)
        if name == 'facets':
            command.add_argument('--by', action='append', required=True, choices=GROUP_FIELDS,
                                 help='Group by up to three scalar fields; repeatable')
        if name == 'select':
            command.add_argument('--fields', default=DEFAULT_FIELDS,
                                 help='Comma-separated scalar fields; ref is always included. Full evidence: fetch.')
            command.add_argument('--after', help='Continue after this exact qualified ref; each page uses a new snapshot')


def _expression(family, field):
    if field == 'family':
        return "'" + family + "'"
    if field == 'ref':
        return "'" + family + ":' || e.id"
    if family == 'entity' and field in query_filters.ENTITY_MISSING:
        return 'NULL'
    return 'e.' + field


def _selection(conn, args, api):
    parsed = query_filters.parse(args.where, api)
    parsed.prepare(conn)
    project_filter = None
    if args.project is not None:
        api.identifier(args.project, 'project')
        project_filter = query_filters.parse("project = '" + args.project.replace("'", "''") + "'", api)
        project_filter.prepare(conn)
    states = ['confirmed']
    if args.include_hypotheses:
        states.append('hypothesis')
    if args.include_superseded:
        states.append('superseded')
    families = ['entry', 'entity'] if args.family == 'all' else [args.family]
    has_entities = conn.execute("SELECT 1 FROM sqlite_master WHERE 1=1 AND type = 'table' AND name = 'entities'").fetchone()
    if 'entity' in families and not has_entities:
        if args.family == 'entity':
            api.graph.require_v2(conn, api)
        families.remove('entity')
    selections = []
    for family in families:
        predicate, params = parsed.sql(family)
        where = 'e.knowledge_state IN (' + ','.join('?' for _ in states) + ') AND ' + predicate
        params = states + params
        if project_filter is not None:
            scope, values = project_filter.sql(family)
            where += ' AND ' + scope
            params += values
        selections.append((family, 'entries' if family == 'entry' else 'entities', where, params))
    scope = {'families': families, 'knowledge_states': states, 'filter_applied': bool(parsed)}
    if args.project is not None:
        scope['project'] = args.project
    return selections, scope


def _measure(response, api):
    for _ in range(8):
        size = len(api.output.rendered(response))
        if response['budget']['output_chars'] == size:
            return size
        response['budget']['output_chars'] = size
    return len(api.output.rendered(response))


def _ref(value, api):
    family, separator, identity = api.string(value, 'after').partition(':')
    if not separator or family not in ('entry', 'entity'):
        raise api.MemoryError('after must be entry:<id> or entity:<id>')
    api.identifier(identity, 'after ID')
    return value


def _fields(value, api):
    fields = ['ref']
    value = api.string(value, 'fields')
    if len(value) > 1000:
        raise api.MemoryError('fields must not exceed 1000 characters')
    for field in value.split(','):
        field = field.strip()
        if field not in ('ref',) + SCALARS:
            raise api.MemoryError('Unknown query field: ' + field + '; use scalar metadata or fetch whole records')
        if field not in fields:
            fields.append(field)
    return fields


def _union(selections, fields, *, maximum=None, after=None, page_limit=None):
    statements, parameters = [], []
    for family, table, where, values in selections:
        columns = []
        column_parameters = []
        oversized, oversized_parameters = [], []
        for field in fields:
            expression = _expression(family, field)
            if maximum is not None:
                # BLOB length preserves embedded NULs; substring(TEXT) would silently cut them.
                probe = 'LENGTH(CAST(' + expression + ' AS BLOB)) > ?'
                oversized.append(probe)
                oversized_parameters.append(maximum * 4)
                expression = 'CASE WHEN ' + probe + ' THEN NULL ELSE ' + expression + ' END'
                column_parameters.append(maximum * 4)
            columns.append(expression + ' AS ' + field)
        if oversized:
            columns.append('(' + ' OR '.join(oversized) + ') AS _oversized')
            column_parameters.extend(oversized_parameters)
        if after is not None:
            after_family, _, after_id = after.partition(':')
            if family < after_family:
                continue
            if family == after_family:
                where += ' AND e.id COLLATE BINARY > ?'
                values = values + [after_id]
        statement = 'SELECT ' + ', '.join(columns) + ' FROM ' + table + ' AS e WHERE 1=1 AND ' + where
        if page_limit is not None:
            # Allow native-ID range seeks and bound each family before the final merge.
            # SQLite may choose a selective filter index plus an internal sort.
            statement = 'SELECT * FROM (' + statement + ' ORDER BY e.id COLLATE BINARY LIMIT ?)'
            values = values + [page_limit]
        statements.append(statement)
        parameters.extend(column_parameters + values)
    return ' UNION ALL '.join(statements), parameters


def _select(conn, args, selections, response, api):
    fields = _fields(args.fields, api)
    after = _ref(args.after, api) if args.after is not None else None
    union, params = _union(selections, fields, maximum=args.max_chars, after=after, page_limit=args.limit + 1)
    if not union:
        response.update(columns=fields, rows=[], oversized=[], has_more=False, next_after=None,
                        full_records=False, returned=0)
        _measure(response, api)
        return response
    cursor = conn.execute('SELECT * FROM (' + union + ') ORDER BY ref COLLATE BINARY LIMIT ?', params + [args.limit + 1])
    response.update(columns=fields, rows=[], oversized=[], has_more=False, next_after=None,
                    full_records=False, returned=0)
    processed = 0
    last = None
    try:
        for row in cursor:
            if processed == args.limit:
                response['has_more'] = True
                break
            values = list(row)[:-1]
            oversized = row[-1]
            reference = values[0]
            response.update(has_more=False, next_after=reference)
            response['rows'].append(values)
            response['returned'] = len(response['rows'])
            fits = not oversized and _measure(response, api) <= args.max_chars
            if not fits:
                response['rows'].pop()
                response['returned'] = len(response['rows'])
                response['has_more'] = True
                if response['rows'] or response['oversized']:
                    break
                # Do not silently truncate a field or skip an unreported record.
                response['oversized'].append({'ref': reference, 'read_more': {'args': ['fetch', '--ref', reference]}})
            processed += 1
            last = reference
            response['has_more'] = True
        else:
            response['has_more'] = False
    finally:
        cursor.close()
    response['next_after'] = last if response['has_more'] else None
    response['returned'] = len(response['rows'])
    response['budget']['output_chars'] = 0
    if _measure(response, api) > args.max_chars:
        raise api.MemoryError('Query metadata exceeds output budget; increase --max-chars')
    return response


def _facets(conn, args, selections, response, api):
    fields = list(dict.fromkeys(args.by))
    if not 1 <= len(fields) <= 3 or any(field not in GROUP_FIELDS for field in fields):
        raise api.MemoryError('facets accepts one to three distinct scalar grouping fields')
    union, params = _union(selections, fields)
    groups = ', '.join(fields)
    order = ', '.join(field + ' COLLATE BINARY' for field in fields)
    grouped = ('SELECT ' + groups + ', COUNT(*) AS count FROM (' + union + ') GROUP BY ' +
               groups + ' ORDER BY count DESC, ' + order + ' LIMIT ?')
    # Group on complete keys in SQLite, but never hydrate an unbounded key into Python.
    probes = ['LENGTH(CAST(' + field + ' AS BLOB)) > ?' for field in fields]
    projected = ['CASE WHEN ' + probe + ' THEN NULL ELSE ' + field + ' END AS ' + field
                 for field, probe in zip(fields, probes)]
    bounded = ('SELECT ' + ', '.join(projected) + ', count, (' + ' OR '.join(probes) +
               ') AS _oversized FROM (' + grouped + ') AS grouped ORDER BY count DESC, ' +
               ', '.join('grouped.' + field + ' COLLATE BINARY' for field in fields))
    caps = [args.max_chars * 4] * len(fields)
    cursor = conn.execute(bounded, caps + caps + params + [args.limit + 1])
    response.update(columns=fields + ['count'], rows=[], has_more=False, counts_exact=True, returned=0)
    try:
        for row in cursor:
            if len(response['rows']) == args.limit:
                response['has_more'] = True
                break
            if row[-1]:
                raise api.MemoryError('A facet key exceeds the output budget; narrow --where or increase --max-chars')
            response['rows'].append(list(row)[:-1])
            response['returned'] = len(response['rows'])
            response['has_more'] = False  # Reserve the longer boolean representation.
            if _measure(response, api) > args.max_chars:
                response['rows'].pop()
                response['has_more'] = True
                break
        else:
            response['has_more'] = False
    finally:
        cursor.close()
    response['returned'] = len(response['rows'])
    if _measure(response, api) > args.max_chars:
        raise api.MemoryError('Query metadata exceeds output budget; increase --max-chars')
    return response


def run(conn, args, api):
    if args.action not in ('count', 'exists', 'facets', 'select'):
        raise api.MemoryError('Unknown query operation')
    if args.family not in ('entry', 'entity', 'all'):
        raise api.MemoryError('Unknown query family')
    if not isinstance(args.max_chars, int) or not 2000 <= args.max_chars <= 100000:
        raise api.MemoryError('max_chars must be between 2000 and 100000')
    if args.action in ('facets', 'select') and (not isinstance(args.limit, int) or not 1 <= args.limit <= 200):
        raise api.MemoryError('limit must be between 1 and 200')
    if not conn.in_transaction:
        with api.transaction(conn, write=False):
            return run(conn, args, api)
    selections, scope = _selection(conn, args, api)
    response = {'view': 'query', 'operation': args.action, 'scope': scope,
                'budget': {'max_chars': args.max_chars, 'output_chars': 0}}
    if args.action in ('count', 'exists'):
        value = 0
        for _, table, where, params in selections:
            projection = 'COUNT(*)' if args.action == 'count' else '1'
            suffix = '' if args.action == 'count' else ' LIMIT 1'
            row = conn.execute('SELECT ' + projection + ' FROM ' + table + ' AS e WHERE 1=1 AND ' + where + suffix, params).fetchone()
            if args.action == 'exists':
                if row is not None:
                    value = True
                    break
            else:
                value += row[0]
        response.update({args.action: bool(value) if args.action == 'exists' else value, 'exact': True})
        _measure(response, api)
        return response
    return (_select if args.action == 'select' else _facets)(conn, args, selections, response, api)
