import DroverKit
import SwiftUI

/// Quota and host diagnostics have their own scroll area, never the inbox's.
struct ProviderAccountsView: View {
    let accounts: [ProviderAccount]
    let status: DataStatus
    let statusMessage: String?
    var hostTitles: [String: String] = [:]
    var onRefresh: (() async -> Void)? = nil

    var body: some View {
        let subscriptions = ProviderSubscriptionGrouping.group(accounts, hostTitles: hostTitles)
        let providers = Array(Set(subscriptions.map(\.provider))).sorted()
        let section = ProviderSectionPresentation(status: status, message: statusMessage,
                                                   hasRetainedValues: !accounts.isEmpty)
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 12) {
                Text("Provider-reported capacity. Host icons show usage reporting, not sign-in status.")
                    .droverText(.subtitle)
                    .fixedSize(horizontal: false, vertical: true)
                if let warning = section.warningText {
                    Label(warning, systemImage: "exclamationmark.triangle")
                        .droverText(.nested)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if subscriptions.isEmpty {
                    ContentUnavailableView("No reported accounts", systemImage: "person.crop.circle",
                        description: Text("Refresh to check for provider usage readings from your hosts."))
                }
                ForEach(providers, id: \.self) { provider in
                    Text(provider == "openai" ? "OpenAI" : provider.capitalized).droverText(.h3)
                        .padding(.top, 6)
                        .accessibilityIdentifier("provider-heading-\(provider)")
                    ForEach(subscriptions.filter { $0.provider == provider }) { subscription in
                        ProviderAccountCard(subscription: subscription, section: section)
                    }
                }
            }
            .padding(14)
        }
        .accessibilityIdentifier("provider-accounts-scroll")
        .background(DroverColor.bg)
        .navigationTitle("Accounts")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar(.visible, for: .navigationBar)
        .refreshable { await onRefresh?() }
    }
}
