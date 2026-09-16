# django-fk-optimize

[![CI](https://github.com/oktayelb/django-fk-optimize/actions/workflows/ci.yml/badge.svg)](https://github.com/oktayelb/django-fk-optimize/actions/workflows/ci.yml)

Finds the foreign keys your code loads one row at a time, and measures what
`select_related()` or `prefetch_related()` would actually save on your data.

A management command reads your source to find the querysets, reads a recording
of your own traffic to find out how many rows each one really loads, and then
times the alternatives against your database. It prints the line to change, the
change to make, and what it costs now versus what it would cost. When nothing
is worth changing it says so in one line instead of printing a table.

Which of the two hints is right is not something the model definition can tell
you. It depends on how many rows come back and how many distinct targets they
point at, and that is a property of your data, so it has to be measured.

Requires Django 4.2 or newer and Python 3.10 or newer. No dependencies beyond
Django.

## Install

Not published to PyPI yet. Until it is:

```bash
pip install "git+https://github.com/oktayelb/django-fk-optimize"
```

Add it to `INSTALLED_APPS`, which is what makes the management command
available:

```python
INSTALLED_APPS = [
    ...,
    "django_fk_optimize",
]
```

## Quickstart

**1. Record what your app really does.** Add the middleware:

```python
MIDDLEWARE = [
    ...,
    "django_fk_optimize.middleware.FkOptimizeMiddleware",
]
```

**2. Use the app.** Click through the pages you care about, run your test suite,
let the staging traffic in — whatever exercises the code you want judged. Every
query is appended to `.fk_optimize/recording.jsonl` as a normalised shape and
the line of your code that caused it. Parameters are never written down.

**3. Ask.**

```bash
python manage.py fk_optimize
```

That is it. Narrow it to one app or one model by naming it:

```bash
python manage.py fk_optimize shop
python manage.py fk_optimize shop.Book
```

## What you get

```
1 change worth making

shop/views.py:6  catalogue()                      shop.Book.publisher
  rows        120  (observed)
  what        120 extra queries, one per row
  current     1 + 120 queries     1.4 ms recorded sql / 15.2 ms measured
  best        select_related    1 query     0.7 ms measured
  alternative prefetch_related  2 queries   0.8 ms measured
  fix         Book.objects.all().select_related("publisher")
  saving      ~14.5 ms per call (95%), 120 fewer queries  confidence: resolved

2 already fine
  shop/views.py:12                    shop.Book.publisher         already covered by select_related("publisher")
  shop/views.py:17                    shop.Book.publisher         publisher_id is already on the row; nothing to do

coverage
  scanned     24 files, 4 models, 3 call sites (3 resolved, 0 probable)
  not seen    0 querysets the scanner could not follow, 0 files it could not parse
  recording   .fk_optimize/recording.jsonl  121 records, 0 malformed, 2 query groups, 0s old
  traced      1 to a call site, 0 with no call site, 0 to no model at all
  benchmark   1 relation timed
```

Line by line:

- `shop/views.py:6 catalogue()` — the queryset, the function it is in, and the
  relation being loaded. Paste the first part into your editor.
- `rows 120 (observed)` — how many rows that queryset returned. `observed` means
  it was counted in the recording. `estimated` means it came from a `COUNT(*)`
  because there was no recording to count it in, and `static bound` means the
  code caps it itself with a slice or a `get()`. The report always says which.
- `current` / `best` / `alternative` — the query count and the time for each
  strategy, measured just now on your database over the same slice of rows.
  Query counts are deterministic; the milliseconds are not, which is exactly why
  both are printed. `recorded sql` is what your own traffic spent on the extra
  queries; `measured` is what this run spent reproducing them.
- `fix` — the one-line change, written out.
- `confidence: resolved` — the static scan and the recording agreed on both the
  function and the table. `probable` means only one of them matched.
- `already fine` — sites there is nothing to do about. Touching only
  `book.publisher_id` never needs a hint: the column is already on the row.
- `coverage` — what the run could and could not see. A report that hides what it
  failed on reads like a clean bill of health for exactly the code it missed.

## The two modes of the default report

The command is useful either way; the recording changes how much it knows.

**Without a recording**, the scanner still finds every queryset and the row
counts come from `COUNT(*)`. Verdicts say `estimated`, and the report tells you
how to do better:

```
  rows        120  (estimated)
  current     121 queries         14.8 ms measured
...
  recording   none at .fk_optimize/recording.jsonl

  no recording: N was estimated from row counts. For observed numbers, add
  "django_fk_optimize.middleware.FkOptimizeMiddleware" to MIDDLEWARE and
  exercise the pages you care about, or wrap a script in
  django_fk_optimize.recording.record().
```

**With a recording**, N is counted rather than guessed, the report can show what
the queries really cost in production shape, and it also catches the accesses no
source scan can ever see — a `{{ book.publisher.name }}` in a template, a DRF
serializer field. Those are reported against the view that built the queryset,
which is where the fix goes anyway.

## Recording from outside a request

The recorder is built on Django's `connection.execute_wrapper`, so it works
anywhere, not just in a view. The middleware is a thin shim around a request;
`record()` is the same shim for everything else — a celery task, a script, a
test, a management command:

```python
from django_fk_optimize.recording import record

with record(".fk_optimize/recording.jsonl"):
    build_the_nightly_report()
```

With no argument it writes to `FK_OPTIMIZE["RECORDING_PATH"]`. The file is
append-only JSONL, so several processes can write to it and you can keep adding
to one recording over a session.

## The per-relation sweep

```bash
python manage.py fk_optimize shop.Book --no-callsites
```

This is a different question, not a lesser one. Instead of asking "what should
this line do", it asks "what does each relation on this model cost", and times
every one of them on its own and then all together:

```
shop.Book -- foreign key optimization results
Per-relation timings (median of 5 runs, at most 500 rows):
#   relation                kind         winner                           vanilla        select_related      prefetch_related
1   publisher               forward      select_related           0.014253s  121q       0.000701s    1q       0.000772s    2q
2   author                  forward      select_related           0.014569s  121q       0.000723s    1q       0.000898s    2q
3   editor                  forward      vanilla                  0.000529s    1q       0.000656s    1q       0.000692s    1q
4   tags                    m2m          prefetch_related         0.017176s  121q                   n/a       0.002901s    2q

Suggested operations (each relation measured on its own -- see the combined result below before applying them together):
select_related: 2, prefetch_related: 1, vanilla: 1

Combined queryset timings:
No optimization:        0.054572s in 361 queries
Suggested optimization: 0.003634s in 2 queries
Suggested optimization is faster by 0.050937s (93.34%), 359 fewer queries.
```

Two things only this mode gives you:

- **Reverse and many-to-many relations.** The verdict engine judges forward
  many-to-one relations only. The sweep times every relation kind, including
  `tags` above.
- **The combination.** Joins multiply, so the per-relation winners applied
  together can be slower than the N+1 they replaced. The combined line at the
  bottom is there to catch that, and the report says so when it happens.

## Options

```
python manage.py fk_optimize [app_label | app_label.ModelName] [options]
```

| option | what it does |
|---|---|
| `--callsites` / `--no-callsites` | scan the source and join it to the recording, or fall back to the per-relation sweep. Default: on. |
| `--benchmark` / `--no-benchmark` | time the alternatives against the database. Default: on. |
| `--recording PATH` | the JSONL recording to read. Default: `FK_OPTIMIZE["RECORDING_PATH"]`. |
| `--clear-recording` | delete the recording once it has been read. |
| `--sample-size N` | rows per timed queryset, and the cap on an estimated N. Default: 500. |
| `--repeat K` | timed runs per strategy after a discarded warmup; the median is reported. Default: 5. |
| `--timeout SECONDS` | wall-clock budget for the whole run. Partial results are still printed, and the report says it was cut short. |
| `--min-rows N` | ignore relations on tables with fewer rows than this. Default: 0. |
| `--json [PATH]` | write the report as JSON. |
| `--fail-on-findings` | exit 1 when any actionable verdict exists. |
| `--include-django` | include Django's own and third-party apps. Off by default. |
| `--django-models` | deprecated alias for `--include-django`. |

Two things to know about `--json`:

- **Put the app or model first.** `--json` takes an *optional* value, so
  `fk_optimize --json shop` reads `shop` as the output path and reports on the
  whole project. Write `fk_optimize shop --json`.
- With no path it replaces the text report on stdout. With a path it writes the
  file **and** still prints the text report.

The payload is `{schema_version, generated_at, verdicts: [...], coverage: {...}}`,
one object per verdict with the model, relation, row count and its provenance,
every measurement, the saving and the exact fix.

## Settings

Every key is optional, and a bad value falls back to its default rather than
raising — a typo in a settings dict should not take an application down for the
sake of a profiler.

```python
FK_OPTIMIZE = {
    "RECORDING_PATH": ".fk_optimize/recording.jsonl",
    "ENABLED": True,        # False makes the middleware a no-op
    "SAMPLE_SIZE": 500,     # default for --sample-size
    "MAX_RECORDS": 100_000, # the recorder stops appending past this
    "SAMPLE_RATE": 1.0,     # fraction of requests to record
}
```

`MAX_RECORDS` and `SAMPLE_RATE` are what make the middleware safe to leave on
in a shared environment. The recorder buffers one request in memory and writes
once at the end, never per query, and it swallows its own errors: a bug in here
turns the recording off, not the response into a 500.

## In CI

```yaml
- run: python manage.py fk_optimize --fail-on-findings
```

It exits 1 and names the findings:

```
CommandError: 1 actionable finding (--fail-on-findings)
```

Run it after whatever produces your recording — a test suite wrapped in
`record()` is the usual answer. Without one it still works, on estimated row
counts. `--min-rows` is useful here to keep a fixture table of four rows from
failing a build.

## How it works

Three parts, and no one of them is enough on its own.

- **The static scan** reads your source and finds which line iterates which
  model and touches which relation, and which hints are already there. It cannot
  know how many rows come back.
- **The recorder** knows exactly that, because it counted. Grouping the recorded
  queries by shape and by the line that caused them turns a repeated single-row
  lookup into an N+1 of a known size. It also sees the accesses that happen
  inside Django's template engine or a serializer, where there is no line of
  yours to scan.
- **The benchmark** prices the alternatives: it takes the same bounded slice of
  the real table and runs it with nothing, with `select_related()` and with
  `prefetch_related()`, one discarded warmup then `--repeat` runs, and reports
  the median and the query count.

The static half and the runtime half are joined on the enclosing function rather
than on the line — the scanner files the site where the queryset is built and
the recorder attributes the lazy load to the line that touched the relation, and
those are never the same line — and the match is confirmed by checking that the
relation's target table is the table the repeated query read.

## Limitations

- **Verdicts cover forward many-to-one relations only.** Those are the ones
  `select_related()` can join and the ones that produce the classic N+1. Reverse
  and many-to-many relations are covered by `--no-callsites`, which times them
  but cannot tell you where they are used.
- **Static analysis cannot see a template or a serializer.** `{{ book.publisher.name }}`
  and a DRF `source=` field are attribute access inside library code; no AST
  scan will ever find them. That is precisely why the recorder exists, and with
  a recording those show up as findings against the view. Without one, they are
  invisible.
- **A measurement is a measurement of the database you ran it against.** Verdicts
  from a dev database with 40 rows are verdicts about 40 rows. Run it against
  something with production-shaped data, or at least read the row counts in the
  report before believing the milliseconds.
- **Times are noisy; query counts are not.** Both are printed for that reason.
  If a verdict only makes sense because of the milliseconds, distrust it.
- **`MAX_RECORDS` is per-process and approximate.** Each worker counts its own
  records, so a project on eight workers can write up to eight times the limit,
  and the recording is a sample rather than a ledger.
- **The recording is append-only.** It grows until you delete it or pass
  `--clear-recording`. It holds query shapes and your own file and line numbers,
  never query parameters, but it is still a description of your codebase — keep
  it out of version control. `.fk_optimize/` is the default location for that
  reason.

## License

MIT. See [LICENSE](LICENSE).
