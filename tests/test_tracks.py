"""Native context and event pagination acceptance on synthetic stores only."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from richi import graph, memory, output, research, tracks


class TrackContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='richi-tracks-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'fixture.sqlite3'
        self.conn = memory.connect(self.path, create=True)
        self.addCleanup(self.conn.close)
        memory.initialize(self.conn, self.path)
        fixture = json.loads((Path(__file__).parent/'fixtures'/'tracks.json').read_text())
        with memory.transaction(self.conn):
            for row in fixture['projects']:
                memory.project_put(self.conn,row)
            for row in fixture['entities']:
                graph.put(self.conn,row,'entity',memory)
            research.write(self.conn,dict(request_id='fixture',entries=fixture['entries'],edges=fixture['edges']),memory)

    def args(self, command='task', action='context', **kw):
        value=dict(command=command,action=action,ref='entry:task:01',max_chars=100000,limit=50,after=None,cursor=None,event_kind=None,check_sources=False,check_limit=None)
        value.update(kw)
        return SimpleNamespace(**value)

    def read(self, **kw):
        args=self.args(**kw)
        with memory.transaction(self.conn,write=False):
            r=tracks.run(self.conn,args,memory,self.path)
        self.assertEqual(len(output.rendered(r)),r['budget']['output_chars'])
        self.assertLessEqual(len(output.rendered(r)),args.max_chars)
        return r

    def update(self,identity,**changes):
        v=memory.entry_get(self.conn,identity)
        v['expected_revision']=v.pop('revision')
        v.pop('created_at');v.pop('updated_at');v.update(changes)
        with memory.transaction(self.conn):
            return memory.entry_put(self.conn,v)

    def test_standalone_does_not_expand_project(self):
        r=self.read(ref='entry:task:20')
        self.assertEqual(r['track_count'],0)
        self.assertEqual(r['records'],[])
        self.assertEqual(r['counts']['total'],1)
        self.assertTrue(r['task']['research']['motivation']['sources'])
        self.assertFalse(r['scope']['projects_expanded'])

    def test_two_tracks_two_goals_deduplicate_and_keep_negative_evidence(self):
        r=self.read()
        self.assertEqual(r['track_count'],2)
        self.assertEqual(r['goal_count'],2)
        refs=[c['ref'] for c in [r['task']]+r['tracks']+r['goals']+r['records']]
        self.assertEqual(len(refs),len(set(refs)))
        self.assertIn('entry:decision:old-rejection',refs)
        self.assertIn('entry:run:negative',refs)
        self.assertNotIn('entry:note:outside',refs)
        self.assertEqual(r['counts']['outcomes']['not_completed'],1)
        self.assertEqual(r['counts']['assessments']['conflicting'],1)
        self.assertEqual(r['evidence_comparability'],'not_inferred')
        self.assertEqual(r['goal_effect'],'unknown')

    def test_technical_track_without_goal_and_completed_tasks_unknown_effect(self):
        r=self.read(command='track',action='summary',ref='entry:track:reliability')
        self.assertEqual(r['goal_count'],0)
        self.assertGreater(r['counts']['work_states']['done'],0)
        self.assertEqual(r['goal_effect'],'unknown')
        self.assertNotIn('entry:task:20',[c['ref'] for c in r['records']])

    def test_small_budgets_exact_serialization_and_progress(self):
        for compact in (False,True):
            with output.json_mode(compact=compact):
                after=None; cursors=set(); references=set()
                for _ in range(30):
                    r=self.read(max_chars=2000,after=after)
                    self.assertEqual(r['counts']['outcomes']['negative'],2)
                    self.assertTrue(r['truncated'])
                    references.update(card['ref'] for card in r['records'])
                    if not r['has_more']:
                        break
                    after=r['next_after']
                    self.assertNotIn(after,cursors)
                    cursors.add(after)
                else:
                    self.fail('budget pagination did not progress')
                self.assertIn('entry:run:negative',references)
                self.assertIn('entry:hypothesis:conflict',references)

    def test_small_budget_pages_preserve_every_evidence_identity(self):
        full = self.read()
        expected = {card['ref'] for card in full['records'] + full['tracks'] + full['goals']}
        for compact in (False, True):
            with output.json_mode(compact=compact):
                after, seen, headers = None, [], set()
                for _ in range(40):
                    page = self.read(max_chars=2000, after=after)
                    headers.update(card['ref'] for card in page['tracks'] + page['goals'])
                    for card in page['records']:
                        seen.append(card['ref'])
                        self.assertIn('read_more', card)
                    if not page['has_more']:
                        break
                    self.assertNotEqual(page['next_after'], after)
                    after = page['next_after']
                else:
                    self.fail('Small-budget evidence enumeration did not finish')
                self.assertEqual(set(seen) | headers, expected)
                self.assertEqual(len(seen), len(set(seen)))

    def test_more_than_eight_tracks_and_goals_have_complete_continuations(self):
        entries, edges = [], []
        for index in range(10):
            track = 'wide-track:' + str(index)
            goal = 'wide-goal:' + str(index)
            graph.put(self.conn, dict(id=goal, kind='document', title='Goal', summary='Independent goal'),
                      'entity', memory)
            entries.append(dict(id=track, kind='task', title='Track', summary='Explicit scope',
                                research=dict(role='track', motivation=dict(text='Why', sources=[dict(reference='test:why')]),
                                              expected_outcome='Outcome')))
            edges.extend([
                dict(id='wide-membership:' + str(index), from_ref='entry:task:20', to_ref='entry:' + track,
                     kind='in_track', description='Task contribution'),
                dict(id='wide-goal-link:' + str(index), from_ref='entry:' + track, to_ref='entity:' + goal,
                     kind='pursues_goal', description='Goal contribution')])
            if index:
                edges.append(dict(id='wide-extra-goal:' + str(index), from_ref='entry:wide-track:0',
                                  to_ref='entity:' + goal, kind='pursues_goal', description='Additional goal'))
        research.write(self.conn, dict(request_id='wide-fixture', entries=entries, edges=edges), memory)
        for command, action, ref in (('task', 'context', 'entry:task:20'),
                                     ('track', 'summary', 'entry:wide-track:0')):
            full = self.read(command=command, action=action, ref=ref)
            expected = {card['ref'] for card in full['tracks'] + full['goals'] + full['records']}
            self.assertEqual(full['goal_count'], 10)
            if command == 'task':
                self.assertEqual(full['track_count'], 10)
            for compact in (False, True):
                with output.json_mode(compact=compact):
                    after, visible, records = None, set(), []
                    for _ in range(80):
                        page = self.read(command=command, action=action, ref=ref, max_chars=2000,
                                         limit=3, after=after)
                        cards = page['tracks'] + page['goals'] + page['records']
                        self.assertEqual(len(cards), len({card['ref'] for card in cards}))
                        visible.update(card['ref'] for card in cards)
                        records.extend(card['ref'] for card in page['records'])
                        if not page['has_more']:
                            break
                        self.assertNotEqual(after, page['next_after'])
                        after = page['next_after']
                    else:
                        self.fail('Wide context pagination did not finish')
                    self.assertEqual(visible, expected)
                    self.assertEqual(len(records), len(set(records)))

    def test_oversized_unicode_and_arrays_not_hydrated(self):
        value='\\"\nЖ😀'*200000
        self.update('decision:current',summary=value,sources=[{'reference':value}])
        r=self.read(max_chars=2000)
        self.assertTrue(r['truncated'])
        self.assertEqual(r['counts']['roles']['decision'],2)

    def test_context_cursor_rejects_cross_scope_and_store(self):
        r=self.read(limit=1)
        with self.assertRaises(memory.MemoryError):
            self.read(ref='entry:task:02',after=r['next_after'])
        old=self.conn.execute("SELECT value FROM metadata WHERE key='research_store_uuid'").fetchone()[0]
        self.conn.execute("UPDATE metadata SET value='other-store' WHERE key='research_store_uuid'")
        with self.assertRaises(memory.MemoryError):
            self.read(after=r['next_after'])
        self.conn.execute("UPDATE metadata SET value=? WHERE key='research_store_uuid'",(old,))

    def feed(self,**kw):
        return self.read(command='track',action='changes',ref='entry:track:reliability',**kw)

    def drain(self,cursor=None,limit=3):
        events=[]
        for _ in range(100):
            r=self.feed(cursor=cursor,limit=limit,max_chars=4000)
            events.extend(r['events']);cursor=r['cursor']
            if not r['has_more']:
                return events,cursor
        self.fail('changes pagination did not finish')

    def test_changes_fixed_upper_ties_detach_and_notifications(self):
        first=self.feed(limit=2)
        upper=first['snapshot_upper']
        old=graph.get(self.conn,'edge','task:03@entry:track:reliability',memory,history=False)
        args=self.args('track','detach',ref='entry:track:reliability',member_ref='entry:task:03',expected_revision=old['revision'])
        with patch.object(memory,'now',return_value='2026-10-25T00:00:00Z'):
            with memory.transaction(self.conn):
                tracks.membership(self.conn,args,memory)
            self.update('run:negative',title='Renamed after snapshot')
        rest,cursor=self.drain(first['cursor'])
        seq=[e['seq'] for e in first['events']+rest]
        self.assertEqual(seq,sorted(set(seq)))
        self.assertTrue(all(s<=upper for s in seq))
        new,cursor=self.drain(cursor)
        self.assertIn('detached',[e['event_kind'] for e in new])
        self.assertIn('entry:run:negative',[e['object_ref'] for e in new])
        self.assertTrue(all('payload' not in e for e in new))
        self.assertEqual(len({e['recorded_at'] for e in new}),1)
        self.assertEqual(self.feed(cursor=cursor)['events'],[])

    def test_changes_scope_filter_invalid_and_state_transition(self):
        r=self.feed(event_kind='entry_updated')
        cursor=r['cursor']
        self.update('run:negative',knowledge_state='superseded')
        n=self.feed(event_kind='entry_updated',cursor=cursor)
        self.assertIn('entry:run:negative',[e['object_ref'] for e in n['events']])
        with self.assertRaises(memory.MemoryError):
            self.feed(cursor=cursor,event_kind='detached')
        for invalid in ('invalid','e30','!!!'):
            with self.assertRaises(memory.MemoryError):
                self.feed(cursor=invalid)

    def test_escaped_maximum_ids_never_return_a_stuck_changes_cursor(self):
        track_id = 't:' + ('\\"' * 99)
        member_id = 'm:' + ('\\"' * 99)
        def card(identity, role):
            return dict(id=identity, kind='task', title='Synthetic escaped ID', summary='Evidence',
                        research=dict(role=role, motivation=dict(text='Why', sources=[dict(reference='test:reason')]),
                                      expected_outcome='Outcome'))
        research.write(self.conn, dict(request_id='escaped-fixture',
            entries=[card(track_id, 'track'), card(member_id, 'task')],
            edges=[dict(id='escaped-membership', from_ref='entry:' + member_id,
                        to_ref='entry:' + track_id, kind='in_track', description='Explicit contribution')]), memory)
        self.update(member_id, title='Changed after membership')
        for compact in (False, True):
            with output.json_mode(compact=compact):
                kwargs = dict(command='track', action='changes', ref='entry:' + track_id,
                              event_kind='entry_updated', limit=1, max_chars=2000)
                try:
                    page = self.read(**kwargs)
                except memory.MemoryError as exc:
                    self.assertIn('pointer exceeds budget', str(exc))
                    kwargs['max_chars'] = 4000
                    page = self.read(**kwargs)
                self.assertEqual(page['count'], 1)
                self.assertEqual([event['object_ref'] for event in page['events']], ['entry:' + member_id])
                self.assertFalse(page['has_more'])
                again = self.read(cursor=page['cursor'], **kwargs)
                self.assertEqual(again['events'], [])
                self.assertFalse(again['has_more'])

    def test_attach_replay_detach_no_project_change(self):
        before=memory.entry_get(self.conn,'task:20')['project_ids']
        a=self.args('track','attach',ref='entry:track:reliability',member_ref='entry:task:20',expected_revision=0,contribution='Isolated task contributes now',source=['test://explicit'])
        with memory.transaction(self.conn):
            result=tracks.membership(self.conn,a,memory)
        upper=self.conn.execute('SELECT max(seq) FROM research_events').fetchone()[0]
        with memory.transaction(self.conn):
            repeat=tracks.membership(self.conn,a,memory)
        self.assertEqual(repeat['status'],'unchanged')
        self.assertEqual(upper,self.conn.execute('SELECT max(seq) FROM research_events').fetchone()[0])
        self.assertEqual(before,memory.entry_get(self.conn,'task:20')['project_ids'])
        a.action='detach';a.expected_revision=result['revision']
        with memory.transaction(self.conn):
            tracks.membership(self.conn,a,memory)
        self.assertEqual(self.read(ref='entry:task:20')['track_count'],0)
        with memory.transaction(self.conn):
            self.assertEqual(tracks.membership(self.conn,a,memory)['status'],'unchanged')

    def test_membership_retries_keep_original_revision_precondition(self):
        args = self.args('track', 'attach', ref='entry:track:reliability', member_ref='entry:task:20',
                         expected_revision=0, contribution='Explicit contribution', source=['test://membership'])
        attached = tracks.membership(self.conn, args, memory)
        self.assertEqual(tracks.membership(self.conn, args, memory)['status'], 'unchanged')
        args.action = 'detach'
        args.expected_revision = attached['revision']
        tracks.membership(self.conn, args, memory)
        self.assertEqual(tracks.membership(self.conn, args, memory)['status'], 'unchanged')

    def test_optional_source_checks_are_explicit_and_read_only(self):
        before=memory.entry_get(self.conn,'task:01')
        result=self.read(check_sources=True)
        self.assertTrue(result['source_checks']['requested'])
        self.assertEqual(result['source_checks']['checked'],0)
        self.assertEqual(before,memory.entry_get(self.conn,'task:01'))
        with self.assertRaises(memory.MemoryError):
            self.read(check_limit=2)

    def test_optional_source_checks_obey_small_budgets_in_both_json_modes(self):
        for compact in (False, True):
            with output.json_mode(compact=compact):
                for maximum in (2000, 2100, 2200, 2500, 3000, 4000):
                    with self.subTest(compact=compact, maximum=maximum):
                        result = self.read(check_sources=True, max_chars=maximum)
                        self.assertIn('source_checks', result)
                        self.assertTrue(result['truncated'])

    def test_default_compact_context_contains_mandatory_evidence_details(self):
        with output.json_mode(compact=True):
            result=self.read(max_chars=tracks.DEFAULT_CHARS)
        detailed={c['ref'] for c in result['records'] if 'research' in c}
        for ref in ('entry:run:negative','entry:run:infra','entry:hypothesis:conflict','entry:decision:old-rejection'):
            self.assertIn(ref,detailed)

    def test_large_atomic_write_receipt_is_bounded_and_replayable(self):
        values=[dict(id='bulk:%03d:'%i+'Ж\\"'*60,kind='note',title='Bulk',summary='Synthetic') for i in range(100)]
        path=self.path.parent/'bundle.json'
        path.write_text(json.dumps(dict(request_id='bounded-bulk',entries=values)),encoding='utf-8')
        args=self.args('research','put',file=str(path),max_chars=2000)
        with memory.transaction(self.conn):
            result=tracks.run(self.conn,args,memory,self.path)
        self.assertLessEqual(len(output.rendered(result)),2000)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['entries'])+result['omitted']['entries'],100)
        with memory.transaction(self.conn):
            again=tracks.run(self.conn,args,memory,self.path)
        self.assertTrue(again['replayed'])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entries WHERE id LIKE 'bulk:%'").fetchone()[0],100)

    def test_read_does_not_change_verified_or_journal(self):
        before=memory.entry_get(self.conn,'run:negative')
        seq=self.conn.execute('SELECT max(seq) FROM research_events').fetchone()[0]
        self.read(); self.feed()
        self.assertEqual(before,memory.entry_get(self.conn,'run:negative'))
        self.assertEqual(seq,self.conn.execute('SELECT max(seq) FROM research_events').fetchone()[0])


if __name__=='__main__':
    unittest.main()
