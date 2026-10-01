# Provider capacity account identity

The TestFlight 0.1.0 (5) duplicate Google card came from the Studio's agy
1.2.11 installation: `~/.gemini/google_accounts.json` was absent, while
`~/.gemini/antigravity-cli/antigravity-oauth-token` contained an `id_token`
with the signed-in email. The old probe read only the account file and emitted
`Antigravity`, splitting it from Mini/NAS readings with the same user's email.

The probe reads identity from the credential source used for quota (macOS
Keychain service `gemini`, account `antigravity`, then the credential file).
It decodes the ID token as local identity metadata, without using it as proof
of authentication. The email becomes `account_identity` after trimming and
lowercasing. If only Google's subject is available, identity is `google-sub:`
plus its SHA-256 hash. The legacy account file's active email is a fallback;
its `old` list is not evidence of a signed-in user. Missing or malformed
identity leaves `account_identity` null and the label `Unknown account`;
quota collection still runs. Credentials are never written or returned.

The hub persists the new field in Arrow/Parquet and returns it in capacity
responses. Older Parquet rows remain readable through union-by-name. For
older email-bearing snapshots the hub derives normalized email identity.

DroverKit groups by provider plus explicit identity, falling back to a
normalized email for old readings. Display names and plan labels do not
identify accounts. Unknown readings join the sole known identity for that
provider in the supplied fleet readings. With zero or multiple known accounts,
unknown readings form one `Unknown account` group. This is deliberately a
presentation inference, not a rewrite of stored identity. With ambiguity,
unknown readings never choose between known accounts.

Each account uses the freshest healthy eligible host's windows, then the
freshest eligible host, then the freshest historical host if all are collapsed.
The display label can come from another member carrying a known identity.
Quotas are never added across hosts (#456).

`ProviderSubscriptionGrouping.staleHostThreshold` is 72 hours. A
`host_offline` reading whose last successful observation is older than that
threshold moves to a collapsed **Stale hosts** disclosure on Accounts.
`host_retired` moves there immediately; the hub refresh loop emits this category
when the registry host status is `retired`. Exactly 72 hours remains visible.
Collapsed hosts cannot supply Home quotas. Accounts with no eligible hosts
remain on Accounts and are excluded from Home's account count, meter strip,
and lowest-remaining calculation. All snapshots and history are retained.

## Implementation files

- `src/drover/server/providers/agy.py`: current credential identity and legacy fallback.
- `src/drover/server/providers/types.py`: optional identity and Arrow field; normalized legacy emails.
- `src/drover/server/providers/service.py`: identity ingestion and stored snapshot decoding.
- `src/drover/server/harness/daemon.py`: identity in the host usage response.
- `src/drover/server/cockpit/service.py`: retired-host connector overlay.
- `apps/drover/DroverKit/Sources/DroverKit/CockpitModels.swift`: wire identity and unknown-label decoding.
- `apps/drover/DroverKit/Sources/DroverKit/CockpitPresentation.swift`: account grouping, stale host classification and Home eligibility.
- `apps/drover/Drover/Screens/Cockpit/ProviderCapacitySection.swift`: collapsed stale-host disclosure.
- `apps/drover/Drover/UITesting/ObservabilityFixtureRoot.swift`: Studio fallback, Mini/NAS email fixture.
- `apps/drover/DroverUITests/ObservabilityFixtureUITests.swift`: regression journey alongside #447/#456 journeys.
- `apps/drover/DroverKit/Tests/DroverKitTests/CockpitPresentationTests.swift`: grouping, ambiguity, normalization, missing identity, threshold and retirement tests.
- `tests/test_structured_agy.py`: credential identity parsing and missing/malformed identity tests.
- `tests/fixtures/agy/identity-present.json` and `identity-missing.json`: synthetic credential fixtures.
- `tests/test_provider_usage.py`: identity persistence/wire round trip and retired-host overlay/recovery.

## Verification on 2026-10-01

Base matched `origin/main`, `4da686ba055553fd9de410680851b206970dde26`.
`git log --oneline -3` returned #456 (`4da686b`), #455 (`3e59d86`),
and #454 (`c631f2a`). The local `main` ref was two commits behind; the worktree
was already based on the current remote main.

Dependencies: `uv sync --extra dev`. All commands below ran from the repository
root unless otherwise noted. Both backend commands removed every inherited
`DROVER_DUCKDB_*` variable before launching pytest.

Focused backend: **312 passed** (97.75 seconds):

```sh
python3 - <<'PY' > /tmp/provider-focused-final.log 2>&1
import os
for k in list(os.environ):
 if k.startswith('DROVER_DUCKDB_'): del os.environ[k]
os.execv('.venv/bin/pytest',['pytest','tests/test_structured_agy.py','tests/test_provider_usage.py','tests/test_provider_snapshot_partitions.py','tests/test_harness_daemon.py','tests/test_cockpit_analytics.py','-q'])
PY
```

Full backend: **4,379 passed, 69 skipped, 9 warnings** (170.37 seconds).
Warnings were Python fork and MCP client deprecations.

```sh
python3 - <<'PY' > /tmp/provider-backend-full.log 2>&1
import os
for k in list(os.environ):
 if k.startswith('DROVER_DUCKDB_'): del os.environ[k]
os.execv('.venv/bin/pytest',['pytest','-n','4','-q'])
PY
```

DroverKit: **797 passed in 40 suites** (12.704 seconds):

```sh
swift test --package-path apps/drover/DroverKit > /tmp/provider-swift-final.log 2>&1
```

Simulator: iPhone 18 Pro, iOS 27.0,
`B149C6A0-62BA-4D8D-9AA1-6B5514F883E8`. Generated the project with
`xcodegen generate` from `apps/drover`. Simulator tests use normal signing:
an initial run with `CODE_SIGNING_ALLOWED=NO` failed Keychain tests with
`-34018`, and was replaced by a signed run. The regression UI test was also
corrected to query the visible account button and the stale host's accessibility
label (host titles may fall back to lower-case ids), instead of a container
identifier and visual text. Cards explicitly contain their accessibility
children so the account and stale-host disclosure remain separate controls.

Observability UI journeys: **5 passed, 0 failures** (68.875 seconds), including
all four existing #447/#456 journeys and the Studio/Mini/NAS regression:

```sh
xcodebuild -project apps/drover/Drover.xcodeproj -scheme DroverUITests \
  -destination 'platform=iOS Simulator,id=B149C6A0-62BA-4D8D-9AA1-6B5514F883E8' \
  -derivedDataPath /tmp/drover-provider-derived \
  -only-testing:DroverUITests/ObservabilityFixtureUITests test \
  > /tmp/provider-ui-tests-final.log 2>&1
```

The local identity smoke check also returned `arniesaha@gmail.com` as both
label and identity, without querying quota or changing agy's credential:

```sh
uv run python - <<'PY'
from drover.server.providers.agy import AgyUsageProbe
label, identity = AgyUsageProbe()._account_metadata()
print({'account_label': label, 'account_identity': identity})
PY
```

Final app unit run: **899 passed in 61 suites** (12.910 seconds), including
DroverKit tests and native card layout tests:

```sh
xcodebuild -project apps/drover/Drover.xcodeproj -scheme Drover \
  -destination 'platform=iOS Simulator,id=B149C6A0-62BA-4D8D-9AA1-6B5514F883E8' \
  -derivedDataPath /tmp/drover-provider-derived test \
  > /tmp/provider-app-tests-final.log 2>&1
```

`git diff --check` and Black's check of the seven changed Python files passed.
No live hub data, credential stores, deployment, push or PR was changed.
