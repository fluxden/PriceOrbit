"""Per-store adapter library.

A growing fallback for stores the importer's generic structured-data layers
can't read on their own (anti-bot big-box retailers, JS-hydrated availability).
The importer pulls :func:`match_adapter` + :func:`apply_adapter` in lazily, so
this package may import the importer's pure helpers without a cycle.
"""
from __future__ import annotations

from app.services.site_adapters.base import SiteAdapter, apply_adapter
from app.services.site_adapters.registry import ADAPTERS, match_adapter

__all__ = ["SiteAdapter", "apply_adapter", "ADAPTERS", "match_adapter"]
