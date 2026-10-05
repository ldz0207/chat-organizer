#!/usr/bin/env python3
"""Offline upkeep-policy evaluator; never authenticates approval or modifies a harness."""
import argparse
import html
import json
import sys
from plan_preview import PlanError, canonical, digest, exact, load, require, text


def integer(value, label, minimum=0):
    require(type(value) is int and value >= minimum, label + ' must be an integer >= ' + str(minimum))


def evaluate(data):
    exact(data, ['now', 'context', 'taxonomy_sha256', 'trigger', 'capabilities', 'policy', 'candidates', 'ledger'], 'request')
    now = data['now']
    integer(now, 'now')
    context = data['context']
    exact(context, ['harness', 'version', 'device', 'account_scope'], 'context')
    for value in context.values():
        text(value, 'context value')
    text(data['taxonomy_sha256'], 'taxonomy_sha256')
    require(data['trigger'] in ['event', 'invocation'], 'unsupported abstract trigger type')
    caps = data['capabilities']
    exact(caps, ['read_back', 'move', 'rename', 'conditional_write', 'event_delivery'], 'capabilities')
    require(all(type(v) is bool for v in caps.values()), 'capabilities must be explicit booleans')
    policy = data['policy']
    exact(policy, ['id', 'enabled', 'approval_ref', 'context', 'taxonomy_sha256', 'actions', 'allowed_group_ids',
                   'triggers', 'max_per_run', 'max_per_window', 'window_seconds', 'debounce_seconds',
                   'title_max_chars', 'title_style', 'expires_at', 'workspace_id', 'task_scope', 'allowed_task_categories'], 'policy')
    text(policy['id'], 'policy ID')
    require(type(policy['enabled']) is bool, 'enabled must be boolean')
    require(isinstance(policy['approval_ref'], str), 'approval_ref must be text')
    exact(policy['context'], context.keys(), 'policy context')
    for value in policy['context'].values():
        text(value, 'policy context value')
    text(policy['taxonomy_sha256'], 'policy taxonomy')
    for key, allowed in [('actions', {'move', 'rename'}), ('triggers', {'event', 'invocation'})]:
        require(isinstance(policy[key], list) and all(isinstance(x, str) and x in allowed for x in policy[key])
                and len(set(policy[key])) == len(policy[key]), 'invalid policy ' + key)
    require(isinstance(policy['allowed_group_ids'], list) and all(isinstance(x, str) and x for x in policy['allowed_group_ids']), 'invalid allowed groups')
    require(len(set(policy['allowed_group_ids'])) == len(policy['allowed_group_ids']), 'duplicate allowed group ID')
    for key in ['max_per_run', 'max_per_window', 'window_seconds', 'title_max_chars']:
        integer(policy[key], key, 1)
    integer(policy['debounce_seconds'], 'debounce_seconds')
    text(policy['title_style'], 'title_style')
    text(policy['workspace_id'], 'workspace_id')
    text(policy['task_scope'], 'task_scope')
    require(isinstance(policy['allowed_task_categories'], list) and bool(policy['allowed_task_categories'])
            and all(isinstance(x, str) and x for x in policy['allowed_task_categories']), 'invalid task categories')
    require(policy['expires_at'] is None or type(policy['expires_at']) is int, 'invalid expires_at')
    ledger = data['ledger']
    require(isinstance(ledger, list), 'ledger must be list')
    for record in ledger:
        exact(record, ['key', 'timestamp', 'status'], 'ledger record')
        text(record['key'], 'ledger key')
        integer(record['timestamp'], 'ledger timestamp')
        require(record['timestamp'] <= now, 'ledger timestamp in future')
        require(record['status'] in ['success', 'failed', 'unknown'], 'invalid ledger status')
    require(isinstance(data['candidates'], list), 'candidates must be list')
    window = [x for x in ledger if now - x['timestamp'] < policy['window_seconds']]
    used = len({x['key'] for x in window if x['status'] == 'success'})
    selected, seen, decisions = 0, set(), []
    for candidate in data['candidates']:
        exact(candidate, ['task_id', 'action', 'before', 'target', 'reason', 'status', 'active', 'protected',
                          'current_revision', 'expected_revision', 'last_changed', 'classification_confident',
                          'manual_lock', 'title_owner', 'ownership_verified', 'last_policy_title', 'style_matches',
                          'workspace_id', 'task_category', 'scope_verified'], 'candidate')
        sid = text(candidate['task_id'], 'task ID')
        require(candidate['action'] in ['move', 'rename'], 'unsupported action')
        token = (sid, candidate['action'])
        require(token not in seen, 'duplicate task/action candidate')
        seen.add(token)
        for k in ['target', 'reason', 'status', 'current_revision', 'expected_revision', 'workspace_id', 'task_category']:
            text(candidate[k], k)
        for k in ['active', 'protected', 'classification_confident', 'manual_lock', 'ownership_verified', 'style_matches', 'scope_verified']:
            require(type(candidate[k]) is bool, k + ' must be explicit boolean')
        require(candidate['before'] is None or isinstance(candidate['before'], str), 'invalid before')
        require(candidate['last_policy_title'] is None or isinstance(candidate['last_policy_title'], str), 'invalid last_policy_title')
        require(candidate['title_owner'] in ['default', 'policy', 'manual', 'unknown'], 'invalid title_owner')
        integer(candidate['last_changed'], 'last_changed')
        require(candidate['last_changed'] <= now, 'last_changed in future')
        key = digest({'context': context, 'policy_id': policy['id'], 'task_id': sid,
                      'action': candidate['action'], 'target': candidate['target']})
        state, reason = 'covered_by_declared_policy', 'Still requires actual approval, native capability, and concurrency verification'
        action = candidate['action']
        if not policy['enabled'] or not policy['approval_ref'].strip():
            state, reason = 'pending_approval', 'Automatic policy is not opted in'
        elif policy['context'] != context or policy['taxonomy_sha256'] != data['taxonomy_sha256']:
            state, reason = 'pending_approval', 'Context or approved taxonomy changed'
        elif policy['expires_at'] is not None and now >= policy['expires_at']:
            state, reason = 'pending_approval', 'Policy expired'
        elif (candidate['workspace_id'] != policy['workspace_id'] or candidate['task_category'] not in policy['allowed_task_categories']
              or not candidate['scope_verified']):
            state, reason = 'pending_approval', 'Outside verified workspace or task-category scope'
        elif action not in policy['actions'] or data['trigger'] not in policy['triggers']:
            state, reason = 'pending_approval', 'Outside allowed action or trigger scope'
        elif not caps['read_back'] or not caps['conditional_write'] or not caps[action] or (data['trigger'] == 'event' and not caps['event_delivery']):
            state, reason = 'pending_approval', 'Verified native capability unavailable; proposal only'
        elif candidate['active'] or candidate['status'] != 'idle':
            state, reason = 'deferred', 'Active or non-idle task'
        elif candidate['protected'] or candidate['manual_lock']:
            state, reason = 'skipped', 'Protected or manually locked'
        elif candidate['current_revision'] != candidate['expected_revision']:
            state, reason = 'deferred', 'Revision changed; re-read before retry'
        elif action == 'move' and candidate['before'] is not None and candidate['before'] != candidate['target']:
            state, reason = 'pending_approval', 'Existing membership is outside incremental ungrouped scope'
        elif action == 'move' and (candidate['target'] not in policy['allowed_group_ids'] or not candidate['classification_confident']):
            state, reason = 'pending_approval', 'Unapproved target or ambiguous classification'
        elif action == 'rename' and (not candidate['ownership_verified'] or candidate['title_owner'] not in ['default', 'policy']
                                    or (candidate['title_owner'] == 'policy' and candidate['before'] != candidate['last_policy_title'])):
            state, reason = 'skipped', 'Title ownership unknown or changed; preserve and lock externally'
        elif action == 'rename' and (not candidate['style_matches'] or len(candidate['target']) > policy['title_max_chars']
                                    or any(ord(c) < 32 for c in candidate['target'])):
            state, reason = 'pending_approval', 'Title does not fit approved style or length'
        elif candidate['before'] == candidate['target'] or any(x['key'] == key and x['status'] == 'success' for x in ledger):
            state, reason = 'skipped', 'Already satisfied or already recorded; no duplicate action'
        elif any(x['key'] == key and x['status'] == 'unknown' for x in ledger):
            state, reason = 'deferred', 'Prior outcome unknown; verify before retry'
        elif any(x['status'] in ['failed', 'unknown'] for x in ledger):
            state, reason = 'deferred', 'Unresolved failure/unknown outcome; reconcile before more work'
        elif now - candidate['last_changed'] < policy['debounce_seconds']:
            state, reason = 'deferred', 'Debounce window still open'
        elif selected >= policy['max_per_run'] or used + selected >= policy['max_per_window']:
            state, reason = 'deferred', 'Rate limit reached; retain for next genuine invocation/event'
        if state == 'covered_by_declared_policy':
            selected += 1
        decisions.append({'task_id': sid, 'action': action, 'before': candidate['before'], 'target': candidate['target'],
                          'key': key, 'decision': state, 'reason': reason, 'candidate_reason': candidate['reason']})
    return {'execution': 'NOT_EXECUTED', 'policy_sha256': digest(policy), 'decisions': decisions,
            'notice': 'Offline evaluation only. Declared approval, ownership, style, ledger, and capabilities are not authenticated.'}


def render_html(report):
    esc = lambda v: html.escape('' if v is None else str(v), quote=True)
    rows = ''.join('<tr>' + ''.join('<td>' + esc(row.get(k)) + '</td>' for k in
                  ['task_id', 'action', 'before', 'target', 'decision', 'reason']) + '</tr>' for row in report['decisions'])
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<title>自动维护审阅 · 未执行</title><style>body{font:16px system-ui;margin:2rem;max-width:1100px}'
            'table{border-collapse:collapse;width:100%}td,th{border:1px solid #bbb;padding:.65rem;text-align:left}'
            'td{overflow-wrap:anywhere}th{background:#eef3f6}</style><h1>自动维护审阅 · 未执行</h1>'
            '<p>覆盖声明策略 ≠ 已验证授权或已执行。此静态报告没有启用、接受或写入功能。</p><p>' + esc(report['notice']) + '</p>'
            '<table><thead><tr><th>任务 ID</th><th>动作</th><th>原值</th><th>建议值</th><th>判定</th><th>原因</th></tr></thead>'
            '<tbody>' + rows + '</tbody></table><p>策略摘要：' + esc(report['policy_sha256']) + '</p></html>')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input')
    parser.add_argument('--html', action='store_true')
    args = parser.parse_args()
    try:
        report = evaluate(load(args.input))
        print(render_html(report) if args.html else json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (PlanError, OSError, ValueError, TypeError) as exc:
        print('Validation failed: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
