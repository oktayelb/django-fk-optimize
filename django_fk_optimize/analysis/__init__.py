"""The analysis layer: measure, count, join, decide, render.

Four modules, in the order the command runs them:

* `benchmark` classifies a relation and times the strategies it supports.
* `cardinality` counts rows and distinct targets, which is where N comes from
  when there is no recording to observe it.
* `verdicts` joins the static call sites to the recorded queries and decides
  what, if anything, is worth changing.
* `report` renders that as text or as JSON.

Nothing above `benchmark` issues a query without `recording.suppressed()`
around it: this package reads a recording of the application's queries and
must never find its own in there.
"""

from .benchmark import (
    ALL_STRATEGIES,
    DEFAULT_REPEAT,
    DEFAULT_SAMPLE_SIZE,
    FORWARD,
    MANY_TO_MANY,
    NO_JOIN_STRATEGIES,
    REVERSE,
    REVERSE_ONE_TO_ONE,
    Benchmark,
    Deadline,
    FieldOperation,
    Measurement,
    RelationPlan,
    RelationResult,
    plan_for,
    plan_named,
    plans_for,
)

__all__ = [
    "ALL_STRATEGIES",
    "Benchmark",
    "DEFAULT_REPEAT",
    "DEFAULT_SAMPLE_SIZE",
    "Deadline",
    "FORWARD",
    "FieldOperation",
    "MANY_TO_MANY",
    "Measurement",
    "NO_JOIN_STRATEGIES",
    "REVERSE",
    "REVERSE_ONE_TO_ONE",
    "RelationPlan",
    "RelationResult",
    "plan_for",
    "plan_named",
    "plans_for",
]
