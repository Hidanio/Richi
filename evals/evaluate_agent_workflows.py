#!/usr/bin/env python3
"""Prerecorded synthetic tool workflows: visible payload/context, not model usage."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

from evaluate_indexed_filters import cli, clone, digest, logical, physical, worker

BASELINE_REVISION = 'b8a2a9aa889a7d65d433e985ca3f6a9d4cec4c60'
CORE = 'skills/project-memory/SKILL.md'
SEARCH_DOC = 'skills/project-memory/references/CLI.md'
RETRIEVAL_DOC = 'skills/project-memory/references/RETRIEVAL_TOOLS.md'
QUERY_DOC = 'skills/project-memory/references/QUERY_TOOLS.md'
FILTERS_DOC = 'skills/project-memory/references/FILTERS.md'
EVIDENCE_WHERE = "project = 'atlas' AND kind = 'decision' AND work_state = 'done'"
POSITIVE_WHERE = "kind = 'decision' AND work_state = 'done'"
NEGATIVE_WHERE = "tags HAS 'not-observed-in-fixture'"
FIELDS = ['ref', 'title', 'work_state']


def render(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n'


def fixture():
    data = {'project': [{'id': 'atlas', 'name': 'Atlas'}, {'id': 'beacon', 'name': 'Beacon'}],
            'entry': [], 'entity': [], 'edge': []}
    prose = ('The synthetic transport preserves a receipt for each partition. '
             'The observation applies to a single checked recovery window. '
             'A separate source records the order of journal writes and acknowledgements. '
             'A cold restore needs explicit validation before normal traffic resumes. ')
    for project, count in [('atlas', 60), ('beacon', 20)]:
        for index in range(count):
            identity = project + '-%03d' % index
            selected = project == 'atlas' and index < 3
            topic = 'Pool sizing' if index < 12 else 'Receipt transport'
            value = {'id': identity, 'kind': ('decision', 'experiment', 'task', 'note')[index % 4],
                'title': '%s observation %s' % (topic, identity),
                'summary': (topic + '. ' + prose * 3 +
                    'Final condition: use this result only with durable receipts; it does not prove a production release.'),
                'project_ids': [project], 'knowledge_state': 'hypothesis' if index % 10 == 0 else 'superseded' if index % 10 == 1 else 'confirmed',
                'work_state': ('planned', 'done', 'blocked', None, 'implemented', 'released')[index % 6],
                'verified_at': '2026-10-01', 'sources': [
                    {'reference': 'https://example.invalid/workflows/' + identity, 'label': 'Synthetic observation'},
                    {'reference': 'https://example.invalid/conditions/' + identity, 'label': 'Scope and exceptions'}],
                'tags': ['transport', 'review' if index % 3 else 'retained'], 'aliases': [], 'jira_key': None}
            if selected:
                value.update(kind='decision', work_state='done', knowledge_state='confirmed')
            # Other lexical matches must fail at least one metadata condition.
            elif index < 12:
                value.update(kind='note', work_state='planned')
            data['entry'].append(value)
    rows = [r for r in data['entry'] if r['project_ids'] == ['atlas'] and r['knowledge_state'] == 'confirmed']
    evidence = [r for r in rows if r['kind'] == 'decision' and r['work_state'] == 'done' and r['id'] < 'atlas-012']
    groups = Counter(r['work_state'] for r in rows)
    oracle = {'evidence_refs': ['entry:' + r['id'] for r in evidence],
              'count': len(rows), 'groups': sorted([[k, v] for k, v in groups.items()], key=lambda r: (r[0] is not None, r[0] or '')),
              'exists': [True, False],
              'projection': [['entry:' + r['id'], r['title'], r['work_state']] for r in sorted(rows, key=lambda r: r['id'])],
              'known_refs': ['entry:atlas-000', 'entry:missing-from-fixture']}
    return data, oracle


PROMPTS = {
    'evidence': 'Read the full current confirmed Atlas decisions about pool sizing whose work state is done. Preserve conditions and sources; distinguish them from notes and other projects.',
    'distribution': 'Count current confirmed Atlas entries and give their complete work-state distribution, including a null state if present. Count records, not successful real-world work.',
    'existence': 'Within current confirmed Atlas entries, check separately whether a done decision exists and whether any record has tag not-observed-in-fixture. Answer only about this stored scope.',
    'projection': 'List ref, title and work_state for every current confirmed Atlas entry. Metadata only; no conclusion about evidence or release validity.',
    'known_refs': 'Read entry:atlas-000 and entry:missing-from-fixture. Return the first in full and identify the absent reference explicitly.'}


def docs_section(root, path):
    content = (root / path).read_text(encoding='utf-8')
    if path == SEARCH_DOC:
        start = content.index('## Поиск и контекст')
        end = content.index('\n## ', start + 4)
        return content[start:end] + '\n', 'section: Поиск и контекст'
    if path == FILTERS_DOC:
        return content[:content.index('Date equality,')], 'prefix: strict scope, grammar and string predicates'
    return content, 'whole focused document'


class Transcript:
    def __init__(self, name, arm, python, database, source, prompt):
        self.name, self.arm, self.python, self.database = name, arm, python, database
        self.source, self.prompt = source, prompt
        self.core = (source / CORE).read_text(encoding='utf-8')
        self.steps = []
        self.round = 0

    def append(self, args, stdout, kind='data', code='', intermediate_bytes=0, round_id=None):
        if round_id is None:
            self.round += 1
            round_id = self.round
        self.steps.append({'arguments': render({'cmd': args}), 'stdout': stdout, 'kind': kind,
            'round': round_id, 'analysis_code': code, 'intermediate_off_model_bytes': intermediate_bytes})
        return json.loads(stdout) if kind == 'data' else None

    def command(self, argv, kind='data', round_id=None):
        raw, value, _ = cli(self.python, self.database, argv)
        self.append(shlex.join(['richi', '--compact-json'] + argv), raw, kind, round_id=round_id)
        return value

    def pipeline(self, argv, code):
        raw, _, _ = cli(self.python, self.database, argv)
        proc = subprocess.run([str(self.python), '-I', '-B', '-c', code], input=raw, text=True,
                              capture_output=True, timeout=30)
        if proc.returncode or proc.stderr:
            raise RuntimeError(proc.stderr or proc.stdout)
        command = shlex.join(['richi', '--compact-json'] + argv) + ' | ' + shlex.join(['python3', '-c', code])
        return self.append(command, proc.stdout, code=code, intermediate_bytes=len(raw.encode()))

    def document(self, path, round_id=None):
        content, selection = docs_section(self.source, path)
        self.append('read_document ' + shlex.quote('/installed/' + path) + ' ' + shlex.quote(selection), content, 'documentation', round_id=round_id)

    def semantic_docs(self, paths):
        self.round += 1
        for path in paths:
            self.document(path, round_id=self.round)

    def native_docs(self, names):
        group = 'search' if self.name in ('evidence', 'known_refs') else 'analysis'
        self.command(['capabilities', 'list', '--group', group], kind='documentation')
        # Known independent schema descriptions may be requested in one round.
        self.round += 1
        for name in names:
            self.command(['capabilities', 'describe', name], kind='documentation', round_id=self.round)


def selected(rows):
    return [r for r in rows if 'atlas' in r['project_ids'] and r['knowledge_state'] == 'confirmed']


def evidence(rows):
    return [r for r in selected(rows) if r['kind'] == 'decision' and r['work_state'] == 'done']


def verify_full(rows, expected_refs, expected_full):
    actual = {'entry:' + r['id']: r for r in rows}
    if len(rows) != len(expected_refs) or set(actual) != set(expected_refs):
        raise AssertionError('Full-record set differs from predetermined fixture refs')
    if any(actual[ref] != expected_full[ref] for ref in expected_refs):
        raise AssertionError('A required full record, condition or source changed')
    return {'refs': sorted(actual), 'full_record_sha256': {ref: digest(actual[ref]) for ref in sorted(actual)}}


def fetch_all(transcript, refs):
    args = ['fetch']
    for reference in refs:
        args += ['--ref', reference]
    args += ['--max-chars', '16000']
    results, offset = [], 0
    while True:
        result = transcript.command(args + (['--offset', str(offset)] if offset else []))
        results.extend(result['results'])
        if not result['remaining_count']:
            if not result['request_complete']:
                raise AssertionError('Incomplete fetch falsely reported as complete')
            return results
        next_offset = result['next_offset']
        if not isinstance(next_offset, int) or next_offset <= offset:
            raise AssertionError('Fetch continuation failed to advance')
        offset = next_offset


SCOPE_CODE = "rows=[r for r in json.load(sys.stdin)['entries'] if 'atlas' in r['project_ids'] and r['knowledge_state']=='confirmed']\n"
PREFIX_CODE = 'import json,sys\n'
PRINT_CODE = "print(json.dumps(answer,ensure_ascii=False,separators=(',',':')))\n"
CONTEXT = ['context', '--project', 'atlas', '--limit', '200']
SEARCH = ['search', 'pool', '--project', 'atlas', '--limit', '200']


def baseline_workflow(name, mode, python, database, source, oracle, expected_full):
    t = Transcript(name, 'baseline_' + mode, python, database, source, PROMPTS[name])
    t.document(RETRIEVAL_DOC if name == 'known_refs' or (name == 'evidence' and mode == 'discovery') else SEARCH_DOC)
    if name == 'evidence':
        if mode == 'discovery':
            route = t.command(['discover', 'pool', '--project', 'atlas', '--limit', '100', '--max-chars', '100000'])
            if route['budget']['omitted_results']:
                raise AssertionError('Baseline discovery is incomplete')
            refs = [r['ref'] for r in route['results'] if r['ref'].startswith('entry:') and 'atlas' in r['project_ids']
                    and r['kind'] == 'decision' and r['work_state'] == 'done' and r['knowledge_state'] == 'confirmed']
            items = fetch_all(t, refs)
            if any(item['status'] != 'found' for item in items): raise AssertionError('Evidence fetch incomplete')
            rows = [item['record'] for item in items]
        elif mode == 'python':
            code = PREFIX_CODE + SCOPE_CODE + "answer={'entries':[r for r in rows if r['kind']=='decision' and r['work_state']=='done']}\n" + PRINT_CODE
            rows = t.pipeline(SEARCH, code)['entries']
        else:
            rows = evidence(t.command(SEARCH)['entries'])
        answer = verify_full(rows, oracle['evidence_refs'], expected_full)
    elif name == 'known_refs':
        items = fetch_all(t, oracle['known_refs'])
        if [(i['ref'], i['status']) for i in items] != [(oracle['known_refs'][0], 'found'), (oracle['known_refs'][1], 'missing')]:
            raise AssertionError('Known/missing reference status changed')
        answer = verify_full([items[0]['record']], oracle['known_refs'][:1], expected_full)
        answer['missing'] = oracle['known_refs'][1:]
    else:
        if mode == 'python':
            operation = {
                'distribution': "from collections import Counter\ngroups=Counter(r['work_state'] for r in rows)\nanswer={'count':len(rows),'groups':sorted([[k,v] for k,v in groups.items()],key=lambda r:(r[0] is not None,r[0] or ''))}\n",
                'existence': "answer={'exists':[any(r['kind']=='decision' and r['work_state']=='done' for r in rows),any('not-observed-in-fixture' in r['tags'] for r in rows)]}\n",
                'projection': "answer={'rows':[['entry:'+r['id'],r['title'],r['work_state']] for r in sorted(rows,key=lambda r:r['id'])]}\n"}[name]
            result = t.pipeline(CONTEXT, PREFIX_CODE + SCOPE_CODE + operation + PRINT_CODE)
        else:
            rows = selected(t.command(CONTEXT)['entries'])
            if name == 'distribution':
                groups = Counter(r['work_state'] for r in rows)
                result = {'count': len(rows), 'groups': sorted([[k, v] for k, v in groups.items()], key=lambda r: (r[0] is not None, r[0] or ''))}
            elif name == 'existence':
                result = {'exists': [any(r['kind'] == 'decision' and r['work_state'] == 'done' for r in rows),
                                     any('not-observed-in-fixture' in r['tags'] for r in rows)]}
            else:
                result = {'rows': [['entry:' + r['id'], r['title'], r['work_state']] for r in sorted(rows, key=lambda r: r['id'])]}
        expected = {'distribution': {'count': oracle['count'], 'groups': oracle['groups']},
                    'existence': {'exists': oracle['exists']}, 'projection': {'rows': oracle['projection']}}[name]
        if result != expected: raise AssertionError('Baseline answer differs: ' + name)
        answer = result
    return t, answer


def native_select(t, limit):
    rows, after = [], None
    while True:
        args = ['query', 'select', '--project', 'atlas', '--fields', ','.join(FIELDS), '--limit', str(limit), '--max-chars', '8000']
        if after: args += ['--after', after]
        result = t.command(args)
        if result['columns'] != FIELDS or result.get('oversized'):
            raise AssertionError('Projection lost a field/record')
        rows.extend(result['rows'])
        if not result['has_more']: return rows
        next_after = result['next_after']
        if not next_after or (after and next_after <= after): raise AssertionError('Cursor did not advance')
        after = next_after


def candidate_workflow(name, mode, python, database, source, oracle, expected_full):
    t = Transcript(name, 'candidate_' + mode, python, database, source, PROMPTS[name])
    descriptions = {'evidence': ['discover', 'fetch'], 'distribution': ['query count', 'query facets'],
        'existence': ['query exists'], 'projection': ['query select'], 'known_refs': ['fetch']}
    if mode == 'catalog':
        t.native_docs(descriptions[name])
    guides = {'evidence': [RETRIEVAL_DOC, FILTERS_DOC], 'distribution': [QUERY_DOC],
        'existence': [QUERY_DOC, FILTERS_DOC], 'projection': [QUERY_DOC], 'known_refs': [RETRIEVAL_DOC]}
    t.semantic_docs([SEARCH_DOC, FILTERS_DOC] if mode == 'filtered_search' else guides[name])
    if name == 'evidence' and mode == 'filtered_search':
        rows = t.command(SEARCH + ['--where', EVIDENCE_WHERE])['entries']
        return t, verify_full(rows, oracle['evidence_refs'], expected_full)
    if name == 'evidence':
        route = t.command(['discover', 'pool', '--project', 'atlas', '--where', EVIDENCE_WHERE,
                           '--limit', '100', '--max-chars', '100000'])
        if route['budget']['omitted_results']: raise AssertionError('Candidate discovery incomplete')
        refs = [r['ref'] for r in route['results']]
        if set(refs) != set(oracle['evidence_refs']): raise AssertionError('Scoped discovery set differs')
        items = fetch_all(t, refs)
        if any(item['status'] != 'found' for item in items): raise AssertionError('Evidence incomplete')
        answer = verify_full([item['record'] for item in items], oracle['evidence_refs'], expected_full)
    elif name == 'known_refs':
        items = fetch_all(t, oracle['known_refs'])
        if [(i['ref'], i['status']) for i in items] != [(oracle['known_refs'][0], 'found'), (oracle['known_refs'][1], 'missing')]:
            raise AssertionError('Known/missing outcome differs')
        answer = verify_full([items[0]['record']], oracle['known_refs'][:1], expected_full)
        answer['missing'] = oracle['known_refs'][1:]
    elif name == 'distribution':
        t.round += 1
        count = t.command(['query', 'count', '--project', 'atlas'], round_id=t.round)
        facets = t.command(['query', 'facets', '--project', 'atlas', '--by', 'work_state', '--limit', '200'], round_id=t.round)
        if not count['exact'] or not facets['counts_exact'] or facets['has_more'] or facets['columns'] != ['work_state', 'count']:
            raise AssertionError('Aggregate answer incomplete')
        answer = {'count': count['count'], 'groups': sorted(facets['rows'], key=lambda r: (r[0] is not None, r[0] or ''))}
        if answer != {'count': oracle['count'], 'groups': oracle['groups']}: raise AssertionError('Aggregate oracle mismatch')
    elif name == 'existence':
        t.round += 1
        values = [t.command(['query', 'exists', '--project', 'atlas', '--where', predicate], round_id=t.round)
                  for predicate in (POSITIVE_WHERE, NEGATIVE_WHERE)]
        if not all(v['exact'] for v in values): raise AssertionError('Existence scope is not exact')
        answer = {'exists': [v['exists'] for v in values]}
        if answer['exists'] != oracle['exists']: raise AssertionError('Existence oracle mismatch')
    else:
        answer = {'rows': native_select(t, 200)}
        if answer['rows'] != oracle['projection']: raise AssertionError('Projection oracle mismatch')
    return t, answer


def tokenize(python, texts):
    import os
    cache = os.environ.get('TIKTOKEN_CACHE_DIR')
    names = ('9b5ad71b2ce5302211f9c61530b329a4922fc6a4', 'fb374d419588a4632f3f557e76b4b70aebbca790')
    if not cache or not all((Path(cache) / name).is_file() for name in names):
        raise ValueError('An already populated tokenizer cache is required')
    code = """import json,sys,tiktoken,tiktoken.load,importlib.metadata
tiktoken.load.read_file=lambda path: (_ for _ in ()).throw(RuntimeError('tokenizer cache unavailable'))
texts=json.load(sys.stdin)
counts={name:{key:len(tiktoken.get_encoding(name).encode(value,disallowed_special=())) for key,value in texts.items()} for name in ('o200k_base','cl100k_base')}
print(json.dumps({'version':importlib.metadata.version('tiktoken'),'counts':counts}))
"""
    proc = subprocess.run([str(python), '-I', '-c', code], input=json.dumps(texts), text=True, capture_output=True, timeout=90)
    if proc.returncode: raise RuntimeError(proc.stderr)
    return json.loads(proc.stdout)


def finalize(transcripts, tokenizer):
    texts = {}
    for index, (t, answer) in enumerate(transcripts):
        prefix = str(index) + ':'
        texts[prefix + 'user'] = t.prompt
        texts[prefix + 'core'] = t.core
        for i, step in enumerate(t.steps):
            for field in ('arguments', 'stdout', 'analysis_code'):
                texts[prefix + str(i) + ':' + field] = step[field]
    encoded = tokenize(tokenizer, texts)
    result = []
    for index, (t, answer) in enumerate(transcripts):
        prefix = str(index) + ':'
        entry = {'workflow': t.name, 'arm': t.arm, 'answer': answer, 'answer_sha256': digest(answer),
                 'core_sha256': hashlib.sha256(t.core.encode()).hexdigest(), 'steps': [], 'tokens': {}}
        for i, step in enumerate(t.steps):
            entry['steps'].append({'arguments': json.loads(step['arguments']), 'kind': step['kind'], 'round': step['round'],
                'response_chars': len(step['stdout']), 'response_sha256': hashlib.sha256(step['stdout'].encode()).hexdigest(),
                'analysis_code_chars': len(step['analysis_code']), 'intermediate_off_model_bytes': step['intermediate_off_model_bytes'],
                'tokens': {enc: {field: values[prefix + str(i) + ':' + field] for field in ('arguments', 'stdout', 'analysis_code')}
                           for enc, values in encoded['counts'].items()}})
        for enc, values in encoded['counts'].items():
            metrics = {'user_prompt': values[prefix + 'user'], 'loaded_core_skill': values[prefix + 'core'],
                'tool_arguments': 0, 'documentation_responses': 0, 'data_responses': 0, 'unescaped_handcrafted_code_tokens_not_additive': 0}
            for i, step in enumerate(t.steps):
                metrics['tool_arguments'] += values[prefix + str(i) + ':arguments']
                metrics['documentation_responses' if step['kind'] == 'documentation' else 'data_responses'] += values[prefix + str(i) + ':stdout']
                metrics['unescaped_handcrafted_code_tokens_not_additive'] += values[prefix + str(i) + ':analysis_code']
            for scenario in ('cold_bound_chat', 'operation_only_increment'):
                selected_steps = [(i, s) for i, s in enumerate(t.steps) if scenario == 'cold_bound_chat' or s['kind'] != 'documentation']
                current = values[prefix + 'user'] + (values[prefix + 'core'] if scenario == 'cold_bound_chat' else 0)
                boundaries = [current]
                for round_id in sorted({s['round'] for _, s in selected_steps}):
                    current += sum(values[prefix + str(i) + ':arguments'] + values[prefix + str(i) + ':stdout']
                                   for i, s in selected_steps if s['round'] == round_id)
                    boundaries.append(current)
                metrics[scenario] = {'terminal_visible_tokens': current, 'cumulative_replayed_visible_input': sum(boundaries),
                    'context_at_model_boundaries': boundaries, 'declared_model_decision_rounds_including_final': len(boundaries),
                    'logical_tool_calls': len(selected_steps)}
            entry['tokens'][enc] = metrics
        result.append(entry)
    return result, encoded['version']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline-python', 'candidate-python', 'baseline-source', 'candidate-source', 'tokenizer-python', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--baseline-only', action='store_true', help='Contract smoke only; no candidate comparison')
    args = parser.parse_args()
    if args.output.exists(): parser.error('Use a new report path')
    data, oracle = fixture()
    transcripts = []
    with tempfile.TemporaryDirectory(prefix='richi-agent-workflows-') as tmp:
        root = Path(tmp)
        old_db, new_db = root / 'baseline/memory.sqlite3', root / 'candidate/memory.sqlite3'
        seeded = worker(args.baseline_python, root, {'action': 'seed', 'database': str(old_db), 'corpus': data})
        build_canonical_unchanged = None
        database_arms = [('baseline', old_db)]
        if not args.baseline_only:
            clone(old_db, new_db)
            cli(args.candidate_python, new_db, ['search-index', 'build'])
            build_canonical_unchanged = logical(new_db) == seeded['logical_sha256']
            if not build_canonical_unchanged: raise AssertionError('Index build changed canonical fixture rows')
            database_arms.append(('candidate', new_db))
        runtime = {arm: worker(getattr(args, arm + '_python'), root, {'action': 'measure',
            'database': str(db), 'cases': [], 'repeats': 1})['runtime'] for arm, db in database_arms}
        before = {arm: (logical(db), physical(db)) for arm, db in database_arms}
        expected_full = {}
        for reference in oracle['evidence_refs']:
            _, record, _ = cli(args.baseline_python, old_db, ['entry', 'get', reference.partition(':')[2]])
            expected_full[reference] = record
        for name in PROMPTS:
            modes = ['raw', 'python', 'discovery'] if name == 'evidence' else ['direct'] if name == 'known_refs' else ['raw', 'python']
            for mode in modes:
                transcripts.append(baseline_workflow(name, mode, args.baseline_python, old_db, args.baseline_source, oracle, expected_full))
            if not args.baseline_only:
                for mode in (('native', 'catalog', 'filtered_search') if name == 'evidence' else ('native', 'catalog')):
                    transcripts.append(candidate_workflow(name, mode, args.candidate_python, new_db, args.candidate_source, oracle, expected_full))
        pagination = None
        if not args.baseline_only:
            probe = Transcript('projection_cursor_contract', 'candidate_native', args.candidate_python, new_db,
                               args.candidate_source, 'Verify complete projected metadata using seven-row pages.')
            rows = native_select(probe, 7)
            if rows != oracle['projection']: raise AssertionError('Cursor oracle mismatch')
            pagination = {'calls': len(probe.steps), 'rows': len(rows), 'exact_order_and_union': True,
                          'calls_excluded_from_main_workflows': True}
            transcripts.append((probe, {'rows': rows}))
        invariants = {arm: {'canonical_unchanged': logical(db) == before[arm][0], 'physical_unchanged': physical(db) == before[arm][1]}
                      for arm, db in database_arms}
    workflows, version = finalize(transcripts, args.tokenizer_python)
    report = {'format_version': 1, 'declared_baseline_revision': BASELINE_REVISION,
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'fixture_sha256': digest(data),
        'fixture_scope': {'atlas_entries': 60, 'beacon_entries': 20, 'baseline_context_limit': 200, 'relations': 0},
        'oracle': oracle, 'baseline_only': args.baseline_only, 'tokenizer_version': version,
        'workflows': workflows, 'pagination_contract': pagination, 'read_invariants': invariants,
        'runtime': runtime, 'build_canonical_unchanged': build_canonical_unchanged,
        'limitations': [
            'Prerecorded plans and deterministic fixture oracles; no model answer, accuracy, reasoning or billing was measured.',
            'Normalized model-visible command arguments exclude harness DB/config/home flags and provider transport/framing.',
            'Core skill and semantic guides are measured in every candidate mode; optional catalog adds list/describe. Direct evidence search loads the search guide section and filter semantics. Workspace binding/project validation are assumed already complete.',
            'Cold bound-chat context includes loaded core and operation docs; operation-only increment excludes unknown prior context, not an assertion that prior context is free.',
            'Cumulative input sums independently encoded visible messages at each declared model decision boundary; assumes no compaction/caching and excludes generated reasoning/answers.',
            'Every extra prior-context token adds one terminal token and one token per model decision boundary to cumulative input.',
            'Raw-baseline oracle operations represent unmeasured model-side selection/arithmetic, not a tested autonomous agent.',
            'Python baseline executes the recorded code; its raw intermediate data stays off-model and is not charged as visible context.',
            'Aggregates and projections deliberately omit evidence bodies; only evidence/known-ref workflows require full-record fidelity.',
            'Index construction and fixture/oracle setup are outside agent read workflows; no latency is measured.',
            'Workflows are independent task scenarios; summing their cold totals is not a measured single-session conversation.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as handle: json.dump(report, handle, ensure_ascii=False, indent=2); handle.write('\n')
    print(json.dumps({'report': str(args.output), 'workflows': len(workflows), 'read_invariants': invariants}))
    return int(not all(value for arm in invariants.values() for value in arm.values()))


if __name__ == '__main__':
    sys.exit(main())
