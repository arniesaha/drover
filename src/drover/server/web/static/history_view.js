// Session history view logic for the web console (docs/design/session-history.md).
//
// Pure functions only -- no DOM, no fetch -- so history.html stays a thin shell
// and tests/test_web_history.py can drive every rule under node. Inlined into
// history.html by ui.py.
const DroverHistory = (() => {
  const PAGE_SIZE = 30;
  const DEBOUNCE_MS = 250;
  const ROW_HEIGHT = 76;
  const DAY_HEIGHT = 34;
  const OVERSCAN = 8;
  const SNAPSHOT_MAX_ITEMS = 1500;
  const SNAPSHOT_MAX_AGE_MS = 30 * 60 * 1000;
  const STATES = ["running", "awaiting", "finished", "failed"];
  const LIST_KEYS = [["host", "hosts"], ["harness", "harnesses"], ["repo", "repos"], ["state", "states"]];

  function emptyFilters() {
    return {hosts: [], harnesses: [], repos: [], states: [], since: "", until: "", q: ""};
  }

  function normalizeFilters(filters) {
    const out = emptyFilters();
    for (const [, key] of LIST_KEYS) {
      out[key] = [...new Set((filters && filters[key]) || [])].filter(Boolean).sort();
    }
    out.states = out.states.filter((s) => STATES.includes(s));
    out.since = (filters && filters.since) || "";
    out.until = (filters && filters.until) || "";
    out.q = ((filters && filters.q) || "").trim();
    return out;
  }

  // The filters as query parameters, in a stable order, so the same filters
  // always produce the same URL (and the same snapshot key).
  function filterParams(filters) {
    const f = normalizeFilters(filters);
    const params = new URLSearchParams();
    for (const [param, key] of LIST_KEYS) for (const value of f[key]) params.append(param, value);
    if (f.since) params.set("since", f.since);
    if (f.until) params.set("until", f.until);
    if (f.q) params.set("q", f.q);
    return params;
  }

  function filtersKey(filters) {
    return filterParams(filters).toString();
  }

  // A date picked as "to" means through the end of that day; the API's
  // `until` is exclusive, so send the following day.
  function apiUntil(until) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(until || "")) return until;
    const [y, m, d] = until.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d + 1)).toISOString().slice(0, 10);
  }

  function pageUrl(filters, cursor, limit) {
    const params = filterParams(filters);
    if (params.has("until")) params.set("until", apiUntil(params.get("until")));
    params.set("limit", String(limit || PAGE_SIZE));
    if (cursor) params.set("cursor", cursor);
    return "/sessions/history?" + params.toString();
  }

  function filtersFromSearch(search) {
    const params = new URLSearchParams(search || "");
    const out = emptyFilters();
    for (const [param, key] of LIST_KEYS) out[key] = params.getAll(param);
    out.since = params.get("since") || "";
    out.until = params.get("until") || "";
    out.q = params.get("q") || "";
    return normalizeFilters(out);
  }

  function hasFilters(filters) {
    return filtersKey(filters) !== "";
  }

  function toggle(filters, key, value) {
    const f = normalizeFilters(filters);
    f[key] = f[key].includes(value) ? f[key].filter((v) => v !== value) : [...f[key], value];
    return normalizeFilters(f);
  }

  // since/until for a quick range chip, as UTC dates the API accepts.
  function rangeFilters(filters, days, now) {
    const f = normalizeFilters(filters);
    if (!days) return {...f, since: "", until: ""};
    const since = new Date((now || Date.now()) - days * 86400000);
    return {...f, since: since.toISOString(), until: ""};
  }

  // Merge a fetched page, keeping the first occurrence of each id. A session
  // that gained activity can reappear above the cursor; it must not render
  // twice.
  function merge(existing, incoming) {
    const seen = new Set(existing.map((item) => item.id));
    const out = existing.slice();
    for (const item of incoming || []) {
      if (!seen.has(item.id)) {
        seen.add(item.id);
        out.push(item);
      }
    }
    return out;
  }

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  // Calendar day in the viewer's time zone (or a fixed offset for tests).
  function dayKey(iso, offsetMinutes) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "unknown";
    if (offsetMinutes === undefined) {
      return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
    }
    const shifted = new Date(date.getTime() + offsetMinutes * 60000);
    return `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}`;
  }

  function dayLabel(key, todayKey, yesterdayKey) {
    if (key === "unknown") return "Unknown date";
    if (key === todayKey) return "Today";
    if (key === yesterdayKey) return "Yesterday";
    const [y, m, d] = key.split("-").map(Number);
    const date = new Date(Date.UTC(y, m - 1, d));
    return date.toLocaleDateString(undefined, {
      weekday: "short", month: "short", day: "numeric", year: "numeric", timeZone: "UTC",
    });
  }

  // Items -> display rows with a day header before each new day.
  function rows(items, now, offsetMinutes) {
    const nowMs = now === undefined ? Date.now() : now;
    const today = dayKey(new Date(nowMs).toISOString(), offsetMinutes);
    const yesterday = dayKey(new Date(nowMs - 86400000).toISOString(), offsetMinutes);
    const out = [];
    let current = null;
    for (const item of items) {
      const key = dayKey(item.last_activity, offsetMinutes);
      if (key !== current) {
        current = key;
        out.push({kind: "day", key, label: dayLabel(key, today, yesterday)});
      }
      out.push({kind: "item", key: item.id, item});
    }
    return out;
  }

  // Top offset of every row plus the total height. Two fixed heights keep
  // this O(n) once per data change and the window O(log n) per scroll.
  function layout(displayRows) {
    const offsets = new Array(displayRows.length);
    let y = 0;
    for (let i = 0; i < displayRows.length; i++) {
      offsets[i] = y;
      y += displayRows[i].kind === "day" ? DAY_HEIGHT : ROW_HEIGHT;
    }
    return {offsets, total: y};
  }

  function firstAtOrAfter(offsets, y) {
    let lo = 0;
    let hi = offsets.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (offsets[mid] < y) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  // The slice of rows to put in the DOM for a viewport, with overscan.
  function visibleRange(lay, scrollTop, viewportHeight, overscan) {
    const n = lay.offsets.length;
    if (!n) return {start: 0, end: 0};
    const extra = overscan === undefined ? OVERSCAN : overscan;
    const top = Math.max(0, scrollTop);
    const first = Math.max(0, firstAtOrAfter(lay.offsets, top + 1) - 1);
    const last = firstAtOrAfter(lay.offsets, top + viewportHeight);
    return {start: Math.max(0, first - extra), end: Math.min(n, last + extra)};
  }

  // Ask for the next page while the viewer is still this many rows away.
  function shouldLoadMore(range, rowCount, hasMore, loading, threshold) {
    return Boolean(hasMore && !loading && range.end >= rowCount - (threshold || 10));
  }

  function debounce(fn, ms, timers) {
    const t = timers || {setTimeout, clearTimeout};
    let handle = null;
    const wrapped = (...args) => {
      if (handle !== null) t.clearTimeout(handle);
      handle = t.setTimeout(() => {
        handle = null;
        fn(...args);
      }, ms === undefined ? DEBOUNCE_MS : ms);
    };
    wrapped.cancel = () => {
      if (handle !== null) t.clearTimeout(handle);
      handle = null;
    };
    return wrapped;
  }

  // Retry-After as milliseconds: delta-seconds or an HTTP-date. Bounded so a
  // bad header cannot park the page for hours.
  function retryAfterMs(header, now) {
    if (!header) return 2000;
    const seconds = Number(header);
    let ms = Number.isFinite(seconds) ? seconds * 1000 : Date.parse(header) - (now || Date.now());
    if (!Number.isFinite(ms)) ms = 2000;
    return Math.min(Math.max(ms, 500), 60000);
  }

  function stateTone(state) {
    return {running: "ok", awaiting: "warn", failed: "bad", finished: "muted"}[state] || "muted";
  }

  function tokenLabel(tokens) {
    if (!tokens) return "";
    const total = (tokens.input || 0) + (tokens.output || 0);
    if (!total) return "";
    if (total >= 1e6) return `${(total / 1e6).toFixed(1)}M tok`;
    if (total >= 1e3) return `${Math.round(total / 1e3)}k tok`;
    return `${total} tok`;
  }

  // Transcript pages come from the existing paged endpoint, newest first,
  // then older by sequence. Never more than 200 events per request.
  const TRANSCRIPT_PAGE = 200;
  const TRANSCRIPT_MAX_MESSAGES = 1000;
  const MESSAGE_CHARS = 4000;
  const ANSI = /\x1b\[[0-?]*[ -\/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g;
  const ROLE_BY_TYPE = {
    user_input: "user",
    "terminal.input": "user",
    assistant_output: "assistant",
    tool_action: "tool",
    tool_result: "tool",
    "terminal.output": "terminal",
  };

  function transcriptUrl(id, beforeSeq, limit) {
    const params = new URLSearchParams();
    if (beforeSeq) params.set("before_seq", String(beforeSeq));
    params.set("limit", String(Math.min(limit || TRANSCRIPT_PAGE, TRANSCRIPT_PAGE)));
    return `/harness/sessions/${encodeURIComponent(id)}/messages?${params}`;
  }

  function messageView(message) {
    const m = message || {};
    const inner = m.payload && typeof m.payload === "object" ? m.payload : {};
    let text = [m.text, m.content, inner.text, m.content_preview]
      .find((value) => typeof value === "string" && value) || "";
    text = text.replace(ANSI, "");
    if (text.length > MESSAGE_CHARS) text = text.slice(0, MESSAGE_CHARS) + "…";
    const type = m.type || m.event_type || "";
    return {
      seq: m.seq,
      role: m.role || ROLE_BY_TYPE[type] || type || "event",
      text,
      ts: m.ts || m.created_at || null,
      unavailable: Boolean(m.payload_unavailable),
    };
  }

  // What Back from the live console (or a reload) restores. Large lists keep
  // only their scroll position: re-paging from the top is cheaper than a
  // multi-megabyte snapshot.
  function snapshot(state, now) {
    const keepItems = state.items.length <= SNAPSHOT_MAX_ITEMS;
    return JSON.stringify({
      key: filtersKey(state.filters),
      items: keepItems ? state.items : [],
      nextCursor: keepItems ? state.nextCursor : null,
      hasMore: keepItems ? state.hasMore : true,
      complete: keepItems,
      scrollTop: state.scrollTop || 0,
      savedAt: now === undefined ? Date.now() : now,
    });
  }

  function restore(raw, filters, now) {
    if (!raw) return null;
    let saved;
    try {
      saved = JSON.parse(raw);
    } catch (_) {
      return null;
    }
    const nowMs = now === undefined ? Date.now() : now;
    if (!saved || saved.key !== filtersKey(filters)) return null;
    if (!(nowMs - saved.savedAt < SNAPSHOT_MAX_AGE_MS)) return null;
    return saved;
  }

  return {
    PAGE_SIZE,
    DEBOUNCE_MS,
    ROW_HEIGHT,
    DAY_HEIGHT,
    STATES,
    emptyFilters,
    normalizeFilters,
    filtersKey,
    apiUntil,
    pageUrl,
    filtersFromSearch,
    hasFilters,
    toggle,
    rangeFilters,
    merge,
    dayKey,
    rows,
    layout,
    visibleRange,
    shouldLoadMore,
    debounce,
    retryAfterMs,
    stateTone,
    tokenLabel,
    TRANSCRIPT_PAGE,
    TRANSCRIPT_MAX_MESSAGES,
    transcriptUrl,
    messageView,
    snapshot,
    restore,
  };
})();

if (typeof module !== "undefined" && module.exports) module.exports = DroverHistory;
