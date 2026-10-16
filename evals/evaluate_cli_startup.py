#!/usr/bin/env python3
"""Paired synthetic CLI startup experiment; no live settings or performance SLA.

Reuse the public growth fixture/helper from revision ff96689 via --growth-evals.
Exit 1 on execution/parity/invariant failures. Existing quality misses are reported
separately and do not fail this behavior-preserving optimization experiment.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import pstats
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

GROWTH_HASH = '013ea13bef2a127ed0c631454cc0615e744a333d153a633ddcd2c4895cccc908'
FIXTURE_HASH = 'a38d6fdad925a1f2045b13faad83a611bbe01ed274fba42b0888772df4b60e31'

# Ordinary timing uses python -m richi unchanged. Only separate profiles use this
# wrapper: save the launcher before exec, then profile the same runtime program.
PROFILE = r'''
import cProfile, json, os, runpy, sys, time
from pathlib import Path
directory, *arguments = sys.argv[1:]
directory = Path(directory)
profiler = cProfile.Profile()
original_exec = os.execve
def execute(file, argv, env):
    profiler.disable()
    profiler.dump_stats(str(directory / 'launcher.prof'))
    index = argv.index('-c') + 1
    original = argv[index]
    wrapper = "import cProfile\n_p=cProfile.Profile()\n_p.enable()\ntry:\n exec(compile(%r,'<selected-runtime>','exec'))\nfinally:\n _p.disable()\n _p.dump_stats(%r)\n" % (original, str(directory / 'runtime.prof'))
    replacement = list(argv)
    replacement[index] = wrapper
    original_exec(file, replacement, env)
os.execve = execute
sys.argv = ['richi', *arguments]
profiler.enable()
runpy.run_module('richi', run_name='__main__')
'''


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def invoke(growth, python, database, *arguments, payload=None, timeout=120, profile=None):
    prefix = [str(python), '-I', '-B']
    prefix += ['-c', PROFILE, str(profile)] if profile else ['-m', 'richi']
    command = prefix + ['--config', str(database.parent / 'config.json'), '--db', str(database), *arguments]
    started = time.perf_counter_ns()
    result = subprocess.run(command, input=None if payload is None else json.dumps(payload),
                            capture_output=True, text=True, encoding='utf-8', timeout=timeout,
                            env=growth.environment(database.parent))
    elapsed = (time.perf_counter_ns() - started) / 1000000
    if result.returncode or result.stderr:
        raise ValueError('CLI failed: ' + result.stderr.strip())
    return result.stdout, json.loads(result.stdout), elapsed


def arguments(case, diagnostic=False):
    value = ['recall', case['query'], '--project', case['project'], '--limit', str(case['limit']),
             '--max-chars', '100000' if diagnostic else '16000']
    return value + ['--expect', case['expect_ref']] if diagnostic else value


def installed(growth, python, root):
    code = """import hashlib,json,pathlib,platform,sqlite3,sys,richi,richi_launcher,richi_bootstrap
roots={'richi':pathlib.Path(richi.__file__).parent,'richi_launcher':pathlib.Path(richi_launcher.__file__).parent}
hashes={family+'/'+str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for family,root in roots.items() for p in sorted(root.rglob('*')) if p.is_file() and p.suffix in {'.py','.sql','.html'}}
hashes['richi_bootstrap.py']=hashlib.sha256(pathlib.Path(richi_bootstrap.__file__).read_bytes()).hexdigest()
print(json.dumps({'python':sys.version,'executable':sys.executable,'platform':platform.platform(),'sqlite':sqlite3.sqlite_version,'version':richi.__version__,'package':str(roots['richi']),'hashes':hashes}))
"""
    result = subprocess.run([str(python), '-I', '-B', '-c', code], capture_output=True,
                            text=True, timeout=60, env=growth.environment(root))
    if result.returncode:
        raise ValueError(result.stderr)
    return json.loads(result.stdout)


def source_metadata(source):
    hashes = {str(p.relative_to(source)): sha(p) for folder in ('src/richi', 'launcher')
              for p in sorted((source / folder).rglob('*'))
              if p.is_file() and p.suffix in {'.py', '.sql', '.html'}}
    return {'path': str(source), 'hashes': hashes}


def profile_summary(path):
    stats = pstats.Stats(str(path))
    rows = [{'file': filename, 'line': line, 'function': name, 'calls': calls,
             'self_ms': own * 1000, 'cumulative_ms': cumulative * 1000}
            for (filename, line, name), (_, calls, own, cumulative, _) in stats.stats.items()]
    selected = {'_find_and_load', 'describe_runtime', 'resolve_settings', 'parser', 'recall',
                'candidates', 'entry_candidate_rows', 'entry_routing_match', 'lexical', 'fit_response'}
    return {'profile_seconds': stats.total_tt,
            'phases': [row for row in rows if row['function'] in selected],
            'top_self': sorted(rows, key=lambda row: row['self_ms'], reverse=True)[:10],
            'module_bodies': [row for row in rows if row['function'] == '<module>' and
                              Path(row['file']).name in {'tempfile.py', 'subprocess.py', 'dataclasses.py',
                                                        'inspect.py', 'shutil.py', 'config.py', 'runtime.py'}]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline-python', 'candidate-python', 'baseline-source', 'candidate-source', 'growth-evals', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=9, help='Paired samples per condition, 1..15 (default 9)')
    args = parser.parse_args(argv)
    # Keep venv interpreter symlinks intact; resolving them would select the base
    # interpreter. Source/config locations may safely use canonical paths.
    args.baseline_python = args.baseline_python.absolute()
    args.candidate_python = args.candidate_python.absolute()
    args.baseline_source = args.baseline_source.resolve()
    args.candidate_source = args.candidate_source.resolve()
    args.growth_evals = args.growth_evals.resolve()
    if not 1 <= args.repeats <= 15:
        parser.error('repeats must be 1..15')
    if args.output.exists():
        parser.error('Refusing to overwrite output')
    helper_path = args.growth_evals / 'evaluate_memory_growth.py'
    fixture_path = args.growth_evals / 'memory_growth_cases.json'
    if not helper_path.is_file() or not fixture_path.is_file():
        parser.error('growth-evals must contain the helper and fixture from ff96689')
    if sha(helper_path) != GROWTH_HASH or sha(fixture_path) != FIXTURE_HASH:
        parser.error('Use the unchanged growth helper/fixture from ff96689')
    spec = importlib.util.spec_from_file_location('growth', helper_path)
    growth = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(growth)
    document = json.loads(fixture_path.read_text())
    growth.validate(document)
    root = Path(tempfile.mkdtemp(prefix='richi-cli-startup-'))
    print('Synthetic stores and raw profiles: ' + str(root), flush=True)
    variants = {'baseline': (args.baseline_python, args.baseline_source),
                'candidate': (args.candidate_python, args.candidate_source)}
    metadata = {name: dict(installed=installed(growth, python, root), source=source_metadata(source))
                for name, (python, source) in variants.items()}
    for field in ('python', 'sqlite'):
        if metadata['baseline']['installed'][field] != metadata['candidate']['installed'][field]:
            raise ValueError('Baseline and candidate must use the same ' + field + ' version/build')
    for name, value in metadata.items():
        source_hashes = {}
        for key, digest in value['source']['hashes'].items():
            if key.startswith('src/richi/'):
                source_hashes['richi/' + key[len('src/richi/'):]] = digest
            elif key.startswith('launcher/richi_launcher/') or key == 'launcher/richi_bootstrap.py':
                source_hashes[key[len('launcher/'):]] = digest
        if source_hashes != value['installed']['hashes']:
            raise ValueError(name + ': source and installed runtime/launcher content differ')
    growth.cli = lambda database, *a, **kw: invoke(growth, args.baseline_python, database, *a, **kw)
    report = {'schema_version': 1, 'started_at': datetime.now(timezone.utc).isoformat(),
              'runner_sha256': sha(__file__), 'growth_revision': 'ff966896ec9c28497430ec18b2c39706ee86d5ab',
              'growth_helper_sha256': GROWTH_HASH, 'fixture_sha256': FIXTURE_HASH,
              'repeats_per_variant_per_condition': args.repeats, 'metadata': metadata,
              'protocol': 'One untimed warmup/variant/condition; rotate conditions each round, alternate AB/BA by round+condition index; sequential subprocesses.',
              'limitations': ['Host load uncontrolled; no caches flushed, no cold or SLA claim.',
                              'cProfile timings overlap and include instrumentation overhead; do not add/subtract them from ordinary wall time.',
                              'Launcher profile wrapper preimports infrastructure/json/pathlib; profiles exclude interpreter startup, exec and teardown.',
                              'Entry-growth fixture only; no entity, history or same-Jira-detail growth.',
                              'Crowded target miss measures bounded known-record discoverability among legitimate lexical matches.',
                              'Quality misses are retained; exit success means parity/invariants, not perfect retrieval.'],
              'datasets': [], 'selections': [], 'conditions': [], 'invariant_failures': []}
    databases, baselines, catalogs = {}, {}, {}
    for count in (100, 10000):
        corpus = growth.generate(document, count, 0)
        catalogs[count] = {f + ':' + r['id']: r for f in ('entry', 'entity') for r in corpus[f]}
        seed = root / ('seed-b%d' % count) / 'synthetic.sqlite3'
        growth.seed(seed, corpus, 0)
        if '-wal' in growth.readonly_digest(seed):
            raise ValueError('Closed seed has a nonempty WAL; cannot copy its main file alone')
        for mode in ('release', 'dev'):
            for variant, (python, source) in variants.items():
                directory = root / ('%s-%s-b%d' % (variant, mode, count))
                directory.mkdir()
                database = directory / 'synthetic.sqlite3'
                shutil.copy2(seed, database)
                # Some SQLite/VFS builds cannot read a copied WAL-mode DB until
                # its coordination sidecars exist. Prepare only synthetic copies
                # before timing/digests, without changing any main-database bytes.
                with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=rw', uri=True)) as conn:
                    conn.execute('SELECT count(*) FROM entries').fetchone()
                if sha(database) != sha(seed):
                    raise ValueError('Preparing copied database changed seed bytes')
                config = {} if mode == 'release' else {'dev': True, 'development': {'source': str(source)}}
                (directory / 'config.json').write_text(json.dumps(config))
                databases[count, mode, variant] = database
                baselines[count, mode, variant] = growth.readonly_digest(database)
                _, view, _ = invoke(growth, python, database, 'config', 'show')
                selected = view['runtime']
                package = source / 'src' / 'richi' if mode == 'dev' else Path(metadata[variant]['installed']['package'])
                if (selected['mode'] != mode or not selected['available'] or
                        Path(selected['package']).resolve() != package.resolve() or
                        Path(view['database']).resolve() != database.resolve()):
                    raise ValueError('Effective runtime/database differs from the requested isolated condition')
                report['selections'].append({'background': count, 'variant': variant, 'runtime': selected})
        report['datasets'].append({'background': count, 'current_corpus_sha256': growth.digest(corpus),
                                   'storage': growth.storage(seed)})
    cases = [c for c in document['cases'] if c['id'] in {'current-id', 'crowded-lexical'}]
    expected = {}
    for count in (100, 10000):
        for mode in ('release', 'dev'):
            for case in cases:
                cell = {'id': '%s-b%d-%s' % (mode, count, case['id']), 'mode': mode,
                        'background': count, 'case': case, 'samples': []}
                for variant, (python, _) in variants.items():
                    raw, response, _ = invoke(growth, python, databases[count, mode, variant], *arguments(case))
                    outcome = growth.classify_result(raw, response, case, catalogs[count], 16000)
                    report['invariant_failures'].extend(outcome['invariant_failures'])
                    if variant == 'baseline':
                        expected[cell['id']] = response
                        cell['normal_outcome'] = outcome
                    elif response != expected[cell['id']]:
                        report['invariant_failures'].append(cell['id'] + ': warmup parity differs')
                report['conditions'].append(cell)
    print('Warmups complete; starting paired ordinary CLI samples', flush=True)
    cells = report['conditions']
    for round_index in range(args.repeats):
        for index in [(round_index + i) % len(cells) for i in range(len(cells))]:
            cell = cells[index]
            order = ['baseline', 'candidate'] if (round_index + index) % 2 == 0 else ['candidate', 'baseline']
            sample = {'round': round_index + 1, 'order': order}
            for variant in order:
                raw, response, elapsed = invoke(growth, variants[variant][0],
                    databases[cell['background'], cell['mode'], variant], *arguments(cell['case']))
                sample[variant + '_ms'] = elapsed
                if response != expected[cell['id']]:
                    report['invariant_failures'].append(cell['id'] + ': ordinary response changed')
            sample['candidate_minus_baseline_ms'] = sample['candidate_ms'] - sample['baseline_ms']
            cell['samples'].append(sample)
        print('Paired round %d/%d' % (round_index + 1, args.repeats), flush=True)
    for cell in cells:
        for variant in variants:
            cell[variant] = growth.summarize_latencies([s[variant + '_ms'] for s in cell['samples']])
        cell['median_paired_delta_ms'] = statistics.median(s['candidate_minus_baseline_ms'] for s in cell['samples'])
        cell['diagnostics'], cell['profiles'] = {}, {}
        diagnostic_baseline = None
        for variant, (python, _) in variants.items():
            database = databases[cell['background'], cell['mode'], variant]
            raw, response, _ = invoke(growth, python, database, *arguments(cell['case'], diagnostic=True))
            checks = growth.classify_result(raw, response, cell['case'], catalogs[cell['background']], 100000)
            report['invariant_failures'].extend(checks['invariant_failures'])
            if variant == 'baseline':
                diagnostic_baseline = response
            elif response != diagnostic_baseline:
                report['invariant_failures'].append(cell['id'] + ': diagnostic response changed')
            cell['diagnostics'][variant] = response['explain']
            if cell['background'] == 10000:
                directory = root / (cell['id'] + '-' + variant + '-profile')
                directory.mkdir()
                _, response, _ = invoke(growth, python, database, *arguments(cell['case']), profile=directory)
                if response != expected[cell['id']]:
                    report['invariant_failures'].append(cell['id'] + ': profiled response changed')
                cell['profiles'][variant] = {stage: profile_summary(directory / (stage + '.prof'))
                                             for stage in ('launcher', 'runtime')}
        if cell['diagnostics']['baseline'] != cell['diagnostics']['candidate']:
            report['invariant_failures'].append(cell['id'] + ': diagnostic workload/outcome changed')
    report['readonly_unchanged'] = all(growth.readonly_digest(db) == baselines[key] for key, db in databases.items())
    if not report['readonly_unchanged']:
        report['invariant_failures'].append('Database/nonempty-WAL bytes changed')
    for name, (python, source) in variants.items():
        if metadata[name] != dict(installed=installed(growth, python, root), source=source_metadata(source)):
            report['invariant_failures'].append(name + ': source or installation changed during measurement')
    report['invariant_failures'] = sorted(set(report['invariant_failures']))
    report['summary'] = {'execution_completed': True, 'parity_and_invariants_passed': not report['invariant_failures'],
                         'quality_miss_conditions': sum(bool(c['normal_outcome']['quality_misses']) for c in cells),
                         'ordinary_measured_calls': len(cells) * args.repeats * 2}
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write('\n')
    print(json.dumps(report['summary']), flush=True)
    return int(bool(report['invariant_failures']))


if __name__ == '__main__':
    raise SystemExit(main())
