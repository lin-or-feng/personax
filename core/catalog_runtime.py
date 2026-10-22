"""Resolve operator-owned paths once; never accept catalog paths from HTTP/UI."""
from __future__ import annotations

import os
from pathlib import Path

from .types import KnowledgeCatalogConfig

ROOT = Path(__file__).resolve().parents[1]


def catalog_config(persona: dict | None = None) -> KnowledgeCatalogConfig:
    settings = dict((persona or {}).get("knowledge_catalog", {}) or {})
    if os.getenv("PERSONAX_KNOWLEDGE_DB"):
        settings["db_path"] = os.environ["PERSONAX_KNOWLEDGE_DB"]
    config = KnowledgeCatalogConfig.model_validate(settings)
    for field in ("db_path", "source_root", "qdrant_path"):
        path = Path(getattr(config, field))
        if not path.is_absolute():
            setattr(config, field, str(ROOT / path))
    return config
