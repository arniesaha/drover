# Public Copy and Issue Hygiene Design

**Date:** 2026-08-08  
**Scope:** Repository metadata, public documentation, and active GitHub issues

## Goal

Give Drover a direct, natural public voice. Remove em dashes and irrelevant
legacy product positioning from current public prose, replace formulaic or
promotional writing with concrete language, and bring the open issue tracker in
line with the code and runtime state that now exist.

This is an editorial and project-hygiene change. It must not alter runtime
behavior, compatibility contracts, captured protocol data, or historical
records without a current public-surface reason.

## Current State

The repository is public and `main` is protected. The GitHub description still
uses an em dash and a parenthetical reference to an earlier product name. The
README and current documentation are technically accurate, but the complete
public surface has not had one consistent editorial pass.

There are 497 em-dash characters across 148 tracked files. Most are in source
comments, UI strings, tests, protocol captures, or fixtures. A repository-wide
replacement would create noisy diffs and could change behavior or historical
evidence. The active public tracker currently has 15 open issues, several of
which may be resolved, superseded, operationally stale, or worded for a private
project rather than public contributors.

## Approaches Considered

### Targeted public-surface editorial pass

This is the selected approach. It covers current public prose and active issue
content while leaving implementation comments, product strings, tests, runtime
prompts, and captured fixtures unchanged. It provides a consistent landing
experience without obscuring technical history or creating unrelated code
churn.

### Mechanical repository-wide replacement

This would remove every em dash from all tracked files. It is rejected because
the majority occur outside public write-ups. Rewriting test fixtures, protocol
captures, user-visible strings, and implementation comments would add risk
without improving the repository landing experience.

### Metadata-only cleanup

This would update only the GitHub description. It is rejected because the user
asked for a broader README, documentation, and issue audit, and because partial
editing would leave the public voice inconsistent.

## Editorial Scope

Tracked public prose includes:

- `README.md`
- Markdown under `docs/`
- `apps/drover/README.md`
- contributor-facing and operator-facing Markdown under `skills/`
- any root-level contributor or security Markdown added before implementation

External public prose includes:

- the GitHub repository description and related current metadata
- titles and bodies of open issues
- new issue status and closure comments written during this audit
- the body of the new cleanup pull request

The cleanup excludes:

- source-code comments and doc comments
- UI and error-message strings
- `src/drover/prompts/`, because those files affect runtime model behavior
- tests, snapshots, captured protocol output, and fixtures
- closed issues and merged pull requests, except for a separate material
  disclosure finding
- identifiers and compatibility fixtures that must remain byte-compatible

## Editorial Rules

Every in-scope em dash will be replaced according to sentence meaning, using a
period, comma, colon, or parentheses. A blind substitution with a hyphen is not
acceptable.

Current marketing copy will not describe Drover by reference to an earlier
product name. Historical names remain only where they identify a real
compatibility surface, such as `nexus.*` telemetry attributes or storage keys.
Those references must explicitly say that new public interfaces use Drover.

The editorial pass will prefer:

- direct subject and verb constructions;
- concrete behavior and supported boundaries;
- short paragraphs with one purpose;
- exact component, command, file, and protocol names;
- explicit limitations instead of promotional claims.

The pass will remove or rewrite:

- decorative slogans repeated after the primary tagline;
- canned contrasts such as “not just X, but Y”;
- empty intensifiers such as “powerful,” “robust,” or “seamless” when they add
  no measurable meaning;
- generic introductions, conclusions, and transition filler;
- claims that are broader than the current implementation or support boundary.

The main tagline, technical terminology, networking limitations, and local-first
positioning remain. Hyphens inside established compounds such as `local-first`
and `coding-agent` are not em dashes and are not part of this cleanup.

## Repository Metadata

The GitHub description will become:

> Drive your coding-agent fleet from your pocket. A local-first cockpit and
> context store for CLI coding agents.

The description contains no legacy-positioning parenthetical and makes two
current, verifiable claims. Homepage and topics will be reviewed for accuracy,
but no value will be invented merely to fill an empty field.

## Issue Audit

Each of the 15 open issues will be reviewed against current `main`, merged pull
requests, tests, and live state when the issue is operational. Titles alone are
not evidence.

Every issue will receive one classification:

1. **Resolved:** merged code, tests, or verified runtime behavior satisfy the
   issue. Add a short evidence comment and close it.
2. **Superseded:** another issue or merged pull request now owns the work. Link
   that record, explain the relationship, and close the obsolete issue.
3. **Still valid:** keep it open. Rewrite the title or body only as needed to
   state current scope, status, constraints, and acceptance criteria. Add or
   correct labels when they improve triage.
4. **Operational follow-up:** keep it open unless current live evidence proves
   resolution. Remove private-machine wording and describe the reproducible
   product or deployment concern instead.

No issue will be closed simply because it is old, because related code changed,
or because its title sounds complete. Closure comments will name the deciding
commit, pull request, test, or runtime check. Issue #50 closes only after the
self-hosted runner passes its allowed-job, rejected-PR, cleanup, and restart
proofs.

Before external edits, affected issue records will be saved to a private,
git-ignored audit workspace. Edits will be read back and checked for exact state
and public-copy rules. The backup is for recovery and will never be committed.

## Regression Checks

`scripts/check_public_release.py` will gain path-scoped public-copy rules with
tests. The rules will reject:

- Unicode em-dash characters in tracked public-prose paths;
- current marketing language that positions Drover as formerly using the
  earlier product name.

The rules will not scan source comments, runtime prompts, tests, or captured
fixtures for punctuation. Existing compatibility-name rules and allowlists
remain responsible for technical `nexus.*` contracts.

Human review remains the gate for tone. A word blacklist cannot reliably detect
formulaic writing without false positives, so the scanner will enforce only the
two objective requirements.

## Rollout Sequence

1. Create a fresh isolated branch from protected `origin/main`.
2. Add failing scanner tests for public em dashes and legacy-positioning copy.
3. Implement the path-scoped scanner rules.
4. Audit and rewrite tracked public prose.
5. Run link checks, the public-release scanner, focused scanner tests, and
   proportionate repository verification.
6. Open a pull request and inspect the complete prose diff for changed meaning.
7. Require the protected hosted Python and iOS checks to pass, then merge.
8. Update the GitHub description and read it back exactly.
9. Audit all open issues, snapshot affected records privately, apply evidence
   based closures or updates, and read every change back.
10. Verify the final issue counts, labels, description, and objective copy rules.

Tracked documentation changes are merged before issue and metadata edits so the
repository itself remains the reviewable source of editorial policy. External
issue changes use evidence from the merged commit and cannot bypass protected
`main`.

## Verification

Completion requires:

- zero em dashes in defined tracked public-prose paths;
- no current public marketing copy that describes Drover through the earlier
  product name;
- every retained `nexus.*` reference classified as a compatibility contract;
- exact read-back of the new GitHub description;
- an audit record for all 15 issues that were open at design time;
- evidence comments for every closed or superseded issue;
- current status, scope, and acceptance criteria for every edited open issue;
- zero broken local Markdown links;
- a clean public-release scanner and focused regression tests;
- passing protected hosted checks on the cleanup pull request;
- clean local and remote branch state after merge.

## Out of Scope

- Rewriting source comments, doc comments, UI copy, tests, fixtures, snapshots,
  or captured protocol records solely to change punctuation.
- Renaming `nexus.*` compatibility keys or changing stored data contracts.
- Rewriting closed issue or merged pull-request history for style.
- Closing roadmap or operational issues without current evidence.
- Adding new branding, taglines, screenshots, features, or architecture changes.
