"""Which files a scan should read.

Kept apart from the scanner so the scanner never touches a filesystem: it takes
source text, which is what makes it testable without a project around it.

It also answers the question of whose code a file is.  A report about a line
nobody in the room can edit is not a finding, it is noise with an exit code
attached, so an installed package is out of scope until somebody asks for it.
"""

from __future__ import annotations

import os
import sysconfig
from functools import lru_cache
from pathlib import Path

SKIP_DIRECTORIES = {
    "migrations",
    "__pycache__",
    ".git",
    ".tox",
    ".venv",
    "venv",
    "node_modules",
    # Only ever reached for a project that vendors its dependencies *inside*
    # an app.  An installed package is kept out by `is_third_party` instead:
    # the walk over such an app starts below site-packages, so that name is
    # never one of the intermediate directories this set is compared against.
    "site-packages",
    ".mypy_cache",
    ".pytest_cache",
}

# The directory a packaging tool unpacks into, under either of the two names
# in use.  Compared as a path component and never as a substring, so that a
# project living in ~/code/site-packages-talk/ stays the author's own code.
VENDOR_DIRECTORIES = frozenset({"site-packages", "dist-packages"})


@lru_cache(maxsize=1)
def install_roots() -> tuple[str, ...]:
    """The directories this interpreter installs packages into.

    `purelib` and `platlib` only, although `sysconfig` offers the standard
    library's directories too: no Django app is ever installed there, and
    including them would make this answer depend on whether the project
    happens to sit under the prefix Python was built with.

    Each ends in a separator so that the prefix test is a test on whole path
    components -- without it `/usr/lib/python3/site-packages` would also claim
    `/usr/lib/python3/site-packages-of-mine`.

    Cached because it is asked once per installed app and `sysconfig` reads
    the build configuration to answer.  A test that fakes the install layout
    calls `install_roots.cache_clear()`.
    """
    roots = set()
    paths = sysconfig.get_paths()
    for name in ("purelib", "platlib"):
        path = paths.get(name)
        if path:
            roots.add(os.path.join(os.path.realpath(path), ""))
    return tuple(sorted(roots))


def is_third_party(path) -> bool:
    """Did this code arrive through a packaging tool rather than get written here?

    The same judgement `recording/wrapper.py::_library_prefixes()` makes about
    a stack frame, asked about an app's directory, and deliberately made the
    same way: from where the code sits, never from what it is called.  A
    project of one's own may perfectly well live in ~/code/django/ or be an
    app named `rest_framework`, and a name test would hand its N+1 to the
    library it was named after.

    An editable install is the case that decides the shape of this.  `pip
    install -e` leaves the source tree where the author keeps it and drops
    only a link into site-packages, so the app's `path` is the working copy
    and this says first-party -- which is the right answer, because that
    source is exactly what the person running the command is editing.

    The two tests are asked of two different spellings of the path on purpose.
    The component test uses the path as the registry gives it, because that is
    the path the report prints and the one the reader recognises.  The prefix
    test resolves first, because a virtualenv's install directory is very
    often reached through a symlink and a prefix comparison between a resolved
    and an unresolved path is a comparison between two different strings.
    """
    if not path:
        return False
    given = Path(path).expanduser().absolute()
    if VENDOR_DIRECTORIES.intersection(given.parts):
        return True
    return os.path.join(os.path.realpath(given), "").startswith(install_roots())


def app_in_scope(config, include_django=False, include_third_party=False) -> bool:
    """Is this app one the run should read?

    Two switches, because two different things get excluded for two different
    reasons.  Django's own apps are excluded because their querysets are not
    the project's to tune; everything else that came out of site-packages is
    excluded because it is somebody else's release.  `--include-django` was
    only ever able to say the first, which left every installed third-party
    app permanently in scope with no flag to turn it off.

    So the flags do not nest: asking for third-party apps does not drag
    django.* in behind them, and asking for django.* does not depend on where
    Django itself happens to be installed.
    """
    if config.name.startswith("django."):
        return include_django
    return include_third_party or not is_third_party(getattr(config, "path", None))


def app_roots(include_django=False, include_third_party=False):
    """(dotted package, directory) for each app worth scanning."""
    from django.apps.registry import apps

    roots = []
    for config in apps.get_app_configs():
        if not app_in_scope(config, include_django, include_third_party):
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


def discover(include_django=False, include_third_party=False, skip=SKIP_DIRECTORIES):
    """[(path, package)] for every scannable file in the project's own apps."""
    found = []
    for package, root in app_roots(include_django, include_third_party):
        for path in python_files(root, skip):
            found.append((path, package_of(path, root, package)))
    return found
