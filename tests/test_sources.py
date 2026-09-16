from pathlib import Path

from django_fk_optimize.utils import discover, package_of, python_files


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
