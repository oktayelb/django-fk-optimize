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

**A relation is a path, not a name.**  `book.publisher.country` is
`publisher__country`, two hops and two lazy loads per row, and one hint that
carries both.  Everything here resolves through `Vocabulary.resolve_path()` and
`resolve_hops()` rather than looking a name up on the starting model, because
the second hop is not a relation of the model the loop iterates and a lookup
that expects it to be silently drops the deeper half of every chain.  The hops
matter on their own too: select_related() is legal only when every one of them
is joinable.
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
from ..utils.vocabulary import FORWARD, Relation, Vocabulary
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

# How much the saving is worth believing, which is not the same question as
# how sure we are that we found the right call site. `confidence` answers the
# second; printing it alone next to "rows 0" made it look like an answer to
# the first.
EVIDENCE_OBSERVED = "observed"
EVIDENCE_ESTIMATED = "estimated"
EVIDENCE_NONE = "none"

# What a verdict says to do.
N_PLUS_ONE = "n_plus_one"
EXTRA_QUERY = "extra_query"
REMOVE_HINT = "remove_hint"
SWITCH_TO_SELECT = "switch_to_select_related"
KEEP_PREFETCH = "keep_prefetch"
ALREADY_HINTED = "already_hinted"
ID_ONLY = "id_only"
# A related manager read but never consumed. Free already, and prefetching it
# would add a query rather than remove one.
FREE_MANAGER = "free_manager"
# A related manager consumed through .filter()/.first()/..., which re-queries
# per row even when the relation is prefetched.
PREFETCH_BYPASSED = "prefetch_bypassed"

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


def _touched(site: CallSite, vocabulary: Vocabulary) -> dict[str, Relation]:
    """Every path this site reaches through, and the relation it ends at.

    Keyed by the whole path, valued by its *last* hop, because that is the hop
    whose target table a recorded query can be matched against.  A path the
    vocabulary cannot walk is dropped rather than half-walked: an intermediate
    model from an app this run does not cover is a gap, and a guess about the
    rest of the chain would be worse than the gap.
    """
    return {
        path: relation
        for path in site.touched
        if (relation := vocabulary.resolve_path(site.model, path)) is not None
    }


def _reported_as(site: CallSite, path: str) -> str:
    """The path the fix will name, for a path a table matched.

    `touched` carries every prefix, and a site reading
    `book.publisher.country.name` issues a lazy query on the publisher table
    *and* on the country table.  It is reported once, at the deepest uncovered
    path, because select_related("publisher__country") joins both -- so a
    recorded N+1 on the publisher table belongs to the `publisher__country`
    finding.  Returning the prefix instead would attach real observed numbers
    to a verdict nobody emits, which is how a finding disappears.
    """
    if path in site.missing:
        return path
    deeper = [other for other in site.missing if other.startswith(path + "__")]
    return deeper[0] if len(deeper) == 1 else path


def _confirm(site, table, vocabulary, tables) -> str:
    """The touched path whose final target table is `table`, or "".

    Per hop, now that the prefixes are there: `author` confirms against the
    author table and `author__mentor` against the mentor's.  An uncovered path
    is preferred over a covered one, because a hinted relation issues no lazy
    query at all -- which is what tells `author__mentor` apart from `author`
    when both end at the same table, as a self-reference does.
    """
    if not table:
        return ""
    matched = [
        path
        for path, relation in _touched(site, vocabulary).items()
        if tables.table(relation.target) == table
    ]
    for path in matched:
        reported = _reported_as(site, path)
        if reported in site.missing:
            return reported
    return matched[0] if matched else ""


def _sole_missing(site, vocabulary) -> str:
    """The one uncovered path, when there is only one.

    A hinted relation issues no lazy query, so a recorded N+1 at this site can
    only have come from an unhinted one.  With exactly one of those, the name
    follows without a table to confirm it -- at `probable`, because "only one
    candidate" is a deduction and not an observation.
    """
    reachable = _touched(site, vocabulary)
    missing = [path for path in site.missing if path in reachable]
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
    # Only a forward many-to-one dereferences to a single row of the *target*
    # table. A reverse or many-to-many access queries the child table with the
    # parent's id, which is a different shape entirely, so those relations can
    # never explain this group and must not dilute the candidate list.
    found = [
        (info.label, relation.name)
        for info in vocabulary.models.values()
        for relation in info.relations.values()
        if relation.target == target and relation.kind == FORWARD
    ]
    # A proxy model repeats its concrete model's relations against the same
    # table, so it would turn one candidate into two and make a nameable
    # finding look ambiguous. Collapse on (table, relation).
    unique: dict[tuple[str, str], tuple[str, str]] = {}
    for label, name in found:
        unique.setdefault((tables.table(label) or label, name), (label, name))
    candidates = tuple(unique.values())
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

    # What it costs now, as recorded -- per invocation, both of them.  A whole
    # recording's worth of queries printed beside "per call" is the number
    # nobody can act on: it moves with how long the recording ran.
    observed_queries: int | None = None
    observed_seconds: float | None = None
    # How many invocations that median was taken over.  An N seen once is a
    # weaker claim than the same N seen on thirty-seven page loads, and a
    # report that prints them identically is hiding the difference.
    observed_invocations: int = 0

    # what it costs now and what it could cost, as measured
    current: Measurement | None = None
    # Whether the pick came off the clock or off the relation kind. A timing
    # over too few rows is noise, and a pick made from noise must not print
    # like a pick made from evidence.
    basis: str = MEASURED
    # For REMOVE_HINT: the method the unused hint was written with, kept so
    # the fix can be recomputed against a line that has other changes on it.
    hint_method: str = ""
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
    def evidence(self) -> str:
        """How much the saving is worth believing.

        A verdict about a table with no rows has a real call site and no
        evidence at all for what fixing it would save. Those are two different
        claims and they get two different words.
        """
        if self.kind == REMOVE_HINT:
            # An unused hint is a static fact: the relation is never touched
            # here, whatever the table happens to hold today.
            return EVIDENCE_OBSERVED
        if not self.rows.known or self.rows.n <= 0:
            return EVIDENCE_NONE
        if self.rows.provenance == OBSERVED:
            return EVIDENCE_OBSERVED
        return EVIDENCE_ESTIMATED

    @property
    def measured(self) -> bool:
        """Whether there are numbers here that are worth quoting.

        A structural pick has durations attached -- they were really taken --
        but they did not decide anything and the difference between them is
        noise. Quoting a saving off them produces things like "saves -0.1 ms",
        which is worse than saying nothing.
        """
        return (
            self.current is not None
            and self.best is not None
            and self.basis == MEASURED
        )

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


def rewrite(verdict: Verdict, expression: str, *, prefetch: bool = False) -> str:
    """`expression` with this one verdict's change applied.

    Split out of `fit()` so the same rule can be replayed over a line that
    several verdicts change, instead of each one rewriting the original and
    quietly discarding what the others said.
    """
    method = (
        FieldOperation.PREFETCH_RELATED.value
        if prefetch
        else FieldOperation.SELECT_RELATED.value
    )
    if verdict.kind in (N_PLUS_ONE, EXTRA_QUERY):
        return with_hint(expression, method, verdict.relation)
    if verdict.kind == SWITCH_TO_SELECT:
        dropped = without_hint(
            expression, FieldOperation.PREFETCH_RELATED.value, verdict.relation
        )
        return with_hint(dropped, FieldOperation.SELECT_RELATED.value, verdict.relation)
    if verdict.kind == REMOVE_HINT:
        return without_hint(
            expression,
            verdict.hint_method or FieldOperation.SELECT_RELATED.value,
            verdict.relation,
        )
    return expression


def reconcile(verdicts: Iterable[Verdict]) -> int:
    """Make every change on one line agree with the others on that line.

    Each verdict rewrites the call site it was built from, so two findings on
    the same line each produced a fix that undid the other: one said to keep
    select_related('subnet'), the next said to keep select_related('site'),
    and both were right on their own and wrong together. Seen in the wild on
    a line with two unused hints and on a line with two missing ones.

    So the fix line for a shared call site shows the finished line -- every
    change on it, applied -- and each verdict keeps its own rows, evidence and
    measurements.
    """
    shared: dict[tuple[str, int, str], list[Verdict]] = {}
    for verdict in verdicts:
        if not verdict.actionable or verdict.runtime_only or not verdict.expression:
            continue
        shared.setdefault((verdict.file, verdict.line, verdict.expression), []).append(
            verdict
        )

    reconciled = 0
    for (_path, _line, expression), members in shared.items():
        if len(members) < 2:
            continue
        text = expression
        for verdict in sorted(members, key=lambda item: item.relation):
            text = rewrite(
                verdict,
                text,
                prefetch=verdict.best_strategy == FieldOperation.PREFETCH_RELATED.value,
            )
        note = f"{len(members)} changes on this line; the fix shows all of them applied"
        for verdict in members:
            verdict.fix = text
            verdict.notes = verdict.notes + (note,)
        reconciled += 1
    return reconciled


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
            verdict.fix = rewrite(verdict, verdict.expression, prefetch=prefetch)
    elif verdict.kind == SWITCH_TO_SELECT:
        verdict.fix = rewrite(verdict, verdict.expression, prefetch=prefetch)
    verdict.best_strategy = verdict.best_strategy or method


CardinalityFor = Callable[[str, str], "Cardinality | None"]


@dataclass(frozen=True)
class Observation:
    """Every recorded group that one finding speaks for.

    A chain reported at a single path issues a lazy query on *every* hop along
    it -- `book.publisher.country` costs two per row and is recorded as two
    groups -- while the fix names one hint that removes both.  So the finding
    carries two numbers that are emphatically not the same number:

    * `rows` is how many rows the loop went round.  Every hop fires once per
      row, so the largest group is the row count and adding the groups up
      would multiply it by the length of the chain.
    * `queries` is what those rows cost, which *is* the sum, because each hop
      really did go back to the database.

    Printing the first where the second belongs is what put `1 + 200 queries`
    beside `400 fewer queries` in the same block -- the tool contradicting
    itself four lines apart, which is the one thing a measurement tool cannot
    do and stay believable.
    """

    matches: tuple[Match, ...]

    @property
    def primary(self) -> Match:
        """The group the finding is described by: confidence, source, table.

        The first, which is the deepest hop the join confirmed.  These are
        facts about how the site was identified, and identification happened
        once however many hops the chain has.
        """
        return self.matches[0]

    @property
    def rows(self) -> int:
        return max(match.group.count for match in self.matches)

    @property
    def queries(self) -> int:
        return sum(match.group.count for match in self.matches)

    @property
    def seconds(self) -> float:
        return sum(match.group.seconds for match in self.matches)

    @property
    def invocations(self) -> int:
        return max(match.group.invocations for match in self.matches)

    @property
    def hops(self) -> int:
        """How many relations along the chain were separately recorded."""
        return len(self.matches)


class _Observed(dict):
    """The matched groups, and which of them a verdict has spoken for.

    A verdict speaks for a recorded group exactly when it reads that group's
    numbers, so the read is the only place the fact is reliably known.
    Recording it here keeps `build()`'s guarantee -- every matched group
    produces a verdict -- one line away from the thing it guarantees, instead
    of re-deriving it afterwards from the verdicts and getting it subtly wrong
    for the one shape nobody thought of.

    Values are `Observation`s rather than single matches, because several
    recorded groups can belong to one finding; `add()` is what keeps them all
    instead of letting the second hop overwrite the first.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.claimed: set[tuple[int, str]] = set()

    def add(self, key, match: Match) -> None:
        found = self.get_quietly(key)
        matches = (found.matches if found is not None else ()) + (match,)
        self[key] = Observation(matches)

    def get_quietly(self, key) -> Observation | None:
        """Read without claiming, for assembling rather than reporting."""
        return dict.get(self, key)

    def get(self, key, default=None):
        if key in self:
            self.claimed.add(key)
            return self[key]
        return default


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

    observed = _Observed()
    for match in result.matched:
        if match.relation:
            observed.add((id(match.site), match.relation), match)

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
    found = observed.get((id(site), relation))
    if found is not None:
        # The largest group, not the sum: every hop of a chain fires once per
        # row, so adding them up would report the loop as longer than it was.
        return Rows(found.rows, OBSERVED)
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


def _covering(hints: tuple[str, ...], path: str) -> str:
    """The hint that already loads `path`, or "".

    The same asymmetry `Hints.covers()` is built on, kept here so the verdict
    can quote the hint that is really in the source rather than the path it
    was asked about: a site that reads `book.publisher` under
    select_related("publisher__country") is covered, and saying so in the
    author's own words is the difference between a line they recognise and one
    they go looking for.
    """
    for hint in hints:
        if hint == path or hint.startswith(path + "__"):
            return hint
    return ""


def _site_verdicts(site, observed, vocabulary, cardinality, sample_size):
    info = vocabulary.models.get(site.model)
    if info is None:
        return []
    out: list[Verdict] = []
    missing = set(site.missing)

    for path in site.touched:
        if vocabulary.resolve_path(site.model, path) is None:
            continue
        prefetched = _covering(site.hints.prefetch, path)
        selected = _covering(site.hints.select, path)
        if path in missing:
            out.append(
                _unhinted(site, path, observed, cardinality, sample_size, vocabulary)
            )
        elif prefetched:
            out.append(
                _prefetched(
                    site, path, observed, cardinality, sample_size, hint=prefetched
                )
            )
        elif selected:
            verdict = _base(
                site,
                ALREADY_HINTED,
                path,
                _rows(site, path, observed, cardinality, sample_size),
            )
            method = FieldOperation.SELECT_RELATED.value
            verdict.headline = f'already covered by {method}("{selected}")'
            out.append(verdict)
        # Otherwise this is a bare prefix of a deeper uncovered path -- the
        # `publisher` of a site that reads `publisher.country.name`. It is not
        # hinted and it is not the finding: one select_related("publisher__
        # country") settles both hops, so the finding is filed against the
        # deepest path and the prefix says nothing. Calling it `already_hinted`
        # (which is what an `else` here did) told the reader a relation nobody
        # had hinted was already taken care of.

    for relation in site.unused:
        # A hint the scanner cannot see a touch for is not an unused hint:
        # `touched` only ever holds forward many-to-one relations.  Resolved as
        # a path, so an unused select_related("publisher__country") is reported
        # rather than dropped for not being a relation of Book.
        if vocabulary.resolve_path(site.model, relation) is None:
            continue
        method = (
            FieldOperation.SELECT_RELATED.value
            if relation in site.hints.select
            else FieldOperation.PREFETCH_RELATED.value
        )
        verdict = _base(site, REMOVE_HINT, relation, Rows(0, UNKNOWN))
        verdict.actionable = True
        verdict.hint_method = method
        verdict.headline = f'{method}("{relation}") is never used here'
        verdict.fix = without_hint(verdict.expression, method, relation)
        _soften(verdict, site)
        out.append(verdict)

    for relation in site.free:
        verdict = _base(site, FREE_MANAGER, relation, Rows(0, UNKNOWN))
        verdict.headline = (
            f"{relation} is read but never consumed; a related manager costs "
            "nothing until something evaluates it"
        )
        verdict.notes = verdict.notes + (
            f'prefetch_related("{relation}") here would add a query, not remove one',
        )
        out.append(verdict)

    for relation in site.bypassed:
        verdict = _base(site, PREFETCH_BYPASSED, relation, Rows(0, UNKNOWN))
        hinted = relation in site.hints.prefetch
        verdict.headline = (
            f"{relation} is consumed in a way prefetch_related cannot serve"
        )
        verdict.notes = verdict.notes + (
            "filter(), first() and the rest re-query per row even when the "
            "relation is prefetched; only all(), count() and exists() read "
            "the cache",
        )
        if hinted:
            verdict.notes = verdict.notes + (
                f'prefetch_related("{relation}") is paid for here and not used',
            )
        out.append(verdict)

    for attname in site.id_only:
        # A column attribute is read off whatever model the path arrives at,
        # not off the model the loop iterates: `book.author.mentor_id` is
        # `mentor_id` on Author, and checking it against Book's columns finds
        # nothing and says nothing.
        prefix, _, column = attname.rpartition("__")
        owner = info
        if prefix:
            arrival = vocabulary.resolve_path(site.model, prefix)
            owner = vocabulary.models.get(arrival.target) if arrival else None
        if owner is None:
            continue
        relation = next(
            (rel.name for rel in owner.relations.values() if rel.attname == column),
            "",
        )
        if not relation:
            continue
        verdict = _base(
            site,
            ID_ONLY,
            f"{prefix}__{relation}" if prefix else relation,
            Rows(0, UNKNOWN),
        )
        verdict.headline = f"{attname} is already on the row; nothing to do"
        out.append(verdict)

    return out


def _prefetch_only(vocabulary, label: str, path: str) -> bool:
    """Whether a hint for this path has to be prefetch_related().

    Decided by every hop, not by the last one.  select_related() raises
    FieldError on a reverse many-to-one and on a many-to-many wherever they
    appear along the path, so `club__books__publisher` needs a batch even
    though it ends at a forward FK that a join would carry happily.  The one
    reverse relation Django *can* join is a one-to-one, which is why this is
    read off `Relation.joinable` rather than off the direction.

    prefetch_related() takes `__` paths too, so there is always a hint to
    offer; the question is only which.
    """
    hops = vocabulary.resolve_hops(label, path) if vocabulary is not None else None
    if not hops:
        return False
    return any(not hop.joinable for hop in hops)


def _unhinted(
    site, relation, observed, cardinality, sample_size, vocabulary=None
) -> Verdict:
    rows = _rows(site, relation, observed, cardinality, sample_size)
    found = observed.get((id(site), relation))
    kind = N_PLUS_ONE if rows.n > 1 else EXTRA_QUERY
    verdict = _base(site, kind, relation, rows)
    verdict.actionable = True
    hops = 1
    if found is not None:
        verdict.confidence = found.primary.confidence
        # Queries are summed and rows are not: a two-hop chain goes back to
        # the database twice for every row it read.
        verdict.observed_queries = found.queries
        # Per invocation, beside a per-invocation N: the report prints both in
        # the same "what this costs one call" row.
        verdict.observed_seconds = found.seconds
        verdict.observed_invocations = found.invocations
        verdict.source = found.primary.group.source
        hops = found.hops
        if not found.primary.by_table:
            verdict.notes = verdict.notes + (
                "matched on the enclosing scope only; the table did not confirm it",
            )
    prefetch = _prefetch_only(vocabulary, site.model, relation)
    if kind == N_PLUS_ONE and hops > 1:
        # One hint removes the whole chain, so this is one finding -- but it
        # must not quote one hop's traffic as the cost of all of them.
        headline = (
            f"{verdict.observed_queries} extra queries, "
            f"{hops} per row across {relation.replace('__', ' -> ')}"
        )
    elif kind == N_PLUS_ONE:
        headline = f"{rows.n} extra queries, one per row"
    elif prefetch:
        headline = "one extra query, and a batch would carry it"
    else:
        headline = "one extra query, and a join would carry it for free"
    verdict.headline = headline
    fit(verdict, prefetch=prefetch)
    _soften(verdict, site)
    return verdict


def _prefetched(
    site, relation, observed, cardinality, sample_size, hint: str = ""
) -> Verdict:
    rows = _rows(site, relation, observed, cardinality, sample_size)
    stats = cardinality(site.model, relation) if cardinality else None
    # The hint as it is written in the source, which is what the reader has to
    # find and change; `relation` is the path that was touched, and the two
    # differ whenever a deeper hint covers a shallower touch.
    hint = hint or relation
    if stats is not None and stats.counted and not stats.batches_well:
        verdict = _base(site, SWITCH_TO_SELECT, hint, rows)
        verdict.actionable = True
        verdict.headline = (
            f'prefetch_related("{hint}") returns '
            f"{stats.distinct_ratio:.0%} as many rows as the parent query; "
            "a join carries them for free"
        )
        fit(verdict)
        _soften(verdict, site)
        return verdict

    verdict = _base(site, KEEP_PREFETCH, hint, rows)
    if stats is not None and stats.counted:
        verdict.headline = (
            f'prefetch_related("{hint}") is the right hint: '
            f"{stats.present} rows share {stats.distinct} targets"
        )
    else:
        verdict.headline = f'prefetch_related("{hint}") covers this touch'
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
        observed_seconds=group.seconds,
        observed_invocations=group.invocations,
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
