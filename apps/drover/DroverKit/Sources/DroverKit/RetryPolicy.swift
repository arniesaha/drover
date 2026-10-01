import Foundation

/// Server delays are floors. Only the local backoff and added jitter are
/// capped: truncating a long Retry-After would send traffic before permission.
public enum RetryPolicy {
    public static func retryAfter(_ value: String?, now: Date = Date()) -> TimeInterval? {
        guard let value = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty else { return nil }
        if value.allSatisfy({ $0.isASCII && $0.isNumber }) {
            return min(Double(value) ?? .infinity, Date.distantFuture.timeIntervalSince(now))
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        // Accept the current HTTP date and the two obsolete HTTP formats.
        for format in ["EEE, dd MMM yyyy HH:mm:ss 'GMT'",
                       "EEEE, dd-MMM-yy HH:mm:ss 'GMT'", "EEE MMM d HH:mm:ss yyyy"] {
            formatter.dateFormat = format
            if let date = formatter.date(from: value) {
                return max(0, date.timeIntervalSince(now))
            }
        }
        return nil
    }

    public static func delay(
        retryAfter: TimeInterval? = nil,
        backoff: TimeInterval = 1,
        minimum: TimeInterval = 1,
        jitter: Double = Double.random(in: 0...1)
    ) -> TimeInterval {
        let minimum = minimum.isFinite ? min(1, max(0, minimum)) : 1
        let local = backoff.isFinite ? min(300, max(minimum, backoff)) : 300
        let floor = max(local, retryAfter ?? 0)
        let fraction = jitter.isFinite ? min(1, max(0, jitter)) : 0
        return floor + min(30, floor * 0.2) * fraction
    }

    public static func busyMessage(until deadline: Date, now: Date = Date()) -> String {
        let seconds = min(Double(Int.max / 2), max(0, ceil(deadline.timeIntervalSince(now))))
        return "Hub busy, retrying in \(Int(seconds))s"
    }

    /// Long server delays are waited in bounded, cancellable chunks.
    public static func wait(until deadline: Date) async throws {
        while deadline > Date() {
            try Task.checkCancellation()
            try await Task.sleep(for: .seconds(min(300, deadline.timeIntervalSinceNow)))
        }
        try Task.checkCancellation()
    }
}

/// A hub-wide cooldown shared by foreground and background clients. The
/// persisted deadline survives an OS relaunch; no credential is persisted.
public actor HubRetryGate {
    public static let shared = HubRetryGate(defaults: .standard)
    private let defaults: UserDefaults?
    private var deadlines: [String: Date] = [:]
    private var failures: [String: Int] = [:]

    public init(defaults: UserDefaults? = nil) {
        self.defaults = defaults
    }

    private func key(_ url: URL) -> String { "drover.retryAfter.\(url.absoluteString)" }

    public func deadline(for url: URL, now: Date = Date()) -> Date? {
        let stored = defaults?.object(forKey: key(url)) as? Date
        let deadline = max(deadlines[key(url)] ?? .distantPast, stored ?? .distantPast)
        return deadline > now ? deadline : nil
    }

    public func record(
        for url: URL, header: String?, now: Date = Date(),
        jitter: Double = Double.random(in: 0...1)
    ) -> Date {
        let key = key(url)
        let attempt = min(9, failures[key] ?? 0)
        failures[key] = attempt + 1
        let delay = RetryPolicy.delay(
            retryAfter: RetryPolicy.retryAfter(header, now: now),
            backoff: pow(2, Double(attempt)), jitter: jitter
        )
        let proposed = now.addingTimeInterval(delay)
        let deadline = max(proposed, self.deadline(for: url, now: now) ?? .distantPast)
        deadlines[key] = deadline
        defaults?.set(deadline, forKey: key)
        return deadline
    }

    public func deferReads(for url: URL, until proposed: Date) {
        let deadline = max(proposed, self.deadline(for: url) ?? .distantPast)
        deadlines[key(url)] = deadline
        defaults?.set(deadline, forKey: key(url))
    }

    public func succeeded(for url: URL) {
        // An older in-flight success cannot shorten a newer busy response.
        guard deadline(for: url) == nil else { return }
        failures[key(url)] = nil
        deadlines[key(url)] = nil
        defaults?.removeObject(forKey: key(url))
    }
}
