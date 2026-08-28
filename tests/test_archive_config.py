"""Archive configuration keeps Pond disabled and bounded by default."""

from dataclasses import FrozenInstanceError

import pytest

from drover.config import ArchiveConfig, default_config, load_config


def _load_archive_config(tmp_path, archive_toml: str):
    path = tmp_path / "config.toml"
    path.write_text("[archive]\n" + archive_toml)
    return load_config(path).archive


def test_archive_defaults_are_disabled_and_bounded() -> None:
    archive = default_config().archive
    assert archive == ArchiveConfig(
        enabled=False,
        base_url="",
        timeout_seconds=3.0,
        search_limit=5,
        context_before=2,
        context_after=2,
        max_context_chars=24_000,
        max_response_bytes=1_048_576,
    )


@pytest.mark.parametrize(
    ("base_url", "want"),
    [
        ("http://localhost", "http://localhost"),
        ("http://localhost/", "http://localhost"),
        ("http://127.0.0.1", "http://127.0.0.1"),
        ("http://127.0.0.1/", "http://127.0.0.1"),
        ("http://[::1]", "http://[::1]"),
        ("http://[::1]/", "http://[::1]"),
    ],
)
def test_archive_load_accepts_all_loopback_host_spellings_and_normalizes_root_slash(
    tmp_path, base_url, want
):
    archive = _load_archive_config(
        tmp_path, f'enabled = true\nbase_url = "{base_url}"\n'
    )

    assert archive.enabled is True
    assert archive.base_url == want


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_seconds", "0.1"),
        ("timeout_seconds", "10.0"),
        ("search_limit", "1"),
        ("search_limit", "20"),
        ("context_before", "0"),
        ("context_before", "10"),
        ("context_after", "0"),
        ("context_after", "10"),
        ("max_context_chars", "1000"),
        ("max_context_chars", "100000"),
        ("max_response_bytes", "1024"),
        ("max_response_bytes", "2097152"),
    ],
)
def test_archive_load_accepts_every_numeric_boundary(tmp_path, field, value):
    archive = _load_archive_config(tmp_path, f"{field} = {value}\n")

    expected = float(value) if field == "timeout_seconds" else int(value)
    assert getattr(archive, field) == expected


@pytest.mark.parametrize("value", ["'false'", "'true'"])
def test_archive_rejects_string_booleans(tmp_path, value):
    with pytest.raises(ValueError, match="archive.enabled"):
        _load_archive_config(tmp_path, f"enabled = {value}\n")


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "https://localhost",
        "http://pond.local",
        "http://user@localhost",
        "http://localhost?query=1",
        "http://localhost#fragment",
        "http://localhost/v1",
    ],
)
def test_archive_rejects_unsafe_enabled_urls(tmp_path, base_url):
    with pytest.raises(ValueError, match="archive.base_url"):
        _load_archive_config(tmp_path, f'enabled = true\nbase_url = "{base_url}"\n')


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_archive_rejects_non_finite_timeout(tmp_path, value):
    with pytest.raises(ValueError, match="archive.timeout_seconds"):
        _load_archive_config(tmp_path, f"timeout_seconds = {value}\n")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_seconds", "0.09"),
        ("timeout_seconds", "10.1"),
        ("search_limit", "0"),
        ("search_limit", "21"),
        ("context_before", "-1"),
        ("context_before", "11"),
        ("context_after", "-1"),
        ("context_after", "11"),
        ("max_context_chars", "999"),
        ("max_context_chars", "100001"),
        ("max_response_bytes", "1023"),
        ("max_response_bytes", "2097153"),
    ],
)
def test_archive_rejects_numeric_values_outside_boundaries(tmp_path, field, value):
    with pytest.raises(ValueError, match=f"archive.{field}"):
        _load_archive_config(tmp_path, f"{field} = {value}\n")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_seconds", True),
        ("search_limit", True),
        ("context_before", 2.0),
        ("context_after", 2.0),
        ("max_context_chars", 24_000.0),
        ("max_response_bytes", 1_048_576.0),
    ],
)
def test_archive_config_rejects_wrong_numeric_types_before_comparing(field, value):
    kwargs = {
        "enabled": False,
        "base_url": "",
        "timeout_seconds": 3.0,
        "search_limit": 5,
        "context_before": 2,
        "context_after": 2,
        "max_context_chars": 24_000,
        "max_response_bytes": 1_048_576,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=f"archive.{field}"):
        ArchiveConfig(**kwargs)


def test_archive_config_is_immutable():
    archive = default_config().archive

    with pytest.raises(FrozenInstanceError):
        archive.enabled = True
