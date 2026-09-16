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

### Added

- `--sample-size` (default 500) bounds every timed queryset. Timings used to
  walk the whole table, three times per relation plus warmups.
- `--repeat` (default 5) runs each strategy that many times after a discarded
  warmup and reports the median, instead of a single noisy `perf_counter` run.
- A query-count column per strategy, captured with `CaptureQueriesContext`.
  Unlike a duration, it is deterministic and reproducible on another machine.
- `django_fk_optimize.utils`: the static-analysis package behind the call-site
  scanner — model vocabulary, per-module import resolution, queryset call-site
  detection and source discovery — is now part of the distribution.
- Packaging metadata (`pyproject.toml`): setuptools backend, Django >= 4.2,
  Python >= 3.10, MIT, classifiers, project URLs and a `dev` extra.
- A test suite that runs under plain `pytest` with no pytest-django, bootstrapping
  Django in `conftest.py` against in-memory sqlite.

### Changed

- The app config gains `label`, `verbose_name` and `default_auto_field`; without
  the last one the app raised `models.W042` in projects that had not set
  `DEFAULT_AUTO_FIELD`.
