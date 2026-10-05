#!/usr/bin/env python3
"""Offline-only plan validator. Never opens a harness, writes files, or applies a plan."""
import argparse
import hashlib
import json
import sys
import unicodedata


class PlanError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise PlanError(message)


def text(value, label):
    require(isinstance(value, str) and bool(value.strip()), label + ' must be nonempty text')
    return value


def exact(obj, keys, label):
    require(isinstance(obj, dict) and set(obj) == set(keys), label + ' has missing/unknown fields')


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(obj):
    return hashlib.sha256(canonical(obj).encode('utf-8')).hexdigest()


def keyed(rows, fields, label):
    require(isinstance(rows, list), label + ' must be a list')
    result = {}
    for row in rows:
        exact(row, fields, label)
        key = text(row['id'], label + '.id')
        require(key not in result, label + ' duplicate ID: ' + key)
        result[key] = row
    return result


def inventory_check(inv):
    exact(inv, ['schema_version', 'context', 'snapshot_revision', 'groups', 'tasks'], 'inventory')
    require(type(inv['schema_version']) is int and inv['schema_version'] == 1, 'unsupported inventory version')
    exact(inv['context'], ['harness', 'version', 'device', 'account_scope', 'route'], 'context')
    for k, v in inv['context'].items():
        text(v, 'context.' + k)
    require(inv['context']['route'] in ['supported-native', 'native-ui', 'proposal-only'], 'unknown route')
    text(inv['snapshot_revision'], 'snapshot_revision')
    groups = keyed(inv['groups'], ['id', 'name', 'description'], 'groups')
    names = set()
    for group in groups.values():
        name = unicodedata.normalize('NFC', text(group['name'], 'group name')).casefold().strip()
        require(name not in names, 'duplicate group names are outside this offline helper; use ID-labeled manual proposal without renaming groups')
        names.add(name)
        text(group['description'], 'group description')
    tasks = keyed(inv['tasks'], ['id', 'title', 'group_id', 'status', 'active', 'protected', 'revision'], 'tasks')
    for task in tasks.values():
        text(task['title'], 'task title')
        text(task['revision'], 'task revision')
        text(task['status'], 'task status')
        require(type(task['active']) is bool and type(task['protected']) is bool, 'active/protected must be explicit booleans')
        require(task['group_id'] is None or (isinstance(task['group_id'], str) and task['group_id'] in groups), 'task group missing from inventory')
    return groups, tasks


def preview(inv, proposal):
    old_groups, tasks = inventory_check(inv)
    exact(proposal, ['schema_version', 'inventory_sha256', 'groups', 'assignments', 'pending'], 'proposal')
    require(type(proposal['schema_version']) is int and proposal['schema_version'] == 1, 'unsupported proposal version')
    require(proposal['inventory_sha256'] == digest(inv), 'inventory changed; regenerate preview and obtain matching approval')
    groups = keyed(proposal['groups'], ['id', 'name', 'description'], 'proposed groups')
    names = set()
    for key, group in groups.items():
        name = unicodedata.normalize('NFC', text(group['name'], 'group name')).casefold().strip()
        require(name not in names, 'duplicate/ambiguous proposed group names')
        names.add(name)
        text(group['description'], 'group description')
        if key not in old_groups:
            require(key.startswith('new:'), 'new group needs local new: ID (not a fabricated native ID)')
    require(list(groups)[:len(old_groups)] == list(old_groups), 'preserve existing group order; append new groups only')
    for key, old in old_groups.items():
        require(groups.get(key) == old, 'existing groups must be preserved exactly')
    require(isinstance(proposal['pending'], list), 'pending must be a list')
    pending = set()
    for key in proposal['pending']:
        require(isinstance(key, str) and key in tasks and key not in pending, 'invalid/duplicate pending task')
        pending.add(key)
    require(isinstance(proposal['assignments'], list), 'assignments must be a list')
    seen, moves, noops = set(), [], []
    for item in proposal['assignments']:
        exact(item, ['task_id', 'target_group_id', 'reason'], 'assignment')
        sid, target = item['task_id'], item['target_group_id']
        require(isinstance(sid, str) and sid in tasks and sid not in seen and sid not in pending, 'invalid/duplicate/pending assignment')
        require(isinstance(target, str) and target in groups, 'unknown target group')
        text(item['reason'], 'assignment reason')
        seen.add(sid)
        task = tasks[sid]
        if task['group_id'] == target:
            noops.append(sid)
            continue
        require(task['status'] == 'idle' and task['active'] is False and task['protected'] is False,
                'task is active, protected, archived, running, or status unknown: ' + sid)
        moves.append({'task_id': sid, 'title': task['title'], 'from_group_id': task['group_id'],
                      'to_group_id': target, 'reason': item['reason'], 'expected_revision': task['revision']})
    retained = sorted(set(tasks) - {x['task_id'] for x in moves} - pending)
    final_counts = {key: 0 for key in groups}
    targets = {x['task_id']: x['to_group_id'] for x in moves}
    ungrouped = 0
    for sid, task in tasks.items():
        target = targets.get(sid, task['group_id'])
        if target is None:
            ungrouped += 1
        else:
            final_counts[target] += 1
    new_groups = [g for key, g in groups.items() if key not in old_groups]
    plan = {'schema_version': 1, 'context': inv['context'], 'inventory_sha256': digest(inv),
            'groups': proposal['groups'], 'new_groups': new_groups, 'moves': moves,
            'unchanged': retained, 'noops': noops, 'pending': sorted(pending),
            'counts': {'groups_total': len(groups), 'groups_new': len(new_groups),
                       'tasks_total': len(tasks), 'moves': len(moves), 'unchanged': len(retained),
                       'pending': len(pending), 'ungrouped_after_proposal': ungrouped,
                       'per_group_after_proposal': final_counts}}
    return {'plan': plan, 'plan_sha256': digest(plan), 'execution': 'NOT_EXECUTED',
            'approval': 'USER_CONFIRMATION_REQUIRED',
            'notice': 'Offline validation only; route claims are unverified. No harness or approval is authenticated.'}


def pairs_unique(pairs):
    out = {}
    for k, v in pairs:
        require(k not in out, 'duplicate JSON key: ' + k)
        out[k] = v
    return out


def load(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f, object_pairs_hook=pairs_unique,
                         parse_constant=lambda value: (_ for _ in ()).throw(PlanError('non-finite JSON number')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inventory')
    parser.add_argument('proposal', nargs='?')
    parser.add_argument('--fingerprint', action='store_true', help='validate inventory and print its SHA-256')
    args = parser.parse_args()
    try:
        inv = load(args.inventory)
        if args.fingerprint:
            require(args.proposal is None, '--fingerprint takes inventory only')
            inventory_check(inv)
            print(digest(inv))
        else:
            require(args.proposal is not None, 'proposal is required unless --fingerprint')
            print(json.dumps(preview(inv, load(args.proposal)), ensure_ascii=False, indent=2))
        return 0
    except (PlanError, OSError, ValueError, TypeError) as exc:
        print('Validation failed: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
