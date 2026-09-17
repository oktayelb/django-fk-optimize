import json
import time

from django_fk_optimize.recording import store


def make(shape, *, file="/app/views.py", line=10, ts=None, duration=0.001, **kw):
    return store.Record(
        ts=time.time() if ts is None else ts,
        shape=shape,
        shape_hash=kw.pop("shape_hash", str(abs(hash(shape)) % 10**8)),
        duration=duration,
        file=file,
        line=line,
        function=kw.pop("function", "view"),
        **kw,
    )


LOOKUP = 'SELECT "testapp_publisher"."id" FROM "testapp_publisher" WHERE "testapp_publisher"."id" = ? LIMIT ?'
LISTING = (
    'SELECT "testapp_book"."id" FROM "testapp_book" ORDER BY "testapp_book"."id" ASC'
)


def test_append_and_load_round_trip(tmp_path):
    path = tmp_path / "nested" / "recording.jsonl"

    written = store.append(path, [make(LOOKUP), make(LISTING)])

    assert written == 2
    assert path.exists()  # parent directories are created
    recording = store.load(path)
    assert recording.count == 2
    assert recording.malformed == 0
    assert [record.shape for record in recording.records] == [LOOKUP, LISTING]


def test_append_is_a_single_write_of_whole_lines(tmp_path):
    path = tmp_path / "recording.jsonl"

    store.append(path, [make(LOOKUP), make(LISTING)])
    store.append(path, [make(LOOKUP)])

    lines = path.read_text().splitlines()
    assert len(lines) == 3
    assert all(json.loads(line)["shape"] for line in lines)


def test_appending_nothing_does_not_create_a_file(tmp_path):
    path = tmp_path / "recording.jsonl"

    assert store.append(path, []) == 0
    assert not path.exists()


def test_a_missing_recording_is_a_result_not_an_error(tmp_path):
    recording = store.load(tmp_path / "absent.jsonl")

    assert recording.exists is False
    assert recording.count == 0
    assert recording.age_seconds is None
    assert recording.groups() == []


def test_malformed_lines_are_skipped_and_counted(tmp_path):
    path = tmp_path / "recording.jsonl"
    store.append(path, [make(LOOKUP)])
    with open(path, "a") as handle:
        handle.write("{not json at all\n")
        handle.write('{"ts": 1.0}\n')  # an object, but not a record
        handle.write("[1, 2, 3]\n")  # valid json, wrong type
        handle.write("\n")  # blank lines are not corruption
    store.append(path, [make(LISTING)])

    recording = store.load(path)

    assert recording.count == 2
    assert recording.malformed == 3


def test_max_records_caps_the_file(tmp_path):
    path = tmp_path / "recording.jsonl"

    assert store.append(path, [make(LOOKUP)] * 2, max_records=3) == 2
    assert store.append(path, [make(LOOKUP)] * 5, max_records=3) == 1
    assert store.append(path, [make(LOOKUP)], max_records=3) == 0
    assert store.count_lines(path) == 3


def test_clear_removes_the_recording(tmp_path):
    path = tmp_path / "recording.jsonl"
    store.append(path, [make(LOOKUP)])

    assert store.clear(path) is True
    assert not path.exists()
    assert store.clear(path) is False  # already gone, still not an error


def test_age_is_measured_from_the_newest_record(tmp_path):
    path = tmp_path / "recording.jsonl"
    store.append(path, [make(LOOKUP, ts=1000.0), make(LOOKUP, ts=2000.0)])

    recording = store.load(path)

    assert recording.oldest == 1000.0
    assert recording.newest == 2000.0
    assert recording.age_seconds > 0


def test_table_is_extracted_from_the_shape():
    assert store.table_of(LOOKUP) == "testapp_publisher"
    assert store.table_of("SELECT a FROM books WHERE b = ?") == "books"
    assert store.table_of("SELECT a FROM `books`") == "books"
    assert store.table_of("SELECT a FROM [books]") == "books"
    assert store.table_of("SELECT a FROM public.books") == "books"


def test_an_unreadable_table_is_none_not_a_guess():
    assert store.table_of("SELECT 1") is None
    assert store.table_of("") is None
    assert store.table_of("BEGIN") is None


def test_kind_separates_a_row_lookup_from_a_listing():
    assert store.kind_of(LOOKUP) == store.SINGLE_ROW
    assert store.kind_of(LISTING) == store.BULK
    assert store.kind_of('SELECT a FROM "t" WHERE "t"."id" IN (?, ?)') == store.BULK
    assert store.kind_of('SELECT a FROM "t" WHERE a = ? AND b = ?') == store.BULK
    assert store.kind_of('INSERT INTO "t" VALUES (?)') == store.OTHER
    assert store.kind_of("BEGIN") == store.OTHER


def test_grouping_counts_repeats_per_call_site():
    records = (
        [make(LOOKUP, line=10, duration=0.001) for _ in range(5)]
        + [make(LOOKUP, line=99, duration=0.002)]
        + [make(LISTING, line=10, duration=0.010)]
    )

    groups = store.group(records)

    assert len(groups) == 3
    busiest = groups[0]
    assert busiest.count == 5
    assert busiest.table == "testapp_publisher"
    assert busiest.kind == store.SINGLE_ROW
    assert busiest.is_n_plus_one is True
    assert busiest.attribution.line == 10
    assert busiest.attribution.function == "view"
    assert busiest.total_seconds == 0.005
    assert round(busiest.average_seconds, 6) == 0.001

    # Same shape, different line: a separate finding with its own fix.
    assert groups[1].count == 1
    assert groups[1].is_n_plus_one is False


def test_grouping_keeps_the_source_classification():
    records = [make(LOOKUP, source="template") for _ in range(3)]

    (found,) = store.group(records)

    assert found.source == "template"
    assert found.count == 3


def test_groups_come_from_a_loaded_recording(tmp_path):
    path = tmp_path / "recording.jsonl"
    store.append(path, [make(LOOKUP) for _ in range(4)])

    (found,) = store.load(path).groups()

    assert found.count == 4
    assert found.shape == LOOKUP


# -- invocations -------------------------------------------------------
#
# `count` is the N of one call, and the only way to know what one call did is
# to know which records came from it.  Everything below is a recording that
# holds more than one invocation, which is every recording made the way the
# README asks for one.


def invocation(name, shape, times, *, line=10, duration=0.001):
    """`times` occurrences of one query, all issued by one recorder."""
    return [make(shape, line=line, duration=duration, run=name) for _ in range(times)]


def test_count_is_the_n_of_one_call_and_total_is_the_whole_file():
    records = (
        invocation("a", LOOKUP, 5)
        + invocation("b", LOOKUP, 5)
        + invocation("c", LOOKUP, 5)
    )

    (found,) = store.group(records)

    assert found.count == 5  # what one page load did
    assert found.total == 15  # what three of them left behind
    assert found.invocations == 3
    assert found.is_n_plus_one is True


def test_one_lookup_per_call_is_not_an_n_plus_one_however_many_calls():
    """The bug this field exists for: traffic used to read as a loop."""
    records = [record for i in range(40) for record in invocation(i, LOOKUP, 1)]

    (found,) = store.group(records)

    assert found.count == 1
    assert found.total == 40
    assert found.is_n_plus_one is False


def test_one_odd_call_does_not_decide_n():
    records = (
        invocation("a", LOOKUP, 200)
        + invocation("b", LOOKUP, 200)
        + invocation("c", LOOKUP, 12)  # an empty page, or the last one
    )

    (found,) = store.group(records)

    assert found.count == 200
    assert found.total == 412
    assert found.invocations == 3


def test_an_even_number_of_calls_lands_between_the_middle_two():
    records = invocation("a", LOOKUP, 2) + invocation("b", LOOKUP, 6)

    (found,) = store.group(records)

    assert found.count == 4


def test_a_single_invocation_reports_what_it_always_did():
    (found,) = store.group(invocation("only", LOOKUP, 7))

    assert (found.count, found.total, found.invocations) == (7, 7, 1)
    assert found.total_seconds == found.seconds


def test_a_recording_written_before_runs_existed_reads_as_one_call(tmp_path):
    """No `run` key at all: the file every installed version until now wrote."""
    path = tmp_path / "recording.jsonl"
    older = []
    for _ in range(6):
        data = make(LOOKUP).as_dict()
        del data["run"]
        older.append(json.dumps(data))
    path.write_text("\n".join(older) + "\n")

    loaded = store.load(path)
    (found,) = loaded.groups()

    assert loaded.malformed == 0  # the absent field is not corruption
    assert [record.run for record in loaded.records] == [""] * 6
    assert (found.count, found.total, found.invocations) == (6, 6, 1)


def test_invocations_interleaved_in_one_file_are_still_one_finding(tmp_path):
    """Two workers appending to one file, a line each at a time."""
    path = tmp_path / "recording.jsonl"
    for _ in range(4):
        store.append(path, [make(LOOKUP, run="worker-1"), make(LOOKUP, run="worker-2")])

    (found,) = store.load(path).groups()

    assert found.invocations == 2
    assert found.count == 4  # what each worker did, not what the file holds
    assert found.total == 8


def test_the_same_query_from_two_lines_stays_two_findings_across_calls():
    records = (
        invocation("a", LOOKUP, 3, line=10)
        + invocation("a", LOOKUP, 1, line=99)
        + invocation("b", LOOKUP, 3, line=10)
        + invocation("b", LOOKUP, 1, line=99)
    )

    first, second = store.group(records)

    assert (first.attribution.line, first.count, first.invocations) == (10, 3, 2)
    assert (second.attribution.line, second.count, second.invocations) == (99, 1, 2)


def test_seconds_is_one_calls_time_and_total_seconds_is_the_files():
    records = (
        invocation("a", LOOKUP, 2, duration=0.001)
        + invocation("b", LOOKUP, 2, duration=0.001)
        + invocation("c", LOOKUP, 2, duration=0.005)  # a slow one
    )

    (found,) = store.group(records)

    assert round(found.seconds, 6) == 0.002  # the median call: 2 x 1ms
    assert round(found.total_seconds, 6) == 0.014
    assert round(found.average_seconds, 6) == round(0.014 / 6, 6)


def test_the_busiest_group_is_the_one_with_the_biggest_n():
    """Per call, not per file: traffic is not a finding."""
    seen_often = [
        record for i in range(20) for record in invocation(i, LOOKUP, 2, line=10)
    ]
    real_loop = invocation("once", LOOKUP, 10, line=99)

    first, second = store.group(seen_often + real_loop)

    assert (first.count, first.total) == (10, 10)
    assert (second.count, second.total) == (2, 40)


def test_a_group_built_by_hand_states_one_calls_numbers():
    found = store.QueryGroup(
        shape_hash="h",
        table="testapp_publisher",
        attribution=store.Attribution("/app/views.py", 10, "view"),
        source="python",
        count=12,
        total_seconds=0.2,
        kind=store.SINGLE_ROW,
    )

    assert (found.total, found.invocations, found.seconds) == (12, 1, 0.2)


def test_invocations_counts_calls_not_the_busiest_group():
    """Two pages hit a different number of times sum, they do not max.

    A group knows how many invocations folded into it but not which ones, so
    the busiest group understates the file and adding the groups up counts one
    page load once per query shape it issued.  The run ids on the records are
    the only exact answer, which is why the count is taken from them.
    """
    recording = store.Recording(
        records=(
            invocation("a", LOOKUP, 5, line=10)
            + invocation("b", LOOKUP, 5, line=10)
            + invocation("c", LOOKUP, 5, line=10)
            + invocation("d", LISTING, 1, line=40)
            + invocation("e", LISTING, 1, line=40)
        )
    )

    lookups, listings = sorted(recording.groups(), key=lambda g: -g.invocations)
    assert lookups.invocations == 3
    assert listings.invocations == 2

    # Neither the largest (3) nor the sum of the groups (5) is the number of
    # calls; five separate recorders ran, and that is what the file holds.
    assert recording.invocations == 5


def test_a_recording_without_run_ids_covers_one_invocation():
    recording = store.Recording(records=[make(LOOKUP), make(LOOKUP), make(LISTING)])

    assert recording.invocations == 1


def test_an_empty_recording_covers_nothing():
    assert store.Recording(records=[]).invocations == 0
