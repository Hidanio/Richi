"""Public storage invariants with synthetic stores; no personal memory."""
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from richi import memory, graph, research
from cli_environment import cli_environment


SOURCE = [{'reference': 'fixture://observation'}]


class ResearchStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'memory.sqlite3'
        self.conn = memory.connect(self.path, create=True)
        memory.initialize(self.conn, self.path)
        memory.project_put(self.conn, {'id': 'one', 'name': 'One'})
        memory.project_put(self.conn, {'id': 'two', 'name': 'Two'})

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def card(self, identity, role='task', **research_fields):
        details = {'role': role}
        if role in {'task', 'track'}:
            details.update(motivation={'text': 'Original need', 'sources': SOURCE}, expected_outcome='Useful outcome')
        if role == 'hypothesis':
            details.update(claim='Claim', scope='Synthetic fixture', expected_effect='Lower latency', validation_method='Controlled trial')
        if role == 'experiment':
            details.update(method='Controlled trial', metrics=['latency'], interpretation_criteria=['p95 lower'])
        if role == 'run':
            details.update(observed_at='2026-10-25T09:00:00Z', conditions='Dataset A, 100 rows',
                           outcome='negative', measurements=[{'name': 'latency', 'value': 40, 'unit': 'ms'}])
        details.update(research_fields)
        return {'id': identity, 'kind': research.ROLE_KINDS[role], 'title': identity, 'summary': 'Synthetic evidence',
                'sources': SOURCE, 'knowledge_state': 'confirmed', 'project_ids': ['one'], 'research': details}

    def edge(self, identity, start, end, kind='in_track', **fields):
        result = {'id': identity, 'from_ref': 'entry:' + start, 'to_ref': 'entry:' + end,
                  'kind': kind, 'description': 'Explicit contribution', 'knowledge_state': 'confirmed', 'sources': SOURCE}
        result.update(fields)
        return result

    def put(self, value):
        return memory.entry_put(self.conn, value)

    def put_edge(self, value):
        return graph.put(self.conn, value, 'edge', memory)

    def editable(self, identity):
        current = memory.entry_get(self.conn, identity)
        payload = {key: value for key, value in current.items() if key not in {'revision', 'created_at', 'updated_at'}}
        payload['expected_revision'] = current['revision']
        return payload

    def setup_run(self):
        self.put(self.card('track', 'track'))
        self.put(self.card('experiment', 'experiment'))
        self.put_edge(self.edge('experiment-track', 'experiment', 'track'))
        bundle = {'request_id': 'create-run', 'entries': [self.card('run', 'run')],
                  'edges': [self.edge('run-experiment', 'run', 'experiment', 'run_of')]}
        result = research.write(self.conn, bundle, memory)
        return bundle, result

    def test_native_role_cas_and_project_scope_are_independent(self):
        self.put(self.card('track', 'track'))
        self.put(self.card('task'))
        before = memory.entry_get(self.conn, 'task')
        self.put_edge(self.edge('member', 'task', 'track'))
        self.assertEqual(before, memory.entry_get(self.conn, 'task'))
        current = self.editable('task')
        current['title'] = 'New title'
        current.pop('research')  # Generic update preserves native fields.
        self.put(current)
        after = memory.entry_get(self.conn, 'task')
        self.assertEqual(after['research'], before['research'])
        self.assertEqual(after['project_ids'], ['one'])
        self.assertEqual(after['revision'], before['revision'] + 1)
        with self.assertRaisesRegex(memory.MemoryError, 'Revision conflict'):
            self.put(current)
        current = self.editable('task')
        current['title'] = 'Blind overwrite'
        current.pop('expected_revision')
        with self.assertRaisesRegex(memory.MemoryError, 'expected_revision'):
            self.put(current)

    def test_run_bundle_required_link_atomicity_and_stable_replay(self):
        run = self.card('run', 'run')
        with self.assertRaisesRegex(memory.MemoryError, 'atomically'):
            self.put(run)
        before = self.conn.execute('SELECT count(*) FROM research_events').fetchone()[0]
        with self.assertRaisesRegex(memory.MemoryError, 'exactly one'):
            research.write(self.conn, {'request_id': 'broken', 'entries': [run]}, memory)
        self.assertIsNone(memory.entry_record(self.conn, 'run'))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM research_events').fetchone()[0], before)
        bundle, initial = self.setup_run()
        updated = self.editable('run')
        updated['title'] = 'Current title'
        self.put(updated)
        replay = research.write(self.conn, bundle, memory)
        self.assertTrue(replay['replayed'])
        self.assertEqual(initial['entries'], replay['entries'])
        self.assertEqual(memory.entry_get(self.conn, 'run')['title'], 'Current title')
        changed = copy.deepcopy(bundle)
        changed['entries'][0]['title'] = 'Different request'
        with self.assertRaisesRegex(memory.MemoryError, 'different payload'):
            research.write(self.conn, changed, memory)

    def test_observation_time_revision_correction_history_and_same_timestamp(self):
        self.setup_run()
        before = memory.entry_get(self.conn, 'run', history=True)
        stamp = before['updated_at']
        value = self.editable('run')
        value['title'] = 'Edited title'
        with patch.object(memory, 'now', return_value=stamp):
            self.put(value)
            value = self.editable('run')
            value['summary'] = 'More precise summary'
            self.put(value)
        after = memory.entry_get(self.conn, 'run', history=True)
        self.assertEqual(after['research']['observed_at'], before['research']['observed_at'])
        self.assertEqual(after['revision'], before['revision'] + 2)
        value = self.editable('run')
        value['research']['measurements'][0]['value'] = 41
        with self.assertRaisesRegex(memory.MemoryError, 'correction_reason'):
            self.put(value)
        value['research']['correction_reason'] = 'Fixed transcription from raw source'
        self.put(value)
        after = memory.entry_get(self.conn, 'run', history=True)
        self.assertEqual(after['history'][0]['after']['research']['measurements'][0]['value'], 40)
        self.assertEqual(after['research']['measurements'][0]['value'], 41)
        self.assertEqual(after['knowledge_state'], 'confirmed')
        self.assertEqual(after['research']['outcome'], 'negative')

    def test_notifications_capture_attach_detach_indirect_and_entity_edits(self):
        self.setup_run()
        events = [tuple(row) for row in self.conn.execute(
            'SELECT e.object_ref,e.event_kind FROM research_events e JOIN research_event_tracks t ON t.seq=e.seq '
            "WHERE t.track_id='track' ORDER BY e.seq")]
        self.assertIn(('entry:run', 'entry_created'), events)
        membership = graph.get(self.conn, 'edge', 'experiment-track', memory, history=False)
        payload = {key: value for key, value in membership.items() if key not in {'revision', 'created_at', 'updated_at'}}
        payload.update(knowledge_state='superseded', expected_revision=membership['revision'])
        self.put_edge(payload)
        detached = self.conn.execute("SELECT e.object_ref FROM research_events e JOIN research_event_tracks t ON t.seq=e.seq "
                                     "WHERE t.track_id='track' AND e.event_kind='detached'").fetchall()
        self.assertEqual([row[0] for row in detached], ['edge:experiment-track'])
        self.assertEqual(research.track_scopes(self.conn, 'entry:run'), set())
        goal = {'id': 'goal', 'kind': 'document', 'title': 'Business need', 'summary': 'Expected effect unmeasured',
                'knowledge_state': 'confirmed', 'sources': SOURCE}
        graph.put(self.conn, goal, 'entity', memory)
        self.put_edge(self.edge('goal-link', 'track', 'unused', 'pursues_goal', to_ref='entity:goal'))
        goal.update(title='Current direction', expected_revision=1)
        graph.put(self.conn, goal, 'entity', memory)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM research_events e JOIN research_event_tracks t ON t.seq=e.seq "
                                             "WHERE t.track_id='track' AND e.object_ref='entity:goal' AND event_kind='entity_updated'").fetchone())

    def test_typed_links_cannot_duplicate_retarget_or_orphan_run(self):
        self.setup_run()
        with self.assertRaisesRegex(memory.MemoryError, 'already exists'):
            self.put_edge(self.edge('duplicate', 'experiment', 'track'))
        existing = graph.get(self.conn, 'edge', 'run-experiment', memory, history=False)
        update = {key: value for key, value in existing.items() if key not in {'revision', 'created_at', 'updated_at'}}
        update.update(expected_revision=existing['revision'], knowledge_state='superseded')
        with self.assertRaisesRegex(memory.MemoryError, 'exactly one'):
            self.put_edge(update)
        update.update(knowledge_state='confirmed', kind='informs')
        with self.assertRaisesRegex(memory.MemoryError, 'identity cannot change'):
            self.put_edge(update)
        self.assertEqual(graph.get(self.conn, 'edge', 'run-experiment', memory, history=False), existing)

    def test_bundle_conflict_rolls_back_earlier_card_and_all_journal_changes(self):
        self.setup_run()
        before = self.conn.execute('SELECT max(seq) FROM research_events').fetchone()[0]
        run = self.editable('run')
        run.update(title='Conflicting title', expected_revision=0)
        with self.assertRaisesRegex(memory.MemoryError, 'Revision conflict'):
            research.write(self.conn, {'request_id': 'conflict', 'entries': [self.card('new-task'), run]}, memory)
        self.assertIsNone(memory.entry_record(self.conn, 'new-task'))
        self.assertEqual(self.conn.execute('SELECT max(seq) FROM research_events').fetchone()[0], before)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM research_requests WHERE request_id='conflict'").fetchone())

    def test_motivation_and_sourced_direction_history(self):
        self.put(self.card('task'))
        value = self.editable('task')
        value['research']['motivation']['text'] = 'Rewritten past'
        with self.assertRaisesRegex(memory.MemoryError, 'historical'):
            self.put(value)
        value = self.editable('task')
        value['research']['expected_outcome'] = 'Revised outcome'
        with self.assertRaisesRegex(memory.MemoryError, 'direction change'):
            self.put(value)
        value['research']['direction_changes'].append({'changed_at': '2026-10-25', 'reason': 'New constraints',
                                                       'sources': SOURCE, 'expected_outcome': 'Revised outcome'})
        self.put(value)
        self.assertEqual(memory.entry_get(self.conn, 'task')['research']['motivation']['text'], 'Original need')

    def test_explicit_migration_preserves_legacy_data_and_history(self):
        legacy_path = Path(self.temp.name) / 'legacy.sqlite3'
        conn = memory.connect(legacy_path, create=True)
        memory.execute_script(conn, (memory.BASE / 'schema.sql').read_text())
        memory.execute_script(conn, (memory.BASE / 'graph_schema.sql').read_text())
        conn.execute("INSERT INTO metadata VALUES ('search_backend','scan')")
        conn.execute('PRAGMA user_version=2')
        memory.entry_put(conn, {'id': 'legacy', 'kind': 'fact', 'title': 'Old', 'summary': 'Preserved',
                                'knowledge_state': 'confirmed', 'sources': SOURCE})
        before = {table: [tuple(row) for row in conn.execute('SELECT * FROM ' + table)]
                  for table in ('entries', 'entry_history', 'graph_nodes')}
        with self.assertRaisesRegex(memory.MemoryError, 'explicit migrate'):
            memory.initialize(conn, legacy_path)
        graph.migrate(conn, legacy_path, memory)
        self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 3)
        self.assertEqual(before, {table: [tuple(row) for row in conn.execute('SELECT * FROM ' + table)] for table in before})
        self.assertEqual(research.revision(conn, 'entry:legacy'), 1)
        self.assertNotIn('revision', memory.entry_get(conn, 'legacy'))
        self.assertEqual(conn.execute('SELECT count(*) FROM research_events').fetchone()[0], 0)
        self.assertEqual(graph.migrate(conn, legacy_path, memory)['status'], 'existing')
        conn.close()

    def test_migration_of_actual_v2_copy_and_collision_rollback(self):
        original = Path(self.temp.name) / 'v2-original.sqlite3'
        source = memory.connect(original, create=True)
        memory.execute_script(source, (memory.BASE / 'schema.sql').read_text())
        memory.execute_script(source, (memory.BASE / 'graph_schema.sql').read_text())
        source.execute("INSERT INTO metadata VALUES ('search_backend','scan')")
        source.execute('PRAGMA user_version=2')
        memory.entry_put(source, {'id': 'v2', 'kind': 'note', 'title': 'Preserved', 'summary': 'Old evidence'})
        copied = Path(self.temp.name) / 'v2-copy.sqlite3'
        target = memory.connect(copied, create=True)
        source.backup(target)
        graph.migrate(target, copied, memory)
        self.assertEqual(source.execute('PRAGMA user_version').fetchone()[0], 2)
        self.assertIsNone(source.execute("SELECT 1 FROM sqlite_master WHERE name='entry_research'").fetchone())
        self.assertEqual(memory.entry_get(source, 'v2', history=True), memory.entry_get(target, 'v2', history=True))
        source.execute('CREATE TABLE research_revisions(valuable TEXT)')
        source.execute("INSERT INTO research_revisions VALUES ('must survive')")
        with self.assertRaises(sqlite3.OperationalError):
            graph.migrate(source, original, memory)
        self.assertEqual(source.execute('PRAGMA user_version').fetchone()[0], 2)
        self.assertIsNone(source.execute("SELECT 1 FROM sqlite_master WHERE name='entry_research'").fetchone())
        self.assertEqual(source.execute('SELECT valuable FROM research_revisions').fetchone()[0], 'must survive')
        source.close()
        target.close()

    def test_generic_write_savepoint_survives_caller_catching_journal_failure(self):
        self.put(self.card('task'))
        before = memory.entry_get(self.conn, 'task', history=True)
        self.conn.execute("CREATE TRIGGER fail_notification BEFORE INSERT ON research_events "
                          "BEGIN SELECT RAISE(ABORT, 'injected journal failure'); END")
        value = self.editable('task')
        value['title'] = 'Must roll back'
        with memory.transaction(self.conn):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'injected journal failure'):
                self.put(value)
        self.assertEqual(memory.entry_get(self.conn, 'task', history=True), before)

    def test_legacy_promotion_requires_cas_and_cannot_make_nested_tracks(self):
        plain = {'id': 'legacy', 'kind': 'task', 'title': 'Legacy', 'summary': 'Existing work'}
        self.put(plain)
        self.put(self.card('track', 'track'))
        self.put_edge(self.edge('legacy-track', 'legacy', 'track'))
        promotion = self.card('legacy', 'track')
        with self.assertRaisesRegex(memory.MemoryError, 'expected_revision'):
            self.put(promotion)
        promotion['expected_revision'] = research.revision(self.conn, 'entry:legacy')
        with self.assertRaisesRegex(memory.MemoryError, 'Invalid endpoints'):
            self.put(promotion)
        self.assertNotIn('research', memory.entry_get(self.conn, 'legacy'))

    def test_assessment_requires_actual_evidence_and_direction_matches(self):
        value = self.card('hypothesis', 'hypothesis', assessment='supported',
                          assessments=[{'assessment': 'untested', 'conditions': 'None observed', 'evidence_refs': []}])
        with self.assertRaisesRegex(memory.MemoryError, 'scoped evidence'):
            self.put(value)
        self.put(self.card('task'))
        value = self.editable('task')
        value['research']['expected_outcome'] = 'New expected effect'
        value['research']['direction_changes'].append({'changed_at': '2026-10-25', 'reason': 'New evidence',
                                                       'sources': SOURCE, 'expected_outcome': 'Different effect'})
        with self.assertRaisesRegex(memory.MemoryError, 'must match'):
            self.put(value)

    def test_shared_experiment_notifications_follow_only_named_task_paths(self):
        for identity, role in [('track', 'track'), ('task', 'task'), ('experiment', 'experiment'), ('hypothesis', 'hypothesis')]:
            self.put(self.card(identity, role))
        self.put_edge(self.edge('task-track', 'task', 'track'))
        self.put_edge(self.edge('experiment-task', 'experiment', 'task', 'informs'))
        self.put_edge(self.edge('experiment-hypothesis', 'experiment', 'hypothesis', 'tests'))
        bundle = {'request_id': 'shared-run', 'entries': [self.card('run', 'run')],
                  'edges': [self.edge('run-experiment', 'run', 'experiment', 'run_of')]}
        research.write(self.conn, bundle, memory)
        self.assertEqual(research.track_scopes(self.conn, 'entry:run'), {'track'})
        self.assertEqual(research.track_scopes(self.conn, 'entry:hypothesis'), {'track'})
        update = self.editable('run')
        update['title'] = 'New result title'
        self.put(update)
        kinds = [row[0] for row in self.conn.execute(
            "SELECT e.event_kind FROM research_events e JOIN research_event_tracks t ON t.seq=e.seq "
            "WHERE e.object_ref='entry:run' AND t.track_id='track' ORDER BY e.seq")]
        self.assertEqual(kinds, ['entry_created', 'entry_updated'])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM graph_edges WHERE kind='in_track' AND from_ref='entry:run'").fetchone()[0], 0)

    def test_public_revision_flag_supports_legacy_promotion_without_changing_default_shape(self):
        self.put({'id': 'legacy', 'kind': 'task', 'title': 'Legacy', 'summary': 'Existing task'})
        command = [sys.executable, '-m', 'richi', '--db', str(self.path), 'entry', 'get', 'legacy']
        environment = cli_environment(self.path.parent)
        plain = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=20)
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertNotIn('revision', json.loads(plain.stdout))
        result = subprocess.run(command + ['--revision'], capture_output=True, text=True, env=environment, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        revision = json.loads(result.stdout)['revision']
        promoted = self.card('legacy')
        promoted['expected_revision'] = revision
        self.put(promoted)
        self.assertEqual(memory.entry_get(self.conn, 'legacy')['revision'], revision + 1)


if __name__ == '__main__':
    unittest.main()
