"""Every relation of every model, checked against the same invariants.

Generated from the app registry rather than written out by hand. A hand-written
list of cases encodes the cases its author thought of; this one encodes the
space Django actually has, so adding a model to `tests.testapp` extends the
matrix automatically and a shape nobody anticipated still gets checked.

The invariants are the ones that were violated by real bugs: a plan's accessor
must exist on an instance, `can_select_related` must be exactly right rather
than merely safe, prefetching must never raise, and the vocabulary and the
benchmark must agree about what kind of relation they are looking at.
"""

import pytest
from django.core.exceptions import FieldError, ObjectDoesNotExist
from django.db.models import Manager

from django_fk_optimize.analysis.benchmark import Benchmark, plan_for, plans_for
from django_fk_optimize.utils.vocabulary import Vocabulary


def app_models():
    from django.apps import apps

    return [m for m in apps.get_models() if m._meta.app_label == "testapp"]


def every_relation():
    """(model, plan) for every relation the command would ever time."""
    cases = []
    for model in app_models():
        for plan in plans_for(model):
            cases.append(pytest.param(model, plan, id=f"{model.__name__}.{plan.name}"))
    return cases


def every_field():
    cases = []
    for model in app_models():
        for field in model._meta.get_fields():
            name = getattr(field, "name", type(field).__name__)
            cases.append(pytest.param(model, field, id=f"{model.__name__}.{name}"))
    return cases


RELATIONS = every_relation()
FIELDS = every_field()


def test_the_matrix_is_not_empty():
    """A guard on the generator itself.

    If `plans_for` ever returns nothing, every parametrized test below would
    vacuously pass and the suite would go green with no coverage at all.
    """
    assert len(RELATIONS) > 15, f"only {len(RELATIONS)} relations generated"
    assert len({model for model, _plan in (p.values for p in RELATIONS)}) > 5


@pytest.mark.parametrize("model,plan", RELATIONS)
def test_the_accessor_exists_on_an_instance(model, plan, menagerie):
    """`getattr(row, accessor)` must not raise AttributeError.

    This is the bug the project started from: a reverse relation's `name` is
    the related_query_name ("book"), while the accessor is "book_set".
    """
    row = model.objects.first()
    if row is None:
        pytest.skip(f"no rows for {model.__name__}")
    try:
        getattr(row, plan.accessor)
    except ObjectDoesNotExist:
        pass  # a reverse one-to-one with nothing on the other side


@pytest.mark.parametrize("model,plan", RELATIONS)
def test_select_related_is_legal_exactly_when_the_plan_says_so(model, plan, menagerie):
    """Tight, not merely safe.

    Asserting only that a permitted join works would let the classification
    quietly refuse joins that are legal -- which is how the reverse one-to-one
    ended up recommended as a two-query prefetch.
    """
    try:
        list(model.objects.select_related(plan.name)[:1])
    except FieldError:
        assert not plan.can_select_related, (
            f"{model.__name__}.{plan.name} can be joined but the plan refuses it"
        )
    else:
        assert plan.can_select_related, (
            f"{model.__name__}.{plan.name} is joinable and the plan missed it"
        )


@pytest.mark.parametrize("model,plan", RELATIONS)
def test_prefetch_related_is_always_legal(model, plan, menagerie):
    list(model.objects.prefetch_related(plan.name)[:1])


@pytest.mark.parametrize("model,plan", RELATIONS)
def test_compare_survives_every_relation(model, plan, menagerie):
    result = Benchmark(sample_size=5, repeat=1).compare(model, plan)

    assert result.plan is plan
    assert result.basis in ("measured", "structural")
    assert set(result.measurements) <= set(plan.strategies), (
        "a strategy was timed that the plan does not permit"
    )
    for operation, measurement in result.measurements.items():
        assert measurement.queries >= 1, operation


@pytest.mark.parametrize("model,plan", RELATIONS)
def test_the_manager_flag_matches_what_the_attribute_returns(model, plan, menagerie):
    """`plan.many` and `Relation.manager` are claims about runtime behaviour."""
    row = model.objects.first()
    if row is None:
        pytest.skip(f"no rows for {model.__name__}")
    try:
        value = getattr(row, plan.accessor)
    except ObjectDoesNotExist:
        return
    assert isinstance(value, Manager) == plan.many, (
        f"{model.__name__}.{plan.accessor} returns {type(value).__name__}"
    )


@pytest.mark.parametrize("model,plan", RELATIONS)
def test_the_vocabulary_and_the_benchmark_agree(model, plan):
    """Two independent descriptions of the same relation, kept in step.

    They are built by different code from the same `_meta`, and a report that
    says "prefetch" while the timing measured a join is worse than either.
    """
    info = Vocabulary.from_models([model]).models[model._meta.label]
    described = info.relation(plan.accessor)
    if described is None:
        pytest.skip(f"{plan.accessor} is not in the vocabulary")

    assert described.joinable == plan.can_select_related
    assert described.manager == plan.many
    assert described.name == plan.name


@pytest.mark.parametrize("model,field", FIELDS)
def test_plan_for_never_raises_on_any_field(model, field):
    """Including the ones it is right to ignore.

    GenericForeignKey, a parent link, a hidden reverse relation and a plain
    column all reach this function, and returning None is the answer for all
    of them -- raising is not.
    """
    plan = plan_for(field)

    if plan is not None:
        assert plan.name and plan.accessor
        assert plan.strategies
