"""Access to the optional ``FK_OPTIMIZE`` settings dict.

One accessor, read lazily.  Nothing here reads ``django.conf.settings`` at
import time: ``middleware.py`` imports this module while Django is still
assembling settings, and a module-level read there would either raise
``ImproperlyConfigured`` or freeze a value the project has not finished
writing.

Every key is optional, and a value of the wrong type falls back to its default
instead of raising.  A typo in a settings dict should not take an application
down for the sake of a profiler that is not even running.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SETTING_NAME = "FK_OPTIMIZE"

DEFAULT_RECORDING_PATH = ".fk_optimize/recording.jsonl"
DEFAULT_ENABLED = True
DEFAULT_SAMPLE_SIZE = 500
DEFAULT_MAX_RECORDS = 100_000
DEFAULT_SAMPLE_RATE = 1.0

DEFAULTS = {
    "RECORDING_PATH": DEFAULT_RECORDING_PATH,
    "ENABLED": DEFAULT_ENABLED,
    "SAMPLE_SIZE": DEFAULT_SAMPLE_SIZE,
    "MAX_RECORDS": DEFAULT_MAX_RECORDS,
    "SAMPLE_RATE": DEFAULT_SAMPLE_RATE,
}


@dataclass(frozen=True)
class Config:
    """The settings block, coerced and defaulted.

    `recording_path` stays relative if the project wrote it relative: it is
    resolved against the working directory at write time, which is where
    ``manage.py`` runs and where a developer expects ``.fk_optimize/`` to
    appear.
    """

    recording_path: Path
    enabled: bool
    sample_size: int
    max_records: int
    sample_rate: float


def raw() -> dict:
    """The ``FK_OPTIMIZE`` dict as written, or an empty one.

    Absent settings, an unconfigured Django, and a non-dict value all mean the
    same thing here -- nothing was asked for -- so all three return ``{}``.
    """
    try:
        from django.conf import settings

        value = getattr(settings, SETTING_NAME, None)
    except Exception:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _flag(value, default: bool) -> bool:
    return default if value is None else bool(value)


def _count(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _rate(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN, which would make every comparison false
        return default
    return min(max(number, 0.0), 1.0)


def _path(value, default: str) -> Path:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value.strip():
        return Path(value)
    return Path(default)


def get_config() -> Config:
    """The whole settings block, coerced. Call it; never cache it."""
    block = raw()
    return Config(
        recording_path=_path(block.get("RECORDING_PATH"), DEFAULT_RECORDING_PATH),
        enabled=_flag(block.get("ENABLED"), DEFAULT_ENABLED),
        sample_size=_count(block.get("SAMPLE_SIZE"), DEFAULT_SAMPLE_SIZE),
        max_records=_count(block.get("MAX_RECORDS"), DEFAULT_MAX_RECORDS),
        sample_rate=_rate(block.get("SAMPLE_RATE"), DEFAULT_SAMPLE_RATE),
    )


def recording_path() -> Path:
    return get_config().recording_path
