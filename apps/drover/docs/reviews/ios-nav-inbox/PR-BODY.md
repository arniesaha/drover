## Summary

- Adds the Home/Inbox navigation treatment, including the floating New Session control.
- Passes the Accounts/cockpit provider snapshot into `LaunchModel`, so harness rows show the selected host's real remaining account quota instead of a placeholder.
- Keeps quota selection deterministic: the harness/provider mapping is host-scoped and multiple reported windows use the limiting percentage.

## Review screenshots

| State | Screenshot |
| --- | --- |
| Home, no account warning | [home-no-account-warning.png](home-no-account-warning.png) |
| Home, account warning | [home-account-warning.png](home-account-warning.png) |
| New Session, reported quota | [launch-picker-real-quota.png](launch-picker-real-quota.png) |
| Inbox | [inbox.png](inbox.png) |
| Floating New Session control clear of the list | [home-floating-new-session-pill.png](home-floating-new-session-pill.png) |

## Validation

- `swift test --package-path apps/drover/DroverKit --filter 'LaunchModelTests|AccountQuotaPresentationTests'`
- `xcodebuild … -only-testing:DroverUITests/AccessibilityJourneyUITests test`: 15 tests passed.
- `xcodebuild … -only-testing:DroverUITests/ObservabilityFixtureUITests test`: 17 tests passed.
