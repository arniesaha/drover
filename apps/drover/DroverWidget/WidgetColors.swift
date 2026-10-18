import SwiftUI

/// Shared palette matching Drover app design language for widgets.
/// Monospaced numbers, dark grounds, and accent green.
public enum WidgetColor {
    // Grounds
    public static let bg = PaletteToken(dark: 0x15_16_1A, light: 0xE4_E7_E5)
    public static let surface = PaletteToken(dark: 0x1D_1F_24, light: 0xF5_F7_F6)
    public static let card = PaletteToken(dark: 0x23_26_2C, light: 0xF5_F7_F6)

    // Foregrounds
    public static let text = PaletteToken(dark: 0xE9_E9_EC, light: 0x2A_2B_2E)
    public static let muted = PaletteToken(dark: 0xA4_A6_AD, light: 0x5B_5D_65)
    public static let faint = PaletteToken(dark: 0x8C_8E_96, light: 0x76_78_81)
    public static let line = PaletteToken(dark: 0x2F_32_3A, light: 0xD3_D6_D4)

    // Accents
    public static let accent = PaletteToken(dark: 0x2A_A7_9C, light: 0x11_6E_68)
    public static let accentHi = PaletteToken(dark: 0x4F_C7_BB, light: 0x0E_54_50)
    public static let accentTint = PaletteToken(dark: 0x11_26_28, light: 0xD8_F0_EC)
    public static let warn = PaletteToken(dark: 0xED_B0_5A, light: 0x8A_4B_0A)

    public struct PaletteToken: ShapeStyle, Equatable, Sendable {
        public let dark: UInt32
        public let light: UInt32

        public init(dark: UInt32, light: UInt32) {
            self.dark = dark
            self.light = light
        }

        public func resolve(in environment: EnvironmentValues) -> Color {
            color(for: environment.colorScheme)
        }

        public func rgb(for scheme: ColorScheme) -> UInt32 {
            scheme == .light ? light : dark
        }

        public func color(for scheme: ColorScheme) -> Color {
            Color(rgb: rgb(for: scheme))
        }
    }
}

extension Color {
    public init(rgb: UInt32) {
        let red = Double((rgb >> 16) & 0xFF) / 255.0
        let green = Double((rgb >> 8) & 0xFF) / 255.0
        let blue = Double(rgb & 0xFF) / 255.0
        self.init(red: red, green: green, blue: blue)
    }
}

/// Compact quota utilisation bar for widget rows.
public struct WidgetCapacityBar: View {
    public let fraction: Double?
    public var isCritical: Bool = false
    public var isStale: Bool = false
    public var height: CGFloat = 3.5

    public init(fraction: Double?, isCritical: Bool = false, isStale: Bool = false, height: CGFloat = 3.5) {
        self.fraction = fraction
        self.isCritical = isCritical
        self.isStale = isStale
        self.height = height
    }

    public var body: some View {
        Capsule()
            .fill(WidgetColor.line)
            .frame(height: height)
            .overlay(alignment: .leading) {
                if let fraction, fraction > 0 {
                    GeometryReader { geo in
                        Capsule()
                            .fill(isStale ? AnyShapeStyle(WidgetColor.muted) : AnyShapeStyle(WidgetColor.accent))
                            .frame(width: max(height, geo.size.width * min(1.0, fraction)))
                    }
                }
            }
    }
}
