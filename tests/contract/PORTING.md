# Phase 1 contract porting ledger

Every `tests/test_harness_daemon.py` test is accounted for below. Contract
tests communicate solely with a launched daemon; dropped entries name the
missing public seam rather than importing daemon internals.

| Source test | Contract test / disposition |
| --- | --- |
| `test_run_harnessd_closes_auth_flows_on_shutdown` | dropped: requires a purpose-built provider login executable; no stable public fixture protocol yet |
| `test_bound_host_in_direct_mode_refuses_startup_with_relay_guidance` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_harnessd_cli_passes_content_consent_config_to_server` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_model_catalog_route_is_host_scoped_and_forceable` | `test_model_catalog_route_is_host_scoped_and_forceable` |
| `test_model_catalog_service_is_created_lazily_once` | dropped: `GET /model-catalog` can establish its response and refresh semantics but cannot reveal service allocation count; observing this needs an injected catalog factory |
| `test_model_catalog_route_rejects_malformed_query` | `test_public_query_error_contract` |
| `test_model_catalog_route_rejects_unknown_or_disabled_harness` | `test_public_query_error_contract` |
| `test_model_catalog_route_requires_bearer_token` | `test_public_http_routing_contract` |
| `test_model_catalog_route_returns_safe_degraded_envelope` | `test_model_catalog_route_returns_safe_degraded_envelope` |
| `test_default_model_catalog_service_uses_only_enabled_resolved_presets` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_model_catalog_is_invalidated_after_auth_mutations_and_successful_poll` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_model_preference_validation_rejects_launch_before_side_effects` | `test_model_preference_validation_rejects_launch_before_side_effects` |
| `test_model_preference_validation_keeps_null_launch_defaults` | `test_model_preference_validation_keeps_null_launch_defaults` |
| `test_model_preference_validation_passes_codex_turn_preferences_unchanged` | `test_model_preference_validation_passes_codex_turn_preferences_unchanged` |
| `test_duplicate_turn_skips_attachment_and_preference_side_effects` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_model_preference_validation_preserves_opaque_identifier_whitespace` | `test_model_preference_validation_preserves_opaque_identifier_whitespace` |
| `test_model_preference_validation_rejects_turn_before_attachment_or_driver` | `test_model_preference_validation_rejects_turn_before_attachment_or_driver` |
| `test_initial_structured_executable_error_closes_entry_and_cleans_worktree` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_missing_structured_executable_turn_returns_json_and_marks_session_errored` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_client_turn_id_requires_a_uuid` | `test_client_turn_id_requires_a_uuid` |
| `test_model_preference_validation_discards_claude_turn_overrides` | `test_model_preference_validation_discards_claude_turn_overrides` |
| `test_cli_harnessd_help_documents_core_options` | `test_harnessd_cli_help_documents_core_options` |
| `test_skinny_harnessd_entrypoint_documents_core_options` | `test_skinny_harnessd_entrypoint_documents_core_options` |
| `test_daemon_can_register_host_with_central_server` | `test_launcher_announces_bound_port_and_registers` |
| `test_reconnecting_daemon_reconciles_revoked_consent_from_registration` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_relay_daemon_registers_itself_as_relay_connected` | `test_relay_hello_and_loopback_sessions_request` |
| `test_wire_event_pusher_pushes_structured_events_to_central` | `test_structured_lifecycle_and_event_push_contract` |
| `test_wire_event_pusher_is_noop_without_central_url_or_token` | dropped: the launcher fixture always supplies both `--central-url` and `--host-token`; proving the no-op requires a second launch topology without either value |
| `test_harnessd_health_and_capabilities` | `test_harnessd_health_and_capabilities` |
| `test_lifecycle_worktree_inventory_is_unavailable_before_first_observation` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_lifecycle_worktree_inventory_returns_cached_snapshot_while_refresh_is_slow` | dropped: requires deterministic control of an internal dependency or timing window |
| `test_lifecycle_worktree_inventory_keeps_last_good_snapshot_on_refresh_failure` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_lifecycle_worktree_inventory_does_not_overlap_hung_refreshes` | dropped: requires deterministic control of an internal dependency or timing window |
| `test_harnessd_provider_usage_returns_cached_snapshot_while_probe_is_slow` | dropped: requires deterministic control of an internal dependency or timing window |
| `test_provider_usage_background_refresh_keeps_last_good_snapshot_on_failure` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_provider_usage_hung_probe_does_not_block_other_providers` | dropped: requires deterministic control of an internal dependency or timing window |
| `test_provider_usage_probe_slower_than_old_bound_completes_with_new_bound` | dropped: requires deterministic control of an internal dependency or timing window |
| `test_provider_usage_does_not_reenter_hung_probe_and_publishes_late_success` | dropped: requires deterministic control of an internal dependency or timing window |
| `test_harnessd_provider_usage_requires_auth_and_reports_unavailable_accounts` | `test_harnessd_provider_usage_requires_auth_and_reports_unavailable_accounts` |
| `test_harnessd_provider_usage_uses_configured_agy_probe` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_harnessd_provider_usage_reports_claude_windows` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_harnessd_provider_usage_keeps_plan_label_on_a_failing_anthropic_card` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_harnessd_provider_usage_probes_codex_at_its_resolved_executable` | `test_harnessd_provider_usage_probes_codex_at_its_resolved_executable` |
| `test_harnessd_provider_usage_reports_codex_cli_not_found` | `test_harnessd_provider_usage_reports_codex_cli_not_found_is_never_silent` |
| `test_harnessd_content_bundle_requires_auth_and_is_disabled_by_default` | `test_public_http_routing_contract` |
| `test_harnessd_content_bundle_requires_configured_token_even_when_enabled` | dropped: the launcher always passes `--host-token`; a no-token fixture is needed to exercise `/advisory/content-bundle` after enabling consent |
| `test_running_harnessd_applies_live_consent_and_revoke_without_restart` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_content_consent_override_survives_restart_and_rejects_stale_enable` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_unknown_epoch_is_idempotent_disabled_and_cannot_enable` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_standalone_config_bootstrap_cannot_override_durable_revoke` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_content_consent_control_is_fail_closed_without_daemon_token` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_harnessd_content_bundle_accepts_only_configured_target_ids_and_is_ephemeral` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_harnessd_content_version_is_authenticated_hashes_only_and_no_store` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_harnessd_content_bundle_rejects_oversized_request_before_parsing` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_harnessd_auth_status_route` | `test_public_route_validation` |
| `test_harnessd_auth_start_poll_and_cancel` | dropped: requires a purpose-built provider login executable; no stable public fixture protocol yet |
| `test_harnessd_auth_routes_require_bearer_token` | `test_public_http_routing_contract` |
| `test_harnessd_auth_status_unknown_harness_returns_404` | `test_public_http_routing_contract` |
| `test_harnessd_auth_status_unavailable_returns_404` | dropped: fake `claude` and `agy` executables make both providers available; a configurable unavailable-auth CLI fixture is needed |
| `test_harnessd_auth_status_decodes_harness_path` | `test_public_route_validation` |
| `test_harnessd_auth_flow_unknown_id_returns_404` | `test_public_http_routing_contract` |
| `test_harnessd_auth_start_unsupported_returns_400` | `test_auth_public_error_routes` |
| `test_harnessd_auth_start_launch_failure_returns_structured_500` | dropped: requires a purpose-built provider login executable; no stable public fixture protocol yet |
| `test_resolve_harness_presets_enables_available_login_shell_clis` | dropped: unit-level filesystem/parser discovery has no daemon HTTP/WS operation |
| `test_resolve_harness_presets_records_resolved_executable` | dropped: unit-level filesystem/parser discovery has no daemon HTTP/WS operation |
| `test_resolve_harness_presets_clears_the_executable_when_the_cli_disappears` | dropped: unit-level filesystem/parser discovery has no daemon HTTP/WS operation |
| `test_resolve_harness_presets_discovers_nvm_clis_and_preserves_node_path` | dropped: unit-level filesystem/parser discovery has no daemon HTTP/WS operation |
| `test_resolve_harness_presets_enables_versioned_claude_cli` | dropped: unit-level filesystem/parser discovery has no daemon HTTP/WS operation |
| `test_native_resume_launches_through_the_structured_adapter` | dropped: unit-level filesystem/parser discovery has no daemon HTTP/WS operation |
| `test_discovers_claude_native_resume_sessions` | dropped: unit-level filesystem/parser discovery has no daemon HTTP/WS operation |
| `test_reads_claude_jsonl_native_transcript` | dropped: unit-level filesystem/parser discovery has no daemon HTTP/WS operation |
| `test_reads_codex_jsonl_native_transcript` | dropped: unit-level filesystem/parser discovery has no daemon HTTP/WS operation |
| `test_discovers_codex_native_resume_sessions` | dropped: unit-level filesystem/parser discovery has no daemon HTTP/WS operation |
| `test_native_history_metadata_refuses_sources_beyond_its_configured_hard_cap` | dropped: unit-level filesystem/parser discovery has no daemon HTTP/WS operation |
| `test_harnessd_creates_shell_session` | `test_shell_create_get_list_and_terminate` |
| `test_harnessd_lists_and_gets_live_sessions` | `test_shell_create_get_list_and_terminate` |
| `test_harnessd_listing_merges_pty_registry_and_manager_only_sessions` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_harnessd_listing_registry_lookups_do_not_scale_with_session_count` | dropped: requires deterministic control of an internal dependency or timing window |
| `test_harnessd_session_listing_bounds_history_but_keeps_live_sessions_first` | `test_harnessd_session_listing_bounds_history_but_keeps_live_sessions_first` |
| `test_harnessd_session_listing_cursor_pages_through_history_once` | `test_harnessd_session_listing_cursor_pages_through_history_once` |
| `test_harnessd_session_listing_rejects_a_forged_cursor` | `test_public_query_error_contract` |
| `test_harnessd_session_listing_cap_matches_the_hub_fleet_render` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_harnessd_reconciles_exited_sessions_out_of_live_inventory` | `test_harnessd_reconciles_exited_sessions_out_of_live_inventory` |
| `test_harnessd_reconciles_orphaned_structured_sessions_on_startup` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_harnessd_reconciles_orphaned_pty_sessions_on_startup` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_harnessd_recovers_restart_lost_structured_session` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_harnessd_recovery_is_idempotent_when_session_is_live` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_harnessd_recovers_claude_with_native_resume_command` | dropped: unit-level filesystem/parser discovery has no daemon HTTP/WS operation |
| `test_harnessd_recovers_agy_onto_its_conversation` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_harnessd_refuses_turns_while_draining_for_an_update` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_harnessd_concurrent_recovery_creates_one_driver` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_harnessd_terminate_waits_for_recovery_and_wins` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_harnessd_recovery_rejects_unsupported_or_incomplete_session` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_harnessd_recovery_rejects_terminated_session` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_harnessd_recovery_rejects_generic_errored_session` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_harnessd_can_terminate_restart_lost_structured_session` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_harnessd_terminates_live_session_and_updates_registry` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_harnessd_delete_terminates_live_session` | `test_public_session_mutation_contract` |
| `test_harnessd_sessions_cors_preflight_and_post_headers` | `test_harnessd_sessions_cors_preflight_and_post_headers` |
| `test_harnessd_creates_in_memory_session_when_registry_is_locked` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_harnessd_rejects_disabled_harness` | `test_public_route_validation` |
| `test_enabled_preset_is_not_terminal_launchable_without_advertised_pty` | `test_public_route_validation` |
| `test_daemon_rejects_unauthenticated_when_token_set` | `test_auth_and_top_level_error_envelopes` |
| `test_daemon_accepts_bearer` | `test_auth_and_top_level_error_envelopes` |
| `test_daemon_healthz_open` | `test_launcher_announces_bound_port_and_registers` |
| `test_daemon_terminal_attach_rejects_unauthenticated` | `test_daemon_terminal_attach_rejects_unauthenticated` |
| `test_structured_session_full_lifecycle` | `test_public_session_mutation_contract` |
| `test_structured_session_inventory_includes_run_preferences` | `test_public_session_mutation_contract` |
| `test_claude_turn_cannot_change_persistent_session_preferences` | `test_model_preference_validation_discards_claude_turn_overrides` |
| `test_structured_turn_appends_user_input_and_seq_is_monotonic` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_initial_prompt_with_image_saves_file_and_records_attachment` | `test_initial_prompt_with_image_saves_file_and_records_attachment` |
| `test_structured_command_preferences_map_to_cli_flags` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_turn_with_image_saves_file_and_appends_path` | `test_turn_with_image_saves_file_and_appends_path` |
| `test_image_only_turn_is_accepted` | `test_image_only_turn_is_accepted` |
| `test_turn_with_bad_base64_is_rejected` | `test_structured_launch_rejects_invalid_turn_input` |
| `test_turn_with_unsupported_media_type_is_rejected` | `test_structured_launch_rejects_invalid_turn_input` |
| `test_empty_turn_still_rejected` | `test_empty_turn_still_rejected` |
| `test_structured_unknown_harness_rejected` | `test_structured_launch_validation_contract` |
| `test_structured_launch_cannot_bypass_a_missing_adapter` | `test_structured_launch_cannot_bypass_a_missing_adapter` |
| `test_structured_launch_rejects_unknown_worktree_policy` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_structured_permission_without_approval_channel_returns_400` | `test_public_query_error_contract` |
| `test_structured_interrupt_unknown_session_is_404` | `test_public_query_error_contract` |
| `test_structured_terminate_closes_driver_and_marks_terminated` | `test_public_session_mutation_contract` |
| `test_structured_codex_session_runs_in_worktree` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_structured_codex_session_non_git_cwd_runs_in_place` | `test_structured_codex_session_non_git_cwd_runs_in_place` |
| `test_structured_claude_session_stays_in_place` | `test_structured_claude_session_stays_in_place` |
| `test_structured_session_rejects_ask_permission_mode` | `test_structured_launch_validation_contract` |
| `test_structured_session_stores_default_permission_mode` | `test_public_session_mutation_contract` |
| `test_structured_session_stores_explicit_auto_permission_mode` | `test_public_session_mutation_contract` |
| `test_structured_session_rejects_unknown_permission_mode` | `test_structured_launch_validation_contract` |
| `test_harnessd_routes_typed_code_into_a_pty_login_flow` | dropped: requires a purpose-built provider login executable; no stable public fixture protocol yet |
| `test_harnessd_refuses_to_start_a_terminal_only_sign_in` | dropped: requires a purpose-built provider login executable; no stable public fixture protocol yet |
| `test_every_offered_preset_can_actually_be_driven` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_observe_only_sources_are_never_drive_targets` | dropped: checked HTTP, terminal WebSocket, relay WebSocket, and CLI launch seams; no request can establish this internal invariant |
| `test_a_repeated_handoff_create_returns_the_session_it_already_made` | `test_handoff_source_is_idempotent_but_parent_is_not` |
| `test_a_handoff_from_a_different_source_still_creates_its_own_session` | `test_public_session_mutation_contract` |
| `test_delegated_children_of_one_parent_are_separate_sessions` | `test_public_session_mutation_contract` |
| `test_a_repeat_create_with_a_client_key_does_not_spawn_a_second_session` | `test_client_session_key_is_idempotent` |
| `test_structured_create_retry_with_ios_client_key_returns_one_session` | `test_public_session_mutation_contract` |
| `test_creates_without_a_client_key_are_still_independent` | `test_unkeyed_creates_are_distinct` |
| `test_create_session_answers_400_when_the_staging_key_is_unusable` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_recover_answers_409_when_the_staging_key_is_unusable` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_structured_launch_refuses_when_worktree_isolation_fails` | dropped: requires pre-seeding or faulting durable daemon state before startup |
| `test_work_run_key_lookup_is_read_only_and_includes_archived_session` | dropped: requires deterministic control of an internal dependency or timing window |
