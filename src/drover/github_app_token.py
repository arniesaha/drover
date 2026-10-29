#!/usr/bin/env python3
"""Mint a GitHub App installation token for agent writes (drover-bot).

Agents (reconciler, Drover workers) write to GitHub as the App, so their
comments, labels, PRs and merges show as ``drover-bot[bot]`` and can be told
apart from operator directives. See docs/operations/github-app.md.

    github_app_token.py [--repo owner/name] [--json]

Prints the token on stdout. Exit codes:
  0  token printed
  3  no App configured: callers keep their current credentials (fallback)
  1  App configured but minting failed (details on stderr, never the key)

Configuration, first match wins per key: environment, then the env files in
``DROVER_GH_APP_ENV`` (os.pathsep-separated, default ``~/.drover/github-app.env``):
  DROVER_GH_APP_ID               App ID (or client ID)
  DROVER_GH_APP_KEY_FILE         path to the App private key (PEM, mode 600)
  DROVER_GH_APP_INSTALLATION_ID  optional; discovered from --repo otherwise

Tokens are cached in ``~/.cache/drover/github-app-token.json`` (mode 600)
until five minutes before they expire. Stdlib only; signing uses the
``openssl`` CLI so no Python crypto package is needed.
"""

import argparse
import base64
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("DROVER_GH_API", "https://api.github.com")
DEFAULT_ENV = "~/.drover/github-app.env"
CACHE = os.path.expanduser("~/.cache/drover/github-app-token.json")
KEYS = ("DROVER_GH_APP_ID", "DROVER_GH_APP_KEY_FILE", "DROVER_GH_APP_INSTALLATION_ID")
NOT_CONFIGURED, FAILED = 3, 1
SKEW = 300  # refresh this many seconds before expiry


class MintError(Exception):
    pass


def load_config(environ=os.environ):
    """App settings from the environment, then the env files. Empty dict = not configured."""
    cfg = {k: environ[k] for k in KEYS if environ.get(k)}
    for path in environ.get("DROVER_GH_APP_ENV", DEFAULT_ENV).split(os.pathsep):
        path = os.path.expanduser(path.strip())
        if not path or not os.path.isfile(path):
            continue
        for line in open(path):
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            k, sep, v = line.partition("=")
            if sep and k.strip() in KEYS and k.strip() not in cfg:
                v = v.strip().strip("'\"")
                if v:
                    cfg[k.strip()] = v
    if not cfg:
        return {}
    if not (cfg.get("DROVER_GH_APP_ID") and cfg.get("DROVER_GH_APP_KEY_FILE")):
        raise MintError("incomplete GitHub App configuration")
    cfg["DROVER_GH_APP_KEY_FILE"] = os.path.expanduser(cfg["DROVER_GH_APP_KEY_FILE"])
    return cfg


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def app_jwt(app_id, key_file, now=None):
    """RS256 JWT for the App, valid 9 minutes (GitHub allows 10), backdated 60s for clock skew."""
    now = int(now if now is not None else time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    if not os.path.isfile(key_file):
        raise MintError(f"private key not found: {key_file}")
    p = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", key_file],
        input=signing_input,
        capture_output=True,
        timeout=30,
    )
    if p.returncode != 0 or not p.stdout:
        raise MintError("openssl signing failed: " + p.stderr.decode()[-200:])
    return f"{header}.{payload}.{_b64url(p.stdout)}"


def _api(method, path, jwt):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=b"" if method == "POST" else None,
        headers={
            "Authorization": "Bearer " + jwt,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "drover-github-app-token",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise MintError(f"HTTP {e.code} {method} {path}: {e.read().decode()[:200]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise MintError(f"{method} {path}: {getattr(e, 'reason', e)}")


def _read_cache():
    try:
        with open(CACHE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write_cache(data):
    os.makedirs(os.path.dirname(CACHE), mode=0o700, exist_ok=True)
    tmp = CACHE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, CACHE)


def _expiry(token):
    return dt.datetime.fromisoformat(
        token["expires_at"].replace("Z", "+00:00")
    ).timestamp()


def mint(cfg, repo=None, now=None):
    """Installation token dict {token, expires_at, installation_id}, from cache when still fresh."""
    now = now if now is not None else time.time()
    inst = cfg.get("DROVER_GH_APP_INSTALLATION_ID")
    cache = _read_cache()
    key = f"{cfg['DROVER_GH_APP_ID']}:{inst or repo or ''}"
    hit = cache.get(key)
    if hit and _expiry(hit) - SKEW > now:
        return hit
    jwt = app_jwt(cfg["DROVER_GH_APP_ID"], cfg["DROVER_GH_APP_KEY_FILE"], now)
    if not inst:
        if not repo:
            raise MintError(
                "set DROVER_GH_APP_INSTALLATION_ID or pass --repo owner/name"
            )
        inst = str(_api("GET", f"/repos/{repo}/installation", jwt)["id"])
    r = _api("POST", f"/app/installations/{inst}/access_tokens", jwt)
    tok = {"token": r["token"], "expires_at": r["expires_at"], "installation_id": inst}
    cache = {k: v for k, v in cache.items() if _expiry(v) > now}
    cache[key] = tok
    _write_cache(cache)
    return tok


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--repo", default=os.environ.get("DROVER_GH_REPO", "arniesaha/drover")
    )
    p.add_argument(
        "--json", action="store_true", help="print {token, expires_at, installation_id}"
    )
    a = p.parse_args(argv)
    try:
        cfg = load_config()
        if not cfg:
            return NOT_CONFIGURED
        tok = mint(cfg, a.repo)
    except (MintError, KeyError, ValueError, subprocess.TimeoutExpired) as e:
        print(f"github_app_token: {e}", file=sys.stderr)
        return FAILED
    print(json.dumps(tok) if a.json else tok["token"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
