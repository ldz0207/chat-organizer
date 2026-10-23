"""Read the calling local thread through the version-bound native sidebar tools."""
import argparse
import json
import os
from pathlib import Path

from app_client import AppClient
from watcher import load_config, validate_groups


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    args=parser.parse_args()
    config=load_config(args.config)
    if os.environ.get('CODEX_VERSION')!=config['backend_version']:
        raise RuntimeError('Untested Codex version; revalidate capabilities first')
    identifier=os.environ['CODEX_THREAD_ID']
    with AppClient(config['app_tools_server'],config.get('app_request_timeout_seconds',90),
                   sidebar_alias_bindings_path=config.get('sidebar_alias_bindings_path')) as app:
        snapshot=app.tool('list_threads',{'limit':50})
    groups={group['native_section_id']:group for group in config['groups']}
    validate_groups(snapshot,groups)
    current=[row for row in snapshot.get('threads',[])+snapshot.get('pinnedThreads',[])
             if row['id']==identifier and row.get('kind')=='codex' and row.get('hostId')=='local']
    if len(current)!=1:
        raise RuntimeError('Calling local thread is not uniquely visible; no write performed')
    key='codex:thread:local:'+identifier
    sections=[section for section in snapshot['sections']
              if section['sectionId'] not in {'pinned','chats','threads'} and key in section['itemKeys']]
    if len(sections)>1:
        raise RuntimeError('Calling thread has ambiguous sidebar membership; no write performed')
    print(json.dumps({'thread':current[0],'custom_section':sections[0] if sections else None,
                      'is_pinned':any(row['id']==identifier for row in snapshot.get('pinnedThreads',[])),
                      'read_only':True},ensure_ascii=False))


if __name__=='__main__':main()
