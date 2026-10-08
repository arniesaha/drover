# Drover V2 architecture diagram prompt

The previous 1536×1024 diagram was supplied to Codex's built-in image-generation
tool as a style reference. This was the full generation prompt:

```text
Use case: infographic-diagram
Asset type: repository architecture diagram, exactly 1536×1024 (3:2 landscape)
Input image: reference image for exact visual style, layout grammar, typography hierarchy, colors, spacing, card structure, arrows, pills, and footer legend. Rebuild its content for V2; do not merely patch text.
Primary request: Create a complete Drover V2 architecture diagram matching the reference's look exactly.

Header: title "Drover architecture". Grey subtitle "Private command plane + verified DuckLake context plane for coding agents".
Trust strip: "Single trusted operator boundary" | "localhost / private LAN / Tailscale only" | "No public-internet exposure" | "Device + host credentials · no RBAC / multi-tenant isolation".

Upper mint panel heading "COMMAND PLANE · LIVE CONTROL" with caption "Interactive session control, routing, streaming events, and terminal I/O".
Cards: "Operator clients" containing "iOS app", "Web / CLI", and "Presentation + local settings live on the client."; "drover-server" / "CENTRAL MACHINE" containing "Harness API" / "HTTP + WebSocket · :7080", "Control store" / "PostgreSQL default · fleet state", and "Command coordinator" / "Routes operations; does not execute remote commands itself."; "Harness hosts" / "1..N MACHINES" containing "drover-harnessd", "Claude Code · Codex · Antigravity (agy) · DeepSeek Harness", "session lifecycle · structured adapters · PTY/tmux · terminal stream", "Direct host" / "private inbound", and "Relay host" / "outbound dial". Add "Host daemon is authoritative for local processes + filesystem access".
Teal arrows: clients → server labeled "/harness" and server → hosts labeled ":7081". Grey dashed reverse arrow labeled "dial-out".

Lower cream panel heading "CONTEXT PLANE V2 · DURABLE MEMORY" with caption "Capture · normalize · preserve · export · verify · derive · recall".
Left-to-right cards: "Activity sources" containing "drover-collect", "Hooks / JSONL", "OTLP producers · :4317", "Claude Code · Codex · Antigravity · DeepSeek · OpenClaw · Hermes", and "Agent events + optional diagnostic spans"; "Single ingest path" containing "Normalize identifiers", "Attribute repository context", "Deduplicate records", and "Write event + preview + outbox atomically"; "Control store + Outbox" / "POSTGRESQL" containing "System of record" / "Fleet + sessions · hot payloads · jobs" and "Transactional Outbox" / "Strongly consistent · read-your-writes"; "LakeOutboxExporter" containing "Fenced owner · batches up to 100" and "polls every 250 ms"; "DuckLake" containing "PostgreSQL catalog", "Parquet data files", and "Append-only analytical copy"; "Disposable query child" containing "5 s timeout · 2 GiB RSS cap · 2 threads" and shield "serving proof + epoch"; "Derive + retrieve" containing "Workers" / "summaries · project briefs · embeddings", "Drover MCP" / "streamable HTTP · :7077", and "Readers" / "Cockpit · iOS app · MCP clients".
Orange solid arrows through ingest and reads; grey dashed async export arrows from the outbox through LakeOutboxExporter to DuckLake.
Cross-plane pills: server → control store labeled "operational state"; drover-harnessd → Single ingest path labeled "agent events + spans".
Consistency callout: "Control reads: PostgreSQL read-your-writes"; "Lake reads: eventually consistent; ≤100 events/batch, 250 ms polling"; "Verified epoch only; proof failure refuses the read".
Footer legend: teal solid "live control / routing"; orange solid "telemetry / context flow"; grey dashed "async / derived". Footer note: "PostgreSQL serves control. DuckLake serves verified analytical reads."

Constraints: match the reference aesthetic exactly; exact spelling and arrow endpoints; include every named card and callout; no invented components, logos, decorative art, gradients, hostnames, usernames, personal paths, or secrets.
```

Three image-generation attempts were inspected. The third still rendered
`PostgreSQL catalo`, so the accepted generated image is retained as
`drover-architecture.generated.png` and the exact label is overlaid by
`drover-architecture.svg`. The committed `docs/drover-architecture.png` is a
1536×1024 rendering of that SVG composite.
