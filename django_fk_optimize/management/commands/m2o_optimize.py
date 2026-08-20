"""Many-to-one queryset optimizer.

Scoped to the only relation kind where select_related vs prefetch_related is an
open question: a forward ForeignKey.  Everything else has an answer that does
not depend on your data, so this command does not report on it at all --

  * reverse FK, m2m, GenericRelation -> prefetch_related is the only legal plan;
    a join would multiply the result rows.
  * one_to_one, either direction     -> select_related, always.  There is one
    parent row per child row, so the join duplicates nothing and saves a
    roundtrip.
  * GenericForeignKey                -> no target model to join against.
  * multi-table-inheritance links    -> Django joins them unconditionally.

Use fk_optimize for the full survey.  This command only shows you decisions.

For a forward FK the two plans move different amounts of data:

    select_related      N * (1 - null_frac) * W bytes, 1 roundtrip
    prefetch_related    D * W bytes,                   2 roundtrips

where N is the child row count, D the number of distinct parents actually
referenced and W the average parent row width.  The child's own columns are
fetched either way and cancel out.  So prefetch wins exactly when

    W * (N * (1 - null_frac) - D)  >  latency * throughput

i.e. when the bytes saved by not duplicating the parent row across the join
exceed what the connection could have shipped during the extra roundtrip.

All three numbers come from statistics the server already keeps -- pg_class and
pg_stats on PostgreSQL, information_schema on MySQL, sqlite_stat1 on SQLite --
so the estimate costs milliseconds and never touches the table itself.  The
statistics have to be read per backend because there is no portable way to ask
for them; everything downstream of them is the same everywhere.  What each
backend can answer differs, and any number it cannot supply is substituted with
a default and reported as an assumption rather than a measurement.

--allow-scan falls back to COUNT queries on any backend.

Every run also times both plans, and the unhinted N+1 they exist to replace, so
the prediction is printed beside a measurement of the same relation.  Then it
times the whole model four ways -- bare N+1, everything joined, everything
prefetched, and the plan this command picked -- so the pick is shown beating the
alternatives rather than merely asserted.  Those timings are calibration, not
the verdict: they are one sample of one table on one connection, while the
estimate is what holds at full size.  Where the two disagree the report says so
rather than quietly switching sides.  --no-benchmark drops the timing pass and
leaves the run reading statistics only.

On caches: the ordering half of the problem is handled -- the variants are
interleaved and rotated so none of them owns the cold slot, the first round is
discarded, and each variant keeps its fastest run.  The regime half is not, and
cannot be from inside a database connection: after the discarded round every
page is in cache, so every number here is a warm one.  That is the right regime
for a query that runs all day and the wrong one for first-hit latency, and no
amount of rearranging inside the session will produce a cold measurement --
that needs the server's buffer pool and the OS page cache dropped from outside.

The output is a lookup table: "if a call site iterates this model and touches
this FK, use X".  It is not advice to bake X into a default manager -- whether a
given call site touches the relation at all is not something this command can
see.
"""

import json
from contextlib import ExitStack
from dataclasses import dataclass, field as dataclass_field
from time import perf_counter

from django.apps.registry import apps
from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connections, router
from django.db.models import Model
from django.db.models.fields.reverse_related import ForeignObjectRel

SELECT_RELATED = "select_related"
PREFETCH_RELATED = "prefetch_related"

# One roundtrip's worth of bytes on a backend the probe does not know how to
# make talk.  ~0.5 ms RTT over a gigabit link.
DEFAULT_BREAKEVEN_BYTES = 64 * 1024

# Used when no catalog is available to tell us how wide a parent row is.
DEFAULT_PARENT_WIDTH = 256

# Payload for the throughput probe.  Kept under MySQL's 4 MB default
# max_allowed_packet, which would refuse a larger result.
PROBE_PAYLOAD_BYTES = 1_000_000

# How to make each backend return N bytes.  repeat() is not portable; SQLite has
# no equivalent, and hex() doubles its input, hence the divisor.
PROBE_SQL = {
    "postgresql": ("SELECT repeat('x', %s)", 1),
    "mysql": ("SELECT REPEAT('x', %s)", 1),
    "sqlite": ("SELECT hex(zeroblob(%s))", 2),
}


@dataclass
class Candidate:
    """A forward ForeignKey -- the only relation this command has an opinion on."""

    model: type[Model]
    field: object
    name: str  # what you pass to select_related()/prefetch_related()


@dataclass
class Estimate:
    source: str  # "catalog" | "scan" | "unavailable"
    rows: float | None = None
    distinct: float | None = None
    parent_width: float | None = None
    null_frac: float = 0.0
    saved_bytes: float | None = None
    breakeven_bytes: float | None = None

    # Numbers this backend could not supply and that were filled in with a
    # default.  A verdict resting on one of these is a guess wearing the same
    # sentence as a measurement, so it says so.
    notes: list[str] = dataclass_field(default_factory=list)

    @property
    def fanout(self) -> float | None:
        if not self.rows or not self.distinct:
            return None
        return self.rows / self.distinct

    @property
    def join_bytes(self) -> float | None:
        """Bytes a join adds to the result set."""
        if self.rows is None or self.parent_width is None:
            return None
        return self.rows * (1.0 - self.null_frac) * self.parent_width


@dataclass
class Timing:
    """One measured queryset shape."""

    seconds: float | None = None
    queries: int | None = None


@dataclass
class PlanSpec:
    """A queryset shape to time: what to hint, under what name."""

    label: str
    select: tuple[str, ...] = ()
    prefetch: tuple[str, ...] = ()

    @property
    def shape(self):
        return self.select, self.prefetch


@dataclass
class Recommendation:
    candidate: Candidate
    plan: str
    reason: str
    estimate: Estimate | None = None
    timings: dict[str, Timing] | None = None


@dataclass
class Combined:
    """The whole-model plan: every per-field verdict rolled into one call.

    There is no powerset search here on purpose.  Sibling FKs joined off the
    same base form a star join -- each one widens the row, none of them multiply
    it -- so their costs add.  Each prefetch is its own separate query, so those
    add too.  Both sides being additive means the best combination is just the
    per-field winners taken together, and enumerating 2^n subsets would buy
    nothing but runtime.
    """

    select: list[str] = dataclass_field(default_factory=list)
    prefetch: list[str] = dataclass_field(default_factory=list)
    queries: int = 1
    join_bytes: float | None = None
    saved_bytes: float | None = None
    call: str = ""


@dataclass
class ModelReport:
    """One model's section of the report.

    Carries the connection alias because the router picks it per model, so two
    models in one run can be answered by two different servers -- and a verdict
    means nothing without knowing which one it came from.
    """

    model: type[Model]
    label: str
    using: str
    recs: list[Recommendation] = dataclass_field(default_factory=list)
    combined: Combined = dataclass_field(default_factory=Combined)

    # The whole-model comparison: every plan timed end to end, and which of
    # those labels is the one this command picked.
    plans: dict[str, Timing] = dataclass_field(default_factory=dict)
    chosen_label: str = "chosen"


class Command(BaseCommand):

    help = "Chooses select_related vs prefetch_related for forward foreign keys."

    def add_arguments(self, parser):

        parser.add_argument("app.model", nargs="?", type=str, default=None)
        parser.add_argument(
            "--timeout",
            type=int,
            default=30,
            help="seconds to spend benchmarking a single relation (default 30)",
        )
        parser.add_argument(
            "--django-models",
            action="store_true",
            help="when set includes django (and third party) models",
        )
        parser.add_argument(
            "--allow-scan",
            action="store_true",
            help="fall back to COUNT queries when the catalog has no statistics. "
            "This reads the whole table -- do not point it at production.",
        )
        parser.add_argument(
            "--no-benchmark",
            action="store_true",
            help="skip the timing pass; report the prediction alone and leave "
            "the tables untouched",
        )
        parser.add_argument(
            "--sample",
            type=int,
            default=1000,
            help="rows to slice off when timing the plans (default 1000, 0 for "
            "the whole table)",
        )
        parser.add_argument(
            "--breakeven-bytes",
            type=int,
            default=None,
            help="override the measured latency*throughput crossover",
        )
        parser.add_argument(
            "--assume-parent-width",
            type=int,
            default=DEFAULT_PARENT_WIDTH,
            help="parent row width in bytes when the catalog cannot supply one",
        )
        parser.add_argument("--json", action="store_true", help="machine-readable output")

    # ------------------------------------------------------------------
    # classification
    # ------------------------------------------------------------------

    def _candidate(self, model, field):
        """Return a Candidate for a forward FK, or None for everything else.

        `field.many_to_one` is only ever true on the forward side -- the reverse
        of a ForeignKey is a ManyToOneRel, which is one_to_many.  So the single
        flag rules out reverse relations, m2m and one-to-one in one test.
        """
        if not field.is_relation or not field.many_to_one:
            return None

        # GenericForeignKey is many_to_one but has no target model to join to.
        if field.related_model is None:
            return None

        # Defensive: a forward many_to_one is never a ForeignObjectRel, and a
        # parent link is a OneToOneField, so neither of these should fire. They
        # cost nothing and make the intent explicit.
        if isinstance(field, ForeignObjectRel):
            return None
        if getattr(field.remote_field, "parent_link", False):
            return None

        return Candidate(model=model, field=field, name=field.name)

    def _candidates(self, model):
        found = []
        for field in model._meta.get_fields():
            candidate = self._candidate(model, field)
            if candidate is not None:
                found.append(candidate)
        return found

    # ------------------------------------------------------------------
    # catalog access
    #
    # Every backend is asked the same three questions -- how many child rows,
    # how many distinct parents they reference, how wide a parent row is -- and
    # every backend has to be asked differently, because no standard exposes a
    # planner's cardinality estimates.  Reading them is the only vendor
    # dependent part; the crossover probe and the decision itself are not.
    #
    #   postgresql  pg_class.reltuples + pg_stats: all three, plus a true null
    #               fraction and real per-column widths.
    #   mysql       information_schema: TABLE_ROWS and index CARDINALITY are
    #               sampled estimates, AVG_ROW_LENGTH is on-disk width; there is
    #               no null fraction anywhere.
    #   sqlite      sqlite_stat1, which only exists after ANALYZE: rows and
    #               distinct, no null fraction, and widths only where the build
    #               includes the dbstat module.
    #
    # A backend that answers nothing falls through to --allow-scan.
    # ------------------------------------------------------------------

    def _unquote(self, name):
        return name.strip('"`[]')

    def _quote_identifier(self, name):
        escaped = name.replace('"', '""')
        return '"%s"' % escaped

    def _current_schema(self, conn):
        """The namespace an unqualified table name resolves in."""
        if conn.vendor == "sqlite":
            return None
        if not hasattr(self, "_schema_cache"):
            self._schema_cache = {}
        if conn.alias not in self._schema_cache:
            sql = ("SELECT current_schema()" if conn.vendor == "postgresql"
                   else "SELECT DATABASE()")
            with conn.cursor() as cur:
                cur.execute(sql)
                self._schema_cache[conn.alias] = cur.fetchone()[0]
        return self._schema_cache[conn.alias]

    def _split_table(self, conn, db_table):
        if "." in db_table:
            schema, table = db_table.split(".", 1)
            return self._unquote(schema), self._unquote(table)
        return self._current_schema(conn), self._unquote(db_table)

    def _catalog(self, conn, model, field):
        """(rows, distinct, null_frac, notes) for a FK column, or None.

        None means this backend has no usable statistics for the column, not
        that the column is empty -- the caller falls back rather than deciding.
        """
        reader = {
            "postgresql": self._pg_stats,
            "mysql": self._mysql_stats,
            "sqlite": self._sqlite_stats,
        }.get(conn.vendor)
        if reader is None:
            return None
        try:
            return reader(conn, model, field)
        except DatabaseError:
            # No read privilege on the statistics views, or a backend that
            # renamed them. Treated the same as never having been analyzed.
            return None

    def _catalog_width(self, conn, parent):
        reader = {
            "postgresql": self._pg_width,
            "mysql": self._mysql_width,
            "sqlite": self._sqlite_width,
        }.get(conn.vendor)
        if reader is None:
            return None
        try:
            return reader(conn, parent)
        except DatabaseError:
            return None

    def _parent_tables(self, parent):
        """Tables select_related would join for this model, ancestors included."""
        tables = []
        for fld in parent._meta.concrete_fields:
            db_table = fld.model._meta.db_table
            if db_table not in tables:
                tables.append(db_table)
        return tables

    # -- postgresql ----------------------------------------------------

    def _pg_reltuples(self, conn, schema, table):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.reltuples FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s",
                [schema, table],
            )
            row = cur.fetchone()
        # reltuples is -1 on PG14+ for a table that has never been analyzed.
        if row is None or row[0] is None or row[0] < 0:
            return None
        return float(row[0])

    def _pg_column_stats(self, conn, schema, table, columns):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT attname, n_distinct, avg_width, null_frac FROM pg_stats "
                "WHERE schemaname = %s AND tablename = %s AND attname = ANY(%s)",
                [schema, table, list(columns)],
            )
            return {r[0]: {"n_distinct": r[1], "avg_width": r[2], "null_frac": r[3]}
                    for r in cur.fetchall()}

    def _pg_stats(self, conn, model, field):
        schema, table = self._split_table(conn, model._meta.db_table)
        rows = self._pg_reltuples(conn, schema, table)
        entry = self._pg_column_stats(conn, schema, table, [field.column]).get(field.column)
        if not rows or not entry or entry["n_distinct"] is None:
            return None

        # Postgres reports a positive count, or a negative fraction of the
        # table when distinctness scales with row count.
        n_distinct = float(entry["n_distinct"])
        if n_distinct > 0:
            distinct = n_distinct
        elif n_distinct < 0:
            distinct = -n_distinct * rows
        else:
            return None

        return rows, distinct, float(entry["null_frac"] or 0.0), []

    def _pg_width(self, conn, parent):
        """Average bytes of one parent row, as select_related would fetch it.

        Grouped by table so multi-table inheritance -- where select_related also
        joins the ancestor tables -- is measured against the right relations.
        """
        by_table = {}
        for fld in parent._meta.concrete_fields:
            by_table.setdefault(fld.model._meta.db_table, []).append(fld.column)

        width = 0.0
        for db_table, columns in by_table.items():
            schema, table = self._split_table(conn, db_table)
            stats = self._pg_column_stats(conn, schema, table, columns)
            if not stats:
                return None
            for column in columns:
                entry = stats.get(column)
                if entry is None or entry["avg_width"] is None:
                    return None
                width += float(entry["avg_width"])
        return width

    # -- mysql ---------------------------------------------------------

    def _mysql_table_stats(self, conn, schema, table):
        """(row estimate, average on-disk row length), either possibly None."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_rows, avg_row_length FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s",
                [schema, table],
            )
            row = cur.fetchone()
        if row is None:
            return None, None
        # InnoDB derives both by sampling index pages, and reports 0 for a table
        # it has never sampled as readily as for an empty one. Neither is worth
        # a verdict, so 0 is treated as missing.
        return (float(row[0]) if row[0] else None,
                float(row[1]) if row[1] else None)

    def _mysql_stats(self, conn, model, field):
        schema, table = self._split_table(conn, model._meta.db_table)
        rows, _ = self._mysql_table_stats(conn, schema, table)
        if not rows:
            return None

        # seq_in_index = 1 keeps only indexes *led* by the column, where
        # cardinality counts that column on its own. Several can lead with it;
        # take the largest, because overstating the number of distinct parents
        # understates prefetch's win and so leaves the join in place unless it
        # is clearly wrong.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(cardinality) FROM information_schema.statistics "
                "WHERE table_schema = %s AND table_name = %s "
                "AND column_name = %s AND seq_in_index = 1",
                [schema, table, field.column],
            )
            row = cur.fetchone()
        if not row or not row[0]:
            return None
        distinct = float(row[0])

        notes = []
        if field.null:
            notes.append(
                "MySQL keeps no null fraction; assuming the column is never "
                "NULL, which overstates the join and so favours prefetch"
            )
        return rows, distinct, 0.0, notes

    def _mysql_width(self, conn, parent):
        """avg_row_length summed over the parent's tables.

        This is bytes on disk, including per-row storage overhead, so it runs a
        little wide compared to what actually crosses the wire.  It is the only
        width MySQL keeps.
        """
        width = 0.0
        for db_table in self._parent_tables(parent):
            schema, table = self._split_table(conn, db_table)
            _, table_width = self._mysql_table_stats(conn, schema, table)
            if not table_width:
                return None
            width += table_width
        return width

    # -- sqlite --------------------------------------------------------

    def _sqlite_stat1(self, conn, table):
        """ANALYZE's output for one table: {index name or None: [numbers]}.

        sqlite_stat1 does not exist at all until ANALYZE has run once, so a
        missing table here means "no statistics", not an error.
        """
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT idx, stat FROM sqlite_stat1 WHERE tbl = %s", [table])
            except DatabaseError:
                return {}
            rows = cur.fetchall()
        return {idx: self._leading_numbers(stat) for idx, stat in rows if stat}

    def _leading_numbers(self, stat):
        """The numeric prefix of a sqlite_stat1 stat string.

        ANALYZE appends flags like "unordered" and "sz=NNN" after the counts.
        """
        numbers = []
        for token in stat.split():
            try:
                numbers.append(float(token))
            except ValueError:
                break
        return numbers

    def _sqlite_leading_index(self, conn, table, column):
        """Name of an index whose first column is `column`, if one exists.

        PRAGMA takes no bound parameters, so the identifiers are quoted by hand.
        """
        with conn.cursor() as cur:
            cur.execute("PRAGMA index_list(%s)" % self._quote_identifier(table))
            names = [row[1] for row in cur.fetchall()]
            for name in names:
                cur.execute("PRAGMA index_info(%s)" % self._quote_identifier(name))
                info = sorted(cur.fetchall())  # (seqno, cid, column)
                if info and info[0][2] == column:
                    return name
        return None

    def _sqlite_stats(self, conn, model, field):
        table = self._unquote(model._meta.db_table)
        stat1 = self._sqlite_stat1(conn, table)
        if not stat1:
            return None

        rows = max((numbers[0] for numbers in stat1.values() if numbers), default=0.0)
        if not rows:
            return None

        index = self._sqlite_leading_index(conn, table, field.column)
        numbers = stat1.get(index) if index else None
        # An index stat reads "N k ...": N rows in the index, k rows per
        # distinct value of its first column. So N / k is the distinct count.
        if not numbers or len(numbers) < 2 or numbers[1] <= 0:
            return None
        distinct = numbers[0] / numbers[1]

        notes = []
        if field.null:
            notes.append(
                "SQLite keeps no null fraction; assuming the column is never "
                "NULL, which overstates the join and so favours prefetch"
            )
        return rows, distinct, 0.0, notes

    def _sqlite_width(self, conn, parent):
        """Average row payload from dbstat, where the build has it.

        dbstat is an optional module (SQLITE_ENABLE_DBSTAT_VTAB).  Without it
        SQLite keeps no width statistic of any kind and the caller assumes one.
        """
        width = 0.0
        with conn.cursor() as cur:
            for db_table in self._parent_tables(parent):
                cur.execute(
                    "SELECT SUM(payload) * 1.0 / NULLIF(SUM(ncell), 0) FROM dbstat "
                    "WHERE name = %s AND pagetype = 'leaf'",
                    [self._unquote(db_table)],
                )
                row = cur.fetchone()
                if row is None or row[0] is None:
                    return None
                width += float(row[0])
        return width

    # ------------------------------------------------------------------

    def _probe_connection(self, conn, options):
        """Bytes the connection could ship during one extra roundtrip."""
        override = options["breakeven_bytes"]
        if override is not None:
            return float(override)

        if not hasattr(self, "_breakeven_cache"):
            self._breakeven_cache = {}
        if conn.alias in self._breakeven_cache:
            return self._breakeven_cache[conn.alias]

        breakeven = float(DEFAULT_BREAKEVEN_BYTES)
        probe = PROBE_SQL.get(conn.vendor)
        if probe is not None:
            sql, per_byte = probe
            try:
                latencies = []
                with conn.cursor() as cur:
                    for _ in range(5):
                        start = perf_counter()
                        cur.execute("SELECT 1")
                        cur.fetchall()
                        latencies.append(perf_counter() - start)
                    latency = min(latencies)

                    start = perf_counter()
                    cur.execute(sql, [PROBE_PAYLOAD_BYTES // per_byte])
                    cur.fetchall()
                    elapsed = perf_counter() - start
                throughput = PROBE_PAYLOAD_BYTES / max(elapsed - latency, 1e-9)
                breakeven = latency * throughput
            except Exception:
                # A probe failure is not worth aborting the run over; the
                # default constant is still a usable crossover.
                pass

        self._breakeven_cache[conn.alias] = breakeven
        return breakeven

    # ------------------------------------------------------------------
    # estimation
    # ------------------------------------------------------------------

    def _estimate(self, candidate, conn, options):
        """Fanout and width for a forward FK, from the catalog if possible."""
        fld = candidate.field
        parent = fld.related_model

        rows = distinct = width = None
        null_frac = 0.0
        notes = []
        source = "unavailable"

        catalog = self._catalog(conn, candidate.model, fld)
        if catalog is not None:
            rows, distinct, null_frac, notes = catalog
            source = "catalog"

        if source == "unavailable" and options["allow_scan"]:
            manager = candidate.model._base_manager.using(conn.alias)
            rows = float(manager.count())
            non_null = manager.exclude(**{f"{fld.name}__isnull": True})
            distinct = float(non_null.values(fld.attname).distinct().count())
            null_frac = 0.0 if not rows else 1.0 - (float(non_null.count()) / rows)
            source = "scan"

        # Widths come from the catalog either way: --allow-scan replaces the
        # fanout counts, which is what a scan can measure cheaply, not the
        # per-row widths, which it cannot.
        #
        # This is the one input no backend is guaranteed to have, and the
        # fallback sets the scale of the whole comparison -- so it is recorded
        # rather than folded silently into the verdict.
        if source != "unavailable":
            width = self._catalog_width(conn, parent)
            if not width:
                width = float(options["assume_parent_width"])
                notes.append(
                    f"no width statistic for {parent._meta.label}; assuming "
                    f"{width:.0f}B per row (--assume-parent-width)"
                )

        estimate = Estimate(
            source=source,
            rows=rows,
            distinct=distinct,
            parent_width=width,
            null_frac=null_frac,
            notes=notes,
        )
        if source != "unavailable" and rows and distinct is not None and width:
            estimate.breakeven_bytes = self._probe_connection(conn, options)
            if (options["breakeven_bytes"] is None
                    and estimate.breakeven_bytes == float(DEFAULT_BREAKEVEN_BYTES)):
                notes.append(
                    f"could not measure the connection; assuming a roundtrip is "
                    f"worth {self._human(DEFAULT_BREAKEVEN_BYTES)} "
                    "(--breakeven-bytes)"
                )
            # Bytes prefetch avoids by not repeating the parent row per child.
            estimate.saved_bytes = width * (rows * (1.0 - null_frac) - distinct)
        return estimate

    # ------------------------------------------------------------------
    # benchmarking (calibration)
    # ------------------------------------------------------------------

    def _time_spec(self, model, spec, touch, options, using):
        """Time one queryset shape *including the attribute access*.

        Timing list(qs) without touching the relations compares one query
        against one query plus a join, which select_related can only lose.  The
        N+1 the hints exist to remove only happens on access.
        """
        manager = model._default_manager.using(using)
        queryset = manager.all()
        sample = options["sample"]
        if sample:
            queryset = queryset[:sample]
        if spec.select:
            queryset = queryset.select_related(*spec.select)
        if spec.prefetch:
            queryset = queryset.prefetch_related(*spec.prefetch)

        start = perf_counter()
        for obj in queryset:
            for name in touch:
                try:
                    getattr(obj, name)
                except ObjectDoesNotExist:
                    pass
        return perf_counter() - start

    def _measure(self, model, specs, touch, options, using, rounds=4):
        """Interleaved, min-of-k timings for a set of queryset shapes.

        Interleaved and rotated because running the shapes in blocks measures
        the buffer cache warming up and crowns whichever went last.  Min rather
        than mean because the noise here is one-sided: a run can be delayed by
        something else on the machine, never speeded up by it.

        This handles which shape gets the cold cache.  It does not, and cannot,
        produce a cold number for any of them -- see the module docstring.
        """
        results = {}
        deadline = perf_counter() + options["timeout"]

        for round_index in range(rounds):
            for offset in range(len(specs)):
                spec = specs[(round_index + offset) % len(specs)]
                if perf_counter() > deadline:
                    return results or None
                timing = results.setdefault(spec.label, Timing())

                if round_index == 0:
                    # The warm-up round is thrown away as a timing, which makes
                    # it the right place to count queries: wrapping a round that
                    # counts would put the counter inside the measurement.
                    timing.queries = self._count_queries(
                        model, spec, touch, options, using)
                    continue

                elapsed = self._time_spec(model, spec, touch, options, using)
                if timing.seconds is None or elapsed < timing.seconds:
                    timing.seconds = elapsed
        return results or None

    def _count_queries(self, model, spec, touch, options, using):
        """Queries one pass of `spec` costs, on every connection it touches.

        Watching only `using` undercounts: the router resolves a related model
        independently of the model that points at it, so an N+1 can land its
        thousand queries on a different alias than the one query that started it.
        """
        counted = {"n": 0}

        def wrapper(execute, sql, params, many, context):
            counted["n"] += 1
            return execute(sql, params, many, context)

        with ExitStack() as stack:
            for alias in connections:
                stack.enter_context(connections[alias].execute_wrapper(wrapper))
            self._time_spec(model, spec, touch, options, using)
        return counted["n"]

    def _field_specs(self, candidate):
        """The three shapes to compare for one relation."""
        return [
            PlanSpec("none"),
            PlanSpec(SELECT_RELATED, select=(candidate.name,)),
            PlanSpec(PREFETCH_RELATED, prefetch=(candidate.name,)),
        ]

    def _model_specs(self, candidates, combined):
        """The whole-model shapes to compare, and the label of the one we picked.

        The chosen plan often *is* one of the other three -- with a single FK it
        always is -- so it only gets its own row when it actually differs.
        Timing the same queryset twice measures noise, not a fourth option.
        """
        names = tuple(candidate.name for candidate in candidates)
        specs = [
            PlanSpec("N+1"),
            PlanSpec(SELECT_RELATED, select=names),
            PlanSpec(PREFETCH_RELATED, prefetch=names),
        ]
        chosen = (tuple(combined.select), tuple(combined.prefetch))
        for spec in specs:
            if spec.shape == chosen:
                return specs, spec.label
        specs.append(PlanSpec("chosen", select=chosen[0], prefetch=chosen[1]))
        return specs, "chosen"

    # ------------------------------------------------------------------
    # per-relation and per-model decisions
    # ------------------------------------------------------------------

    def _optimize_relation(self, candidate, options, using):
        conn = connections[using]
        estimate = self._estimate(candidate, conn, options)

        timings = None
        if not options["no_benchmark"]:
            try:
                timings = self._measure(
                    candidate.model, self._field_specs(candidate),
                    (candidate.name,), options, using,
                )
            except DatabaseError:
                # One unreadable table should cost its own timings, not the
                # whole report.
                timings = None

        if estimate.saved_bytes is None:
            if timings:
                plan = min(
                    (p for p in (SELECT_RELATED, PREFETCH_RELATED)
                     if p in timings and timings[p].seconds is not None),
                    key=lambda p: timings[p].seconds,
                    default=SELECT_RELATED,
                )
                reason = "measured (no statistics available)"
            else:
                plan = SELECT_RELATED
                reason = (
                    "no statistics; defaulting to the join. Run ANALYZE, or pass "
                    "--allow-scan"
                )
            return Recommendation(candidate, plan, reason, estimate, timings)

        if not estimate.distinct:
            # The column references nothing -- every row is NULL.  Either plan
            # fetches zero parent rows, so take the one without the roundtrip.
            return Recommendation(
                candidate,
                SELECT_RELATED,
                "every row is NULL; there is nothing to fetch either way",
                estimate,
                timings,
            )

        if estimate.saved_bytes > estimate.breakeven_bytes:
            plan = PREFETCH_RELATED
            reason = (
                f"fanout {estimate.fanout:.1f}:1 over a {estimate.parent_width:.0f}B "
                f"parent duplicates {self._human(estimate.saved_bytes)} across the "
                f"join, more than the {self._human(estimate.breakeven_bytes)} the "
                "extra roundtrip costs"
            )
        else:
            plan = SELECT_RELATED
            reason = (
                f"fanout {estimate.fanout:.1f}:1 over a {estimate.parent_width:.0f}B "
                f"parent duplicates only {self._human(estimate.saved_bytes)}, below "
                f"the {self._human(estimate.breakeven_bytes)} an extra roundtrip costs"
            )
        return Recommendation(candidate, plan, reason, estimate, timings)

    def _combine(self, model, recs):
        """Roll the per-field verdicts into one queryset call.

        See Combined's docstring for why this is a sum and not a search.
        """
        combined = Combined()
        for rec in recs:
            if rec.plan == SELECT_RELATED:
                combined.select.append(rec.candidate.name)
                if rec.estimate is not None and rec.estimate.join_bytes is not None:
                    combined.join_bytes = (combined.join_bytes or 0.0) + rec.estimate.join_bytes
            else:
                combined.prefetch.append(rec.candidate.name)
                if rec.estimate is not None and rec.estimate.saved_bytes is not None:
                    combined.saved_bytes = (combined.saved_bytes or 0.0) + rec.estimate.saved_bytes

        combined.queries = 1 + len(combined.prefetch)

        call = f"{model.__name__}.objects.all()"
        if combined.select:
            args = ", ".join(f'"{n}"' for n in combined.select)
            call += f".select_related({args})"
        if combined.prefetch:
            args = ", ".join(f'"{n}"' for n in combined.prefetch)
            call += f".prefetch_related({args})"
        combined.call = call if (combined.select or combined.prefetch) else ""
        return combined

    def _optimize_qs(self, model, options):
        using = router.db_for_read(model) or "default"
        candidates = self._candidates(model)
        recs = [
            self._optimize_relation(candidate, options, using)
            for candidate in candidates
        ]
        combined = self._combine(model, recs)

        # Time the model as a whole under every plan, the chosen one included,
        # so the recommendation is shown beating the alternatives instead of
        # only being argued for.
        plans, chosen_label = {}, "chosen"
        if not options["no_benchmark"]:
            specs, chosen_label = self._model_specs(candidates, combined)
            try:
                plans = self._measure(
                    model, specs, [c.name for c in candidates], options, using,
                ) or {}
            except DatabaseError:
                plans = {}

        return ModelReport(
            model=model,
            label=model._meta.label,
            using=using,
            recs=recs,
            combined=combined,
            plans=plans,
            chosen_label=chosen_label,
        )

    # ------------------------------------------------------------------
    # output
    # ------------------------------------------------------------------

    # The timing columns, left to right: what an unhinted queryset costs, then
    # each plan that claims to beat it.
    TIMED_PLANS = (("N+1", "none"), ("select", SELECT_RELATED), ("prefetch", PREFETCH_RELATED))
    TIMING_WIDTH = 11

    def _human(self, num_bytes):
        value = float(num_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if abs(value) < 1024 or unit == "GB":
                return f"{value:.1f}{unit}"
            value /= 1024

    def _duration(self, seconds):
        if seconds is None:
            return "--"
        if seconds >= 1.0:
            return f"{seconds:.2f}s"
        return f"{seconds * 1000:.1f}ms"

    def _database_info(self, alias):
        """What the verdicts were computed against.

        A recommendation is only true of the database it was measured on, and
        the router decides that per model, so the report names it rather than
        leaving the reader to assume "default".
        """
        conn = connections[alias]
        settings_dict = conn.settings_dict
        backend = conn.display_name
        if getattr(conn, "mysql_is_mariadb", False):
            backend = "MariaDB"
        try:
            version = ".".join(str(part) for part in conn.get_database_version())
        except Exception:
            # An older backend, or a driver that will not say. The server name
            # is the part that matters here.
            version = ""
        return {
            "alias": alias,
            "backend": backend,
            "version": version,
            "name": str(settings_dict.get("NAME") or ""),
            "host": settings_dict.get("HOST") or "",
            "port": str(settings_dict.get("PORT") or ""),
        }

    def _database_line(self, info):
        server = " ".join(part for part in (info["backend"], info["version"]) if part)
        target = info["name"]
        if info["host"]:
            target = f"{target} @ {info['host']}"
            if info["port"]:
                target = f"{target}:{info['port']}"
        described = f"{info['alias']} -- {server}"
        if target:
            described = f"{described} ({target})"
        return ("# " + self.style.MIGRATE_LABEL("database: ")
                + self.style.MIGRATE_HEADING(described))

    def _styled_plan(self, plan, text=None):
        style = self.style.WARNING if plan == PREFETCH_RELATED else self.style.SUCCESS
        return style(plan if text is None else text)

    def _styled_call(self, report):
        """The runnable line, coloured the way Django colours SQL."""
        combined = report.combined
        parts = [
            self.style.SQL_TABLE(report.model.__name__),
            ".objects.",
            self.style.SQL_KEYWORD("all"),
            "()",
        ]
        for method, names in ((SELECT_RELATED, combined.select),
                              (PREFETCH_RELATED, combined.prefetch)):
            if names:
                args = ", ".join(self.style.SQL_FIELD(f'"{n}"') for n in names)
                parts.append("." + self.style.SQL_KEYWORD(method) + "(" + args + ")")
        return "".join(parts)

    def _as_dict(self, rec):
        payload = {
            "model": rec.candidate.model._meta.label,
            "field": rec.candidate.name,
            "plan": rec.plan,
            "reason": rec.reason,
        }
        if rec.estimate is not None:
            payload["estimate"] = {
                "source": rec.estimate.source,
                "rows": rec.estimate.rows,
                "distinct": rec.estimate.distinct,
                "fanout": rec.estimate.fanout,
                "parent_width": rec.estimate.parent_width,
                "null_frac": rec.estimate.null_frac,
                "saved_bytes": rec.estimate.saved_bytes,
                "breakeven_bytes": rec.estimate.breakeven_bytes,
                "assumptions": rec.estimate.notes,
            }
        if rec.timings is not None:
            payload["timings"] = {
                label: {"seconds": timing.seconds, "queries": timing.queries}
                for label, timing in rec.timings.items()
            }
        return payload

    def _render(self, reports, options, elapsed=None):
        """One runnable queryset per model, over what each plan actually cost.

        Everything that is not a queryset is commented, so the whole report
        still pastes into a file as-is.
        """
        verbosity = options["verbosity"]
        aliases = []
        for report in reports:
            if report.using not in aliases:
                aliases.append(report.using)

        if verbosity >= 1:
            for alias in aliases:
                self.stdout.write(self._database_line(self._database_info(alias)))
            self.stdout.write("# " + self.style.MIGRATE_LABEL("timings: ")
                              + self._timing_caption(options))
            self.stdout.write("#")

        for report in reports:
            self._render_model(report, options, show_alias=len(aliases) > 1)

        if verbosity >= 2:
            self.stdout.write(self.style.NOTICE(
                "# These apply per call site: use the plan where a queryset actually "
                "iterates and touches the relation. They are not defaults to bake "
                "into a manager."
            ))

        if verbosity >= 1 and elapsed is not None:
            relations = sum(len(report.recs) for report in reports)
            self.stdout.write(
                "# " + self.style.MIGRATE_LABEL("total: ")
                + self.style.MIGRATE_HEADING(
                    f"{self._duration(elapsed)} for {len(reports)} model"
                    f"{'' if len(reports) == 1 else 's'}, "
                    f"{relations} relation{'' if relations == 1 else 's'}"
                )
            )

    def _timing_caption(self, options):
        if options["no_benchmark"]:
            return "not measured (--no-benchmark)"
        rows = options["sample"] or "all"
        return (f"best of 3 warm runs over {rows} rows, relations touched on every "
                "row; calibration, not the verdict")

    def _render_model(self, report, options, show_alias=False):
        verbosity = options["verbosity"]
        recs = report.recs

        if verbosity >= 1:
            heading = report.label
            if show_alias:
                heading = f"{heading}  (via {report.using})"
            self.stdout.write("# " + self.style.MIGRATE_HEADING(heading))

            name_width = max([len("field")] + [len(r.candidate.name) for r in recs])
            header = "#   " + "field".ljust(name_width)
            for title, _ in self.TIMED_PLANS:
                header += title.rjust(self.TIMING_WIDTH)
            header += "   plan"
            self.stdout.write(self.style.MIGRATE_LABEL(header))

            for rec in recs:
                self._render_row(rec, options, name_width)

            self._render_model_plans(report)

        if report.combined.call:
            self.stdout.write(self._styled_call(report))
            # At -v 1 the call is nearly all that is on screen, so say when part
            # of what produced it was assumed rather than measured.
            if verbosity == 1 and any(r.estimate and r.estimate.notes for r in recs):
                self.stdout.write("#   " + self.style.WARNING(
                    "rests on assumed values; -v 2 says which"))

        if verbosity >= 2:
            summary = [f"{report.combined.queries} quer"
                       + ("y" if report.combined.queries == 1 else "ies")]
            if report.combined.saved_bytes:
                summary.append(
                    f"{self._human(report.combined.saved_bytes)} less than joining everything"
                )
            if report.combined.join_bytes:
                summary.append(
                    f"{self._human(report.combined.join_bytes)} carried by the joins"
                )
            self.stdout.write("#   " + ", ".join(summary))

        if verbosity >= 1:
            self.stdout.write("#")

    def _render_model_plans(self, report):
        """The whole model, timed under every plan including the chosen one."""
        if not report.plans:
            return

        label_width = max([len("whole model")] + [len(l) for l in report.plans])
        header = ("#   " + "whole model".ljust(label_width)
                  + "total".rjust(self.TIMING_WIDTH) + "queries".rjust(9))
        self.stdout.write(self.style.MIGRATE_LABEL(header))

        timed = {label: timing.seconds for label, timing in report.plans.items()
                 if timing.seconds is not None}
        fastest = min(timed, key=timed.get) if timed else None

        for label, timing in report.plans.items():
            row = "#   " + label.ljust(label_width)
            cell = self._duration(timing.seconds).rjust(self.TIMING_WIDTH)
            row += self.style.SUCCESS(cell) if label == fastest else cell
            row += ("--" if timing.queries is None else str(timing.queries)).rjust(9)
            if label == report.chosen_label:
                row += "   " + self.style.SUCCESS("<- picked")
            self.stdout.write(row)

        if (fastest is not None and fastest != report.chosen_label
                and report.chosen_label in timed
                and timed[fastest] < timed[report.chosen_label] * 0.9):
            self.stdout.write("#       " + self.style.NOTICE(
                f"{fastest} beat the chosen plan on this sample by "
                f"{self._duration(timed[report.chosen_label] - timed[fastest])}"
            ))

    def _render_row(self, rec, options, name_width):
        timings = rec.timings or {}
        # Fastest of the two real plans, for highlighting only. The verdict
        # comes from the estimate; see _timing_caption for why.
        measured = {plan: timings[plan].seconds
                    for plan in (SELECT_RELATED, PREFETCH_RELATED)
                    if plan in timings and timings[plan].seconds is not None}
        fastest = min(measured, key=measured.get) if measured else None

        row = "#   " + rec.candidate.name.ljust(name_width)
        for _, key in self.TIMED_PLANS:
            seconds = timings[key].seconds if key in timings else None
            # Pad before styling: the escape codes are not printable width.
            cell = self._duration(seconds).rjust(self.TIMING_WIDTH)
            row += self._styled_plan(key, cell) if key == fastest else cell
        row += "   " + self._styled_plan(rec.plan)
        self.stdout.write(row)

        indent = "#       "
        if fastest is not None and fastest != rec.plan and rec.plan in measured:
            # Ten percent of a millisecond-scale sample is noise; below that the
            # two plans measured the same and there is nothing to report.
            if measured[fastest] < measured[rec.plan] * 0.9:
                self.stdout.write(indent + self.style.NOTICE(
                    f"measured {fastest} faster on this sample than the "
                    f"{rec.plan} the estimate picked"
                ))

        if options["verbosity"] >= 2:
            self.stdout.write(indent + rec.reason)
            for note in (rec.estimate.notes if rec.estimate else []):
                self.stdout.write(indent + self.style.WARNING(f"assumed: {note}"))

    # ------------------------------------------------------------------

    def handle(self, *args, **options):

        started = perf_counter()
        selection: str | None = options["app.model"]
        model_s: list[type[Model]] = []

        try:
            if selection is None:
                model_s = list(apps.get_models())
                if not options["django_models"]:
                    local_apps = {ac.label for ac in apps.get_app_configs()
                            if not ac.name.startswith("django.")}
                    model_s = [mdl for mdl in model_s if mdl._meta.app_label in local_apps]

            elif "." in selection:
                model_s.append(apps.get_model(selection))

            else:
                for mdl in apps.get_app_config(selection).get_models():
                    model_s.append(mdl)

        except (LookupError, ValueError) as e:
            raise CommandError(str(e)) from e

        # Only keep models with a forward FK. Everything else has a fixed answer
        # and is reported by fk_optimize, not here.
        model_s = [mdl for mdl in model_s if self._candidates(mdl)]

        if not model_s:
            self.stdout.write(
                "No model found with a forward foreign key. Every other relation "
                "kind has a fixed answer -- see fk_optimize for the full survey."
            )
            return

        reports = []
        for mdl in model_s:
            report = self._optimize_qs(mdl, options)
            if report.recs:
                reports.append(report)

        if options["json"]:
            payload = {
                report.label: {
                    "database": self._database_info(report.using),
                    "fields": [self._as_dict(r) for r in report.recs],
                    "combined": {
                        "select_related": report.combined.select,
                        "prefetch_related": report.combined.prefetch,
                        "queries": report.combined.queries,
                        "join_bytes": report.combined.join_bytes,
                        "saved_bytes": report.combined.saved_bytes,
                        "call": report.combined.call,
                    },
                    "plans": {
                        label: {"seconds": timing.seconds, "queries": timing.queries,
                                "chosen": label == report.chosen_label}
                        for label, timing in report.plans.items()
                    },
                }
                for report in reports
            }
            payload["_total_seconds"] = perf_counter() - started
            self.stdout.write(json.dumps(payload, indent=2))
        else:
            self._render(reports, options, elapsed=perf_counter() - started)
