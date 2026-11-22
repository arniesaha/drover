"""Bounded ASC processing/compliance/internal distribution; never log API bodies."""

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.appstoreconnect.apple.com"


class Rejected(ValueError):
    pass


class Retryable(Exception):
    pass


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

    def request(self, method, path, *, params=None, body=None, deadline):
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
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise Rejected("ASC response exceeded size limit")
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            if error.code == 429 or 500 <= error.code < 600:
                raise Retryable() from None
            raise Rejected(
                f"ASC {method} failed (HTTP {error.code}); check API permissions and export compliance in App Store Connect"
            ) from None
        except (urllib.error.URLError, TimeoutError):
            raise Retryable() from None
        except (json.JSONDecodeError, UnicodeError):
            raise Rejected("ASC returned malformed JSON") from None

    def collection(self, path, *, params=None, deadline, max_pages=None):
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
            page = self.request("GET", path, params=params, deadline=deadline)
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

    def collect(path, params):
        try:
            return client.collection(
                path, params=params, deadline=deadline, max_pages=max_pages
            )
        except Retryable:
            raise Rejected("ASC inventory is temporarily unavailable") from None

    apps = collect(
        "/v1/apps", {"filter[bundleId]": bundle_id, "limit": min(max_versions, 50)}
    )
    if len(apps) != 1 or not isinstance(apps[0], dict):
        raise Rejected("ASC app record is missing or ambiguous")
    try:
        app_id = _inventory_text(apps[0].get("id"), "app")
    except Rejected:
        raise Rejected("ASC app record is missing or ambiguous") from None

    versions_data = collect(
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
