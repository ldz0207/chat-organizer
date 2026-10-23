"""Opt-in, read-only resolution of exact bidirectional desktop thread aliases.

This is version-bound desktop preference metadata, not a stable public API.
Unresolved aliases remain unresolved; titles and positions never identify threads.
"""
import json
from pathlib import Path
from urllib.parse import quote
from uuid import UUID


PREFIX = "codex:thread:local:"
ALIAS_PREFIX = "client-new-thread:"
BUILTIN_SECTIONS = {"pinned", "chats", "threads"}


def normalize_snapshot(snapshot, bindings_path=None):
    if not bindings_path or not any(
        key.startswith(PREFIX + ALIAS_PREFIX)
        for section in snapshot.get("sections", []) if section.get("sectionId") not in BUILTIN_SECTIONS
        for key in section.get("itemKeys", [])
    ):
        return snapshot
    path = Path(bindings_path)
    if not path.is_absolute():
        raise ValueError("Sidebar alias metadata must use an absolute device-local path")
    atoms = json.loads(path.read_text(encoding="utf-8-sig")).get("electron-persisted-atom-state", {})
    forward = atoms.get("client-thread-bindings-v1", {})
    if not isinstance(forward, dict):
        raise ValueError("Sidebar alias metadata format changed")
    aliases = {}
    for section in snapshot.get("sections", []):
        if section.get("sectionId") in BUILTIN_SECTIONS:
            continue
        for key in section.get("itemKeys", []):
            if not key.startswith(PREFIX + ALIAS_PREFIX):
                continue
            alias = key[len(PREFIX):]
            identifier = forward.get(alias)
            if identifier is None:
                continue
            if not isinstance(identifier, str):
                raise ValueError("Sidebar alias target identity is invalid")
            try:
                UUID(identifier)
            except ValueError as error:
                raise ValueError("Sidebar alias target identity is invalid") from error
            reverse_key = "thread-client-id-v1:" + quote("local:" + identifier, safe="")
            reverse = atoms.get(reverse_key)
            if reverse is None:
                continue
            if reverse != alias:
                raise ValueError("Sidebar alias forward/reverse identity mismatch")
            aliases[key] = PREFIX + identifier
    sections, seen = [], set()
    for section in snapshot.get("sections", []):
        if section.get("sectionId") in BUILTIN_SECTIONS:
            sections.append(section)
            continue
        keys = [aliases.get(key, key) for key in section.get("itemKeys", [])]
        for key in keys:
            if key.startswith(PREFIX) and key in seen:
                raise ValueError("Sidebar alias creates a duplicate local thread identity")
            if key.startswith(PREFIX):
                seen.add(key)
        sections.append(dict(section, itemKeys=keys))
    return dict(snapshot, sections=sections)
