#!/usr/bin/env python3
"""Run the static analysis over real Django projects nobody wrote for us.

The test suite is written by the same people as the code, so it encodes the
same assumptions. This does not: it clones well-known Django projects and
points the scanner at them, which is how shapes nobody anticipated get found.

No database, no settings, no `pip install` of the project. The vocabulary is
read out of source (`utils.static_vocabulary`), so a run costs a shallow clone
and a few seconds of parsing, and any project can be added by one line.

What it enforces, from `corpus-baseline.json`:

* nothing raises -- an unhandled exception on any project fails the run,
  while a clone that never completed is retried, then reported as
  unavailable and passed over: an outage at a git host says nothing about
  this scanner, and the run fails on that only if every project was lost;
* parse failures stay at or below the recorded allowance, and are printed with
  the parser's own message beside them: a file that will not parse is as often
  newer than the interpreter reading it as it is broken, and the count alone
  cannot tell those apart -- the run prints its python version for that reason;
* models, call sites and followed expressions stay at or above the recorded
  floors, so a scanner that quietly stops resolving anything cannot pass;
* the census adds up -- every manager expression the scanner met is either
  followed, terminal or not followed, and a project where those three do not
  sum to `seen` is a bug in the bookkeeping, not a shortfall in coverage.

Floors rather than exact counts, because these projects keep moving and a
pinned expectation would fail for their reasons rather than ours.

Coverage is printed as that three-way split and never failed on. It used to be
printed as `sites / (sites + unresolved)`, a denominator made of the cases the
scanner happened to understand, which told django-machina it had 97.7% coverage
of a project where four expressions out of a hundred and ninety resolved. A
shape nobody has taught the scanner yet is supposed to make the number worse,
so the denominator is now everything seen.

The low figures are not all scanner failures. Much of django-machina's
unresolved count is its dynamically generated models, which the vocabulary
`from_tree` reads out of source cannot see at all -- a limit of this script's
AST-only vocabulary, not of the scanner, which would resolve them perfectly
well given the app registry. Reading the registry needs the project installed
and configured, which is what `scripts/live.py` does for a couple of projects
and what this deliberately does not do for eight.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASELINE = Path(__file__).resolve().parent / "corpus-baseline.json"

# Chosen for variety rather than fame: a shop, a CMS, an auth library, a
# documentation host, a forum and a helpdesk exercise different modelling
# habits, and between them they cover most of the ways people write Django.
PROJECTS = {
    "django-oscar": "https://github.com/django-oscar/django-oscar.git",
    "wagtail": "https://github.com/wagtail/wagtail.git",
    "django-cms": "https://github.com/django-cms/django-cms.git",
    "django-allauth": "https://github.com/pennersr/django-allauth.git",
    "readthedocs": "https://github.com/readthedocs/readthedocs.org.git",
    "django-helpdesk": "https://github.com/django-helpdesk/django-helpdesk.git",
    "misago": "https://github.com/rafalp/Misago.git",
    "django-machina": "https://github.com/ellmetha/django-machina.git",
}


class Unavailable(RuntimeError):
    """A clone that never happened, so the project never said anything.

    Kept apart from a crash on purpose. A git host answering 503 is not a
    regression in a scanner, and a job that goes red for it is a job people
    learn to scroll past -- including on the day it is right.
    """


# Three attempts: two is indistinguishable from bad luck, and a fourth costs a
# minute of runner time to learn what the third already said.
ATTEMPTS = 3
BACKOFF = (5, 20)


def clone(name: str, url: str, into: Path) -> Path:
    target = into / name
    if target.exists():
        return target
    command = [
        "git",
        "clone",
        "--depth",
        "1",
        "--filter=blob:none",
        "--quiet",
        url,
        str(target),
    ]
    last = "no attempt was made"
    for attempt in range(ATTEMPTS):
        done = subprocess.run(command, capture_output=True, text=True, timeout=600)
        if done.returncode == 0:
            return target
        last = f"exited {done.returncode}\n{done.stderr.strip()}"
        # A half-finished clone leaves behind a directory that looks finished
        # to the next caller, so each retry starts from nothing.
        shutil.rmtree(target, ignore_errors=True)
        if attempt + 1 < ATTEMPTS:
            pause = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            print(f"   cloning {name} failed, retrying in {pause}s")
            time.sleep(pause)
    raise Unavailable(f"cloning {name} gave up after {ATTEMPTS} attempts\n{last}")


def revision(path: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def where(errors, root: Path, limit: int = 8) -> list[str]:
    """`path: message` for each file that would not parse.

    The count on its own sends the next person off to clone the project and
    reproduce the run before they can even see what broke.  The parser's own
    message is usually the whole answer, and frequently says that this
    interpreter is older than the code it was handed rather than that the code
    is wrong -- a distinction no number can carry.
    """
    lines = []
    for place, message in errors[:limit]:
        try:
            place = str(Path(place).relative_to(root))
        except ValueError:
            pass
        lines.append(f"{place}: {message}")
    if len(errors) > limit:
        lines.append(f"... and {len(errors) - limit} more")
    return lines


def analyse(path: Path) -> dict:
    """Everything the scanner can say about one project, without running it."""
    from django_fk_optimize.utils.callsites import PROBABLE, RESOLVED, scan_files
    from django_fk_optimize.utils.sources import python_files
    from django_fk_optimize.utils.static_vocabulary import from_tree

    started = time.perf_counter()
    vocabulary, stats = from_tree(path)

    # `scan_files` rather than a loop of `scan_file`: the accumulation of the
    # census across files is the product's own, so this measures it instead of
    # reimplementing it and agreeing with itself.
    scanned = scan_files(python_files(path), vocabulary)
    sites = scanned.sites

    touched = sum(len(site.touched) for site in sites)
    return {
        "revision": revision(path),
        "seconds": round(time.perf_counter() - started, 1),
        "model_files": stats.files,
        "models": len(vocabulary),
        "relations": stats.relations,
        "unresolved_targets": len(stats.unresolved_targets),
        "model_parse_errors": len(stats.errors),
        "model_parse_error_detail": where(stats.errors, path),
        "files": scanned.files,
        "parse_errors": len(scanned.errors),
        "parse_error_detail": where(scanned.errors, path),
        "sites": len(sites),
        "sites_resolved": sum(1 for s in sites if s.confidence == RESOLVED),
        "sites_probable": sum(1 for s in sites if s.confidence == PROBABLE),
        # The census: every manager-rooted expression lands in exactly one of
        # the last three, and `sites_seen` is all of them.
        "sites_seen": scanned.seen,
        "sites_attributed": scanned.attributed,
        "sites_terminal": scanned.terminal,
        "sites_unresolved": len(scanned.unresolved),
        "touches": touched,
        "with_hints": sum(1 for s in sites if s.hints),
        "missing_hints": sum(1 for s in sites if s.missing),
        "unused_hints": sum(1 for s in sites if s.unused),
        "free_managers": sum(len(s.free) for s in sites),
        "bypassed": sum(len(s.bypassed) for s in sites),
    }


def check(name: str, result: dict, floors: dict) -> list[str]:
    problems = []
    for key in ("models", "sites", "sites_attributed"):
        floor = floors.get(key)
        if floor is not None and result[key] < floor:
            problems.append(f"{name}: {key} fell to {result[key]}, floor is {floor}")
    for key in ("parse_errors", "model_parse_errors"):
        allowed = floors.get(key, 0)
        if result[key] > allowed:
            problem = f"{name}: {key} rose to {result[key]}, allowance is {allowed}"
            for line in result[f"{key.removesuffix('s')}_detail"]:
                problem += f"\n    {line}"
            problems.append(problem)
    # Not a floor and not a matter of degree. The three buckets partition the
    # expressions the scanner met, so if they do not sum to `seen` the census
    # is miscounting and every coverage figure taken from it is fiction.
    parts = result["sites_attributed"] + result["sites_terminal"]
    parts += result["sites_unresolved"]
    if parts != result["sites_seen"]:
        problems.append(
            f"{name}: census does not add up -- {result['sites_seen']} seen but "
            f"{result['sites_attributed']} + {result['sites_terminal']} + "
            f"{result['sites_unresolved']} = {parts}"
        )
    return problems


def census(result: dict) -> str:
    """The three-way split, as a share of everything the scanner met."""
    seen = result["sites_seen"]
    share = f"{result['sites_attributed'] / seen:.1%}" if seen else "n/a"
    return (
        f"{result['sites_attributed']}/{seen} expressions followed ({share}), "
        f"{result['sites_terminal']} terminal, "
        f"{result['sites_unresolved']} not followed"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", default=None, help="where to keep clones")
    parser.add_argument("--only", nargs="*", help="a subset of project names")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="record the current numbers as the new floors",
    )
    parser.add_argument("--json", default=None, help="write the full report here")
    options = parser.parse_args(argv)

    root = Path(options.workdir or tempfile.mkdtemp(prefix="fk-corpus-"))
    root.mkdir(parents=True, exist_ok=True)
    names = options.only or list(PROJECTS)

    # Printed because it is half of every parse failure below: these projects
    # are read with whatever grammar this interpreter knows, and they adopt new
    # syntax on their own schedule, not ours.
    print(f"python {sys.version.split()[0]}")

    baseline = {}
    if BASELINE.exists():
        baseline = json.loads(BASELINE.read_text())

    results, problems, crashed, unavailable = {}, [], [], []
    for name in names:
        url = PROJECTS.get(name)
        if url is None:
            problems.append(f"{name}: not a known project")
            continue
        print(f"-- {name}")
        try:
            path = clone(name, url, root)
            results[name] = analyse(path)
        except Unavailable as exc:
            # No traceback: the stack of a 503 is noise, and printing one makes
            # an outage read like the crash this job exists to catch.
            unavailable.append(name)
            print(f"   unavailable: {exc}".replace("\n", "\n   "))
            continue
        except Exception:
            # A crash is the one thing this job exists to catch, so it is
            # reported in full and never swallowed.
            crashed.append(name)
            print(traceback.format_exc())
            continue

        result = results[name]
        print(
            f"   {result['models']:>5} models  {result['sites']:>5} sites  "
            f"({result['sites_resolved']} resolved, {result['sites_probable']} probable)  "
            f"{result['parse_errors']} parse errors  {result['seconds']}s"
        )
        print(f"   {census(result)}")
        problems.extend(check(name, result, baseline.get(name, {})))

    if options.write_baseline:
        floors = {
            name: {
                # A little headroom: these projects keep moving, and a floor
                # set to today's exact number fails for their reasons.
                "models": int(r["models"] * 0.8),
                "sites": int(r["sites"] * 0.8),
                "sites_attributed": int(r["sites_attributed"] * 0.8),
                "parse_errors": r["parse_errors"],
                "model_parse_errors": r["model_parse_errors"],
            }
            for name, r in results.items()
        }
        BASELINE.write_text(json.dumps(floors, indent=2, sort_keys=True) + "\n")
        print(f"\nbaseline written to {BASELINE}")

    if options.json:
        Path(options.json).write_text(json.dumps(results, indent=2, sort_keys=True))

    total_sites = sum(r["sites"] for r in results.values())
    print(f"\n{len(results)} projects, {total_sites} call sites")
    if results:
        totals = {
            key: sum(r[key] for r in results.values())
            for key in (
                "sites_seen",
                "sites_attributed",
                "sites_terminal",
                "sites_unresolved",
            )
        }
        print(f"coverage: {census(totals)}")

    if unavailable:
        print(f"UNAVAILABLE, not a regression: {', '.join(unavailable)}")
    if crashed:
        print(f"\nCRASHED: {', '.join(crashed)}")
    for problem in problems:
        print(f"REGRESSION: {problem}")

    # Seven projects still say plenty; none of them says nothing, and a green
    # tick on a run that scanned nothing is worse than a red one.
    if not results and unavailable:
        print("nothing ran: every project was unavailable")
        return 1
    return 1 if crashed or problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
