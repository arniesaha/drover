import Foundation

public struct TokenUsageSummary: Sendable, Equatable {
    public let inputTokens: Int?
    public let outputTokens: Int?
    public let cachedInputTokens: Int?
    public let reasoningOutputTokens: Int?
    public let contextTokens: Int?
    public let contextWindow: Int?

    public var compactText: String {
        var parts: [String] = []
        if let inputTokens, inputTokens > 0 {
            parts.append("in \(TokenCount.format(inputTokens))")
        }
        if let outputTokens, outputTokens > 0 {
            parts.append("out \(TokenCount.format(outputTokens))")
        }
        if let cachedInputTokens, cachedInputTokens > 0 {
            parts.append("cache \(TokenCount.format(cachedInputTokens))")
        }
        if let reasoningOutputTokens, reasoningOutputTokens > 0 {
            parts.append("reason \(TokenCount.format(reasoningOutputTokens))")
        }
        return parts.joined(separator: " | ")
    }

    public var contextText: String? {
        guard let contextTokens, let contextWindow, contextTokens > 0, contextWindow > 0 else {
            return nil
        }
        let percent = Int((Double(contextTokens) / Double(contextWindow) * 100).rounded())
        return "ctx \(TokenCount.format(contextTokens)) / \(TokenCount.format(contextWindow)) (\(percent)%)"
    }

    public init?(message: HarnessMessage) {
        guard let parsed = Self.parse(message.payload) else { return nil }
        inputTokens = parsed.input
        outputTokens = parsed.output
        cachedInputTokens = parsed.cached
        reasoningOutputTokens = parsed.reasoning
        contextTokens = parsed.context
        contextWindow = parsed.window
        guard !compactText.isEmpty || contextText != nil else { return nil }
    }

    private typealias Parsed = (
        input: Int?,
        output: Int?,
        cached: Int?,
        reasoning: Int?,
        context: Int?,
        window: Int?
    )

    private static func parse(_ payload: [String: JSONValue]) -> Parsed? {
        if let stats = payload["stats"]?.objectValue,
           let parsed = parseGeminiStats(stats) {
            return parsed
        }

        let result = payload["result"]?.objectValue
        let usage = payload["usage"]?.objectValue ?? result?["usage"]?.objectValue
        let modelUsage = payload["modelUsage"]?.objectValue ?? result?["modelUsage"]?.objectValue

        guard usage != nil || modelUsage != nil else { return nil }

        let input = usage?["input_tokens"]?.intValue
            ?? usage?["inputTokens"]?.intValue
            ?? usage?["input"]?.intValue
        let output = usage?["output_tokens"]?.intValue
            ?? usage?["outputTokens"]?.intValue
            ?? usage?["candidates"]?.intValue
        let cacheRead = usage?["cache_read_input_tokens"]?.intValue
            ?? usage?["cacheReadInputTokens"]?.intValue
            ?? 0
        let cacheCreation = usage?["cache_creation_input_tokens"]?.intValue
            ?? usage?["cacheCreationInputTokens"]?.intValue
            ?? 0
        let cached = usage?["cached_input_tokens"]?.intValue
            ?? usage?["cached"]?.intValue
            ?? sum(cacheRead, cacheCreation)
        let reasoning = usage?["reasoning_output_tokens"]?.intValue
            ?? usage?["reasoningOutputTokens"]?.intValue
            ?? usage?["thoughts"]?.intValue

        let modelTotals = parseModelUsage(modelUsage)
        let context = modelTotals.context ?? sum(input, cached)
        return (
            input,
            output,
            cached,
            reasoning,
            context,
            modelTotals.window
        )
    }

    private static func parseGeminiStats(_ stats: [String: JSONValue]) -> Parsed? {
        guard let models = stats["models"]?.objectValue else { return nil }
        var input = 0
        var output = 0
        var cached = 0
        var reasoning = 0
        for model in models.values {
            guard let tokens = model.objectValue?["tokens"]?.objectValue else { continue }
            input += tokens["input"]?.intValue ?? 0
            output += tokens["candidates"]?.intValue ?? tokens["output"]?.intValue ?? 0
            cached += tokens["cached"]?.intValue ?? 0
            reasoning += tokens["thoughts"]?.intValue ?? 0
        }
        guard input > 0 || output > 0 || cached > 0 || reasoning > 0 else { return nil }
        return (
            input > 0 ? input : nil,
            output > 0 ? output : nil,
            cached > 0 ? cached : nil,
            reasoning > 0 ? reasoning : nil,
            nil,
            nil
        )
    }

    private static func parseModelUsage(_ modelUsage: [String: JSONValue]?) -> (context: Int?, window: Int?) {
        guard let modelUsage else { return (nil, nil) }
        var context = 0
        var window: Int?
        for entry in modelUsage.values {
            guard let value = entry.objectValue else { continue }
            let input = value["inputTokens"]?.intValue ?? 0
            let cacheRead = value["cacheReadInputTokens"]?.intValue ?? 0
            let cacheCreation = value["cacheCreationInputTokens"]?.intValue ?? 0
            context += input + cacheRead + cacheCreation
            if let entryWindow = value["contextWindow"]?.intValue {
                window = max(window ?? 0, entryWindow)
            }
        }
        return (context > 0 ? context : nil, window)
    }

    private static func sum(_ values: Int?...) -> Int? {
        let total = values.compactMap(\.self).reduce(0, +)
        return total > 0 ? total : nil
    }

}

private extension JSONValue {
    var intValue: Int? {
        numberValue.map { Int($0.rounded()) }
    }
}
