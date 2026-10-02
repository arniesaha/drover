"""Legacy Pond config remains loadable without retaining its integration."""

import pytest

import drover.config as config


@pytest.mark.parametrize(
    "section",
    [
        '[archive]\nenabled = true\nbase_url = "http://127.0.0.1:9797"',
        '[archive]\nenabled = "invalid old value"\ntimeout_seconds = -1',
        'archive = "obsolete"',
    ],
)
def test_legacy_archive_config_is_ignored_once(tmp_path, monkeypatch, caplog, section):
    monkeypatch.setattr(config, "_pond_deprecation_warned", False)
    path = tmp_path / "config.toml"
    path.write_text(section)
    first = config.load_config(path)
    second = config.load_config(path)
    assert first == second
    assert not hasattr(first, "archive")
    warnings = [r for r in caplog.records if "Pond is deprecated" in r.message]
    assert len(warnings) == 1
    assert "ignoring legacy [archive]" in warnings[0].message
    assert "invalid old value" not in warnings[0].message


def test_default_config_has_no_archive_section(caplog):
    assert not hasattr(config.default_config(), "archive")
    assert not any("Pond" in r.message for r in caplog.records)
