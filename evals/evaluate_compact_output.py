#!/usr/bin/env python3
"""Compare installed pretty/compact CLI output on one disposable public fixture.

No production token dependency: install tiktoken in this evaluation's Python and
pass --tokenizer-cache to enable two proxy counts. Without it, character/behavior
checks still run. Exit 1 on execution/invariant failures, not measured omissions.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

BASELINE_REVISION = '748fc91e2308d04787d32c15db36e259a7af58f4'
BASELINE_CODE_SHA256 = 'f8041c49eca36f3bc7391c6ecb39148fc0bdd4d43cdafa08370b1973b0bacc7d'
TOKENIZERS = ('o200k_base', 'cl100k_base')
CASE_FACTS = {
    'jira': ['implementation', 'release', 'next-step'], 'rejection': ['rejection'],
    'investigation': ['investigation', 'trace-method'], 'condition': ['condition', 'exception'],
    'exception': ['condition', 'exception'],
    'historical-measurement': ['measurement', 'measurement-date', 'measurement-conditions',
                             'measurement-method', 'historical-scope', 'failure'],
    'hypothesis': ['hypothesis', 'unknown', 'hypothesis-method'],
}
SKILL_ROUTES = {
    'core_only_recall_or_known_id_get': [],
    'recall_with_flags_and_budget_reference': ['RECALL.md'],
    'entry_update_with_payload_reference': ['CLI.md'],
    'maintenance_with_procedure_reference': ['MAINTENANCE.md'],
    'maintenance_review_with_diagnostics': ['MAINTENANCE.md', 'DIAGNOSTICS.md'],
    'maintenance_entry_and_edge_changes': ['MAINTENANCE.md', 'DIAGNOSTICS.md', 'CLI.md', 'GRAPH.md'],
}


def skill_routes(directory):
    """Measure the routes published by each skill layout, not stale file names.

    Legacy checkouts retain the original route set. The operation layout moves
    material out of broad manuals, so reading those manuals alone is no longer
    a complete entry-update or maintenance route. These are static once-per-file
    counts, not claims about the references an agent will actually choose.
    """
    if not (directory / 'references/ENTRY_WRITE.md').is_file():
        return SKILL_ROUTES
    return {
        'core_only_recall_or_known_id_get': [],
        'recall_with_flags_and_budget_reference': ['RECALL.md'],
        'entry_update_with_payload_reference': ['ENTRY_WRITE.md'],
        'maintenance_with_procedure_reference': ['MEMORY_AUDIT.md', 'MAINTENANCE.md'],
        'maintenance_review_with_diagnostics': ['MEMORY_AUDIT.md'],
        'maintenance_entry_and_edge_changes': [
            'MEMORY_AUDIT.md', 'MAINTENANCE.md', 'ENTRY_WRITE.md', 'GRAPH.md', 'NEIGHBORS.md'],
    }


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def environment(root):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('RICHI_', 'PYTHON')) and key != 'CODEX_THREAD_ID'}
    home = root / 'home'
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'), XDG_DATA_HOME=str(home / '.local/share'))
    return env


def cli(python, database, command, compact=False, payload=None):
    flags = ['--compact-json'] if compact else []
    result = subprocess.run([str(python), '-I', '-B', '-m', 'richi', *flags,
        '--config', str(database.parent / 'config.json'), '--db', str(database), *command],
        input=None if payload is None else json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, encoding='utf-8', timeout=60, env=environment(database.parent))
    if result.returncode or result.stderr:
        raise ValueError('Synthetic CLI failed: ' + result.stderr.strip())
    return result.stdout, json.loads(result.stdout)


def code_hashes(source):
    return {str(path.relative_to(source)).replace('src/richi/', 'richi/', 1).replace('launcher/', '', 1): sha(path)
            for directory in ('src/richi', 'launcher') for path in sorted((source / directory).rglob('*'))
            if path.is_file() and path.suffix in {'.py', '.sql', '.html'}}


def installed(python, root):
    code = """import hashlib,json,pathlib,platform,sqlite3,sys,richi,richi_launcher,richi_bootstrap
roots={'richi':pathlib.Path(richi.__file__).parent,'richi_launcher':pathlib.Path(richi_launcher.__file__).parent}
hashes={name+'/'+str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for name,root in roots.items() for p in sorted(root.rglob('*')) if p.is_file() and p.suffix in {'.py','.sql','.html'}}
hashes['richi_bootstrap.py']=hashlib.sha256(pathlib.Path(richi_bootstrap.__file__).read_bytes()).hexdigest()
print(json.dumps({'python':sys.version,'sqlite':sqlite3.sqlite_version,'platform':platform.platform(),'executable':sys.executable,'package':str(roots['richi']),'hashes':hashes}))
"""
    process = subprocess.run([str(python), '-I', '-B', '-c', code], capture_output=True,
                             text=True, timeout=30, env=environment(root))
    if process.returncode:
        raise ValueError(process.stderr)
    return json.loads(process.stdout)


def seed(python, database, fixture):
    (database.parent / 'config.json').write_text('{}\n', encoding='utf-8')
    cli(python, database, ['init'])
    cli(python, database, ['project', 'upsert', '--json', '-'], payload=fixture['projects'])
    cli(python, database, ['entry', 'put', '--json', '-'], payload=[fixture['parent_after']] + fixture['children'])
    edges = [dict(id='part-of:' + child['id'], from_ref='entry:' + child['id'],
                  to_ref='entry:' + fixture['parent_after']['id'], kind='part_of',
                  description='Source-backed detail belongs to DEMO-84; membership does not confirm its hypothesis.',
                  knowledge_state='confirmed', sources=copy.deepcopy(child['sources']),
                  verified_at=child['sources'][0]['observed_at']) for child in fixture['children']]
    cli(python, database, ['edge', 'put', '--json', '-'], payload=edges)


def readonly_digest(database):
    return {suffix or 'database': sha(path) for suffix in ('', '-wal')
            for path in [Path(str(database) + suffix)] if path.exists() and (not suffix or path.stat().st_size)}


def cases(fixture):
    rows = []
    for query in fixture['queries']:
        for budget in (100000, 2000):
            command = ['recall', query['query'], '--project', 'relay', '--limit', str(query['limit']), '--max-chars', str(budget)]
            if query.get('include_hypotheses'):
                command.append('--include-hypotheses')
            rows.append(dict(id='recall-%s-%d' % (query['name'], budget), command=command, budget=budget,
                             target=query['after_id'], required_facts=CASE_FACTS[query['name']]))
    for index, record in enumerate([fixture['parent_after']] + fixture['children']):
        required = [fact['fact_id'] for fact in fixture['preservation_map'] if fact['destination'] == record['id']]
        rows.append(dict(id='get-%d' % index, command=['entry', 'get', record['id']], budget=None,
                         target=record['id'], required_facts=required))
        for budget in (100000, 2000):
            rows.append(dict(id='read-%d-%d' % (index, budget), command=['entry', 'read', record['id'], '--max-chars', str(budget)],
                             budget=budget, target=record['id'], required_facts=required))
    for budget in (100000, 2000):
        rows.append(dict(id='explain-%d' % budget, command=['recall', 'durable-receipt', '--project', 'relay',
                         '--limit', '8', '--max-chars', str(budget), '--expect', 'entry:' + fixture['parent_after']['id']],
                         budget=budget, target=fixture['parent_after']['id'], required_facts=CASE_FACTS['condition']))
    return rows


def cards(value):
    if 'results' in value:
        return value['results']
    if value.get('entry'):
        return [value['entry']]
    return [value] if 'id' in value and 'summary' in value else []


def comparable(value):
    result = copy.deepcopy(value)
    if 'budget' in result:
        result['budget'].pop('output_chars', None)
    return result


def analyze(raw, value, case, catalog, oracle, tokenizers):
    failures, rows = [], cards(value)
    if case['budget'] is not None:
        if len(raw) > case['budget'] or value.get('budget', {}).get('output_chars') != len(raw):
            failures.append('serialized character budget/count')
    if 'no_match' in value and value['no_match'] != (value['candidate_count'] == 0):
        failures.append('no_match differs from retrieval outcome')
    snapshots = []
    for row in rows:
        record = catalog.get(row['id'])
        if record is None:
            failures.append('unknown returned ID')
            continue
        for field in ('knowledge_state', 'work_state', 'verified_at', 'project_ids'):
            if row.get(field) != record.get(field):
                failures.append('state/scope/date changed: ' + field)
        if case['command'][0] == 'recall' and row['knowledge_state'] == 'hypothesis' and '--include-hypotheses' not in case['command']:
            failures.append('hypothesis escaped filter')
        if not row.get('sources') or any(source not in record['sources'] for source in row['sources']):
            failures.append('missing/changed provenance')
        if 'summary_excerpt' in row:
            if row.get('read_more') != {'command': 'entry get', 'id': row['id']}:
                failures.append('full read pointer changed')
            if row.get('source_count') != len(record['sources']) or row.get('sources_omitted') != len(record['sources']) - len(row['sources']):
                failures.append('source omission count changed')
            if row['summary_excerpt'] != record['summary'] and not (row.get('is_excerpt') and row.get('excerpt_note')):
                failures.append('incomplete excerpt is not marked')
        elif any(row.get(field) != expected for field, expected in record.items()):
            failures.append('full get changed fixture content')
        snapshots.append({key: row[key] for key in ('id', 'ref', 'knowledge_state', 'work_state', 'verified_at',
            'project_ids', 'sources', 'is_excerpt', 'source_count', 'sources_omitted', 'read_more') if key in row})
    # Any returned card may supply the exact fact plus its original source. The
    # preservation map's canonical location remains unchanged in the fixture.
    visible = [fact['fact_id'] for fact in oracle if any(fact['text'] in row.get('summary', row.get('summary_excerpt', '')) and
               fact['source'] in {s['reference'] for s in row.get('sources', [])} for row in rows)]
    return {'stdout': raw, 'stdout_sha256': hashlib.sha256(raw.encode()).hexdigest(),
            'chars': len(raw), 'utf8_bytes': len(raw.encode()),
            'tokens': {name: len(enc.encode(raw, disallowed_special=())) for name, enc in tokenizers.items()},
            'returned_refs': [row.get('ref', 'entry:' + row['id']) for row in rows],
            'target_returned': any(row['id'] == case['target'] for row in rows),
            'atomic_facts_with_text_and_source': visible,
            'required_facts_missing': sorted(set(case['required_facts']) - set(visible)),
            'cards': snapshots, 'edge_ids': [edge['id'] for edge in value.get('edges', [])],
            'logical_metadata': {key: value[key] for key in ('no_match', 'candidate_count', 'selection', 'truncated',
                'budget', 'read_more', 'entry_omitted', 'ambiguity', 'explain') if key in value},
            'invariant_failures': sorted(set(failures))}


def skill_measurements(source, tokenizers):
    directory = source / 'skills/project-memory'
    measurements = {}
    for path in [directory / 'SKILL.md'] + sorted((directory / 'references').glob('*.md')):
        text = path.read_text(encoding='utf-8')
        measurements[path.name] = {'sha256': sha(path), 'chars': len(text), 'utf8_bytes': len(text.encode()),
                                  'tokens': {name: len(enc.encode(text, disallowed_special=())) for name, enc in tokenizers.items()}}
    routes = {}
    for name, references in skill_routes(directory).items():
        routes[name] = {'files_read_once': ['SKILL.md'] + references,
            'tokens': {enc: sum(measurements[file]['tokens'][enc] for file in ['SKILL.md'] + references) for enc in tokenizers},
            'chars': sum(measurements[file]['chars'] for file in ['SKILL.md'] + references)}
    return {'files': measurements, 'routes': routes,
            'all_reference_tokens_separate': {enc: sum(row['tokens'][enc] for file, row in measurements.items() if file != 'SKILL.md') for enc in tokenizers}}


def recall_rollup(rows, tokenizers):
    """Measured omissions are visible independently of execution invariants."""
    result = {}
    for budget in (100000, 2000):
        selected = [row for row in rows if row['command'][0] == 'recall' and
                    '--expect' not in row['command'] and row['budget'] == budget]
        result[str(budget)] = {}
        for variant in ('baseline_pretty', 'candidate_pretty', 'candidate_compact'):
            samples = [row['variants'][variant] for row in selected]
            result[str(budget)][variant] = {
                'queries': len(samples), 'chars': sum(row['chars'] for row in samples),
                'tokens': {name: sum(row['tokens'][name] for row in samples) for name in tokenizers},
                'queries_missing_required_facts': sum(bool(row['required_facts_missing']) for row in samples),
                'required_fact_occurrences_missing': sum(len(row['required_facts_missing']) for row in samples),
                'queries_missing_target_ref': sum(not row['target_returned'] for row in samples),
                'missing_by_query': {row['id']: row['variants'][variant]['required_facts_missing']
                                     for row in selected if row['variants'][variant]['required_facts_missing']},
            }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline-python', 'candidate-python', 'baseline-source', 'candidate-source', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--tokenizer-cache', type=Path, help='Enable optional tiktoken proxy counts using this cache')
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('Refusing to overwrite output')
    args.baseline_source = args.baseline_source.resolve()
    args.candidate_source = args.candidate_source.resolve()
    # Preserve the venv interpreter symlink; resolve() would select base Python.
    variants = {'baseline_pretty': (args.baseline_python.absolute(), False),
                'candidate_pretty': (args.candidate_python.absolute(), False),
                'candidate_compact': (args.candidate_python.absolute(), True)}
    tokenizers = {}
    if args.tokenizer_cache:
        os.environ['TIKTOKEN_CACHE_DIR'] = str(args.tokenizer_cache.resolve())
        try:
            import tiktoken
        except ImportError:
            parser.error('Install tiktoken only in the evaluation driver Python, or omit --tokenizer-cache')
        tokenizers = {name: tiktoken.get_encoding(name) for name in TOKENIZERS}
    fixture_path = args.baseline_source / 'tests/fixtures/card_maintenance.json'
    if sha(fixture_path) != sha(args.candidate_source / 'tests/fixtures/card_maintenance.json'):
        parser.error('The public card-maintenance fixture must be unchanged')
    fixture = json.loads(fixture_path.read_text(encoding='utf-8'))
    catalog = {row['id']: row for row in [fixture['parent_after']] + fixture['children']}
    report = {'schema_version': 1, 'started_at': datetime.now(timezone.utc).isoformat(),
              'baseline_revision': BASELINE_REVISION, 'runner_sha256': sha(__file__),
              'fixture_sha256': sha(fixture_path), 'oracle': fixture['preservation_map'],
              'tokenizer_names': list(tokenizers), 'tiktoken_version': version('tiktoken') if tokenizers else None,
              'budgets': {'ample': 100000, 'tight': 2000}, 'cases': [], 'invariant_failures': [],
              'limitations': ['Public synthetic fixture; not a blind evaluation or an estimate of production accuracy.',
                              'Token counts encode actual stdout only; exclude prompts, tool framing, reasoning and billing/cache policy.',
                              'o200k_base/cl100k_base are proxies, not exact Codex billing. No latency, monetary or saved-time claim.',
                              'Ample equality removes only actual budget.output_chars; tight budgets intentionally may change excerpts, sources, cards and edges.',
                              'Exact fact strings plus attached source visibility are non-LLM checks, not semantic entailment or complete-answer quality.',
                              'Skill route volumes add named files once. They do not establish actual agent routing or client loading/caching behavior.',
                              'New seed timestamps can change exact byte hashes/token counts; captured stdout is retained for exact recounts.']}
    with tempfile.TemporaryDirectory(prefix='richi-compact-eval-') as temp:
        root = Path(temp)
        metadata = {name: installed(python, root) for name, python in [('baseline', args.baseline_python), ('candidate', args.candidate_python)]}
        for name, source in [('baseline', args.baseline_source), ('candidate', args.candidate_source)]:
            if metadata[name]['hashes'] != code_hashes(source):
                raise ValueError(name + ' installation differs from source snapshot')
        baseline_digest = hashlib.sha256(json.dumps(metadata['baseline']['hashes'], sort_keys=True,
                                                     separators=(',', ':')).encode()).hexdigest()
        if baseline_digest != BASELINE_CODE_SHA256:
            raise ValueError('Baseline code differs from the pinned 748fc91 snapshot')
        report['baseline_code_sha256'] = baseline_digest
        if any(metadata['baseline'][field] != metadata['candidate'][field] for field in ('python', 'sqlite')):
            raise ValueError('Use matching Python and SQLite versions')
        report['metadata'] = metadata
        report['skills'] = {name: skill_measurements(source, tokenizers) for name, source in
                            [('baseline', args.baseline_source), ('candidate', args.candidate_source)]}
        database = root / 'synthetic.sqlite3'
        seed(args.baseline_python, database, fixture)
        before = readonly_digest(database)
        for case in cases(fixture):
            row = dict(case, variants={})
            parsed = {}
            for name, (python, compact) in variants.items():
                raw, value = cli(python, database, case['command'], compact=compact)
                parsed[name] = value
                row['variants'][name] = analyze(raw, value, case, catalog, fixture['preservation_map'], tokenizers)
                report['invariant_failures'].extend(case['id'] + ': ' + name + ': ' + failure
                    for failure in row['variants'][name]['invariant_failures'])
                if compact and raw != json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n':
                    report['invariant_failures'].append(case['id'] + ': compact stdout does not match compact renderer')
            row['default_stdout_unchanged'] = row['variants']['baseline_pretty']['stdout'] == row['variants']['candidate_pretty']['stdout']
            if not row['default_stdout_unchanged']:
                report['invariant_failures'].append(case['id'] + ': default pretty changed')
            row['logical_payload_equal_except_output_chars'] = comparable(parsed['candidate_pretty']) == comparable(parsed['candidate_compact'])
            if case['budget'] in (None, 100000) and not row['logical_payload_equal_except_output_chars']:
                report['invariant_failures'].append(case['id'] + ': ample/full payload changed beyond output_chars')
            if case['budget'] is None and row['variants']['candidate_compact']['required_facts_missing']:
                report['invariant_failures'].append(case['id'] + ': full get lost atomic fixture facts')
            report['cases'].append(row)
        report['readonly'] = {'before': before, 'after': readonly_digest(database)}
        report['readonly']['unchanged'] = report['readonly']['before'] == report['readonly']['after']
        if not report['readonly']['unchanged']:
            report['invariant_failures'].append('Database/nonempty-WAL bytes changed')
        for name, python, source in [('baseline', args.baseline_python, args.baseline_source), ('candidate', args.candidate_python, args.candidate_source)]:
            if metadata[name] != installed(python, root) or metadata[name]['hashes'] != code_hashes(source):
                report['invariant_failures'].append(name + ': code changed during evaluation')
            if report['skills'][name] != skill_measurements(source, tokenizers):
                report['invariant_failures'].append(name + ': skill files changed during evaluation')
    report['summary'] = {'cases': len(report['cases']), 'stdout_samples': len(report['cases']) * 3,
                         'execution_completed': True,
                         'default_pretty_parity': all(row['default_stdout_unchanged'] for row in report['cases']),
                         'ample_and_full_payload_parity': all(row['logical_payload_equal_except_output_chars'] for row in report['cases'] if row['budget'] in (None, 100000)),
                         'invariant_failures': len(report['invariant_failures']),
                         'tight_cases_with_changed_payload': sum(not row['logical_payload_equal_except_output_chars'] for row in report['cases'] if row['budget'] == 2000),
                         'recall_quality_and_volume': recall_rollup(report['cases'], tokenizers)}
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(json.dumps(report['summary'], indent=2))
    return int(bool(report['invariant_failures']))


if __name__ == '__main__':
    raise SystemExit(main())
