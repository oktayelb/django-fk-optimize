"""The rendering: what a report says, and what it refuses to leave out."""

import json

from django_fk_optimize.analysis import report
from django_fk_optimize.analysis.benchmark import Measurement
from django_fk_optimize.analysis.verdicts import (
    ESTIMATED,
    ID_ONLY,
    N_PLUS_ONE,
    OBSERVED,
    REMOVE_HINT,
    Rows,
    Verdict,
)
from django_fk_optimize.utils.callsites import RESOLVED


def text_of(verdicts, coverage):
    return "\n".join(line for line, _style in report.render_text(verdicts, coverage))


def finding(**kw):
    verdict = Verdict(
        kind=N_PLUS_ONE,
        model="alarms.Alarm",
        relation="type",
        rows=Rows(412, OBSERVED),
        confidence=RESOLVED,
        actionable=True,
        file="alarms/views.py",
        line=80,
        function="alarm_dashboard",
        expression="Alarm.objects.filter(active=True)",
        observed_queries=412,
        observed_seconds=0.3402,
        current=Measurement(seconds=0.3124, queries=413),
        best_strategy="select_related",
        best=Measurement(seconds=0.0187, queries=1),
        alternative_strategy="prefetch_related",
        alternative=Measurement(seconds=0.0241, queries=2),
        headline="412 extra queries, one per row",
        fix='Alarm.objects.filter(active=True).select_related("type")',
    )
    for key, value in kw.items():
        setattr(verdict, key, value)
    return verdict


def test_a_finding_reads_the_way_it_was_asked_to():
    text = text_of([finding()], report.Coverage(recording_exists=True))

    assert "alarms/views.py:80  alarm_dashboard()" in text
    assert "alarms.Alarm.type" in text
    assert "rows        412  (observed)" in text
    assert "1 + 412 queries" in text
    assert "340.2 ms recorded sql / 312.4 ms measured" in text
    assert "best        select_related    1 query     18.7 ms measured" in text
    assert "alternative prefetch_related  2 queries   24.1 ms measured" in text
    assert (
        'fix         Alarm.objects.filter(active=True).select_related("type")' in text
    )
    assert "~293.7 ms per call (94%), 412 fewer queries" in text
    assert "confidence: resolved" in text


def test_an_estimate_never_reads_as_a_measurement():
    verdict = finding(
        rows=Rows(500, ESTIMATED), observed_queries=None, observed_seconds=None
    )

    text = text_of([verdict], report.Coverage(recording_exists=True))

    assert "rows        500  (estimated)" in text
    assert "recorded" not in text


def test_nothing_worth_changing_is_one_line_not_an_empty_table():
    ok = Verdict(
        kind=ID_ONLY,
        model="alarms.Alarm",
        relation="type",
        rows=Rows(0, "unknown"),
        file="alarms/views.py",
        line=12,
        headline="type_id is already on the row; nothing to do",
    )

    text = text_of([ok], report.Coverage(recording_exists=True))

    assert "no change worth making" in text
    assert "1 already fine" in text
    assert "already on the row" in text


def test_a_removal_is_not_priced_in_milliseconds():
    verdict = Verdict(
        kind=REMOVE_HINT,
        model="alarms.Alarm",
        relation="site",
        rows=Rows(0, "unknown"),
        actionable=True,
        file="alarms/views.py",
        line=80,
        headline='select_related("site") is never used here',
        fix='Alarm.objects.select_related("type")',
    )

    text = text_of([verdict], report.Coverage(recording_exists=True))

    assert "one join per call, not timed" in text
    assert "current" not in text


def test_a_run_with_no_recording_says_how_to_get_one():
    text = text_of([], report.Coverage(recording_path=".fk_optimize/recording.jsonl"))

    assert "none at .fk_optimize/recording.jsonl" in text
    assert "FkOptimizeMiddleware" in text
    assert "recording.record()" in text


def test_coverage_reports_what_could_not_be_read():
    coverage = report.Coverage(
        files=42,
        sites=31,
        sites_resolved=27,
        sites_probable=4,
        sites_unresolved=2,
        scan_errors=1,
        recording_path="rec.jsonl",
        recording_exists=True,
        records=4812,
        malformed=3,
        groups=88,
        recording_age_seconds=3600,
        findings_matched=3,
        findings_runtime_only=1,
        findings_unattributed=2,
    )

    text = text_of([], coverage)

    assert "42 files" in text
    assert "31 call sites (27 resolved, 4 probable)" in text
    assert "2 querysets the scanner could not follow" in text
    assert "1 file it could not parse" in text
    assert "4812 records, 3 malformed" in text
    assert "1 hour old" in text
    assert "1 with no call site" in text
    assert "2 to no model at all" in text


def test_a_count_of_one_reads_as_one():
    coverage = report.Coverage(
        files=1,
        sites=1,
        models=1,
        sites_unresolved=1,
        scan_errors=1,
        recording_exists=True,
        records=1,
        groups=1,
        relations_benchmarked=1,
    )

    text = text_of([], coverage)

    assert "1 file, 1 model, 1 call site" in text
    assert "1 queryset the scanner could not follow" in text
    assert "1 file it could not parse" in text
    assert "1 record," in text
    assert "1 query group," in text
    assert "1 relation timed" in text


def test_a_timed_out_run_says_the_results_are_partial():
    text = text_of([], report.Coverage(recording_exists=True, timed_out=True))

    assert "cut short" in text and "partial" in text


def test_json_carries_the_schema_the_coverage_and_the_fix():
    payload = json.loads(report.dumps([finding()], report.Coverage(files=42)))

    assert payload["schema_version"] == report.SCHEMA_VERSION
    assert payload["generated_at"].endswith("+00:00")
    assert payload["coverage"]["files"] == 42
    (verdict,) = payload["verdicts"]
    assert verdict["rows"] == {"n": 412, "provenance": "observed"}
    assert verdict["best"]["strategy"] == "select_related"
    assert verdict["best"]["queries"] == 1
    assert verdict["saving"]["queries"] == 412
    assert verdict["fix"].endswith('.select_related("type")')
    assert verdict["actionable"] is True


def test_a_path_under_the_working_directory_is_shortened():
    import os

    absolute = os.path.join(os.getcwd(), "alarms", "views.py")

    assert report.relative(absolute) == os.path.join("alarms", "views.py")
    assert report.relative("/somewhere/else/views.py") == "/somewhere/else/views.py"


def test_age_is_readable():
    assert report.age(None) == "unknown age"
    assert report.age(5) == "5s old"
    assert report.age(120) == "2 minutes old"
    assert report.age(3600) == "1 hour old"
    assert report.age(172800) == "2 days old"


def test_evidence_is_reported_separately_from_confidence():
    """`resolved` says which call site this is, not that the number is good.

    On a half-populated development database most suggestions rest on tables
    with no rows. Printing only `confidence: resolved` beside `rows 0` read as
    confidence in the recommendation.
    """
    from django_fk_optimize.analysis import verdicts as V

    grounded = V.Verdict(
        kind=V.N_PLUS_ONE,
        model="testapp.Book",
        relation="publisher",
        rows=V.Rows(60, V.OBSERVED),
        confidence=RESOLVED,
        actionable=True,
        file="views.py",
        line=6,
    )
    empty = V.Verdict(
        kind=V.N_PLUS_ONE,
        model="testapp.Book",
        relation="publisher",
        rows=V.Rows(0, V.UNKNOWN),
        confidence=RESOLVED,
        actionable=True,
        file="views.py",
        line=9,
    )

    assert grounded.evidence == V.EVIDENCE_OBSERVED
    assert empty.evidence == V.EVIDENCE_NONE

    text = text_of([grounded, empty], report.Coverage())
    assert "confidence: resolved · evidence: observed" in text
    assert "confidence: resolved · evidence: none" in text


def test_an_estimated_row_count_is_evidence_but_weaker():
    from django_fk_optimize.analysis import verdicts as V

    verdict = V.Verdict(
        kind=V.N_PLUS_ONE,
        model="testapp.Book",
        relation="publisher",
        rows=V.Rows(60, V.ESTIMATED),
        confidence=RESOLVED,
    )
    assert verdict.evidence == V.EVIDENCE_ESTIMATED


def test_an_unused_hint_needs_no_rows_to_be_certain():
    """Removing a hint is a static fact, not a claim about today's data."""
    from django_fk_optimize.analysis import verdicts as V

    verdict = V.Verdict(
        kind=V.REMOVE_HINT,
        model="testapp.Book",
        relation="author",
        rows=V.Rows(0, V.UNKNOWN),
        confidence=RESOLVED,
    )
    assert verdict.evidence == V.EVIDENCE_OBSERVED


def test_the_json_carries_both_axes():
    from django_fk_optimize.analysis import verdicts as V

    verdict = V.Verdict(
        kind=V.N_PLUS_ONE,
        model="testapp.Book",
        relation="publisher",
        rows=V.Rows(0, V.UNKNOWN),
        confidence=RESOLVED,
    )
    payload = report.verdict_json(verdict)
    assert payload["confidence"] == RESOLVED
    assert payload["evidence"] == V.EVIDENCE_NONE
    assert payload["basis"] == V.MEASURED


def test_a_structural_pick_never_quotes_a_saving():
    """Durations taken over too few rows must not become a claim.

    Seen on a real project: "saving ~-0.1 ms per call (-34%)" -- a negative
    saving, printed for a change the tool was recommending, computed from two
    timings that were both noise.
    """
    from django_fk_optimize.analysis import verdicts as V

    verdict = V.Verdict(
        kind=V.EXTRA_QUERY,
        model="testapp.Book",
        relation="publisher",
        rows=V.Rows(1, V.STATIC_BOUND),
        confidence=RESOLVED,
        actionable=True,
        basis=V.STRUCTURAL,
        current=Measurement(0.0004, 1, rows=0),
        best_strategy="select_related",
        best=Measurement(0.0005, 1, rows=0),
    )

    assert verdict.measured is False, "noise is not a measurement"
    assert verdict.saved_seconds is None

    text = text_of([verdict], report.Coverage())
    assert "too few rows to time" in text
    assert "-0.1 ms" not in text
    assert "~-" not in text


def test_a_real_measurement_still_quotes_its_saving():
    from django_fk_optimize.analysis import verdicts as V

    verdict = V.Verdict(
        kind=V.N_PLUS_ONE,
        model="testapp.Book",
        relation="publisher",
        rows=V.Rows(60, V.OBSERVED),
        confidence=RESOLVED,
        actionable=True,
        basis=V.MEASURED,
        current=Measurement(0.0100, 61, rows=60),
        best_strategy="select_related",
        best=Measurement(0.0010, 1, rows=60),
    )

    assert verdict.measured is True
    text = text_of([verdict], report.Coverage())
    assert "60 fewer queries" in text


def test_the_confidence_column_never_collides_with_the_saving():
    from django_fk_optimize.analysis import verdicts as V

    verdict = V.Verdict(
        kind=V.N_PLUS_ONE,
        model="testapp.Book",
        relation="publisher",
        rows=V.Rows(1234567, V.OBSERVED),
        confidence=RESOLVED,
        actionable=True,
        current=Measurement(0.5, 1234568, rows=1234567),
        best_strategy="select_related",
        best=Measurement(0.001, 1, rows=1234567),
    )
    line = next(
        line
        for line, _style in report.render_text([verdict], report.Coverage())
        if "confidence:" in line
    )
    assert "queriesconfidence" not in line
    assert "  confidence:" in line


# ----------------------------------------------------------------------
# how much of one call, and over how many
# ----------------------------------------------------------------------


def test_an_observed_n_says_how_many_calls_it_was_taken_over():
    """N is a median across invocations, and a median of one is an anecdote."""
    text = text_of(
        [finding(observed_invocations=37)], report.Coverage(recording_exists=True)
    )

    assert "rows        412  (observed)  median of 37 calls" in text


def test_a_single_invocation_is_not_dressed_up_as_a_pattern():
    text = text_of(
        [finding(observed_invocations=1)], report.Coverage(recording_exists=True)
    )

    assert "412  (observed)  one call only" in text


def test_an_estimate_has_no_invocations_to_claim():
    """A row count is not a median of anything, whatever the field holds."""
    verdict = finding(rows=Rows(500, ESTIMATED), observed_invocations=9)

    text = text_of([verdict], report.Coverage(recording_exists=True))

    assert "rows        500  (estimated)" in text
    assert "calls" not in text


def test_the_recording_says_how_many_runs_it_holds():
    coverage = report.Coverage(
        recording_exists=True,
        recording_path="rec.jsonl",
        records=4812,
        groups=88,
        recording_invocations=37,
    )

    text = text_of([], coverage)

    assert "4812 records over 37 invocations," in text


def test_a_recording_with_no_run_count_does_not_invent_one():
    coverage = report.Coverage(recording_exists=True, records=12)

    assert "12 records, 0 malformed" in text_of([], coverage)


def test_json_carries_the_reach_of_the_observation():
    payload = json.loads(
        report.dumps([finding(observed_invocations=37)], report.Coverage())
    )

    (verdict,) = payload["verdicts"]
    assert verdict["observed"] == {
        "queries": 412,
        "seconds": 0.3402,
        "invocations": 37,
    }
