"""Find the places a queryset is iterated and its relations are touched.

The report m2o_optimize produces is a lookup table -- "if a call site iterates
this model and touches this FK, use X" -- and this module supplies the half that
table cannot see: which call sites those are, what they already hint, and how
many rows each one actually pulls.

It resolves names rather than inferring types: the Vocabulary knows every model
and every relation, ModuleImports knows what each file's names refer to, and
what is left is bookkeeping over a chain of queryset calls.  Where the
bookkeeping runs out -- a queryset built in another function, a custom method,
an instance handed to a callee -- the site is reported at a lower confidence
instead of being dropped or guessed at.  A call site nobody can resolve is a
cheap mistake; a confident wrong one is not.

Deliberately not imported here: Django.  Build a Vocabulary by hand and this
scanner runs against a string, with no settings and no database.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from dataclasses import field as dataclass_field

RESOLVED = "resolved"
PROBABLE = "probable"
UNRESOLVED = "unresolved"

ITERATION = "iteration"
INSTANCE = "instance"

# Methods that hand back a queryset over the same model, so the chain continues.
CHAINING = frozenset(
    {
        "all",
        "filter",
        "exclude",
        "annotate",
        "alias",
        "order_by",
        "reverse",
        "distinct",
        "select_related",
        "prefetch_related",
        "only",
        "defer",
        "using",
        "none",
        "extra",
        "select_for_update",
        "complex_filter",
        "iterator",
        "prefetch_related_objects",
        "resolve_expression",
    }
)

# Methods that stop producing model instances.  A hint on any of these is
# useless at best: select_related() after values() raises outright.
TERMINAL = frozenset(
    {
        "count",
        "exists",
        "aggregate",
        "update",
        "delete",
        "values",
        "values_list",
        "in_bulk",
        "dates",
        "datetimes",
        "explain",
        "bulk_create",
        "bulk_update",
        "contains",
    }
)

# Methods that return one instance.  Still worth a hint -- one get() plus three
# relation touches is four queries -- but the row count is 1, which decides
# select_related over prefetch_related on its own.
SINGLE = frozenset({"get", "first", "last", "earliest", "latest"})

# Same, but the result is a tuple or otherwise not the instance itself.
OPAQUE_RESULT = frozenset({"get_or_create", "update_or_create", "create"})


class _Terminal:
    """A chain that deliberately stops producing instances.

    Distinct from None, which means the scan could not follow the chain at all.
    Filing values() under "unresolved" would pad the coverage warning with
    call sites that were understood perfectly and simply have no hint to give.
    """

    def __repr__(self):
        return "<terminal>"


TERMINAL_CHAIN = _Terminal()


@dataclass(frozen=True)
class Hints:
    select: tuple[str, ...] = ()
    prefetch: tuple[str, ...] = ()
    opaque: bool = False  # a hint whose arguments were not literals

    def covers(self, path: str) -> bool:
        """Whether some hint here already loads the relation at `path`.

        Only one direction is true, and it is the surprising one.  A *deeper*
        hint covers a shallower path -- select_related("publisher__country")
        joins the publisher on its way to the country, so the `publisher` hop
        costs nothing extra.  A shallower hint covers nothing: after
        select_related("publisher") the publisher is on the row and
        `publisher.country` is still a query per row.

        Reading a prefix as coverage is what made a half-applied fix read as a
        clean site forever: the first hop gets hinted, the site stops being
        reported, and the second N+1 outlives the report that was supposed to
        find it.
        """
        return any(
            hint == path or hint.startswith(path + "__")
            for hint in tuple(self.select) + tuple(self.prefetch)
        )

    def __bool__(self):
        return bool(self.select or self.prefetch or self.opaque)


@dataclass
class Binding:
    """What a local name currently refers to."""

    kind: str  # ITERATION source (queryset) or INSTANCE
    model: object  # ModelInfo
    hints: Hints = Hints()
    bound: int | None = None
    bound_reason: str = ""
    confidence: str = RESOLVED
    notes: tuple[str, ...] = ()
    origin: str = ""  # the chain this started as, for a name that hides it
    # id() of the manager expression this chain grew out of, so a site found
    # three scopes away can still be counted against the queryset that built
    # it.  Zero when the chain has no manager expression in this file.
    origin_id: int = 0


@dataclass(frozen=True)
class Scope:
    """The function a call site sits in, and the lines that function spans.

    A runtime recorder attributes a lazy load to the line that *touched* the
    relation; this scanner files the site at the line that built the queryset.
    Those are never the same line, so the only thing the two halves can be
    joined on is the enclosing function.
    """

    function: str = ""
    start: int = 0
    end: int = 0

    def contains(self, line: int) -> bool:
        return self.start <= line <= self.end


MODULE_SCOPE_NAME = "<module>"


@dataclass
class CallSite:
    path: str
    line: int
    model: str
    kind: str
    expression: str
    hints: Hints = Hints()
    touched: tuple[str, ...] = ()
    id_only: tuple[str, ...] = ()
    # A related manager read but never consumed: free, and prefetching it
    # would cost an extra query for nothing.
    free: tuple[str, ...] = ()
    # A related manager consumed through .filter()/.first()/..., which
    # re-queries per row even when the relation has been prefetched.
    bypassed: tuple[str, ...] = ()
    bound: int | None = None
    bound_reason: str = ""
    escapes: bool = False
    confidence: str = RESOLVED
    notes: tuple[str, ...] = ()

    # The enclosing scope, for matching a runtime line back to this site.
    # "<module>" and the module's span when the site is not inside a function.
    function: str = ""
    scope_start: int = 0
    scope_end: int = 0

    @property
    def scope(self) -> Scope:
        return Scope(self.function, self.scope_start, self.scope_end)

    def contains_line(self, line: int) -> bool:
        """Is `line` inside the function this site was written in?"""
        return self.scope_start <= line <= self.scope_end

    @property
    def missing(self) -> tuple[str, ...]:
        """The deepest uncovered paths this site reaches through.

        `touched` holds every prefix, so a site that reads
        `book.publisher.country.name` has `publisher` and `publisher__country`
        uncovered at once -- and reporting both would ask for two hints where
        select_related("publisher__country") already joins both tables.  A path
        that something deeper extends is therefore dropped: one finding, one
        fix, and never two verdicts where one hint settles both.
        """
        uncovered = [path for path in self.touched if not self.hints.covers(path)]
        return tuple(
            path
            for path in uncovered
            if not any(other.startswith(path + "__") for other in uncovered)
        )

    @property
    def unused(self) -> tuple[str, ...]:
        """Hints for relations this site never touches -- a join for nothing.

        A hint counts as used when any path this site reaches meets it in
        *either* direction: select_related("publisher") is used by a site that
        reads `publisher.country`, and select_related("publisher__country") is
        used by one that only reads `publisher`.  Only a hint nothing overlaps
        at all is reported.

        The asymmetry with `missing` is deliberate.  Over-reporting here tells
        someone to delete a join they need, and they will believe it;
        under-reporting only means the tool stays quiet about a join that costs
        one extra column.  Those are not the same mistake.
        """
        if self.escapes or self.hints.opaque:
            return ()
        reached = tuple(self.touched) + tuple(self.bypassed)
        hinted = tuple(self.hints.select) + tuple(self.hints.prefetch)
        return tuple(
            hint
            for hint in hinted
            if not any(_overlaps(hint, path) for path in reached)
        )


@dataclass
class ScanReport:
    """Sites found, plus what the scan could not account for.

    The unresolved list is not noise to be tidied away.  A scanner that reports
    only what it understood reads as a clean bill of health for the files it
    failed on, which is the one failure mode that makes a tool like this worse
    than nothing.

    `seen`, `attributed`, `terminal` and `unresolved` are a census, not a
    sample: every manager expression in the file lands in exactly one of the
    last three, and

        seen == attributed + terminal + len(unresolved)

    holds per file and therefore over any number of files.  Without that
    invariant the coverage figure is computed from a denominator made of the
    cases the scanner happened to look at, which is how a project where four
    queryset expressions out of a hundred and ninety-one became call sites
    reported 97.7% coverage.  A shape nobody has taught the scanner yet is
    supposed to make the number *worse*.
    """

    sites: list[CallSite] = dataclass_field(default_factory=list)
    unresolved: list[tuple[str, int, str]] = dataclass_field(default_factory=list)
    errors: list[tuple[str, str]] = dataclass_field(default_factory=list)
    files: int = 0

    # Every manager expression in the file, counted once at its outermost node.
    seen: int = 0
    # Those that produced at least one call site.  Expressions rather than
    # sites: one queryset iterated in two places is one expression understood,
    # and counting it twice would break the census it is part of.
    attributed: int = 0
    # Those that end in values(), count(), create(), ... -- understood, and
    # with nothing a hint could improve.
    terminal: int = 0

    def extend(self, other: ScanReport):
        self.sites.extend(other.sites)
        self.unresolved.extend(other.unresolved)
        self.errors.extend(other.errors)
        self.files += other.files
        self.seen += other.seen
        self.attributed += other.attributed
        self.terminal += other.terminal


# ----------------------------------------------------------------------
# entry points
# ----------------------------------------------------------------------


def scan_source(source, path, vocabulary, package=None) -> ScanReport:
    report = ScanReport(files=1)
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        report.errors.append((str(path), str(exc)))
        return report
    scanner = _Scanner(str(path), vocabulary, tree, package)
    scanner.run()
    report.sites.extend(scanner.sites)
    report.unresolved.extend(scanner.unresolved)
    report.seen = scanner.seen
    report.attributed = scanner.attributed
    report.terminal = scanner.terminal
    return report


def scan_file(path, vocabulary, package=None) -> ScanReport:
    try:
        source = open(path, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError) as exc:
        return ScanReport(errors=[(str(path), str(exc))], files=1)
    return scan_source(source, path, vocabulary, package)


def scan_files(paths, vocabulary, package_for=None) -> ScanReport:
    total = ScanReport()
    for path in paths:
        package = package_for(path) if package_for else None
        total.extend(scan_file(path, vocabulary, package))
    return total


# ----------------------------------------------------------------------
# the scanner
# ----------------------------------------------------------------------


class _Scanner:
    def __init__(self, path, vocabulary, tree, package=None):
        from .imports import ModuleImports

        self.path = path
        self.vocabulary = vocabulary
        self.tree = tree
        self.imports = ModuleImports.from_ast(tree, package)
        self.sites: list[CallSite] = []
        self.unresolved: list[tuple[str, int, str]] = []
        self._seen: set[int] = set()
        self._managers = {
            name for info in vocabulary.models.values() for name in info.managers
        }
        self._aliases: dict[str, object] = {}
        self._opaque: set[str] = set()
        # Querysets the enclosing class hands its own methods, keyed by how
        # they are reached: ("attr", "queryset") for `self.queryset`,
        # ("call", "get_queryset") for `self.get_queryset()`.  Empty outside a
        # class body, and saved and restored around each one.
        self._attributes: dict[tuple[str, str], Binding] = {}
        self._parents: dict[int, ast.AST] = {}
        # The census (see ScanReport): every manager expression in the file,
        # then the ids of the ones that turned into a site.
        self._expressions: dict[int, ast.AST] = {}
        self._unknown: dict[int, ast.AST] = {}
        self._from_expression: set[int] = set()
        self._terminal: set[int] = set()
        self._loaded_models()

    def _loaded_models(self):
        """Names bound by a dynamic model loader, which is still a literal.

        `Product = get_model("catalogue", "Product")` is how django-oscar and
        django-machina reach every model they have, and both arguments are
        constants -- so refusing to read it is refusing to read the project.
        Before this, oscar resolved nothing at all: two hundred models and not
        one call site.
        """
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            call = node.value
            if not isinstance(target, ast.Name) or not isinstance(call, ast.Call):
                continue
            if _called(call.func) != "get_model":
                continue
            info = self._model_from_labels(_string_args(call))
            if info is not None:
                self._aliases[target.id] = info
            else:
                # The name was bound by a loader we could not read, so the
                # class name is not evidence about which model came back.
                # Falling through to the bare-name lookup would be a guess
                # dressed up as a resolution.
                self._opaque.add(target.id)

    def _model_from_labels(self, args):
        if len(args) == 2:
            app, name = args
        elif len(args) == 1 and "." in args[0]:
            app, name = args[0].split(".", 1)
        else:
            return None
        return self.vocabulary.by_label_parts(app, name)

    def run(self):
        self._parents = _parents([self.tree])
        self._census()
        self._scope_of(self.tree.body, {}, _module_scope(self.tree))
        self._account()

    # -- the census ----------------------------------------------------

    @property
    def seen(self) -> int:
        return len(self._expressions) + len(self._unknown)

    @property
    def attributed(self) -> int:
        return len(self._from_expression)

    @property
    def terminal(self) -> int:
        return len(self._terminal)

    def _census(self):
        """Every manager expression in the file, before anything is resolved.

        Taken up front and from the whole tree, because the point of the number
        is to be independent of what the rest of the scan manages to follow:
        counting only the expressions the walk reached would make the
        denominator grow every time the scanner learns a new shape, which is
        exactly backwards.

        Counted at the outermost node of each chain, so
        `Book.objects.filter(x).select_related(y)` is one expression and not
        the three nested ones ast.walk() offers.

        A chain that goes through a manager whose *model* cannot be named --
        `permission_model.objects.filter(...)`, or a model this vocabulary
        never learned -- is counted too, in `_unknown`.  It is not rooted in
        anything we know, so it can never become a site, and a denominator that
        leaves out the expressions the scan is worst at is a denominator that
        flatters the scan.
        """
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.Attribute, ast.Call, ast.Subscript)):
                continue
            if _extends(self._parents.get(id(node)), node):
                continue
            if self._manager_rooted(node):
                self._expressions[id(node)] = node
            elif self._manager_shaped(node):
                self._unknown[id(node)] = node

    def _manager_rooted(self, node) -> bool:
        """A chain whose base is a model we know and whose first step is one of
        its managers -- `Book.objects...`, and nothing looser."""
        base, steps = _unwind(node)
        if not isinstance(base, ast.Name) or not steps:
            return False
        kind, name, _step = steps[0]
        if kind != "attr":
            return False
        info = self._model_from_name(base.id)
        return info is not None and name in info.managers

    def _manager_shaped(self, node) -> bool:
        """A chain that reads a manager off something this scan cannot name.

        `registry.objects.all()`, `self.model.objects.filter(...)`, a model the
        vocabulary never learned: a manager is plainly being used, and the only
        thing missing is the one fact that would make it a call site.  That is
        the definition of a case the scanner does not handle yet, so it belongs
        in the backlog rather than outside the count.

        Read off the syntax tree rather than the text of the expression, which
        the first version of this did: ".objects" is in "self.objects_list"
        too.
        """
        _base, steps = _unwind(node)
        return any(
            kind == "attr" and name in self._managers for kind, name, _step in steps
        )

    def _outermost(self, node):
        """The whole chain `node` is part of, for crediting a site to it."""
        current = node
        while True:
            parent = self._parents.get(id(current))
            if not _extends(parent, current):
                return current
            current = parent

    def _credit(self, binding):
        """Mark the manager expression a site grew out of as accounted for."""
        if binding.origin_id in self._expressions:
            self._from_expression.add(binding.origin_id)

    def _account(self):
        """Put every manager expression that is not a site in a bucket.

        Re-resolved here rather than remembered during the walk, because most
        manager expressions are never resolved at all: `Book.objects.filter(
        ...).delete()` is a bare statement, and a scan that only classifies
        what it iterated would file every write in the project under "could not
        follow".  The chains this asks about are rooted in a model name, so the
        answer does not depend on the scope they were written in.
        """
        leftover = []
        for key, node in self._expressions.items():
            if key in self._from_expression:
                continue
            if self._resolve(node, {}) is TERMINAL_CHAIN:
                self._terminal.add(key)
            else:
                # Resolved but never consumed here counts as backlog too: a
                # queryset handed to a template or a callee may well be an N+1
                # this scan cannot see, and calling it understood would be the
                # comfortable answer rather than the true one.
                leftover.append(node)
        leftover.extend(self._unknown.values())
        for node in sorted(leftover, key=lambda item: getattr(item, "lineno", 0)):
            self.unresolved.append(
                (self.path, getattr(node, "lineno", 0), _source(node))
            )

    # -- scopes --------------------------------------------------------

    def _scope_of(self, body, inherited, scope_info):
        """Walk one scope, then look for touches on the instances it bound.

        Bindings are last-write-wins in source order; branches are not tracked.
        A name reassigned in one arm of an if is the price of staying a scanner
        rather than becoming an interpreter.
        """
        scope = dict(inherited)
        instances: dict[str, tuple[Binding, ast.AST]] = {}

        for node in body:
            self._statement(node, scope, instances, scope_info)

        for name, (binding, origin) in instances.items():
            reached = _touches(body, name, binding.model, self.vocabulary)
            touched, id_only, escapes = (
                reached.touched,
                reached.id_only,
                reached.escapes,
            )
            if not (touched or reached.free or reached.bypassed) and not escapes:
                continue
            self._credit(binding)
            self.sites.append(
                CallSite(
                    path=self.path,
                    line=origin.lineno,
                    model=binding.model.label,
                    kind=INSTANCE,
                    expression=_source(origin),
                    hints=binding.hints,
                    touched=touched,
                    id_only=id_only,
                    free=reached.free,
                    bypassed=reached.bypassed,
                    bound=binding.bound if binding.bound is not None else 1,
                    bound_reason=binding.bound_reason or "single object",
                    escapes=escapes,
                    confidence=PROBABLE if escapes else binding.confidence,
                    notes=binding.notes
                    + (("instance escapes into a call",) if escapes else ()),
                    function=scope_info.function,
                    scope_start=scope_info.start,
                    scope_end=scope_info.end,
                )
            )

    def _statement(self, node, scope, instances, scope_info):
        if isinstance(node, ast.ClassDef):
            # A class body is not a function, so it inherits the scope it is
            # written in -- but it does own the attributes its methods read off
            # `self`, and those are collected before any method is walked.
            outer, self._attributes = self._attributes, {}
            try:
                self._class_attributes(node, scope)
                self._scope_of(node.body, scope, scope_info)
            finally:
                self._attributes = outer
            return

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # A def -- including a nested one -- starts its own scope.
            self._scope_of(node.body, scope, _function_scope(node))
            return

        if isinstance(node, ast.Assign):
            self._assign(node, scope, instances)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            self._loop(node, scope, scope_info)

        for child in _child_statements(node):
            self._statement(child, scope, instances, scope_info)

        for sub in ast.walk(node):
            if not isinstance(
                sub, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
            ):
                continue
            # ast.walk covers the whole subtree, and every enclosing statement
            # walks it again on the way down, so the same comprehension arrives
            # here once per level of nesting.
            if id(sub) in self._seen:
                continue
            self._seen.add(id(sub))
            self._comprehension(sub, scope, scope_info)

    def _assign(self, node, scope, instances):
        for target in node.targets:
            for name, value in _paired(target, node.value):
                binding = self._resolve(value, scope)
                if binding is None or binding is TERMINAL_CHAIN:
                    continue
                scope[name.id] = binding
                if binding.kind == INSTANCE:
                    instances[name.id] = (binding, value)

    # -- what a class hands its own methods -----------------------------

    def _class_attributes(self, node, scope):
        """Collect the querysets this class's methods can reach off `self`.

        Three shapes account for most Django written since 2013 -- a DRF
        viewset's `queryset = Book.objects.all()`, a class-based view's
        `get_queryset()`, and a plain `self.books = ...` set in one method and
        iterated in another -- and all three scanned to nothing at all before
        this, because the queryset and the loop that consumes it are in
        different scopes.  Not one of them needs type inference: the chain is
        still rooted in a manager this module already resolves, and the only
        thing that widens is the bookkeeping.

        One pass before the methods are walked, and class attributes before
        methods within it, so a `get_queryset()` returning
        `self.queryset.filter(...)` finds the attribute it reads.  Nothing
        here crosses a class or a module: a base class in another file is
        precisely the guess this scanner exists not to make.
        """
        for statement in node.body:
            for target, value in _class_assignments(statement):
                binding = self._queryset(value, scope)
                if binding is not None:
                    self._attributes[("attr", target)] = _reached(
                        binding, f"through the class attribute {target}"
                    )

        for statement in node.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for target, value in _self_assignments(statement):
                binding = self._queryset(value, scope)
                if binding is not None:
                    self._attributes[("attr", target)] = _reached(
                        binding,
                        f"through self.{target}, assigned in {statement.name}()",
                    )
            binding = self._returned_queryset(statement, scope)
            if binding is not None:
                self._attributes[("call", statement.name)] = binding

    def _queryset(self, value, scope) -> Binding | None:
        """`value` as a queryset binding, or None if it is anything else.

        An instance is refused rather than bound: `self.book` is followed
        wherever it is touched only within the scope that built it, and
        pretending otherwise would attribute touches to a row this class may
        never have loaded.
        """
        binding = self._resolve(value, scope)
        if binding is None or binding is TERMINAL_CHAIN:
            return None
        return binding if binding.kind == ITERATION else None

    def _returned_queryset(self, node, scope) -> Binding | None:
        """The queryset a method always returns, if it always returns one.

        Every return has to resolve, and to the same model, because a method
        that hands back a queryset down one branch and a list down another is
        not a queryset-returning method and treating it as one would put
        touches on rows that never existed.

        The result is PROBABLE whatever the chain inside was: `get_queryset()`
        is the single most overridden method in Django, and the subclass that
        overrides it is usually in another file this scan will not read.
        """
        bindings = [self._queryset(value, scope) for value in _returns(node)]
        if not bindings or any(binding is None for binding in bindings):
            return None
        if len({binding.model.label for binding in bindings}) != 1:
            return None

        binding = _reached(
            bindings[0],
            f"through self.{node.name}(), which a subclass can override",
            PROBABLE,
        )
        if any(other.hints != binding.hints for other in bindings[1:]):
            # The branches hint differently, so what this call site ends up
            # with depends on which one ran.  Opaque says exactly that, and
            # keeps the report from claiming a relation is already covered.
            binding.hints = Hints(opaque=True)
            binding.notes = binding.notes + (
                f"{node.name}() hints differently on different branches",
            )
        return binding

    def _loop(self, node, scope, scope_info):
        binding = self._resolve(node.iter, scope)
        if binding is TERMINAL_CHAIN:
            return
        if binding is None:
            # The census has already counted it; there is nothing to record.
            return
        if binding.kind == INSTANCE:
            return
        if isinstance(node.target, ast.Name):
            self._record_iteration(
                node.iter, node.target.id, binding, node.body, scope_info
            )

    def _comprehension(self, node, scope, scope_info):
        for generator in node.generators:
            binding = self._resolve(generator.iter, scope)
            if binding is TERMINAL_CHAIN:
                continue
            if binding is None:
                continue
            if binding.kind == INSTANCE or not isinstance(generator.target, ast.Name):
                continue
            body = [node.elt] if hasattr(node, "elt") else [node.key, node.value]
            self._record_iteration(
                generator.iter,
                generator.target.id,
                binding,
                body + list(generator.ifs),
                scope_info,
            )

    def _record_iteration(self, origin, varname, binding, body, scope_info):
        reached = _touches(body, varname, binding.model, self.vocabulary)
        touched, id_only, escapes = (
            reached.touched,
            reached.id_only,
            reached.escapes,
        )
        # `for a in qs:` says nothing about what qs is; the binding remembers.
        expression = _source(origin)
        if isinstance(origin, ast.Name) and binding.origin:
            expression = f"{binding.origin}  (as {origin.id})"
        confidence = binding.confidence
        notes = binding.notes
        if escapes:
            confidence = PROBABLE
            notes = notes + ("row escapes into a call; touches may be elsewhere",)
        self._credit(binding)
        self.sites.append(
            CallSite(
                path=self.path,
                line=getattr(origin, "lineno", 0),
                model=binding.model.label,
                kind=ITERATION,
                expression=expression,
                hints=binding.hints,
                touched=touched,
                id_only=id_only,
                free=reached.free,
                bypassed=reached.bypassed,
                bound=binding.bound,
                bound_reason=binding.bound_reason,
                escapes=escapes,
                confidence=confidence,
                notes=notes,
                function=scope_info.function,
                scope_start=scope_info.start,
                scope_end=scope_info.end,
            )
        )

    # -- resolution ----------------------------------------------------

    def _model_from_name(self, name):
        alias = self._aliases.get(name)
        if alias is not None:
            return alias
        if name in self._opaque:
            return None
        qualname = self.imports.names.get(name)
        if qualname:
            found = self.vocabulary.by_qualname(qualname)
            if found is not None:
                return found
            # Imported from somewhere the registry does not know under that
            # path (a re-export, say). The bare name is the last resort.
        return self.vocabulary.by_name(name)

    def _resolve(self, node, scope) -> Binding | None:
        base, steps = _unwind(node)
        if not isinstance(base, ast.Name):
            return None

        binding = scope.get(base.id)
        index = 0
        if binding is not None:
            binding = Binding(**vars(binding))
        elif base.id in SELF_NAMES and steps:
            # `self.queryset`, `self.get_queryset()`, `cls.queryset`: a binding
            # the enclosing class made, which the chain then continues from.
            found = self._attributes.get((steps[0][0], steps[0][1]))
            if found is None:
                return None
            binding = Binding(**vars(found))
            index = 1
        else:
            info = self._model_from_name(base.id)
            if info is None:
                return None
            if not steps or steps[0][0] != "attr" or steps[0][1] not in info.managers:
                return None
            binding = Binding(
                kind=ITERATION,
                model=info,
                origin=_source(node),
                origin_id=id(self._outermost(node)),
            )
            index = 1

        for kind, name, step in steps[index:]:
            if kind == "attr":
                # A manager reached through a name already bound to one is
                # fine; anything else is an attribute of an instance, and this
                # chain is not a queryset.
                if name in binding.model.managers:
                    continue
                return None

            if kind == "subscript":
                bound, single = _subscript_bound(step)
                if single:
                    binding.kind = INSTANCE
                    binding.bound, binding.bound_reason = 1, "indexed"
                elif bound is not None:
                    binding.bound, binding.bound_reason = bound, "slice"
                continue

            if name in TERMINAL or name in OPAQUE_RESULT:
                return TERMINAL_CHAIN
            if name in SINGLE:
                binding.kind = INSTANCE
                binding.bound, binding.bound_reason = 1, name + "()"
                continue
            if name in ("select_related", "prefetch_related"):
                binding.hints = _merge_hints(binding.hints, name, step)
                continue
            if name in CHAINING:
                continue

            # A custom manager or queryset method. It almost certainly returns
            # the same model, so the chain survives -- at lower confidence,
            # because "almost certainly" is not a fact.
            binding.confidence = PROBABLE
            binding.notes = binding.notes + (f"through custom method {name}()",)

        return binding


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

# The names a method reaches its own class's attributes through.  Both, because
# a classmethod is written with `cls` and means the same thing.
SELF_NAMES = frozenset({"self", "cls"})


def _overlaps(one: str, other: str) -> bool:
    """Whether two relation paths meet -- same path, or one inside the other."""
    return one == other or one.startswith(other + "__") or other.startswith(one + "__")


def _extends(parent, node) -> bool:
    """Whether `parent` carries the chain `node` is part of one step further.

    `Book.objects` inside `Book.objects.all()` is not an expression of its own;
    it is the middle of one.  This is how the census counts a chain once.
    """
    return (
        (isinstance(parent, ast.Attribute) and parent.value is node)
        or (isinstance(parent, ast.Call) and parent.func is node)
        or (isinstance(parent, ast.Subscript) and parent.value is node)
    )


def _paired(target, value):
    """(name, the expression it is bound to) for one assignment target.

    `a = b = qs` gives both names the same chain; `books, count = qs, 1` is
    element-wise, which is the shape that used to be dropped whole because the
    target was not a Name.  A starred target or an unpacked call yields
    nothing: which element a name receives is a runtime fact there, and a
    scanner that guessed would bind a queryset to a number.
    """
    if isinstance(target, (ast.Tuple, ast.List)):
        if not isinstance(value, (ast.Tuple, ast.List)):
            return
        if len(target.elts) != len(value.elts):
            return
        if any(isinstance(element, ast.Starred) for element in target.elts):
            return
        for element, item in zip(target.elts, value.elts, strict=True):
            yield from _paired(element, item)
    elif isinstance(target, ast.Name):
        yield target, value


def _class_assignments(statement):
    """(attribute name, value) for one statement in a class body."""
    if isinstance(statement, ast.Assign):
        for target in statement.targets:
            for name, value in _paired(target, statement.value):
                yield name.id, value
    elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
        # `queryset: QuerySet[Book] = Book.objects.all()` is the same
        # declaration with a type on it.
        if isinstance(statement.target, ast.Name):
            yield statement.target.id, statement.value


def _self_assignments(node):
    """(attribute name, value) for every `self.x = ...` in one method."""
    for statement in _own_statements(node):
        if not isinstance(statement, ast.Assign):
            continue
        for target in statement.targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in SELF_NAMES
            ):
                yield target.attr, statement.value


def _returns(node):
    """The value of every `return` this function makes itself.

    A nested def's returns belong to the nested def: a method that returns a
    list and happens to close over a helper returning a queryset is not a
    queryset-returning method.
    """
    for statement in _own_statements(node):
        if isinstance(statement, ast.Return) and statement.value is not None:
            yield statement.value


def _own_statements(node):
    """Every statement in a function body except another function's."""
    stack = list(node.body)
    while stack:
        statement = stack.pop(0)
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield statement
        stack.extend(_child_statements(statement))


def _reached(binding, note, confidence=None) -> Binding:
    """A copy of `binding`, saying how this call site got hold of it."""
    copy = Binding(**vars(binding))
    copy.notes = copy.notes + (note,)
    if confidence is not None:
        copy.confidence = confidence
    return copy


def _end_line(node, default=0):
    return getattr(node, "end_lineno", None) or getattr(node, "lineno", default)


def _module_scope(tree) -> Scope:
    end = max((_end_line(node) for node in tree.body), default=0)
    return Scope(MODULE_SCOPE_NAME, 1, end)


def _function_scope(node) -> Scope:
    # Decorators sit above `node.lineno`, so a site cannot be inside one and
    # the span deliberately starts at the `def`.
    return Scope(node.name, node.lineno, _end_line(node, node.lineno))


def _called(node) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _string_args(call: ast.Call) -> list[str]:
    args = []
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            args.append(arg.value)
        else:
            return []
    return args


def _unwind(node):
    """Peel a dotted/called/sliced expression into (base, steps in source order)."""
    steps = []
    current = node
    while True:
        if isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
            steps.append(("call", current.func.attr, current))
            current = current.func.value
        elif isinstance(current, ast.Subscript):
            steps.append(("subscript", None, current))
            current = current.value
        elif isinstance(current, ast.Attribute):
            steps.append(("attr", current.attr, current))
            current = current.value
        else:
            break
    steps.reverse()
    return current, steps


def _merge_hints(hints, method, call):
    names, opaque = [], hints.opaque
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            names.append(arg.value)
        else:
            opaque = True
    if not call.args:
        # select_related() with no arguments follows every non-null FK, which
        # covers relations this scan cannot enumerate from the call itself.
        opaque = True
    if method == "select_related":
        return Hints(tuple(hints.select) + tuple(names), hints.prefetch, opaque)
    return Hints(hints.select, tuple(hints.prefetch) + tuple(names), opaque)


def _subscript_bound(node):
    """(row cap, is single object) for a subscript on a queryset."""
    index = node.slice
    if isinstance(index, ast.Slice):
        upper = index.upper
        lower = index.lower
        if isinstance(upper, ast.Constant) and isinstance(upper.value, int):
            start = (
                lower.value
                if isinstance(lower, ast.Constant) and isinstance(lower.value, int)
                else 0
            )
            return max(upper.value - start, 0), False
        return None, False
    if isinstance(index, ast.Constant) and isinstance(index.value, int):
        return 1, True
    return None, False


# What a related manager answers without going back to the database once the
# queryset has been prefetched.  Measured, not assumed (Django 6.0.4, three
# publishers with nine books between them):
#
#     bare `publisher.book_set`   1 query  ->  2 with prefetch_related
#     .all() / iteration / len()  4        ->  2
#     .count() / .exists()        4        ->  2
#     .filter(...)                4        ->  5
#     .first()                    4        ->  5
#
# So reading the attribute is free and prefetching it is a pessimisation,
# while .filter() and .first() re-query per row *and* pay for the prefetch on
# top.  Recommending a hint for either would make the code slower.
PREFETCH_SERVED = frozenset({"all", "count", "exists", "len"})


def _manager_chain(node, parents):
    """The methods applied to a manager attribute, outermost last.

    `book.tags` -> [], `book.tags.all()` -> ["all"],
    `book.tags.all().filter(...)` -> ["all", "filter"].
    """
    chain: list[str] = []
    current = node
    while True:
        parent = parents.get(id(current))
        if isinstance(parent, ast.Attribute) and parent.value is current:
            chain.append(parent.attr)
            current = parent
        elif isinstance(parent, ast.Call) and parent.func is current:
            current = parent
        else:
            return chain


def _parents(nodes):
    found = {}
    for node in nodes:
        for parent in ast.walk(node):
            for child in ast.iter_child_nodes(parent):
                found[id(child)] = parent
    return found


@dataclass(frozen=True)
class Touches:
    """Every way a call site reaches a relation, kept apart by what it costs."""

    touched: tuple[str, ...] = ()  # costs a query, and a hint would fix it
    id_only: tuple[str, ...] = ()  # alarm.type_id: already on the row
    free: tuple[str, ...] = ()  # the manager itself, never consumed
    bypassed: tuple[str, ...] = ()  # consumed in a way prefetch cannot serve
    escapes: bool = False


def _chain(node, info, vocabulary, parents):
    """Classify one attribute chain hanging off a row, hop by hop.

    Yields (bucket, path) in Django's own `__` spelling.  A touch is a *path*
    and not a name because `book.publisher.country.name` is two queries per row
    and one `select_related("publisher__country")` settles both, while the
    `select_related("publisher")` the shallow read used to produce fixes half
    of it and then reports the site as fine for ever after.

    Every prefix is yielded as well as the full path: a recorded N+1 may be on
    any hop, and the runtime half confirms one hop at a time by the table it
    queried.

    The walk stops at the first hop that is not a relation, at a target model
    this vocabulary cannot name -- a guessed hop is worse than a short path --
    and at a manager, which ends the chain of instances: whatever
    `book.tags.first().name` reads belongs to a row no join can reach from
    here.
    """
    path: list[str] = []
    current = info
    while True:
        if not isinstance(node.ctx, ast.Load):
            # `post.thread.category = post.category` reads `post.thread` and
            # then *writes* the category: assigning a relation issues no query,
            # so the hop being assigned is not a touch and nothing can be
            # chained off it.  Misago has this exact line, and counting it made
            # up an N+1 that does not exist.
            return

        relation = current.relation(node.attr)
        if relation is None:
            if node.attr in current.attnames:
                # `alarm.type_id` is already in the row.  Recorded so the
                # report can say "you are fine" rather than say nothing.
                yield "id_only", "__".join(path + [node.attr])
            return

        path.append(relation.name)
        reached = "__".join(path)
        if getattr(relation, "manager", False):
            methods = _manager_chain(node, parents)
            if not methods:
                yield "free", reached
            elif all(method in PREFETCH_SERVED for method in methods):
                yield "touched", reached
            else:
                yield "bypassed", reached
            return

        yield "touched", reached
        parent = parents.get(id(node))
        if not (isinstance(parent, ast.Attribute) and parent.value is node):
            return
        current = _model(vocabulary, relation.target)
        if current is None:
            return
        node = parent


def _model(vocabulary, label):
    return vocabulary.models.get(label) if vocabulary is not None else None


def _touches(nodes, varname, info, vocabulary=None) -> Touches:
    """Relation paths of `info` reached through `varname` anywhere in `nodes`.

    `vocabulary` is what makes a chain longer than one hop readable: the model
    on the far side of a relation has to be named before the next attribute
    means anything.  Without one the walk is exactly the single-hop scan this
    used to be.
    """
    touched, id_only, free, bypassed = [], [], [], []
    escapes = False
    parents = _parents(nodes)
    buckets = {
        "touched": touched,
        "id_only": id_only,
        "free": free,
        "bypassed": bypassed,
    }

    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                if sub.value.id != varname:
                    continue
                for bucket, path in _chain(sub, info, vocabulary, parents):
                    buckets[bucket].append(path)
            elif isinstance(sub, ast.Call):
                for arg in list(sub.args) + [kw.value for kw in sub.keywords]:
                    if isinstance(arg, ast.Name) and arg.id == varname:
                        escapes = True

    unique = dict.fromkeys
    return Touches(
        touched=tuple(unique(touched)),
        id_only=tuple(unique(id_only)),
        free=tuple(name for name in unique(free) if name not in touched),
        bypassed=tuple(name for name in unique(bypassed) if name not in touched),
        escapes=escapes,
    )


def _child_statements(node):
    for name in ("body", "orelse", "finalbody"):
        for child in getattr(node, name, []) or []:
            if isinstance(child, ast.stmt):
                yield child
    for handler in getattr(node, "handlers", []) or []:
        for child in handler.body:
            yield child


def _source(node, limit=90):
    try:
        text = ast.unparse(node)
    except Exception:
        return "<expression>"
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
