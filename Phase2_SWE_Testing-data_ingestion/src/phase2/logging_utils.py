import logging
from pathlib import Path


def configure_logging(workdir: Path, level: str = "INFO") -> Path:
    level_name = level.upper()
    numeric_level = getattr(logging, level_name, logging.INFO)

    log_dir = workdir / "phase2" / "artifacts" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "phase2.log"

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
        force=True,
    )
    return log_path
