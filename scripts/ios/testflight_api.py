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

    def collection(self, path, *, params=None, deadline):
        items = []
        while path:
            page = self.request("GET", path, params=params, deadline=deadline)
            items.extend(page["data"])
            path = page.get("links", {}).get("next")
            params = None
        return items


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


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
