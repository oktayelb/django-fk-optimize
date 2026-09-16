"""End-to-end check: does the installed package actually find an N+1?

The unit suite imports the package from the source tree and calls its pieces
directly.  This does neither.  It writes a throwaway Django project on disk,
runs a view that loads a foreign key one row at a time, records it, and then
shells out to `manage.py fk_optimize --fail-on-findings` and looks at the exit
code -- 1, with the relation named in the report.  It then applies the fix the
report printed and runs it again, expecting 0.

That is the whole product: scan, record, judge, and a CI gate that opens once
the code is fixed.  Run it against a wheel installed into a clean virtualenv
and packaging regressions show up here too.

    python scripts/smoke.py             # uses this interpreter
    python scripts/smoke.py --keep      # leave the project on disk to poke at

Exit status is 0 when both halves behaved, 1 when either did not.
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MANAGE = """\
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
from django.core.management import execute_from_command_line

execute_from_command_line(sys.argv)
"""

SETTINGS = """\
SECRET_KEY = "smoke"
INSTALLED_APPS = ["django_fk_optimize", "shop"]
DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "smoke.sqlite3"}
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
FK_OPTIMIZE = {"RECORDING_PATH": "recording.jsonl"}
"""

MODELS = """\
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

# Seeds the database and produces a recording, so the report's N is observed
# rather than estimated.
EXERCISE = """\
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")

import django

django.setup()

from django.core.management import call_command

from django_fk_optimize.recording import record
from shop.models import Book, Publisher
from shop.views import catalogue

call_command("migrate", "--run-syncdb", verbosity=0)
Book.objects.all().delete()
Publisher.objects.all().delete()
publishers = [Publisher.objects.create(name=f"publisher-{i}") for i in range(5)]
for index in range(60):
    Book.objects.create(title=f"book-{index}", publisher=publishers[index % 5])

with record("recording.jsonl"):
    catalogue()
"""

RELATION = "shop.Book.publisher"
FIX = 'select_related("publisher")'


def write_project(root: Path, view: str) -> None:
    (root / "shop").mkdir(parents=True, exist_ok=True)
    (root / "manage.py").write_text(MANAGE, encoding="utf-8")
    (root / "settings.py").write_text(SETTINGS, encoding="utf-8")
    (root / "exercise.py").write_text(EXERCISE, encoding="utf-8")
    (root / "shop" / "__init__.py").write_text("", encoding="utf-8")
    (root / "shop" / "models.py").write_text(MODELS, encoding="utf-8")
    (root / "shop" / "views.py").write_text(view, encoding="utf-8")


def run(python: str, root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [python, *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def report(root: Path, python: str) -> subprocess.CompletedProcess:
    """Re-record, then judge. The recording is rebuilt from the current view."""
    (root / "recording.jsonl").unlink(missing_ok=True)
    exercised = run(python, root, "exercise.py")
    if exercised.returncode != 0:
        raise SystemExit(
            f"could not exercise the project:\n{exercised.stdout}\n{exercised.stderr}"
        )
    return run(python, root, "manage.py", "fk_optimize", "shop", "--fail-on-findings")


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}")
    if not condition and detail:
        print(detail)
    return condition


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="interpreter to run the project with. Default: this one.",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="where to write the throwaway project. Default: a temp directory.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="do not delete the project afterwards.",
    )
    options = parser.parse_args(argv)

    root = Path(options.workdir or tempfile.mkdtemp(prefix="fk-optimize-smoke-"))
    root.mkdir(parents=True, exist_ok=True)
    print(f"smoke project: {root}")

    passed = True
    try:
        print("the code with the N+1:")
        write_project(root, BROKEN_VIEW)
        before = report(root, options.python)
        output = before.stdout + before.stderr
        passed &= check(
            "exits 1", before.returncode == 1, f"exit {before.returncode}\n{output}"
        )
        passed &= check(f"names {RELATION}", RELATION in output, output)
        passed &= check(f"suggests {FIX}", FIX in output, output)
        passed &= check("says the rows were observed", "(observed)" in output, output)

        print("the same code with the fix applied:")
        write_project(root, FIXED_VIEW)
        after = report(root, options.python)
        output = after.stdout + after.stderr
        passed &= check(
            "exits 0", after.returncode == 0, f"exit {after.returncode}\n{output}"
        )
        passed &= check(
            "says there is no change worth making",
            "no change worth making" in output,
            output,
        )
    finally:
        if not options.keep:
            shutil.rmtree(root, ignore_errors=True)

    print("smoke passed" if passed else "smoke failed")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
