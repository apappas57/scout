from __future__ import annotations
import os
import tomllib
from pathlib import Path

_DEFAULT_PUBLIC = Path(__file__).resolve().parent.parent / "config" / "scout.config.example.toml"
_DEFAULT_PRIVATE = Path(__file__).resolve().parent.parent / "config" / "scout.config.toml"


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_toml(p: Path) -> dict:
    if not Path(p).exists():
        return {}
    with open(p, "rb") as fh:
        return tomllib.load(fh)


def load_config(public: Path = _DEFAULT_PUBLIC, private: Path = _DEFAULT_PRIVATE) -> dict:
    cfg = _deep_merge(_load_toml(public), _load_toml(private))
    paths = cfg.setdefault("paths", {})
    for key, val in list(paths.items()):
        if isinstance(val, str):
            paths[key] = os.path.expanduser(val)
    return cfg
