import ast

from django_fk_optimize.utils import ModuleImports


def imports_of(source, package=None):
    return ModuleImports.from_ast(ast.parse(source), package)


def test_absolute_from_import():
    found = imports_of("from alarms.models import Alarm, AlarmType")

    assert found.names["Alarm"] == "alarms.models.Alarm"
    assert found.names["AlarmType"] == "alarms.models.AlarmType"


def test_aliased_from_import():
    found = imports_of("from alarms.models import Alarm as A")

    assert found.names["A"] == "alarms.models.Alarm"
    assert "Alarm" not in found.names


def test_relative_from_import_uses_the_package():
    found = imports_of("from .models import Alarm", package="alarms.views")

    assert found.names["Alarm"] == "alarms.views.models.Alarm"


def test_parent_relative_from_import():
    found = imports_of("from ..models import Alarm", package="alarms.views")

    assert found.names["Alarm"] == "alarms.models.Alarm"


def test_relative_import_without_a_package_resolves_to_nothing():
    # Resolving it would mean guessing which app it came from.
    found = imports_of("from .models import Alarm")

    assert found.names == {}


def test_star_import_binds_no_name():
    found = imports_of("from alarms.models import *")

    assert found.names == {}


def test_plain_and_aliased_module_imports():
    found = imports_of("import alarms.models\nimport alarms.models as m\n")

    assert found.modules["alarms.models"] == "alarms.models"
    assert found.modules["m"] == "alarms.models"


def test_qualname_resolves_through_imports():
    found = imports_of("import alarms.models as m\nfrom alarms.models import Alarm")

    assert found.qualname(ast.parse("Alarm", mode="eval").body) == "alarms.models.Alarm"
    assert (
        found.qualname(ast.parse("m.Alarm", mode="eval").body) == "alarms.models.Alarm"
    )
    # Unknown names pass through as written rather than being invented.
    assert found.qualname(ast.parse("other.Thing", mode="eval").body) == "other.Thing"


def test_qualname_refuses_non_dotted_chains():
    found = imports_of("")

    assert found.qualname(ast.parse("things[0].name", mode="eval").body) is None
    assert found.qualname(ast.parse("f().name", mode="eval").body) is None
