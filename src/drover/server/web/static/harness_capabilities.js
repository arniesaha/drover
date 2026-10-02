// Capability-driven harness controls for the web console (#419).
//
// Every launch and session control is derived from the host's advertised
// capability envelope (schema v1, docs/harness-adapter-architecture.md). No
// harness name appears here: a harness the matrix does not describe gets no
// controls. Inlined into harness.html / harness_terminal.html by ui.py and
// loaded directly by tests/test_web_harness_capabilities.py under node.
const DroverCapabilities = (() => {
  const SCHEMA_VERSION = 1;
  // Modes this client can drive, most preferred first. When a harness
  // advertises both, structured wins: approvals, interrupt, attachments and
  // the model catalog describe structured adapter operations, while a PTY
  // session exposes only raw terminal I/O.
  const CLIENT_MODES = ["structured", "pty"];
  const MIME = /^[a-z0-9.+-]+\/(?:[a-z0-9.+-]+|\*)$/;

  const REASONS = {
    missing: "This host does not advertise this harness.",
    legacy: "This host predates harness capability schema v1. Upgrade Drover on the host to launch from the web; existing sessions stay listed.",
    invalid: "This host sent an invalid capability matrix for this harness.",
    unsupported: (version) => `This host advertises capability schema v${version}; this web console understands v${SCHEMA_VERSION}. Upgrade the Drover hub.`,
    disabled: "Not enabled on this host.",
    noMode: "Advertises no launch mode this web console supports.",
  };

  const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

  function rows(host) {
    const harnesses = isObject(host?.capabilities) ? host.capabilities.harnesses : null;
    return Array.isArray(harnesses) ? harnesses : [];
  }

  function rowName(row) {
    if (typeof row === "string") return row;
    return isObject(row) && typeof row.name === "string" ? row.name : "";
  }

  // The host's label for a harness (additive row field, #422). Older hosts
  // omit it; the raw name is then shown. Presentation only, never identity.
  function rowDisplayName(row, name) {
    const label = isObject(row) ? row.display_name : undefined;
    return typeof label === "string" && label.trim() ? label : name;
  }

  function closed(name, status, reason, extra = {}) {
    return {
      name,
      displayName: name,
      status,
      reason,
      launchable: false,
      mode: null,
      modes: [],
      approvals: false,
      interrupt: false,
      nativeResume: false,
      modelCatalog: false,
      usage: false,
      worktree: false,
      interactiveAuth: false,
      attachments: [],
      ...extra,
    };
  }

  // Unknown fields, unknown modes and non-boolean flags never enable
  // anything: a client must fail closed on what it does not understand.
  function fromRow(row) {
    const name = rowName(row);
    if (!name) return null;
    const displayName = rowDisplayName(row, name);
    if (typeof row === "string" || !Object.prototype.hasOwnProperty.call(row, "capabilities")) {
      return closed(name, "legacy", REASONS.legacy, {legacy: true, displayName});
    }
    const matrix = row.capabilities;
    if (!isObject(matrix) || !Number.isInteger(matrix.schema_version) || matrix.schema_version < 1) {
      return closed(name, "invalid", REASONS.invalid, {displayName});
    }
    if (matrix.schema_version !== SCHEMA_VERSION) {
      return closed(name, "unsupported", REASONS.unsupported(matrix.schema_version), {
        schemaVersion: matrix.schema_version,
        displayName,
      });
    }
    if (matrix.harness_id !== undefined && matrix.harness_id !== name) {
      return closed(name, "invalid", REASONS.invalid, {displayName});
    }
    const booleanFields = ["approvals", "interrupt", "native_resume", "model_catalog",
      "usage", "worktree", "interactive_auth", "turn_preferences"];
    if (!Array.isArray(matrix.launch_modes) ||
        matrix.launch_modes.some((mode) => typeof mode !== "string") ||
        booleanFields.some((key) => matrix[key] !== undefined && typeof matrix[key] !== "boolean") ||
        (matrix.attachments !== undefined && (!Array.isArray(matrix.attachments) ||
          matrix.attachments.some((mime) => typeof mime !== "string" || !MIME.test(mime))))) {
      return closed(name, "invalid", REASONS.invalid, {displayName});
    }
    const advertised = matrix.launch_modes;
    const modes = CLIENT_MODES.filter((mode) => advertised.includes(mode));
    const flag = (key) => matrix[key] === true;
    const attachments = Array.isArray(matrix.attachments)
      ? matrix.attachments.filter((mime) => typeof mime === "string" && MIME.test(mime))
      : [];
    const enabled = row.enabled === true;
    const mode = modes[0] || null;
    const launchable = enabled && mode !== null;
    return {
      name,
      displayName,
      status: launchable ? "ready" : (enabled ? "no-mode" : "disabled"),
      reason: launchable ? "" : (enabled ? REASONS.noMode : REASONS.disabled),
      launchable,
      mode,
      modes,
      approvals: flag("approvals"),
      interrupt: flag("interrupt"),
      nativeResume: flag("native_resume"),
      modelCatalog: flag("model_catalog"),
      usage: flag("usage"),
      worktree: flag("worktree"),
      interactiveAuth: flag("interactive_auth"),
      attachments,
    };
  }

  function harnessControls(host, name) {
    const row = rows(host).find((item) => rowName(item) === name);
    return (row && fromRow(row)) || closed(String(name ?? ""), "missing", REASONS.missing);
  }

  function allControls(host) {
    return rows(host).map(fromRow).filter(Boolean);
  }

  // Launch targets in the host's advertised order, structured-capable first so
  // a one-click start gets the richest advertised session.
  function launchTargets(host) {
    const ready = allControls(host).filter((item) => item.launchable);
    return [
      ...ready.filter((item) => item.mode === "structured"),
      ...ready.filter((item) => item.mode !== "structured"),
    ];
  }

  function preferredLaunchTarget(host) {
    return launchTargets(host)[0] || null;
  }

  // Resolved from current state at the moment of submission, never from a
  // stale <select> or hidden control.
  function requireLaunch(host, name) {
    const controls = harnessControls(host, name);
    if (!controls.launchable) throw new Error(controls.reason || REASONS.missing);
    return controls;
  }

  function launchBody(controls, {cwd, rows: termRows, cols, model, thinkingEffort} = {}) {
    if (!controls?.launchable) throw new Error(controls?.reason || REASONS.missing);
    const body = {harness: controls.name, mode: controls.mode};
    if (cwd) body.cwd = cwd;
    if (controls.mode === "pty") {
      body.rows = termRows || 32;
      body.cols = cols || 100;
    } else if (controls.modelCatalog) {
      if (model) body.model = model;
      if (model && thinkingEffort) body.thinking_effort = thinkingEffort;
    }
    return body;
  }

  function acceptsAttachment(controls, mime) {
    const type = String(mime || "").toLowerCase();
    return (controls?.attachments || []).some((allowed) =>
      allowed === type || (allowed.endsWith("/*") && type.startsWith(allowed.slice(0, -1)))
    );
  }

  // Session controls combine the session's actual mode with what its harness
  // still advertises on its host. Terminal attach, Ctrl-C and Kill belong to
  // any PTY session (including pre-matrix ones); structured operations need
  // both a structured session and an advertised capability.
  function sessionControls(host, session) {
    const controls = harnessControls(host, session?.harness);
    const structured = session?.mode === "structured" && controls.modes.includes("structured");
    return {
      harness: controls,
      terminal: session?.mode !== "structured",
      structured,
      turns: structured,
      approvals: structured && controls.approvals,
      interrupt: structured && controls.interrupt,
      attachments: structured ? controls.attachments : [],
      reason: session?.mode === "structured" && !structured
        ? (controls.reason || REASONS.noMode)
        : "",
    };
  }

  const ACTIONS = {turns: "turns", approve: "approvals", interrupt: "interrupt"};

  function requireSessionAction(host, session, action) {
    const controls = sessionControls(host, session);
    const key = ACTIONS[action];
    if (!key || !controls[key]) {
      throw new Error(controls.reason || `This harness does not advertise ${action} for this session.`);
    }
    return controls;
  }

  function hostSummary(host) {
    const all = allControls(host);
    return {
      controls: all,
      legacy: all.length > 0 && all.every((item) => item.legacy),
      partlyLegacy: all.some((item) => item.legacy),
      unsupported: all.some((item) => item.status === "unsupported"),
      launchable: all.some((item) => item.launchable),
    };
  }

  // A pending approval is the newest approval_prompt whose request_id has no
  // later approval_response. Shown only when the session advertises approvals.
  function pendingApproval(events) {
    const answered = new Set();
    const list = Array.isArray(events) ? events : [];
    for (let index = list.length - 1; index >= 0; index -= 1) {
      const event = list[index] || {};
      const type = event.event_type || event.normalized_type;
      const requestId = event.payload?.request_id;
      if (type === "approval_response" && requestId) answered.add(requestId);
      if (type === "approval_prompt" && requestId && !answered.has(requestId)) {
        return {request_id: requestId, tool: event.payload?.tool || "", text: event.content_preview || event.payload?.text || ""};
      }
    }
    return null;
  }

  return {
    SCHEMA_VERSION,
    CLIENT_MODES,
    harnessControls,
    allControls,
    launchTargets,
    preferredLaunchTarget,
    requireLaunch,
    launchBody,
    acceptsAttachment,
    sessionControls,
    requireSessionAction,
    hostSummary,
    pendingApproval,
  };
})();

if (typeof module !== "undefined" && module.exports) module.exports = DroverCapabilities;
