#!/usr/bin/env python3
"""Run the whole command against real projects, with a real app registry.

`corpus.py` reads eight projects out of source and never imports one. That is
the point of it -- it costs a shallow clone and a few seconds -- but it means a
third of this package is all that job can reach: the AST scanner, and a
vocabulary assembled by parsing `models.py`. `Vocabulary.from_apps`,
`Tables.from_apps`, the join between the scan and the recording, the verdict
engine, the benchmark, the renderer and the management command itself are only
ever exercised against models this repository wrote for itself.

So this one does the expensive half, for three projects instead of eight.
Per project: shallow clone, a throwaway virtualenv, `pip install` the
project and this package into it, a minimal settings module, `migrate`, and
then `manage.py fk_optimize --json` for real. Nothing is stubbed and nothing is
faked; the app registry is the project's own.

sqlite, and no service container. A live job that needs postgres to start is a
live job that goes red for reasons that have nothing to do with this package,
and the `postgres` job already covers the backend-shaped code.

What it asserts:

* `migrate` and the command both exit cleanly -- a traceback here is the whole
  finding, and the projects are chosen so that a failure is ours;
* the JSON parses and carries `schema_version`, because that payload is the
  contract anything downstream reads;
* the census adds up: every manager expression the scanner met is followed,
  terminal or not followed, and those three sum to `seen`;
* models, call sites, followed expressions, verdicts and timed relations stay
  at or above the floors recorded beside each project.

Floors, not exact counts, for the reason `corpus.py` gives: these projects keep
moving, and an expectation pinned to today's number fails for their reasons
rather than ours. Roughly four-fifths of what was seen when the project was
added, which catches a collapse and ignores a drift.

Benchmarking is left ON. The database is empty, so the timings themselves say
nothing -- but that is not what the run is for. A previously reported crash was
a benchmark querying a table that did not exist, which took the whole run down
with it, and that is a shape only a real schema can produce. On an empty
migrated database it costs a few hundred milliseconds. Each project is also run
once with `--no-callsites`, which is the command's other branch entirely: it
times every relation of every selected model, so every table gets touched,
including the ones no call site mentions.

An honest note on the third project. django-machina is installed non-editable
and scanned with `--include-third-party`, because its build backend has no PEP
660 hook and cannot be installed in editable mode. It earns its place anyway:
its models are generated at import time by a factory rather than written out as
classes, so `corpus.py`'s source-read vocabulary cannot see them at all, and
this is the only job that watches the registry resolve them.

    python scripts/live.py                      # all of them
    python scripts/live.py --only allauth       # one, by name
    python scripts/live.py --workdir /var/tmp/x # reuse the clones and venvs

The workdir is never deleted. Clones and virtualenvs are the slow part of this
by an order of magnitude, and a second run over the same directory reuses both.
CI throws its runner away and does not care; locally, pass `--workdir`.

Exit status is 0 when every project behaved, 1 when any did not.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

MANAGE = """\
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
from django.core.management import execute_from_command_line

execute_from_command_line(sys.argv)
"""

URLS = "urlpatterns = []\n"

# The smallest settings module each project will boot from. Whatever a project
# adds is appended, so it can extend INSTALLED_APPS, add middleware, or replace
# any of this outright -- it is only Python, read top to bottom.
BASE_SETTINGS = """\
SECRET_KEY = "live"
DEBUG = False
ALLOWED_HOSTS = ["*"]
SITE_ID = 1
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
ROOT_URLCONF = "urls"
STATIC_URL = "/static/"
DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "live.sqlite3"}
}
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.sites",
    "django.contrib.staticfiles",
    "django_fk_optimize",
]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
"""


@dataclass(frozen=True)
class Project:
    """One real project, and what a healthy run of it looks like.

    `settings` is appended to `BASE_SETTINGS`. `arguments` go to every
    `fk_optimize` invocation. `floors` are the counts the run must not fall
    below, read out of the JSON report's coverage block plus the number of
    verdicts.
    """

    url: str
    settings: str
    floors: dict
    editable: bool = True
    arguments: tuple[str, ...] = ()


PROJECTS = {
    # An auth library: small, pure python, no optional extras needed to boot.
    # Its call sites are almost all class-based, so this is where the registry
    # meets the `probable` half of the scanner.
    "allauth": Project(
        url="https://github.com/pennersr/django-allauth.git",
        settings="""
INSTALLED_APPS += [
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.mfa",
    "allauth.usersessions",
    "allauth.headless",
]
MIDDLEWARE += ["allauth.account.middleware.AccountMiddleware"]
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
""",
        # No verdicts and no timings on an empty database: allauth's call sites
        # touch no relation the estimator can price. The scan and the registry
        # are what this project is here to prove.
        floors={"models": 5, "sites": 15, "sites_attributed": 14},
    ),
    # A ticketing app, and the one with real verdicts: reverse relations, a
    # filter(id__in=...) that escapes into a call, and six relations the
    # benchmark actually times against the schema.
    "helpdesk": Project(
        url="https://github.com/django-helpdesk/django-helpdesk.git",
        settings="""
INSTALLED_APPS += [
    "django.contrib.admin",
    "django.contrib.humanize",
    "helpdesk",
    "rest_framework",
]
# Teams mode pulls in pinax-teams, which is an optional extra; left on, the
# models carry a lazy reference to an app that is not installed and the system
# check refuses to run anything.
HELPDESK_TEAMS_MODE_ENABLED = False
""",
        floors={
            "models": 15,
            "sites": 22,
            "sites_attributed": 22,
            "verdicts": 6,
            "relations_benchmarked": 4,
        },
    ),
    # A forum whose concrete models are produced by a factory at import time.
    # `corpus.py` reads 53 model classes out of its source and can still only
    # follow four expressions in the whole project, because the names its call
    # sites use are never written down as classes. The registry has them.
    "machina": Project(
        url="https://github.com/ellmetha/django-machina.git",
        editable=False,
        arguments=("--include-third-party",),
        settings="""
from machina import MACHINA_MAIN_TEMPLATE_DIR

INSTALLED_APPS += [
    "mptt",
    "haystack",
    "widget_tweaks",
    "machina",
    "machina.apps.forum",
    "machina.apps.forum_conversation",
    "machina.apps.forum_conversation.forum_attachments",
    "machina.apps.forum_conversation.forum_polls",
    "machina.apps.forum_feeds",
    "machina.apps.forum_moderation",
    "machina.apps.forum_search",
    "machina.apps.forum_tracking",
    "machina.apps.forum_member",
    "machina.apps.forum_permission",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [MACHINA_MAIN_TEMPLATE_DIR],
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "machina.core.context_processors.metadata",
            ],
            "loaders": [
                "django.template.loaders.filesystem.Loader",
                "django.template.loaders.app_directories.Loader",
            ],
        },
    }
]
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "machina_attachments": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache"
    },
}
# The simple backend needs no index and no service; haystack only has to import.
HAYSTACK_CONNECTIONS = {
    "default": {"ENGINE": "haystack.backends.simple_backend.SimpleEngine"}
}
""",
        floors={
            "models": 10,
            "sites": 4,
            "sites_attributed": 4,
            "verdicts": 4,
            "relations_benchmarked": 4,
        },
    ),
}


# ----------------------------------------------------------------------
# building one project
# ----------------------------------------------------------------------


@dataclass
class Outcome:
    """What happened to one project, and what was wrong with it."""

    name: str
    seconds: float = 0.0
    revision: str = "unknown"
    coverage: dict = field(default_factory=dict)
    verdicts: int = 0
    problems: list = field(default_factory=list)


def run(command, cwd=None, timeout=1800) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(part) for part in command],
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def must(done: subprocess.CompletedProcess, what: str) -> None:
    """Anything that has to have worked for the next step to mean anything."""
    if done.returncode != 0:
        raise RuntimeError(
            f"{what} exited {done.returncode}\n{done.stdout}\n{done.stderr}"
        )


def clone(name: str, url: str, into: Path) -> Path:
    target = into / name
    if target.exists():
        return target
    must(
        run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--filter=blob:none",
                "--quiet",
                url,
                target,
            ]
        ),
        f"cloning {name}",
    )
    return target


def revision(path: Path) -> str:
    done = run(["git", "-C", path, "rev-parse", "--short", "HEAD"], timeout=60)
    return done.stdout.strip() or "unknown"


def virtualenv(root: Path, source: Path, editable: bool) -> Path:
    """A venv with the project and this package in it. Returns its python."""
    python = root / "venv" / "bin" / "python"
    if not python.exists():
        must(run([sys.executable, "-m", "venv", root / "venv"]), "creating the venv")
        must(run([python, "-m", "pip", "install", "-q", "--upgrade", "pip"]), "pip")
    install = [python, "-m", "pip", "install", "-q"]
    must(
        run(install + (["-e"] if editable else []) + [source]), "installing the project"
    )
    # Installed the same way a user would, from the tree rather than from an
    # editable link, so a module missing from the package is a failure here.
    must(run(install + [REPO]), "installing django_fk_optimize")
    return python


def scaffold(root: Path, project: Project) -> None:
    (root / "manage.py").write_text(MANAGE, encoding="utf-8")
    (root / "urls.py").write_text(URLS, encoding="utf-8")
    (root / "settings.py").write_text(
        BASE_SETTINGS + project.settings, encoding="utf-8"
    )


# ----------------------------------------------------------------------
# judging one project
# ----------------------------------------------------------------------


def inspect(payload: dict, floors: dict) -> list[str]:
    problems = []
    if "schema_version" not in payload:
        problems.append("the JSON report carries no schema_version")
    coverage = payload.get("coverage")
    if not coverage:
        problems.append("the JSON report carries no coverage block")
        return problems

    if coverage.get("scan_errors"):
        problems.append(f"{coverage['scan_errors']} files the scanner could not parse")
    for flag, wanted in (
        ("scanned", True),
        ("benchmarked", True),
        ("timed_out", False),
    ):
        if coverage.get(flag) is not wanted:
            problems.append(f"{flag} is {coverage.get(flag)!r}, expected {wanted!r}")

    # The same invariant `corpus.py` asserts, taken here over a vocabulary
    # built from the app registry rather than from source.
    parts = (
        coverage.get("sites_attributed", 0)
        + coverage.get("sites_terminal", 0)
        + coverage.get("sites_unresolved", 0)
    )
    if parts != coverage.get("sites_seen"):
        problems.append(
            f"census does not add up -- {coverage.get('sites_seen')} seen, "
            f"parts sum to {parts}"
        )

    counts = dict(coverage, verdicts=len(payload.get("verdicts", [])))
    for key, floor in sorted(floors.items()):
        found = counts.get(key)
        if found is None:
            problems.append(f"{key} is not in the report at all")
        elif found < floor:
            problems.append(f"{key} fell to {found}, floor is {floor}")
    return problems


def live(name: str, project: Project, workdir: Path) -> Outcome:
    started = time.perf_counter()
    outcome = Outcome(name)
    root = workdir / name
    root.mkdir(parents=True, exist_ok=True)

    source = clone(name, project.url, workdir / "src")
    outcome.revision = revision(source)
    python = virtualenv(root, source, project.editable)
    scaffold(root, project)

    must(
        run([python, "manage.py", "migrate", "--run-syncdb", "-v", "0"], cwd=root),
        "migrate",
    )

    destination = root / "report.json"
    destination.unlink(missing_ok=True)
    report = run(
        [python, "manage.py", "fk_optimize", "--json", destination, *project.arguments],
        cwd=root,
    )
    if report.returncode != 0:
        outcome.problems.append(
            f"fk_optimize exited {report.returncode}\n{report.stdout}\n{report.stderr}"
        )
    elif not destination.exists():
        outcome.problems.append(f"--json {destination} wrote nothing")
    else:
        try:
            payload = json.loads(destination.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            outcome.problems.append(f"the JSON report does not parse: {exc}")
        else:
            outcome.coverage = payload.get("coverage", {})
            outcome.verdicts = len(payload.get("verdicts", []))
            outcome.problems.extend(inspect(payload, project.floors))

    # The other branch of the command: no scan, no join, every relation of
    # every selected model timed against the schema. Nothing else in CI runs
    # it, and it is the branch that touches tables no call site names.
    sweep = run(
        [python, "manage.py", "fk_optimize", "--no-callsites", *project.arguments],
        cwd=root,
    )
    if sweep.returncode != 0:
        outcome.problems.append(
            f"--no-callsites exited {sweep.returncode}\n{sweep.stdout}\n{sweep.stderr}"
        )

    outcome.seconds = round(time.perf_counter() - started, 1)
    return outcome


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workdir", default=None, help="where to keep clones and venvs"
    )
    parser.add_argument("--only", nargs="*", choices=sorted(PROJECTS), help="a subset")
    parser.add_argument("--json", default=None, help="write the full report here")
    options = parser.parse_args(argv)

    workdir = Path(options.workdir or tempfile.mkdtemp(prefix="fk-live-"))
    workdir.mkdir(parents=True, exist_ok=True)
    print(f"live workdir: {workdir}")

    outcomes, crashed = [], []
    for name in options.only or list(PROJECTS):
        print(f"-- {name}")
        try:
            outcome = live(name, PROJECTS[name], workdir)
        except Exception:
            # Installing and migrating somebody else's project is the fragile
            # part, so it is reported in full rather than summarised.
            crashed.append(name)
            print(traceback.format_exc())
            continue
        outcomes.append(outcome)
        coverage = outcome.coverage
        print(
            f"   {outcome.revision}  {coverage.get('models', 0)} models  "
            f"{coverage.get('sites', 0)} sites  {outcome.verdicts} verdicts  "
            f"{coverage.get('relations_benchmarked', 0)} relations timed  "
            f"{outcome.seconds}s"
        )
        print(
            f"   {coverage.get('sites_attributed', 0)}/{coverage.get('sites_seen', 0)} "
            f"expressions followed, {coverage.get('sites_terminal', 0)} terminal, "
            f"{coverage.get('sites_unresolved', 0)} not followed"
        )

    if options.json:
        Path(options.json).write_text(
            json.dumps(
                {
                    outcome.name: {
                        "revision": outcome.revision,
                        "seconds": outcome.seconds,
                        "verdicts": outcome.verdicts,
                        "coverage": outcome.coverage,
                        "problems": outcome.problems,
                    }
                    for outcome in outcomes
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    print(f"\n{len(outcomes)} projects installed, migrated and reported on")
    if crashed:
        print(f"\nCRASHED: {', '.join(crashed)}")
    for outcome in outcomes:
        for problem in outcome.problems:
            print(f"REGRESSION: {outcome.name}: {problem}")
    return 1 if crashed or any(o.problems for o in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
