#!/usr/bin/env python3
"""Frozen synthetic, installed-package track evaluation. Never opens a live store.

Full runs require >=30 fresh-process observations per scenario and arm. Smoke
runs are explicitly marked and cannot be presented as performance evidence.
"""
import argparse
from contextlib import closing
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
FIXTURE = HERE.parent / 'tests/fixtures/tracks.json'
BASELINE = HERE / 'track_baseline.py'
BASELINE_REVISION = '2084321'
SCENARIOS = ('context', 'standalone', 'strict', 'broad', 'aggregates', 'changes')
TRACK = 'entry:track:reliability'


def render(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n'


def digest(value):
    return hashlib.sha256(render(value).encode()).hexdigest()


def environment(root):
    # Synthetic fixture setup is explicitly isolated from all user bindings.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('RICHI_', 'PYTHON')) and key != 'CODEX_THREAD_ID'}
    home = Path(root) / 'synthetic-home'
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'), XDG_DATA_HOME=str(home / '.local/share'))
    return env


def summary(samples):
    return {'samples_ms': samples, 'n': len(samples), 'p50_ms': statistics.median(samples),
            'p95_ms': sorted(samples)[math.ceil(.95 * len(samples)) - 1],
            'min_ms': min(samples), 'max_ms': max(samples), 'p95_method': 'nearest rank'}


def command(python, database, args, *, stdin=None, script=None):
    invocation = [str(python), '-I', '-B']
    invocation += [str(script)] if script else ['-m', 'richi', '--config', str(database.parent / 'config.json'),
                                              '--db', str(database), '--compact-json']
    started = time.perf_counter_ns()
    proc = subprocess.run(invocation + args, input=stdin, capture_output=True, text=True,
                          encoding='utf-8', timeout=600, env=environment(database.parent))
    elapsed = (time.perf_counter_ns() - started) / 1e6
    if proc.returncode or proc.stderr:
        raise RuntimeError(' '.join(invocation + args) + '\n' + (proc.stderr or proc.stdout))
    return json.loads(proc.stdout), proc.stdout, elapsed


def worker(python, database, action, **values):
    return command(python, database, ['--worker'], script=Path(__file__).resolve(),
                   stdin=render(dict(action=action, database=str(database), **values)))[0]


def corpus(fixture, size, native):
    if size < len(fixture['entries']):
        raise ValueError('Corpus scale cannot discard frozen acceptance facts')
    data = copy.deepcopy(fixture)
    for index in range(size - len(data['entries'])):
        data['entries'].append({'id': 'background:%05d' % index, 'kind': 'note',
            'title': 'Unrelated same-project background %05d' % index,
            'summary': 'Synthetic unrelated engineering context; no explicit track membership. ' * 4,
            'project_ids': ['relay' if index % 2 == 0 else 'ledger'], 'knowledge_state': 'confirmed',
            'work_state': 'done', 'sources': [{'reference': 'https://example.test/background/%d' % index}],
            'verified_at': '2026-10-24', 'tags': ['background'], 'aliases': []})
    if not native:
        for item in data['entries']:
            item.pop('research', None)
    return data


def native_arguments(scenario, cursor=None, maximum=32000):
    if scenario in ('context', 'standalone'):
        return ['task', 'context', 'entry:task:20' if scenario == 'standalone' else 'entry:task:01',
                '--max-chars', str(maximum), '--limit', '100']
    if scenario == 'changes':
        return ['track', 'changes', TRACK, '--max-chars', str(maximum), '--limit', '200'] + (['--cursor', cursor] if cursor else [])
    args = ['query', 'facets' if scenario == 'aggregates' else 'select',
            '--max-chars', str(maximum), '--limit', '200' if scenario == 'aggregates' else '20',
            '--include-hypotheses', '--include-superseded']
    args += ['--project', 'relay'] if scenario == 'broad' else ['--track', TRACK]
    if scenario == 'aggregates':
        args += ['--by', 'kind', '--by', 'work_state']
    return args


def baseline_metadata_arguments(scenario, maximum=32000):
    args = native_arguments(scenario, maximum=maximum)
    if '--track' in args:
        position = args.index('--track')
        args[position:position + 2] = ['--where', "tags HAS 'in-track:" + TRACK + "'"]
    return args


def storage(database):
    with closing(sqlite3.connect(database)) as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        pages = {key: conn.execute('PRAGMA ' + key).fetchone()[0]
                 for key in ('page_size', 'page_count', 'freelist_count')}
        counts = {name: conn.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0]
                  for name in ('entries', 'entry_history', 'graph_edges', 'edge_history')}
        try:
            sizes = [{'name': row[0], 'bytes': row[1], 'unused_bytes': row[2]}
                     for row in conn.execute('SELECT name,sum(pgsize),sum(unused) FROM dbstat GROUP BY name ORDER BY name')]
        except sqlite3.OperationalError:
            sizes = None
        indexes = [dict(name=row[0], table=row[1]) for row in conn.execute(
            "SELECT name,tbl_name FROM sqlite_master WHERE type='index' ORDER BY name")]
    return {'main_bytes': database.stat().st_size, 'pages': pages, 'counts': counts,
            'objects': sizes, 'indexes': indexes, 'normalization': 'checkpoint(TRUNCATE); no VACUUM; freelist reported'}


def clone(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    (destination.parent / 'config.json').write_text('{}\n')
    with closing(sqlite3.connect(source)) as old, closing(sqlite3.connect(destination)) as new:
        old.backup(new)


def canonical_legacy(database):
    """Lossless legacy tables and column order, excluding additive metadata."""
    with closing(sqlite3.connect(database)) as conn:
        values = {}
        for table in ('projects', 'entries', 'entry_projects', 'relations', 'entry_history',
                      'entry_events', 'entities', 'entity_history', 'graph_nodes', 'graph_edges', 'edge_history'):
            values[table] = conn.execute('SELECT * FROM "' + table + '" ORDER BY rowid').fetchall()
        return digest(values)


def load_baseline():
    spec = importlib.util.spec_from_file_location('track_baseline', BASELINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime_hashes(memory):
    import richi_bootstrap
    import richi_launcher
    paths = {'launcher/richi_bootstrap.py': Path(richi_bootstrap.__file__)}
    for prefix, root in (('src/richi/', Path(memory.__file__).parent),
                         ('launcher/richi_launcher/', Path(richi_launcher.__file__).parent)):
        paths.update({prefix + path.name: path for path in root.iterdir()
                      if path.is_file() and path.suffix in ('.py', '.sql', '.html')})
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sorted(paths.items())}


def worker_main(request):
    from richi import memory, graph, output
    database = Path(request['database'])
    if request['action'] == 'seed':
        database.parent.mkdir(parents=True, exist_ok=True)
        (database.parent / 'config.json').write_text('{}\n')
        data = request['corpus']
        started = time.perf_counter_ns()
        with closing(memory.connect(database, create=True)) as conn:
            memory.initialize(conn, database)
            with memory.transaction(conn):
                for row in data['projects']:
                    memory.project_put(conn, row)
                for row in data['entities']:
                    graph.put(conn, row, 'entity', memory)
            if request['native']:
                from richi import research
                typed = [row for row in data['entries'] if row.get('research')]
                with memory.transaction(conn):
                    research.write(conn, {'request_id': 'fixture-seed', 'entries': typed, 'edges': data['edges']}, memory)
            else:
                typed = []
            ordinary = [row for row in data['entries'] if row not in typed]
            for start in range(0, len(ordinary), 250):
                with memory.transaction(conn):
                    for row in ordinary[start:start + 250]:
                        memory.entry_put(conn, row)
            if not request['native']:
                with memory.transaction(conn):
                    for row in data['edges']:
                        graph.put(conn, row, 'edge', memory)
        return {'build_ms': (time.perf_counter_ns() - started) / 1e6,
                'runtime': {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
                    'package': str(Path(memory.__file__).resolve()),
                    'runtime_files_sha256': runtime_hashes(memory)}}
    if request['action'] == 'rename_run':
        with closing(memory.connect(database)) as conn, memory.transaction(conn):
            value = memory.entry_get(conn, 'run:support')
            value.pop('created_at'); expected = value.pop('updated_at')
            value['expected_updated_at'] = expected
            if 'revision' in value:
                value['expected_revision'] = value.pop('revision')
            value['title'] += ' — title corrected; measurement unchanged'
            return memory.entry_put(conn, value)
    if request['action'] == 'core':
        from richi import query_tools, retrieval_tools
        baseline = load_baseline()
        with closing(memory.connect(database, readonly=True)) as conn, output.json_mode(compact=True):
            parsed = {}
            def call(arguments):
                key = tuple(arguments)
                if key not in parsed:
                    parsed[key] = memory.parser().parse_args(arguments)
                args = parsed[key]
                if args.command == 'query': return query_tools.run(conn, args, memory)
                if args.command == 'fetch': return retrieval_tools.run(conn, args, memory)
                if args.command == 'entry': return memory.entry_get(conn, args.id, history=args.history)
                if args.command == 'edge': return graph.get(conn, 'edge', args.id, memory, history=args.history)
                if args.command == 'graph': return graph.export(conn, args, memory)
                from richi import tracks
                return tracks.run(conn, args, memory, database)
            def perform():
                if request['native']:
                    return call(native_arguments(request['scenario'], request.get('cursor')))
                if request['scenario'] in ('strict', 'broad', 'aggregates'):
                    return call(baseline_metadata_arguments(request['scenario']))
                return baseline.run(call, request['scenario'], cursor=request.get('cursor'))
            # Exclude argument parser construction/import from query-core timing.
            perform()
            samples, result = [], None
            for _ in range(request['repeats']):
                start = time.perf_counter_ns()
                result = perform()
                samples.append((time.perf_counter_ns() - start) / 1e6)
            return {'latency': summary(samples), 'response': result}
    if request['action'] == 'plans':
        from richi import query_tools
        with closing(memory.connect(database, readonly=True)) as conn:
            args = memory.parser().parse_args(native_arguments('strict') if request['native'] else
                ['query', 'select', '--where', "tags HAS 'in-track:" + TRACK + "'", '--include-hypotheses', '--include-superseded'])
            selections, _ = query_tools._selection(conn, args, memory)
            sql, parameters = query_tools._union(selections, ['ref', 'kind'], page_limit=21)
            statements = {'strict_select': (sql, parameters)}
            family, table, where, params = selections[0]
            statements['strict_count'] = ('SELECT count(*) FROM ' + table + ' e WHERE ' + where, params)
            if request['native']:
                from richi import tracks
                cte, params = tracks._scope_sql('entry:task:01', True)
                statements['context_scope'] = (cte + 'SELECT ref FROM selected', params)
                traced = []
                conn.set_trace_callback(traced.append)
                tracks._counts(conn, cte, params)
                conn.set_trace_callback(None)
                statements['context_counts'] = (traced[-1], [])
            return {name: {'sql': sql, 'parameters': params,
                'plan': [list(row) for row in conn.execute('EXPLAIN QUERY PLAN ' + sql, params)]}
                for name, (sql, params) in statements.items()}
    raise ValueError('Unknown worker action')


def verify_installation(runtime, revision):
    # A declared SHA alone is insufficient: compare loaded module bytes with it.
    if revision == 'WORKTREE':
        expected = {str(path.relative_to(HERE.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for directory in ('src/richi', 'launcher/richi_launcher') for path in (HERE.parent / directory).iterdir()
                    if path.is_file() and path.suffix in ('.py', '.sql', '.html')}
        expected['launcher/richi_bootstrap.py'] = hashlib.sha256((HERE.parent / 'launcher/richi_bootstrap.py').read_bytes()).hexdigest()
        if runtime['runtime_files_sha256'] != expected:
            different = sorted(key for key in set(expected) | set(runtime['runtime_files_sha256'])
                               if expected.get(key) != runtime['runtime_files_sha256'].get(key))
            raise AssertionError('Smoke installation differs from current worktree; reinstall candidate: ' + repr(different))
        return 'WORKTREE_SMOKE_ONLY'
    rows = subprocess.check_output(['git', '-C', str(HERE.parent), 'ls-tree', '-r', '--name-only', revision, 'src/richi', 'launcher'], text=True).splitlines()
    expected = {row: hashlib.sha256(subprocess.check_output(
        ['git', '-C', str(HERE.parent), 'show', revision + ':' + row])).hexdigest()
        for row in rows if Path(row).suffix in ('.py', '.sql', '.html')}
    actual = runtime['runtime_files_sha256']
    if expected != actual:
        raise AssertionError('Installed runtime differs from declared revision ' + revision + ': ' +
                             repr(sorted(key for key in set(expected) | set(actual) if expected.get(key) != actual.get(key))))
    return subprocess.check_output(['git', '-C', str(HERE.parent), 'rev-parse', revision], text=True).strip()


def refs(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'ref' and isinstance(item, str): found.add(item)
            found.update(refs(item))
    elif isinstance(value, list):
        for item in value: found.update(refs(item))
    return found


def research_cards(value):
    result = {}
    if isinstance(value, dict):
        if isinstance(value.get('research'), dict) and value.get('ref'):
            result[value['ref']] = value['research']
        if isinstance(value.get('record'), dict) and value.get('ref') and value['record'].get('research'):
            result[value['ref']] = value['record']['research']
        for item in value.values(): result.update(research_cards(item))
    elif isinstance(value, list):
        for item in value: result.update(research_cards(item))
    return result


def subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and subset(item, actual[key]) for key, item in expected.items())
    if isinstance(expected, list):
        # Richi's public string-list contract normalizes/deduplicates unordered
        # collections. Ordered observations/direction changes remain ordered.
        if all(isinstance(item, str) for item in expected):
            return isinstance(actual, list) and sorted(set(expected)) == sorted(set(actual))
        return isinstance(actual, list) and len(expected) == len(actual) and all(subset(x, y) for x, y in zip(expected, actual))
    return expected == actual


def sufficient_native_context(python, database, scenario, first):
    """Follow only advertised continuations/omissions, never fixture gold refs."""
    responses, commands, elapsed, omitted = [first], [], 0.0, []
    current, seen_cursors = first, set()
    while True:
        headers = [current.get('task', {})] + current.get('tracks', []) + current.get('goals', [])
        for card in headers + current.get('records', []):
            if card.get('ref') and (card.get('details_omitted') or card.get('research_omitted')):
                omitted.append(card['ref'])
        if not current.get('has_more'): break
        cursor = current.get('next_after')
        if not cursor or cursor in seen_cursors: raise AssertionError('Context continuation did not make progress')
        seen_cursors.add(cursor)
        arguments = native_arguments(scenario) + ['--after', cursor]
        current, _, timing = command(python, database, arguments)
        responses.append(current); commands.append(arguments); elapsed += timing
    available = set(research_cards(responses))
    omitted = [ref for ref in dict.fromkeys(omitted) if ref not in available]
    for start in range(0, len(omitted), 32):
        chosen, offset = omitted[start:start + 32], 0
        while True:
            arguments = ['fetch', *sum((['--ref', ref] for ref in chosen), []), '--max-chars', '32000', '--offset', str(offset)]
            response, _, timing = command(python, database, arguments)
            if any(item['status'] != 'found' for item in response['results']):
                raise AssertionError('Synthetic sufficient-evidence fetch omitted a whole card')
            responses.append(response); commands.append(arguments); elapsed += timing
            if response['request_complete']: break
            if response['next_offset'] == offset: raise AssertionError('Sufficiency fetch did not progress')
            offset = response['next_offset']
    return {'responses': responses, 'additional_commands': commands, 'additional_ms': elapsed,
            'processes': 1 + len(commands), 'response_chars': sum(len(render(value)) for value in responses)}


def write_payload(fixture, index, native):
    value = copy.deepcopy(next(row for row in fixture['entries'] if row['id'] == 'run:support'))
    value['id'] = 'run:benchmark-%04d' % index
    body = json.loads(value['summary']); body['explicit_tracks'] = [TRACK]
    value['summary'] = render(body).strip(); value['tags'] = ['fixture:tracks', 'in-track:' + TRACK]
    edges = [{'id': 'run-of:benchmark-%04d' % index, 'kind': 'run_of', 'from_ref': 'entry:' + value['id'],
              'to_ref': 'entry:experiment:shared', 'description': 'Synthetic repeat', 'knowledge_state': 'confirmed',
              'sources': value['sources']},
             {'id': 'member:benchmark-%04d' % index, 'kind': 'in_track', 'from_ref': 'entry:' + value['id'],
              'to_ref': TRACK, 'description': 'Synthetic repeat', 'knowledge_state': 'confirmed', 'sources': value['sources']}]
    if not native: value.pop('research')
    return {'request_id': 'benchmark-%04d' % index, 'entries': [value], 'edges': edges}


def tokenize(python, messages):
    cache = os.environ.get('TIKTOKEN_CACHE_DIR')
    if not cache or not Path(cache).is_dir():
        raise ValueError('TIKTOKEN_CACHE_DIR must contain a prepopulated o200k_base cache; no tokenizer downloads during evaluation')
    code = """import json,sys,tiktoken,tiktoken.load,importlib.metadata
tiktoken.load.read_file=lambda path: (_ for _ in ()).throw(RuntimeError('tokenizer cache unavailable'))
e=tiktoken.get_encoding('o200k_base')
v=json.load(sys.stdin)
print(json.dumps({'tokenizer':'o200k_base','version':importlib.metadata.version('tiktoken'),'counts':{k:sum(len(e.encode(s,disallowed_special=())) for s in values) for k,values in v.items()}}))
"""
    proc = subprocess.run([str(python), '-I', '-c', code], input=render(messages), capture_output=True, text=True, timeout=90)
    if proc.returncode: raise RuntimeError(proc.stderr)
    return json.loads(proc.stdout)


def main():
    if sys.argv[1:] == ['--worker']:
        sys.stdout.write(render(worker_main(json.load(sys.stdin))))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-python', required=True, type=Path)
    parser.add_argument('--candidate-python', required=True, type=Path)
    parser.add_argument('--baseline-revision', default=BASELINE_REVISION)
    parser.add_argument('--candidate-revision', required=True)
    parser.add_argument('--tokenizer-python', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--repeats', type=int, default=30)
    parser.add_argument('--core-repeats', type=int, default=30)
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    if args.output.exists(): parser.error('Use a new result file')
    if not args.smoke and (args.repeats < 30 or args.core_repeats < 30 or not args.tokenizer_python):
        parser.error('Full evaluation requires >=30 repetitions and an installed tokenizer')
    if not args.smoke and 'WORKTREE' in (args.baseline_revision, args.candidate_revision):
        parser.error('Full evaluation requires committed revisions; WORKTREE is smoke-only')
    if min(args.repeats, args.core_repeats) < 1: parser.error('Repetitions must be positive')
    fixture = json.loads(FIXTURE.read_text())
    report = {'format_version': 1, 'measured_at': datetime.now(timezone.utc).isoformat(),
        'status': 'smoke_only' if args.smoke else 'measured', 'model_quality': {'status': 'not_run',
        'reason': 'No authorized model runner; no claim about engineering solution quality'},
        'configuration': {'seed': fixture['seed'], 'scales': fixture['scales'], 'repeats': args.repeats,
                          'generation': 'deterministic index/modulo corpus; seed identifies frozen fixture, no random sampling',
                          'core_repeats': args.core_repeats, 'fixture_sha256': hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                          'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                          'postprocessor_sha256': hashlib.sha256(BASELINE.read_bytes()).hexdigest()},
        'scale': [], 'limitations': ['Synthetic data; no private stores or production workloads.',
          'Fresh process, shared OS page caches; this is not a cold disk test.',
          'Baseline membership is an explicit fixture authoring convention using existing tags and summary JSON.',
          'Baseline history polling has no multi-command snapshot; native changes are revision notifications, not historical payload replay.',
          'Prepared summary is a stale-able cached control; its preparation is separately reported.',
          'Token estimates are visible-text replay, not billing or hidden reasoning tokens.',
          'No weighted aggregate savings; scenario rows are reported separately.']}
    token_inputs = {}
    baseline_instructions = subprocess.check_output(['git', '-C', str(HERE.parent), 'show', args.baseline_revision + ':skills/project-memory/SKILL.md'], text=True)
    candidate_instructions = (HERE.parent / 'skills/project-memory/SKILL.md').read_text()
    candidate_instructions += '\n' + (HERE.parent / 'skills/project-memory/references/TRACKS.md').read_text()
    token_inputs['baseline_instruction_prefix'] = [baseline_instructions]
    token_inputs['native_instruction_prefix'] = [candidate_instructions]
    token_inputs['baseline_script_once'] = [BASELINE.read_text()]
    with tempfile.TemporaryDirectory(prefix='richi-tracks-eval-') as temporary:
        root = Path(temporary)
        for scale_number, size in enumerate(fixture['scales'][:1] if args.smoke else fixture['scales']):
            print('Track evaluation scale %d: seeding fixed installations' % size, file=sys.stderr, flush=True)
            databases = {arm: root / str(size) / arm / 'memory.sqlite3' for arm in ('baseline', 'native')}
            pythons = {'baseline': args.baseline_python, 'native': args.candidate_python}
            row = {'entries': size, 'seed': {}, 'storage': {}, 'fresh_cli': {}, 'core': {}, 'quality': {}, 'evidence_workflow': {}}
            for arm in ('baseline', 'native'):
                row['seed'][arm] = worker(pythons[arm], databases[arm], 'seed', corpus=corpus(fixture, size, arm == 'native'), native=arm == 'native')
                revision = args.candidate_revision if arm == 'native' else args.baseline_revision
                row['seed'][arm]['verified_revision'] = verify_installation(row['seed'][arm]['runtime'], revision)
                row['storage'][arm] = {'before_writes': storage(databases[arm])}
            migration = root / str(size) / 'migration' / 'memory.sqlite3'
            clone(databases['baseline'], migration)
            before = storage(migration)
            preserved = canonical_legacy(migration)
            migrated, _, elapsed = command(args.candidate_python, migration, ['migrate'])
            row['migration'] = {'fresh_cli_ms': elapsed, 'response': migrated, 'before': before, 'after': storage(migration),
                                'legacy_rows_preserved': canonical_legacy(migration) == preserved}
            if not row['migration']['legacy_rows_preserved']: raise AssertionError('Migration changed legacy rows')
            row['query_plans'], row['index_build'] = {}, {}
            for arm in pythons:
                row['query_plans'][arm] = worker(pythons[arm], databases[arm], 'plans', native=arm == 'native')
                indexed = root / str(size) / ('index-' + arm) / 'memory.sqlite3'
                clone(databases[arm], indexed)
                index_response, _, index_ms = command(pythons[arm], indexed, ['search-index', 'build'])
                row['index_build'][arm] = {'fresh_cli_ms': index_ms, 'response': index_response, 'after': storage(indexed)}

            def invoke(arm, scenario, cursor=None, maximum=32000):
                if arm == 'native': return command(pythons[arm], databases[arm], native_arguments(scenario, cursor, maximum))
                if scenario in ('strict', 'broad', 'aggregates'):
                    return command(pythons[arm], databases[arm], baseline_metadata_arguments(scenario, maximum))
                arguments = ['--python', str(pythons[arm]), '--database', str(databases[arm]),
                             '--scenario', scenario, '--max-chars', str(maximum)]
                metrics_file = databases[arm].parent / ('metrics-' + scenario + '.json')
                arguments += ['--metrics-file', str(metrics_file)]
                if cursor:
                    cursor_file = databases[arm].parent / 'cursor.json'
                    cursor_file.write_text(render(cursor)); arguments += ['--cursor-file', str(cursor_file)]
                answer, raw, elapsed = command(pythons[arm], databases[arm], arguments, script=BASELINE)
                return {'answer': answer, 'instrumentation': json.loads(metrics_file.read_text())}, raw, elapsed

            full = {arm: invoke(arm, 'context', maximum=100000)[0] for arm in pythons}
            for arm, response in full.items():
                cards = research_cards(response)
                required = fixture['expected']['required_evidence_refs'] + ['entry:task:01'] + fixture['expected']['track_refs']
                missing = sorted(set(required) - set(cards))
                unequal = [item['id'] for item in fixture['entries'] if 'entry:' + item['id'] in required
                           and not subset(item['research'], cards.get('entry:' + item['id']))]
                row['quality'][arm] = {'required_evidence_missing': missing,
                    'structured_fact_mismatches': unequal,
                    'unscoped_solution_quality': 'not_evaluated', 'full_context_chars': len(render(response.get('answer', response)))}
                if missing or unequal: raise AssertionError(arm + ' missing/unequal frozen evidence: ' + repr(missing + unequal))
            row['quality']['stress_16k'] = {}
            for arm in pythons:
                stress = invoke(arm, 'context', maximum=16000)[0]
                answer = stress.get('answer', stress)
                row['quality']['stress_16k'][arm] = {
                    'response_chars': len(render(answer)),
                    'required_evidence_without_details': sorted(set(fixture['expected']['required_evidence_refs']) - set(research_cards(answer))),
                    'counts': answer.get('counts'), 'truncated': answer.get('truncated'),
                    'has_more': answer.get('has_more'), 'omitted_refs': answer.get('omitted_refs'),
                    'note': 'Explicit stress budget; final context benchmark uses 32000 chars based on diagnostic sufficiency.'}
            if scale_number == 0:
                for arm in pythons:
                    catalog_args = [['capabilities', 'list'], ['capabilities', 'list', '--group', 'research' if arm == 'native' else 'search']]
                    catalog = [render(arguments) + command(pythons[arm], databases[arm], arguments)[1] for arguments in catalog_args]
                    token_inputs[arm + '_catalog_once'] = catalog
                    if arm == 'native': candidate_instructions += '\n' + '\n'.join(catalog)
                    else: baseline_instructions += '\n' + '\n'.join(catalog)
            cursors = {}
            initial = invoke('baseline', 'changes')[0]
            cursors['baseline'] = initial['answer']['cursor']
            current = None
            while True:
                initial = invoke('native', 'changes', current, maximum=100000)[0]
                current = initial.get('next_cursor', initial.get('cursor'))
                if not initial.get('has_more', False): break
            if not current: raise AssertionError('Native changes must return a resumable cursor')
            cursors['native'] = current
            row['cursor_state_chars'] = {arm: len(render(cursor)) for arm, cursor in cursors.items()}
            for arm in pythons: worker(pythons[arm], databases[arm], 'rename_run')

            for scenario in SCENARIOS:
                print('  %s: alternating %d fresh-process observations per arm' % (scenario, args.repeats), file=sys.stderr, flush=True)
                samples = {arm: [] for arm in pythons}
                workflow_samples = {arm: [] for arm in pythons}
                last, workflows = {}, {}
                for repetition in range(args.repeats):
                    order = ('baseline', 'native') if (repetition + scale_number) % 2 == 0 else ('native', 'baseline')
                    for arm in order:
                        response, raw, elapsed = invoke(arm, scenario, cursors[arm] if scenario == 'changes' else None)
                        samples[arm].append(elapsed); last[arm] = response
                        if scenario in ('context', 'standalone'):
                            if arm == 'native':
                                complete = sufficient_native_context(pythons[arm], databases[arm], scenario, response)
                            else:
                                complete = {'responses': [response['answer']], 'additional_commands': [], 'additional_ms': 0,
                                            'processes': 1, 'response_chars': len(render(response['answer']))}
                            workflows[arm] = complete
                            workflow_samples[arm].append(elapsed + complete['additional_ms'])
                row['fresh_cli'][scenario] = {arm: summary(times) for arm, times in samples.items()}
                if scenario in ('strict', 'broad'):
                    before_refs = [item[0] for item in last['baseline']['rows']]
                    after_refs = [item[0] for item in last['native']['rows']]
                    if before_refs != after_refs: raise AssertionError('Unequal metadata page: ' + scenario)
                    row['quality'][scenario] = {'same_ordered_page': True, 'returned': len(after_refs)}
                if scenario == 'aggregates':
                    if last['baseline']['rows'] != last['native']['rows']:
                        raise AssertionError('Unequal complete aggregate rows')
                    row['quality'][scenario] = {'same_complete_rows': True}
                if scenario == 'changes':
                    for arm in pythons:
                        answer = last[arm].get('answer', last[arm])
                        if 'entry:run:support' not in refs(answer['events']):
                            raise AssertionError(arm + ' changes lost run title update')
                    row['quality'][scenario] = {'title_update_visible_in_both': True, 'payload_contracts_differ': True}
                if scenario == 'context':
                    row['quality']['bounded_context'] = {arm: {
                        'detailed_required_evidence': sorted(set(fixture['expected']['required_evidence_refs']) & set(research_cards(last[arm]))),
                        'required_evidence_without_details': sorted(set(fixture['expected']['required_evidence_refs']) - set(research_cards(last[arm]))),
                        'note': 'Presence/omission is not an inference that a hypothesis or business goal is true.'}
                        for arm in pythons}
                if scenario in ('context', 'standalone'):
                    row['evidence_workflow'][scenario] = {}
                    for arm in pythons:
                        complete = workflows[arm]
                        wanted = fixture['expected']['required_evidence_refs'] if scenario == 'context' else ['entry:task:20']
                        missing = sorted(set(wanted) - set(research_cards(complete['responses'])))
                        if missing: raise AssertionError(arm + ' sufficient-evidence workflow lost ' + repr(missing))
                        row['evidence_workflow'][scenario][arm] = dict(summary(workflow_samples[arm]),
                            processes=complete['processes'], response_chars=complete['response_chars'],
                            required_evidence_missing=missing, replay='advertised context continuation, then fetch explicitly omitted whole cards')
                row['core'][scenario] = {}
                for arm in pythons:
                    core = worker(pythons[arm], databases[arm], 'core', scenario=scenario, native=arm == 'native',
                                  repeats=args.core_repeats, cursor=cursors[arm] if scenario == 'changes' else None)
                    row['core'][scenario][arm] = core['latency']
                    answer = last[arm].get('answer', last[arm])
                    row['fresh_cli'][scenario][arm].update(response_chars=len(render(answer)),
                        response_bytes=len(render(answer).encode()),
                        calls=last[arm].get('instrumentation', {}).get('calls', 1),
                        processes=last[arm].get('instrumentation', {}).get('processes', 1),
                        dispatch_mode=last[arm].get('instrumentation', {}).get('dispatch_mode', 'installed_launcher'),
                        local_response_bytes=last[arm].get('instrumentation', {}).get('local_response_bytes', len(render(answer).encode())))
                    task = 'Recover sufficient evidence for synthetic ' + scenario + '; retain provenance, negative results and unknown applicability.'
                    uses_script = arm == 'baseline' and scenario in ('context', 'standalone', 'changes')
                    arguments = (['python', 'evals/track_baseline.py', '--python', str(pythons[arm]),
                        '--database', str(databases[arm]), '--scenario', scenario,
                        *(['--cursor-file', str(databases[arm].parent / 'cursor.json')] if scenario == 'changes' else [])]
                        if uses_script else baseline_metadata_arguments(scenario) if arm == 'baseline'
                        else native_arguments(scenario, cursors[arm] if scenario == 'changes' else None))
                    key = str(size) + '/' + scenario + '/' + arm
                    visible_responses = workflows[arm]['responses'] if scenario in ('context', 'standalone') else [answer]
                    if arm == 'native' and scenario in ('context', 'standalone'):
                        arguments = [arguments] + workflows[arm]['additional_commands']
                    token_inputs[key + '/first_response_only'] = [render(answer)]
                    token_inputs[key + '/warm_visible'] = [task, render(arguments)] + [render(value) for value in visible_responses]
                    prefix = baseline_instructions if arm == 'baseline' else candidate_instructions
                    token_inputs[key + '/cold_visible'] = [prefix, BASELINE.read_text() if uses_script else '', task, render(arguments)] + [render(value) for value in visible_responses]
                if scenario in ('context', 'standalone'):
                    # Fixed summary control contains the baseline's actually
                    # collected facts, not a hand-picked favorable model answer.
                    prepared, raw, prepare_ms = invoke('baseline', scenario, maximum=100000)
                    path = root / str(size) / (scenario + '-prepared.json')
                    path.write_text(render(prepared['answer']))
                    times = []
                    for _ in range(args.repeats):
                        start = time.perf_counter_ns()
                        proc = subprocess.run([str(args.baseline_python), '-I', '-c',
                            'from pathlib import Path;import sys;sys.stdout.write(Path(sys.argv[1]).read_text())', str(path)],
                            capture_output=True, text=True, env=environment(path.parent), timeout=30)
                        if proc.returncode or json.loads(proc.stdout) != prepared['answer']:
                            raise AssertionError('Prepared control failed')
                        times.append((time.perf_counter_ns() - start) / 1e6)
                    row['fresh_cli'][scenario]['prepared'] = dict(summary(times), preparation_ms=prepare_ms,
                        response_chars=len(path.read_text()), calls=1, limitation='Invalidated by relevant source edits; preparation is not free')
                    token_inputs[str(size) + '/' + scenario + '/prepared/warm_visible'] = [task, 'Read prepared JSON summary', path.read_text()]
                    token_inputs[str(size) + '/' + scenario + '/prepared/cold_visible'] = [baseline_instructions, task, 'Read prepared JSON summary', path.read_text()]
                    token_inputs[str(size) + '/' + scenario + '/prepared/preparation_visible'] = [baseline_instructions, BASELINE.read_text(),
                        task, 'Create prepared JSON by executing evals/track_baseline.py --scenario ' + scenario, path.read_text()]

            row['write_result'] = {arm: [] for arm in pythons}
            for repetition in range(args.repeats):
                for arm in (('baseline', 'native') if repetition % 2 == 0 else ('native', 'baseline')):
                    payload = write_payload(fixture, repetition, arm == 'native')
                    started = time.perf_counter_ns()
                    if arm == 'native':
                        written, _, _ = command(pythons[arm], databases[arm], ['research', 'put', '--file', '-'], stdin=render(payload))
                        write_arguments = [['research', 'put', '--file', '-']]
                        write_inputs, write_outputs = [render(payload)], [render(written)]
                    else:
                        written, _, _ = command(pythons[arm], databases[arm], ['entry', 'put', '--json', '-'], stdin=render(payload['entries'][0]))
                        linked, _, _ = command(pythons[arm], databases[arm], ['edge', 'put', '--json', '-'], stdin=render(payload['edges']))
                        write_arguments = [['entry', 'put', '--json', '-'], ['edge', 'put', '--json', '-']]
                        write_inputs = [render(payload['entries'][0]), render(payload['edges'])]
                        write_outputs = [render(written), render(linked)]
                    row['write_result'][arm].append((time.perf_counter_ns() - started) / 1e6)
                    if repetition == 0:
                        prefix = baseline_instructions if arm == 'baseline' else candidate_instructions
                        task = 'Record one synthetic observed run with its canonical experiment and explicit track membership.'
                        visible = [task, render(write_arguments)] + write_inputs + write_outputs
                        token_inputs[str(size) + '/write_result/' + arm + '/warm_visible'] = visible
                        token_inputs[str(size) + '/write_result/' + arm + '/cold_visible'] = [prefix] + visible
            for arm in pythons:
                row['write_result'][arm] = summary(row['write_result'][arm])
                row['storage'][arm]['after_writes'] = storage(databases[arm])
            row['write_contract'] = 'Native: one atomic entry+two-link request with idempotency. Baseline: entry then batched two-edge CLI write; weaker atomicity across the two calls.'
            row['baseline_dispatch'] = 'Context/history compose installed memory.main CLI entrypoints inside one fresh process; parser/config/handlers/stdout retained. Native/metadata/write use the installed launcher. --dispatch subprocess is an optional slower full-launcher control.'
            report['scale'].append(row)
    report['tokens'] = tokenize(args.tokenizer_python, token_inputs) if args.tokenizer_python else {'status': 'not_run_smoke'}
    report['token_replay'] = {'method': 'Sum independent o200k_base encodings of exact instruction/script/task/command/response text; no model messages or output invented.',
        'cold': 'Full selected skill prefix plus script once plus task, arguments, final response.',
        'warm': 'Task, arguments, final response; prefix/script counted in cold once, not assumed free for whole session.',
        'local_intermediate_outputs': 'Bytes and calls measured; only postprocessor answer enters visible replay. Baseline internal CLI arguments are generated by its counted script, not charged twice. Polling cursor state remains a local file.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(str(args.output))


if __name__ == '__main__':
    main()
