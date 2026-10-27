import SwiftUI
import DroverKit

// MARK: - 1.0 design tokens

/// Semantic roles used by product UI.  They intentionally alias the existing
/// adaptive palette so old screens and new shared components always resolve
/// to the same light/dark ramp.
enum DroverTheme {
    static let canvas = DroverColor.bg
    static let surface = DroverColor.surface
    static let surfaceRaised = DroverColor.sheet
    static let textPrimary = DroverColor.text
    static let textSecondary = DroverColor.muted
    static let border = DroverColor.line
    static let accent = DroverColor.accent
    static let accentSubtle = DroverColor.accentTint
    static let success = DroverColor.accentHi
    static let warning = DroverColor.warn
    static let danger = DroverColor.warn
    static let neutral = DroverColor.faint
}

enum DroverSpace {
    static let x1: CGFloat = 4
    static let x2: CGFloat = 8
    static let x3: CGFloat = 12
    static let x4: CGFloat = 16
    static let x6: CGFloat = 24
    static let x8: CGFloat = 32
    static let x12: CGFloat = 48
}

enum DroverRadius {
    static let control: CGFloat = 8
    static let card: CGFloat = 12
    static let sheet: CGFloat = 16
}

enum DroverTypeScale {
    static let display = Font.system(.largeTitle, design: .default, weight: .semibold)
    static let title = Font.system(.title2, design: .default, weight: .semibold)
    static let section = Font.system(.subheadline, design: .default, weight: .semibold)
    static let body = Font.system(.body, design: .default)
    static let meta = Font.system(.footnote, design: .default)
    static let caption = Font.system(.caption2, design: .monospaced)
}

/// Consistent top-level anatomy. The title is always app-owned rather than a
/// mixture of bespoke wordmarks and navigation-bar titles.
struct ScreenHeader<Actions: View, Filters: View>: View {
    let title: String
    var eyebrow: String?
    var subtitle: String?
    var meta: String?
    @ViewBuilder let actions: () -> Actions
    @ViewBuilder let filters: () -> Filters

    init(
        title: String,
        eyebrow: String? = nil,
        subtitle: String? = nil,
        meta: String? = nil,
        @ViewBuilder actions: @escaping () -> Actions = { EmptyView() },
        @ViewBuilder filters: @escaping () -> Filters = { EmptyView() }
    ) {
        self.title = title
        self.eyebrow = eyebrow
        self.subtitle = subtitle
        self.meta = meta
        self.actions = actions
        self.filters = filters
    }

    var body: some View {
        VStack(alignment: .leading, spacing: DroverSpace.x2) {
            HStack(alignment: .top, spacing: DroverSpace.x3) {
                VStack(alignment: .leading, spacing: DroverSpace.x1) {
                    if let eyebrow {
                        Text(eyebrow)
                            .font(DroverTypeScale.caption)
                            .foregroundStyle(DroverTheme.textSecondary)
                            .textCase(.uppercase)
                            .accessibilityIdentifier(eyebrow.caseInsensitiveCompare("demo") == .orderedSame ? "demo-badge" : "screen-header-eyebrow")
                    }
                    Text(title)
                        .font(DroverTypeScale.title)
                        .foregroundStyle(DroverTheme.textPrimary)
                    if let subtitle {
                        Text(subtitle)
                            .font(DroverTypeScale.meta)
                            .foregroundStyle(DroverTheme.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: DroverSpace.x2)
                actions()
            }
            if let meta {
                Text(meta)
                    .font(DroverTypeScale.meta)
                    .foregroundStyle(DroverTheme.textSecondary)
            }
            filters()
        }
        .padding(.horizontal, DroverSpace.x4)
        .padding(.vertical, DroverSpace.x3)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(DroverTheme.canvas)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("screen-header-\(title.lowercased())")
    }
}

struct Card<Content: View>: View {
    @ViewBuilder let content: () -> Content

    var body: some View {
        content()
            .padding(DroverSpace.x4)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(DroverTheme.surfaceRaised,
                        in: RoundedRectangle(cornerRadius: DroverRadius.card, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: DroverRadius.card, style: .continuous)
                    .strokeBorder(DroverTheme.border, lineWidth: 1)
            }
    }
}

enum DroverStatus: String, CaseIterable, Sendable {
    case needsInput = "Needs input"
    case needsApproval = "Needs approval"
    case running = "Running"
    case finished = "Finished"
    case failed = "Failed"
    case terminated = "Terminated"
    case unknown = "Unknown"

    init(session: SessionSummary) {
        switch session.attention {
        case .needsInput: self = .needsInput
        case .needsApproval: self = .needsApproval
        case .working: self = .running
        case .done:
            self = session.status.lowercased() == "terminated" ? .terminated : .finished
        case .errored: self = .failed
        }
    }

    var symbolName: String {
        switch self {
        case .needsInput: "text.bubble"
        case .needsApproval: "checkmark.seal"
        case .running: "arrow.triangle.2.circlepath"
        case .finished: "checkmark.circle"
        case .failed: "exclamationmark.circle"
        case .terminated: "stop.circle"
        case .unknown: "questionmark.circle"
        }
    }

    var role: PaletteToken {
        switch self {
        case .needsInput, .needsApproval: DroverTheme.warning
        case .running, .finished: DroverTheme.success
        case .failed, .terminated: DroverTheme.danger
        case .unknown: DroverTheme.neutral
        }
    }
}

struct StatusChip: View {
    let status: DroverStatus

    var body: some View {
        Label(status.rawValue, systemImage: status.symbolName)
            .font(DroverTypeScale.caption.weight(.semibold))
            .foregroundStyle(status.role)
            .padding(.horizontal, DroverSpace.x2)
            .padding(.vertical, DroverSpace.x1)
            .background(status.role.opacity(0.14), in: Capsule())
            .overlay { Capsule().strokeBorder(status.role.opacity(0.45), lineWidth: 1) }
            .accessibilityLabel(status.rawValue)
    }
}

/// A shared standard/compact/data row. Use `Card` for the standard variant;
/// compact and data variants deliberately stay flat.
struct ListRow<Leading: View, Trailing: View>: View {
    enum Variant { case standard, compact, data }
    let title: String
    let detail: String
    let variant: Variant
    var status: DroverStatus?
    @ViewBuilder let leading: () -> Leading
    @ViewBuilder let trailing: () -> Trailing

    var body: some View {
        HStack(alignment: .top, spacing: DroverSpace.x3) {
            leading()
            VStack(alignment: .leading, spacing: DroverSpace.x1) {
                Text(title).font(DroverTypeScale.body).foregroundStyle(DroverTheme.textPrimary)
                Text(detail).font(DroverTypeScale.meta).foregroundStyle(DroverTheme.textSecondary)
            }
            Spacer(minLength: DroverSpace.x2)
            VStack(alignment: .trailing, spacing: DroverSpace.x2) {
                if let status { StatusChip(status: status) }
                trailing()
            }
        }
        .padding(variant == .standard ? DroverSpace.x4 : DroverSpace.x3)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background {
            if variant == .standard {
                RoundedRectangle(cornerRadius: DroverRadius.card, style: .continuous)
                    .fill(DroverTheme.surfaceRaised)
            }
        }
        .overlay {
            if variant == .standard {
                RoundedRectangle(cornerRadius: DroverRadius.card, style: .continuous)
                    .strokeBorder(DroverTheme.border, lineWidth: 1)
            }
        }
    }
}

enum StateViewKind { case empty, loading, error, unavailable }

struct StateView: View {
    let kind: StateViewKind
    let title: String
    let message: String
    var meta: String?
    var actionTitle: String?
    var action: (() -> Void)?

    private var symbol: String {
        switch kind {
        case .empty: "tray"
        case .loading: "arrow.triangle.2.circlepath"
        case .error: "exclamationmark.triangle"
        case .unavailable: "wifi.exclamationmark"
        }
    }

    var body: some View {
        VStack(spacing: DroverSpace.x3) {
            if kind == .loading { ProgressView() }
            Image(systemName: symbol).font(.title2).foregroundStyle(DroverTheme.textSecondary)
            Text(title).font(DroverTypeScale.section).foregroundStyle(DroverTheme.textPrimary)
            Text(message).font(DroverTypeScale.meta).foregroundStyle(DroverTheme.textSecondary)
                .multilineTextAlignment(.center)
            if let meta { Text(meta).font(DroverTypeScale.caption).foregroundStyle(DroverTheme.neutral) }
            if let actionTitle, let action {
                Button(actionTitle, action: action).buttonStyle(.bordered)
            }
        }
        .padding(DroverSpace.x8)
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
    }
}
