#!/usr/bin/env python3
"""Render docs/drover-architecture.png.

The diagram is generated, not hand-drawn, so it can be kept in step with
docs/architecture.md. When a harness, port, or boundary claim changes there,
edit the matching string below and re-run this script.

Requires Pillow and the macOS system fonts (Avenir Next, Menlo).

    python3 scripts/make_architecture_diagram.py [output.png]
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "docs" / "drover-architecture.png"

S = 2                      # supersample factor
W, H = 1536, 1024

AVENIR = "/System/Library/Fonts/Avenir Next.ttc"
MENLO = "/System/Library/Fonts/Menlo.ttc"
BOLD, DEMI, MEDIUM, REG = 0, 2, 5, 7

BG        = (246, 248, 249)
INK       = (15, 23, 42)
MUTED     = (100, 116, 139)
FAINT     = (148, 163, 184)
CARD      = (255, 255, 255)
CARD_EDGE = (216, 226, 231)

TEAL_BG, TEAL_EDGE, TEAL_INK, TEAL = (233, 246, 244), (178, 221, 216), (13, 124, 113), (14, 159, 142)
AMB_BG, AMB_EDGE, AMB_INK, AMBER = (252, 246, 236), (238, 216, 179), (177, 95, 10), (224, 123, 57)
TINT_TEAL = (230, 244, 241)
TINT_AMB  = (251, 238, 218)
TINT_BLUE = (238, 243, 247)
DARK      = (11, 18, 32)

_fc = {}


def F(size, idx=REG, mono=False):
    key = (size, idx, mono)
    if key not in _fc:
        _fc[key] = ImageFont.truetype(MENLO if mono else AVENIR, size * S, index=0 if mono else idx)
    return _fc[key]


img = Image.new("RGB", (W * S, H * S), BG)
d = ImageDraw.Draw(img)


def box(x0, y0, x1, y1, fill=CARD, edge=CARD_EDGE, r=12, wid=1, dash=False):
    xy = [x0 * S, y0 * S, x1 * S, y1 * S]
    if dash:
        d.rounded_rectangle(xy, radius=r * S, fill=fill)
        _dash_rect(x0, y0, x1, y1, edge, r)
    else:
        d.rounded_rectangle(xy, radius=r * S, fill=fill, outline=edge, width=wid * S)


def _dash_rect(x0, y0, x1, y1, color, r):
    step, on = 9, 5
    for x in range(int(x0 + r), int(x1 - r), step):
        d.line([x * S, y0 * S, (x + on) * S, y0 * S], fill=color, width=1 * S)
        d.line([x * S, y1 * S, (x + on) * S, y1 * S], fill=color, width=1 * S)
    for y in range(int(y0 + r), int(y1 - r), step):
        d.line([x0 * S, y * S, x0 * S, (y + on) * S], fill=color, width=1 * S)
        d.line([x1 * S, y * S, x1 * S, (y + on) * S], fill=color, width=1 * S)


def T(x, y, s, size=13, idx=REG, fill=INK, mono=False, anchor="la", track=0):
    f = F(size, idx, mono)
    if track:
        total = sum(d.textlength(c, font=f) + track * S for c in s) - track * S
        cx = x * S
        if anchor[0] == "r":
            cx -= total
        elif anchor[0] == "m":
            cx -= total / 2
        for ch in s:
            d.text((cx, y * S), ch, font=f, fill=fill, anchor="la")
            cx += d.textlength(ch, font=f) + track * S
        return
    d.text((x * S, y * S), s, font=f, fill=fill, anchor=anchor)


def TW(s, size=13, idx=REG, mono=False):
    return d.textlength(s, font=F(size, idx, mono)) / S


def arrow(x0, y0, x1, y1, color, wid=2, dash=False, head=7):
    if dash:
        n = max(1, int(((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 / 10))
        for i in range(n):
            t0, t1 = i / n, i / n + 0.55 / n
            d.line([(x0 + (x1 - x0) * t0) * S, (y0 + (y1 - y0) * t0) * S,
                    (x0 + (x1 - x0) * t1) * S, (y0 + (y1 - y0) * t1) * S], fill=color, width=wid * S)
    else:
        d.line([x0 * S, y0 * S, x1 * S, y1 * S], fill=color, width=wid * S)
    dx, dy = x1 - x0, y1 - y0
    ln = max((dx * dx + dy * dy) ** 0.5, 0.001)
    ux, uy = dx / ln, dy / ln
    px, py = -uy, ux
    d.polygon([(x1 * S, y1 * S),
               ((x1 - ux * head + px * head * 0.52) * S, (y1 - uy * head + py * head * 0.52) * S),
               ((x1 - ux * head - px * head * 0.52) * S, (y1 - uy * head - py * head * 0.52) * S)], fill=color)


def pill(cx, cy, s, size=12, fill=CARD, edge=CARD_EDGE, ink=INK, padx=12, h=26):
    w = TW(s, size, MEDIUM) + padx * 2
    box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, fill, edge, r=int(h / 2))
    T(cx, cy, s, size, MEDIUM, ink, anchor="mm")


# ── header ────────────────────────────────────────────────────────────────────
T(44, 20, "Drover architecture", 33, BOLD)
T(46, 64, "Private command plane + durable context plane for coding agents", 15, REG, MUTED)

# ── trust boundary strip ──────────────────────────────────────────────────────
box(44, 98, 1492, 148)
d.rounded_rectangle([74 * S, 114 * S, 92 * S, 132 * S], radius=5 * S, fill=TEAL)
T(102, 115, "Single trusted operator boundary", 15, DEMI)
for xd in (438, 742, 1042):
    d.line([xd * S, 110 * S, xd * S, 136 * S], fill=(228, 234, 238), width=1 * S)
T(468, 116, "localhost / private LAN / Tailscale only", 13, REG, MUTED)
T(772, 116, "No public-internet exposure in v0.3", 13, REG, MUTED)
T(1072, 116, "Device + host credentials  ·  no RBAC / multi-tenant isolation", 13, REG, MUTED)

# ── command plane ─────────────────────────────────────────────────────────────
box(44, 170, 1492, 524, TEAL_BG, TEAL_EDGE, r=14)
T(74, 186, "COMMAND PLANE  ·  LIVE CONTROL", 15, BOLD, TEAL_INK, track=0.7)
T(74, 212, "Interactive session control, routing, streaming events, and terminal I/O", 13, REG, (91, 124, 120))

# operator clients
box(74, 242, 322, 480)
T(98, 262, "Operator clients", 17, DEMI)
box(98, 300, 298, 338, TINT_BLUE, (223, 232, 238), r=9)
T(114, 310, "iOS app", 14, MEDIUM)
box(98, 350, 298, 388, TINT_BLUE, (223, 232, 238), r=9)
T(114, 360, "Web / CLI", 14, MEDIUM)
T(98, 410, "Presentation + local settings", 12, REG, MUTED)
T(98, 428, "live on the client.", 12, REG, MUTED)

# drover-server
box(474, 242, 964, 480)
T(498, 258, "drover-server", 19, DEMI)
T(940, 264, "CENTRAL MACHINE", 11, BOLD, MUTED, anchor="ra", track=0.4)
box(498, 300, 706, 372, TINT_TEAL, (206, 232, 228), r=9)
T(514, 312, "Harness API", 15, DEMI)
T(514, 338, "HTTP + WebSocket · :7080", 11, REG, (40, 96, 90), mono=True)
box(718, 300, 940, 372, TINT_TEAL, (206, 232, 228), r=9)
T(734, 312, "Fleet registry", 15, DEMI)
T(734, 338, "host state + session routing", 11, REG, (71, 85, 105))
box(498, 384, 940, 456, TINT_TEAL, (206, 232, 228), r=9)
T(514, 396, "Command coordinator", 15, DEMI)
T(514, 422, "Routes operations; does not execute remote commands itself.", 11, REG, (71, 85, 105))

# harness hosts
box(1040, 242, 1462, 480)
T(1064, 262, "Harness hosts", 17, DEMI)
T(1438, 266, "1..N MACHINES", 11, BOLD, MUTED, anchor="ra", track=0.4)
box(1064, 300, 1438, 380, TINT_TEAL, (206, 232, 228), r=9)
T(1080, 310, "drover-harnessd", 15, DEMI)
T(1080, 334, "Claude Code · Codex · Antigravity (agy) · DeepSeek Harness", 11, REG, (71, 85, 105))
T(1080, 353, "session lifecycle · structured adapters · PTY/tmux · terminal stream", 10.5, REG, (100, 116, 139))
box(1064, 392, 1246, 456, TINT_BLUE, (223, 232, 238), r=9)
T(1080, 404, "Direct host", 14, DEMI)
T(1080, 426, "private inbound", 11, REG, MUTED)
box(1258, 392, 1438, 456, TINT_BLUE, (223, 232, 238), r=9)
T(1274, 404, "Relay host", 14, DEMI)
T(1274, 426, "outbound dial", 11, REG, MUTED)

# command-plane arrows
arrow(324, 330, 470, 330, TEAL)
T(397, 308, "/harness", 12, MEDIUM, TEAL_INK, anchor="ma")
arrow(966, 316, 1036, 316, TEAL)
T(1001, 294, ":7081", 12, MEDIUM, TEAL_INK, anchor="ma", mono=True)
arrow(1036, 400, 966, 400, FAINT, dash=True)
T(1001, 408, "dial-out", 11, REG, MUTED, anchor="ma")

# authority callout
box(1104, 486, 1462, 534, DARK, DARK, r=10)
T(1283, 494, "Host daemon is authoritative for", 12, DEMI, (255, 255, 255), anchor="ma")
T(1283, 512, "local processes + filesystem access", 12, DEMI, (255, 255, 255), anchor="ma")

# ── cross-plane flows ─────────────────────────────────────────────────────────
arrow(744, 526, 744, 596, FAINT, dash=True)
pill(744, 562, "operational state")
arrow(1250, 540, 1250, 596, FAINT, dash=True)
pill(1250, 562, "agent events + spans")

# ── context plane ─────────────────────────────────────────────────────────────
box(44, 596, 1492, 964, AMB_BG, AMB_EDGE, r=14)
T(74, 612, "CONTEXT PLANE  ·  DURABLE MEMORY", 15, BOLD, AMB_INK, track=0.7)
T(74, 638, "Capture  ·  normalize  ·  preserve  ·  derive  ·  recall", 13, REG, (146, 110, 61))

# activity sources
box(74, 668, 340, 930)
T(98, 686, "Activity sources", 17, DEMI)
box(98, 722, 316, 758, TINT_BLUE, (223, 232, 238), r=9)
T(114, 731, "drover-collect", 14, MEDIUM)
box(98, 768, 316, 804, TINT_BLUE, (223, 232, 238), r=9)
T(114, 777, "Hooks / JSONL", 14, MEDIUM)
box(98, 814, 316, 850, TINT_BLUE, (223, 232, 238), r=9)
T(114, 823, "OTLP producers", 14, MEDIUM)
T(300, 825, ":4317", 11, REG, MUTED, anchor="ra", mono=True)
T(98, 862, "Claude Code · Codex · Antigravity", 11, REG, MUTED)
T(98, 880, "OpenClaw · Hermes · compatible tools", 11, REG, MUTED)
T(98, 900, "Agent events, spans, PR events, routing.", 11, REG, MUTED)

# ingest
box(400, 690, 604, 866)
T(424, 708, "Ingest", 17, DEMI)
for i, line in enumerate(["Normalize identifiers", "Attribute repository context",
                          "Deduplicate records", "Write durable facts"]):
    T(424, 748 + i * 28, line, 12.5, REG, (71, 85, 105))

# redis
box(400, 888, 620, 942, AMB_BG, FAINT, r=10, dash=True)
T(422, 898, "Redis Streams · optional", 12.5, DEMI, (100, 116, 139))
T(422, 918, "leases, retries, backpressure only", 11, REG, MUTED)

# local data boundary
box(668, 668, 1092, 930)
T(692, 686, "Local data boundary", 17, DEMI)
T(1068, 690, "~/.drover/", 11, REG, MUTED, anchor="ra", mono=True)
box(692, 724, 1068, 802, TINT_AMB, (240, 220, 187), r=9)
T(708, 734, "Parquet facts", 15, DEMI)
T(708, 758, "agent_events · spans · pr_events · routing", 11, REG, (120, 88, 45), mono=True)
T(708, 779, "Append-oriented durable system of record", 11, REG, (146, 110, 61))
arrow(880, 806, 880, 826, AMBER)
box(692, 832, 1068, 910, TINT_BLUE, (223, 232, 238), r=9)
T(708, 842, "DuckDB", 15, DEMI)
T(708, 866, "normalized views · operational state · pipeline ledger", 11, REG, (71, 85, 105))
T(708, 886, "harness hosts/sessions/events/tasks + derived serving state", 11, REG, (100, 116, 139))

# derive + retrieve
box(1152, 668, 1462, 930)
T(1176, 686, "Derive + retrieve", 17, DEMI)
box(1176, 724, 1438, 802, TINT_TEAL, (206, 232, 228), r=9)
T(1192, 734, "Workers", 15, DEMI)
T(1192, 758, "summaries · project briefs", 11, REG, (71, 85, 105))
T(1192, 778, "decisions · embeddings", 11, REG, (71, 85, 105))
box(1176, 816, 1438, 878, TINT_BLUE, (223, 232, 238), r=9)
T(1192, 826, "Drover MCP", 15, DEMI)
T(1192, 850, "streamable HTTP · :7077", 11, REG, (71, 85, 105), mono=True)
pill(1307, 906, "Agents / MCP clients query here", 11.5)

# context-plane arrows
arrow(342, 778, 396, 778, AMBER)
arrow(606, 778, 664, 778, AMBER)
arrow(1094, 778, 1148, 778, AMBER)
arrow(622, 915, 690, 878, FAINT, dash=True)
arrow(1070, 890, 1148, 862, FAINT, dash=True)

# ── legend ────────────────────────────────────────────────────────────────────
d.line([44 * S, 982 * S, 1492 * S, 982 * S], fill=(226, 232, 236), width=1 * S)
lx = 60
for color, label, dash in ((TEAL, "live control / routing", False),
                           (AMBER, "telemetry / context flow", False),
                           (FAINT, "optional / derived coordination", True)):
    arrow(lx, 1002, lx + 46, 1002, color, dash=dash, head=6)
    T(lx + 58, 994, label, 12, REG, MUTED)
    lx += 58 + TW(label, 12, REG) + 46
d.line([(lx - 24) * S, 990 * S, (lx - 24) * S, 1014 * S], fill=(226, 232, 236), width=1 * S)
T(1492, 994, "The host daemon owns execution. Parquet owns durable facts.", 12, REG, MUTED, anchor="ra")

out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
out.parent.mkdir(parents=True, exist_ok=True)
img.resize((W, H), Image.LANCZOS).save(out)
print(f"wrote {out} ({out.stat().st_size / 1e6:.2f} MB)")
