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
