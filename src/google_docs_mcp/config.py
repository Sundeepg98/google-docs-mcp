"""User-scoped config storage at ~/.google-docs-mcp/config.json.

Lives next to ``token.json`` in the same data dir so everything OAuth-
or installation-adjacent is in one place. Currently stores the
deployed Apps Script Web App URL used by ``convert_docx_to_tabbed_doc``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypedDict


class Config(TypedDict, total=False):
    apps_script_webapp_url: str


def config_path() -> Path:
    override = os.environ.get("GOOGLE_DOCS_DATA_DIR")
    base = Path(override) if override else Path.home() / ".google-docs-mcp"
    return base / "config.json"


def load() -> Config:
    p = config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def save(updates: Config) -> Config:
    """Merge ``updates`` into the existing config and write it back."""
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    current = load()
    current.update(updates)
    p.write_text(json.dumps(current, indent=2))
    return current


def get_webapp_url() -> str | None:
    return load().get("apps_script_webapp_url")
