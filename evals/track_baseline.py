#!/usr/bin/env python3
"""Working pre-track CLI composition for the frozen synthetic tracks corpus.

The explicit membership tags and summary JSON are fixture authoring conventions,
not features claimed for the old runtime. No raw database reads or hidden native
track calls occur here. Local CLI response bytes are measured separately from the
small postprocessed response shown to a model. Import run() for core measurements.
"""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys

ALL_STATES = ['--include-hypotheses', '--include-superseded']
TRACK = 'entry:track:reliability'


def render(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n'


def predicate(tracks):
    return '(' + ' OR '.join("tags HAS 'in-track:" + ref.replace("'", "''") + "'" for ref in tracks) + ')'


def details(record):
    """Read author-provided structured facts; never infer a research conclusion."""
    try:
        value = json.loads(record['summary'])
        return value if isinstance(value, dict) else {'text': record['summary']}
    except (ValueError, TypeError):
        return {'text': record['summary']}


def run(call, scenario, *, max_chars=32000, task_ref='entry:task:01', cursor=None):
    calls, local_bytes, commands = 0, 0, []

    def invoke(args):
        nonlocal calls, local_bytes
        response = call(args)
        calls += 1
        local_bytes += len(render(response).encode())
        commands.append(args)
        return response

    def fetch(refs):
        records = {}
        for start in range(0, len(refs), 32):
            selected = list(dict.fromkeys(refs[start:start + 32]))
            offset = 0
            while True:
                response = invoke(['fetch', *sum((['--ref', ref] for ref in selected), []),
                                   '--offset', str(offset), '--max-chars', '100000'])
                for item in response['results']:
                    if item['status'] != 'found':
                        raise ValueError('Fixture baseline requires whole record: ' + item['ref'])
                    records[item['ref']] = item['record']
                if response['request_complete']:
                    break
                if response['next_offset'] == offset:
                    raise ValueError('Baseline fetch did not make progress')
                offset = response['next_offset']
        return records

    def select(where, fields='ref,kind,title,knowledge_state,work_state', complete=False):
        result, after = [], None
        while True:
            args = ['query', 'select', '--where', where, '--fields', fields,
                    '--limit', '200' if complete else '20', '--max-chars', '100000', *ALL_STATES]
            if after is not None:
                args += ['--after', after]
            response = invoke(args)
            result.extend(dict(zip(response['columns'], row)) for row in response['rows'])
            if not complete or not response['has_more']:
                return result, response
            after = response['next_after']

    if scenario in ('strict', 'broad'):
        where = predicate([TRACK]) if scenario == 'strict' else "project = 'relay'"
        rows, raw = select(where)
        answer = {'rows': rows, 'scope': {'where': where, 'strict': True},
                  'has_more': raw['has_more'], 'next_after': raw['next_after']}
    elif scenario == 'aggregates':
        answer = invoke(['query', 'facets', '--where', predicate([TRACK]), '--by', 'kind',
                         '--by', 'work_state', '--limit', '200', '--max-chars', '100000', *ALL_STATES])
    elif scenario == 'changes':
        # Pre-track CLI has per-object history, not a global journal. Poll the
        # explicitly known membership set and track edge histories. A per-object
        # revision map avoids timestamp races but grows with tracked objects.
        neighborhood = invoke(['graph', 'neighbors', TRACK, '--depth', '1', '--limit', '200', *ALL_STATES])
        refs = [node['id'] for node in neighborhood['nodes'] if node['id'].startswith('entry:')]
        refs += [edge['id'] for edge in neighborhood['edges']
                 if edge.get('origin') == 'explicit' and edge['kind'] == 'in_track']
        seen = cursor or {}
        events, next_seen = [], dict(seen)
        for ref in refs:
            family, identity = ref.split(':', 1)
            value = invoke([family, 'get', identity, '--history'])
            for event in value['history']:
                if event['id'] > seen.get(ref, 0):
                    events.append({'ref': ref, 'revision': event['id'], 'changed_at': event['changed_at'],
                                   'after': event['after']})
                next_seen[ref] = max(next_seen.get(ref, 0), event['id'])
        events.sort(key=lambda x: (x['changed_at'], x['ref'], x['revision']))
        answer = {'events': events, 'cursor': next_seen, 'scope': {'track': TRACK},
                  'coverage': 'bounded direct neighborhood with per-object history; no global snapshot across CLI calls'}
    else:
        if scenario == 'standalone':
            task_ref = 'entry:task:20'
        task = fetch([task_ref])[task_ref]
        routing = details(task)
        track_refs = routing.get('explicit_tracks', [])
        tracks = fetch(track_refs)
        refs = list(routing.get('evidence_refs', []))
        counts = None
        if track_refs:
            selected, _ = select(predicate(track_refs) + " AND kind != 'task'", complete=True)
            refs += [item['ref'] for item in selected]
            counts = invoke(['query', 'facets', '--where', predicate(track_refs), '--by', 'kind',
                             '--by', 'work_state', '--limit', '200', '--max-chars', '100000', *ALL_STATES])
            for track in tracks.values():
                refs += details(track).get('evidence_refs', [])
        records = fetch(list(dict.fromkeys(refs)))

        def compact(ref, value):
            content = details(value)
            return dict(ref=ref, title=value['title'], research=content.get('research'),
                        text=content.get('text'), sources=value.get('sources', []),
                        explicit_tracks=content.get('explicit_tracks', []), evidence_refs=content.get('evidence_refs', []),
                        project_ids=value.get('project_ids', []), knowledge_state=value['knowledge_state'],
                        work_state=value.get('work_state'))

        answer = {'task': compact(task_ref, task), 'tracks': [compact(ref, value) for ref, value in tracks.items()],
                  'records': [compact(ref, value) for ref, value in sorted(records.items())],
                  'counts': counts, 'scope': {'explicit_tracks': track_refs, 'expand': 'query select without track predicate'},
                  'goal_effect': 'unknown', 'omitted_refs': []}
        # Whole-card omissions remain visible and directly readable. Preserve
        # negative/hypothesis presence in pointers even when details do not fit.
        while len(render(answer)) > max_chars and answer['records']:
            item = answer['records'].pop()
            answer['omitted_refs'].append({'ref': item['ref'], 'role': (item['research'] or {}).get('role'),
                'outcome': (item['research'] or {}).get('outcome'), 'read_more': ['fetch', '--ref', item['ref']]})
        if len(render(answer)) > max_chars:
            raise ValueError('Baseline metadata exceeds requested output budget')
    return {'answer': answer, 'instrumentation': {'calls': calls, 'local_response_bytes': local_bytes,
                                                'commands': commands}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--scenario', choices=('context', 'standalone', 'strict', 'broad', 'aggregates', 'changes'), required=True)
    parser.add_argument('--max-chars', type=int, default=32000)
    parser.add_argument('--cursor-file', type=Path)
    parser.add_argument('--metrics-file', type=Path, help='Optional local instrumentation; stdout remains only the model-visible answer')
    parser.add_argument('--dispatch', choices=('module', 'subprocess'), default='module',
                        help='Execute installed public CLI main in this fresh process, or spawn the full launcher for every command')
    args = parser.parse_args()
    def call(arguments):
        argv = ['--config', str(args.database.parent / 'config.json'), '--db', str(args.database), '--compact-json', *arguments]
        if args.dispatch == 'subprocess':
            proc = subprocess.run([str(args.python), '-I', '-B', '-m', 'richi', *argv],
                capture_output=True, text=True, encoding='utf-8', timeout=180)
            code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        else:
            if Path(sys.executable).absolute() != args.python.absolute():
                raise ValueError('Module dispatch must run under the requested installed Python')
            # Invoke the installed CLI entry point with its real parser, config
            # resolution, handlers and stdout serialization. No SQL/private
            # retrieval functions are used by the postprocessor. The harness
            # isolates the synthetic home/config before starting this process.
            from richi.memory import main as cli_main
            captured, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(captured), redirect_stderr(errors):
                code = cli_main(argv)
            stdout, stderr = captured.getvalue(), errors.getvalue()
        if code or stderr:
            raise RuntimeError(stderr or stdout)
        return json.loads(stdout)
    result = run(call, args.scenario, max_chars=args.max_chars,
                 cursor=json.loads(args.cursor_file.read_text()) if args.cursor_file else None)
    result['instrumentation'].update(dispatch_mode=args.dispatch,
        processes=1 if args.dispatch == 'module' else 1 + result['instrumentation']['calls'])
    if args.scenario == 'changes' and args.cursor_file:
        # Keep the potentially large polling state local; a model needs only
        # the path for the next operation, just as it needs an opaque cursor.
        next_file = args.cursor_file.with_suffix('.next.json')
        next_file.write_text(render(result['answer']['cursor']))
        result['answer']['cursor'] = {'state_file': str(next_file)}
    if args.metrics_file:
        args.metrics_file.write_text(render(result['instrumentation']))
    sys.stdout.write(render(result['answer']))


if __name__ == '__main__':
    main()
