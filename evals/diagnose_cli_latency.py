#!/usr/bin/env python3
"""Fixed-size synthetic CLI latency diagnostic; no existing database accepted.

Sixteen normal CLI samples per arm and eight direct-runtime samples per arm.
The direct path deliberately bypasses bootstrap and is not a CLI replacement.
"""
import argparse
import hashlib
import json
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile
import time

from evaluate_indexed_filters import arguments, clone, corpus, environment, logical, physical, worker


DIRECT = r'''
import json, sys, time
phases = {}
def begin():
    return time.perf_counter_ns(), time.process_time_ns()
def end(name, start):
    phases[name] = {'wall_ms': (time.perf_counter_ns()-start[0])/1e6,
                    'cpu_ms': (time.process_time_ns()-start[1])/1e6}
start = begin()
from richi import memory, output
end('import', start)
start = begin()
args = memory.parser().parse_args(json.loads(sys.argv[1]))
end('parser', start)
with output.json_mode(compact=args.compact_json):
    start = begin()
    result = memory.run(args)
    end('run', start)
    start = begin()
    rendered = output.rendered(result)
    end('render', start)
print(json.dumps({'phases': phases, 'stdout': rendered}))
'''


def measured(python, database, argv, direct=False):
    prefix = ['--config', str(database.parent / 'config.json'), '--db', str(database), '--compact-json']
    command = ([str(python), '-I', '-B', '-c', DIRECT, json.dumps(prefix + argv)] if direct else
               [str(python), '-I', '-B', '-m', 'richi', *prefix, *argv])
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter_ns()
    proc = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                          timeout=180, env=environment(database.parent))
    ended = time.perf_counter_ns()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    if proc.returncode or proc.stderr:
        raise RuntimeError(proc.stderr or proc.stdout)
    row = {'start_perf_ns': started, 'end_perf_ns': ended,
           'wall_ms': (ended-started)/1e6,
           'child_user_ms': (after.ru_utime-before.ru_utime)*1000,
           'child_system_ms': (after.ru_stime-before.ru_stime)*1000}
    row['child_cpu_ms'] = row['child_user_ms'] + row['child_system_ms']
    row['wall_minus_child_cpu_ms'] = row['wall_ms'] - row['child_cpu_ms']
    payload = json.loads(proc.stdout)
    raw = payload['stdout'] if direct else proc.stdout
    if direct:
        row['phases'] = payload['phases']
        row['outside_recorded_phases_ms'] = row['wall_ms'] - sum(v['wall_ms'] for v in payload['phases'].values())
    row['response_sha256'] = hashlib.sha256(raw.encode()).hexdigest()
    row['response_chars'] = len(raw)
    return row, raw


def summarized(rows):
    result = {}
    for arm in ('baseline', 'candidate'):
        selected = [r for r in rows if r['arm'] == arm]
        result[arm] = {'samples': len(selected), **{
            field + '_p50': statistics.median(r[field] for r in selected)
            for field in ('wall_ms', 'child_cpu_ms', 'wall_minus_child_cpu_ms')}}
        if 'phases' in selected[0]:
            result[arm]['phases_p50'] = {name: {
                field: statistics.median(r['phases'][name][field] for r in selected)
                for field in ('wall_ms', 'cpu_ms')} for name in ('import', 'parser', 'run', 'render')}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline-python', 'candidate-python', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Use a new report path')
    source = Path(__file__).resolve().parent
    acceptance = (source / 'recall_cases.json').read_bytes()
    data, cases = corpus(10000, json.loads(acceptance))
    case = next(c for c in cases if c['id'] == 'broad-background')
    report = {'format_version': 1, 'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'fixture_sha256': hashlib.sha256(acceptance).hexdigest(), 'case': case['id'],
              'entries': len(data['entry']), 'entities': len(data['entity']),
              'declared_baseline_revision': 'b8a2a9aa889a7d65d433e985ca3f6a9d4cec4c60',
              'declared_candidate_revision': 'df60', 'normal_cli': [], 'direct_runtime': [],
              'order': 'Repeated baseline,candidate,candidate,baseline blocks; fixed counts, no adaptive stopping',
              'limitations': [
                  'Synthetic warm-cache diagnostic; not a replacement for the original benchmark or its observed regression.',
                  'Child CPU is the RUSAGE_CHILDREN delta around each waited process, including its exec replacement.',
                  'Wall minus child CPU includes parent overhead and off-CPU waits; it does not identify the cause.',
                  'Direct runtime starts a fresh process but bypasses bootstrap/runtime selection and its second interpreter.',
                  'Direct phases include import, complete parser construction/parse, memory.run, and rendering; residual includes startup/exit/transport.',
                  'No OS profiling, cache flushing, CPU affinity, or confidence interval; timing remains observational.']}
    with tempfile.TemporaryDirectory(prefix='richi-cli-diagnostic-') as directory:
        root = Path(directory)
        databases = {arm: root / arm / 'memory.sqlite3' for arm in ('baseline', 'candidate')}
        seed = worker(args.baseline_python, root, {'action': 'seed', 'database': str(databases['baseline']), 'corpus': data})
        clone(databases['baseline'], databases['candidate'])
        measured(args.candidate_python, databases['candidate'], ['search-index', 'build'])
        report['build_canonical_preserved'] = logical(databases['candidate']) == seed['logical_sha256']
        if not report['build_canonical_preserved']:
            raise AssertionError('Build changed canonical rows')
        report['runtime'] = {arm: worker(getattr(args, arm + '_python'), root, {
            'action': 'measure', 'database': str(database), 'cases': [], 'repeats': 1})['runtime']
            for arm, database in databases.items()}
        before = {arm: (logical(db), physical(db)) for arm, db in databases.items()}
        expected = None
        for direct, blocks, label in ((False, 8, 'normal_cli'), (True, 4, 'direct_runtime')):
            for block in range(blocks):
                for position, arm in enumerate(('baseline', 'candidate', 'candidate', 'baseline')):
                    row, raw = measured(getattr(args, arm + '_python'), databases[arm], arguments(case), direct)
                    if expected is None:
                        expected = raw
                    if raw != expected:
                        raise AssertionError('Full serialized response mismatch: ' + arm + ' ' + label)
                    row.update(arm=arm, block=block, position=position)
                    report[label].append(row)
            report[label + '_summary'] = summarized(report[label])
        report['full_response_parity'] = True
        report['read_invariants'] = {arm: {'canonical_unchanged': logical(db) == before[arm][0],
                                         'physical_unchanged': physical(db) == before[arm][1]}
                                     for arm, db in databases.items()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(json.dumps({'report': str(args.output), 'normal_cli': report['normal_cli_summary'],
                      'direct_runtime': report['direct_runtime_summary'], 'read_invariants': report['read_invariants']}))
    return int(not all(v for arm in report['read_invariants'].values() for v in arm.values()))


if __name__ == '__main__':
    sys.exit(main())
