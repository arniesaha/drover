## Summary

- Removes Home's account capacity card and account-attention banner for every
  account payload, including retained and degraded data.
- Gives Accounts a dock badge plus an affected-first list with an explicit
  reason such as `OpenAI · 12% left` or `Claude · login expired`.
- Replaces the opaque system tab band with a translucent floating dock and a
  matching floating New Session control. Reduce Transparency uses solid
  contrast-preserving surfaces.

## Review screenshots

| State | Screenshot |
| --- | --- |
| Home, dark, list scrolled beneath dock and New Session | [home-dark-scrolled.png](home-dark-scrolled.png) |
| Accounts, dark, affected account and dock badge | [accounts-dark-attention-badge.png](accounts-dark-attention-badge.png) |
| Home, light, Reduce Transparency | [home-light-reduce-transparency.png](home-light-reduce-transparency.png) |
| Accounts, light, Reduce Transparency | [accounts-light-reduce-transparency.png](accounts-light-reduce-transparency.png) |

## Validation

- `swift test --package-path apps/drover/DroverKit --filter AccountHealthPresentationTests`
- Focused `InboxStatusHeaderLayoutTests`
- Focused `ObservabilityFixtureUITests` checks for real-shaped account data,
  Reduce Transparency screenshots, and the final Home row clearing the dock.
