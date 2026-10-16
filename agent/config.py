"""Configuration loader - the single source of truth for all thresholds."""
from __future__ import annotations

import os
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(_ROOT, "config", "config.yaml")


class Cfg(dict):
    """A dict that also supports attribute access, recursively."""

    def __getattr__(self, key):
        try:
            value = self[key]
        except KeyError as exc:  # pragma: no cover - defensive
            raise AttributeError(key) from exc
        return Cfg(value) if isinstance(value, dict) else value

    def get_path(self, dotted: str, default=None):
        node = self
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def load_config(path: str | None = None) -> Cfg:
    path = path or os.environ.get("AGENT_CONFIG", DEFAULT_CONFIG_PATH)
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    cfg = Cfg(raw)
    # Hard safety assertion: the agent must never run against live endpoints.
    if cfg.get_path("safety.real_trading") is not False:
        raise RuntimeError("safety.real_trading must be false - refusing to start.")
    return cfg
