"""Schema-2 free-form graph labels do not become native research assertions."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from richi import graph, memory, tracks


class LegacyResearchEdgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='richi-legacy-edge-labels-')
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'legacy.sqlite3'
        self.conn = memory.connect(self.path, create=True)
        self.addCleanup(self.conn.close)
        memory.execute_script(self.conn, (memory.BASE / 'schema.sql').read_text())
        memory.execute_script(self.conn, (memory.BASE / 'graph_schema.sql').read_text())
        self.conn.execute("INSERT INTO metadata(key,value) VALUES ('search_backend','scan')")
        self.conn.execute('PRAGMA user_version=2')
        memory.project_put(self.conn, {'id': 'alpha', 'name': 'Alpha'})
        memory.entry_put(self.conn, {'id': 'legacy', 'kind': 'task', 'title': 'Legacy task',
                         'summary': 'Existing records stay usable', 'knowledge_state': 'confirmed',
                         'sources': [{'reference': 'fixture:legacy'}]})
        # Simulate valid data written by schema-2 Richi, whose edge labels were
        # free-form. A canonical fixture avoids depending on an old executable.
        self.conn.execute("""INSERT INTO graph_edges
          (id,from_ref,to_ref,kind,description,sources,knowledge_state,verified_at,created_at,updated_at)
          VALUES (?,?,?,?,?,'[{"reference":"fixture:legacy-edge"}]','confirmed',NULL,?,?)""",
          ('legacy-membership', 'entry:legacy', 'project:alpha', 'in_track',
           'Legacy label with ordinary project endpoint', memory.now(), memory.now()))
        graph.put(self.conn, {'id': 'legacy-goal', 'kind': 'document', 'title': 'Old document',
                             'summary': 'Generic project documentation', 'knowledge_state': 'confirmed',
                             'sources': [{'reference': 'fixture:legacy-document'}]}, 'entity', memory)
        graph.put(self.conn, {'id': 'legacy-goal-link', 'from_ref': 'project:alpha',
                             'to_ref': 'entity:legacy-goal', 'kind': 'pursues_goal',
                             'description': 'Legacy free-form project relationship', 'knowledge_state': 'confirmed',
                             'sources': [{'reference': 'fixture:legacy-goal-link'}]}, 'edge', memory)
        graph.migrate(self.conn, self.path, memory)

    def test_migration_keeps_legacy_entry_editable_without_false_track_events(self):
        value = memory.entry_get(self.conn, 'legacy')
        self.assertNotIn('revision', value)
        value['expected_updated_at'] = value.pop('updated_at')
        value.pop('created_at')
        value['title'] = 'A normal edit after migration'
        result = memory.entry_put(self.conn, value)
        self.assertEqual(result['status'], 'updated')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM research_event_tracks').fetchone()[0], 0)

    def test_legacy_label_does_not_claim_the_task_has_a_native_track(self):
        args = SimpleNamespace(command='task', action='context', ref='entry:legacy',
                               max_chars=16000, limit=12, after=None,
                               check_sources=False, check_limit=None)
        result = tracks.context(self.conn, args, memory, self.path)
        self.assertEqual(result['track_count'], 0)
        self.assertEqual(result['tracks'], [])
        self.assertEqual(result['counts']['total'], 1)

    def test_legacy_project_goal_labels_do_not_invent_business_goals(self):
        args = SimpleNamespace(command='task', action='context', ref='entry:legacy',
                               max_chars=16000, limit=12, after=None,
                               check_sources=False, check_limit=None)
        result = tracks.context(self.conn, args, memory, self.path)
        self.assertEqual(result['track_count'], 0)
        self.assertEqual(result['goal_count'], 0)
        self.assertEqual(result['goals'], [])
        self.assertEqual(result['counts']['total'], 1)

    def test_existing_free_form_edge_is_still_editable_after_migration(self):
        value = graph.get(self.conn, 'edge', 'legacy-membership', memory, history=False)
        self.assertNotIn('revision', value)
        value['expected_updated_at'] = value.pop('updated_at')
        value.pop('created_at')
        value['description'] = 'Clarified legacy meaning'
        result = graph.put(self.conn, value, 'edge', memory)
        self.assertEqual(result['status'], 'updated')


if __name__ == '__main__':
    unittest.main()
