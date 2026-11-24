"""ASC processing and distribution use deterministic fakes, never the network."""

import json
import io
import urllib.error
from pathlib import Path

import pytest
from ios.testflight_api import (
    Client,
    InventoryFailure,
    Rejected,
    distribute,
    inventory,
    prerelease_access_probe,
    token,
)

INFO = {
    "CFBundleIdentifier": "com.arnab.drover",
    "CFBundleShortVersionString": "0.1.0",
    "CFBundleVersion": "3",
    "ITSAppUsesNonExemptEncryption": False,
}


class Clock:
    value = 0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Fake:
    def __init__(self):
        self.calls = []
        self.polls = 0
        self.encryption = None
        self.assigned = False
        self.state = "VALID"
        self.upload_state = "COMPLETE"
        self.group = True
        self.beta_state = "READY_FOR_BETA_TESTING"

    def collection(self, path, *, params=None, deadline):
        self.calls.append(("GET", path, params))
        if path == "/v1/apps":
            assert params == {"filter[bundleId]": INFO["CFBundleIdentifier"]}
            return [{"id": "app"}]
        if path.endswith("buildUploads"):
            assert params == {
                "filter[cfBundleShortVersionString]": "0.1.0",
                "filter[cfBundleVersion]": "3",
                "filter[platform]": "IOS",
            }
            return [{"attributes": {"state": {"state": self.upload_state}}}]
        if path == "/v1/builds":
            assert params["filter[app]"] == "app"
            assert params["filter[version]"] == "3"
            assert params["filter[preReleaseVersion.version]"] == "0.1.0"
            assert params["filter[preReleaseVersion.platform]"] == "IOS"
            self.polls += 1
            if self.polls == 1:
                return []
            return [
                {
                    "id": "build",
                    "attributes": {
                        "processingState": self.state,
                        "usesNonExemptEncryption": self.encryption,
                    },
                }
            ]
        if path == "/v1/betaGroups":
            if "filter[builds]" in params:
                return [{"id": "group"}] if self.assigned else []
            assert params["filter[name]"] == "Drover Internal"
            return (
                [
                    {
                        "id": "group",
                        "attributes": {
                            "name": "Drover Internal",
                            "isInternalGroup": True,
                        },
                    }
                ]
                if self.group
                else []
            )
        raise AssertionError(path)

    def request(self, method, path, *, body=None, deadline):
        self.calls.append((method, path, body))
        if method == "GET":
            assert path == "/v1/builds/build/buildBetaDetail"
            return {"data": {"attributes": {"internalBuildState": self.beta_state}}}
        if method == "PATCH":
            assert body == {
                "data": {
                    "type": "builds",
                    "id": "build",
                    "attributes": {"usesNonExemptEncryption": False},
                }
            }
            self.encryption = False
        elif method == "POST":
            assert path == "/v1/betaGroups/group/relationships/builds"
            assert body == {"data": [{"type": "builds", "id": "build"}]}
            self.assigned = True


def run(fake, info=INFO, **kwargs):
    clock = Clock()
    return distribute(
        fake,
        info,
        "0.1.0",
        "3",
        clock=clock.now,
        sleep=clock.sleep,
        interval=1,
        timeout=10,
        **kwargs,
    )


def test_waits_for_exact_build_sets_compliance_and_confirms_assignment():
    fake = Fake()
    result = run(fake)
    assert result["internal_group_assigned"] is True
    assert fake.polls == 4
    assert [c[0] for c in fake.calls if c[0] != "GET"] == ["PATCH", "POST"]


def test_existing_assignment_is_idempotent():
    fake = Fake()
    fake.encryption, fake.assigned = False, True
    run(fake)
    assert not any(c[0] != "GET" for c in fake.calls)


@pytest.mark.parametrize("state", ["INVALID", "FAILED"])
def test_invalid_build_fails_without_assignment(state):
    fake = Fake()
    fake.state = state
    with pytest.raises(Rejected, match="FAILED/INVALID"):
        run(fake)
    assert not fake.assigned


def test_failed_upload_is_detected_before_build_visible():
    fake = Fake()
    fake.upload_state = "FAILED"
    with pytest.raises(Rejected, match="upload FAILED/INVALID"):
        run(fake)
    assert fake.polls == 0


def test_missing_internal_group_fails_clearly():
    fake = Fake()
    fake.group = False
    with pytest.raises(Rejected, match="internal beta group does not exist"):
        run(fake)


@pytest.mark.parametrize("declaration", [None, True, "NO", 0])
def test_never_guesses_export_compliance(declaration):
    fake = Fake()
    with pytest.raises(Rejected, match="missing export compliance"):
        run(fake, INFO | {"ITSAppUsesNonExemptEncryption": declaration})
    assert not fake.assigned


def test_processing_timeout_does_not_assign():
    fake = Fake()
    fake.state = "PROCESSING"
    with pytest.raises(
        Rejected, match="timed out.*processing pending.*do not re-upload"
    ):
        run(fake)
    assert not fake.assigned


def test_version_mismatch_fails_before_requests():
    fake = Fake()
    with pytest.raises(Rejected, match="does not match"):
        run(fake, INFO | {"CFBundleVersion": "4"})
    assert not fake.calls


@pytest.fixture
def key(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "key.p8"
    path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path, private


def test_jwt_es256_signature_and_expiry(key):
    import base64

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    value = token(key[1], "EXAMPLE123", "issuer", 1000)
    header, claims, signature = value.split(".")
    decode = lambda v: base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))
    assert json.loads(decode(claims)) == {
        "iss": "issuer",
        "iat": 1000,
        "exp": 1600,
        "aud": "appstoreconnect-v1",
    }
    sig = decode(signature)
    key[1].public_key().verify(
        encode_dss_signature(
            int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
        ),
        f"{header}.{claims}".encode(),
        ec.ECDSA(hashes.SHA256()),
    )


def test_client_pagination_and_timeout_with_fake_opener(key):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return json.dumps(pages.pop(0)).encode()

    class Opener:
        def open(self, request, timeout):
            assert request.full_url.startswith(
                "https://api.appstoreconnect.apple.com/v1/"
            )
            assert timeout == 5
            return Response()

    pages = [
        {
            "data": [{"id": "one"}],
            "links": {
                "next": "https://api.appstoreconnect.apple.com/v1/apps?cursor=two"
            },
        },
        {"data": [{"id": "two"}]},
    ]
    client = Client(key[0], "KEY", "issuer", opener=Opener(), clock=lambda: 0)
    assert client.collection("/v1/apps", deadline=5) == [{"id": "one"}, {"id": "two"}]
    with pytest.raises(Rejected, match="unsafe pagination"):
        client.request("GET", "https://evil.example/v1/apps", deadline=5)


def test_client_collection_rejects_a_next_page_beyond_the_bound(key):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return json.dumps(
                {
                    "data": [{"id": "one"}],
                    "links": {
                        "next": "https://api.appstoreconnect.apple.com/v1/apps?cursor=two"
                    },
                }
            ).encode()

    class Opener:
        def open(self, request, timeout):
            return Response()

    client = Client(key[0], "KEY", "issuer", opener=Opener(), clock=lambda: 0)
    with pytest.raises(Rejected, match="page limit"):
        client.collection("/v1/apps", deadline=5, max_pages=1)


class InventoryClient:
    def __init__(self):
        self.calls = []

    def collection(self, path, *, params=None, deadline, max_pages=None):
        self.calls.append((path, params, deadline, max_pages))
        if path == "/v1/apps":
            return [{"id": "private-app-id"}]
        if path == "/v1/preReleaseVersions":
            return [
                {"id": "version-2", "attributes": {"version": "2.0"}},
                {"id": "version-1", "attributes": {"version": "1.9"}},
            ]
        if path == "/v1/preReleaseVersions/version-2/builds":
            return [
                {
                    "id": "private-build-id",
                    "attributes": {"version": "42", "processingState": "VALID"},
                }
            ]
        if path == "/v1/preReleaseVersions/version-1/builds":
            return [
                {
                    "id": "another-private-build-id",
                    "attributes": {"version": "41", "processingState": "PROCESSING"},
                }
            ]
        if path == "/v1/betaGroups":
            return [
                {
                    "id": "private-group-id",
                    "attributes": {"name": "Drover Internal", "isInternalGroup": True},
                }
            ]
        raise AssertionError(path)


def test_inventory_is_get_only_paginated_bounded_and_allowlisted():
    client = InventoryClient()

    result = inventory(
        client,
        "com.arnab.drover",
        timeout=60,
        max_pages=2,
        max_versions=2,
        max_builds_per_version=2,
        clock=lambda: 0,
    )

    assert result == {
        "versions": [{"version": "1.9"}, {"version": "2.0"}],
        "builds": [
            {"version": "1.9", "build": "41", "processing_state": "PROCESSING"},
            {"version": "2.0", "build": "42", "processing_state": "VALID"},
        ],
        "internal_groups": [{"name": "Drover Internal"}],
    }
    assert all(call[3] == 2 for call in client.calls)
    assert client.calls[0][1] == {"filter[bundleId]": "com.arnab.drover", "limit": 2}
    assert "private-" not in json.dumps(result)
    assert {call[0] for call in client.calls} == {
        "/v1/apps",
        "/v1/preReleaseVersions",
        "/v1/preReleaseVersions/version-2/builds",
        "/v1/preReleaseVersions/version-1/builds",
        "/v1/betaGroups",
    }


@pytest.mark.parametrize(
    "response",
    [[], [{"id": "app"}, {"id": "other"}], [{"attributes": {}}]],
)
def test_inventory_rejects_missing_or_ambiguous_app_metadata(response):
    class BadClient(InventoryClient):
        def collection(self, path, **kwargs):
            if path == "/v1/apps":
                return response
            return super().collection(path, **kwargs)

    with pytest.raises(Rejected, match="app record"):
        inventory(BadClient(), "com.arnab.drover", clock=lambda: 0)


def test_inventory_fails_closed_on_collection_failure_without_exposing_error():
    from ios.testflight_api import Retryable

    class FailingClient(InventoryClient):
        def collection(self, path, **kwargs):
            raise Retryable("private API body")

    with pytest.raises(Rejected, match="temporarily unavailable") as error:
        inventory(FailingClient(), "com.arnab.drover", clock=lambda: 0)
    assert "private API body" not in str(error.value)


def test_inventory_preserves_only_safe_stage_category_and_http_status():
    class FailingClient(InventoryClient):
        def collection(self, path, **kwargs):
            assert path == "/v1/apps"
            raise Rejected(
                "private bearer token https://private.example/response",
                diagnostic_category="http",
                http_status=403,
            )

    with pytest.raises(InventoryFailure) as error:
        inventory(FailingClient(), "com.arnab.drover", clock=lambda: 0)

    failure = error.value
    assert failure.inventory_stage == "apps"
    assert failure.diagnostic_category == "http"
    assert failure.http_status == 403
    assert "private" not in str(failure)


def test_prerelease_probe_compares_only_the_two_same_app_contracts():
    class ProbeClient:
        def __init__(self):
            self.calls = []

        def request(self, method, path, *, params=None, deadline, **kwargs):
            self.calls.append((method, path, params, deadline, kwargs))
            if path == "/v1/apps":
                return {"data": [{"id": "private-app-id"}]}
            if path == "/v1/preReleaseVersions":
                raise Rejected(
                    "private response body",
                    diagnostic_category="http",
                    http_status=403,
                    apple_error_category="forbidden",
                )
            if path == "/v1/apps/private-app-id/preReleaseVersions":
                return {"data": []}
            raise AssertionError(path)

    client = ProbeClient()
    result = prerelease_access_probe(
        client,
        "com.arnab.drover",
        configured_key_id="CONFIGURED123",
        expected_key_id="CONFIGURED123",
        clock=lambda: 0,
    )

    assert result == {
        "configured_key_identity": "matches",
        "probe": "pre-release-access",
        "contracts": [
            {
                "contract": "global-filtered",
                "error_category": "http",
                "http_status": 403,
                "apple_error_category": "forbidden",
                "result": "rejected",
            },
            {"contract": "app-scoped", "result": "ok"},
        ],
    }
    assert {call[1] for call in client.calls} == {
        "/v1/apps",
        "/v1/preReleaseVersions",
        "/v1/apps/private-app-id/preReleaseVersions",
    }
    assert all(call[0] == "GET" for call in client.calls)
    assert all(call[2].get("limit") == 1 for call in client.calls)
    assert client.calls[-1][2] == {"limit": 1}
    assert "private" not in json.dumps(result)


def test_prerelease_probe_classifies_an_allowlisted_apple_error_without_body(key):
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                403,
                "PRIVATE REASON",
                {},
                io.BytesIO(
                    b'{"errors":[{"code":"FORBIDDEN.REQUIRED_AGREEMENTS",'
                    b'"detail":"private bearer token"}]}'
                ),
            )

    client = Client(key[0], "KEY", "issuer", opener=Opener(), clock=lambda: 0)
    with pytest.raises(Rejected) as error:
        client.request(
            "GET", "/v1/preReleaseVersions", deadline=5, diagnostic_error=True
        )

    assert error.value.diagnostic_category == "http"
    assert error.value.http_status == 403
    assert error.value.apple_error_category == "forbidden"
    assert error.value.apple_error_reason == "agreement-required"
    assert "PRIVATE" not in str(error.value)
    assert "token" not in str(error.value)


def test_prerelease_probe_uses_three_bounded_gets_and_ignores_next_page(key):
    class ProbeClock:
        value = 0

        def now(self):
            return self.value

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            assert limit == 64 * 1024 + 1
            return json.dumps(self.payload).encode()

    class Opener:
        def __init__(self, clock):
            self.clock = clock
            self.urls = []
            self.timeouts = []
            self.responses = iter(
                [
                    {"data": [{"id": "private-app-id"}]},
                    {
                        "data": [{"id": "private-version-id"}],
                        "links": {
                            "next": "https://api.appstoreconnect.apple.com/v1/preReleaseVersions?cursor=private"
                        },
                    },
                    {"data": []},
                ]
            )

        def open(self, request, timeout):
            self.urls.append(request.full_url)
            self.timeouts.append(timeout)
            self.clock.value = 44
            return Response(next(self.responses))

    clock = ProbeClock()
    opener = Opener(clock)
    client = Client(key[0], "KEY", "issuer", opener=opener, clock=clock.now)

    result = prerelease_access_probe(
        client,
        "com.arnab.drover",
        configured_key_id="CONFIGURED123",
        clock=clock.now,
    )

    assert result["contracts"] == [
        {"contract": "global-filtered", "result": "ok"},
        {"contract": "app-scoped", "result": "ok"},
    ]
    assert len(opener.urls) == 3
    assert opener.timeouts == [30, 1, 1]
    assert all("cursor=private" not in url for url in opener.urls)
    assert "filter%5Bplatform%5D=IOS" in opener.urls[1]
    assert "filter%5Bplatform%5D" not in opener.urls[2]


def test_apple_error_reason_requires_an_exact_allowlisted_code(key):
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                403,
                "PRIVATE REASON",
                {},
                io.BytesIO(
                    b'{"errors":[{"code":"FORBIDDEN.REQUIRED_AGREEMENTS.EVIL",'
                    b'"detail":"private bearer token"}]}'
                ),
            )

    client = Client(key[0], "KEY", "issuer", opener=Opener(), clock=lambda: 0)
    with pytest.raises(Rejected) as error:
        client.request(
            "GET", "/v1/preReleaseVersions", deadline=5, diagnostic_error=True
        )

    assert error.value.apple_error_category == "forbidden"
    assert error.value.apple_error_reason == "unknown"
    assert "EVIL" not in str(error.value)


def test_inventory_command_writes_only_allowlisted_record(tmp_path, monkeypatch):
    from ios import testflight_inventory as command

    key = tmp_path / "AuthKey_EXAMPLE123.p8"
    key.write_text("synthetic private key")
    key.chmod(0o600)
    output = tmp_path / "inventory.json"
    monkeypatch.setattr(command, "Client", lambda *args: object())
    monkeypatch.setattr(
        command,
        "inventory",
        lambda *args, **kwargs: {
            "versions": [{"version": "2.0"}],
            "builds": [{"version": "2.0", "build": "42", "processing_state": "VALID"}],
            "internal_groups": [{"name": "Drover Internal"}],
        },
    )

    command.main(
        [
            "--bundle-id",
            "com.arnab.drover",
            "--api-key-id",
            "EXAMPLE123",
            "--api-issuer",
            "11111111-2222-3333-4444-555555555555",
            "--private-key",
            str(key),
            "--output",
            str(output),
        ]
    )

    assert json.loads(output.read_text()) == {
        "versions": [{"version": "2.0"}],
        "builds": [{"version": "2.0", "build": "42", "processing_state": "VALID"}],
        "internal_groups": [{"name": "Drover Internal"}],
    }


def test_inventory_command_rejects_an_unallowlisted_artifact_field(
    tmp_path, monkeypatch
):
    from ios import testflight_inventory as command

    key = tmp_path / "AuthKey_EXAMPLE123.p8"
    key.write_text("synthetic private key")
    key.chmod(0o600)
    output = tmp_path / "inventory.json"
    monkeypatch.setattr(command, "Client", lambda *args: object())
    monkeypatch.setattr(
        command,
        "inventory",
        lambda *args, **kwargs: {
            "versions": [],
            "builds": [],
            "internal_groups": [],
            "private_api_detail": "must not be written",
        },
    )

    with pytest.raises(SystemExit, match="private diagnostics discarded"):
        command.main(
            [
                "--bundle-id",
                "com.arnab.drover",
                "--api-key-id",
                "EXAMPLE123",
                "--api-issuer",
                "11111111-2222-3333-4444-555555555555",
                "--private-key",
                str(key),
                "--output",
                str(output),
            ]
        )
    assert not output.exists()


def test_inventory_command_writes_only_allowlisted_failure_diagnostic(
    tmp_path, monkeypatch
):
    from ios import testflight_inventory as command

    key = tmp_path / "AuthKey_EXAMPLE123.p8"
    key.write_text("synthetic private key")
    key.chmod(0o600)
    output = tmp_path / "inventory.json"
    diagnostic = tmp_path / "inventory-diagnostic.json"
    monkeypatch.setattr(command, "Client", lambda *args: object())
    monkeypatch.setattr(
        command,
        "inventory",
        lambda *args: (_ for _ in ()).throw(
            InventoryFailure(
                "apps",
                Rejected(
                    "private bearer token https://private.example/response",
                    diagnostic_category="http",
                    http_status=403,
                ),
            )
        ),
    )

    with pytest.raises(
        SystemExit, match=r"stage=apps error_category=http http_status=403"
    ) as error:
        command.main(
            [
                "--bundle-id",
                "com.arnab.drover",
                "--api-key-id",
                "EXAMPLE123",
                "--api-issuer",
                "11111111-2222-3333-4444-555555555555",
                "--private-key",
                str(key),
                "--output",
                str(output),
                "--diagnostic-output",
                str(diagnostic),
            ]
        )

    assert "private.example" not in str(error.value)
    assert "token=secret" not in str(error.value)
    assert not output.exists()
    assert json.loads(diagnostic.read_text()) == {
        "error_category": "http",
        "http_status": 403,
        "stage": "apps",
    }


def test_inventory_command_rejects_adversarial_diagnostic_attributes(
    tmp_path, monkeypatch
):
    from ios import testflight_inventory as command

    class AdversarialError(Exception):
        inventory_stage = "https://private.example/?token=secret"
        diagnostic_category = "Bearer secret"
        http_status = 999

        def __str__(self):
            return "private response body with token=secret"

    key = tmp_path / "AuthKey_EXAMPLE123.p8"
    key.write_text("synthetic private key")
    key.chmod(0o600)
    diagnostic = tmp_path / "inventory-diagnostic.json"
    monkeypatch.setattr(command, "Client", lambda *args: object())
    monkeypatch.setattr(
        command,
        "inventory",
        lambda *args: (_ for _ in ()).throw(AdversarialError()),
    )

    with pytest.raises(
        SystemExit, match=r"stage=inventory error_category=unknown"
    ) as error:
        command.main(
            [
                "--bundle-id",
                "com.arnab.drover",
                "--api-key-id",
                "EXAMPLE123",
                "--api-issuer",
                "11111111-2222-3333-4444-555555555555",
                "--private-key",
                str(key),
                "--output",
                str(tmp_path / "inventory.json"),
                "--diagnostic-output",
                str(diagnostic),
            ]
        )

    assert "private.example" not in str(error.value)
    assert "secret" not in str(error.value)
    assert json.loads(diagnostic.read_text()) == {
        "error_category": "unknown",
        "stage": "inventory",
    }


def test_inventory_command_writes_fixed_prerelease_probe_without_inventory(
    tmp_path, monkeypatch
):
    from ios import testflight_inventory as command

    key = tmp_path / "AuthKey_EXAMPLE123.p8"
    key.write_text("synthetic private key")
    key.chmod(0o600)
    output = tmp_path / "inventory.json"
    diagnostic = tmp_path / "inventory-diagnostic.json"
    monkeypatch.setattr(command, "Client", lambda *args: object())
    monkeypatch.setattr(
        command,
        "inventory",
        lambda *args: pytest.fail("normal inventory must not run in probe mode"),
    )
    received = {}

    def probe(*args, **kwargs):
        received.update(kwargs)
        return {
            "probe": "pre-release-access",
            "configured_key_identity": "matches",
            "contracts": [
                {
                    "contract": "global-filtered",
                    "result": "rejected",
                    "error_category": "http",
                    "http_status": 403,
                    "apple_error_category": "forbidden",
                },
                {"contract": "app-scoped", "result": "ok"},
            ],
        }

    monkeypatch.setattr(command, "prerelease_access_probe", probe)
    monkeypatch.setenv("EXPECTED_KEY_ID", "EXAMPLE123")

    command.main(
        [
            "--bundle-id",
            "com.arnab.drover",
            "--api-key-id",
            "EXAMPLE123",
            "--api-issuer",
            "11111111-2222-3333-4444-555555555555",
            "--private-key",
            str(key),
            "--output",
            str(output),
            "--diagnostic-output",
            str(diagnostic),
            "--prerelease-access-probe",
            "--expected-key-id-env",
        ]
    )

    assert not output.exists()
    assert received["expected_key_id"] == "EXAMPLE123"
    assert json.loads(diagnostic.read_text()) == {
        "contracts": [
            {
                "apple_error_category": "forbidden",
                "contract": "global-filtered",
                "error_category": "http",
                "http_status": 403,
                "result": "rejected",
            },
            {"contract": "app-scoped", "result": "ok"},
        ],
        "configured_key_identity": "matches",
        "probe": "pre-release-access",
    }


def test_http_errors_never_expose_key_or_body(key):
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 403, "PRIVATE KEY MATERIAL", {}, None
            )

    client = Client(key[0], "KEY", "issuer", opener=Opener(), clock=lambda: 0)
    with pytest.raises(Rejected, match="HTTP 403") as error:
        client.request("GET", "/v1/apps", deadline=5)
    assert "PRIVATE" not in str(error.value)
    assert error.value.diagnostic_category == "http"
    assert error.value.http_status == 403


def test_transient_errors_are_bounded():
    from ios.testflight_api import Retryable

    class Unavailable(Fake):
        def collection(self, *args, **kwargs):
            raise Retryable()

    with pytest.raises(Rejected, match="timed out.*temporarily unavailable"):
        run(Unavailable())


def test_production_workflow_passes_distribution_arguments():
    import yaml

    workflow = yaml.safe_load(
        (
            Path(__file__).parents[1]
            / ".github/workflows/ios-testflight-production.yml"
        ).read_text()
    )
    job = workflow["jobs"]["archive-upload"]
    step = next(
        s
        for s in job["steps"]
        if s.get("name", "").startswith("Validate, upload, wait")
    )
    for argument in [
        "--validate",
        "--internal-group",
        "--version",
        "--build",
        "--info-plist",
    ]:
        assert argument in step["run"]
    assert (
        "vars.DROVER_TESTFLIGHT_INTERNAL_GROUP"
        in job["env"]["TESTFLIGHT_INTERNAL_GROUP"]
    )
    assert "Drover Internal" in job["env"]["TESTFLIGHT_INTERNAL_GROUP"]
    assert any(
        s.get("if") == "always()" and s.get("name") == "Remove temporary credentials"
        for s in job["steps"]
    )


@pytest.mark.parametrize("fails", [False, True])
def test_upload_wrapper_preserves_acknowledgement_during_distribution(
    tmp_path, monkeypatch, capsys, fails
):
    import plistlib
    import subprocess
    import sys

    import ios.testflight_api as api

    # Execute the wrapper's Python with fake altool and ASC boundaries.
    monkeypatch.setitem(sys.modules, "testflight_api", api)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    (private / "AuthKey_EXAMPLE123.p8").write_text("PRIVATE_SENTINEL")
    (private / "AuthKey_EXAMPLE123.p8").chmod(0o600)
    ipa = tmp_path / "Drover.ipa"
    ipa.write_bytes(b"IPA")
    info = tmp_path / "Info.plist"
    info.write_bytes(plistlib.dumps(INFO))
    record = tmp_path / "receipt.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "-",
            str(Path(__file__).parents[1] / "scripts/ios"),
            "--ipa",
            str(ipa),
            "--api-key-id",
            "EXAMPLE123",
            "--api-issuer",
            "11111111-2222-3333-4444-555555555555",
            "--private-keys-dir",
            str(private),
            "--record",
            str(record),
            "--version",
            "0.1.0",
            "--build",
            "3",
            "--info-plist",
            str(info),
            "--internal-group",
            "Drover Internal",
        ],
    )

    def upload(*args, **kwargs):
        kwargs["stdout"].write(
            json.dumps({"success-message": "No errors uploading archive."}).encode()
        )
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", upload)
    monkeypatch.setattr(api, "Client", lambda *args: Fake())

    def complete(*args):
        if fails:
            raise Rejected("configured internal beta group does not exist")
        return {"processing_state": "VALID", "internal_group_assigned": True}

    monkeypatch.setattr(api, "distribute", complete)
    source = (
        (Path(__file__).parents[1] / "scripts/ios/upload_testflight.sh")
        .read_text()
        .split("<<'PY'\n", 1)[1]
        .rsplit("\nPY", 1)[0]
    )
    if fails:
        with pytest.raises(SystemExit) as error:
            exec(compile(source, "upload_testflight.sh", "exec"), {})
        assert error.value.code == 1
    else:
        exec(compile(source, "upload_testflight.sh", "exec"), {})
    receipt = json.loads(record.read_text())
    assert receipt["upload_confirmed"] is True
    assert ("distribution_failure" in receipt) is fails
    captured = capsys.readouterr()
    assert "PRIVATE_SENTINEL" not in captured.out + captured.err + record.read_text()


def test_missing_encryption_declaration_with_known_encryption_fails_clearly():
    fake = Fake()
    fake.encryption = True
    fake.beta_state = "MISSING_EXPORT_COMPLIANCE"
    with pytest.raises(Rejected, match="missing export compliance"):
        run(fake, INFO | {"ITSAppUsesNonExemptEncryption": True})
    assert not fake.assigned


def test_compliance_propagation_timeout_is_clear():
    fake = Fake()
    fake.beta_state = "MISSING_EXPORT_COMPLIANCE"
    with pytest.raises(Rejected, match="timed out.*export compliance"):
        run(fake)
    assert not fake.assigned


@pytest.mark.parametrize("status", [429, 500, 503])
def test_client_transient_http_errors_retry_without_body(key, status):
    from ios.testflight_api import Retryable

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, status, "PRIVATE", {}, None)

    client = Client(key[0], "KEY", "issuer", opener=Opener(), clock=lambda: 0)
    with pytest.raises(Retryable):
        client.request("GET", "/v1/apps", deadline=5)


def test_assignment_must_be_visible_before_success():
    class Delayed(Fake):
        def request(self, method, path, *, body=None, deadline):
            if method == "POST":
                return {}  # Acknowledged write never becomes visible.
            return super().request(method, path, body=body, deadline=deadline)

    fake = Delayed()
    with pytest.raises(Rejected, match="timed out.*group assignment"):
        run(fake)


def test_missing_build_timeout_is_clear():
    class Missing(Fake):
        def collection(self, path, **kwargs):
            if path == "/v1/builds":
                return []
            return super().collection(path, **kwargs)

    with pytest.raises(Rejected, match="timed out.*build not yet visible"):
        run(Missing())
