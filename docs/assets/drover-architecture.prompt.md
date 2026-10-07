# Drover V2 architecture diagram prompt

Generated with Codex's built-in image-generation tool on 2026-10-06. The first
attempt connected the cross-plane event arrow to `drover-server`; the second
prompt below corrected it and produced the committed PNG.

```text
Use case: infographic-diagram
Asset type: repository architecture diagram, landscape 16:9 PNG
Primary request: Draw a clean technical architecture diagram for Drover V2. Correct topology is essential.
Style: light warm-white background; flat vector-like rounded rectangles; thin dark arrows; restrained navy, teal, orange; large crisp horizontal typography; no decoration.
Layout: two wide horizontal lanes.
Top lane title exactly "COMMAND PLANE". Four boxes left to right with arrows: "Clients\niOS • Web • CLI" → "drover-server" → "drover-harnessd\nper host" → "Agent processes".
Bottom lane title exactly "CONTEXT PLANE V2". Eight stages left to right with arrows: "drover-collect + hooks" → "Single ingest path" → "Control store\nPostgreSQL / DuckDB" → "Outbox" → "LakeOutboxExporter" → a grouped box "DuckLake" containing "PostgreSQL catalog" and "Parquet data files" → "Disposable query child\n5 s • 2 GiB RSS" → "Readers\nCockpit • MCP • iOS".
Critical cross-lane connector: draw one arrow that begins specifically at the bottom edge of the THIRD top-lane box, "drover-harnessd\nper host", and ends specifically at the top edge of the SECOND bottom-lane box, "Single ingest path". Label only that arrow "events". Do not connect drover-server downward.
Place a small checkpoint/shield on the arrow from DuckLake to Disposable query child, labeled exactly "serving proof + epoch".
Constraints: exact spelling and capitalization; no extra nodes or labels; all labels legible at README width; no hostnames, addresses, usernames, paths, tokens, logos, clouds, gradients, shadows, isometric art, or crossed labels. Make LakeOutboxExporter wide enough that its camel-case name has breathing room.
```
