"""Synthetic identity mappings only; never read the current user's preferences."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/codex_recency"))
from sidebar_aliases import normalize_snapshot, PREFIX
import watcher

NATIVE = "11111111-1111-4111-8111-111111111111"
ALIAS = "client-new-thread:22222222-2222-4222-8222-222222222222"
REVERSE = "thread-client-id-v1:" + quote("local:" + NATIVE, safe="")


class Aliases(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "preferences.json"
        self.atoms = {"client-thread-bindings-v1": {ALIAS:NATIVE}, REVERSE:ALIAS}
        self.write()
        self.snapshot = {"sections":[{"sectionId":"view-a","name":"Example","itemKeys":[PREFIX+"other",PREFIX+ALIAS]}]}

    def write(self):
        self.path.write_text(json.dumps({"electron-persisted-atom-state":self.atoms}),encoding="utf-8")

    def test_exact_bidirectional_binding_preserves_input_and_order(self):
        original=copy.deepcopy(self.snapshot)
        result=normalize_snapshot(self.snapshot,self.path)
        self.assertEqual(result['sections'][0]['itemKeys'],[PREFIX+'other',PREFIX+NATIVE])
        self.assertEqual(self.snapshot,original)

    def test_disabled_does_not_open_any_file(self):
        self.assertIs(normalize_snapshot(self.snapshot),self.snapshot)
        plain={'sections':[{'itemKeys':[PREFIX+NATIVE]}]}
        self.assertIs(normalize_snapshot(plain,self.path/'does-not-exist'),plain)

    def test_missing_reverse_is_not_guessed(self):
        del self.atoms[REVERSE];self.write()
        self.assertEqual(normalize_snapshot(self.snapshot,self.path),self.snapshot)

    def test_disagreeing_reverse_stops_before_native_write(self):
        self.atoms[REVERSE]='client-new-thread:another';self.write()
        with self.assertRaisesRegex(ValueError,'mismatch'):
            normalize_snapshot(self.snapshot,self.path)

    def test_invalid_identity_stops(self):
        self.atoms['client-thread-bindings-v1'][ALIAS]='not-a-thread-id';self.write()
        with self.assertRaisesRegex(ValueError,'invalid'):
            normalize_snapshot(self.snapshot,self.path)

    def test_duplicate_canonical_identity_stops(self):
        self.snapshot['sections'][0]['itemKeys'].append(PREFIX+NATIVE)
        with self.assertRaisesRegex(ValueError,'duplicate'):
            normalize_snapshot(self.snapshot,self.path)

    def test_remote_alias_is_not_rewritten(self):
        self.snapshot['sections'][0]['itemKeys']=['codex:thread:remote:'+ALIAS]
        self.assertIs(normalize_snapshot(self.snapshot,self.path),self.snapshot)

    def test_builtin_pending_cache_does_not_change_custom_group_identity(self):
        builtin={'sectionId':'chats','itemKeys':[PREFIX+NATIVE]}
        self.snapshot['sections'].append(builtin)
        result=normalize_snapshot(self.snapshot,self.path)
        self.assertEqual(result['sections'][0]['itemKeys'],[PREFIX+'other',PREFIX+NATIVE])
        self.assertEqual(result['sections'][1],builtin)

    def test_native_move_still_checks_members_and_position(self):
        outer=self
        class App:
            def __init__(self):self.keys=[PREFIX+'other',PREFIX+ALIAS];self.writes=[]
            def tool(self,name,args):
                if name=='move_thread_to_sidebar_section':
                    self.writes.append(args)
                    outer.assertEqual(args['threadId'],NATIVE)
                    self.keys=[PREFIX+ALIAS,PREFIX+'other']
                    return {}
                return normalize_snapshot({'sections':[{'sectionId':'view-a','name':'Example','itemKeys':self.keys}]},outer.path)
        app=App()
        groups={'native-a':{'native_section_id':'native-a','sidebar_section_id':'view-a','name':'Example'}}
        receipt=watcher.move(app,{'id':NATIVE,'recencyAt':10,'section':{'id':'native-a'}},groups)
        self.assertEqual(receipt['result'],'moved')
        self.assertTrue(receipt['active_member_set_unchanged'])
        self.assertEqual(len(app.writes),1)


if __name__=='__main__':unittest.main()
