"""Recording: what the database was actually asked for, and from where.

`record()` is the entry point for anything that is not an HTTP request -- a
celery task, a script, a test.  `FkOptimizeMiddleware` is the same thing around
a request.  `suppressed()` is how the analysis layer keeps its own benchmark
queries out of the file it is about to read.
"""

from .store import (
    BULK,
    OTHER,
    SINGLE_ROW,
    Attribution,
    QueryGroup,
    Record,
    Recording,
    append,
    clear,
    count_lines,
    group,
    kind_of,
    load,
    table_of,
)
from .wrapper import (
    PYTHON,
    SERIALIZER,
    TEMPLATE,
    Recorder,
    attribute,
    is_disabled,
    is_recording,
    is_suppressed,
    normalise,
    record,
    reset,
    shape_hash,
    suppressed,
)

__all__ = [
    "Attribution",
    "BULK",
    "OTHER",
    "PYTHON",
    "QueryGroup",
    "Record",
    "Recorder",
    "Recording",
    "SERIALIZER",
    "SINGLE_ROW",
    "TEMPLATE",
    "append",
    "attribute",
    "clear",
    "count_lines",
    "group",
    "is_disabled",
    "is_recording",
    "is_suppressed",
    "kind_of",
    "load",
    "normalise",
    "record",
    "reset",
    "shape_hash",
    "suppressed",
    "table_of",
]
