# Folder picker follow-ups

Follow-up to PR #563, based on main `1e69016`. The PR comment and review APIs returned no threads when read for this work. The PR body explicitly deferred create folder, cursor paging, and child counts. This change implements the first two items.

## Folder creation

Authenticated `POST /harness/hosts/{host_id}/fs/create` accepts a JSON object with `path` (the current directory) and `name` (one new folder component). The hub injects the route's host identity. The daemon requires its configured bearer token and matching host identity. Existing shared authorization rejects unbound or differently bound host credentials, profile credentials, preflight credentials, and auth-disabled hubs.

The parent uses the same allowed-root, traversal, hidden-folder, and no-symlink policy as browsing. Every ancestor opens with `O_NOFOLLOW`. Descriptor-relative `mkdir` creates a folder with mode `0700`, subject to the host's umask. Names must be nonempty, at most 255 UTF-8 bytes, without surrounding whitespace, a leading dot, slashes, backslashes, control characters, or surrogate characters. Creation never overwrites an existing directory, file, or symlink. Success is `201` with `{name, path}`; invalid names are `400 invalid_name`, existing items are `409 folder_exists`, and policy or permission failures are `403`.

The iOS picker offers New folder only inside a successfully loaded directory. Its form reports creation errors and supports retry. Create and use folder selects the returned child and dismisses the picker. While a write is pending, duplicate creation and interactive dismissal are disabled. The client checks the returned path against the requested parent and name before selecting it.

## Paging

`GET /harness/hosts/{host_id}/fs/list` adds `limit` (1 through 100, default 100) and `cursor` (empty on the first page). Responses add nullable `next_cursor`. Existing `truncated` remains true while another page exists, preserving compatibility with older clients. New clients also accept listings from older hosts without `next_cursor`.

Each page returns at most `limit` visible directory entries and examines metadata for at most 5,000 new directory children. A sparse filter can produce an empty page with a continuation cursor. Pagination resumes filesystem enumeration order, sorting names within each page. It never materializes the entire directory. Resuming a later page re-enumerates earlier names without reading their metadata, so very late pages still incur enumeration overhead.

Cursors bind the offset to the path, filter, device, inode, and directory modification/change timestamps. A replaced or changed directory, changed filter, or malformed cursor produces `400 invalid_cursor`. Reload starts a new listing. Directory mutations are not a snapshot guarantee, and clients must reload when a continuation becomes stale.

The iOS list loads a continuation when its bottom progress row appears. It deduplicates paths, prevents simultaneous continuation requests, resets pagination on filter refresh, and discards responses from an older refresh generation. Page errors preserve loaded entries and provide retry and reload. A stale cursor explicitly explains that the folder changed.

New controls use accessibility labels, at least 44pt targets, and system text styles without fixed text heights.

## Verification

Only scoped checks were run:

- `uv run --extra dev pytest -q tests/test_fs_listing.py tests/test_fs_listing_hub.py`: 76 passed. Includes creation validation, permissions, no overwrite, symlink swaps, bearer and host binding, credential scopes, paging beyond the scan budget, cursor binding, stale cursors, and hub forwarding.
- `swift test --package-path apps/drover/DroverKit --jobs 2 --filter FolderBrowserTests`: 16 passed. Includes host/path request encoding, creation success and failures, returned-path verification, page append/deduplication, filter reset, stale-cursor handling, duplicate load prevention, and an in-flight page after refresh.
- `xcodebuild -project apps/drover/Drover.xcodeproj -scheme Drover -destination 'generic/platform=iOS Simulator' -derivedDataPath /tmp/drover-folder-followups-build CODE_SIGNING_ALLOWED=NO build`: passed.
- Black, isort, and `git diff --check`: passed for the changed files.

No live host data was used. No push, PR, merge, or deploy was performed.

## Deferred scope

Child counts, rename/delete, global alphabetical ordering across pages, directory snapshots, and deployment-wide credential work remain outside this change. Spoken VoiceOver verification, simulator UI journeys for the new controls, and live UAT throwaway-session creation were not run. The new unit tests and simulator build cover this change's requested verification scope.
