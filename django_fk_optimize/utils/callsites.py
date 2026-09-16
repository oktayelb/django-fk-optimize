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

    def covers(self, name: str) -> bool:
        return name in self.select or name in self.prefetch

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
    bound: int | None = None
    bound_reason: str = ""
    escapes: bool = False
    confidence: str = RESOLVED
    notes: tuple[str, ...] = ()

    @property
    def missing(self) -> tuple[str, ...]:
        """Relations this site touches with no hint covering them."""
        return tuple(name for name in self.touched if not self.hints.covers(name))

    @property
    def unused(self) -> tuple[str, ...]:
        """Hints for relations this site never touches -- a join for nothing."""
        if self.escapes or self.hints.opaque:
            return ()
        hinted = tuple(self.hints.select) + tuple(self.hints.prefetch)
        return tuple(name for name in hinted if name not in self.touched)


@dataclass
class ScanReport:
    """Sites found, plus what the scan could not account for.

    The unresolved list is not noise to be tidied away.  A scanner that reports
    only what it understood reads as a clean bill of health for the files it
    failed on, which is the one failure mode that makes a tool like this worse
    than nothing.
    """

    sites: list[CallSite] = dataclass_field(default_factory=list)
    unresolved: list[tuple[str, int, str]] = dataclass_field(default_factory=list)
    errors: list[tuple[str, str]] = dataclass_field(default_factory=list)
    files: int = 0

    def extend(self, other: ScanReport):
        self.sites.extend(other.sites)
        self.unresolved.extend(other.unresolved)
        self.errors.extend(other.errors)
        self.files += other.files


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

    def run(self):
        self._scope_of(self.tree.body, {})

    # -- scopes --------------------------------------------------------

    def _scope_of(self, body, inherited):
        """Walk one scope, then look for touches on the instances it bound.

        Bindings are last-write-wins in source order; branches are not tracked.
        A name reassigned in one arm of an if is the price of staying a scanner
        rather than becoming an interpreter.
        """
        scope = dict(inherited)
        instances: dict[str, tuple[Binding, ast.AST]] = {}

        for node in body:
            self._statement(node, scope, instances)

        for name, (binding, origin) in instances.items():
            touched, id_only, escapes = _touches(body, name, binding.model)
            if not touched and not escapes:
                continue
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
                    bound=binding.bound if binding.bound is not None else 1,
                    bound_reason=binding.bound_reason or "single object",
                    escapes=escapes,
                    confidence=PROBABLE if escapes else binding.confidence,
                    notes=binding.notes
                    + (("instance escapes into a call",) if escapes else ()),
                )
            )

    def _statement(self, node, scope, instances):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self._scope_of(node.body, scope)
            return

        if isinstance(node, ast.Assign):
            self._assign(node, scope, instances)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            self._loop(node, scope)

        for child in _child_statements(node):
            self._statement(child, scope, instances)

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
            self._comprehension(sub, scope)

    def _assign(self, node, scope, instances):
        binding = self._resolve(node.value, scope)
        if binding is None or binding is TERMINAL_CHAIN:
            return
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            scope[target.id] = binding
            if binding.kind == INSTANCE:
                instances[target.id] = (binding, node.value)

    def _loop(self, node, scope):
        binding = self._resolve(node.iter, scope)
        if binding is TERMINAL_CHAIN:
            return
        if binding is None:
            self._maybe_unresolved(node.iter)
            return
        if binding.kind == INSTANCE:
            return
        if isinstance(node.target, ast.Name):
            self._record_iteration(node.iter, node.target.id, binding, node.body)

    def _comprehension(self, node, scope):
        for generator in node.generators:
            binding = self._resolve(generator.iter, scope)
            if binding is TERMINAL_CHAIN:
                continue
            if binding is None:
                self._maybe_unresolved(generator.iter)
                continue
            if binding.kind == INSTANCE or not isinstance(generator.target, ast.Name):
                continue
            body = [node.elt] if hasattr(node, "elt") else [node.key, node.value]
            self._record_iteration(
                generator.iter,
                generator.target.id,
                binding,
                body + list(generator.ifs),
            )

    def _record_iteration(self, origin, varname, binding, body):
        touched, id_only, escapes = _touches(body, varname, binding.model)
        # `for a in qs:` says nothing about what qs is; the binding remembers.
        expression = _source(origin)
        if isinstance(origin, ast.Name) and binding.origin:
            expression = f"{binding.origin}  (as {origin.id})"
        confidence = binding.confidence
        notes = binding.notes
        if escapes:
            confidence = PROBABLE
            notes = notes + ("row escapes into a call; touches may be elsewhere",)
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
                bound=binding.bound,
                bound_reason=binding.bound_reason,
                escapes=escapes,
                confidence=confidence,
                notes=notes,
            )
        )

    def _maybe_unresolved(self, node):
        """Record a loop that looked like a queryset but would not resolve."""
        text = _source(node)
        if any(f".{name}" in text for name in self._managers):
            self.unresolved.append((self.path, getattr(node, "lineno", 0), text))

    # -- resolution ----------------------------------------------------

    def _model_from_name(self, name):
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
        if binding is None:
            info = self._model_from_name(base.id)
            if info is None:
                return None
            if not steps or steps[0][0] != "attr" or steps[0][1] not in info.managers:
                return None
            binding = Binding(kind=ITERATION, model=info, origin=_source(node))
            index = 1
        else:
            binding = Binding(**vars(binding))

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


def _touches(nodes, varname, info):
    """Relations of `info` reached through `varname` anywhere in `nodes`."""
    touched, id_only, escapes = [], [], False
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                if sub.value.id != varname:
                    continue
                if info.relation(sub.attr) is not None:
                    touched.append(sub.attr)
                elif sub.attr in info.attnames:
                    # alarm.type_id is already in the row. Recorded so the
                    # report can say "you are fine" rather than say nothing.
                    id_only.append(sub.attr)
            elif isinstance(sub, ast.Call):
                for arg in list(sub.args) + [kw.value for kw in sub.keywords]:
                    if isinstance(arg, ast.Name) and arg.id == varname:
                        escapes = True
    return tuple(dict.fromkeys(touched)), tuple(dict.fromkeys(id_only)), escapes


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
