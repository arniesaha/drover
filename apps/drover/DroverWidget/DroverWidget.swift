import WidgetKit
import SwiftUI

@main
struct DroverWidgetBundle: WidgetBundle {
    var body: some Widget {
        DroverWidget()
    }
}

struct DroverWidget: Widget {
    let kind: String = "com.arnab.drover.widget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: DroverTimelineProvider()) { entry in
            DroverWidgetEntryView(entry: entry)
                .containerBackground(WidgetColor.bg.color(for: .dark), for: .widget)
        }
        .configurationDisplayName("Drover Fleet")
        .description("At-a-glance quota across accounts and active fleet hosts.")
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge, .accessoryRectangular])
        .contentMarginsDisabled()
    }
}

#if DEBUG
#Preview("Wide Medium", as: .systemMedium) {
    DroverWidget()
} timeline: {
    DroverWidgetEntry(date: Date(), snapshot: .preview())
}

#Preview("Square Small", as: .systemSmall) {
    DroverWidget()
} timeline: {
    DroverWidgetEntry(date: Date(), snapshot: .preview())
}

#Preview("Large", as: .systemLarge) {
    DroverWidget()
} timeline: {
    DroverWidgetEntry(date: Date(), snapshot: .preview())
}

#Preview("Accessory Rectangular", as: .accessoryRectangular) {
    DroverWidget()
} timeline: {
    DroverWidgetEntry(date: Date(), snapshot: .preview())
}
#endif
