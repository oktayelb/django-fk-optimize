"""Render verdicts: as the block a person reads, and as the object CI reads.

Two rules shape both renderings.

**Say where every number came from.**  A row count is `observed`, `static
bound` or `estimated`; a duration is `recorded` (what the application really
spent) or `measured` (what this command just spent reproducing it).  The
labels are not decoration.  An estimate printed as a measurement is how a
profiler talks somebody into rewriting a query that was never slow.

**Say what was not seen.**  The coverage block is not optional and is not a
footnote.  Files that failed to parse, call sites that would not resolve,
malformed recording lines and the age of the recording all bound how much the
findings above are worth, and a report that omits them reads as a clean bill
of health for exactly the code the tool failed on.

Nothing here imports Django.  Styling is returned as a name per line and the
command maps it onto `self.style`, so the renderer stays a pure function of
its inputs and can be asserted on directly.
"""

from __future__ import annotations

import json
import os
import textwrap
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .verdicts import ACTIONABLE_KINDS, MEASURED, OBSERVED, REMOVE_HINT, Verdict

SCHEMA_VERSION = 1

PLAIN = ""
HEADING = "heading"
SUCCESS = "success"
WARNING = "warning"
NOTICE = "notice"

LABEL = 12  # width of the "rows" / "current" / "best" column

HOW_TO_RECORD = (
    "no recording: N was estimated from row counts. For observed numbers, add "
    '"django_fk_optimize.middleware.FkOptimizeMiddleware" to MIDDLEWARE and '
    "exercise the pages you care about, or wrap a script in "
    "django_fk_optimize.recording.record()."
)


@dataclass
class Coverage:
    """What the run could see, and what it could not.

    Printed under every report and carried in every `--json` payload.
    """

    files: int = 0
    sites: int = 0
    sites_resolved: int = 0
    sites_probable: int = 0
    sites_unresolved: int = 0
    scan_errors: int = 0

    # The census the scanner takes of every manager-rooted expression it meets:
    # `seen` is all of them, and each one lands in exactly one of `attributed`
    # (it produced a call site), `sites_terminal` (it ends in values()/count()/
    # create()/... and there is nothing to hint) and `sites_unresolved`.
    #
    # Printed as the three-way split rather than as a percentage of the part
    # that worked.  A denominator that leaves out what the scanner missed
    # answers "of the expressions I understood, how many did I understand?" --
    # which is how a project where four expressions out of a hundred and ninety
    # resolved got told its coverage was 97.7%.
    #
    # Defaulted, so the command can populate them in its own time and this
    # block stays correct meanwhile: nobody took a census is a different state
    # from a census that came back empty, and zero `seen` is read as the first.
    sites_seen: int = 0
    sites_terminal: int = 0
    sites_attributed: int = 0

    recording_path: str = ""
    recording_exists: bool = False
    records: int = 0
    malformed: int = 0
    groups: int = 0
    # How many distinct runs the recording holds.  Every per-call number in the
    # report above is a median over these, and one invocation is one anecdote.
    recording_invocations: int = 0
    recording_age_seconds: float | None = None

    findings_matched: int = 0
    findings_runtime_only: int = 0
    findings_unattributed: int = 0

    models: int = 0
    relations_benchmarked: int = 0
    relations_skipped: int = 0
    benchmarked: bool = True
    scanned: bool = True
    timed_out: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------
# small formatters
# ----------------------------------------------------------------------


def milliseconds(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    if seconds >= 1.0:
        return f"{seconds:.2f} s"
    return f"{seconds * 1000:.1f} ms"


def plural(count: int, singular: str, many: str = "") -> str:
    """`1 file`, `2 files`. A count is not an excuse for `1 relations`."""
    if count == 1:
        return f"{count} {singular}"
    return f"{count} {many or singular + 's'}"


def queries(count: int | None) -> str:
    if count is None:
        return "n/a"
    return plural(count, "query", "queries")


def age(seconds: float | None) -> str:
    if seconds is None:
        return "unknown age"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            return f"{plural(int(seconds // size), unit)} old"
    return f"{int(seconds)}s old"


def relative(path: str) -> str:
    """A path as it would be typed, when it is under the working directory.

    An absolute path is correct and unreadable; `alarms/views.py:80` is what a
    reader can paste into an editor. Anything outside the tree stays absolute,
    because a string of `../..` is worse than either.
    """
    if not path:
        return path
    try:
        short = os.path.relpath(path)
    except (OSError, ValueError):
        return path
    return path if short.startswith("..") else short


def pad(text: str, width: int) -> str:
    """Left-justify, but never let two columns run into each other."""
    return text if len(text) >= width else text.ljust(width)


def _row(label: str, text: str) -> tuple[str, str]:
    return (f"  {label:<{LABEL}}{text}", PLAIN)


# ----------------------------------------------------------------------
# text
# ----------------------------------------------------------------------


def _header(verdict: Verdict) -> str:
    where = (
        f"{relative(verdict.file)}:{verdict.line}" if verdict.file else "(unattributed)"
    )
    if verdict.function:
        where = f"{where}  {verdict.function}()"
    return f"{pad(where, 48)}  {verdict.target or verdict.model}"


def _current(verdict: Verdict) -> str:
    if verdict.observed_queries is not None:
        shape = f"1 + {verdict.observed_queries} queries"
    elif verdict.current is not None:
        shape = queries(verdict.current.queries)
    elif verdict.rows.known:
        shape = f"1 + {verdict.rows.n} queries ({verdict.rows.provenance})"
    else:
        shape = "unknown"
    timings = []
    if verdict.observed_seconds is not None:
        # The recorder times the execute() call, so this is database time; the
        # benchmark times the loop, so that one includes the ORM as well.
        timings.append(f"{milliseconds(verdict.observed_seconds)} recorded sql")
    if verdict.current is not None:
        timings.append(f"{milliseconds(verdict.current.seconds)} measured")
    return f"{shape:<20}{' / '.join(timings)}".rstrip()


def _strategy(name: str, measurement, basis: str = MEASURED) -> str:
    # The duration is printed either way -- it was really taken -- but only
    # the word "measured" claims it decided anything.
    how = "measured" if basis == MEASURED else "not decisive"
    return (
        f"{name:<18}{queries(measurement.queries):<12}"
        f"{milliseconds(measurement.seconds)} {how}"
    )


def _saving(verdict: Verdict) -> str:
    # Two axes, named separately: which call site this is, and how much
    # the number beside it is worth.
    confidence = f"confidence: {verdict.confidence} · evidence: {verdict.evidence}"
    if verdict.kind == REMOVE_HINT:
        saved = "one join per call, not timed"
    elif verdict.measured and verdict.saved_seconds is not None:
        share = verdict.saved_share
        percent = f" ({share:.0%})" if share is not None else ""
        saved = (
            f"~{milliseconds(verdict.saved_seconds)} per call{percent}, "
            f"{verdict.saved_queries} fewer queries"
        )
    elif verdict.basis != MEASURED and verdict.best is not None:
        saved = f"{queries(verdict.best.queries)} once fixed, too few rows to time"
    elif verdict.rows.known and verdict.rows.n > 0:
        saved = f"{verdict.rows.n} queries per call, not timed"
    else:
        saved = "not timed"
    return f"{saved:<48}  {confidence}"


def _over(invocations: int) -> str:
    """How many invocations an observed N was taken over.

    Said out loud, because N is a median and a median of one is a single page
    load that happened to be recorded.  Printing "412 (observed)" for that and
    for the same figure seen on thirty-seven requests hides the difference
    between an anecdote and a pattern.  Zero means the number came from
    somewhere that does not count invocations, and claims nothing.
    """
    if invocations <= 0:
        return ""
    if invocations == 1:
        return "one call only"
    return f"median of {invocations} calls"


def _block(verdict: Verdict) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = [("", PLAIN), (_header(verdict), HEADING)]
    if verdict.rows.known:
        # Only an observed N was taken over invocations; an estimate is a count
        # of table rows and has nothing to average.
        over = (
            _over(verdict.observed_invocations)
            if verdict.rows.provenance == OBSERVED
            else ""
        )
        # The provenance stays in its own parentheses and the reach follows it,
        # rather than being folded in: "(observed)" is the phrase the rest of
        # the world greps this report for.
        rows = f"{verdict.rows.n}  ({verdict.rows.provenance})"
        lines.append(_row("rows", f"{rows}  {over}".rstrip()))
    lines.append(_row("what", verdict.headline))
    current = _current(verdict)
    if current != "unknown":
        lines.append(_row("current", current))
    if verdict.best is not None:
        lines.append(
            _row(
                "best",
                _strategy(verdict.best_strategy, verdict.best, verdict.basis),
            )
        )
    if verdict.alternative is not None:
        lines.append(
            _row(
                "alternative",
                _strategy(
                    verdict.alternative_strategy,
                    verdict.alternative,
                    verdict.basis,
                ),
            )
        )
    if verdict.runtime_only:
        lines.append(_row("seen in", f"{verdict.source} (no call site to match)"))
    if verdict.candidates:
        lines.append(_row("candidates", ", ".join(verdict.candidates)))
    if verdict.fix:
        label = "fix" if verdict.actionable else "possible fix"
        lines.append(_row(label, verdict.fix))
    lines.append((f"  {'saving':<{LABEL}}{_saving(verdict)}", PLAIN))
    for note in verdict.notes:
        lines.append(_row("note", note))
    return lines


def _one_liner(verdict: Verdict) -> tuple[str, str]:
    where = (
        f"{relative(verdict.file)}:{verdict.line}" if verdict.file else "(unattributed)"
    )
    return (f"  {pad(where, 34)}  {pad(verdict.target, 26)}  {verdict.headline}", PLAIN)


def _census(coverage: Coverage) -> list[tuple[str, str]]:
    """What the scanner met, and what it did with each of it.

    The three-way split is the honest shape of this number.  "2 querysets the
    scanner could not follow" is true and says nothing, because it never says
    two out of what; a reader takes it for a rounding error next to a report
    full of findings, and the hundred and eighty-five expressions nobody has
    taught the scanner yet go unmentioned.  Seen against `seen`, the same two
    are either a rounding error or the whole story, and the reader can tell
    which.

    Falls back to the old line while `seen` is zero, which means the caller has
    not counted rather than that there was nothing to count.
    """
    if not coverage.sites_seen:
        return [
            _row(
                "not seen",
                f"{plural(coverage.sites_unresolved, 'queryset')} "
                "the scanner could not follow, "
                f"{plural(coverage.scan_errors, 'file')} it could not parse",
            )
        ]

    lines = [
        _row(
            "querysets",
            f"{plural(coverage.sites_seen, 'expression')} seen: "
            f"{coverage.sites_attributed} followed, "
            f"{coverage.sites_terminal} terminal, "
            f"{coverage.sites_unresolved} not followed",
        ),
        _row("not seen", f"{plural(coverage.scan_errors, 'file')} it could not parse"),
    ]
    accounted = (
        coverage.sites_attributed + coverage.sites_terminal + coverage.sites_unresolved
    )
    if accounted != coverage.sites_seen:
        # The census is the one number here that can be checked against itself,
        # so it is -- loudly.  A split that does not add up means expressions
        # are falling out of the count somewhere, and a coverage figure built
        # on a leaking denominator is worth less than no figure at all.
        lines.append(
            (
                f"  the census does not add up: {coverage.sites_seen} seen, "
                f"{accounted} accounted for",
                WARNING,
            )
        )
    return lines


def _coverage(coverage: Coverage) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = [("", PLAIN), ("coverage", HEADING)]
    if coverage.scanned:
        lines.append(
            _row(
                "scanned",
                f"{plural(coverage.files, 'file')}, "
                f"{plural(coverage.models, 'model')}, "
                f"{plural(coverage.sites, 'call site')} "
                f"({coverage.sites_resolved} resolved, "
                f"{coverage.sites_probable} probable)",
            )
        )
        lines.extend(_census(coverage))
    else:
        lines.append(_row("scanned", "nothing (--no-callsites)"))

    if coverage.recording_exists:
        # Every N above is a median over these invocations, so how many there
        # were bounds the whole report: one run is one page load's luck.
        over = (
            f" over {plural(coverage.recording_invocations, 'invocation')}"
            if coverage.recording_invocations
            else ""
        )
        lines.append(
            _row(
                "recording",
                f"{relative(coverage.recording_path)}  "
                f"{plural(coverage.records, 'record')}{over}, "
                f"{coverage.malformed} malformed, "
                f"{plural(coverage.groups, 'query group')}, "
                f"{age(coverage.recording_age_seconds)}",
            )
        )
        lines.append(
            _row(
                "traced",
                f"{coverage.findings_matched} to a call site, "
                f"{coverage.findings_runtime_only} with no call site, "
                f"{coverage.findings_unattributed} to no model at all",
            )
        )
    else:
        lines.append(_row("recording", f"none at {relative(coverage.recording_path)}"))
        lines.append(("", PLAIN))
        for text in textwrap.wrap(HOW_TO_RECORD, width=76):
            lines.append((f"  {text}", NOTICE))

    if not coverage.benchmarked:
        lines.append(_row("benchmark", "skipped (--no-benchmark)"))
    else:
        lines.append(
            _row(
                "benchmark",
                f"{plural(coverage.relations_benchmarked, 'relation')} timed"
                + (
                    f", {coverage.relations_skipped} skipped "
                    "(the database would not read the table)"
                    if coverage.relations_skipped
                    else ""
                ),
            )
        )
    if coverage.timed_out:
        lines.append(
            ("  run cut short by --timeout; the results above are partial", WARNING)
        )
    return lines


def render_text(verdicts, coverage: Coverage) -> list[tuple[str, str]]:
    """The report, as (text, style-name) pairs."""
    verdicts = list(verdicts)
    findings = [verdict for verdict in verdicts if verdict.actionable]
    unsure = [
        verdict
        for verdict in verdicts
        if not verdict.actionable and verdict.kind in ACTIONABLE_KINDS
    ]
    fine = [
        verdict
        for verdict in verdicts
        if not verdict.actionable and verdict.kind not in ACTIONABLE_KINDS
    ]

    lines: list[tuple[str, str]] = []
    if findings:
        lines.append(
            (
                f"{plural(len(findings), 'change')} worth making",
                WARNING,
            )
        )
        for verdict in findings:
            lines.extend(_block(verdict))
    else:
        # A tool that always finds something is a tool nobody trusts.
        lines.append(("no change worth making", SUCCESS))

    if unsure:
        lines.append(("", PLAIN))
        lines.append(
            (
                f"{plural(len(unsure), 'site')} this run could not fully account for",
                HEADING,
            )
        )
        for verdict in unsure:
            lines.extend(_block(verdict))

    if fine:
        lines.append(("", PLAIN))
        lines.append((f"{len(fine)} already fine", HEADING))
        for verdict in fine:
            lines.append(_one_liner(verdict))

    lines.extend(_coverage(coverage))
    return lines


# ----------------------------------------------------------------------
# json
# ----------------------------------------------------------------------


def _measurement(measurement) -> dict | None:
    if measurement is None:
        return None
    return {"seconds": measurement.seconds, "queries": measurement.queries}


def verdict_json(verdict: Verdict) -> dict:
    return {
        "kind": verdict.kind,
        "actionable": verdict.actionable,
        "confidence": verdict.confidence,
        "evidence": verdict.evidence,
        "basis": verdict.basis,
        "file": verdict.file,
        "line": verdict.line,
        "function": verdict.function,
        "expression": verdict.expression,
        "model": verdict.model,
        "relation": verdict.relation,
        "candidates": list(verdict.candidates),
        "source": verdict.source,
        "runtime_only": verdict.runtime_only,
        "rows": {"n": verdict.rows.n, "provenance": verdict.rows.provenance},
        "observed": {
            "queries": verdict.observed_queries,
            "seconds": verdict.observed_seconds,
            # Both of the above are per invocation; this is how many.
            "invocations": verdict.observed_invocations,
        },
        "current": _measurement(verdict.current),
        "best": {
            "strategy": verdict.best_strategy or None,
            **(_measurement(verdict.best) or {"seconds": None, "queries": None}),
        },
        "alternative": {
            "strategy": verdict.alternative_strategy or None,
            **(_measurement(verdict.alternative) or {"seconds": None, "queries": None}),
        },
        "saving": {
            "seconds": verdict.saved_seconds,
            "queries": verdict.saved_queries,
            "share": verdict.saved_share,
        },
        "headline": verdict.headline,
        "fix": verdict.fix,
        "notes": list(verdict.notes),
    }


def render_json(verdicts, coverage: Coverage) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.fromtimestamp(time.time(), timezone.utc).isoformat(),
        "verdicts": [verdict_json(verdict) for verdict in verdicts],
        "coverage": coverage.as_dict(),
    }


def dumps(verdicts, coverage: Coverage) -> str:
    return json.dumps(render_json(verdicts, coverage), indent=2, sort_keys=False)


__all__ = [
    "HEADING",
    "NOTICE",
    "PLAIN",
    "SCHEMA_VERSION",
    "SUCCESS",
    "WARNING",
    "Coverage",
    "dumps",
    "render_json",
    "render_text",
    "relative",
    "verdict_json",
]
