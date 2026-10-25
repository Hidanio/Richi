"""Typed research cards, explicit relationships and immutable change notifications.

Canonical content remains in entries/entities/graph_edges and their histories.
The journal holds notifications only: reading a card later returns current state.
"""
import hashlib
import json
import math


ROLE_KINDS = {'track': 'task', 'task': 'task', 'hypothesis': 'investigation',
              'experiment': 'experiment', 'run': 'experiment', 'decision': 'decision'}
ASSESSMENTS = {'untested', 'supported', 'refuted', 'insufficient', 'conflicting'}
OUTCOMES = {'supporting', 'negative', 'inconclusive', 'not_completed'}
APPLICABILITY = {'unknown', 'applicable', 'limited', 'not_applicable'}
TYPED_EDGES = {'in_track', 'pursues_goal', 'tests', 'run_of', 'informs'}
COMMON = {'role', 'conditions', 'applicability', 'correction_reason'}
ROLE_FIELDS = {
    'task': {'motivation', 'expected_outcome', 'success_criteria', 'constraints', 'open_questions', 'direction_changes'},
    'track': {'motivation', 'expected_outcome', 'success_criteria', 'constraints', 'open_questions', 'direction_changes'},
    'hypothesis': {'claim', 'scope', 'expected_effect', 'validation_method', 'assessment', 'assessments'},
    'experiment': {'method', 'baseline', 'variant', 'metrics', 'interpretation_criteria'},
    'run': {'observed_at', 'revisions', 'measurements', 'limitations', 'outcome'},
    'decision': {'action', 'rationale', 'rejected_options', 'revisit_conditions'},
}
OBSERVATION_FIELDS = {'observed_at', 'conditions', 'revisions', 'measurements', 'limitations', 'outcome'}


def enabled(conn):
    return conn.execute('PRAGMA user_version').fetchone()[0] == 3


def require(conn, api):
    if not enabled(conn):
        raise api.MemoryError('Research operations require schema 3; back up and run migrate explicitly')


def revision(conn, reference):
    if not enabled(conn):
        return None
    row = conn.execute('SELECT revision FROM research_revisions WHERE object_ref = ?', (reference,)).fetchone()
    return row[0] if row else 0


def get(conn, identity):
    if not enabled(conn):
        return None
    row = conn.execute('SELECT payload FROM entry_research WHERE entry_id = ?', (identity,)).fetchone()
    return json.loads(row[0]) if row else None


def role(conn, reference):
    if not enabled(conn) or not reference.startswith('entry:'):
        return None
    row = conn.execute('SELECT role FROM entry_research WHERE entry_id = ?', (reference[6:],)).fetchone()
    return row[0] if row else None


def native_edge(conn, record):
    """Legacy free-form kinds opt into constraints through a native endpoint."""
    if not enabled(conn) or not record or record['kind'] not in TYPED_EDGES:
        return False
    start, end = role(conn, record['from_ref']), role(conn, record['to_ref'])
    if record['kind'] == 'informs':
        return start is not None
    return start is not None or end is not None


def check_revision(conn, obj, reference, api, *, required=False):
    if 'expected_revision' not in obj:
        if required:
            raise api.MemoryError('Typed record update requires expected_revision; read the current record')
        return
    expected = obj['expected_revision']
    if type(expected) is not int or expected < 0:
        raise api.MemoryError('expected_revision must be a nonnegative integer')
    if not enabled(conn):
        raise api.MemoryError('expected_revision requires schema 3')
    if expected != revision(conn, reference):
        raise api.MemoryError('Revision conflict for ' + reference + '; read the current record')


def _objects(value, field, api, maximum=100):
    if not isinstance(value, list) or len(value) > maximum or any(not isinstance(x, dict) for x in value):
        raise api.MemoryError(field + ' must be an array of at most ' + str(maximum) + ' objects')
    return value


def validate(conn, value, kind, api, *, old=None, bundle=False):
    require(conn, api)
    if not isinstance(value, dict):
        raise api.MemoryError('research must be an object; removing a native role is not supported')
    selected = api.enum(value.get('role'), 'research.role', set(ROLE_KINDS))
    if ROLE_KINDS[selected] != kind:
        raise api.MemoryError('research role ' + selected + ' requires entry kind ' + ROLE_KINDS[selected])
    if old and old['role'] != selected:
        raise api.MemoryError('Changing an existing research role is not supported')
    api.fields(value, COMMON | ROLE_FIELDS[selected])
    result = {'role': selected,
              'applicability': api.enum(value.get('applicability', 'unknown'), 'research.applicability', APPLICABILITY)}
    for key in ('conditions', 'correction_reason'):
        if key in value:
            result[key] = api.string(value[key], 'research.' + key)
    def text(key):
        result[key] = api.string(value.get(key), 'research.' + key)
    def strings(key):
        result[key] = api.string_list(value.get(key, []), 'research.' + key)
    if selected in {'task', 'track'}:
        motivation = value.get('motivation')
        if not isinstance(motivation, dict):
            raise api.MemoryError('research.motivation must contain text and sources')
        api.fields(motivation, {'text', 'sources'})
        result['motivation'] = {'text': api.string(motivation.get('text'), 'motivation.text'),
                                'sources': api.sources(motivation.get('sources', []))}
        if not result['motivation']['sources']:
            raise api.MemoryError('Task/track motivation requires at least one source')
        text('expected_outcome')
        for key in ('success_criteria', 'constraints', 'open_questions'):
            strings(key)
        result['direction_changes'] = []
        for item in _objects(value.get('direction_changes', []), 'direction_changes', api):
            api.fields(item, {'changed_at', 'reason', 'sources', 'expected_outcome'})
            change = {key: api.string(item.get(key), 'direction_changes.' + key)
                      for key in ('reason', 'expected_outcome')}
            change['changed_at'] = api.timestamp(item.get('changed_at'), 'direction_changes.changed_at')
            change['sources'] = api.sources(item.get('sources', []))
            if not change['changed_at'] or not change['sources']:
                raise api.MemoryError('Direction changes require changed_at and sources')
            result['direction_changes'].append(change)
        if old and result['motivation'] != old['motivation']:
            raise api.MemoryError('Original motivation is historical; record a direction change instead')
        if old and result['direction_changes'][:len(old.get('direction_changes', []))] != old.get('direction_changes', []):
            raise api.MemoryError('Direction changes are append-only within a card')
        if old and result['expected_outcome'] != old['expected_outcome']:
            if len(result['direction_changes']) <= len(old.get('direction_changes', [])):
                raise api.MemoryError('Changing expected_outcome requires a sourced direction change')
            if result['direction_changes'][-1]['expected_outcome'] != result['expected_outcome']:
                raise api.MemoryError('Latest direction change must match the current expected_outcome')
    elif selected == 'hypothesis':
        for key in ('claim', 'scope', 'expected_effect', 'validation_method'):
            text(key)
        result['assessment'] = api.enum(value.get('assessment', 'untested'), 'research.assessment', ASSESSMENTS)
        result['assessments'] = []
        for item in _objects(value.get('assessments', []), 'assessments', api):
            api.fields(item, {'assessment', 'conditions', 'evidence_refs'})
            assessment = {'assessment': api.enum(item.get('assessment'), 'assessment', ASSESSMENTS),
                          'conditions': api.string(item.get('conditions'), 'assessment.conditions'),
                          'evidence_refs': api.string_list(item.get('evidence_refs', []), 'evidence_refs')}
            if assessment['assessment'] != 'untested' and not assessment['evidence_refs']:
                raise api.MemoryError('A scoped assessment requires evidence_refs')
            for reference in assessment['evidence_refs']:
                api.graph.ref(reference, api)
                if not bundle:
                    api.graph.require_node(conn, reference, api)
            result['assessments'].append(assessment)
        if result['assessment'] != 'untested' and not result['assessments']:
            raise api.MemoryError('An assessed hypothesis requires scoped assessments with evidence')
        if result['assessment'] != 'untested' and not any(
                item['assessment'] != 'untested' and item['evidence_refs'] and
                (result['assessment'] in {'insufficient', 'conflicting'} or
                 item['assessment'] == result['assessment']) for item in result['assessments']):
            raise api.MemoryError('The hypothesis assessment must have matching scoped evidence')
    elif selected == 'experiment':
        text('method')
        for key in ('baseline', 'variant'):
            if key in value:
                text(key)
        for key in ('metrics', 'interpretation_criteria'):
            strings(key)
    elif selected == 'run':
        if old is None and not bundle:
            raise api.MemoryError('Create a run and its run_of relationship atomically with research put')
        result['observed_at'] = api.timestamp(value.get('observed_at'), 'research.observed_at')
        if result['observed_at'] is None:
            raise api.MemoryError('A run requires observed_at')
        text('conditions')
        result['outcome'] = api.enum(value.get('outcome'), 'research.outcome', OUTCOMES)
        result['revisions'] = []
        for item in _objects(value.get('revisions', []), 'revisions', api):
            api.fields(item, {'reference', 'revision'})
            result['revisions'].append({key: api.string(item.get(key), 'revisions.' + key)
                                        for key in ('reference', 'revision')})
        result['measurements'] = []
        for item in _objects(value.get('measurements', []), 'measurements', api):
            api.fields(item, {'name', 'value', 'unit'})
            number = item.get('value')
            try:
                finite = type(number) in (int, float) and math.isfinite(number)
            except OverflowError:
                finite = False
            if not finite:
                raise api.MemoryError('measurement.value must be a finite number')
            result['measurements'].append({'name': api.string(item.get('name'), 'measurement.name'),
                                           'value': number, 'unit': api.string(item.get('unit'), 'measurement.unit')})
        strings('limitations')
        if result['outcome'] in {'supporting', 'negative'} and not result['measurements']:
            raise api.MemoryError('Supporting/negative outcomes require measurements with units')
        if old and any(result.get(key) != old.get(key) for key in OBSERVATION_FIELDS):
            if not result.get('correction_reason'):
                raise api.MemoryError('Changing an observation requires correction_reason; add a new run for new observations')
    else:
        for key in ('action', 'rationale'):
            text(key)
        for key in ('rejected_options', 'revisit_conditions'):
            strings(key)
    return result


def store(conn, identity, payload, api):
    if payload is None:
        return
    values = (identity, payload['role'], api.dumps(payload), *(payload.get(key) for key in
              ('assessment', 'outcome', 'applicability', 'observed_at')))
    conn.execute('INSERT INTO entry_research(entry_id,role,payload,assessment,outcome,applicability,observed_at) '
                 'VALUES (?,?,?,?,?,?,?) ON CONFLICT(entry_id) DO UPDATE SET '
                 'role=excluded.role,payload=excluded.payload,assessment=excluded.assessment,outcome=excluded.outcome,'
                 'applicability=excluded.applicability,observed_at=excluded.observed_at', values)


def validate_edge(conn, record, api, *, old=None, bundle=False):
    if record['kind'] not in TYPED_EDGES and (not old or old['kind'] not in TYPED_EDGES):
        return
    if not enabled(conn):
        return  # Preserve schema-2 free-form graph behavior until explicit migration.
    if not native_edge(conn, record) and not native_edge(conn, old):
        return  # Legacy graph kinds were unrestricted, including these names.
    kind, start, end = record['kind'], record['from_ref'], record['to_ref']
    start_role, end_role = role(conn, start), role(conn, end)
    # informs predates native research and remains a free-form graph relation
    # when neither endpoint has opted in to this typed model.
    if kind == 'informs' and start_role is None and not native_edge(conn, old):
        return
    if old and (old['kind'], old['from_ref'], old['to_ref']) != (record['kind'], record['from_ref'], record['to_ref']):
        raise api.MemoryError('Typed relationship identity cannot change; detach or add another relationship')
    valid = False
    if kind == 'in_track':
        valid = start.startswith('entry:') and end_role == 'track' and start != end and start_role != 'track'
    elif kind == 'pursues_goal':
        goal = conn.execute('SELECT kind FROM entities WHERE id = ?', (end[7:],)).fetchone() if end.startswith('entity:') else None
        valid = start_role == 'track' and goal is not None and goal[0] == 'document'
    elif kind == 'tests':
        valid = start_role == 'experiment' and end_role == 'hypothesis'
    elif kind == 'run_of':
        valid = start_role == 'run' and end_role == 'experiment'
    elif kind == 'informs':
        target = conn.execute('SELECT kind FROM entries WHERE id = ?', (end[6:],)).fetchone() if end.startswith('entry:') else None
        valid = start_role in {'hypothesis', 'experiment', 'run', 'decision'} and target is not None and target[0] == 'task' and end_role != 'track'
    if not valid:
        raise api.MemoryError('Invalid endpoints for typed relationship ' + kind)
    if kind == 'in_track' and not record['description'].strip():
        raise api.MemoryError('Track membership requires an explicit contribution in description')
    if record['knowledge_state'] != 'superseded':
        same = conn.execute('SELECT id FROM graph_edges WHERE kind=? AND from_ref=? AND to_ref=? AND id<>? '
                            "AND knowledge_state <> 'superseded'", (kind, start, end, record['id'])).fetchone()
        if same:
            raise api.MemoryError('An active typed relationship already exists: ' + same[0])
    if kind == 'run_of' and not bundle:
        count = conn.execute("SELECT count(*) FROM graph_edges WHERE kind='run_of' AND from_ref=? "
                             "AND knowledge_state <> 'superseded' AND id<>?", (start, record['id'])).fetchone()[0]
        if count + (record['knowledge_state'] != 'superseded') != 1:
            raise api.MemoryError('A run requires exactly one active run_of relationship')


def track_scopes(conn, reference):
    """Fixed semantic dependencies, never generic/project graph traversal."""
    if not enabled(conn):
        return set()
    subjects = {reference}
    for row in conn.execute("SELECT g.to_ref FROM graph_edges g JOIN entry_research r ON 'entry:'||r.entry_id=g.from_ref "
                            "WHERE g.from_ref=? AND ((g.kind='run_of' AND r.role='run') OR "
                            "(g.kind='informs' AND r.role IN ('hypothesis','experiment','run','decision'))) "
                            "AND g.knowledge_state <> 'superseded' UNION SELECT g.from_ref FROM graph_edges g "
                            "JOIN entry_research r ON 'entry:'||r.entry_id=g.from_ref "
                            "WHERE g.to_ref=? AND ((g.kind='tests' AND r.role='experiment') OR "
                            "(g.kind='pursues_goal' AND r.role='track')) AND g.knowledge_state <> 'superseded'",
                            (reference, reference)):
        subjects.add(row[0])
    # A shared experiment can be linked to a task instead of being a direct
    # track member. Propagate its runs/hypotheses through this one named path.
    for subject in tuple(subjects):
        if role(conn, subject) == 'experiment':
            subjects.update(row[0] for row in conn.execute(
                "SELECT g.to_ref FROM graph_edges g JOIN entries e ON g.to_ref='entry:'||e.id "
                "WHERE g.from_ref=? AND g.kind='informs' AND g.knowledge_state <> 'superseded' "
                "AND e.kind='task'", (subject,)))
    scopes = set()
    for subject in subjects:
        if role(conn, subject) == 'track':
            scopes.add(subject[6:])
        scopes.update(row[0] for row in conn.execute(
            "SELECT r.entry_id FROM graph_edges g JOIN entry_research r ON g.to_ref='entry:'||r.entry_id "
            "AND r.role='track' WHERE g.from_ref=? AND g.kind='in_track' "
            "AND g.knowledge_state <> 'superseded'", (subject,)))
    return scopes


def edge_scopes(conn, record):
    if not record:
        return set()
    scopes = track_scopes(conn, record['from_ref']) | track_scopes(conn, record['to_ref'])
    if record['kind'] == 'in_track' and role(conn, record['to_ref']) == 'track':
        scopes.add(record['to_ref'][6:])
    return scopes


def validate_neighbors(conn, identity, api):
    """Role promotion must not turn old free-form edges into invalid typed links."""
    reference = 'entry:' + identity
    for row in conn.execute('SELECT * FROM graph_edges WHERE from_ref=? OR to_ref=?', (reference, reference)):
        edge = dict(row)
        validate_edge(conn, edge, api, old=edge, bundle=True)


def notify(conn, reference, event_kind, recorded_at, scopes, api):
    if not enabled(conn):
        return
    current = revision(conn, reference) + 1
    conn.execute('INSERT INTO research_revisions(object_ref,revision) VALUES (?,?) '
                 'ON CONFLICT(object_ref) DO UPDATE SET revision=excluded.revision', (reference, current))
    seq = conn.execute('INSERT INTO research_events(object_ref,revision,event_kind,recorded_at) VALUES (?,?,?,?)',
                       (reference, current, event_kind, recorded_at)).lastrowid
    conn.executemany('INSERT INTO research_event_tracks(seq,track_id) VALUES (?,?)',
                     ((seq, track) for track in sorted(scopes)))


def write(conn, payload, api):
    """Atomic bundle; immutable request receipt makes retries independent of CAS."""
    require(conn, api)
    if not isinstance(payload, dict):
        raise api.MemoryError('research put expects one object')
    api.fields(payload, {'request_id', 'entries', 'edges'})
    request_id = api.identifier(payload.get('request_id'), 'request_id')
    entries = _objects(payload.get('entries', []), 'entries', api)
    edges = _objects(payload.get('edges', []), 'edges', api)
    if not entries and not edges:
        raise api.MemoryError('research put requires at least one entry or edge')
    for family, items in (('entry', entries), ('edge', edges)):
        ids = [api.identifier(item.get('id')) for item in items]
        if len(ids) != len(set(ids)):
            raise api.MemoryError('Duplicate ' + family + ' IDs in one research bundle')
    digest = hashlib.sha256(api.dumps(payload).encode('utf-8')).hexdigest()
    if not conn.in_transaction:
        with api.transaction(conn):
            return write(conn, payload, api)
    conn.execute('SAVEPOINT research_bundle')
    try:
        previous = conn.execute('SELECT payload_hash,response FROM research_requests WHERE request_id=?', (request_id,)).fetchone()
        if previous:
            if previous[0] != digest:
                raise api.MemoryError('request_id already used with a different payload')
            response = json.loads(previous[1])
            response['replayed'] = True
            conn.execute('RELEASE research_bundle')
            return response
        start_seq = conn.execute('SELECT coalesce(max(seq),0) FROM research_events').fetchone()[0]
        response = {'request_id': request_id, 'replayed': False,
                    'entries': [api.entry_put(conn, item, _research_bundle=True) for item in entries],
                    'edges': [api.graph.put(conn, item, 'edge', api, _research_bundle=True) for item in edges]}
        # Validate only touched runs, plus endpoints of run_of changes, after all writes.
        touched = {item['id'] for item in entries}
        touched.update(item['from_ref'][6:] for item in edges if item.get('kind') == 'run_of')
        for identity in touched:
            details = get(conn, identity)
            if not details:
                continue
            validate_neighbors(conn, identity, api)
            if details['role'] == 'run':
                count = conn.execute("SELECT count(*) FROM graph_edges WHERE from_ref=? AND kind='run_of' "
                                     "AND knowledge_state <> 'superseded'", ('entry:' + identity,)).fetchone()[0]
                if count != 1:
                    raise api.MemoryError('A run requires exactly one active run_of relationship')
            for item in details.get('assessments', []):
                for reference in item['evidence_refs']:
                    api.graph.require_node(conn, reference, api)
        # New cards are recorded before their required relationships. Scope
        # those notifications against the final graph within the same commit.
        # Existing scope rows preserve before-state evidence such as detaches.
        for event in conn.execute('SELECT seq,object_ref FROM research_events WHERE seq>? ORDER BY seq', (start_seq,)).fetchall():
            reference = event['object_ref']
            if reference.startswith('edge:'):
                edge = conn.execute('SELECT * FROM graph_edges WHERE id=?', (reference[5:],)).fetchone()
                scopes = edge_scopes(conn, dict(edge)) if edge else set()
            else:
                scopes = track_scopes(conn, reference)
            conn.executemany('INSERT OR IGNORE INTO research_event_tracks(seq,track_id) VALUES (?,?)',
                             ((event['seq'], track) for track in scopes))
        conn.execute('INSERT INTO research_requests(request_id,payload_hash,response) VALUES (?,?,?)',
                     (request_id, digest, api.dumps(response)))
        conn.execute('RELEASE research_bundle')
        return response
    except BaseException:
        conn.execute('ROLLBACK TO research_bundle')
        conn.execute('RELEASE research_bundle')
        raise
