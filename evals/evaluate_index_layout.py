#!/usr/bin/env python3
"""Disposable, in-process index-layout comparison; no existing DB accepted.

This isolates old/new index implementations inside one source runtime, not two
installed packages. The full project benchmark measures installed endpoints.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import types
from unittest import mock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--old-revision', default='84e1b6c3684dd17ca7385d511fcd1b408d11a68a')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output must not exist')
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(args.checkout / 'src'), str(args.checkout / 'evals')]
    import richi
    from richi import memory, output, recall, search_index
    import evaluate_indexed_filters as ev
    old = types.ModuleType('richi.search_index_old')
    old.__package__ = 'richi'
    source = subprocess.check_output(['git', '-C', str(args.checkout), 'show',
                                      args.old_revision + ':src/richi/search_index.py'])
    exec(compile(source, 'frozen-search-index-v1.py', 'exec'), old.__dict__)
    fixture = (args.checkout / 'evals/recall_cases.json').read_bytes()
    data, cases = ev.corpus(10000, json.loads(fixture))
    queries = [case['query'] for case in cases] + ['transport', 'component', 'route', 'arch', 'entry:background-00001']
    report = {'fixture_entries': len(data['entry']), 'fixture_entities': len(data['entity']),
              'old_revision': args.old_revision, 'frozen_v1_sha256': hashlib.sha256(source).hexdigest(),
              'candidate_source_sha256': hashlib.sha256(Path(search_index.__file__).read_bytes()).hexdigest(),
              'fixture_sha256': hashlib.sha256(fixture).hexdigest(), 'arms': {},
              'ordinary_response_parity': [], 'routing_parity': True}
    with tempfile.TemporaryDirectory(prefix='richi-layout-reproduction-') as directory:
        root = Path(directory)
        seed = root / 'seed/memory.sqlite3'
        ev.worker_main({'action': 'seed', 'database': str(seed), 'corpus': data})
        with sqlite3.connect(seed) as source_conn:
            source_conn.execute('VACUUM')
        modules, connections = {'v1': old, 'v2': search_index}, {}
        try:
            for name, module in modules.items():
                path = root / (name + '.sqlite3')
                with sqlite3.connect(seed) as origin, sqlite3.connect(path) as target:
                    origin.backup(target)
                conn = memory.connect(path)
                connections[name] = conn
                page_size = conn.execute('PRAGMA page_size').fetchone()[0]
                before = conn.execute('PRAGMA page_count').fetchone()[0] * page_size
                canonical_before = ev.logical(path)
                started = time.perf_counter_ns()
                with memory.transaction(conn):
                    status = module.build(conn, memory)
                elapsed = (time.perf_counter_ns() - started) / 1e6
                report['arms'][name] = {'build_ms': elapsed, 'status': status,
                    'added_page_bytes': conn.execute('PRAGMA page_count').fetchone()[0] * page_size - before,
                    'canonical_rows_preserved': ev.logical(path) == canonical_before,
                    'pages': [dict(row) for row in conn.execute("SELECT name,sum(pgsize) bytes,sum(payload) payload,sum(unused) unused FROM dbstat WHERE name LIKE 'richi_search%' OR name LIKE 'sqlite_autoindex_richi_search%' GROUP BY name")],
                    'lookup': {}, 'writes': {}}
            for query in queries:
                terms, keys = recall.terms_for(query)
                for family in ('entry', 'entity'):
                    samples, values = {'v1': [], 'v2': []}, {}
                    for repeat in range(40):
                        for name in (('v1', 'v2') if repeat % 2 == 0 else ('v2', 'v1')):
                            module, conn = modules[name], connections[name]
                            started = time.perf_counter_ns()
                            value = (module.entry_ids(conn, query, keys) if family == 'entry' else
                                     module.entity_ids(conn, query, terms, keys))
                            samples[name].append((time.perf_counter_ns() - started) / 1e6)
                            values[name] = value
                    report['routing_parity'] &= values['v1'] == values['v2']
                    for name in samples:
                        report['arms'][name]['lookup'][family + ':' + query] = {
                            'p50_ms': statistics.median(samples[name]), 'samples_ms': samples[name], 'ids': len(values[name])}
            with output.json_mode(compact=True):
                for case in cases:
                    values, raws = {}, {}
                    for name, module in modules.items():
                        with mock.patch.object(richi, 'search_index', module):
                            values[name] = recall.recall(connections[name], memory.parser().parse_args(ev.arguments(case)), memory)
                            raws[name] = output.rendered(values[name])
                    report['ordinary_response_parity'].append({'id': case['id'],
                        'equal': values['v1'] == values['v2'], 'serialized_equal': raws['v1'] == raws['v2'],
                        'sha256': {name: hashlib.sha256(raw.encode()).hexdigest() for name, raw in raws.items()}})
            for name, module in modules.items():
                conn = connections[name]
                with mock.patch.object(richi, 'search_index', module):
                    for kind in ('metadata', 'text'):
                        samples = []
                        for number in range(20):
                            row = memory.entry_get(conn, 'background-%05d' % (number + 10))
                            row.pop('created_at'); row.pop('updated_at')
                            if kind == 'metadata':
                                row['verified_at'] = '2026-10-25'
                            else:
                                row['summary'] += ' Additional synthetic measurement.'
                            started = time.perf_counter_ns()
                            with memory.transaction(conn):
                                memory.entry_put(conn, row)
                            samples.append((time.perf_counter_ns() - started) / 1e6)
                        report['arms'][name]['writes'][kind] = {'p50_ms': statistics.median(samples), 'samples_ms': samples}
            report['old_v1_reader_on_v2_fallback'] = old.entry_ids(connections['v2'], 'background-00010', []) is None
        finally:
            for conn in connections.values():
                conn.close()
    with args.output.open('x') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(args.output)


if __name__ == '__main__':
    main()
