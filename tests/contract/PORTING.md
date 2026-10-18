# Phase 1 contract porting ledger

This suite deliberately exercises only a launched daemon over HTTP, WebSocket,
and its configured hub connection.  Source assertions on `state.registry`,
`state.pty`, monkeypatched adapters, or monkeypatched worktree failures are
replaced only when the same result is observable at the public boundary.

| Source coverage | Contract coverage | Notes |
| --- | --- | --- |
| `test_harnessd_creates_shell_session` | `test_shell_create_get_list_and_terminate` | Registry/event internals omitted; lifecycle response is public. |
| `test_harnessd_lists_and_gets_live_sessions` | `test_shell_create_get_list_and_terminate` | HTTP inventory and lookup retained. |
| `test_a_repeat_create_with_a_client_key_does_not_spawn_a_second_session` | `test_client_session_key_is_idempotent` | Inventory is the observable duplicate fence. |
| `test_creates_without_a_client_key_are_still_independent` | `test_unkeyed_creates_are_distinct` | Public session IDs retained. |
| `test_a_repeated_handoff_create_returns_the_session_it_already_made` | `test_handoff_source_is_idempotent_but_parent_is_not` | Registry count replaced by inventory. |
| `test_delegated_children_of_one_parent_are_separate_sessions` | `test_handoff_source_is_idempotent_but_parent_is_not` | Parent linkage is verified through separate public creates. |
| Tests requiring `monkeypatch`, direct `state.*`, fake registry locks, or timing control | dropped for now | They need a public fault-injection seam; a Rust acceptance test must not import daemon internals. |
