"""Which files a scan should read.

Kept apart from the scanner so the scanner never touches a filesystem: it takes
source text, which is what makes it testable without a project around it.
"""

from __future__ import annotations

import os
from pathlib import Path

SKIP_DIRECTORIES = {
    "migrations",
    "__pycache__",
    ".git",
    ".tox",
    ".venv",
    "venv",
    "node_modules",
    "site-packages",
    ".mypy_cache",
    ".pytest_cache",
}


def app_roots(include_django=False):
    """(dotted package, directory) for each app worth scanning."""
    from django.apps.registry import apps

    roots = []
    for config in apps.get_app_configs():
        if not include_django and config.name.startswith("django."):
            continue
        if not config.path:
            continue
        roots.append((config.name, Path(config.path)))
    return roots


def python_files(root: Path, skip=SKIP_DIRECTORIES):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield Path(dirpath) / filename


def package_of(path: Path, root: Path, package: str) -> str:
    """Dotted package containing `path`, for resolving relative imports.

    `from .models import Alarm` in alarms/views.py has to become
    "alarms.models", and only the file's own package can say so.
    """
    relative = Path(path).resolve().parent.relative_to(Path(root).resolve())
    parts = [part for part in relative.parts if part not in (".", "")]
    return ".".join([package] + parts)


def discover(include_django=False, skip=SKIP_DIRECTORIES):
    """[(path, package)] for every scannable file in the project's own apps."""
    found = []
    for package, root in app_roots(include_django):
        for path in python_files(root, skip):
            found.append((path, package_of(path, root, package)))
    return found
