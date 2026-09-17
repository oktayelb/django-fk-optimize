"""End-to-end check: does the installed package actually find an N+1?

The unit suite imports the package from the source tree and calls its pieces
directly.  This does neither.  Each scenario writes a throwaway Django project
on disk, runs code that loads a foreign key one row at a time, records it, and
then shells out to `manage.py fk_optimize --fail-on-findings` and looks at the
exit code and at the report it printed.

That is the whole product: scan, record, judge, and a CI gate that opens once
the code is fixed.  Run it against a wheel installed into a clean virtualenv
and packaging regressions show up here too.

The scenarios, and the bug each one stands guard over:

* `basic` -- the textbook case.  Two models, one FK, a function that loops.
  The happy path; everything else is a variation on it.
* `invocations` -- the same view exercised three times.  N is per call, so the
  report must say 200 rows and not 600.  A recording summed across the whole
  run reported the latter, and a smoke test that calls its view exactly once
  can never see the difference.
* `chain` -- `book.publisher.country.name`, two hops.  The fix has to be
  `select_related("publisher__country")` and not the first hop of it, and
  applying what the report printed has to genuinely clear the finding rather
  than leave a half-applied fix reading as already fine.
* `class` -- a queryset reached through `self.get_queryset()`.  The shape every
  class-based view in Django has, and one the scanner used to find nothing at
  all in.
* `third-party` -- an app installed from a directory named `site-packages`.
  Somebody else's release is not yours to change, so its N+1 is out of the
  report and out of the CI gate unless `--include-third-party` asks for it.

Every assertion is on what a user reads: the exit code, the relation named, the
numbers in the block.  Internal consistency is checked as well as presence --
a block that says `1 + 400 queries` and then `200 fewer queries` is wrong even
though both numbers are plausible on their own.

    python scripts/smoke.py                 # uses this interpreter
    python scripts/smoke.py --only chain    # one scenario, by name
    python scripts/smoke.py --keep          # leave the projects on disk

It works from a plain source tree and from an installed wheel.  If the
interpreter it is asked to run the projects with cannot already import
`django_fk_optimize`, the repository root is put on that interpreter's
PYTHONPATH; if it can, nothing is touched, so the CI invocation keeps importing
the wheel it just built and packaging mistakes still surface here.

Exit status is 0 when every scenario behaved, 1 when any did not.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

MANAGE = """\
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# A scenario that needs an installed-looking app puts it in ./site-packages.
sys.path.insert(0, os.path.join(HERE, "site-packages"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
from django.core.management import execute_from_command_line

execute_from_command_line(sys.argv)
"""

SETTINGS = """\
SECRET_KEY = "smoke"
INSTALLED_APPS = {apps}
DATABASES = {{
    "default": {{"ENGINE": "django.db.backends.sqlite3", "NAME": "smoke.sqlite3"}}
}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
FK_OPTIMIZE = {{"RECORDING_PATH": "recording.jsonl"}}
"""

# Boilerplate every exercise script shares.  Whatever the scenario appends to
# it seeds the database and records, so the report's N is observed rather than
# estimated.
EXERCISE = """\
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "site-packages"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")

import django

django.setup()

from django.core.management import call_command

from django_fk_optimize.recording import record

call_command("migrate", "--run-syncdb", verbosity=0)

"""

ROWS = 200


# ----------------------------------------------------------------------
# writing and running a throwaway project
# ----------------------------------------------------------------------


def write_project(root: Path, apps, files: dict, exercise: str) -> None:
    """Lay down a project: manage.py, settings, some app modules, exercise.py.

    `files` is keyed by path relative to the project root.  Any directory a
    file lands in gets an `__init__.py`, so a scenario lists the modules it
    cares about and nothing else.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "manage.py").write_text(MANAGE, encoding="utf-8")
    (root / "settings.py").write_text(
        SETTINGS.format(apps=json.dumps(list(apps))), encoding="utf-8"
    )
    (root / "exercise.py").write_text(EXERCISE + exercise, encoding="utf-8")
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        init = path.parent / "__init__.py"
        if path.parent != root and not init.exists():
            init.write_text("", encoding="utf-8")
        path.write_text(text, encoding="utf-8")


def interpreter_env(python: str, cwd: Path):
    """The environment the throwaway projects run under.

    Untouched when `python` can already import the package -- that is the CI
    case, where the point is that the wheel is what gets imported.  When it
    cannot, this is a source tree with no install, and the repository root goes
    on PYTHONPATH so a developer can run the script without installing first.

    Asked with the project directory as the working directory, because that is
    where the subprocesses will ask it.
    """
    probe = subprocess.run(
        [python, "-c", "import django_fk_optimize"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        return None
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO), existing] if existing else [str(REPO)]
    )
    return env


class Runner:
    """One interpreter, one project directory, the two commands worth running."""

    def __init__(self, python: str, root: Path, env):
        self.python = python
        self.root = root
        self.env = env

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.python, *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )

    def exercise(self) -> None:
        """Rebuild the recording from whatever the views currently say."""
        (self.root / "recording.jsonl").unlink(missing_ok=True)
        done = self.run("exercise.py")
        if done.returncode != 0:
            raise SystemExit(
                f"could not exercise {self.root}:\n{done.stdout}\n{done.stderr}"
            )

    def judge(self, *args: str) -> str:
        """`manage.py fk_optimize`, with its exit code stitched into the text.

        One string per run, because every assertion below is about what a
        person reads and the exit code is part of that.
        """
        done = self.run("manage.py", "fk_optimize", *args)
        self.exit_code = done.returncode
        return done.stdout + done.stderr

    def report(self, *args: str) -> str:
        self.exercise()
        return self.judge(*args)


# ----------------------------------------------------------------------
# reading the report back
# ----------------------------------------------------------------------


def row(output: str, label: str) -> str:
    """The text of one labelled line of a report block, or ''."""
    match = re.search(rf"^  {label} +(\S.*)$", output, re.MULTILINE)
    return match.group(1).strip() if match else ""


def number(text: str, pattern: str):
    """The one number `pattern` captures in `text`, or None."""
    match = re.search(pattern, text)
    return int(match.group(1)) if match else None


def block_numbers(output: str) -> dict:
    """The three figures a finding has to agree with itself about.

    `rows` is N for the call site, `extra` is the queries the loop costs beyond
    the first, and `saved` is what the fix gives back.  They are printed three
    lines apart by three different code paths, which is exactly why they are
    worth comparing.
    """
    return {
        "rows": number(row(output, "rows"), r"^(\d+)\b"),
        "extra": number(row(output, "current"), r"^1 \+ (\d+) quer"),
        "saved": number(row(output, "saving"), r"(\d+) fewer queries"),
    }


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}")
    if not condition and detail:
        print(detail)
    return condition


# ----------------------------------------------------------------------
# the app every scenario but the chain uses
# ----------------------------------------------------------------------

SHOP_MODELS = """\
from django.db import models


class Publisher(models.Model):
    name = models.CharField(max_length=100)


class Book(models.Model):
    title = models.CharField(max_length=200)
    publisher = models.ForeignKey(Publisher, on_delete=models.CASCADE)
"""

# The deliberate N+1: one query for the books, then one per book for its
# publisher.
BROKEN_VIEW = """\
from .models import Book


def catalogue():
    lines = []
    for book in Book.objects.all():
        lines.append(f"{book.title} / {book.publisher.name}")
    return lines
"""

# The fix the report prints, applied verbatim.
FIXED_VIEW = BROKEN_VIEW.replace(
    "Book.objects.all()", 'Book.objects.all().select_related("publisher")'
)

SHOP_SEED = f"""\
from shop.models import Book, Publisher

Book.objects.all().delete()
Publisher.objects.all().delete()
publishers = [Publisher.objects.create(name=f"publisher-{{i}}") for i in range(5)]
for index in range({ROWS}):
    Book.objects.create(title=f"book-{{index}}", publisher=publishers[index % 5])

"""

RELATION = "shop.Book.publisher"
# Which of the two hints wins is a stopwatch's decision, and a smoke test that
# asserts the outcome of a race is a smoke test that fails for no reason. What
# has to be true is that the fix is a join on the right relation.
FIX = re.compile(r'\.(select|prefetch)_related\("publisher"\)')


# ----------------------------------------------------------------------
# scenarios
# ----------------------------------------------------------------------


def basic(runner: Runner) -> bool:
    """The textbook N+1, found and then made to go away."""
    passed = True
    print("  the code with the N+1:")
    write_project(
        runner.root,
        ["django_fk_optimize", "shop"],
        {"shop/models.py": SHOP_MODELS, "shop/views.py": BROKEN_VIEW},
        SHOP_SEED
        + "from shop.views import catalogue\n\n"
        + 'with record("recording.jsonl"):\n    catalogue()\n',
    )
    output = runner.report("shop", "--fail-on-findings")
    passed &= check(
        "exits 1", runner.exit_code == 1, f"exit {runner.exit_code}\n{output}"
    )
    passed &= check(f"names {RELATION}", RELATION in output, output)
    passed &= check("suggests a join on publisher", bool(FIX.search(output)), output)
    passed &= check("says the rows were observed", "(observed)" in output, output)

    print("  the same code with the fix applied:")
    (runner.root / "shop" / "views.py").write_text(FIXED_VIEW, encoding="utf-8")
    output = runner.report("shop", "--fail-on-findings")
    passed &= check(
        "exits 0", runner.exit_code == 0, f"exit {runner.exit_code}\n{output}"
    )
    passed &= check(
        "says there is no change worth making",
        "no change worth making" in output,
        output,
    )
    return passed


def invocations(runner: Runner) -> bool:
    """N is per call.  Three calls must not read as three times the rows."""
    write_project(
        runner.root,
        ["django_fk_optimize", "shop"],
        {"shop/models.py": SHOP_MODELS, "shop/views.py": BROKEN_VIEW},
        SHOP_SEED
        + "from shop.views import catalogue\n\n"
        + "for _ in range(3):\n"
        + '    with record("recording.jsonl"):\n'
        + "        catalogue()\n",
    )
    output = runner.report("shop", "--fail-on-findings")
    figures = block_numbers(output)
    passed = check(
        "exits 1", runner.exit_code == 1, f"exit {runner.exit_code}\n{output}"
    )
    passed &= check("knows it saw three calls", "median of 3 calls" in output, output)
    passed &= check(
        f"reports rows {ROWS} rather than {ROWS * 3}",
        figures["rows"] == ROWS,
        f"rows: {figures['rows']}\n{output}",
    )
    passed &= check(
        "the queries saved match the queries spent",
        figures["extra"] == figures["saved"] == ROWS,
        f"{figures}\n{output}",
    )
    return passed


CHAIN_MODELS = """\
from django.db import models


class Country(models.Model):
    name = models.CharField(max_length=100)


class Publisher(models.Model):
    name = models.CharField(max_length=100)
    country = models.ForeignKey(Country, on_delete=models.CASCADE)


class Book(models.Model):
    title = models.CharField(max_length=200)
    publisher = models.ForeignKey(Publisher, on_delete=models.CASCADE)
"""

# Two hops off one row, so the loop costs 2N and the fix has to name both.
CHAIN_VIEW = """\
from .models import Book


def catalogue():
    lines = []
    for book in Book.objects.all():
        lines.append(f"{book.title} / {book.publisher.country.name}")
    return lines
"""

CHAIN_SEED = f"""\
from shop.models import Book, Country, Publisher
from shop.views import catalogue

Book.objects.all().delete()
Publisher.objects.all().delete()
Country.objects.all().delete()
countries = [Country.objects.create(name=f"country-{{i}}") for i in range(3)]
publishers = [
    Publisher.objects.create(name=f"publisher-{{i}}", country=countries[i % 3])
    for i in range(5)
]
for index in range({ROWS}):
    Book.objects.create(title=f"book-{{index}}", publisher=publishers[index % 5])

with record("recording.jsonl"):
    catalogue()
"""

CHAIN_FIX = 'select_related("publisher__country")'


def chain(runner: Runner) -> bool:
    """Two hops.  The whole path, and applying it has to clear the finding."""
    write_project(
        runner.root,
        ["django_fk_optimize", "shop"],
        {"shop/models.py": CHAIN_MODELS, "shop/views.py": CHAIN_VIEW},
        CHAIN_SEED,
    )
    output = runner.report("shop", "--fail-on-findings")
    figures = block_numbers(output)
    passed = check(
        "exits 1", runner.exit_code == 1, f"exit {runner.exit_code}\n{output}"
    )
    passed &= check(
        "names the whole path, not the first hop",
        CHAIN_FIX in row(output, "fix"),
        f"fix: {row(output, 'fix')!r}\n{output}",
    )
    passed &= check(
        f"{ROWS} rows, {ROWS * 2} extra queries, {ROWS * 2} saved",
        figures == {"rows": ROWS, "extra": ROWS * 2, "saved": ROWS * 2},
        f"{figures}\n{output}",
    )

    print("  the printed fix, applied verbatim:")
    (runner.root / "shop" / "views.py").write_text(
        CHAIN_VIEW.replace("Book.objects.all()", f"Book.objects.all().{CHAIN_FIX}"),
        encoding="utf-8",
    )
    output = runner.report("shop", "--fail-on-findings")
    passed &= check(
        "exits 0", runner.exit_code == 0, f"exit {runner.exit_code}\n{output}"
    )
    passed &= check(
        "says there is no change worth making",
        "no change worth making" in output,
        output,
    )
    return passed


# Not a Django CBV -- importing one would drag in the whole request machinery
# for no gain -- but the shape of every one of them: the queryset is built by a
# method and iterated through `self`, never named in the loop.
CLASS_VIEW = """\
from .models import Book


class Catalogue:
    def get_queryset(self):
        return Book.objects.all()

    def lines(self):
        rows = []
        for book in self.get_queryset():
            rows.append(f"{book.title} / {book.publisher.name}")
        return rows
"""


def class_bound(runner: Runner) -> bool:
    """A queryset reached through self.  Found, and hinted where it is built."""
    write_project(
        runner.root,
        ["django_fk_optimize", "shop"],
        {"shop/models.py": SHOP_MODELS, "shop/views.py": CLASS_VIEW},
        SHOP_SEED
        + "from shop.views import Catalogue\n\n"
        + 'with record("recording.jsonl"):\n    Catalogue().lines()\n',
    )
    output = runner.report("shop", "--fail-on-findings")
    passed = check(
        "exits 1", runner.exit_code == 1, f"exit {runner.exit_code}\n{output}"
    )
    passed &= check(f"names {RELATION}", RELATION in output, output)
    passed &= check("blames the method that loops", "lines()" in output, output)
    passed &= check(
        "hints the queryset where it is built",
        'self.get_queryset().select_related("publisher")' in row(output, "fix"),
        f"fix: {row(output, 'fix')!r}\n{output}",
    )
    return passed


VENDOR_MODELS = """\
from django.db import models


class Maker(models.Model):
    name = models.CharField(max_length=100)


class Widget(models.Model):
    label = models.CharField(max_length=200)
    maker = models.ForeignKey(Maker, on_delete=models.CASCADE)
"""

VENDOR_VIEW = """\
from .models import Widget


def inventory():
    lines = []
    for widget in Widget.objects.all():
        lines.append(f"{widget.label} / {widget.maker.name}")
    return lines
"""

VENDOR_SEED = f"""\
from shop.models import Book, Publisher
from shop.views import catalogue
from vendor.models import Maker, Widget
from vendor.views import inventory

Book.objects.all().delete()
Publisher.objects.all().delete()
Widget.objects.all().delete()
Maker.objects.all().delete()
publishers = [Publisher.objects.create(name=f"publisher-{{i}}") for i in range(5)]
makers = [Maker.objects.create(name=f"maker-{{i}}") for i in range(5)]
for index in range({ROWS}):
    Book.objects.create(title=f"book-{{index}}", publisher=publishers[index % 5])
    Widget.objects.create(label=f"widget-{{index}}", maker=makers[index % 5])

with record("recording.jsonl"):
    catalogue()
    inventory()
"""

VENDOR_RELATION = "vendor.Widget.maker"


def third_party(runner: Runner) -> bool:
    """Somebody else's release.  Reported only when asked for, and never a gate.

    The app lives in a directory literally named `site-packages`, which is how
    `utils.sources.is_third_party` decides -- from where the code sits, never
    from what it is called.  The project's own app is clean, so the exit code
    is about the vendored N+1 and nothing else.  Both are recorded, so this
    covers the runtime half too: a recorded finding in somebody else's code
    used to trip `--fail-on-findings` with no flag to turn it off.
    """
    write_project(
        runner.root,
        ["django_fk_optimize", "shop", "vendor"],
        {
            "shop/models.py": SHOP_MODELS,
            "shop/views.py": FIXED_VIEW,
            "site-packages/vendor/models.py": VENDOR_MODELS,
            "site-packages/vendor/views.py": VENDOR_VIEW,
        },
        VENDOR_SEED,
    )
    output = runner.report("--fail-on-findings")
    passed = check(
        "silent about the vendored app by default",
        VENDOR_RELATION not in output,
        output,
    )
    passed &= check(
        "does not trip the gate on somebody else's code",
        runner.exit_code == 0,
        f"exit {runner.exit_code}\n{output}",
    )

    print("  with --include-third-party:")
    output = runner.judge("--include-third-party", "--fail-on-findings")
    passed &= check(f"names {VENDOR_RELATION}", VENDOR_RELATION in output, output)
    passed &= check(
        "suggests a join on maker", 'select_related("maker")' in output, output
    )
    passed &= check(
        "exits 1", runner.exit_code == 1, f"exit {runner.exit_code}\n{output}"
    )
    return passed


SCENARIOS = {
    "basic": basic,
    "invocations": invocations,
    "chain": chain,
    "class": class_bound,
    "third-party": third_party,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="interpreter to run the projects with. Default: this one.",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="where to write the throwaway projects. Default: a temp directory.",
    )
    parser.add_argument(
        "--only",
        nargs="*",
        choices=sorted(SCENARIOS),
        help="a subset of scenarios, by name. Default: all of them.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="do not delete the projects afterwards.",
    )
    options = parser.parse_args(argv)

    root = Path(options.workdir or tempfile.mkdtemp(prefix="fk-optimize-smoke-"))
    root.mkdir(parents=True, exist_ok=True)
    print(f"smoke projects: {root}")
    env = interpreter_env(options.python, root)
    print(
        f"interpreter: {options.python} ({'installed' if env is None else 'source tree'})"
    )

    passed = True
    try:
        for name in options.only or list(SCENARIOS):
            print(f"{name}:")
            scenario = SCENARIOS[name]
            passed &= scenario(Runner(options.python, root / name, env))
    finally:
        if not options.keep:
            shutil.rmtree(root, ignore_errors=True)

    print("smoke passed" if passed else "smoke failed")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
