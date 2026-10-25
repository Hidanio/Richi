"""Strict explicit track scope and scalar research queries on synthetic stores."""
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from richi import graph, memory, output, query_filters, query_tools, research


class TrackFilterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='richi-track-filters-')
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'synthetic.sqlite3'
        self.conn = memory.connect(self.path, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.path)
        for project in ('alpha', 'beta'):
            memory.project_put(self.conn, {'id': project, 'name': project})
        self.entry('track:a', role='track')
        self.entry('track:b', role='track')
        self.entry('track:empty', role='track')
        self.entry('task:shared', role='task', project_ids=['alpha'])
        self.entry('task:beta', role='task', project_ids=['beta'], knowledge_state='hypothesis')
        self.entry('standalone', project_ids=['alpha'])
        self.entry('detail', project_ids=['alpha'])
        self.entry('detached', project_ids=['alpha'])
        self.entry('old-result', knowledge_state='superseded')
        self.edge('a-shared', 'task:shared', 'track:a')
        self.edge('b-shared', 'task:shared', 'track:b')
        self.edge('a-beta', 'task:beta', 'track:a', knowledge_state='hypothesis')
        self.edge('a-old', 'old-result', 'track:a')
        self.edge('a-detached', 'detached', 'track:a', knowledge_state='superseded')
        self.edge('detail-parent', 'detail', 'task:shared', kind='part_of')

    def entry(self, identity, role=None, **changes):
        value = dict(id=identity, kind='note', title='Synthetic title', summary='Evidence',
                     sources=[{'reference': 'fixture:source'}], knowledge_state='confirmed',
                     project_ids=[], expected_updated_at=None)
        if role:
            value['kind'] = research.ROLE_KINDS[role]
            value['research'] = dict(role=role, motivation={'text': 'Original cause',
                                    'sources': [{'reference': 'fixture:motivation'}]},
                                    expected_outcome='Expected effect')
        value.update(changes)
        return memory.entry_put(self.conn, value)

    def edge(self, identity, member, track, **changes):
        value = dict(id=identity, from_ref='entry:' + member, to_ref='entry:' + track,
                     kind='in_track', description='Explicit test contribution',
                     knowledge_state='confirmed', sources=[{'reference': 'fixture:membership'}],
                     expected_updated_at=None)
        value.update(changes)
        return graph.put(self.conn, value, 'edge', memory)

    def query(self, action='select', **changes):
        args = dict(action=action, family='entry', project=None, track=None, where=None,
                    include_hypotheses=False, include_superseded=False, limit=20,
                    max_chars=8000, fields='ref,role', after=None, by=['role'])
        args.update(changes)
        with output.json_mode(compact=True):
            result = query_tools.run(self.conn, SimpleNamespace(**args), memory)
            self.assertLessEqual(len(output.rendered(result)), args['max_chars'])
            self.assertEqual(result['budget']['output_chars'], len(output.rendered(result)))
        return result

    def refs(self, expression):
        parsed = query_filters.parse(expression, memory)
        parsed.prepare(self.conn)
        sql, parameters = parsed.sql('entry')
        return {row[0] for row in self.conn.execute('SELECT e.id FROM entries AS e WHERE ' + sql, parameters)}

    def test_direct_membership_never_inherits_project_or_part_of(self):
        expected = {'task:shared', 'task:beta', 'old-result'}
        self.assertEqual(self.refs("track='track:a'"), expected)
        page = self.query(track='entry:track:a')
        self.assertEqual({row[0] for row in page['rows']}, {'entry:' + x for x in expected})
        self.assertEqual(page['scope']['knowledge_states'], ['confirmed', 'hypothesis', 'superseded'])
        self.assertEqual(page['scope']['membership'], 'direct_active_in_track')
        self.assertEqual(self.query('count', track='track:a', project='alpha')['count'], 1)
        self.assertEqual(self.query('count', track='track:a', project='beta')['count'], 1)
        self.assertEqual(self.query('count', track='track:empty')['count'], 0)
        with self.assertRaisesRegex(memory.MemoryError, 'Unknown track'):
            self.query('count', track='standalone')

    def test_set_semantics_include_empty_sets_and_deduplicate(self):
        self.assertEqual(self.refs("track IN ('track:a','track:b')"),
                         {'task:shared', 'task:beta', 'old-result'})
        self.assertEqual(self.refs("track='track:a' AND track='track:b'"), {'task:shared'})
        all_ids = self.refs(None)
        self.assertEqual(self.refs("track != 'track:a'"), all_ids - {'task:shared', 'task:beta', 'old-result'})
        self.assertEqual(self.refs("track NOT IN ('track:a','track:b')"), self.refs('track IS NULL'))
        self.assertEqual(self.refs("track LIKE 'track:%'"), self.refs('track IS NOT NULL'))
        self.assertEqual(self.refs("track NOT LIKE 'track:%'"), self.refs('track IS NULL'))

    def test_legacy_state_gate_remains_explicit_with_where_only(self):
        self.assertEqual(self.query('count', where="track='track:a'")['count'], 1)
        self.assertEqual(self.query('count', where="track='track:a'", include_hypotheses=True,
                                    include_superseded=True)['count'], 3)
        self.assertEqual(self.query('count', track='track:a', where="knowledge_state='confirmed'")['count'], 1)

    def test_role_projection_and_facets_are_complete_without_payload_reads(self):
        denied = {'payload', 'summary', 'sources', 'tags', 'aliases'}
        def authorizer(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and column in denied:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.conn.set_authorizer(authorizer)
        try:
            result = self.query('facets', track='track:a', by=['role', 'knowledge_state'])
            self.assertEqual(sum(row[-1] for row in result['rows']), 3)
            page = self.query(track='track:a', fields='role,assessment,outcome,applicability,observed_at')
            self.assertEqual(len(page['rows']), 3)
            self.assertEqual(self.query('count', track='track:a', where="role='task' AND applicability='unknown'")['count'], 2)
            self.assertEqual(self.query('count', track='track:a', where='role IS NULL')['count'], 1)
        finally:
            self.conn.set_authorizer(None)

    def test_stable_metadata_pages_return_every_unique_member(self):
        after, collected = None, []
        while True:
            page = self.query(track='track:a', limit=1, after=after)
            collected.extend(row[0] for row in page['rows'])
            if not page['has_more']:
                break
            after = page['next_after']
        self.assertEqual(collected, ['entry:old-result', 'entry:task:beta', 'entry:task:shared'])

    def test_outcomes_assessments_and_observation_dates_are_independent(self):
        self.entry('plan', kind='experiment', research={'role': 'experiment', 'method': 'Controlled comparison'})
        self.entry('claim', kind='investigation', research={'role': 'hypothesis', 'claim': 'Cache helps',
                   'scope': 'Workload v2', 'expected_effect': 'Lower latency', 'validation_method': 'Compare p95'})
        self.edge('claim-b', 'claim', 'track:b')
        for index, outcome in enumerate(('supporting', 'negative', 'not_completed')):
            identity = 'run:' + str(index)
            research.write(self.conn, {'request_id': identity, 'entries': [{
                'id': identity, 'kind': 'experiment', 'title': 'Observed ' + outcome,
                'summary': 'Conditions are preserved', 'knowledge_state': 'confirmed',
                'sources': [{'reference': 'fixture:observed'}], 'expected_revision': 0,
                'research': {'role': 'run', 'observed_at': '2026-10-25T09:00:00+01:00',
                             'conditions': 'workload ' + str(index), 'outcome': outcome,
                             'measurements': [{'name': 'p95', 'value': 20, 'unit': 'ms'}]}
            }], 'edges': [{
                'id': identity + '-of', 'from_ref': 'entry:' + identity, 'to_ref': 'entry:plan',
                'kind': 'run_of', 'description': 'Execution of plan', 'knowledge_state': 'confirmed',
                'sources': [{'reference': 'fixture:observed'}], 'expected_revision': 0
            }]}, memory)
            self.edge(identity + '-b', identity, 'track:b')
        self.assertEqual(self.query('count', track='track:b', where="outcome='negative'")['count'], 1)
        self.assertEqual(self.query('count', track='track:b', where="assessment='untested'")['count'], 1)
        self.assertEqual(self.query('count', track='track:b', where="observed_at='2026-10-25T08:00:00Z'")['count'], 3)
        result = self.query('facets', track='track:b', where="role='run'", by=['outcome', 'knowledge_state'])
        self.assertEqual(result['rows'], [['negative', 'confirmed', 1], ['not_completed', 'confirmed', 1],
                                         ['supporting', 'confirmed', 1]])
        self.assertNotIn('claim', self.refs("NOT assessment='untested'"))
        self.assertNotIn('standalone', self.refs("NOT assessment='untested'"))

    def test_entities_have_empty_track_sets_and_null_research_scalars(self):
        graph.put(self.conn, dict(id='component', kind='component', title='Component', summary='Meaning',
                                 knowledge_state='confirmed', sources=[{'reference': 'fixture:entity'}]),
                  'entity', memory)
        self.assertEqual(self.query('count', family='entity', track='track:a')['count'], 0)
        self.assertEqual(self.query('count', family='entity', where='track IS NULL AND role IS NULL')['count'], 1)
        self.assertEqual(self.query(family='entity', fields='role,outcome')['rows'], [['entity:component', None, None]])

    def test_schema_two_still_reads_normally_but_new_filters_require_migration(self):
        legacy = sqlite3.connect(':memory:')
        self.addCleanup(legacy.close)
        legacy.execute('CREATE TABLE entries(id TEXT, knowledge_state TEXT)')
        legacy.execute("INSERT INTO entries VALUES ('old','confirmed')")
        legacy.execute('PRAGMA user_version=2')
        with self.assertRaisesRegex(memory.MemoryError, 'schema 3'):
            query_filters.parse("role='task'", memory).prepare(legacy)
        with self.assertRaisesRegex(memory.MemoryError, 'schema 3'):
            query_filters.parse("track='track:a'", memory).prepare(legacy)
        parsed = query_filters.parse("id='old'", memory)
        parsed.prepare(legacy)
        sql, params = parsed.sql('entry')
        self.assertEqual(legacy.execute('SELECT e.id FROM entries AS e WHERE ' + sql, params).fetchone()[0], 'old')


if __name__ == '__main__':
    unittest.main()
