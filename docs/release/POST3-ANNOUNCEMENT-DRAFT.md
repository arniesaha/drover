# Announcement draft - do not publish

I have finished a private release validation pass for Drover's MCP history reads. The important part was not a new feature. It was making replay and search take the verified lake-serving path without spending part of the request budget on redundant prechecks.

The private rollout was checked against an installed wheel, a rebuilt matching-root fixture, and an existing recorded Studio session. Replay and search both returned the known fixture marker after activation.

This is still a private validation result, not a public release announcement. Real recap and handoff remain gated on the Studio Claude CLI login, and the public release branch still needs to integrate the cumulative fixes onto current public main.
