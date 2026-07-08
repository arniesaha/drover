You are writing a project-level brief for handoff. The next agent picking
up work in this repository needs to know, in under 30 seconds: what this
project is, what state it's currently in, and what's pending. Output a
JSON object only — no prose, no markdown fences.

Required keys:
  - "brief_md":         ~5 sentences. What the project IS (purpose, stack, where it lives), and the current macro-state.
  - "recent_themes_md": ~3 sentences on what has been the focus across the last few sessions. Cite recurring decisions or directions.
  - "key_files":        array of file paths that show up across multiple sessions and shape what the project does.
  - "open_questions":   array of unresolved questions, deduplicated across the input summaries.
  - "next_steps_md":    1-3 concrete next actions for the next agent.

Be specific over generic. Cite file paths, function names, and decisions
where the input mentions them. If the recent summaries conflict, flag the
conflict instead of papering over it.

## Project metadata

project_key   : {project_key}
repo_owner    : {repo_owner}
repo_name     : {repo_name}
session_count : {session_count}
last_activity : {last_activity_at}

## Recent session summaries (newest first)

{summaries}
