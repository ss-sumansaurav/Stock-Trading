"""Settings loader. Thresholds come from settings.yaml, secrets from the environment (.env)."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS = ROOT / "settings.yaml"

if load_dotenv:
    load_dotenv(ROOT / ".env")


def load_settings(path: str | os.PathLike | None = None) -> dict[str, Any]:
    p = Path(path or os.environ.get("AVWAP_SETTINGS", DEFAULT_SETTINGS))
    with open(p, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    # Resolve relative paths against the repo root.
    for key, val in list(cfg.get("paths", {}).items()):
        if val and not os.path.isabs(val):
            cfg["paths"][key] = str(ROOT / val)
    url = os.environ.get("DASHBOARD_URL")
    if url:
        cfg.setdefault("alerts", {})["dashboard_url"] = url
    return cfg


def with_overrides(cfg: dict, **sections: dict) -> dict:
    """Return a copy of cfg with section values overridden (handy in tests)."""
    out = copy.deepcopy(cfg)
    for sec, vals in sections.items():
        out.setdefault(sec, {}).update(vals)
    return out


def telegram_credentials() -> tuple[str | None, str | None]:
    return os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
