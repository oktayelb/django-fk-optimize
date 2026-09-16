"""Code-scanning utilities shared by the optimizer commands.

Everything here is importable and runnable on its own -- a Vocabulary can be
built by hand, and the scanners take source text rather than a project -- so the
analysis can be tested without a database, a settings module, or a command.
"""

from .callsites import (
    INSTANCE,
    ITERATION,
    PROBABLE,
    RESOLVED,
    UNRESOLVED,
    CallSite,
    Hints,
    ScanReport,
    scan_file,
    scan_files,
    scan_source,
)
from .imports import ModuleImports
from .sources import discover, package_of, python_files
from .vocabulary import ModelInfo, Relation, Vocabulary

__all__ = [
    "CallSite",
    "Hints",
    "INSTANCE",
    "ITERATION",
    "ModelInfo",
    "ModuleImports",
    "PROBABLE",
    "RESOLVED",
    "Relation",
    "ScanReport",
    "UNRESOLVED",
    "Vocabulary",
    "discover",
    "package_of",
    "python_files",
    "scan_file",
    "scan_files",
    "scan_source",
]
