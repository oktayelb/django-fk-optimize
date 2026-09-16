"""A Vocabulary read out of source, for code that cannot be imported.

`Vocabulary.from_apps()` asks the app registry, which is exact and needs a
configured project: settings, installed dependencies, a database driver. That
is the right answer inside a project and the wrong one for scanning somebody
else's, where the whole point is to run the analyser over unfamiliar code
without standing the project up first.

So this builds the same Vocabulary from `ast` alone. It is deliberately
approximate and says so: a field added by a mixin the scan cannot resolve, a
model built by a factory, a `related_name` computed at import time -- all of
those are missed, and a missed relation shows up downstream as a call site the
scanner declines to explain rather than as a wrong answer.

What it is *for* is breadth. Pointed at a corpus of real projects it exercises
the parser, the import resolver and the relation classifier against code nobody
wrote for the test suite, which is where the shapes nobody anticipated live.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

from .sources import SKIP_DIRECTORIES, python_files
from .vocabulary import (
    FORWARD,
    MANY_TO_MANY,
    REVERSE,
    REVERSE_ONE_TO_ONE,
    ModelInfo,
    Relation,
    Vocabulary,
)

FORWARD_FIELDS = {"ForeignKey", "OneToOneField"}
M2M_FIELDS = {"ManyToManyField"}
RELATION_FIELDS = FORWARD_FIELDS | M2M_FIELDS

# A class is a model when a base is spelled like one -- or when a base is
# itself a model, which has to be worked out rather than assumed. django-oscar
# writes `class Product(AbstractProduct)`, and a name-only test finds no
# concrete models in the whole project: 206 abstract classes and not one of
# the classes anybody queries.
MODEL_BASE_HINT = "Model"


@dataclass
class Stats:
    """What the pass could and could not account for."""

    files: int = 0
    parsed: int = 0
    errors: list[tuple[str, str]] = dataclass_field(default_factory=list)
    models: int = 0
    forward: int = 0
    reverse: int = 0
    unresolved_targets: list[str] = dataclass_field(default_factory=list)

    @property
    def relations(self) -> int:
        return self.forward + self.reverse


@dataclass
class _Declared:
    """One class, before model-ness and targets have been worked out."""

    name: str
    label: str
    module: str
    bases: tuple[str, ...] = ()
    looks_like_model: bool = False
    # (accessor, target_text, kind, related_name, nullable)
    fields: list[tuple[str, str, str, str | None, bool]] = dataclass_field(
        default_factory=list
    )


def _base_names(node: ast.ClassDef) -> tuple[str, ...]:
    names = []
    for base in node.bases:
        text = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        if text:
            names.append(text)
    return tuple(names)


def _looks_like_model(bases: tuple[str, ...]) -> bool:
    return any(MODEL_BASE_HINT in text for text in bases)


def _models_among(declared: list[_Declared]) -> set[str]:
    """Which declared classes are models, by closure over their bases.

    Seeded with the ones naming a base spelled like a model, then widened
    until it stops growing: a class whose base is a model is a model. Without
    the closure a project that puts its fields on abstract bases -- which is
    the recommended way to write a pluggable Django app -- registers nothing
    that anybody actually queries.
    """
    by_name: dict[str, list[_Declared]] = {}
    for item in declared:
        by_name.setdefault(item.name, []).append(item)

    models = {item.name for item in declared if item.looks_like_model}
    while True:
        grown = {
            item.name
            for item in declared
            if item.name not in models and any(base in models for base in item.bases)
        }
        if not grown:
            return models
        models |= grown


def _inherited_fields(item: _Declared, by_name, seen=None) -> list:
    """`item`'s own relations plus every base's, nearest last.

    `class Product(AbstractProduct)` carries all of AbstractProduct's foreign
    keys and declares none of its own; reading only the subclass finds a model
    with no relations at all.
    """
    seen = seen if seen is not None else set()
    if item.name in seen:
        return list(item.fields)  # a cycle; own fields are still real
    seen.add(item.name)

    collected = []
    for base in item.bases:
        found = by_name.get(base) or []
        if len(found) == 1:
            collected.extend(_inherited_fields(found[0], by_name, seen))
    collected.extend(item.fields)

    # Nearest definition wins, so a subclass can override a base's field.
    merged = {}
    for field in collected:
        merged[field[0]] = field
    return list(merged.values())


def _call_name(node) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _target_text(call: ast.Call) -> str:
    if not call.args:
        for keyword in call.keywords:
            if keyword.arg == "to":
                return _literal(keyword.value)
        return ""
    return _literal(call.args[0])


def _literal(node) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _kwarg(call: ast.Call, name: str):
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _app_label(path: Path, root: Path) -> str:
    """The app a file belongs to, guessed from the directory holding it.

    Django's real label comes from the AppConfig. Without importing, the
    directory containing `models.py` is the best available stand-in and is
    right for the overwhelming majority of projects.
    """
    parent = path.parent
    if parent.name == "models":  # a models/ package
        parent = parent.parent
    return parent.name or root.name


def _module(path: Path, root: Path) -> str:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return path.stem
    parts = list(relative.with_suffix("").parts)
    return ".".join(parts)


def _declare(tree: ast.Module, path: Path, root: Path, stats: Stats):
    label_app = _app_label(path, root)
    module = _module(path, root)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = _base_names(node)
        declared = _Declared(
            name=node.name,
            label=f"{label_app}.{node.name}",
            module=module,
            bases=bases,
            looks_like_model=_looks_like_model(bases),
        )
        for statement in node.body:
            if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                continue
            target = statement.targets[0]
            if not isinstance(target, ast.Name):
                continue
            call = statement.value
            if not isinstance(call, ast.Call):
                continue
            field = _call_name(call.func)
            if field not in RELATION_FIELDS:
                continue
            related = _kwarg(call, "related_name")
            null = _kwarg(call, "null")
            declared.fields.append(
                (
                    target.id,
                    _target_text(call) or "",
                    MANY_TO_MANY if field in M2M_FIELDS else FORWARD,
                    _literal(related) if related is not None else None,
                    bool(getattr(null, "value", False)),
                )
            )
        found.append(declared)
    return found


def _resolve(declared: list[_Declared], stats: Stats) -> Vocabulary:
    all_by_name: dict[str, list[_Declared]] = {}
    for item in declared:
        all_by_name.setdefault(item.name, []).append(item)

    # Fields are inherited before model-ness is applied, so an abstract base
    # can carry the relations for the concrete class that nobody declares.
    for item in declared:
        item.fields = _inherited_fields(item, all_by_name)

    models = _models_among(declared)
    declared = [item for item in declared if item.name in models]
    stats.models += len(declared)

    by_name: dict[str, list[_Declared]] = {}
    for item in declared:
        by_name.setdefault(item.name, []).append(item)

    def label_for(text: str, owner: _Declared) -> str:
        if not text:
            return ""
        if text == "self":
            return owner.label
        if "." in text:  # "app.Model"
            bare = text.split(".")[-1]
        else:
            bare = text
        found = by_name.get(bare) or []
        # Two apps may both define `Comment`. Refusing is cheaper than
        # attributing a relation to the wrong model.
        return found[0].label if len(found) == 1 else ""

    infos: dict[str, ModelInfo] = {}
    for item in declared:
        infos[item.label] = ModelInfo(
            label=item.label,
            name=item.name,
            module=item.module,
            relations={},
            managers=("objects",),
            field_names=frozenset(name for name, _t, _k, _r, _n in item.fields),
            all_relation_names=frozenset(name for name, _t, _k, _r, _n in item.fields),
        )

    # A class other model classes inherit from is an abstract base often
    # enough to treat it as one: `AbstractLine` and its concrete `Line` both
    # carry the same foreign key, and synthesising the reverse accessor from
    # both gives `abstractline_set`, which no code ever writes. The concrete
    # leaf is the one whose name Django would use.
    base_names = {base for item in declared for base in item.bases}

    for item in declared:
        info = infos[item.label]
        originates_reverse = item.name not in base_names
        for name, text, kind, related_name, null in item.fields:
            target = label_for(text, item)
            if not target:
                stats.unresolved_targets.append(f"{item.label}.{name} -> {text or '?'}")
                continue
            info.relations[name] = Relation(
                name=name,
                attname=f"{name}_id" if kind == FORWARD else "",
                target=target,
                null=null,
                kind=kind,
            )
            stats.forward += 1

            if related_name == "+" or not originates_reverse:
                continue  # no reverse accessor, or an abstract base's copy
            other = infos.get(target)
            if other is None:
                continue
            accessor = related_name or (
                f"{item.name.lower()}_set"
                if kind == FORWARD
                else f"{item.name.lower()}_set"
            )
            if accessor in other.relations:
                continue
            other.relations[accessor] = Relation(
                name=accessor,
                attname="",
                target=item.label,
                null=True,
                kind=MANY_TO_MANY if kind == MANY_TO_MANY else REVERSE,
            )
            stats.reverse += 1

    return Vocabulary(models=infos)


def from_tree(root, skip=SKIP_DIRECTORIES) -> tuple[Vocabulary, Stats]:
    """Read every `models.py` under `root` and build what can be read.

    Never raises on unreadable or unparseable input: a corpus scan that dies on
    one file in ten thousand tells you nothing about the other 9999.
    """
    root = Path(root)
    stats = Stats()
    declared: list[_Declared] = []

    for path in python_files(root, skip):
        # Every file, not just models.py. Real projects put models in
        # abstract_models.py, in a models/ package, in base.py, or beside the
        # views that use them; django-oscar's models.py files are mostly
        # `from .abstract_models import *`, so a models.py-only pass finds
        # almost nothing there.
        stats.files += 1
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
            stats.errors.append((str(path), f"{type(exc).__name__}: {exc}"))
            continue
        stats.parsed += 1
        declared.extend(_declare(tree, path, root, stats))

    return _resolve(declared, stats), stats


# `REVERSE_ONE_TO_ONE` is imported for the kind constants to stay in one place
# even though a static read cannot tell a reverse one-to-one from a reverse
# many-to-one without resolving the field class.
_ = REVERSE_ONE_TO_ONE
