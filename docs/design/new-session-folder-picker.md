# New Session folder picker

## Purpose and research

Choose an existing working directory on the selected host without typing a path on a phone. Keep saved paths and manual entry. This is a bounded addition to the current launch flow, not a remote file manager.

Research checked 2026-10-10. Product documentation establishes the patterns below; the proposed mobile interaction is our synthesis, not a claim that every product implements every control.

| Reference | Useful pattern for Drover |
| --- | --- |
| [iOS Files](https://support.apple.com/en-gb/guide/iphone/iphe4bff8827/ios) | Browse locations and favorite folders, with familiar lists and search. Use large rows, a separate selection action, and native navigation back gestures. |
| [UIDocumentPicker directory access](https://developer.apple.com/documentation/uikit/providing-access-to-directories) | Folder selection is an explicit grant. The system picker sees File Providers, not an arbitrary Drover host, so a custom authenticated browser is needed. |
| [VS Code Remote Open Folder](https://code.visualstudio.com/docs/remote/ssh) | Connect to the host first, then choose a remote folder. Keep host context fixed while browsing. |
| [Codespaces](https://docs.github.com/en/codespaces/developing-in-a-codespace/opening-an-existing-codespace) | Repository/workspace context is a better starting point than filesystem root. |
| [Termius SFTP](https://www.termius.com/free-ssh-client-for-iphone) and [Blink Files integration](https://docs.blink.sh/advanced/files-app) | Remote folders should be browsable with mobile controls. Drover needs listing only, not transfer or file editing. |
| [Working Copy external repositories](https://workingcopyapp.com/manual/external-repos/) | Repository identity helps distinguish project directories. Show a Git badge based on metadata, without reading Git contents. |
| [Codex remote engineering](https://developers.openai.com/blog/mastering-codex-remote-for-engineering) and [Claude Code Remote Control](https://code.claude.com/docs/en/remote-control) | Host, repository, worktree and existing-session context reduce phone typing. Claude's documented flow starts from a trusted local project; it is not evidence of a general remote folder picker. |
| [JetBrains Gateway](https://blog.jetbrains.com/blog/2021/12/03/dive-into-jetbrains-gateway/) | Choose the connection and project directory before starting the IDE backend. |

Breadcrumbs show location without consuming the full screen. Recents first and pinned roots shorten common journeys. Search filters names in the current folder, never recursively. Native swipe back, explicit Up, 44-point minimum targets, and Git badges support one-handed use. No absolute device-local picker is presented as the remote host's filesystem.

## Recommended flow

Tapping the working-directory row opens a sheet tied to the selected host. The existing clock menu stays. A separate manual-entry action keeps the existing text completion behavior.

Entry state lists saved recent/favorite paths for this host, then allowed roots supplied by the daemon (home by default and explicitly configured project roots). Saved paths are navigation shortcuts, not authorization grants. Opening any saved path must pass the daemon policy. The browser starts at the entry screen instead of automatically browsing the last typed path.

Browse state has native push/back navigation, scrollable ancestor breadcrumb buttons, an Up action, directory rows with folder icons and Git badges, pull-to-refresh, and a name filter. Selecting a row drills in; Use this folder selects only the successfully loaded current directory and dismisses the sheet. Cancelling leaves the working directory unchanged. Native back restores the previous folder screen.

```
Choose folder                 Cancel
Recent and favorite folders
  [clock] project
Locations
  [folder] Home
  [folder] configured project root
New folder (disabled, coming later)

< Back          Choose folder
Home > Projects > project       Up
[ Filter folders                    ]
[folder] sources                    >
[folder] tools             Git      >
[ Use this folder                   ]
```

Loading shows a spinner and disables selection. A loaded empty folder says No folders; a filter with no matches says No matching folders. Permission denial says the folder is unavailable or outside allowed locations and offers Retry/back. Missing or non-directory paths are distinct from empty folders. Unreachable hosts say Host offline or unreachable and offer Retry; saved paths remain visible on the entry screen. Older hosts show an upgrade message. Authentication errors ask the user to check the connection credential. Error copy does not echo raw transport errors or private endpoint addresses.

Create folder is a disabled affordance with an accessibility explanation. Follow-up requires a scoped write permission, descriptor-relative creation, validated single-component names, allowed roots, no overwrite, and race tests. The present host model grants session execution but has no narrower filesystem-write capability, so adding a write endpoint is outside this small change.

## Data and interfaces

Today `LaunchModel` merges `/harness` cwd_suggestions with `DroverClient.completePath`, which calls hub GET `/harness/hosts/{host_id}/fs/complete?path=...`. `MetricsCollector.proxy_harness_fs_complete` uses `_proxy_harness_fs` and `_harness_request` for direct/relay routing. Daemon `_complete_directories` returns parent, entries (name/path), truncated and optional error. POST `/fs/exists` validates untagged favorites. Neither route is root constrained today. Keep their existing behavior to avoid changing manual completion in this feature.

Add GET `/harness/hosts/{host_id}/fs/list`, forwarded through the same routing choke point to `/fs/list?host_id=...`. Empty path returns allowed roots only. A path request returns lexically normalized absolute path, parent (null at an allowed root), roots, entries with name, path, is_dir=true, is_git_repo, hidden, and truncated. Git detection uses only the existence/type of a non-symlink `.git` entry, including worktree marker files; no file contents or Git subprocesses. Child counts and has_children are omitted: probing every descendant adds IO and permission ambiguity without improving drill-in. Every directory remains navigable, including empty ones.

Return at most 100 entries. Inspect at most 5,000 immediate entries, plus one to detect truncation, and flag truncation if either cap is hit. Sort the bounded scan by case-insensitive name and use a query filter before the response cap. No recursive search or full-directory child count. A truncated message explains the cap and invites filtering; scan-limited folders may still omit matches. Cursor paging is deferred until there is a measured need for directories beyond the scan budget.

Allowed roots come from daemon environment `DROVER_FOLDER_ROOTS` (platform path separator); unset defaults to home. Explicit empty configuration fails closed. Configuration belongs to the host operator and never comes from request input. Roots must be absolute paths without traversal or symlink components, including ancestors. Do not resolve a replaced root into a new authorized target. Hidden directories and all symlink entries are excluded. This stricter symlink policy also blocks in-root links, avoiding ambiguous breadcrumb destinations and escape races.

## Security and P0 dependencies

Require enabled hub authentication and the existing shared HTTP request authorization. Resolve host credentials through the existing `bearer_credential` helper and refuse host-scoped credentials bound to any other host or lacking a binding. Device credentials and the legacy operator credential retain fleet access. Profile/preflight credentials cannot list folders. Do not add another credential store or token resolver.

Require a configured daemon bearer token even when its other endpoints permit tokenless operation. Require the request host_id to match daemon identity. Keep `_harness_request` as the upstream routing boundary, including relay behavior. No path supplied by a caller can grant a root. Refuse `..` components, relative paths, NULs, paths outside roots, non-directories and symlink traversal. Open roots and descendants with directory descriptors and O_NOFOLLOW, then list/stat children relative to those descriptors, so replacing a path with an escaping symlink cannot redirect a listing. Return directory metadata only and no file contents.

`feat/p01-mcp-auth` at research time adds shared MCP/HTTP credential policy in commit `1125c3f`; this feature uses existing HTTP helpers and does not modify MCP or profile policy. P0.2 remains necessary for fleet-wide host registration binding, revocation/stream handling and per-host upstream credentials. The P0.1 commit changes no file touched by this implementation, so no direct textual conflict was found against that commit. The current direct proxy forwards the operator token and the current legacy deployment can reuse that token across hosts. This feature does not claim to fix that deployment-wide replay boundary. Its new route verifies destination identity and incoming host-credential binding, and must preserve P0.2's shared upstream credential resolver when integrated. No auth-disabled listing fallback is allowed. Likely integration overlap: web GET routing, metrics filesystem proxy and daemon state/routes; preserve the stronger policy from both branches.

## Implementation and verification plan

1. Commit this document before product changes.
2. Add daemon filesystem policy tests using temporary roots, then implement a focused descriptor-based listing module and authenticated route. Cover roots, directories-only, Git/hidden metadata, traversal, symlinks, files, permissions, caps, invalid input, missing/wrong tokens, tokenless mode and host mismatch.
3. Add hub binding/routing tests, then extend filesystem proxy and GET dispatch using existing auth resolution and transport.
4. Add strict Swift response types, client call, independently testable folder loading/filter/error state, and host-scoped saved shortcuts. Add the SwiftUI navigation sheet and manual-entry mode to LaunchView.
5. Run new server tests, existing filesystem suggestions/proxy tests, scoped Swift package tests and relevant simulator view tests, and build iOS. Inspect screenshots if a local fixture can exercise the UI without private host data. Review diff for privacy, auth regressions and whitespace. Commit implementation only after reporting actual verification results. No push, PR, merge or deploy.

## Implementation verification

Verified 2026-10-10 in the isolated feature checkout. No live hub or host was contacted. No full test suite was run.

| Check | Result |
| --- | --- |
| `uv run --extra dev pytest tests/test_fs_listing.py tests/test_fs_listing_hub.py tests/test_fs_completion.py tests/test_metrics.py -k 'fs_ or cwd_suggestions or completion or exists_' -q` | 65 passed, 170 deselected. Includes root replacement and during-open symlink races, disappearing children, hidden/Git metadata, caps, permissions and hub credential binding. |
| `uv run --extra dev pytest tests/test_web_auth.py -q` | 23 passed. Shared HTTP credential policy preserved. |
| Scoped Swift package tests: FolderBrowserTests, LaunchCwdCompletionTests and LaunchModelTests | 52 passed. Covers strict decoding, request encoding, selection, local filtering, breadcrumbs, retry, unavailable/offline/auth/permission states and saved paths independent of manual prefixes. |
| iOS simulator build for scheme `Drover` | Passed. Existing concurrency warnings in unrelated models remain. |
| Simulator scheme `DroverUITests`, only `FolderBrowserUITests`, parallel testing disabled | 2 passed. Native edge swipe back, navigation back button, Up, filter, selection, recents/manual-entry controls and error/offline selection guards. |
| Simulator scheme `Drover`, only `CwdSuggestionsStatusTests` | 4 passed. Existing simulator progress-view flattening warnings were emitted by these pre-existing render tests. |
| Black, isort and `git diff --check` | Passed for changed Python files and diff. |

Initial simulator attempts were interrupted by another checkout using the same simulator and app bundle. A dedicated simulator isolated the final successful runs. An initial UI assertion used the visible Back label instead of SwiftUI's BackButton accessibility identifier; corrected after inspecting the actual hierarchy. A read-only review found and helped close the configured-root replacement escape, child-deletion race and unknown-host copy error; the follow-up review had no remaining important or critical findings.

Screenshots from the final successful synthetic run were visually inspected. They contain fixture data only:

- [Entry with recents and roots](new-session-folder-picker-screenshots/folder-entry-recents-and-roots.png)
- [Browse with breadcrumb, Up and Git badge](new-session-folder-picker-screenshots/folder-browse-breadcrumb-and-git.png)
- [Empty folder, still selectable](new-session-folder-picker-screenshots/folder-empty.png)
- [Permission denied, selection disabled](new-session-folder-picker-screenshots/folder-permission-denied.png)
- [Offline host, Retry and selection disabled](new-session-folder-picker-screenshots/folder-host-offline.png)

VoiceOver labels and hints are supplied, but a spoken VoiceOver journey was not run. Pull-to-refresh uses SwiftUI refreshable; it was not separately exercised by the UI automation. Folder creation, cursor paging beyond the bounded scan, child counts and deployment-wide P0.2 credential replay protections remain deferred as described above.

Changed files:

| Area | Files |
| --- | --- |
| Host and hub | `src/drover/server/harness/folders.py`, `src/drover/server/harness/daemon.py`, `src/drover/server/metrics.py`, `src/drover/server/web/app.py` |
| Server tests | `tests/test_fs_listing.py`, `tests/test_fs_listing_hub.py` |
| iOS views | `apps/drover/Drover/Screens/Launch/LaunchView.swift`, `apps/drover/Drover/Screens/Launch/FolderBrowserSheet.swift` |
| iOS model and client | `apps/drover/DroverKit/Sources/DroverKit/FolderBrowserModel.swift`, `apps/drover/DroverKit/Sources/DroverKit/LaunchModel.swift`, `apps/drover/DroverKit/Sources/DroverKit/DroverClient.swift` |
| iOS tests and synthetic transport | `apps/drover/DroverKit/Tests/DroverKitTests/FolderBrowserTests.swift`, `apps/drover/DroverUITests/FolderBrowserUITests.swift`, `apps/drover/Drover/UITesting/FolderBrowserFixture.swift`, `apps/drover/Drover/UITesting/FixtureHubURLProtocol.swift` |
| Design and evidence | `docs/design/new-session-folder-picker.md` and the five linked PNGs |
