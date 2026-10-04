# Adapter extension points

Status: measured for #422, the extensibility gate of the harness-adapter program
(#414). It must pass before Profile or meta-harness work starts. See
[harness-adapter-architecture.md](../harness-adapter-architecture.md) for the
contract itself.

## The proof

`tests/harness_fixture_adapter.py` defines `synthetic-lab`, a test-only drive
adapter. It never ships: it is not in `BUILTIN_ADAPTERS` or `DEFAULT_PRESETS`.
Its capability mix differs from every built-in adapter:

| Capability | `synthetic-lab` | Built-ins |
| --- | --- | --- |
| `launch_modes` | `structured` and `pty` | `structured` only (shell: `pty` only) |
| `approvals` / `interrupt` | yes / **no** | Claude Code: yes / yes; others: no / yes |
| `native_resume` | yes, without restart recovery | yes, with restart recovery |
| `model_catalog` (model, thinking effort) | **no** | yes |
| `worktree` | no | Codex, agy, DeepSeek: yes |
| `attachments` | `image/png` only | four image types |
| `interactive_auth`, `usage` | no | varies |

`tests/test_adapter_extensibility.py` drives it through every layer. No layer
knows its ID:

1. **Registry.** It validates alongside the four built-ins.
2. **Host envelope.** harnessd publishes it from the declaration, including its
   `display_name`.
3. **Central API.** The hub proxies the same row. Launch, turns, approvals,
   Continue (Drover handoff and native resume) and PTY launch with native resume
   all work through the hub.
4. **Refusals.** Every undeclared operation is refused with a deterministic 400
   before the adapter is called: interrupt, a JPEG attachment, a model
   override, a thinking effort, and a native resume without a session ID. No
   registry row, attachment file or provider call is left behind.
5. **Web.** `tests/fixtures/web/harness_capabilities_hosts.json` (`lab-host`)
   carries the published row byte for byte (asserted).
   `test_synthetic_adapter_renders_from_its_published_row` checks controls,
   launch body, MIME filter, label and the refused interrupt.
6. **iOS.** `harness-capabilities-mixed.json` (`lab-host`) carries the same row
   (asserted from Python). `syntheticAdapterRowDecodesFromItsPublishedEnvelope`
   and `aSyntheticAdapterLaunchesFromItsEnvelopeAlone` cover decoding,
   controls, label and launch body.

## Measured extension points

These are the files a new drive adapter touches. Measured on `synthetic-lab`
and on the four built-ins:

| # | Where | What | Required? |
| --- | --- | --- | --- |
| 1 | `harness/structured/<harness>.py` | The native driver: wire protocol and `StructuredMessage` normalization. | Structured mode |
| 2 | `harness/structured/adapters.py` | The `HarnessAdapter` subclass: `id`, `display_name`, `capabilities`, and one hook per declared capability. | Always |
| 3 | `harness/structured/adapters.py` | One entry in the `BUILTIN_ADAPTERS` list. | Always |
| 4 | `harness/daemon.py` `DEFAULT_PRESETS` | The host availability row: the executable to resolve on `PATH` and a description. | Always |
| 5 | `harness/model_catalog/<harness>.py` | The catalog adapter returned by `model_catalog_adapter`. | If `model_catalog` |
| 6 | `tests/` | Contract tests for every declared capability. | Always |
| 7 | The adapter's `native_sessions` / `native_transcript` | Provider-local resume candidates and transcript, read from the CLI's own history. | Optional; `native_sessions` requires `native_resume` |

The test fixture needs only 2, 4, 6 and 7: it brings an in-process driver, its
"registry entry" is the registry the test builds, and its `native_sessions`
serves in-memory candidates (no history parser is invented for it).

### Native resume

Native resume is one capability with one optional extension:

- **Eligibility** is the advertised `native_resume` flag, nothing else. The web
  Continue page (`nativeResumeQuery` / `nativeResumeBody` in
  `harness_capabilities.js`) and the iOS chat menu (`ChatModel.canResumeNatively`)
  look up candidates, and send one, only for a harness whose host advertises it,
  and only a candidate belonging to that harness.
- **Discovery** is `HarnessAdapter.native_sessions`. harnessd asks every adapter
  that declares `native_resume` (or the requested one) and publishes
  `{..., harness}` rows, dropping items without a native session ID. Registering
  `native_sessions` without `native_resume` is rejected. Claude Code and Codex
  implement it with their existing JSONL readers; agy and DeepSeek do not, so
  they list no candidates while their resume operation still works.
- **Transcripts** are `HarnessAdapter.native_transcript` (Claude Code and Codex
  today). harnessd picks the adapter from the session's registry row; command
  sniffing remains only as a read-only label for terminals with no row.
- **Execution** is the adapter's `resume` operation (structured) or
  `LaunchRequest.native_session_id` (pty). Undeclared resume is refused before
  any driver starts, at harnessd, the hub, the web module and the iOS model.

**Unchanged:** `structured/manager.py`, harnessd routing, `capabilities.py`,
`metrics.py` (central API), `web/static/*`, and every iOS source. The gate is
that adding an adapter needs no edit in any of them.

Point 4 duplicates the adapter's `display_name` as the preset description.
Deriving presets from the registry, with an `executable` attribute on the
adapter, would remove it. That is optional and deferred.

## Contract changes the proof required

The fixture exposed these special cases and gaps. Each was fixed in the
contract, not worked around for the fixture:

1. **PTY launch ignored the matrix.** Any enabled preset could start as a raw
   terminal: an unregistered preset, or Claude Code, Codex and agy, whose
   adapters advertise only `structured`. harnessd now checks the published
   contract (`HarnessDaemonState.harness_contract`) and requires `pty` in it. A
   PTY adapter builds its command through `build_command(LaunchRequest)`. The
   daemon's own shell is the only matrix row without an adapter.
2. **Native resume was a harness-name table in harnessd.**
   `_native_resume_args` mapped `claude-code`, `codex` and `agy` to CLI flags
   for PTY launches, while structured launches ignored `native_resume`. That
   table is deleted. A structured launch now accepts
   `native_resume: {session_id}`, checks the `native_resume` capability, and
   calls the adapter's `resume` operation. A PTY adapter receives the session ID
   in `LaunchRequest`.
3. **The hub routed Continue and recovery from its own built-in list.**
   `metrics.py` used `_STRUCTURED_HANDOFF_HARNESSES` and
   `_RECOVERABLE_STRUCTURED_HARNESSES`, both derived from the hub's
   `BUILTIN_ADAPTERS`. A host-registered adapter the hub did not compile in
   could therefore not be recovered. A matrix-less host was routed by name.
   Both lists are gone:
   - Continue reads only the target host's v1 row. It prefers `structured` and
     requires `native_resume` for a native resume. It refuses matrix-less
     targets with an upgrade message.
   - Recovery always asks the host, whose adapter decides
     (`native_resume` and `recover_after_restart`).
4. **Undeclared operations reached the adapter or escaped the handler.**
   - Interrupt on an adapter without `interrupt` raised past the HTTP handler.
   - Turns accepted any of the four transport image types, whatever MIME types
     the adapter declared.
   - The manager passed images to `send_turn` when an adapter declared no
     attachments.
   - A model or effort for an adapter without a catalog failed as "Model
     choices are unavailable".

   Now the following refuse with `"<id> does not support <operation>"` before
   any file, row or provider call: interrupt, approvals, attachments (with
   their declared MIME types), model catalog and native resume. The manager
   repeats the attachment and resume checks as defense in depth.
5. **Envelope display names (additive v1 row field).** Rows now carry
   `display_name` from the adapter. Shell's is `Shell`. Central validates it as
   text of at most 256 characters and projects it. The web pickers and the iOS
   launch sheet show it, and fall back to the raw ID for hosts that predate it.
   It is presentation only, never identity. Older clients ignore it. iOS
   `HarnessPresentation` still maps known IDs to icons for session rows, which
   do not carry the envelope.

### Behavior changes

- Provider CLIs can no longer be started as raw PTY sessions through
  harnessd's API (`mode` omitted or `pty`). The web and iOS clients already
  launch them as structured sessions (#419/#420).
- Native resume runs as a structured session. The bare "latest" forms
  (`claude --continue`, `codex resume --last`) are gone. No client sent them;
  web candidates always carry a session ID.
- Continue onto a matrix-less (pre-#418) host is refused. Before, the hub
  routed it by harness name. Clients already withheld it.
- iOS gains a "Resume a native session" chat action, shown only when the
  session's host advertises structured launch and `native_resume` for its
  harness and its adapter discovered candidates.

## Residual harness-name code

None of this is on the drive path the fixture exercised. Each item is listed so
a future adapter does not trip over it:

| Location | Why it is keyed by name | Suggested home |
| --- | --- | --- |
| `daemon.py` `harness == "shell"` | Daemon-owned terminal, no adapter | Fine as is |
| `daemon.py` `_harness_name_for_command` | Read-only transcript label for a legacy terminal with no registry row | Delete once such rows age out |
| `auth.py` `CommandAuthAdapter._probe` | Parses Claude/Codex/agy status output | A parser supplied by the adapter's `auth_adapter` |
| `factory_observer.py` `factory_observer_command` | Rewrites Claude permission flags for unattended runs | An adapter hook for unattended policy |
| `usage.py` `CUMULATIVE_HARNESSES` | Codex reports cumulative token counts | Adapter `usage` metadata |
| `daemon.py` staging gate (`claude-code`, `codex`) | Staging credential provisioning exists only for these | Staging config |
| `DEFAULT_PRESETS["claude-code"].startup_gate_markers` | PTY seed gate. Unreachable now that Claude Code is not PTY-launchable | Move to a PTY adapter attribute or delete |

## Running the gate

```bash
uv run --extra dev pytest -q tests/test_adapter_extensibility.py \
  tests/test_web_harness_capabilities.py tests/test_harness_capabilities.py
swift test --package-path apps/drover/DroverKit --jobs 2 --filter Capabilit
```
