"""Strict scope survives bounded views; cards copy only returned source objects."""
import unittest

from richi import compact, memory, output, recall, retrieval_tools
import test_retrieval_tools as fixtures


class RetrievalFilterBudgetTests(unittest.TestCase):
    setUp = fixtures.RetrievalToolsTests.setUp
    entry = fixtures.RetrievalToolsTests.entry
    entity = fixtures.RetrievalToolsTests.entity
    edge = fixtures.RetrievalToolsTests.edge

    def test_filtered_discovery_excludes_unscoped_graph_candidates(self):
        self.entry('wanted', aliases=['routingneedle'], kind='decision')
        self.entry('outside', aliases=['routingneedle'], project_ids=['beta'], kind='decision')
        self.entity('neighbor')
        self.edge('linked', 'entity:neighbor', 'entry:wanted')
        args = memory.parser().parse_args(['discover', 'routingneedle', '--where',
                                          "project = 'alpha' AND kind = 'decision'"])
        result = retrieval_tools.run(self.conn, args, memory)
        self.assertEqual([r['ref'] for r in result['results']], ['entry:wanted'])
        self.assertTrue(result['selection']['filter_applied'])
        self.assertFalse(result['no_match'])

    def test_filter_marker_survives_oversized_metadata_fallbacks(self):
        projects = []
        for index in range(8):
            identity = str(index) + 'p' * 199
            memory.project_put(self.conn, {'id': identity, 'name': identity})
            projects.append(identity)
        self.entry('a' * 200, aliases=['routingneedle'], project_ids=projects,
                   sources=[{'reference': 'https://example.test/' + 'x' * 50000}])
        for small in (False, True):
            for command in ('discover', 'recall'):
                for explain in (False, True) if command == 'recall' else (False,):
                    arguments = [command, 'routingneedle', '--where', "kind = 'note'", '--max-chars', '2000']
                    if explain:
                        arguments += ['--explain']
                    args = memory.parser().parse_args(arguments)
                    with self.subTest(compact=small, command=command, explain=explain), output.json_mode(compact=small):
                        result = (retrieval_tools.run(self.conn, args, memory) if command == 'discover'
                                  else recall.recall(self.conn, args, memory))
                        self.assertTrue(result['selection']['filter_applied'])
                        self.assertFalse(result['no_match'])
                        self.assertTrue(result['truncated'])
                        self.assertLessEqual(len(output.rendered(result)), 2000)
                        self.assertEqual(result['budget']['output_chars'], len(output.rendered(result)))

    def test_card_does_not_copy_omitted_sources_and_returned_sources_are_independent(self):
        class NotCopied:
            def __deepcopy__(self, memo):
                raise AssertionError('An omitted source was unnecessarily copied')
        sources = [{'reference': 'first', 'nested': {'value': 1}}, {'reference': 'second'}, NotCopied()]
        result = compact.card({'id': 'example', 'summary': 'Whole summary.', 'sources': sources}, 'entry:example')
        self.assertEqual(result['source_count'], 3)
        self.assertEqual(result['sources_omitted'], 1)
        result['sources'][0]['nested']['value'] = 2
        self.assertEqual(sources[0]['nested']['value'], 1)


if __name__ == '__main__':
    unittest.main()
