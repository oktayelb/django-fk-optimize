"""The closed vocabulary every scan matches against.

Static analysis of Django code does not need type inference.  The set of models,
their relations, their column attributes and their manager names is finite,
enumerable, and -- because this package runs inside Django -- available by
asking rather than by guessing.  So `alarm.type` is not "an attribute access on
something that might be a model"; it is a lookup in a table built here.

Nothing in this module imports Django at import time, so the scanners can be
exercised against a hand-built Vocabulary with no settings configured.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field

# How a relation behaves when a row touches it.  The distinction is not
# cosmetic: it decides which hint is legal, which is useful, and -- for the
# two manager kinds -- whether reading the attribute costs anything at all.
FORWARD = "forward"  # alarm.type      -> an instance, one query, joinable
REVERSE_ONE_TO_ONE = (
    "reverse1to1"  # author.profile  -> an instance, one query, joinable
)
REVERSE = "reverse"  # publisher.book_set -> a manager
MANY_TO_MANY = "m2m"  # book.tags       -> a manager

MANAGER_KINDS = frozenset({REVERSE, MANY_TO_MANY})
JOINABLE_KINDS = frozenset({FORWARD, REVERSE_ONE_TO_ONE})


@dataclass(frozen=True)
class Relation:
    """One relation, as the code that touches it sees it.

    `name` is the accessor: what a call site writes, and what the hint takes.
    `attname` is the column attribute and exists only on a forward relation;
    touching *that* is not a touch at all, since `alarm.type_id` is already
    loaded while `alarm.type` goes back to the database.

    `kind` decides everything downstream.  A forward many-to-one can be joined
    and is one query per row when it is not; a reverse many-to-one or a
    many-to-many hands back a *manager*, which is free to read and only costs
    something once it is consumed.
    """

    name: str
    attname: str
    target: str
    null: bool = False
    kind: str = FORWARD

    @property
    def manager(self) -> bool:
        """Whether the accessor returns a manager rather than an instance."""
        return self.kind in MANAGER_KINDS

    @property
    def joinable(self) -> bool:
        """Whether select_related() is legal on it."""
        return self.kind in JOINABLE_KINDS


@dataclass
class ModelInfo:
    label: str  # "alarms.Alarm"
    name: str  # "Alarm"
    module: str  # "alarms.models"
    relations: dict[str, Relation] = dataclass_field(default_factory=dict)
    managers: tuple[str, ...] = ("objects",)
    field_names: frozenset[str] = frozenset()

    # Every relation of any kind, so a scan can tell "a relation we do not
    # cover" apart from "not a relation", instead of silently dropping it.
    all_relation_names: frozenset[str] = frozenset()

    @property
    def qualname(self) -> str:
        return f"{self.module}.{self.name}"

    @property
    def attnames(self) -> frozenset[str]:
        return frozenset(rel.attname for rel in self.relations.values() if rel.attname)

    @property
    def forward_relations(self) -> dict[str, Relation]:
        """Only the forward many-to-one relations, for callers that still
        mean that and nothing else."""
        return {
            name: rel for name, rel in self.relations.items() if rel.kind == FORWARD
        }

    def relation(self, attr: str) -> Relation | None:
        return self.relations.get(attr)


@dataclass
class Vocabulary:
    models: dict[str, ModelInfo] = dataclass_field(default_factory=dict)

    def __post_init__(self):
        self._by_qualname: dict[str, ModelInfo] = {}
        self._by_name: dict[str, list[ModelInfo]] = {}
        for info in self.models.values():
            self._by_qualname[info.qualname] = info
            self._by_name.setdefault(info.name, []).append(info)

    # -- construction --------------------------------------------------

    @classmethod
    def from_apps(cls, include_django=False):
        """Every model in the project, straight from the app registry."""
        from django.apps.registry import apps

        local = {
            config.label
            for config in apps.get_app_configs()
            if include_django or not config.name.startswith("django.")
        }
        return cls.from_models(
            model for model in apps.get_models() if model._meta.app_label in local
        )

    @classmethod
    def from_models(cls, model_classes):
        return cls(
            models={model._meta.label: _describe(model) for model in model_classes}
        )

    # -- lookup --------------------------------------------------------

    def by_qualname(self, qualname: str) -> ModelInfo | None:
        return self._by_qualname.get(qualname)

    def by_name(self, name: str) -> ModelInfo | None:
        """A model by bare class name, or None when the name is ambiguous.

        Two apps may both define `Comment`.  Returning None there is the point:
        a scan that guessed would attribute call sites to the wrong model, and
        an unresolved call site is a far cheaper mistake than a confident wrong
        one.
        """
        found = self._by_name.get(name) or []
        return found[0] if len(found) == 1 else None

    def __contains__(self, label):
        return label in self.models

    def __len__(self):
        return len(self.models)


def _relation(field, ForeignObjectRel) -> Relation | None:
    """One `_meta.get_fields()` entry as a Relation, or None to ignore it.

    The accessor is the key throughout: it is what the call site writes and
    what the hint takes.  For a reverse relation that is `get_accessor_name()`
    ("book_set"), never `.name`, which is the related_query_name ("book") and
    belongs in a filter rather than on an instance.  Conflating the two is the
    original bug this project was built around.

    None means there is nothing here a hint could help: a hidden reverse
    relation has no accessor, a parent link is joined by inheritance whatever
    we do, and a GenericForeignKey has no single related model to point at.
    """
    related = getattr(field, "related_model", None)
    if related is None:
        return None

    if isinstance(field, ForeignObjectRel):
        if field.hidden:
            return None
        accessor = field.get_accessor_name()
        if not accessor:
            return None
        if field.many_to_many:
            kind = MANY_TO_MANY
        elif field.one_to_one:
            kind = REVERSE_ONE_TO_ONE
        else:
            kind = REVERSE
        return Relation(
            name=accessor,
            attname="",
            target=related._meta.label,
            null=True,  # there may simply be nothing on the other side
            kind=kind,
        )

    if field.many_to_many:
        return Relation(
            name=field.name,
            attname="",
            target=related._meta.label,
            null=True,
            kind=MANY_TO_MANY,
        )
    if getattr(field.remote_field, "parent_link", False):
        return None
    if field.many_to_one or field.one_to_one:
        return Relation(
            name=field.name,
            attname=field.attname,
            target=related._meta.label,
            null=bool(field.null),
            kind=FORWARD,
        )
    return None


def _describe(model) -> ModelInfo:
    from django.db.models.fields.reverse_related import ForeignObjectRel

    relations, all_relations, field_names = {}, set(), set()
    for field in model._meta.get_fields():
        name = getattr(field, "name", None)
        if name:
            field_names.add(name)
        if not getattr(field, "is_relation", False):
            continue
        if name:
            all_relations.add(name)

        described = _relation(field, ForeignObjectRel)
        if described is not None:
            relations[described.name] = described

    return ModelInfo(
        label=model._meta.label,
        name=model.__name__,
        module=model.__module__,
        relations=relations,
        managers=tuple(model._meta.managers_map) or ("objects",),
        field_names=frozenset(field_names),
        all_relation_names=frozenset(all_relations),
    )
