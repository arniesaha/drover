"""Bounded ASC processing/compliance/internal distribution; never log API bodies."""

import base64
import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.appstoreconnect.apple.com"
MAX_PROBE_RESPONSE_BYTES = 64 * 1024
MAX_PROBE_ERROR_BYTES = 16 * 1024
APPLE_ERROR_CATEGORIES = frozenset(
    {"forbidden", "unauthorized", "not-found", "parameter", "rate-limited", "unknown"}
)
APPLE_ERROR_REASONS = frozenset({"agreement-required", "unknown"})
APPLE_ERROR_CODE_REASONS = {
    # This exact code was returned by the protected prerelease diagnostic.
    # Do not match prefixes: Apple error text stays private and untrusted.
    "FORBIDDEN.REQUIRED_AGREEMENTS": "agreement-required",
}


class Rejected(ValueError):
    def __init__(
        self,
        message,
        *,
        diagnostic_category="rejected",
        http_status=None,
        apple_error_category=None,
        apple_error_reason=None,
    ):
        super().__init__(message)
        self.diagnostic_category = diagnostic_category
        self.http_status = http_status
        self.apple_error_category = apple_error_category
        self.apple_error_reason = apple_error_reason


class Retryable(Exception):
    def __init__(
        self, *_private_details, diagnostic_category="temporary", http_status=None
    ):
        super().__init__()
        self.diagnostic_category = diagnostic_category
        self.http_status = http_status


class InventoryFailure(Rejected):
    """A fixed-stage, redaction-safe summary of a GET-only inventory failure."""

    def __init__(self, stage, cause):
        category = getattr(cause, "diagnostic_category", "unknown")
        super().__init__(
            (
                "ASC inventory is temporarily unavailable"
                if category in {"temporary", "network"}
                else "ASC inventory request failed"
            ),
            diagnostic_category=category,
            http_status=getattr(cause, "http_status", None),
            apple_error_category=getattr(cause, "apple_error_category", None),
            apple_error_reason=getattr(cause, "apple_error_reason", None),
        )
        self.inventory_stage = stage


def _apple_error_details(error):
    """Classify a bounded ASC error body without retaining any of its text."""
    try:
        raw = error.read(MAX_PROBE_ERROR_BYTES + 1)
        if not isinstance(raw, bytes) or len(raw) > MAX_PROBE_ERROR_BYTES:
            return "unknown", "unknown"
        payload = json.loads(raw.decode("utf-8"))
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if not isinstance(errors, list) or len(errors) != 1:
            return "unknown", "unknown"
        code = errors[0].get("code") if isinstance(errors[0], dict) else None
        if not isinstance(code, str) or len(code) > 128:
            return "unknown", "unknown"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "unknown", "unknown"

    normalized = code.upper()
    reason = APPLE_ERROR_CODE_REASONS.get(normalized, "unknown")
    if normalized.startswith("FORBIDDEN"):
        return "forbidden", reason
    if normalized.startswith("UNAUTHORIZED"):
        return "unauthorized", reason
    if normalized.startswith("NOT_FOUND"):
        return "not-found", reason
    if normalized.startswith("PARAMETER"):
        return "parameter", reason
    if normalized.startswith("RATE_LIMIT"):
        return "rate-limited", reason
    return "unknown", reason


def token(key, key_id, issuer, now):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    claims = {
        "iss": issuer,
        "iat": int(now),
        "exp": int(now) + 600,
        "aud": "appstoreconnect-v1",
    }
    payload = b".".join(encode(json.dumps(v).encode()) for v in (header, claims))
    r, s = decode_dss_signature(key.sign(payload, ec.ECDSA(hashes.SHA256())))
    return (
        payload + b"." + encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    ).decode()


class Client:
    def __init__(self, key_path, key_id, issuer, *, opener=None, clock=time.monotonic):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        self.key = serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        )
        if not isinstance(self.key, ec.EllipticCurvePrivateKey) or not isinstance(
            self.key.curve, ec.SECP256R1
        ):
            raise Rejected("ASC requires an ES256 private key")
        self.key_id, self.issuer = key_id, issuer
        self.clock = clock
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def request(
        self,
        method,
        path,
        *,
        params=None,
        body=None,
        deadline,
        diagnostic_error=False,
        max_response_bytes=2 * 1024 * 1024,
    ):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise Rejected(
                "Apple processing/distribution timed out; do not re-upload this build"
            )
        url = path if path.startswith(BASE + "/v1/") else BASE + path
        if not url.startswith(BASE + "/v1/"):
            raise Rejected("ASC returned an unsafe pagination URL")
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer "
                + token(self.key, self.key_id, self.issuer, time.time()),
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(request, timeout=min(30, remaining)) as response:
                raw = response.read(max_response_bytes + 1)
            if len(raw) > max_response_bytes:
                raise Rejected("ASC response exceeded size limit")
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            apple_error_category, apple_error_reason = (
                _apple_error_details(error) if diagnostic_error else (None, None)
            )
            if error.code == 429 or 500 <= error.code < 600:
                raise Retryable(http_status=error.code) from None
            raise Rejected(
                f"ASC {method} failed (HTTP {error.code}); check API permissions and export compliance in App Store Connect",
                diagnostic_category="http",
                http_status=error.code,
                apple_error_category=apple_error_category,
                apple_error_reason=apple_error_reason,
            ) from None
        except (urllib.error.URLError, TimeoutError):
            raise Retryable(diagnostic_category="network") from None
        except (json.JSONDecodeError, UnicodeError):
            raise Rejected(
                "ASC returned malformed JSON", diagnostic_category="malformed"
            ) from None

    def collection(
        self,
        path,
        *,
        params=None,
        deadline,
        max_pages=None,
        diagnostic_error=False,
        max_response_bytes=2 * 1024 * 1024,
    ):
        if max_pages is not None and (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or max_pages < 1
        ):
            raise Rejected("invalid ASC collection page limit")
        items = []
        pages = 0
        while path:
            if max_pages is not None and pages >= max_pages:
                raise Rejected("ASC collection exceeded page limit")
            page = self.request(
                "GET",
                path,
                params=params,
                deadline=deadline,
                diagnostic_error=diagnostic_error,
                max_response_bytes=max_response_bytes,
            )
            pages += 1
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise Rejected("ASC returned malformed collection")
            items.extend(page["data"])
            links = page.get("links", {})
            if not isinstance(links, dict):
                raise Rejected("ASC returned malformed collection")
            path = links.get("next")
            if path is not None and (not isinstance(path, str) or not path):
                raise Rejected("ASC returned malformed pagination")
            params = None
        return items


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _inventory_text(value, field):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 191
        or any(ord(character) < 32 for character in value)
    ):
        raise Rejected(f"ASC inventory {field} is incomplete")
    return value


def inventory(
    client,
    bundle_id,
    *,
    timeout=60,
    max_pages=2,
    max_versions=20,
    max_builds_per_version=50,
    max_internal_groups=50,
    clock=time.monotonic,
):
    """Return a bounded, GET-only projection of existing iOS TestFlight state.

    The result deliberately contains no ASC resource IDs, account details,
    tokens, diagnostics, or candidate selection.  Callers can inspect existing
    versions/builds/groups but must still choose any later release candidate.
    """
    if (
        not isinstance(bundle_id, str)
        or not bundle_id.strip()
        or len(bundle_id) > 191
        or not 0 < timeout <= 600
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (
                max_pages,
                max_versions,
                max_builds_per_version,
                max_internal_groups,
            )
        )
    ):
        raise Rejected("invalid ASC inventory bounds")
    deadline = clock() + timeout

    def collect(stage, path, params):
        try:
            return client.collection(
                path, params=params, deadline=deadline, max_pages=max_pages
            )
        except (Rejected, Retryable) as error:
            raise InventoryFailure(stage, error) from None

    apps = collect(
        "apps",
        "/v1/apps",
        {"filter[bundleId]": bundle_id, "limit": min(max_versions, 50)},
    )
    if len(apps) != 1 or not isinstance(apps[0], dict):
        raise Rejected("ASC app record is missing or ambiguous")
    try:
        app_id = _inventory_text(apps[0].get("id"), "app")
    except Rejected:
        raise Rejected("ASC app record is missing or ambiguous") from None

    versions_data = collect(
        "pre-release-versions",
        "/v1/preReleaseVersions",
        {
            "filter[app]": app_id,
            "filter[platform]": "IOS",
            "limit": min(max_versions, 50),
        },
    )
    if len(versions_data) > max_versions:
        raise Rejected("ASC inventory exceeds version limit")
    versions = []
    builds = []
    for version_data in versions_data:
        if not isinstance(version_data, dict) or not isinstance(
            version_data.get("attributes"), dict
        ):
            raise Rejected("ASC inventory version metadata is incomplete")
        version_id = _inventory_text(version_data.get("id"), "version")
        version = _inventory_text(version_data["attributes"].get("version"), "version")
        versions.append({"version": version})
        version_builds = collect(
            "builds",
            f"/v1/preReleaseVersions/{version_id}/builds",
            {"limit": min(max_builds_per_version, 50)},
        )
        if len(version_builds) > max_builds_per_version:
            raise Rejected("ASC inventory exceeds build limit")
        for build_data in version_builds:
            if not isinstance(build_data, dict) or not isinstance(
                build_data.get("attributes"), dict
            ):
                raise Rejected("ASC inventory build metadata is incomplete")
            attributes = build_data["attributes"]
            builds.append(
                {
                    "version": version,
                    "build": _inventory_text(attributes.get("version"), "build"),
                    "processing_state": _inventory_text(
                        attributes.get("processingState"), "processing state"
                    ),
                }
            )

    groups_data = collect(
        "internal-groups",
        "/v1/betaGroups",
        {
            "filter[app]": app_id,
            "filter[isInternalGroup]": "true",
            "limit": min(max_internal_groups, 50),
        },
    )
    if len(groups_data) > max_internal_groups:
        raise Rejected("ASC inventory exceeds internal group limit")
    groups = []
    for group_data in groups_data:
        if not isinstance(group_data, dict) or not isinstance(
            group_data.get("attributes"), dict
        ):
            raise Rejected("ASC inventory group metadata is incomplete")
        attributes = group_data["attributes"]
        if attributes.get("isInternalGroup") is not True:
            raise Rejected("ASC inventory group metadata is incomplete")
        groups.append({"name": _inventory_text(attributes.get("name"), "group")})

    return {
        "versions": [
            {"version": version}
            for version in sorted({row["version"] for row in versions})
        ],
        "builds": sorted(builds, key=lambda row: (row["version"], row["build"])),
        "internal_groups": sorted(groups, key=lambda row: row["name"]),
    }


def _probe_failure(contract, error):
    result = {
        "contract": contract,
        "result": "rejected",
        "error_category": getattr(error, "diagnostic_category", "unknown"),
    }
    if getattr(error, "http_status", None) is not None:
        result["http_status"] = error.http_status
    if getattr(error, "apple_error_category", None) in APPLE_ERROR_CATEGORIES:
        result["apple_error_category"] = error.apple_error_category
    if getattr(error, "apple_error_reason", None) in APPLE_ERROR_REASONS:
        result["apple_error_reason"] = error.apple_error_reason
    return result


def _probe_collection_page(client, path, *, params, deadline):
    """Read one validated collection page without traversing pagination links."""
    page = client.request(
        "GET",
        path,
        params=params,
        deadline=deadline,
        diagnostic_error=True,
        max_response_bytes=MAX_PROBE_RESPONSE_BYTES,
    )
    if not isinstance(page, dict) or not isinstance(page.get("data"), list):
        raise Rejected("ASC returned malformed collection")
    links = page.get("links")
    if links is not None:
        if not isinstance(links, dict):
            raise Rejected("ASC returned malformed collection")
        next_link = links.get("next")
        if next_link is not None and not isinstance(next_link, str):
            raise Rejected("ASC returned malformed pagination")
    return page["data"]


def prerelease_access_probe(
    client,
    bundle_id,
    *,
    configured_key_id,
    expected_key_id=None,
    timeout=45,
    clock=time.monotonic,
):
    """Compare documented prerelease reads for one resolved app, GET-only."""
    if (
        not isinstance(bundle_id, str)
        or not bundle_id.strip()
        or len(bundle_id) > 191
        or not isinstance(configured_key_id, str)
        or not re.fullmatch(r"[A-Z0-9]{8,20}", configured_key_id)
        or (
            expected_key_id is not None
            and (
                not isinstance(expected_key_id, str)
                or not re.fullmatch(r"[A-Z0-9]{8,20}", expected_key_id)
            )
        )
        or not 0 < timeout <= 60
    ):
        raise Rejected("invalid ASC prerelease access probe bounds")
    deadline = clock() + timeout
    try:
        apps = _probe_collection_page(
            client,
            "/v1/apps",
            params={"filter[bundleId]": bundle_id, "limit": 1},
            deadline=deadline,
        )
    except (Rejected, Retryable) as error:
        raise InventoryFailure("apps", error) from None
    if len(apps) != 1 or not isinstance(apps[0], dict):
        raise Rejected("ASC app record is missing or ambiguous")
    try:
        app_id = _inventory_text(apps[0].get("id"), "app")
    except Rejected:
        raise Rejected("ASC app record is missing or ambiguous") from None

    def compare(contract, path, params):
        try:
            _probe_collection_page(
                client,
                path,
                params=params,
                deadline=deadline,
            )
        except (Rejected, Retryable) as error:
            return _probe_failure(contract, error)
        return {"contract": contract, "result": "ok"}

    return {
        "probe": "pre-release-access",
        "configured_key_identity": (
            "not-compared"
            if expected_key_id is None
            else (
                "matches"
                if hmac.compare_digest(configured_key_id, expected_key_id)
                else "mismatch"
            )
        ),
        "contracts": [
            compare(
                "global-filtered",
                "/v1/preReleaseVersions",
                {"filter[app]": app_id, "filter[platform]": "IOS", "limit": 1},
            ),
            compare(
                "app-scoped",
                f"/v1/apps/{app_id}/preReleaseVersions",
                # Apple documents this app-bound route with `limit`; its current
                # DocC contract does not list filter[platform]. Do not assume it.
                {"limit": 1},
            ),
        ],
    }


def distribute(
    client,
    info,
    version,
    build,
    group="Drover Internal",
    *,
    timeout=2700,
    interval=30,
    clock=time.monotonic,
    sleep=time.sleep,
):
    """Exact app/version/build selection. All requests share one wall deadline."""
    if (
        info.get("CFBundleShortVersionString") != version
        or str(info.get("CFBundleVersion")) != build
    ):
        raise Rejected("candidate version/build does not match Info.plist")
    if not 0 < timeout <= 2700 or interval <= 0 or not group.strip():
        raise Rejected("invalid processing timeout or internal group")
    deadline = clock() + timeout
    app_id = group_id = None
    compliance_submitted = assignment_submitted = False
    last = "build not yet visible"
    while clock() < deadline:
        try:
            if app_id is None:
                apps = client.collection(
                    "/v1/apps",
                    params={"filter[bundleId]": info["CFBundleIdentifier"]},
                    deadline=deadline,
                )
                if len(apps) != 1:
                    raise Rejected(
                        "ASC app record missing or ambiguous for candidate bundle ID"
                    )
                app_id = apps[0]["id"]
            if group_id is None:
                groups = client.collection(
                    "/v1/betaGroups",
                    params={
                        "filter[app]": app_id,
                        "filter[name]": group,
                        "filter[isInternalGroup]": "true",
                    },
                    deadline=deadline,
                )
                groups = [
                    g
                    for g in groups
                    if g["attributes"].get("name") == group
                    and g["attributes"].get("isInternalGroup") is True
                ]
                if len(groups) != 1:
                    raise Rejected(
                        "configured internal beta group does not exist or is ambiguous; create it in App Store Connect"
                    )
                group_id = groups[0]["id"]
            uploads = client.collection(
                f"/v1/apps/{app_id}/buildUploads",
                params={
                    "filter[cfBundleShortVersionString]": version,
                    "filter[cfBundleVersion]": build,
                    "filter[platform]": "IOS",
                },
                deadline=deadline,
            )
            for upload in uploads:
                state = upload["attributes"].get("state", {}).get("state")
                if state in {"FAILED", "INVALID"}:
                    raise Rejected(
                        "Apple build upload FAILED/INVALID; inspect App Store Connect and submit a corrected build"
                    )
            builds = client.collection(
                "/v1/builds",
                params={
                    "filter[app]": app_id,
                    "filter[version]": build,
                    "filter[preReleaseVersion.version]": version,
                    "filter[preReleaseVersion.platform]": "IOS",
                },
                deadline=deadline,
            )
            if len(builds) > 1:
                raise Rejected("ASC candidate build is ambiguous")
            if builds:
                candidate = builds[0]
                attributes = candidate["attributes"]
                state = attributes.get("processingState")
                last = "Apple processing pending"
                if state in {"FAILED", "INVALID"}:
                    raise Rejected(
                        "Apple build processing FAILED/INVALID; inspect App Store Connect and submit a corrected build"
                    )
                if state == "VALID":
                    if attributes.get("expired"):
                        raise Rejected("candidate build is expired")
                    build_id = candidate["id"]
                    if info.get("ITSAppUsesNonExemptEncryption") is False:
                        if attributes.get("usesNonExemptEncryption") is not False:
                            if not compliance_submitted:
                                client.request(
                                    "PATCH",
                                    f"/v1/builds/{build_id}",
                                    body={
                                        "data": {
                                            "type": "builds",
                                            "id": build_id,
                                            "attributes": {
                                                "usesNonExemptEncryption": False
                                            },
                                        }
                                    },
                                    deadline=deadline,
                                )
                                compliance_submitted = True
                            last = "waiting for export compliance to propagate"
                            sleep(min(interval, max(0, deadline - clock())))
                            continue
                    elif attributes.get("usesNonExemptEncryption") is None:
                        raise Rejected(
                            "missing export compliance; Info.plist does not declare ITSAppUsesNonExemptEncryption=NO; resolve in App Store Connect"
                        )
                    detail = client.request(
                        "GET",
                        f"/v1/builds/{build_id}/buildBetaDetail",
                        deadline=deadline,
                    )["data"]["attributes"]
                    if detail.get("internalBuildState") == "MISSING_EXPORT_COMPLIANCE":
                        if info.get("ITSAppUsesNonExemptEncryption") is not False:
                            raise Rejected(
                                "missing export compliance; resolve the encryption declaration in App Store Connect"
                            )
                        last = "waiting for export compliance to propagate"
                        sleep(min(interval, max(0, deadline - clock())))
                        continue
                    assigned = client.collection(
                        "/v1/betaGroups",
                        params={"filter[id]": group_id, "filter[builds]": build_id},
                        deadline=deadline,
                    )
                    if any(g["id"] == group_id for g in assigned):
                        return {
                            "processing_state": "VALID",
                            "internal_group_assigned": True,
                            "build_id": build_id,
                            "internal_group_id": group_id,
                        }
                    if not assignment_submitted:
                        client.request(
                            "POST",
                            f"/v1/betaGroups/{group_id}/relationships/builds",
                            body={"data": [{"type": "builds", "id": build_id}]},
                            deadline=deadline,
                        )
                        assignment_submitted = True
                    last = "waiting for internal group assignment to propagate"
        except Retryable:
            last = "ASC temporarily unavailable or rate limited"
        sleep(min(interval, max(0, deadline - clock())))
    raise Rejected(
        f"Apple processing/distribution timed out: {last}; do not re-upload this build; inspect App Store Connect"
    )
