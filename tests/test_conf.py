from pathlib import Path

from django.test.utils import override_settings

from django_fk_optimize import conf


def test_defaults_apply_when_the_setting_is_absent():
    config = conf.get_config()

    assert config.recording_path == Path(".fk_optimize/recording.jsonl")
    assert config.enabled is True
    assert config.sample_size == 500
    assert config.max_records == 100_000
    assert config.sample_rate == 1.0


def test_an_empty_block_is_the_same_as_no_block():
    with override_settings(FK_OPTIMIZE={}):
        assert conf.get_config() == conf.get_config()
        assert conf.get_config().enabled is True


def test_values_are_read_from_the_settings_block():
    with override_settings(
        FK_OPTIMIZE={
            "RECORDING_PATH": "/tmp/somewhere.jsonl",
            "ENABLED": False,
            "SAMPLE_SIZE": 10,
            "MAX_RECORDS": 3,
            "SAMPLE_RATE": 0.25,
        }
    ):
        config = conf.get_config()

    assert config.recording_path == Path("/tmp/somewhere.jsonl")
    assert config.enabled is False
    assert config.sample_size == 10
    assert config.max_records == 3
    assert config.sample_rate == 0.25


def test_settings_are_read_at_call_time_not_at_import_time():
    with override_settings(FK_OPTIMIZE={"ENABLED": False}):
        assert conf.get_config().enabled is False
    assert conf.get_config().enabled is True


def test_a_bad_value_falls_back_instead_of_raising():
    with override_settings(
        FK_OPTIMIZE={
            "SAMPLE_SIZE": "not a number",
            "MAX_RECORDS": -1,
            "SAMPLE_RATE": "nonsense",
            "RECORDING_PATH": "",
        }
    ):
        config = conf.get_config()

    assert config.sample_size == 500
    assert config.max_records == 100_000
    assert config.sample_rate == 1.0
    assert config.recording_path == Path(".fk_optimize/recording.jsonl")


def test_a_non_dict_setting_is_ignored():
    with override_settings(FK_OPTIMIZE=["nope"]):
        assert conf.raw() == {}
        assert conf.get_config().enabled is True


def test_sample_rate_is_clamped():
    with override_settings(FK_OPTIMIZE={"SAMPLE_RATE": 7}):
        assert conf.get_config().sample_rate == 1.0
    with override_settings(FK_OPTIMIZE={"SAMPLE_RATE": -3}):
        assert conf.get_config().sample_rate == 0.0
