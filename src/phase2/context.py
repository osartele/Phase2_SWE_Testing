from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class RunContext:
    workdir: Path
    config_path: Path | None
    config: dict[str, Any]
    log_level: str
