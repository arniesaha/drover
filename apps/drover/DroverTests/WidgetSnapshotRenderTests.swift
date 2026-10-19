import Testing
import SwiftUI
import DroverKit

@Suite(.serialized)
struct WidgetSnapshotRenderTests {
    private let outputDirectory = URL(fileURLWithPath: "/tmp/drover-widget-snapshots")

    @MainActor
    private func render<V: View>(view: V, size: CGSize, fileName: String) throws {
        try? FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
        let fileURL = outputDirectory.appendingPathComponent(fileName)

        let renderer = ImageRenderer(content: view.frame(width: size.width, height: size.height))
        renderer.scale = 3.0
        guard let uiImage = renderer.uiImage, let data = uiImage.pngData() else {
            Issue.record("Failed to render \(fileName)")
            return
        }
        try data.write(to: fileURL)
        #expect(FileManager.default.fileExists(atPath: fileURL.path))
        #expect(data.count > 1000)

        // Mirror to the artifact directory for presentation
        let artifactDir = URL(fileURLWithPath: "/Users/arnab/.gemini/antigravity-cli/brain/54106faf-d76d-4f5a-888a-23f33e36eb8b")
        if FileManager.default.fileExists(atPath: artifactDir.path) {
            let artifactTarget = artifactDir.appendingPathComponent(fileName)
            try? FileManager.default.removeItem(at: artifactTarget)
            try? FileManager.default.copyItem(at: fileURL, to: artifactTarget)
        }
    }

    @Test @MainActor func renderMediumDark() throws {
        let entry = DroverWidgetEntry(date: Date(), snapshot: .preview())
        let view = MediumWidgetView(entry: entry)
            .environment(\.colorScheme, .dark)
            .background(WidgetColor.bg.color(for: .dark))
            .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
        try render(view: view, size: CGSize(width: 360, height: 169), fileName: "widget-medium-dark.png")
    }

    @Test @MainActor func renderMediumLight() throws {
        let entry = DroverWidgetEntry(date: Date(), snapshot: .preview())
        let view = MediumWidgetView(entry: entry)
            .environment(\.colorScheme, .light)
            .background(WidgetColor.bg.color(for: .light))
            .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
        try render(view: view, size: CGSize(width: 360, height: 169), fileName: "widget-medium-light.png")
    }

    @Test @MainActor func renderSmallDark() throws {
        let entry = DroverWidgetEntry(date: Date(), snapshot: .preview())
        let view = SmallWidgetView(entry: entry)
            .environment(\.colorScheme, .dark)
            .background(WidgetColor.bg.color(for: .dark))
            .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
        try render(view: view, size: CGSize(width: 169, height: 169), fileName: "widget-small-dark.png")
    }

    @Test @MainActor func renderSmallLight() throws {
        let entry = DroverWidgetEntry(date: Date(), snapshot: .preview())
        let view = SmallWidgetView(entry: entry)
            .environment(\.colorScheme, .light)
            .background(WidgetColor.bg.color(for: .light))
            .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
        try render(view: view, size: CGSize(width: 169, height: 169), fileName: "widget-small-light.png")
    }

    @Test @MainActor func renderLargeDark() throws {
        let entry = DroverWidgetEntry(date: Date(), snapshot: .preview())
        let view = LargeWidgetView(entry: entry)
            .environment(\.colorScheme, .dark)
            .background(WidgetColor.bg.color(for: .dark))
            .clipShape(RoundedRectangle(cornerRadius: 26, style: .continuous))
        try render(view: view, size: CGSize(width: 360, height: 379), fileName: "widget-large-dark.png")
    }

    @Test @MainActor func renderLargeLight() throws {
        let entry = DroverWidgetEntry(date: Date(), snapshot: .preview())
        let view = LargeWidgetView(entry: entry)
            .environment(\.colorScheme, .light)
            .background(WidgetColor.bg.color(for: .light))
            .clipShape(RoundedRectangle(cornerRadius: 26, style: .continuous))
        try render(view: view, size: CGSize(width: 360, height: 379), fileName: "widget-large-light.png")
    }

    @Test @MainActor func renderAccessoryRectangular() throws {
        let entry = DroverWidgetEntry(date: Date(), snapshot: .preview())
        let view = AccessoryRectangularWidgetView(entry: entry)
            .environment(\.colorScheme, .dark)
        try render(view: view, size: CGSize(width: 330, height: 75), fileName: "widget-accessory-dark.png")
    }
}
