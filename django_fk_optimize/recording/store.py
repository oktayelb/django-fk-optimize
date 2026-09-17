"""The recording file: append-only JSONL, one query per line.

Append-only and line-oriented because the writers are a web application's
worker processes.  There is no lock and no coordination between them; the only
thing that keeps the file readable is that each flush is a single `write()` of
an already-joined buffer, which the OS appends whole under `O_APPEND`.  Workers
therefore interleave whole lines rather than fragments.

"Therefore" is doing a lot of work in that sentence -- a buffer larger than the
pipe-atomic size can still be split -- so the reader treats a malformed line as
an expected event: it is skipped, counted, and reported.  A profiler that
crashes a command because a worker was killed mid-write is worse than one that
says "4 lines were unreadable".

Nothing in this module knows how a record is produced; it only knows the shape
on disk and how to fold those records into the groups the analysis reads.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from statistics import median

# What a query does to the database, as far as the N+1 hunt cares.
SINGLE_ROW = "single"  # one row, looked up by one value -- the N+1 signature
BULK = "bulk"  # a set of rows: the query an N+1 replaces
OTHER = "other"  # writes, DDL, transaction control

# The four quotings Django's backends emit, optionally schema-qualified:
# FROM x, FROM "x", FROM `x`, FROM [x], FROM "schema"."x".  A subquery or
# anything else yields no table rather than a guess.
_IDENT = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_$]*)'
_FROM = re.compile(rf"\bFROM\s+({_IDENT}(?:\.{_IDENT})*)", re.IGNORECASE)


@dataclass(frozen=True)
class Attribution:
    """The user line a query was issued from."""

    file: str = ""
    line: int = 0
    function: str = ""

    def __str__(self):
        return f"{self.file}:{self.line}"


@dataclass(frozen=True)
class Record:
    """One recorded query.

    Deliberately absent: the parameters.  `shape` has had every literal and
    every placeholder flattened out of it before it arrives here, because a
    recording file ends up in a repository, a bug report or a CI artifact, and
    none of those are places for the contents of a WHERE clause.
    """

    ts: float
    shape: str
    shape_hash: str
    duration: float
    file: str = ""
    line: int = 0
    function: str = ""
    source: str = "python"
    context: tuple[tuple[str, int, str], ...] = ()
    db: str = "default"
    # Which invocation issued this query: one recorder's id.  Without it a
    # recording is a heap -- the same lookup from the same line reads the same
    # whether it ran twenty times on one page load or once on twenty of them,
    # and those are a bug and a non-bug.  Empty means a file written before
    # runs were recorded.
    run: str = ""

    @property
    def attribution(self) -> Attribution:
        return Attribution(self.file, self.line, self.function)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["context"] = [list(frame) for frame in self.context]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Record:
        return cls(
            ts=float(data["ts"]),
            shape=str(data["shape"]),
            shape_hash=str(data["shape_hash"]),
            duration=float(data.get("duration") or 0.0),
            file=str(data.get("file") or ""),
            line=int(data.get("line") or 0),
            function=str(data.get("function") or ""),
            source=str(data.get("source") or "python"),
            context=tuple(
                (str(frame[0]), int(frame[1]), str(frame[2]))
                for frame in data.get("context") or ()
                if len(frame) >= 3
            ),
            db=str(data.get("db") or "default"),
            # Absent in any file written before runs existed, and absent for
            # good: those records are read as one anonymous invocation rather
            # than rejected.
            run=str(data.get("run") or ""),
        )


@dataclass(frozen=True)
class QueryGroup:
    """Every occurrence of one query shape at one call site.

    Two counts live here, and confusing them is the difference between a
    finding and a fiction.  `count` is the N in "N+1": how many times this
    query ran in *one* invocation of the code that issues it.  `total` is how
    many times it ran in the whole recording, which is `count` again for every
    invocation that went through the same line.  A recording made the way the
    README asks for one -- click through the pages, run the suite, leave it on
    in staging -- holds many invocations, so `total` is a measure of traffic
    and only `count` describes the loop anybody can go and fix.

    Neither is a row count from the cursor: `cursor.rowcount` is -1 for a
    SELECT on most backends, and the number that matters is how many times the
    query ran anyway.

    `seconds` and `total_seconds` divide the same way -- time in one
    invocation, time across the recording -- and pair with the count of the
    same reach.  A per-call N printed beside a whole-recording duration is the
    same category error in two columns.

    Records from different invocations that share a shape and a call site stay
    in one group.  They are one finding seen several times, and the several
    times are what make `count` more than one unlucky request.
    """

    shape_hash: str
    table: str | None
    attribution: Attribution
    source: str
    # Per invocation: the N, and the time this query cost one call.
    count: int
    total_seconds: float
    kind: str
    shape: str = ""
    # Across the recording.  Stated together or not at all, so that a group
    # assembled by hand can give the per-call numbers only; see __post_init__.
    total: int = 0
    invocations: int = 1
    seconds: float = 0.0

    def __post_init__(self):
        """Read a group that states no totals as a single invocation.

        `group()` states every field.  Anything else building a QueryGroup --
        a test fixture, a caller folding records from somewhere that is not a
        recording file -- knows one call's worth of numbers and means exactly
        one call of them.  That is the same reading a recording written before
        runs existed gets, so the two arrive at one answer instead of at two
        defensible ones.

        `total` is the flag for both, because zero is unambiguous there: a
        group exists because at least one record made it, so no real group
        carries a total of zero, while a group whose queries were all too
        quick to time carries a perfectly real zero duration.
        """
        if not self.total:
            object.__setattr__(self, "total", self.count)
            object.__setattr__(self, "seconds", self.total_seconds)

    @property
    def average_seconds(self) -> float:
        """What one execution of this query cost.

        Over `total`, not `count`: every execution in the file is a sample of
        how long this query takes, and there is no reason to average over the
        subset that one invocation happened to contain.
        """
        return self.total_seconds / self.total if self.total else 0.0

    @property
    def is_n_plus_one(self) -> bool:
        """Repeated single-row lookups from one place: the signature.

        More than one *per call*.  The same query on each of fifty page loads
        is fifty rows fetched one at a time and nothing at all to fix.
        """
        return self.kind == SINGLE_ROW and self.count > 1


@dataclass
class Recording:
    """What was on disk, plus what could not be read.

    `malformed` is part of the result, not a detail swallowed on the way: a
    reader that quietly drops lines reports a smaller N than really happened,
    and an under-reported N+1 is a wrong answer delivered confidently.
    """

    path: Path | None = None
    records: list[Record] = dataclass_field(default_factory=list)
    malformed: int = 0
    exists: bool = True

    def __len__(self):
        return len(self.records)

    @property
    def count(self) -> int:
        return len(self.records)

    @property
    def newest(self) -> float | None:
        return max((record.ts for record in self.records), default=None)

    @property
    def oldest(self) -> float | None:
        return min((record.ts for record in self.records), default=None)

    @property
    def age_seconds(self) -> float | None:
        """How long ago the last query was recorded, or None if empty.

        Printed next to every verdict built on it: a recording from last month
        describes last month's code.
        """
        newest = self.newest
        return None if newest is None else max(time.time() - newest, 0.0)

    @property
    def invocations(self) -> int:
        """How many separate calls this recording covers.

        Counted here rather than folded out of the groups, because a group
        remembers how many invocations it was folded from but not which ones.
        Two pages hit three and five times give groups saying 3 and 5, and
        neither the larger nor the sum is the answer -- the first understates
        and the second counts one page load once per query shape it issued.
        The records still carry their run ids, so the union is exact and there
        is no reason to estimate it.

        A recording written before runs were recorded has one anonymous run and
        counts as the single invocation it is read as everywhere else.
        """
        return len({record.run for record in self.records})

    def groups(self) -> list[QueryGroup]:
        return group(self.records)


# ----------------------------------------------------------------------
# reading and writing
# ----------------------------------------------------------------------


def append(path, records, max_records: int | None = None) -> int:
    """Append records as JSONL. Returns how many were written.

    `max_records` is a ceiling on the whole file, checked by counting the lines
    already in it.  The count is the caller's to cache if it cares about the
    cost; the recorder does exactly that.
    """
    records = list(records)
    if not records:
        return 0
    target = Path(path)
    if max_records is not None:
        room = max_records - count_lines(target)
        if room <= 0:
            return 0
        records = records[:room]
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(record.as_dict(), separators=(",", ":")) + "\n" for record in records
    )
    # One open, one write: two workers appending at the same moment interleave
    # lines, not halves of a line.
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(payload)
    return len(records)


def load(path) -> Recording:
    """Read a recording. A missing or corrupt file is a result, not an error."""
    target = Path(path)
    recording = Recording(path=target)
    try:
        handle = open(target, encoding="utf-8")
    except FileNotFoundError:
        recording.exists = False
        return recording
    except OSError:
        recording.exists = False
        return recording
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if not isinstance(data, dict):
                    raise ValueError("not an object")
                recording.records.append(Record.from_dict(data))
            except (ValueError, TypeError, KeyError, IndexError):
                recording.malformed += 1
    return recording


def clear(path) -> bool:
    """Remove the recording. True if there was one to remove."""
    try:
        os.unlink(Path(path))
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def count_lines(path) -> int:
    try:
        with open(Path(path), "rb") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


# ----------------------------------------------------------------------
# aggregation
# ----------------------------------------------------------------------


def table_of(shape: str) -> str | None:
    """The table a normalised query selects from, or None.

    None rather than a guess: the join downstream confirms a match by table,
    and a wrong table turns a confirmation into a false positive.
    """
    match = _FROM.search(shape or "")
    if match is None:
        return None
    # Django quotes a schema qualifier segment by segment ("schema"."table"),
    # so the last segment is the table name.
    name = match.group(1).split(".")[-1].strip('"`[]')
    return name or None


def kind_of(shape: str) -> str:
    """Single-row lookup, bulk select, or neither.

    The distinction is the whole point of the recording: a thousand bulk
    selects are a thousand separate pieces of work, while a thousand identical
    single-row lookups from one line are one piece of work done wrong.
    """
    text = (shape or "").strip()
    if text[:6].upper() != "SELECT":
        return OTHER
    upper = text.upper()
    head, sep, tail = upper.partition(" WHERE ")
    if not sep:
        return BULK
    for stop in (" LIMIT ", " ORDER BY ", " GROUP BY "):
        tail = tail.partition(stop)[0]
    if " IN (" in tail:
        return BULK
    return SINGLE_ROW if tail.count("?") == 1 else BULK


def group(records) -> list[QueryGroup]:
    """Fold records into (shape, call site) groups, busiest first.

    Grouped by shape *and* call site: the same lookup issued from two views is
    two findings with two fixes, and merging them would report an N that no
    single place ever produced.

    Not grouped by run.  Two hits on the same page are one finding, and
    splitting them would turn a loop seen fifty times into fifty loops.  The
    run is used inside each group instead, to divide what the file holds by
    how many invocations put it there -- which is the only reason this tool
    knows an N a static linter cannot work out, and the only reason that N is
    an answer about one page load rather than about an afternoon of traffic.

    A record with no run belongs to a file written before runs were recorded.
    Every one of them falls in the same empty-string bucket, so such a file
    reads as a single invocation and reports exactly what it always did.
    """
    buckets: dict[tuple, dict] = {}
    for record in records:
        key = (record.shape_hash, record.file, record.line, record.source)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = buckets[key] = {
                "shape_hash": record.shape_hash,
                "table": table_of(record.shape),
                "attribution": record.attribution,
                "source": record.source,
                "kind": kind_of(record.shape),
                "shape": record.shape,
                "runs": {},
            }
        tally = bucket["runs"].setdefault(record.run, [0, 0.0])
        tally[0] += 1
        tally[1] += record.duration
    groups = [_fold(bucket) for bucket in buckets.values()]
    groups.sort(
        key=lambda item: (
            -item.count,
            -item.total,
            -item.total_seconds,
            item.attribution.file,
            item.attribution.line,
        )
    )
    return groups


def _fold(bucket: dict) -> QueryGroup:
    """One bucket's per-run tallies, as the per-call and whole-file figures.

    The median, not the mean: a recording picks up whatever the application
    was doing, and one request that hit an empty page, or one that hit a
    paginated view on its last page, must not be allowed to decide what the
    loop does.  It is the same choice the benchmark makes across its repeats,
    for the same reason.

    An even number of invocations can put the median between two of them, so
    it is rounded -- N is a count of queries and there is no such thing as
    half a query.
    """
    runs = bucket.pop("runs")
    counts = [count for count, _ in runs.values()]
    seconds = [total for _, total in runs.values()]
    return QueryGroup(
        count=round(median(counts)),
        total_seconds=sum(seconds),
        total=sum(counts),
        invocations=len(runs),
        seconds=median(seconds),
        **bucket,
    )
