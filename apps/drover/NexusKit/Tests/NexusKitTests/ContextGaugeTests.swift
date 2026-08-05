import Foundation
import Testing
@testable import NexusKit

/// The numbers here are copied from a real session
/// (harness-571701ec, 2026-08-05) where the shipped code displayed
/// "ctx 9.1M / 1M (914%)" against a 1M window.
@Suite struct ContextGaugeTests {
    private func assistant(seq: Int, input: Int, cacheRead: Int, cacheCreation: Int) -> HarnessMessage {
        HarnessMessage.fixture(
            seq: seq, type: .assistantOutput,
            payload: ["usage": .object([
                "input_tokens": .number(Double(input)),
                "cache_read_input_tokens": .number(Double(cacheRead)),
                "cache_creation_input_tokens": .number(Double(cacheCreation)),
            ])]
        )
    }

    private func result(seq: Int, cumulativeCacheRead: Int, window: Int) -> HarnessMessage {
        HarnessMessage.fixture(
            seq: seq, type: .status, text: "turn complete",
            payload: ["result": .object([
                "modelUsage": .object([
                    "claude-opus-5[1m]": .object([
                        "inputTokens": .number(211),
                        "cacheReadInputTokens": .number(Double(cumulativeCacheRead)),
                        "cacheCreationInputTokens": .number(120_147),
                        "contextWindow": .number(Double(window)),
                    ])
                ])
            ])]
        )
    }

    @Test func usesTheLatestAssistantCallNotTheLifetimeCounter() {
        // modelUsage says 9,145,279; the real prompt was 158,148.
        let messages = [
            assistant(seq: 733, input: 2, cacheRead: 154_527, cacheCreation: 2_446),
            result(seq: 740, cumulativeCacheRead: 9_024_921, window: 1_000_000),
            assistant(seq: 741, input: 2, cacheRead: 156_973, cacheCreation: 1_173),
        ]
        let gauge = ContextGauge(messages: messages)
        #expect(gauge?.usedTokens == 158_148)
        #expect(gauge?.window == 1_000_000)
        #expect(gauge?.text == "ctx 158.1K / 1M · 16%")
    }

    @Test func dropsWhenTheSessionCompacts() {
        let before = [assistant(seq: 1, input: 16, cacheRead: 1_059_493, cacheCreation: 5_809)]
        let after = before + [assistant(seq: 2, input: 2, cacheRead: 156_973, cacheCreation: 1_173)]
        #expect(ContextGauge(messages: before)?.usedTokens == 1_065_318)
        #expect(ContextGauge(messages: after)?.usedTokens == 158_148)
    }

    @Test func showsOverHundredPercentRatherThanClamping() {
        let messages = [
            assistant(seq: 1, input: 16, cacheRead: 1_059_493, cacheCreation: 5_809),
            result(seq: 2, cumulativeCacheRead: 9_024_921, window: 1_000_000),
        ]
        #expect(ContextGauge(messages: messages)?.text == "ctx 1.1M / 1M · 107%")
    }

    @Test func omitsTheDenominatorUntilAWindowIsKnown() {
        let messages = [assistant(seq: 1, input: 2, cacheRead: 156_973, cacheCreation: 1_173)]
        let gauge = ContextGauge(messages: messages)
        #expect(gauge?.window == nil)
        #expect(gauge?.text == "ctx 158.1K")
    }

    @Test func isNilWhenNoPerCallUsageExists() {
        // Gemini's `stats` shape carries no per-call usage.
        let gemini = HarnessMessage.fixture(
            seq: 1, type: .assistantOutput,
            payload: ["stats": .object(["models": .object([:])])]
        )
        #expect(ContextGauge(messages: [gemini]) == nil)
        #expect(ContextGauge(messages: []) == nil)
    }

    @Test func ignoresResultUsageWhichIsAlsoCumulative() {
        // result.usage summed 7,468,690 at num_turns=100 -- not a gauge.
        let resultWithUsage = HarnessMessage.fixture(
            seq: 1, type: .status, text: "turn complete",
            payload: ["result": .object([
                "usage": .object([
                    "input_tokens": .number(181),
                    "cache_read_input_tokens": .number(7_373_739),
                    "cache_creation_input_tokens": .number(94_770),
                ])
            ])]
        )
        #expect(ContextGauge(messages: [resultWithUsage]) == nil)
    }
}
