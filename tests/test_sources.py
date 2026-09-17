from pathlib import Path
from types import SimpleNamespace

from django_fk_optimize.utils import discover, package_of, python_files, sources
from django_fk_optimize.utils.sources import app_in_scope, app_roots, is_third_party


def test_package_of_nested_file():
    package = package_of(
        Path("/srv/app/alarms/views/admin.py"), Path("/srv/app"), "proj"
    )

    assert package == "proj.alarms.views"


def test_package_of_file_at_the_root():
    package = package_of(Path("/srv/app/models.py"), Path("/srv/app"), "alarms")

    assert package == "alarms"


def test_python_files_skips_migrations_and_caches(tmp_path):
    (tmp_path / "views.py").write_text("")
    (tmp_path / "notes.txt").write_text("")
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "0001_initial.py").write_text("")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "views.py").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "helpers.py").write_text("")

    found = {path.relative_to(tmp_path).as_posix() for path in python_files(tmp_path)}

    assert found == {"views.py", "sub/helpers.py"}


def test_discover_finds_the_test_app_with_its_package():
    found = dict(discover())

    models = [path for path in found if path.as_posix().endswith("testapp/models.py")]
    assert models, "the test app's models.py should be discoverable"
    assert found[models[0]] == "tests.testapp"
    # django.* apps stay out unless asked for.
    assert not any("/django/contrib/" in path.as_posix() for path in found)


# -- whose code is it --------------------------------------------------


def config(name, path):
    """The two attributes `app_in_scope` reads off an AppConfig."""
    return SimpleNamespace(name=name, path=str(path))


def test_an_installed_app_is_third_party():
    assert is_third_party("/srv/venv/lib/python3.12/site-packages/rest_framework")
    assert is_third_party("/usr/lib/python3/dist-packages/allauth")


def test_an_editable_install_is_still_your_code():
    """`pip install -e` leaves the source where the author keeps it.

    The link that points at it lives in site-packages; the code does not, and
    the code is what the person running the command can edit.
    """
    assert not is_third_party("/home/dev/code/wagtail-fork/wagtail")


def test_a_directory_that_merely_reads_like_one_is_not():
    """The test is on path components, not on a substring of the path."""
    assert not is_third_party("/home/dev/site-packages-talk/demo")


def test_the_interpreters_install_directory_counts(monkeypatch, tmp_path):
    """Not every install root is spelled site-packages; ask sysconfig too."""
    purelib = tmp_path / "python" / "lib"
    (purelib / "vendored").mkdir(parents=True)
    monkeypatch.setattr(
        sources.sysconfig, "get_paths", lambda: {"purelib": str(purelib)}
    )
    sources.install_roots.cache_clear()
    try:
        assert is_third_party(purelib / "vendored")
        assert not is_third_party(tmp_path / "project" / "shop")
    finally:
        sources.install_roots.cache_clear()


def test_third_party_apps_are_out_of_scope_until_asked_for():
    vendored = config("rest_framework", "/srv/venv/lib/python3.12/site-packages/drf")

    assert not app_in_scope(vendored)
    assert app_in_scope(vendored, include_third_party=True)
    # --include-django is about django.*, and says nothing about anyone else's
    # package.
    assert not app_in_scope(vendored, include_django=True)


def test_include_django_still_means_django_only():
    """Django's own apps ship from site-packages like everything else.

    Which flag reaches them therefore cannot be decided by where they were
    installed: --include-django is the one that says django.*, and asking for
    third-party apps does not drag them along behind it.
    """
    contrib = config(
        "django.contrib.auth", "/srv/venv/lib/python3.12/site-packages/django/contrib"
    )

    assert not app_in_scope(contrib)
    assert not app_in_scope(contrib, include_third_party=True)
    assert app_in_scope(contrib, include_django=True)
    assert app_in_scope(contrib, include_django=True, include_third_party=True)


def test_the_projects_own_apps_are_always_in_scope():
    own = config("shop", "/srv/project/shop")

    assert app_in_scope(own)
    assert app_in_scope(own, include_django=True, include_third_party=True)


def test_app_roots_leaves_an_installed_app_out(monkeypatch, tmp_path):
    from django.apps.registry import apps

    installed = config("vendorapp", tmp_path / "site-packages" / "vendorapp")
    mine = config("shop", tmp_path / "shop")
    monkeypatch.setattr(apps, "get_app_configs", lambda: [installed, mine])

    assert [name for name, _ in app_roots()] == ["shop"]
    assert [name for name, _ in app_roots(include_third_party=True)] == [
        "vendorapp",
        "shop",
    ]
