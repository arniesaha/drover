# GitHub Actions runner model

Drover uses GitHub-hosted runners for repository CI. The project does not
operate a self-hosted runner attached to this public repository: a workflow
configuration mistake could otherwise run untrusted code on the machine that
holds the local fleet, DuckDB data, and operator credentials.

## Required checks

Pull requests to `main` run the Linux validation workflow in
[`ci.yml`](../.github/workflows/ci.yml) and the iOS workflow in
[`ios.yml`](../.github/workflows/ios.yml). Both run on GitHub-hosted runners.
The CI workflow includes a guard that rejects any reintroduction of a
`self-hosted` runner label.

## Optional macOS coverage

[`macos.yml`](../.github/workflows/macos.yml) provides extra Python coverage
on a GitHub-hosted macOS runner. It is intentionally manual because hosted
macOS minutes are expensive and the required Linux and iOS checks already
cover every pull request.

Run it from a reviewed `main` branch before a release or after a batch of
platform-sensitive changes:

```bash
gh workflow run "macOS verification" --ref main
```

Use the Actions UI to inspect the resulting run. The workflow configures its
own Git identity and runs each Python test module in a separate process, which
keeps its resource usage bounded on the hosted runner.

## Local validation

For the normal development loop, install the development dependencies and run
the same Python suite locally:

```bash
uv sync --extra dev
uv run pytest
```

Never add a self-hosted runner for this public repository to a machine that
contains production data or credentials. If an internal automation need
requires one, isolate it in a separate private repository with its own access
controls and review process.
