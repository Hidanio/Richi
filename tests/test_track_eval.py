"""Frozen corpus invariants; runtime behavior is covered in test_tracks.py."""
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TrackEvaluationFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((ROOT / 'tests/fixtures/tracks.json').read_text())
        cls.entries = {item['id']: item for item in cls.data['entries']}
        cls.edges = cls.data['edges']

    def test_frozen_expectations_cover_all_acceptance_ids(self):
        self.assertEqual(['A%02d' % number for number in range(1, 17)],
                         [case['id'] for case in self.data['acceptance']])
        self.assertEqual([40, 1000, 10000], self.data['scales'])

    def test_same_facts_are_available_to_old_summary_reader(self):
        for item in self.entries.values():
            if item.get('research'):
                self.assertEqual(item['research'], json.loads(item['summary'])['research'])

    def test_standalone_and_shared_membership_are_explicit(self):
        task_entries = [entry for entry in self.entries.values() if entry.get('research', {}).get('role') == 'task']
        self.assertEqual(20, len(task_entries))
        self.assertTrue(all(entry['work_state'] == 'done' for entry in task_entries))
        standalone = 'entry:task:20'
        self.assertFalse(any(edge['from_ref'] == standalone and edge['kind'] == 'in_track' for edge in self.edges))
        tracks = {edge['to_ref'] for edge in self.edges if edge['from_ref'] == 'entry:task:01' and edge['kind'] == 'in_track'}
        self.assertEqual(set(self.data['expected']['track_refs']), tracks)
        self.assertEqual(['ledger'], self.entries['task:02']['project_ids'])

    def test_zero_and_multiple_independent_goal_sources(self):
        goal_edges = [edge for edge in self.edges if edge['kind'] == 'pursues_goal']
        self.assertFalse(any(edge['from_ref'] == 'entry:track:reliability' for edge in goal_edges))
        self.assertEqual(set(self.data['expected']['product_goal_refs']), {edge['to_ref'] for edge in goal_edges})

    def test_canonical_experiment_preserves_distinct_outcomes_and_conditions(self):
        self.assertEqual(1, sum(entry.get('research', {}).get('role') == 'experiment' for entry in self.entries.values()))
        runs = [entry for entry in self.entries.values() if entry.get('research', {}).get('role') == 'run']
        self.assertEqual(5, len(runs))
        self.assertEqual({'supporting', 'negative', 'not_completed'}, {item['research']['outcome'] for item in runs})
        for item in runs:
            links = [edge for edge in self.edges if edge['from_ref'] == 'entry:' + item['id'] and edge['kind'] == 'run_of']
            self.assertEqual(['entry:experiment:shared'], [edge['to_ref'] for edge in links])
        self.assertEqual('insufficient', self.entries['hypothesis:cache']['research']['assessment'])
        self.assertEqual('conflicting', self.entries['hypothesis:conflict']['research']['assessment'])
        self.assertEqual('superseded', self.entries['decision:old-rejection']['knowledge_state'])

    def test_generator_never_removes_evidence_or_copies_run(self):
        spec = importlib.util.spec_from_file_location('evaluate_tracks', ROOT / 'evals/evaluate_tracks.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for size in self.data['scales']:
            old, new = (module.corpus(self.data, size, native) for native in (False, True))
            self.assertEqual(size, len(new['entries']))
            self.assertEqual(len({item['id'] for item in new['entries']}), size)
            for old_item, new_item in zip(old['entries'], new['entries']):
                expected = dict(new_item); expected.pop('research', None)
                self.assertEqual(expected, old_item)


if __name__ == '__main__':
    unittest.main()
