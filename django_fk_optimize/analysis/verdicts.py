"""Join what the scanner saw to what the database was actually asked for.

Neither half is enough on its own.  The scanner knows *which line* iterates
*which model* and *touches which relation*, and it cannot know how many rows
that is.  The recording knows how many rows, how long it took, and catches the
access paths an AST can never see -- a template expression, a serializer field
-- and it does not know what code to change.  The join is where a finding
becomes a fix.

Three things this module is careful about, each of which is a way to be
confidently wrong:

**The two halves file the same event on different lines.**  The scanner files
the call site at the line that built the queryset; the recorder attributes the
lazy load to the line that touched the relation, several lines below it.  So
the join is on the *enclosing scope*, narrowest first, and never on the line.

**A scope match is a coincidence until the table agrees.**  A function that
builds three querysets contains three call sites and one of them is the one
that issued the query.  Resolving the site's touched relation to its target
table and requiring that table to be the one the repeated query selected from
is what separates the match from the coincidence.  Scope and table both ->
`resolved`.  One of the two -> `probable`.  The label is printed, always.

**A hint the scanner cannot see is not an unused hint.**  `touched` only ever
holds forward many-to-one relations, so `prefetch_related("tags")` looks unused
to a scanner that cannot see an m2m touch.  Only a hint naming a forward
relation of the model is ever reported as removable.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from django.db import DatabaseError

from ..recording.store import BULK, QueryGroup
from ..recording.wrapper import PYTHON, SERIALIZER, TEMPLATE
from ..utils.callsites import PROBABLE, RESOLVED, CallSite
from ..utils.vocabulary import Vocabulary
from .benchmark import (
    DEFAULT_SAMPLE_SIZE,
    MEASURED,
    STRUCTURAL,
    Deadline,
    FieldOperation,
    Measurement,
    plan_named,
)
from .cardinality import Cardinality

# Where the row count came from, in the order it is preferred.
OBSERVED = "observed"
STATIC_BOUND = "static bound"
ESTIMATED = "estimated"
UNKNOWN = "unknown"

# What a verdict says to do.
N_PLUS_ONE = "n_plus_one"
EXTRA_QUERY = "extra_query"
REMOVE_HINT = "remove_hint"
SWITCH_TO_SELECT = "switch_to_select_related"
KEEP_PREFETCH = "keep_prefetch"
ALREADY_HINTED = "already_hinted"
ID_ONLY = "id_only"

# Verdicts that mean "change this". --fail-on-findings fires on exactly these.
ACTIONABLE_KINDS = frozenset({N_PLUS_ONE, EXTRA_QUERY, REMOVE_HINT, SWITCH_TO_SELECT})
# Verdicts whose alternative is worth timing.
COSTED_KINDS = frozenset({N_PLUS_ONE, EXTRA_QUERY, SWITCH_TO_SELECT})


# ----------------------------------------------------------------------
# tables
# ----------------------------------------------------------------------


@dataclass
class Tables:
    """`db_table` <-> model label, both ways.

    The table name is the only thing the recording and the model registry have
    in common: a recorded query carries a table, a static call site carries a
    relation, and `Relation.target -> model -> _meta.db_table` is the bridge.
    """

    by_label: dict[str, str] = dataclass_field(default_factory=dict)
    by_table: dict[str, str] = dataclass_field(default_factory=dict)

    @classmethod
    def from_models(cls, model_classes) -> Tables:
        tables = cls()
        for model in model_classes:
            meta = model._meta
            tables.by_label[meta.label] = meta.db_table
            # A proxy model shares its parent's table, so the reverse map keeps
            # the concrete model: a query on that table is the parent's query.
            if meta.db_table not in tables.by_table or not meta.proxy:
                tables.by_table.setdefault(meta.db_table, meta.label)
                if not meta.proxy:
                    tables.by_table[meta.db_table] = meta.label
        return tables

    @classmethod
    def from_apps(cls, include_django: bool = False) -> Tables:
        from django.apps.registry import apps

        local = {
            config.label
            for config in apps.get_app_configs()
            if include_django or not config.name.startswith("django.")
        }
        return cls.from_models(
            model for model in apps.get_models() if model._meta.app_label in local
        )

    def table(self, label: str) -> str | None:
        return self.by_label.get(label)

    def label(self, table: str | None) -> str | None:
        return self.by_table.get(table) if table else None


# ----------------------------------------------------------------------
# the join
# ----------------------------------------------------------------------


def _key(path: str) -> str:
    """One spelling for a path, so a scan and a traceback can be compared."""
    if not path:
        return ""
    return os.path.normcase(os.path.abspath(path))


@dataclass(frozen=True)
class Match:
    """One recorded N+1, and the call site it was traced back to."""

    group: QueryGroup
    site: CallSite | None = None
    model: str = ""
    relation: str = ""
    candidates: tuple[tuple[str, str], ...] = ()
    by_scope: bool = False
    by_table: bool = False
    confidence: str = PROBABLE

    @property
    def runtime_only(self) -> bool:
        return self.site is None

    @property
    def named(self) -> bool:
        return bool(self.model and self.relation)


@dataclass
class JoinResult:
    matched: list[Match] = dataclass_field(default_factory=list)
    runtime_only: list[Match] = dataclass_field(default_factory=list)
    unattributed: list[QueryGroup] = dataclass_field(default_factory=list)

    @property
    def observed(self) -> int:
        return len(self.matched) + len(self.runtime_only)


def _forward(site: CallSite, vocabulary: Vocabulary):
    info = vocabulary.models.get(site.model)
    if info is None:
        return {}
    return {
        name: relation
        for name in site.touched
        if (relation := info.relation(name)) is not None
    }


def _confirm(site, table, vocabulary, tables) -> str:
    """The touched relation whose target table is `table`, or ""."""
    for name, relation in _forward(site, vocabulary).items():
        if table and tables.table(relation.target) == table:
            return name
    return ""


def _sole_missing(site, vocabulary) -> str:
    """The one unhinted forward relation, when there is only one.

    A hinted relation issues no lazy query, so a recorded N+1 at this site can
    only have come from an unhinted one.  With exactly one of those, the name
    follows without a table to confirm it -- at `probable`, because "only one
    candidate" is a deduction and not an observation.
    """
    missing = [name for name in site.missing if name in _forward(site, vocabulary)]
    return missing[0] if len(missing) == 1 else ""


def _match_confidence(site: CallSite, by_table: bool) -> str:
    if not by_table:
        return PROBABLE
    if site.confidence != RESOLVED or site.escapes or site.hints.opaque:
        return PROBABLE
    return RESOLVED


def _span(site: CallSite) -> int:
    return max(site.scope_end - site.scope_start, 0)


def _infer(group, vocabulary, tables, parents) -> tuple[tuple[str, str], ...]:
    """(model label, relation) pairs that could have produced this query.

    With no static site there is nothing to confirm against but the table, so
    the answer is every forward FK that points at it.  Narrowed by the *bulk*
    queries the same recording holds: an N+1 is a set of single-row lookups
    hanging off one query that fetched a page of parent rows, and only a model
    that was itself selected in bulk can be that parent.
    """
    target = tables.label(group.table)
    if target is None:
        return ()
    candidates = tuple(
        (info.label, relation.name)
        for info in vocabulary.models.values()
        for relation in info.relations.values()
        if relation.target == target
    )
    seen = tuple(pair for pair in candidates if tables.table(pair[0]) in parents)
    return seen or candidates


def join(groups, sites, vocabulary, tables) -> JoinResult:
    """Attach every recorded N+1 to the call site that caused it, if any."""
    by_file: dict[str, list[CallSite]] = {}
    for site in sites:
        by_file.setdefault(_key(site.path), []).append(site)

    groups = list(groups)
    parents = {group.table for group in groups if group.table and group.kind == BULK}
    result = JoinResult()

    for group in groups:
        if not group.is_n_plus_one:
            continue
        match = _match(group, by_file, vocabulary, tables)
        if match is not None:
            result.matched.append(match)
            continue
        candidates = _infer(group, vocabulary, tables, parents)
        if not candidates:
            result.unattributed.append(group)
            continue
        model, relation = candidates[0] if len(candidates) == 1 else ("", "")
        result.runtime_only.append(
            Match(
                group=group,
                model=model,
                relation=relation,
                candidates=candidates,
                by_table=True,
                confidence=RESOLVED if len(candidates) == 1 else PROBABLE,
            )
        )
    return result


def _match(group, by_file, vocabulary, tables) -> Match | None:
    line = group.attribution.line
    file_sites = by_file.get(_key(group.attribution.file), ())
    containing = [site for site in file_sites if site.contains_line(line)]

    if containing:
        # Narrowest containing scope first: a nested function's span sits
        # inside its parent's, and the inner one is where the code is.
        narrowest = min(_span(site) for site in containing)
        finalists = sorted(
            (site for site in containing if _span(site) == narrowest),
            key=lambda site: abs(site.line - line),
        )
        for site in finalists:
            relation = _confirm(site, group.table, vocabulary, tables)
            if relation:
                return Match(
                    group=group,
                    site=site,
                    model=site.model,
                    relation=relation,
                    by_scope=True,
                    by_table=True,
                    confidence=_match_confidence(site, True),
                )
        site = finalists[0]
        return Match(
            group=group,
            site=site,
            model=site.model,
            relation=_sole_missing(site, vocabulary),
            by_scope=True,
            by_table=False,
            confidence=PROBABLE,
        )

    # No scope contains the line -- an inlined frame, a decorator, a moved
    # file.  A site in the same file touching the same table is still worth
    # something, and is worth exactly `probable`.
    for site in file_sites:
        relation = _confirm(site, group.table, vocabulary, tables)
        if relation:
            return Match(
                group=group,
                site=site,
                model=site.model,
                relation=relation,
                by_scope=False,
                by_table=True,
                confidence=PROBABLE,
            )
    return None


# ----------------------------------------------------------------------
# verdicts
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Rows:
    """N, and how it was arrived at. The second half is never dropped."""

    n: int
    provenance: str = UNKNOWN

    @property
    def known(self) -> bool:
        return self.provenance != UNKNOWN

    def __str__(self):
        return f"{self.n} ({self.provenance})"


@dataclass
class Verdict:
    kind: str
    model: str
    relation: str
    rows: Rows
    confidence: str = PROBABLE
    actionable: bool = False

    # where
    file: str = ""
    line: int = 0
    function: str = ""
    expression: str = ""
    source: str = PYTHON
    runtime_only: bool = False

    # what it costs now, as recorded
    observed_queries: int | None = None
    observed_seconds: float | None = None

    # what it costs now and what it could cost, as measured
    current: Measurement | None = None
    # Whether the pick came off the clock or off the relation kind. A timing
    # over too few rows is noise, and a pick made from noise must not print
    # like a pick made from evidence.
    basis: str = MEASURED
    best_strategy: str = ""
    best: Measurement | None = None
    alternative_strategy: str = ""
    alternative: Measurement | None = None

    headline: str = ""
    fix: str = ""
    notes: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.file else "(unattributed)"

    @property
    def target(self) -> str:
        return f"{self.model}.{self.relation}" if self.relation else self.model

    @property
    def measured(self) -> bool:
        return self.current is not None and self.best is not None

    @property
    def saved_seconds(self) -> float | None:
        if not self.measured:
            return None
        return self.current.seconds - self.best.seconds

    @property
    def saved_queries(self) -> int | None:
        if not self.measured:
            return None
        return self.current.queries - self.best.queries

    @property
    def saved_share(self) -> float | None:
        if not self.measured or not self.current.seconds:
            return None
        return self.saved_seconds / self.current.seconds

    @property
    def sort_key(self):
        return (
            not self.actionable,
            -(self.saved_queries or 0),
            -self.rows.n,
            self.file,
            self.line,
            self.relation,
        )


# -- the one-line code change ------------------------------------------

_TERMINAL = re.compile(r"\.(?:get|first|last|earliest|latest)\(")
_SUBSCRIPT = re.compile(r"\[[^\[\]]*\]\s*$")
_ALIAS = re.compile(r"\s+\(as \w+\)$")


def expression_of(site: CallSite | None) -> str:
    if site is None:
        return ""
    return _ALIAS.sub("", site.expression or "").strip()


def with_hint(expression: str, method: str, name: str) -> str:
    """`expression` with `.method("name")` spliced in where it is legal.

    Not appended: `Book.objects.get(pk=1).select_related("author")` is an
    AttributeError on a model instance, and `qs[:50].select_related(...)` is a
    TypeError on a sliced queryset.  The hint has to go before whichever of
    those ends the chain.
    """
    insert = f'.{method}("{name}")'
    if not expression or expression.startswith("<"):
        return insert.lstrip(".")
    head, tail = expression, ""
    subscript = _SUBSCRIPT.search(head)
    if subscript:
        head, tail = head[: subscript.start()], head[subscript.start() :] + tail
    terminals = list(_TERMINAL.finditer(head))
    if terminals:
        cut = terminals[-1].start()
        head, tail = head[:cut], head[cut:] + tail
    return head + insert + tail


def without_hint(expression: str, method: str, name: str) -> str:
    """`expression` with one name dropped from one hint call.

    Drops the whole call when that name was its only argument, and only the
    argument when it was not; a `select_related("a", "b")` with "b" unused is
    still doing useful work for "a".
    """
    quoted = {f'"{name}"', f"'{name}'"}
    pattern = re.compile(rf"\.{method}\(([^()]*)\)")

    def replace(match):
        args = [arg.strip() for arg in match.group(1).split(",") if arg.strip()]
        kept = [arg for arg in args if arg not in quoted]
        if kept == args:
            return match.group(0)
        if not kept:
            return ""
        return f".{method}({', '.join(kept)})"

    changed = pattern.sub(replace, expression)
    if changed == expression:
        return f'drop .{method}("{name}") from {expression}'
    return changed


def fit(verdict: Verdict, *, prefetch: bool = False) -> None:
    """(Re)write the verdict's one-line fix for its current best strategy."""
    method = (
        FieldOperation.PREFETCH_RELATED.value
        if prefetch
        else FieldOperation.SELECT_RELATED.value
    )
    if verdict.kind in (N_PLUS_ONE, EXTRA_QUERY):
        if verdict.runtime_only:
            where = f"{verdict.function}()" if verdict.function else "the view"
            verdict.fix = (
                f'add .{method}("{verdict.relation}") to the '
                f"{verdict.model} queryset in {where}"
                if verdict.relation
                else f"add a select_related() for the query in {where}"
            )
        else:
            verdict.fix = with_hint(verdict.expression, method, verdict.relation)
    elif verdict.kind == SWITCH_TO_SELECT:
        dropped = without_hint(
            verdict.expression,
            FieldOperation.PREFETCH_RELATED.value,
            verdict.relation,
        )
        verdict.fix = with_hint(
            dropped, FieldOperation.SELECT_RELATED.value, verdict.relation
        )
    verdict.best_strategy = verdict.best_strategy or method


CardinalityFor = Callable[[str, str], "Cardinality | None"]


def build(
    sites: Iterable[CallSite],
    groups: Iterable[QueryGroup],
    vocabulary: Vocabulary,
    tables: Tables,
    *,
    cardinality: CardinalityFor | None = None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    join_result: JoinResult | None = None,
) -> tuple[list[Verdict], JoinResult]:
    """Every verdict the evidence supports, worst first."""
    sites = list(sites)
    result = join_result or join(groups, sites, vocabulary, tables)

    observed: dict[tuple[int, str], Match] = {}
    for match in result.matched:
        if match.relation:
            observed.setdefault((id(match.site), match.relation), match)

    verdicts: list[Verdict] = []
    for site in sites:
        verdicts.extend(
            _site_verdicts(site, observed, vocabulary, cardinality, sample_size)
        )
    for match in result.runtime_only:
        verdicts.append(_runtime_verdict(match))

    verdicts.sort(key=lambda verdict: verdict.sort_key)
    return verdicts, result


def _rows(site, relation, observed, cardinality, sample_size) -> Rows:
    """N, in the order Trap D lays down: observed, then bound, then estimated."""
    match = observed.get((id(site), relation))
    if match is not None:
        return Rows(match.group.count, OBSERVED)
    if site.bound is not None:
        return Rows(site.bound, STATIC_BOUND)
    stats = cardinality(site.model, relation) if cardinality else None
    if stats is not None:
        return Rows(stats.estimate(sample_size), ESTIMATED)
    return Rows(0, UNKNOWN)


def _base(site: CallSite, kind: str, relation: str, rows: Rows) -> Verdict:
    return Verdict(
        kind=kind,
        model=site.model,
        relation=relation,
        rows=rows,
        file=site.path,
        line=site.line,
        function=site.function,
        expression=expression_of(site),
        confidence=site.confidence,
        notes=site.notes,
    )


def _soften(verdict: Verdict, site: CallSite) -> None:
    """A site the scanner could not fully account for never asserts a fix.

    `escapes` means the row was handed to a function this scan cannot follow,
    so the touches recorded here are a lower bound.  `opaque` means a hint's
    arguments were not literals, so what it covers is unknown.  Either way the
    finding is worth printing and is not worth acting on unread.
    """
    if site.escapes or site.hints.opaque:
        verdict.confidence = PROBABLE
        verdict.actionable = False
        reason = (
            "the row escapes into a call"
            if site.escapes
            else "a hint's arguments are not literals"
        )
        verdict.notes = verdict.notes + (
            f"not asserted: {reason}, so this site is only partly understood",
        )


def _site_verdicts(site, observed, vocabulary, cardinality, sample_size):
    info = vocabulary.models.get(site.model)
    if info is None:
        return []
    out: list[Verdict] = []
    missing = set(site.missing)

    for relation in site.touched:
        if info.relation(relation) is None:
            continue
        if relation in missing:
            out.append(_unhinted(site, relation, observed, cardinality, sample_size))
        elif relation in site.hints.prefetch:
            out.append(_prefetched(site, relation, observed, cardinality, sample_size))
        else:
            verdict = _base(
                site,
                ALREADY_HINTED,
                relation,
                _rows(site, relation, observed, cardinality, sample_size),
            )
            verdict.headline = f'already covered by select_related("{relation}")'
            out.append(verdict)

    for relation in site.unused:
        # A hint the scanner cannot see a touch for is not an unused hint:
        # `touched` only ever holds forward many-to-one relations.
        if info.relation(relation) is None:
            continue
        method = (
            FieldOperation.SELECT_RELATED.value
            if relation in site.hints.select
            else FieldOperation.PREFETCH_RELATED.value
        )
        verdict = _base(site, REMOVE_HINT, relation, Rows(0, UNKNOWN))
        verdict.actionable = True
        verdict.headline = f'{method}("{relation}") is never used here'
        verdict.fix = without_hint(verdict.expression, method, relation)
        _soften(verdict, site)
        out.append(verdict)

    for attname in site.id_only:
        relation = next(
            (rel.name for rel in info.relations.values() if rel.attname == attname),
            "",
        )
        if not relation:
            continue
        verdict = _base(site, ID_ONLY, relation, Rows(0, UNKNOWN))
        verdict.headline = f"{attname} is already on the row; nothing to do"
        out.append(verdict)

    return out


def _unhinted(site, relation, observed, cardinality, sample_size) -> Verdict:
    rows = _rows(site, relation, observed, cardinality, sample_size)
    match = observed.get((id(site), relation))
    kind = N_PLUS_ONE if rows.n > 1 else EXTRA_QUERY
    verdict = _base(site, kind, relation, rows)
    verdict.actionable = True
    if match is not None:
        verdict.confidence = match.confidence
        verdict.observed_queries = match.group.count
        verdict.observed_seconds = match.group.total_seconds
        verdict.source = match.group.source
        if not match.by_table:
            verdict.notes = verdict.notes + (
                "matched on the enclosing scope only; the table did not confirm it",
            )
    verdict.headline = (
        f"{rows.n} extra queries, one per row"
        if kind == N_PLUS_ONE
        else "one extra query, and a join would carry it for free"
    )
    fit(verdict)
    _soften(verdict, site)
    return verdict


def _prefetched(site, relation, observed, cardinality, sample_size) -> Verdict:
    rows = _rows(site, relation, observed, cardinality, sample_size)
    stats = cardinality(site.model, relation) if cardinality else None
    if stats is not None and stats.counted and not stats.batches_well:
        verdict = _base(site, SWITCH_TO_SELECT, relation, rows)
        verdict.actionable = True
        verdict.headline = (
            f'prefetch_related("{relation}") returns '
            f"{stats.distinct_ratio:.0%} as many rows as the parent query; "
            "a join carries them for free"
        )
        fit(verdict)
        _soften(verdict, site)
        return verdict

    verdict = _base(site, KEEP_PREFETCH, relation, rows)
    if stats is not None and stats.counted:
        verdict.headline = (
            f'prefetch_related("{relation}") is the right hint: '
            f"{stats.present} rows share {stats.distinct} targets"
        )
    else:
        verdict.headline = f'prefetch_related("{relation}") covers this touch'
        verdict.notes = verdict.notes + (
            "not counted, so prefetch was not compared against a join",
        )
    return verdict


def _runtime_verdict(match: Match) -> Verdict:
    """A finding with no static call site: a template or a serializer.

    Reported at the deepest user frame, which for a template render is the view
    -- which is also where the queryset was built and where the fix goes.
    """
    group = match.group
    verdict = Verdict(
        kind=N_PLUS_ONE,
        model=match.model,
        relation=match.relation,
        rows=Rows(group.count, OBSERVED),
        confidence=match.confidence,
        actionable=bool(match.named),
        file=group.attribution.file,
        line=group.attribution.line,
        function=group.attribution.function,
        source=group.source,
        runtime_only=True,
        observed_queries=group.count,
        observed_seconds=group.total_seconds,
        candidates=tuple(f"{label}.{name}" for label, name in match.candidates),
    )
    where = {
        TEMPLATE: "a template expression",
        SERIALIZER: "a serializer field",
    }.get(group.source, "code the scanner could not read")
    verdict.headline = (
        f"{group.count} extra queries from {where}; no call site to match"
    )
    if not match.named:
        verdict.notes = verdict.notes + (
            "several relations point at this table; the candidates are listed",
        )
    fit(verdict)
    return verdict


# ----------------------------------------------------------------------
# costing the alternative
# ----------------------------------------------------------------------


def _brief(exc: Exception, limit: int = 70) -> str:
    """The first line of a database error, short enough to sit in a note."""
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


@dataclass(frozen=True)
class Costed:
    """How much of the costing actually happened.

    `skipped` is not a tidy-up detail.  A run that priced 3 of 40 relations and
    a run that priced all 40 print the same verdicts, and only this number
    tells them apart.
    """

    timed: int = 0
    skipped: int = 0


def cost(
    verdicts: Iterable[Verdict],
    benchmark,
    model_for: Callable[[str], object | None],
    deadline: Deadline | None = None,
) -> Costed:
    """Time each verdict's alternative over a slice the size of its own N.

    Timed at the verdict's N, not at `--sample-size`: a loop over twelve rows
    is not explained by a measurement of five hundred.
    """
    timed = skipped = 0
    for verdict in verdicts:
        if deadline is not None and deadline.expired():
            break
        if verdict.kind not in COSTED_KINDS or not verdict.relation:
            continue
        model = model_for(verdict.model)
        if model is None:
            continue
        plan = plan_named(model, verdict.relation)
        if plan is None:
            continue
        rows = verdict.rows.n if verdict.rows.known and verdict.rows.n else 1
        try:
            result = benchmark.at(rows).compare(model, plan)
        except DatabaseError as exc:
            # A model whose migration has not been applied here, a table the
            # connecting role cannot read, a column that has been renamed.  A
            # development database is half-migrated more often than not, and
            # one unreadable table is no reason to abandon the other thirty-
            # nine: the verdict keeps its static half and says why it has no
            # numbers.
            verdict.notes = verdict.notes + (
                f"not timed: the database would not read this table ({_brief(exc)})",
            )
            skipped += 1
            continue
        offers = result.ranked()
        if not offers:
            continue
        verdict.current = result.get(FieldOperation.VANILLA)
        verdict.basis = result.basis
        verdict.best_strategy, verdict.best = offers[0][0].value, offers[0][1]
        if len(offers) > 1:
            verdict.alternative_strategy = offers[1][0].value
            verdict.alternative = offers[1][1]
        if result.basis == STRUCTURAL:
            verdict.notes = verdict.notes + (
                f"{result.rows} rows is too few to time; the hint comes from "
                "the relation kind, not from the clock",
            )
        fit(
            verdict,
            prefetch=verdict.best_strategy == FieldOperation.PREFETCH_RELATED.value,
        )
        timed += 1
    return Costed(timed=timed, skipped=skipped)
