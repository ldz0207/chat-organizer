import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from plan_preview import PlanError, digest, load, pairs_unique, preview


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.inv = load(ROOT / 'examples' / 'inventory.json')
        self.prop = load(ROOT / 'examples' / 'proposal.json')

    def refresh(self):
        self.prop['inventory_sha256'] = digest(self.inv)

    def rejected(self):
        with self.assertRaises(PlanError):
            preview(self.inv, self.prop)

    def test_example_counts_and_no_execution(self):
        r = preview(self.inv, self.prop)
        self.assertEqual(r['execution'], 'NOT_EXECUTED')
        self.assertEqual(r['approval'], 'USER_CONFIRMATION_REQUIRED')
        self.assertEqual(r['plan']['counts']['moves'], 1)
        self.assertEqual(r['plan']['counts']['groups_total'], 2)
        self.assertEqual(r['plan']['counts']['tasks_total'], 4)
        self.assertEqual(r['plan']['pending'], ['task-4'])
        self.assertIn('task-3', r['plan']['unchanged'])

    def test_no_input_mutation(self):
        before = copy.deepcopy((self.inv, self.prop))
        preview(self.inv, self.prop)
        self.assertEqual((self.inv, self.prop), before)

    def test_deterministic_digest(self):
        self.assertEqual(preview(self.inv, self.prop), preview(self.inv, self.prop))

    def test_stale_inventory(self):
        self.inv['tasks'][1]['revision'] = 'later'
        self.rejected()

    def test_context_device_drift(self):
        self.inv['context']['device'] = 'another device'
        self.rejected()

    def test_running_and_active_and_protected(self):
        for field, value in [('status', 'running'), ('status', 'unknown'), ('status', 'archived'), ('status', 'deleted'), ('active', True), ('protected', True)]:
            with self.subTest(field=field, value=value):
                self.setUp()
                self.inv['tasks'][1][field] = value
                self.refresh()
                self.rejected()

    def test_missing_active_rejected(self):
        del self.inv['tasks'][1]['active']
        self.refresh()
        self.rejected()

    def test_nonboolean_active_rejected(self):
        self.inv['tasks'][1]['active'] = 'false'
        self.refresh()
        self.rejected()

    def test_existing_groups_preserved(self):
        self.prop['groups'][0]['name'] = '自动改名'
        self.rejected()

    def test_existing_group_deletion_rejected(self):
        self.prop['groups'] = self.prop['groups'][1:]
        self.rejected()

    def test_duplicate_group_names(self):
        self.prop['groups'][1]['name'] = '研究'
        self.rejected()

    def test_unicode_normalization_collision(self):
        self.inv['groups'][0]['name'] = 'café'
        self.prop['groups'][0]['name'] = 'café'
        self.prop['groups'][1]['name'] = 'CAFE\u0301'
        self.refresh()
        self.rejected()

    def test_existing_group_order_preserved(self):
        self.inv['groups'].append({'id':'existing:other','name':'其他','description':'已有分组'})
        self.prop['groups'].insert(0, copy.deepcopy(self.inv['groups'][1]))
        self.refresh()
        self.rejected()

    def test_unknown_target(self):
        self.prop['assignments'][0]['target_group_id'] = 'missing'
        self.rejected()

    def test_duplicate_assignment(self):
        self.prop['assignments'] *= 2
        self.rejected()

    def test_pending_assignment_collision(self):
        self.prop['pending'].append('task-2')
        self.rejected()

    def test_title_change_not_supported(self):
        self.prop['assignments'][0]['title'] = 'rewrite'
        self.rejected()

    def test_untrusted_approval_field_not_accepted(self):
        self.prop['approved'] = True
        self.rejected()

    def test_unknown_schema(self):
        self.prop['schema_version'] = 2
        self.rejected()

    def test_duplicate_json_keys(self):
        with self.assertRaises(PlanError):
            json.loads('{"x":1,"x":2}', object_pairs_hook=pairs_unique)

    def test_new_group_id_is_local_placeholder(self):
        self.prop['groups'][1]['id'] = 'fabricated-native-id'
        self.rejected()

    def test_idempotent_target_noop(self):
        self.inv['groups'].append(copy.deepcopy(self.prop['groups'][1]))
        self.inv['tasks'][1]['group_id'] = 'new:events'
        self.refresh()
        result = preview(self.inv, self.prop)['plan']
        self.assertEqual(result['moves'], [])
        self.assertEqual(result['new_groups'], [])
        self.assertEqual(result['noops'], ['task-2'])

    def test_manual_title_preserved(self):
        self.prop['assignments'].append({'task_id':'task-1','target_group_id':'new:events','reason':'explicit regrouping proposal'})
        result = preview(self.inv, self.prop)['plan']
        moved = [x for x in result['moves'] if x['task_id'] == 'task-1'][0]
        self.assertEqual(moved['title'], '用户手动标题：数值方法笔记')

    def test_native_route_is_not_authenticated(self):
        self.inv['context']['route'] = 'supported-native'
        self.refresh()
        result = preview(self.inv, self.prop)
        self.assertEqual(result['execution'], 'NOT_EXECUTED')
        self.assertIn('unverified', result['notice'])

    def test_cli_and_fingerprint(self):
        script = str(ROOT / 'scripts' / 'plan_preview.py')
        inv = str(ROOT / 'examples' / 'inventory.json')
        prop = str(ROOT / 'examples' / 'proposal.json')
        result = subprocess.run([sys.executable, script, inv, prop], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['execution'], 'NOT_EXECUTED')
        fp = subprocess.run([sys.executable, script, inv, '--fingerprint'], capture_output=True, text=True, check=True)
        self.assertEqual(fp.stdout.strip(), digest(self.inv))
        invalid = subprocess.run([sys.executable, script, inv], capture_output=True, text=True)
        self.assertEqual(invalid.returncode, 2)


if __name__ == '__main__':
    unittest.main()
