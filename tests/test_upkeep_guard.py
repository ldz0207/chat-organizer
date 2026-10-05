import copy
from pathlib import Path
import subprocess
import sys
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from plan_preview import PlanError, load
from upkeep_guard import evaluate, render_html


class UpkeepTests(unittest.TestCase):
    def setUp(self):
        self.data = load(ROOT / 'examples' / 'upkeep.json')
        self.data['policy']['enabled'] = True
        self.data['policy']['approval_ref'] = 'synthetic-test-approval-not-real'

    def decision(self):
        return evaluate(self.data)['decisions'][0]['decision']

    def test_default_disabled(self):
        self.data = load(ROOT / 'examples' / 'upkeep.json')
        self.assertEqual(self.decision(), 'pending_approval')

    def test_declared_coverage_never_executes(self):
        r = evaluate(self.data)
        self.assertEqual(self.decision(), 'covered_by_declared_policy')
        self.assertEqual(r['execution'], 'NOT_EXECUTED')
        self.assertIn('not authenticated', r['notice'])

    def test_no_mutation(self):
        before = copy.deepcopy(self.data)
        evaluate(self.data)
        self.assertEqual(self.data, before)

    def test_missing_approval(self):
        self.data['policy']['approval_ref'] = ''
        self.assertEqual(self.decision(), 'pending_approval')

    def test_context_and_taxonomy_drift(self):
        for key in ['device', 'account_scope', 'version']:
            self.setUp(); self.data['context'][key] = 'changed'
            self.assertEqual(self.decision(), 'pending_approval')
        self.setUp(); self.data['taxonomy_sha256'] = 'changed'
        self.assertEqual(self.decision(), 'pending_approval')

    def test_workspace_category_and_scope_evidence(self):
        for field, value in [('workspace_id', 'other-workspace'), ('task_category', 'old-project'), ('scope_verified', False)]:
            self.setUp(); self.data['candidates'][0][field] = value
            self.assertEqual(self.decision(), 'pending_approval')

    def test_expired_policy(self):
        self.data['policy']['expires_at'] = 2000
        self.assertEqual(self.decision(), 'pending_approval')

    def test_unsupported_event_and_conditional_write(self):
        self.data['trigger'] = 'event'
        self.data['policy']['triggers'] = ['event']
        self.assertEqual(self.decision(), 'pending_approval')
        self.setUp(); self.data['capabilities']['conditional_write'] = False
        self.assertEqual(self.decision(), 'pending_approval')

    def test_active_and_unknown_status(self):
        self.data['candidates'][0]['active'] = True
        self.assertEqual(self.decision(), 'deferred')
        self.setUp(); self.data['candidates'][0]['status'] = 'unknown'
        self.assertEqual(self.decision(), 'deferred')

    def test_protected_and_manual_lock(self):
        for k in ['protected', 'manual_lock']:
            self.setUp(); self.data['candidates'][0][k] = True
            self.assertEqual(self.decision(), 'skipped')

    def test_revision_conflict(self):
        self.data['candidates'][0]['current_revision'] = 'r2'
        self.assertEqual(self.decision(), 'deferred')

    def test_manual_and_unknown_title_ownership(self):
        for owner in ['manual', 'unknown']:
            self.setUp(); self.data['candidates'][0]['title_owner'] = owner
            self.assertEqual(self.decision(), 'skipped')
        self.setUp(); self.data['candidates'][0]['ownership_verified'] = False
        self.assertEqual(self.decision(), 'skipped')

    def test_external_title_edit_is_protected(self):
        c = self.data['candidates'][0]
        c['title_owner'], c['last_policy_title'] = 'policy', 'Original automatic title'
        c['before'] = 'My manual replacement'
        self.assertEqual(self.decision(), 'skipped')

    def test_verified_policy_owned_title(self):
        c = self.data['candidates'][0]
        c['title_owner'], c['last_policy_title'] = 'policy', c['before']
        self.assertEqual(self.decision(), 'covered_by_declared_policy')

    def test_style_length_and_control_chars(self):
        self.data['candidates'][0]['target'] = 'X' * 21
        self.assertEqual(self.decision(), 'pending_approval')
        self.setUp(); self.data['candidates'][0]['target'] = 'Line\nBreak'
        self.assertEqual(self.decision(), 'pending_approval')
        self.setUp(); self.data['candidates'][0]['style_matches'] = False
        self.assertEqual(self.decision(), 'pending_approval')

    def test_group_scope(self):
        c = self.data['candidates'][0]
        c['action'], c['before'], c['target'] = 'move', None, 'existing:research'
        self.assertEqual(self.decision(), 'covered_by_declared_policy')
        c['target'] = 'new:surprise'
        self.assertEqual(self.decision(), 'pending_approval')
        c['target'], c['before'] = 'existing:research', 'existing:manual-choice'
        self.assertEqual(self.decision(), 'pending_approval')

    def test_ambiguous_grouping(self):
        c = self.data['candidates'][0]
        c['action'], c['before'], c['target'], c['classification_confident'] = 'move', None, 'existing:research', False
        self.assertEqual(self.decision(), 'pending_approval')

    def test_debounce(self):
        self.data['candidates'][0]['last_changed'] = 1999
        self.assertEqual(self.decision(), 'deferred')

    def test_noop(self):
        self.data['candidates'][0]['before'] = self.data['candidates'][0]['target']
        self.assertEqual(self.decision(), 'skipped')

    def test_success_idempotency(self):
        key = evaluate(self.data)['decisions'][0]['key']
        self.data['ledger'] = [{'key':key, 'timestamp':1500, 'status':'success'}]
        self.assertEqual(self.decision(), 'skipped')

    def test_unknown_prior_result(self):
        key = evaluate(self.data)['decisions'][0]['key']
        self.data['ledger'] = [{'key':key, 'timestamp':1500, 'status':'unknown'}]
        self.assertEqual(self.decision(), 'deferred')

    def test_failure_pause(self):
        self.data['ledger'] = [{'key':'other-action','timestamp':1500,'status':'failed'}]
        self.assertEqual(self.decision(), 'deferred')
        self.data['now'] = 9000
        self.assertEqual(self.decision(), 'deferred')

    def test_run_and_window_rate_limits(self):
        self.data['policy']['max_per_run'] = 1
        candidate = copy.deepcopy(self.data['candidates'][0]); candidate['task_id'] = 'task-3'
        self.data['candidates'].append(candidate)
        result = evaluate(self.data)['decisions']
        self.assertEqual([x['decision'] for x in result], ['covered_by_declared_policy','deferred'])
        self.setUp(); self.data['policy']['max_per_window'] = 1
        self.data['ledger'] = [{'key':'other','timestamp':1500,'status':'success'}]
        self.assertEqual(self.decision(), 'deferred')
        self.data['now'] = 6000
        self.assertEqual(self.decision(), 'covered_by_declared_policy')

    def test_invalid_inputs(self):
        self.data['policy']['max_per_run'] = True
        with self.assertRaises(PlanError): evaluate(self.data)
        self.setUp(); self.data['candidates'] *= 2
        with self.assertRaises(PlanError): evaluate(self.data)

    def test_html_injection_escaped(self):
        self.data['candidates'][0]['before'] = '<script>alert(1)</script>'
        self.data['candidates'][0]['reason'] = '<img src=x onerror=alert(1)>'
        report = render_html(evaluate(self.data))
        self.assertNotIn('<script>', report)
        self.assertNotIn('<img ', report)
        self.assertIn('&lt;script&gt;', report)

    def test_cli_html(self):
        proc = subprocess.run([sys.executable, str(ROOT/'scripts/upkeep_guard.py'), str(ROOT/'examples/upkeep.json'), '--html'], capture_output=True,text=True,check=True)
        self.assertIn('<!doctype html>', proc.stdout)
        self.assertIn('pending_approval', proc.stdout)


if __name__ == '__main__':
    unittest.main()
