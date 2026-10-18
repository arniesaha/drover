import Foundation

/// Read-only fictional datasets for the offline App Store review demo mode.
/// Purely fictional hosts, projects, repositories, and quotas. No private
/// hostnames, IP addresses, credentials, or personal emails.
public enum DemoHubData: Sendable {
    public static let syntheticHost = "demo.drover.invalid"
    public static let syntheticURLString = "https://demo.drover.invalid"
    public static let syntheticToken = "demo-offline-token-no-secret"

    public static let hostStudio = "studio"
    public static let hostWorkstation = "workstation"
    public static let hostBuildBox = "build-box"

    public static let sessionStreaming = "demo-session-streaming"
    public static let sessionApproval = "demo-session-approval"
    public static let sessionDiffArtifacts = "demo-session-diff-artifacts"
    public static let sessionReply = "demo-session-reply"
    public static let sessionInterrupt = "demo-session-interrupt"
    public static let sessionRunning2 = "demo-session-running-2"
    public static let sessionFinished1 = "demo-session-finished-1"
    public static let sessionFinished2 = "demo-session-finished-2"
    public static let sessionFailed = "demo-session-failed"
    public static let sessionInput2 = "demo-session-input-2"
    public static let sessionLaunched = "demo-session-launched"

    public static let approvalRequestID = "demo-req-migration-004"
    public static let defaultHarness = "claude-code"

    // MARK: - Harness Capability Rows

    private static var claudeCodeRow: [String: Any] {
        [
            "name": "claude-code",
            "enabled": true,
            "description": "Claude Code CLI",
            "command": [String](),
            "capabilities": [
                "schema_version": 1,
                "harness_id": "claude-code",
                "launch_modes": ["structured"],
                "approvals": true,
                "interrupt": true,
                "native_resume": true,
                "model_catalog": true,
                "usage": true,
                "worktree": true,
                "interactive_auth": false,
                "turn_preferences": true,
                "attachments": ["image/jpeg", "image/png", "image/gif", "image/webp"],
            ] as [String: Any],
        ]
    }

    private static var codexRow: [String: Any] {
        [
            "name": "codex",
            "enabled": true,
            "description": "Codex CLI",
            "command": [String](),
            "capabilities": [
                "schema_version": 1,
                "harness_id": "codex",
                "launch_modes": ["structured"],
                "approvals": true,
                "interrupt": true,
                "native_resume": true,
                "model_catalog": true,
                "usage": true,
                "worktree": true,
                "interactive_auth": false,
                "turn_preferences": true,
                "attachments": ["image/jpeg", "image/png", "image/gif", "image/webp"],
            ] as [String: Any],
        ]
    }

    // MARK: - Hosts

    public static var defaultHosts: [[String: Any]] {
        [
            [
                "host_id": hostStudio,
                "status": "online",
                "connection_kind": "direct",
                "last_seen_at": ISO8601DateFormatter().string(from: Date().addingTimeInterval(-30)),
                "capabilities": [
                    "display_name": "Mac Studio",
                    "harnesses": [claudeCodeRow, codexRow],
                ] as [String: Any],
            ],
            [
                "host_id": hostWorkstation,
                "status": "online",
                "connection_kind": "direct",
                "last_seen_at": ISO8601DateFormatter().string(from: Date().addingTimeInterval(-60)),
                "capabilities": [
                    "display_name": "Linux Workstation",
                    "harnesses": [codexRow, claudeCodeRow],
                ] as [String: Any],
            ],
            [
                "host_id": hostBuildBox,
                "status": "online",
                "connection_kind": "direct",
                "last_seen_at": ISO8601DateFormatter().string(from: Date().addingTimeInterval(-120)),
                "capabilities": [
                    "display_name": "CI Build Box",
                    "harnesses": [codexRow, claudeCodeRow],
                ] as [String: Any],
            ],
        ]
    }

    // MARK: - Sessions

    public static var defaultSessions: [[String: Any]] {
        let now = Date()
        func iso(_ offsetSeconds: TimeInterval) -> String {
            ISO8601DateFormatter().string(from: now.addingTimeInterval(offsetSeconds))
        }

        return [
            [
                "session_id": sessionStreaming,
                "host_id": hostStudio,
                "host_display_name": "Mac Studio",
                "harness": "claude-code",
                "mode": "structured",
                "status": "working",
                "awaiting": NSNull(),
                "cwd": "/projects/acme-web-app",
                "repo_name": "acme/web-app",
                "preview": "Streaming checkout flow refactor",
                "last_activity": iso(-15),
                "started_at": iso(-600),
                "model": "claude-3-7-sonnet",
            ],
            [
                "session_id": sessionApproval,
                "host_id": hostWorkstation,
                "host_display_name": "Linux Workstation",
                "harness": "codex",
                "mode": "structured",
                "status": "working",
                "awaiting": "approval",
                "cwd": "/projects/acme-api",
                "repo_name": "acme/api",
                "preview": "Database migration for user sessions table",
                "last_activity": iso(-45),
                "started_at": iso(-900),
                "model": "gpt-4o",
            ],
            [
                "session_id": sessionDiffArtifacts,
                "host_id": hostStudio,
                "host_display_name": "Mac Studio",
                "harness": "claude-code",
                "mode": "structured",
                "status": "working",
                "awaiting": NSNull(),
                "cwd": "/projects/acme-web-app",
                "repo_name": "acme/web-app",
                "preview": "Refactor navigation header & responsive tabs",
                "last_activity": iso(-120),
                "started_at": iso(-1800),
                "model": "claude-3-7-sonnet",
            ],
            [
                "session_id": sessionReply,
                "host_id": hostBuildBox,
                "host_display_name": "CI Build Box",
                "harness": "codex",
                "mode": "structured",
                "status": "working",
                "awaiting": "input",
                "cwd": "/projects/demo-cli-tool",
                "repo_name": "demo/cli-tool",
                "preview": "Add JSON output format to CLI inspect command",
                "last_activity": iso(-240),
                "started_at": iso(-2400),
                "model": "gpt-4o",
            ],
            [
                "session_id": sessionInterrupt,
                "host_id": hostWorkstation,
                "host_display_name": "Linux Workstation",
                "harness": "claude-code",
                "mode": "structured",
                "status": "working",
                "awaiting": NSNull(),
                "cwd": "/projects/acme-api",
                "repo_name": "acme/api",
                "preview": "Running test suite and stress tests",
                "last_activity": iso(-360),
                "started_at": iso(-3000),
                "model": "claude-3-7-sonnet",
            ],
            [
                "session_id": sessionRunning2,
                "host_id": hostStudio,
                "host_display_name": "Mac Studio",
                "harness": "codex",
                "mode": "structured",
                "status": "working",
                "awaiting": NSNull(),
                "cwd": "/projects/acme-web-app",
                "repo_name": "acme/web-app",
                "preview": "Optimizing Webpack bundle chunk splitting",
                "last_activity": iso(-500),
                "started_at": iso(-3600),
                "model": "gpt-4o",
            ],
            [
                "session_id": sessionFinished1,
                "host_id": hostStudio,
                "host_display_name": "Mac Studio",
                "harness": "claude-code",
                "mode": "structured",
                "status": "completed",
                "awaiting": NSNull(),
                "cwd": "/projects/demo-cli-tool",
                "repo_name": "demo/cli-tool",
                "preview": "Implement release checklist verification",
                "last_activity": iso(-3600),
                "started_at": iso(-7200),
                "ended_at": iso(-3600),
                "model": "claude-3-5-haiku",
            ],
            [
                "session_id": sessionFinished2,
                "host_id": hostBuildBox,
                "host_display_name": "CI Build Box",
                "harness": "codex",
                "mode": "structured",
                "status": "completed",
                "awaiting": NSNull(),
                "cwd": "/projects/acme-api",
                "repo_name": "acme/api",
                "preview": "Fix token expiration edge case in auth middleware",
                "last_activity": iso(-7200),
                "started_at": iso(-10800),
                "ended_at": iso(-7200),
                "model": "gpt-4o",
            ],
            [
                "session_id": sessionFailed,
                "host_id": hostBuildBox,
                "host_display_name": "CI Build Box",
                "harness": "claude-code",
                "mode": "structured",
                "status": "errored",
                "awaiting": NSNull(),
                "cwd": "/projects/acme-web-app",
                "repo_name": "acme/web-app",
                "preview": "End-to-end Cypress build failed on port conflict",
                "last_activity": iso(-10800),
                "started_at": iso(-14400),
                "ended_at": iso(-10800),
                "model": "claude-3-7-sonnet",
            ],
            [
                "session_id": sessionInput2,
                "host_id": hostWorkstation,
                "host_display_name": "Linux Workstation",
                "harness": "codex",
                "mode": "structured",
                "status": "working",
                "awaiting": "input",
                "cwd": "/projects/acme-mobile",
                "repo_name": "acme/mobile",
                "preview": "iOS layout audit: accessibility dynamic type sizes",
                "last_activity": iso(-14400),
                "started_at": iso(-18000),
                "model": "gpt-4o",
            ],
        ]
    }

    // MARK: - CWD Suggestions

    public static var defaultCwdSuggestions: [[String: Any]] {
        [
            [
                "path": "/projects/acme-web-app",
                "source": "recent session",
                "host_id": hostStudio,
            ],
            [
                "path": "/projects/acme-api",
                "source": "recent session",
                "host_id": hostWorkstation,
            ],
            [
                "path": "/projects/demo-cli-tool",
                "source": "favorite",
                "host_id": hostBuildBox,
            ],
            [
                "path": "/projects/acme-mobile",
                "source": "recent session",
                "host_id": hostWorkstation,
            ],
        ]
    }

    // MARK: - Cockpit Overview & Quota Card

    public static var providerAccounts: [[String: Any]] {
        let reported = ISO8601DateFormatter().string(from: Date().addingTimeInterval(-60))
        return [
            [
                "snapshot_id": "account-anthropic-team",
                "dedup_key": "anthropic-team",
                "provider": "anthropic",
                "account_label": "demo.user@example.com",
                "plan_label": "Team",
                "host_id": hostStudio,
                "status": "ok",
                "observed_at": reported,
                "source": "demo-quota",
                "error_category": NSNull(),
                "windows": [
                    ["kind": "seven_day", "used_percent": 42],
                    ["kind": "five_hour", "used_percent": 18],
                ],
            ],
            [
                "snapshot_id": "account-openai-enterprise",
                "dedup_key": "openai-enterprise",
                "provider": "openai",
                "account_label": "team@example.com",
                "plan_label": "Enterprise",
                "host_id": hostWorkstation,
                "status": "ok",
                "observed_at": reported,
                "source": "demo-quota",
                "error_category": NSNull(),
                "windows": [
                    ["kind": "seven_day", "used_percent": 28],
                    ["kind": "five_hour", "used_percent": 8],
                ],
            ],
            [
                "snapshot_id": "account-google-pro",
                "dedup_key": "google-pro",
                "provider": "google",
                "account_label": "demo-dev@example.org",
                "plan_label": "Pro",
                "host_id": hostBuildBox,
                "status": "ok",
                "observed_at": reported,
                "source": "demo-quota",
                "error_category": NSNull(),
                "windows": [
                    ["kind": "seven_day", "used_percent": 15],
                    ["kind": "five_hour", "used_percent": 5],
                ],
            ],
        ]
    }

    public static func cockpitOverview(sessions: [[String: Any]]) -> [String: Any] {
        var base = analytics(days: 7, metric: "tokens")
        base["provider_capacity"] = [
            "status": "ok",
            "data": providerAccounts,
        ]
        base["insight_counts"] = [
            "critical": 0,
            "high": 1,
            "medium": 0,
            "low": 0,
        ]
        base["cockpit_api_version"] = 1
        base["cockpit_sections"] = ["provider_capacity", "activity", "insights"]
        return base
    }

    // MARK: - Analytics (7d / 30d)

    public static func analytics(days: Int = 7, metric: String? = nil) -> [String: Any] {
        let activeMetric = metric ?? "tokens"
        let scale = days > 14 ? 4 : 1
        let reported = ISO8601DateFormatter().string(from: Date().addingTimeInterval(-60))

        func m(_ sessions: Int, _ tokens: Int, cost: Double = 0) -> [String: Any] {
            [
                "session_count": sessions * scale,
                "total_tokens": tokens * scale,
                "cost_usd": cost * Double(scale),
                "cache_read_tokens": (tokens / 3) * scale,
                "cache_write_tokens": (tokens / 6) * scale,
                "total_latency_ms": 1500 * sessions * scale,
            ]
        }

        func breakdown(_ key: String, _ sessions: Int, _ tokens: Int, cost: Double = 0, isProject: Bool = false) -> [String: Any] {
            var row = m(sessions, tokens, cost: cost)
            row[isProject ? "project_key" : "key"] = key
            if isProject {
                row["harnesses"] = ["claude-code", "codex"]
                row["hosts"] = [hostStudio, hostWorkstation]
                row["harness_attributed_session_count"] = sessions * scale
                row["host_attributed_session_count"] = sessions * scale
                row["token_attributed_session_count"] = sessions * scale
            }
            return row
        }

        let coverage = ["token_percent": 91, "cost_percent": 74]

        return [
            "cockpit_api_version": 1,
            "filters": ["days": days, "metric": activeMetric],
            "provider_capacity": [
                "status": "ok",
                "data": providerAccounts,
            ],
            "activity": [
                "status": "ok",
                "observed_at": reported,
                "coverage": coverage,
                "data": [
                    "totals": m(142, 4_820_000, cost: 24.50),
                    "projects": [
                        breakdown("acme/web-app", 72, 2_650_000, cost: 13.80, isProject: true),
                        breakdown("acme/api", 45, 1_420_000, cost: 7.20, isProject: true),
                        breakdown("demo/cli-tool", 18, 580_000, cost: 2.80, isProject: true),
                        breakdown("acme/mobile", 7, 170, cost: 0.70, isProject: true),
                    ],
                    "harnesses": [
                        breakdown("claude-code", 86, 3_100_000, cost: 16.10),
                        breakdown("codex", 56, 1_720_000, cost: 8.40),
                    ],
                    "hosts": [
                        breakdown(hostStudio, 82, 2_890_000, cost: 14.90),
                        breakdown(hostWorkstation, 42, 1_350_000, cost: 6.80),
                        breakdown(hostBuildBox, 18, 580_000, cost: 2.80),
                    ],
                    "models": [
                        breakdown("claude-3-7-sonnet", 74, 2_750_000, cost: 14.50),
                        breakdown("gpt-4o", 48, 1_490_000, cost: 7.40),
                        breakdown("claude-3-5-haiku", 20, 580_000, cost: 2.60),
                    ],
                    "project_metric": activeMetric,
                    "coverage": coverage,
                ] as [String: Any],
            ] as [String: Any],
        ]
    }

    // MARK: - Insights

    public static var defaultInsights: [String: Any] {
        [
            "findings": [
                [
                    "finding_id": "demo-finding-01",
                    "analyzer_id": "dependency-drift-analyzer",
                    "rule_id": "stale-lockfile",
                    "target_type": "repository",
                    "target_id": "acme/web-app",
                    "analyzer_class": "deterministic",
                    "severity": "high",
                    "confidence": "confirmed",
                    "title": "Outdated dependency lockfile in acme/web-app",
                    "impact": "Dependencies in package-lock.json diverge from package.json specifications.",
                    "remediation": [
                        "Run `npm install --package-lock-only` in the repository root.",
                        "Verify build reproducibility with CI before merging.",
                    ],
                    "state": "open",
                    "dismissal_reason": NSNull(),
                    "first_seen_at": "2026-10-17T12:00:00Z",
                    "last_seen_at": "2026-10-17T22:00:00Z",
                    "resolved_at": NSNull(),
                    "dismissed_at": NSNull(),
                    "regressed_at": NSNull(),
                ] as [String: Any]
            ],
            "next_cursor": NSNull(),
        ]
    }

    // MARK: - History & Facets

    public static var defaultHistoryItems: [HistoryItem] {
        let hosts = [
            HistoryHost(id: hostStudio, name: "Mac Studio"),
            HistoryHost(id: hostWorkstation, name: "Linux Workstation"),
            HistoryHost(id: hostBuildBox, name: "CI Build Box"),
        ]
        let now = Date()

        return [
            HistoryItem(
                id: sessionStreaming,
                title: "Streaming checkout flow refactor",
                harness: "claude-code",
                model: "claude-3-7-sonnet",
                host: hosts[0],
                repo: "acme/web-app",
                branch: "feat/checkout-flow",
                state: .running,
                status: "working",
                startedAt: now.addingTimeInterval(-600),
                endedAt: nil,
                lastActivity: now.addingTimeInterval(-15),
                summary: "Implementing Apple Pay and form validation in checkout modal.",
                hasTranscript: true,
                tokens: HistoryTokens(input: 12400, output: 4200, cacheRead: 8500, cacheWrite: 2100)
            ),
            HistoryItem(
                id: sessionApproval,
                title: "Database migration for user sessions table",
                harness: "codex",
                model: "gpt-4o",
                host: hosts[1],
                repo: "acme/api",
                branch: "db/user-sessions-table",
                state: .awaiting,
                status: "working",
                startedAt: now.addingTimeInterval(-900),
                endedAt: nil,
                lastActivity: now.addingTimeInterval(-45),
                summary: "Executing psql migration 004_sessions.sql pending operator approval.",
                hasTranscript: true,
                tokens: HistoryTokens(input: 8900, output: 1450, cacheRead: 4200, cacheWrite: 900)
            ),
            HistoryItem(
                id: sessionDiffArtifacts,
                title: "Refactor navigation header & responsive tabs",
                harness: "claude-code",
                model: "claude-3-7-sonnet",
                host: hosts[0],
                repo: "acme/web-app",
                branch: "feat/responsive-nav",
                state: .running,
                status: "working",
                startedAt: now.addingTimeInterval(-1800),
                endedAt: nil,
                lastActivity: now.addingTimeInterval(-120),
                summary: "Updated NavigationBar components, pushed branch, opened PR #142.",
                hasTranscript: true,
                tokens: HistoryTokens(input: 15200, output: 3800, cacheRead: 9100, cacheWrite: 1800)
            ),
            HistoryItem(
                id: sessionReply,
                title: "Add JSON output format to CLI inspect command",
                harness: "codex",
                model: "gpt-4o",
                host: hosts[2],
                repo: "demo/cli-tool",
                branch: "cli/json-output",
                state: .awaiting,
                status: "working",
                startedAt: now.addingTimeInterval(-2400),
                endedAt: nil,
                lastActivity: now.addingTimeInterval(-240),
                summary: "Added --json serializer to inspect command. Awaiting question reply.",
                hasTranscript: true,
                tokens: HistoryTokens(input: 6400, output: 2100, cacheRead: 3100, cacheWrite: 600)
            ),
            HistoryItem(
                id: sessionInterrupt,
                title: "Running test suite and stress tests",
                harness: "claude-code",
                model: "claude-3-7-sonnet",
                host: hosts[1],
                repo: "acme/api",
                branch: "test/load-suite",
                state: .running,
                status: "working",
                startedAt: now.addingTimeInterval(-3000),
                endedAt: nil,
                lastActivity: now.addingTimeInterval(-360),
                summary: "Running full integration suite against mock cluster.",
                hasTranscript: true,
                tokens: HistoryTokens(input: 18400, output: 5200, cacheRead: 11000, cacheWrite: 2400)
            ),
            HistoryItem(
                id: sessionFinished1,
                title: "Implement release checklist verification",
                harness: "claude-code",
                model: "claude-3-5-haiku",
                host: hosts[0],
                repo: "demo/cli-tool",
                branch: "main",
                state: .finished,
                status: "completed",
                startedAt: now.addingTimeInterval(-7200),
                endedAt: now.addingTimeInterval(-3600),
                lastActivity: now.addingTimeInterval(-3600),
                summary: "Checked release artifacts, signatures, and changelog notes.",
                hasTranscript: true,
                tokens: HistoryTokens(input: 9800, output: 2600, cacheRead: 6000, cacheWrite: 1100)
            ),
            HistoryItem(
                id: sessionFinished2,
                title: "Fix token expiration edge case in auth middleware",
                harness: "codex",
                model: "gpt-4o",
                host: hosts[2],
                repo: "acme/api",
                branch: "fix/token-exp",
                state: .finished,
                status: "completed",
                startedAt: now.addingTimeInterval(-10800),
                endedAt: now.addingTimeInterval(-7200),
                lastActivity: now.addingTimeInterval(-7200),
                summary: "Corrected leeway clock calculation for JWT header verification.",
                hasTranscript: true,
                tokens: HistoryTokens(input: 11200, output: 1900, cacheRead: 7500, cacheWrite: 1400)
            ),
            HistoryItem(
                id: sessionFailed,
                title: "End-to-end Cypress build failed on port conflict",
                harness: "claude-code",
                model: "claude-3-7-sonnet",
                host: hosts[2],
                repo: "acme/web-app",
                branch: "ci/cypress-e2e",
                state: .failed,
                status: "errored",
                startedAt: now.addingTimeInterval(-14400),
                endedAt: now.addingTimeInterval(-10800),
                lastActivity: now.addingTimeInterval(-10800),
                summary: "E2E runner could not bind port 3000 due to leftover server process.",
                hasTranscript: true,
                tokens: HistoryTokens(input: 14100, output: 3100, cacheRead: 8200, cacheWrite: 1500)
            ),
        ]
    }

    public static var defaultFacets: [String: Any] {
        [
            "hosts": [
                ["id": hostStudio, "name": "Mac Studio", "retired": false],
                ["id": hostWorkstation, "name": "Linux Workstation", "retired": false],
                ["id": hostBuildBox, "name": "CI Build Box", "retired": false],
            ],
            "harnesses": ["claude-code", "codex"],
            "repos": ["acme/web-app", "acme/api", "demo/cli-tool", "acme/mobile"],
        ]
    }

    // MARK: - Virtual Filesystem for Launch Picker

    public static func directoryListing(path: String) -> [String: Any] {
        let cleanPath = path.trimmingCharacters(in: .whitespacesAndNewlines)
        let entries: [[String: Any]]
        let parent: Any

        switch cleanPath {
        case "", "/projects":
            entries = [
                ["name": "acme-web-app", "path": "/projects/acme-web-app", "is_dir": true, "is_git_repo": true, "hidden": false],
                ["name": "acme-api", "path": "/projects/acme-api", "is_dir": true, "is_git_repo": true, "hidden": false],
                ["name": "demo-cli-tool", "path": "/projects/demo-cli-tool", "is_dir": true, "is_git_repo": true, "hidden": false],
                ["name": "acme-mobile", "path": "/projects/acme-mobile", "is_dir": true, "is_git_repo": true, "hidden": false],
            ]
            parent = cleanPath == "/projects" ? "/" : NSNull()
        case "/projects/acme-web-app":
            entries = [
                ["name": "src", "path": "/projects/acme-web-app/src", "is_dir": true, "is_git_repo": false, "hidden": false],
                ["name": "public", "path": "/projects/acme-web-app/public", "is_dir": true, "is_git_repo": false, "hidden": false],
                ["name": "package.json", "path": "/projects/acme-web-app/package.json", "is_dir": false, "is_git_repo": false, "hidden": false],
            ]
            parent = "/projects"
        case "/projects/acme-api":
            entries = [
                ["name": "src", "path": "/projects/acme-api/src", "is_dir": true, "is_git_repo": false, "hidden": false],
                ["name": "migrations", "path": "/projects/acme-api/migrations", "is_dir": true, "is_git_repo": false, "hidden": false],
                ["name": "Cargo.toml", "path": "/projects/acme-api/Cargo.toml", "is_dir": false, "is_git_repo": false, "hidden": false],
            ]
            parent = "/projects"
        case "/projects/demo-cli-tool":
            entries = [
                ["name": "src", "path": "/projects/demo-cli-tool/src", "is_dir": true, "is_git_repo": false, "hidden": false],
                ["name": "tests", "path": "/projects/demo-cli-tool/tests", "is_dir": true, "is_git_repo": false, "hidden": false],
                ["name": "README.md", "path": "/projects/demo-cli-tool/README.md", "is_dir": false, "is_git_repo": false, "hidden": false],
            ]
            parent = "/projects"
        default:
            entries = []
            parent = "/projects"
        }

        return [
            "roots": [["name": "Projects", "path": "/projects"]],
            "path": cleanPath.isEmpty ? "/projects" : cleanPath,
            "parent": parent,
            "entries": entries,
            "truncated": false,
        ]
    }

    // MARK: - Model Catalog

    public static var defaultModelCatalog: [String: Any] {
        [
            "models": [
                [
                    "id": "claude-3-7-sonnet",
                    "display_name": "Claude 3.7 Sonnet",
                    "description": "Anthropic flagship reasoning and coding model",
                    "supports_thinking": true,
                ],
                [
                    "id": "claude-3-5-haiku",
                    "display_name": "Claude 3.5 Haiku",
                    "description": "Fast and lightweight coding assistant",
                    "supports_thinking": false,
                ],
                [
                    "id": "gpt-4o",
                    "display_name": "GPT-4o",
                    "description": "OpenAI multimodal flagship model",
                    "supports_thinking": false,
                ],
            ]
        ]
    }
}
