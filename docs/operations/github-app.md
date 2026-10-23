# Agent GitHub identity (drover-bot)

Issue #786. Agents write to GitHub as a GitHub App, `drover-bot[bot]`, never as
the operator. That lets the reconciler tell an operator directive (an
`autonomy:*` label, a hard stop) from an agent writing one for itself.

## 1. Create the App (operator, once)

Open this prefilled registration URL, check the values, then press **Create
GitHub App**:

```text
https://github.com/settings/apps/new?name=drover-bot&description=Drover+agent+identity%3A+reconciler+and+worker+writes+%28comments%2C+labels%2C+PRs%2C+merges%29.&url=https%3A%2F%2Fgithub.com%2Farniesaha%2Fdrover&public=false&webhook_active=false&issues=write&pull_requests=write&contents=write&checks=write&actions=read&statuses=read&metadata=read
```

| Permission | Access | Why |
| --- | --- | --- |
| Issues | write | reconciler comments and labels, worker progress comments |
| Pull requests | write | open, update, mark ready and merge PRs |
| Contents | write | push branches, merge |
| Checks | write | publish agent-created check results when needed; inspect exact-head CI |
| Actions, Commit statuses | read | workflow run URLs/logs and exact-head CI evidence |
| Metadata | read | required by GitHub |

Not granted: `workflows` (agents cannot edit `.github/workflows/`), and
`actions: write` (no reruns). Add either on the App page later if a worker
genuinely needs it. No webhook: the hub decision loop (#784) can turn the App
webhook on later (events `issues`, `issue_comment`, `label`, `pull_request`,
`check_suite`, `workflow_run`) once it has a reachable endpoint.

After creating it:

1. Note the **App ID** on the App's settings page.
2. **Generate a private key** (bottom of the same page). GitHub downloads a
   `.pem` file once.
3. **Install App** → *Only select repositories* → the repos agents work in
   (at least `arniesaha/drover`).

### Alternative: manifest flow

`github-app-manifest.html` next to this file posts the same settings as a
manifest. GitHub then redirects to issue #786 with `?code=...` in the URL;
paste that URL to the orchestrator within an hour and it exchanges the code
(`POST /app-manifests/{code}/conversions`) for the App ID and private key, so
nobody has to move a `.pem` by hand.

## 2. Store the key (never in git)

On every host that writes as an agent (the reconciler host and each worker
host):

```sh
install -d -m 700 ~/.drover
install -m 600 drover-bot.private-key.pem ~/.drover/github-app.pem
cat > ~/.drover/github-app.env <<'EOF'
DROVER_GH_APP_ID=<app id>
DROVER_GH_APP_KEY_FILE=~/.drover/github-app.pem
EOF
chmod 600 ~/.drover/github-app.env
```

`DROVER_GH_APP_INSTALLATION_ID` is optional; the helper discovers it from the
repo. Keep a copy of the ID and key in the operator's secret store as the
backup. The `.pem` must never be committed, pasted into an issue, or put in a
worker brief.

## 3. Use it

```sh
python3 scripts/github_app_token.py --repo owner/name   # prints a token
```

- Exit `0`: token on stdout, cached in `~/.cache/drover/github-app-token.json`
  (mode 600) until five minutes before expiry (tokens last one hour).
- Exit `3`: no App configured. Keep the current credentials; behaviour is
  unchanged from before the App existed.
- Exit `1`: configured but minting failed (stderr says why).

Workers wrap every GitHub write (gh, git push) like this, re-running it before
each batch because tokens expire:

```sh
t=$(python3 scripts/github_app_token.py --repo owner/name) && export GH_TOKEN="$t"
```

`gh` reads `GH_TOKEN`, so comments, labels and PRs appear as
`drover-bot[bot]`. A worker that gets exit `1` stops and reports instead of
falling back to the operator's login.

## 4. Authority rules (reconciler today, hub decision loop later)

The operator login is `arniesaha` (`DROVER_OPERATOR_LOGIN`).

1. **Approval labels are operator-only.** An `autonomy:*` label counts only if
   the latest `labeled` event for it was by the operator. Otherwise the
   reconciler ignores the label (the issue has no approved stage) and logs it.
2. **Holds can't be lifted by agents.** If `blocked:human` or `stalled` was
   last removed by anyone but the operator, it still counts as present. Agents
   may *add* these (that only restricts).
3. **Hard stops are operator-only.** `Hard stop:` lines count from the issue
   body when the operator wrote it, and from operator comments. Others are
   ignored and logged.
4. **Done needs evidence.** A `Drover-Done: <stage>` line counts only if the
   same line carries a commit SHA (7-40 hex) and a GitHub Actions run URL
   (`https://github.com/<owner>/<repo>/actions/runs/<id>`). Bare claims are
   ignored and logged.
5. **Ignored means logged, not reverted.** The reconciler never undoes an
   agent's label change; it reports it in its `ignored` output and an append-only
   authority log, and the operator decides.

Without an App configured every write is still the operator's login, so rules
1-3 accept everything exactly as before; rule 4 applies regardless.
