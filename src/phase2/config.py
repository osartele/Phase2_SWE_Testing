import json
import logging
import tomllib
from pathlib import Path
from typing import Any

import yaml


LOGGER = logging.getLogger(__name__)


def _resolve_config_path(config_path: Path | None, workdir: Path) -> Path | None:
    if config_path is not None:
        candidate = config_path.expanduser()
        if not candidate.is_absolute():
            candidate = workdir / candidate
        return candidate.resolve()

    default_candidates = (
        workdir / "phase2" / "configs" / "experiments" / "default.yaml",
        workdir / "phase2" / "configs" / "experiments" / "default.yml",
        workdir / "phase2" / "configs" / "experiments" / "default.toml",
        workdir / "phase2" / "configs" / "experiments" / "default.json",
    )
    for candidate in default_candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def load_config(config_path: Path | None, workdir: Path) -> tuple[Path | None, dict[str, Any]]:
    resolved = _resolve_config_path(config_path, workdir)
    if resolved is None:
        LOGGER.info("No config file found. Running with defaults.")
        return None, {}

    if not resolved.exists():
        raise FileNotFoundError(f"Config file not found: {resolved}")

    suffix = resolved.suffix.lower()
    raw = resolved.read_text(encoding="utf-8")
    if suffix in {".yaml", ".yml"}:
        payload = yaml.safe_load(raw) or {}
    elif suffix == ".json":
        payload = json.loads(raw)
    elif suffix == ".toml":
        payload = tomllib.loads(raw)
    else:
        raise ValueError(
            f"Unsupported config format '{suffix}'. Use .yaml, .yml, .json, or .toml."
        )

    if not isinstance(payload, dict):
        raise ValueError(f"Config root must be an object/map. Received: {type(payload)!r}")

    LOGGER.info("Loaded config from %s", resolved)
    return resolved, payload
