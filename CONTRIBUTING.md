# Contributing

## Running the suite

```bash
python -m pytest -q
```

From the repository root, and that is the whole setup. There is no
pytest-django and no install step: `tests/conftest.py` calls
`settings.configure()` and `django.setup()` itself, and pytest puts the
repository root on `sys.path` because `tests/` is a package. All you need
installed is Django and pytest.

The database is in-memory sqlite. Each test gets a transaction that is rolled
back at the end (`db`), and `library` on top of that is a small fixed dataset.
Assert on query counts, never on wall-clock time — `CaptureQueriesContext` is
deterministic and `perf_counter` is not.

## Running it against postgres

Everything the analysis layer emits is SQL, and some of it is
backend-shaped — `COUNT(DISTINCT ...)`, and a table name parsed back out of the
query Django wrote. Sqlite cannot prove any of that portable, so CI runs the
suite a second time against a real server. To do the same locally:

```bash
podman run -d --rm --name fkpg \
    -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=fk_optimize \
    -p 55432:5432 postgres:16

pip install "psycopg[binary]"

FK_OPTIMIZE_TEST_DB=postgres PGPORT=55432 PGPASSWORD=postgres \
    python -m pytest -q
```

`FK_OPTIMIZE_TEST_DB` takes `sqlite` (the default) or `postgres`; connection
details come from the usual `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE`
variables. To add a third backend, extend `BACKENDS` and `database()` in
`tests/conftest.py` and add a job to `.github/workflows/ci.yml` modelled on the
postgres one. Two things that had to be true for postgres and will be true for
the next backend as well: the test app is unmigrated, so `contenttypes` is
unmigrated with it or its generic FK points at a table that does not exist yet;
and connections opened on executor threads have to be released before the test
database can be dropped.

## The end-to-end check

```bash
python scripts/smoke.py
```

Writes a throwaway Django project with a deliberate N+1, records it, runs
`manage.py fk_optimize --fail-on-findings` and requires exit 1 with the relation
named, then applies the printed fix and requires exit 0. `--keep` leaves the
project on disk to poke at. Run it against a wheel in a clean virtualenv and it
also catches anything missing from the distribution:

```bash
python -m build
python -m venv /tmp/wheelenv && /tmp/wheelenv/bin/pip install dist/*.whl
/tmp/wheelenv/bin/python scripts/smoke.py
```

## Lint

```bash
ruff check --fix .
ruff format .
```

Configured in `ruff.toml`, which pre-commit reads too — one source, not two. CI
pins the ruff version so a new release cannot turn a green branch red on its
own; if you upgrade ruff locally, upgrade the pin in
`.github/workflows/ci.yml` in the same commit.

## The version matrix

`.github/workflows/ci.yml` lists Django/Python pairs explicitly rather than
crossing two lists and excluding the impossible cells, because most cells of
that cross product do not exist. Check a new pair against Django's own
[installation FAQ](https://docs.djangoproject.com/en/stable/faq/install/)
before adding it. The floors — Django 4.2 and Python 3.10 — are what
`pyproject.toml` claims, so anything that breaks the oldest cell is a bug in
the code or a change to those floors, not a cell to drop.

## The relation matrix

`tests/test_matrix.py` does not list its cases. It enumerates every relation of
every model in `tests/testapp` and checks the same invariants against all of
them, so adding a model to the test app extends the matrix for free — and a
shape nobody anticipated is still covered.

Add a row for any new model in the `menagerie` fixture. The generated tests
skip a model they cannot instantiate, and a skipped case is not coverage. Keep
new rows out of `library`: several tests pin exact query counts against it, and
a `Textbook` is a `Book`, so adding one there moves numbers all over the suite.

One assertion there is deliberately tight rather than safe:
`select_related()` must raise `FieldError` *exactly* when the plan refuses it.
Asserting only that permitted joins work would let the classification quietly
refuse legal ones, which is how the reverse one-to-one ended up recommended as
a two-query prefetch.

## The corpus

`scripts/corpus.py` clones eight well-known Django projects and points the
scanner at them. No database, no settings, no install of the project: the
vocabulary is read out of source by `utils/static_vocabulary.py`, so a whole
run is a shallow clone and a few seconds of parsing each.

```bash
python scripts/corpus.py                      # all of them
python scripts/corpus.py --only wagtail       # one
python scripts/corpus.py --write-baseline     # record new floors
```

It fails on a crash, or when `models`/`sites` fall below the floors in
`scripts/corpus-baseline.json`. Those are floors and not exact counts because
these projects keep moving; a pinned expectation would go red for their reasons
rather than ours. Rewrite the baseline when a real improvement raises the
numbers, and say so in the commit.

It never fails on `sites_unresolved`. That is the coverage metric this project
does not grade itself on: a queryset the scanner could not follow is a case we
do not handle yet, and the number rising on a newly added project is a backlog
item. Both of the scanner's biggest blind spots so far — models built on
project-local abstract bases, and models fetched through `get_model()` — were
found this way and not by the test suite.

Adding a project is one line in `PROJECTS`. Prefer variety of modelling habits
over fame.

## Commits

Conventional Commits, imperative mood, lowercase subject, one logical change
per commit, each one leaving the repository working.
