# Drover design system

Drover uses one compact system across iOS and web. Screen-specific colors, ad-hoc status terms, and one-off empty states are not part of the system.

## Tokens

| Role | Web | iOS |
| --- | --- | --- |
| Color | `--canvas`, `--surface`, `--surface-raised`, `--text-primary`, `--text-secondary`, `--border`, `--accent`, `--accent-subtle`, `--success`, `--warning`, `--danger`, `--neutral` | `Color.drover*` asset colors with the same semantic role |
| Type | display 32/38, title 22/28, body 16/24, meta 13/18, caption 11/14; all web values are rem-based | `DroverTextStyle`, relative to Dynamic Type styles |
| Spacing | 4, 8, 12, 16, 24, 32, 48 (`--space-*`) | `DroverSpace` constants |
| Radius | control 8, card 12, sheet/dock 16 | matching `RoundedRectangle` radii |
| Motion | 150 ms maximum; disable decorative transitions under reduced motion | honour Reduce Motion |

Both light and dark themes define every color role. Components consume roles rather than literal colors. Layout must reflow at browser zoom / large Dynamic Type; never rely on fixed text heights.

## Shared components

### ScreenHeader

Every top-level screen has one. Title is required; eyebrow, subtitle/meta, up to two trailing actions, and a filter row are optional. Web uses `ScreenHeader`; iOS uses the corresponding `ScreenHeader` view inside each root `NavigationStack` while retaining native navigation semantics.

### ListRow

Leading identity, title, one metadata line, trailing `StatusChip`, and an optional enabled 44 pt action. Variants are standard (bounded/actionable), compact (history), and data (ranked/quota). Use cards only for a bounded group, not every text block.

### Card

`surfaceRaised`, border, 12 pt radius, 16 pt padding. It groups a bounded piece of information or an actionable standard row.

### StatusChip

The only labels are: `Needs input`, `Needs approval`, `Running`, `Finished`, `Failed`, `Terminated`, and `Unknown`. Icon and semantic role communicate state; provider and screen identity never change its meaning.

### StateView

Used for empty, loading, error, and unavailable states. It contains an icon, title, short explanation, optional meta, and at most one recovery action. Show a skeleton only where the final content shape is already known.

## Navigation

The shared destination order is Home, Inbox, Analytics, Accounts, Settings. Web uses a sidebar that becomes a five-item bottom bar on narrow screens. `New Session` is global: header/sidebar on wide layouts and immediately above the phone tab bar. Session detail carries its source (`Home` or `Inbox`) in its back affordance.
