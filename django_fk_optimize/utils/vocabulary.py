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


@dataclass(frozen=True)
class Relation:
    """A forward many-to-one -- the only relation kind this project optimizes.

    `name` is what a call site touches and what select_related() takes.
    `attname` is the column attribute, and touching *that* is not a touch at
    all: `alarm.type_id` is already loaded and costs no query, while
    `alarm.type` and even `alarm.type.pk` go back to the database.
    """

    name: str
    attname: str
    target: str
    null: bool = False


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
        return frozenset(rel.attname for rel in self.relations.values())

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

        # The same test m2o_optimize uses, for the same reason: many_to_one is
        # true only on the forward side, which rules out reverse relations, m2m
        # and one-to-one in one go.
        if not field.many_to_one or field.related_model is None:
            continue
        if isinstance(field, ForeignObjectRel):
            continue
        if getattr(field.remote_field, "parent_link", False):
            continue

        relations[field.name] = Relation(
            name=field.name,
            attname=field.attname,
            target=field.related_model._meta.label,
            null=bool(field.null),
        )

    return ModelInfo(
        label=model._meta.label,
        name=model.__name__,
        module=model.__module__,
        relations=relations,
        managers=tuple(model._meta.managers_map) or ("objects",),
        field_names=frozenset(field_names),
        all_relation_names=frozenset(all_relations),
    )
