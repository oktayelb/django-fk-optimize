#!/usr/bin/env python3
"""Run the static analysis over real Django projects nobody wrote for us.

The test suite is written by the same people as the code, so it encodes the
same assumptions. This does not: it clones well-known Django projects and
points the scanner at them, which is how shapes nobody anticipated get found.

No database, no settings, no `pip install` of the project. The vocabulary is
read out of source (`utils.static_vocabulary`), so a run costs a shallow clone
and a few seconds of parsing, and any project can be added by one line.

What it enforces, from `corpus-baseline.json`:

* nothing raises -- an unhandled exception on any project fails the run;
* parse failures stay at or below the recorded allowance;
* models and call sites stay at or above the recorded floors, so a scanner
  that quietly stops resolving anything cannot pass.

Floors rather than exact counts, because these projects keep moving and a
pinned expectation would fail for their reasons rather than ours.

`sites_unresolved` is reported and never failed on. It is the honest coverage
metric: a queryset the scanner could not follow is a case we do not handle
yet, and the number going up on a new project is a backlog item, not a
regression.
"""

from __future__ import annotations

import argparse
import json
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


def clone(name: str, url: str, into: Path) -> Path:
    target = into / name
    if target.exists():
        return target
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--filter=blob:none",
            "--quiet",
            url,
            str(target),
        ],
        check=True,
        timeout=600,
    )
    return target


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


def analyse(path: Path) -> dict:
    """Everything the scanner can say about one project, without running it."""
    from django_fk_optimize.utils.callsites import PROBABLE, RESOLVED, scan_file
    from django_fk_optimize.utils.sources import python_files
    from django_fk_optimize.utils.static_vocabulary import from_tree

    started = time.perf_counter()
    vocabulary, stats = from_tree(path)

    sites, unresolved, errors, files = [], 0, 0, 0
    for source in python_files(path):
        files += 1
        report = scan_file(source, vocabulary)
        sites.extend(report.sites)
        unresolved += len(report.unresolved)
        errors += len(report.errors)

    touched = sum(len(site.touched) for site in sites)
    return {
        "revision": revision(path),
        "seconds": round(time.perf_counter() - started, 1),
        "model_files": stats.files,
        "models": len(vocabulary),
        "relations": stats.relations,
        "unresolved_targets": len(stats.unresolved_targets),
        "model_parse_errors": len(stats.errors),
        "files": files,
        "parse_errors": errors,
        "sites": len(sites),
        "sites_resolved": sum(1 for s in sites if s.confidence == RESOLVED),
        "sites_probable": sum(1 for s in sites if s.confidence == PROBABLE),
        "sites_unresolved": unresolved,
        "touches": touched,
        "with_hints": sum(1 for s in sites if s.hints),
        "missing_hints": sum(1 for s in sites if s.missing),
        "unused_hints": sum(1 for s in sites if s.unused),
        "free_managers": sum(len(s.free) for s in sites),
        "bypassed": sum(len(s.bypassed) for s in sites),
    }


def check(name: str, result: dict, floors: dict) -> list[str]:
    problems = []
    for key in ("models", "sites"):
        floor = floors.get(key)
        if floor is not None and result[key] < floor:
            problems.append(f"{name}: {key} fell to {result[key]}, floor is {floor}")
    for key in ("parse_errors", "model_parse_errors"):
        allowed = floors.get(key, 0)
        if result[key] > allowed:
            problems.append(
                f"{name}: {key} rose to {result[key]}, allowance is {allowed}"
            )
    return problems


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

    baseline = {}
    if BASELINE.exists():
        baseline = json.loads(BASELINE.read_text())

    results, problems, crashed = {}, [], []
    for name in names:
        url = PROJECTS.get(name)
        if url is None:
            problems.append(f"{name}: not a known project")
            continue
        print(f"-- {name}")
        try:
            path = clone(name, url, root)
            results[name] = analyse(path)
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
            f"{result['sites_unresolved']:>4} not followed  "
            f"{result['parse_errors']} parse errors  {result['seconds']}s"
        )
        problems.extend(check(name, result, baseline.get(name, {})))

    if options.write_baseline:
        floors = {
            name: {
                # A little headroom: these projects keep moving, and a floor
                # set to today's exact number fails for their reasons.
                "models": int(r["models"] * 0.8),
                "sites": int(r["sites"] * 0.8),
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
    total_unresolved = sum(r["sites_unresolved"] for r in results.values())
    print(
        f"\n{len(results)} projects, {total_sites} call sites, "
        f"{total_unresolved} querysets not followed"
    )
    if total_sites:
        print(
            f"coverage: {1 - total_unresolved / (total_sites + total_unresolved):.1%}"
        )

    if crashed:
        print(f"\nCRASHED: {', '.join(crashed)}")
    for problem in problems:
        print(f"REGRESSION: {problem}")
    return 1 if crashed or problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
