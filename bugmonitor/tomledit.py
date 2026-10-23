"""Format-preserving edits to config.toml: comments, layout, and unrelated settings stay as they are."""
from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import tomlkit
from tomlkit.items import AoT, Table


def _plain(v):
    return v.unwrap() if hasattr(v, "unwrap") else v


def _table(doc, dotted: str, create: bool = True):
    node = doc
    for part in dotted.split("."):
        if part not in node:
            if not create:
                return None
            node[part] = tomlkit.table()
        node = node[part]
    return node


def _write(path: Path, doc, original: str) -> None:
    text = tomlkit.dumps(doc)
    tomllib.loads(text)  # refuse to write anything that isn't valid TOML
    if text == original:
        return
    if path.exists():
        shutil.copy2(path, path.with_suffix(".toml.bak"))
    path.write_text(text, encoding="utf-8")


def set_values(path: Path, updates: dict[str, dict], only_if_missing=frozenset()) -> list[str]:
    """updates = {section: {key: value}}. Keys in only_if_missing ("section.key") are never overwritten."""
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    doc = tomlkit.parse(original)
    changes = []
    for section, kv in updates.items():
        tbl = _table(doc, section)
        for key, value in kv.items():
            if key in tbl:
                if f"{section}.{key}" in only_if_missing or _plain(tbl[key]) == value:
                    continue
                comment = getattr(getattr(tbl[key], "trivia", None), "comment", "")
                item = tomlkit.item(value)
                if comment:                                   # keep the line's own comment
                    item.comment(comment.lstrip("# ").rstrip())
                tbl[key] = item
                changes.append(f"[{section}] set {key} = {tomlkit.item(value).as_string()}")
            else:
                tbl[key] = value
                changes.append(f"[{section}] added {key} = {tomlkit.item(value).as_string()}")
    if changes:
        _write(path, doc, original)
    return changes


def set_table_array(path: Path, dotted: str, items: list[dict]) -> None:
    """Replace every [[dotted]] entry (e.g. "routes", "team.members") with `items`."""
    original = path.read_text(encoding="utf-8")
    doc = tomlkit.parse(original)
    *parents, name = dotted.split(".")
    parent = _table(doc, ".".join(parents)) if parents else doc
    aot = tomlkit.aot()
    for it in items:
        t = tomlkit.table()
        for k, v in it.items():
            if v is not None:
                t[k] = v
        aot.append(t)
    tail = []
    if name in parent:
        old = parent[name]
        if isinstance(old, AoT) and old.body:
            # Comments after the last [[entry]] (e.g. the next section's commented template)
            # belong to that entry in tomlkit; carry them over so they survive the rewrite.
            body = old.body[-1].value.body
            while body and body[-1][0] is None:
                tail.insert(0, body.pop()[1])
        del parent[name]
    elif items:
        # New array: lift trailing comments off the end of the parent so the entries land above them.
        container = parent.value if isinstance(parent, Table) else parent
        body = container.body
        if body and body[-1][0] is not None and isinstance(body[-1][1], Table):
            body = body[-1][1].value.body
        while body and body[-1][0] is None:
            tail.insert(0, body.pop()[1])
    if items:
        if not tail:
            tail = [tomlkit.ws("\n")]
        for item in tail:
            aot.body[-1].value.append(None, item)
        parent[name] = aot
    else:
        container = parent.value if isinstance(parent, Table) else parent
        for item in tail:
            container.append(None, item)
    _write(path, doc, original)
    check = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    node = check
    for p in dotted.split("."):
        node = node.get(p, {}) if isinstance(node, dict) else {}
    assert len(node or []) == len(items), f"{dotted}: expected {len(items)} entries after saving"
