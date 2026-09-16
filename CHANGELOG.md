# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.1.0 — unreleased

First packaged release. `manage.py fk_optimize` measures what
`select_related()` and `prefetch_related()` actually cost on your own tables,
one relation at a time, instead of guessing from the model definitions.

### Fixed

- **`fk_optimize` no longer crashes on most models.** `Model._meta.get_fields()`
  returns `ForeignObjectRel` objects for reverse FK, reverse one-to-one and
  many-to-many relations. Those names were passed straight into
  `select_related()`, which raises `FieldError` on anything that is not a
  forward relation, and into `getattr()` under the related query name rather
  than the accessor (`book` instead of `book_set`), which raises
  `AttributeError`. Every relation is now classified first, and only forward
  many-to-one / one-to-one relations are offered `select_related()`.
- Touching a reverse or m2m relation now consumes the related manager. A
  manager issues no query until it is evaluated, so the N+1 the benchmark
  exists to provoke never happened and the strategies were compared on nothing.
- Hidden relations (`related_name="+"`), parent links from multi-table
  inheritance and `GenericForeignKey` are skipped rather than measured; none of
  them has an accessor or a single target table to join.
- `--timeout` is implemented. It was parsed and never read. It is now a real
  wall-clock deadline, checked between models and between relations; partial
  results are printed and the run says it was cut short.
- Every result table is labelled with `Model._meta.label`. A project-wide run
  used to print one anonymous table per model.
- When the per-relation winners applied together measure slower than no
  optimization at all, the report says so and tells you not to apply them as a
  set. Joins combine multiplicatively, so the combination can lose to the N+1
  it replaced.
- `.gitignore` no longer ignores itself, and no longer hides every `utils/`
  directory in the tree.
- The coverage block counts in English. It interpolated numbers straight into
  plural nouns, so a run that timed one relation reported `1 relations timed`.

### Added

- **`fk_optimize` now reports call sites, not just models.** The default run
  scans the project's source for querysets, reads the query recording, joins
  the two, and prints one block per finding: the row count and where that
  count came from, what the call site costs now, what the best alternative
  measures, the exact one-line change to make, and the confidence. When
  nothing is worth changing it says so in one line rather than printing an
  empty table.
- `django_fk_optimize.analysis`: `benchmark` (relation classification and
  bounded, repeated, median timing), `cardinality` (COUNT-based row and
  distinct-target stats), `verdicts` (the static/runtime join and the rules)
  and `report` (text and JSON rendering).
- The static and runtime halves are joined on the **enclosing function**, not
  on the line. The scanner files a call site at the line that built the
  queryset and the recorder attributes the lazy load to the line that touched
  the relation, so the two are never the same line. The narrowest containing
  scope wins, and the match is confirmed by resolving the site's relation to
  its target table and requiring that to be the table the repeated query read.
  Both signals agree: `resolved`. One of them: `probable`.
- Queries no AST could ever see — a `{{ book.publisher.name }}` in a template,
  a DRF serializer field — are reported as runtime-only findings with the
  relation inferred from the queried table, instead of being dropped.
- Every verdict says where its row count came from: `observed` from the
  recording, `static bound` from a `[:50]` or a `get()`, or `estimated` from a
  COUNT. An estimate is never printed as a measurement.
- A mandatory coverage block under every report and in every `--json` payload:
  files scanned, call sites resolved and probable, querysets the scanner could
  not follow, files it could not parse, records read, malformed lines, the age
  of the recording, and how many findings were traced to a call site.
- New flags: `--callsites/--no-callsites`, `--benchmark/--no-benchmark`,
  `--recording PATH`, `--clear-recording`, `--min-rows N`,
  `--json [PATH]`, `--fail-on-findings` (non-zero exit for CI) and
  `--include-django`.
- A call site whose only touch is the column attribute (`book.publisher_id`)
  is reported as already optimal. A hint for a relation the site never touches
  is offered for removal.
- Reverse one-to-one relations are offered `select_related()`. Django joins
  the reverse side of a `OneToOneField` and caches the absence of a row as
  well as its presence, so a parent with no child costs no extra query either.
- `--sample-size` (default 500) bounds every timed queryset. Timings used to
  walk the whole table, three times per relation plus warmups.
- `--repeat` (default 5) runs each strategy that many times after a discarded
  warmup and reports the median, instead of a single noisy `perf_counter` run.
- A query-count column per strategy, captured with `CaptureQueriesContext`.
  Unlike a duration, it is deterministic and reproducible on another machine.
- `django_fk_optimize.recording`: a query recorder built on Django's
  `connection.execute_wrapper`, so it fires for celery tasks, management
  commands and tests as well as for HTTP. Each query is stored as a normalised
  shape with the user line that caused it, never with its parameters, in an
  append-only JSONL file that can be folded into per-call-site groups. Bounded
  by `MAX_RECORDS` and `SAMPLE_RATE`, silent on failure, and suppressible so
  this package never records its own queries.
- `FkOptimizeMiddleware`: the HTTP entry point for the recorder, sync and
  async. Under ASGI the wrapper is installed on the thread that actually runs
  the ORM rather than on the event loop.
- An optional `FK_OPTIMIZE` settings dict — `RECORDING_PATH`, `ENABLED`,
  `SAMPLE_SIZE`, `MAX_RECORDS`, `SAMPLE_RATE` — read lazily, with every key
  optional and a bad value falling back to its default rather than raising.
- Call sites now carry their enclosing scope (`function`, `scope_start`,
  `scope_end`). A recorder attributes a lazy load to the line that touched the
  relation, which is never the line that built the queryset, so the function is
  what the static and runtime halves can be joined on.
- `django_fk_optimize.utils`: the static-analysis package behind the call-site
  scanner — model vocabulary, per-module import resolution, queryset call-site
  detection and source discovery — is now part of the distribution.
- Packaging metadata (`pyproject.toml`): setuptools backend, Django >= 4.2,
  Python >= 3.10, MIT, classifiers through Django 6.0 and Python 3.14, project
  URLs and a `dev` extra.
- A test suite that runs under plain `pytest` with no pytest-django, bootstrapping
  Django in `conftest.py` against in-memory sqlite. `FK_OPTIMIZE_TEST_DB=postgres`
  points the same suite at a real server through the libpq environment
  variables; the default needs no argument and no database to be running.
- A GitHub Actions workflow. The suite runs on eight Django/Python pairs from
  4.2 on 3.10 to 6.0 on 3.14, and once more against a postgres service
  container, because `COUNT(DISTINCT ...)` and a table name parsed back out of
  the SQL are not things sqlite alone can prove portable. `build` makes the
  real wheel and imports it from outside the source tree, where an editable
  install can no longer cover for a module missing from the distribution.
- `scripts/smoke.py`: the whole product asserted from outside. It writes a
  throwaway Django project with a deliberate N+1, records it, runs the command
  with `--fail-on-findings` and requires exit 1 with the relation named, then
  applies the fix the report printed and requires exit 0.
- A README that documents the command that exists rather than the one that was
  planned: the three-step quickstart, an annotated real report, both modes, the
  full option reference, the settings block, and what the tool cannot see.

### Changed

- `--django-models` is deprecated in favour of `--include-django`. It still
  works and prints a deprecation notice.
- Relation classification and timing moved out of the management command into
  `analysis/benchmark.py`, so there is one definition of "is this relation
  joinable?" rather than one per caller. A reverse one-to-one now has its own
  relation kind, since it is the one reverse relation Django can join and the
  one that hands back an instance rather than a manager.
- `--no-callsites` keeps the old per-relation sweep. With no source to join
  to there is no verdict to give, only what each strategy costs on this
  database — which is still the only thing that covers reverse and
  many-to-many relations.
- The app config gains `label`, `verbose_name` and `default_auto_field`; without
  the last one the app raised `models.W042` in projects that had not set
  `DEFAULT_AUTO_FIELD`.
