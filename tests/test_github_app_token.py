"""Tests for scripts/github_app_token.py (no network; openssl CLI for signing)."""

import base64
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "github_app_token.py"
spec = importlib.util.spec_from_file_location("github_app_token", SCRIPT)
gat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gat)

needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="openssl CLI missing")


@pytest.fixture
def key(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl CLI missing")
    pem = tmp_path / "app.pem"
    subprocess.run(
        ["openssl", "genrsa", "-out", str(pem), "2048"], check=True, capture_output=True
    )
    return pem


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    path = tmp_path / "cache" / "token.json"
    monkeypatch.setattr(gat, "CACHE", str(path))
    return path


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_not_configured_exits_3(tmp_path, monkeypatch):
    for k in gat.KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DROVER_GH_APP_ENV", str(tmp_path / "missing.env"))
    assert gat.main([]) == gat.NOT_CONFIGURED


def test_config_env_wins_over_file(tmp_path):
    env = tmp_path / "app.env"
    env.write_text(
        "export DROVER_GH_APP_ID=111\nDROVER_GH_APP_KEY_FILE='~/k.pem'\n"
        "DROVER_GH_APP_INSTALLATION_ID=\n"
    )
    cfg = gat.load_config({"DROVER_GH_APP_ENV": str(env), "DROVER_GH_APP_ID": "222"})
    assert cfg["DROVER_GH_APP_ID"] == "222"
    assert cfg["DROVER_GH_APP_KEY_FILE"].endswith("/k.pem")
    assert "~" not in cfg["DROVER_GH_APP_KEY_FILE"]
    assert "DROVER_GH_APP_INSTALLATION_ID" not in cfg


@needs_openssl
def test_jwt_is_rs256_and_verifies(key, tmp_path):
    jwt = gat.app_jwt("123", str(key), now=1_000_000)
    h, p, s = jwt.split(".")
    assert json.loads(_unb64(h)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(_unb64(p)) == {"iat": 999_940, "exp": 1_000_540, "iss": "123"}
    pub = tmp_path / "pub.pem"
    subprocess.run(
        ["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(pub)],
        check=True,
        capture_output=True,
    )
    sig = tmp_path / "sig"
    sig.write_bytes(_unb64(s))
    ok = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(sig)],
        input=f"{h}.{p}".encode(),
        capture_output=True,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_missing_key_is_a_mint_error(tmp_path):
    with pytest.raises(gat.MintError):
        gat.app_jwt("1", str(tmp_path / "nope.pem"))


@needs_openssl
def test_mint_discovers_installation_and_caches(key, monkeypatch, cache):
    calls = []

    def fake_api(method, path, jwt):
        calls.append((method, path))
        if path.startswith("/repos/"):
            return {"id": 42}
        return {"token": "ghs_x", "expires_at": "2026-10-23T19:00:00Z"}

    monkeypatch.setattr(gat, "_api", fake_api)
    cfg = {"DROVER_GH_APP_ID": "7", "DROVER_GH_APP_KEY_FILE": str(key)}
    now = gat._expiry({"expires_at": "2026-10-23T18:00:00Z"})
    tok = gat.mint(cfg, "o/r", now=now)
    assert tok == {"token": "ghs_x", "expires_at": "2026-10-23T19:00:00Z", "installation_id": "42"}
    assert calls == [("GET", "/repos/o/r/installation"), ("POST", "/app/installations/42/access_tokens")]
    assert oct(cache.stat().st_mode & 0o777) == "0o600"
    # Fresh cache: no API call. Inside the 5-minute skew window: mint again.
    assert gat.mint(cfg, "o/r", now=now + 600)["token"] == "ghs_x"
    assert len(calls) == 2
    gat.mint(cfg, "o/r", now=now + 3600 - 200)
    assert len(calls) == 4


def test_mint_without_installation_or_repo_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(gat, "app_jwt", lambda *a: "jwt")
    with pytest.raises(gat.MintError):
        gat.mint({"DROVER_GH_APP_ID": "7", "DROVER_GH_APP_KEY_FILE": "k"}, None)
