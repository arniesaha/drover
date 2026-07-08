You are writing a compact rolling handoff brief for an OPEN agent session.
Another agent may need to pick up this task mid-flight, without waiting for
the session to end. Output a JSON object only — no prose, no markdown fences.

Required keys:
  - "brief_md":          ≤2KB. The minimum context an incoming agent needs to resume: what the task is, what's been done, where things stand right now.
  - "last_user_req":     The user's most recent ask, in their own words if possible (1-2 sentences).
  - "current_objective": The single goal the agent is actively pursuing this turn.
  - "suggested_next":    1-3 concrete next actions for whoever picks this up.

Optional keys (include when supported by the events):
  - "files_touched":  array of file paths the session has read or edited so far.
  - "open_blockers":  short prose on what's blocking progress (errors hit, ambiguous spec, missing creds, etc.). Empty string if nothing is blocking.

Be specific over generic. Cite file paths, function names, error strings,
and decisions where the events mention them. Prefer the latest signal
over older signal when they conflict.

## Session metadata

session_id : {session_id}
agent_id   : {agent_id}
started_at : {started_at}
last_event : {ended_at}
events     : {event_count}

## Recent events (newest last)

{turns}
