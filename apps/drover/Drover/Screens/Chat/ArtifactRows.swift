import SwiftUI
import DroverKit
import UIKit

/// What this session produced, as rows you can act on.
///
/// Machine strings never wrap: a branch name gets middle truncation so both
/// the prefix that identifies the work and the suffix that distinguishes it
/// survive, and one verb sits beside it. They are pinned under the transcript
/// rather than left inline — on a phone the branch you want is otherwise
/// somewhere up a scroll you have to hunt for.
struct ArtifactRows: View {
    let artifacts: [SessionArtifact]
    @Environment(\.openURL) private var openURL

    var body: some View {
        VStack(spacing: 0) {
            ForEach(Array(artifacts.enumerated()), id: \.element.id) { index, artifact in
                if index > 0 { Divider().overlay(DroverColor.line) }
                row(artifact)
            }
        }
        .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(DroverColor.line, lineWidth: 1)
        }
        .padding(.horizontal, 14)
        .padding(.bottom, 8)
    }

    private func row(_ artifact: SessionArtifact) -> some View {
        HStack(spacing: 10) {
            Image(systemName: artifact.kind == .branch ? "arrow.triangle.branch" : "arrow.triangle.pull")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(DroverColor.accentHi)
                .frame(width: 16)

            VStack(alignment: .leading, spacing: 1) {
                Text(artifact.kind.rawValue)
                    .droverText(.h3)
                Text(artifact.value)
                    .droverText(.mono)
                    .foregroundStyle(DroverColor.text)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            Spacer(minLength: 8)

            Button {
                if let url = artifact.url {
                    openURL(url)
                } else {
                    UIPasteboard.general.string = artifact.value
                }
            } label: {
                Text(artifact.action)
                    .font(.system(.caption, design: .default, weight: .medium))
                    .foregroundStyle(DroverColor.accentHi)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    .overlay { Capsule().strokeBorder(DroverColor.accent, lineWidth: 1) }
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("artifact-action")
            .accessibilityLabel("\(artifact.action) \(artifact.kind.rawValue)")
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 9)
    }
}
