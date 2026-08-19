# Contributing to Drover

Thank you for your interest in contributing to Drover! This document provides guidelines
for external contributors to help ensure a smooth and effective collaboration.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [How to Contribute](#how-to-contribute)
- [Developer Environment Setup](#developer-environment-setup)
- [Code Style](#code-style)
- [Commit Conventions](#commit-conventions)
- [Testing](#testing)
- [Pull Request Process](#pull-request-process)
- [Documentation](#documentation)
- [Security](#security)

## Code of Conduct

We are committed to providing a welcoming and inclusive environment. All contributors are
expected to be respectful and professional. Report unacceptable behavior to the maintainers.

## How to Contribute

### Finding Issues

Start with issues labeled:
- `good first issue`: Great for new contributors
- `documentation`: Improvements to docs
- `help wanted`: Issues needing community attention

### Getting Help

- **General questions**: Open a discussion on GitHub
- **Bug reports**: Use the bug report template
- **Feature requests**: Open a feature request and propose implementation details

## Developer Environment Setup

### Prerequisites

```bash
# Required
- Python 3.11+
- uv (Python package manager): https://docs.astral.sh/uv/
- Xcode 16+ and XcodeGen (for iOS app development)
- An agent CLI (Claude Code, Codex, etc.)

# Optional
- Docker (for containerized development)
- DuckDB CLI (for context store inspection)
```

### Initial Setup

```bash
# Clone the repository
git clone https://github.com/arniesaha/drover.git
cd drover

# Install dependencies
uv sync --extra dev

# Initialize the server
uv run drover-server init
```

### Starting in Development Mode

```bash
# Terminal 1: Start the server
uv run drover-server run

# Terminal 2: Start the harness daemon (local host)
uv run drover-harnessd \
  --host-id local-dev \
  --display-name "Local Dev Host" \
  --kind macos \
  --listen 127.0.0.1:7081 \
  --local-url http://127.0.0.1:7081 \
  --central-url http://127.0.0.1:7080
```

### Environment Variables

Create a `.env` file to override defaults:

```bash
# .env (do not commit)
DROVER_API_TOKEN=your-dev-token
DROVER_CONTROL_PLANE_DUCKDB=/path/to/custom/registry.duckdb
DROVER_LOG_LEVEL=debug
DROVER_REPO_ROOTS_JSON='{"~/projects":"example/repo"}'
```

Available environment variables:
- `DROVER_API_TOKEN`: API token for local development
- `DROVER_LOG_LEVEL`: Logging verbosity (DEBUG, INFO, WARNING, ERROR)
- `DROVER_REPO_ROOTS_JSON`: Custom repository path mappings
- `DROVER_CONTROL_PLANE_DUCKDB`: Custom control plane database path
- `DROVER_AGENT_ADOPTION_JSON`: Adoption registry configuration

### iOS App Development

```bash
cd apps/drover

# Generate project files
brew install xcodegen
xcodegen generate

# Open and run in Xcode
open Drover.xcodeproj
```

Sign the app with your Apple developer team before running on device.

## Code Style

### Python

The development extra provides the supported Python tooling:

- **Black**: Code formatting with line length **88**
- **isort**: Import sorting

### Manual Formatting

Install the development extra, then format Python before submitting:

```bash
# Install development extra
uv sync --extra dev

# Format code
uv run black .
uv run isort .
```

**Black configuration (line length 88):**
```toml
# pyproject.toml
[tool.black]
line-length = 88
target-version = ["py311"]
```

**isort configuration:**
```toml
[tool.isort]
profile = "black"
line_length = 88
```

## Commit Conventions

We follow the [Conventional Commits](https://www.conventionalcommits.org/) spec:

```
<type>(<scope>): <description>

[optional body]

[optional footer(s)]
```

### Types

| Type | Purpose |
|------|---------|
| `feat` | New features |
| `fix` | Bug fixes |
| `docs` | Documentation changes |
| `style` | Formatting, no logic change |
| `refactor` | Code refactoring |
| `perf` | Performance improvements |
| `test` | Adding or modifying tests |
| `chore` | Maintenance, tooling, configs |

### Examples

```
feat(server): add context container redaction policy
fix(harnessd): handle PTY session termination gracefully
docs(security): document credential revocation procedures
perf(context): optimize session summary generation
```

### Body Format

- Use imperative mood: "Add feature" not "Added feature"
- Wrap at 72 characters
- Explain **why**, not **what** (code shows what)

### Footer Format

Use for breaking changes or issue references:

```
BREAKING CHANGE: description

Closes #123
```

## Testing

### Running Tests

```bash
# All Python tests, in parallel
uv run pytest -n auto

# A specific module, serially, so output is not interleaved
uv run pytest tests/test_check_public_release.py
```

`-n auto` is not in `addopts`, so a bare `pytest` still runs serially. That is
deliberate: `-x`, `--pdb`, and `-s` all behave badly under `pytest-xdist`, and a
single-file run does not earn the worker startup cost. Use `-n auto` when you
want the whole suite, and leave it off when you are debugging one test.

On the reference host, the full suite takes about 42 seconds with `-n auto`
against about 5 minutes serially.

### Test Organization

```
tests/
├── test_*.py          # Python unit and integration coverage
└── shell/             # Shell workflow checks
```

### Test Standards

- **Unit tests**: Fast, isolated, mocked dependencies
- **Integration tests**: External services, database, filesystem
- **E2E tests**: Full system flow, may take longer

### Writing Tests

Use pytest fixtures for test data:

```python
@pytest.fixture
def sample_session():
    return {
        "session_id": "test-session-123",
        "created_at": "2024-12-01T00:00:00Z",
        "status": "active",
    }

def test_session_lifecycle(sample_session):
    """Test that sessions transition correctly."""
    # Test implementation
    pass
```

## Pull Request Process

### Before Submitting

1. **Ensure tests pass**: `uv run pytest` on your changes
2. **Update documentation**: Add docs for new features
3. **Add CHANGELOG entry**: See below
4. **Format your code**: `uv run black --check .` and `uv run isort --check-only .`

### Adding to CHANGELOG.md

Add an entry under `[Unreleased]` in the format matching the category:

```markdown
## [Unreleased]

### Added
- New feature for X

### Changed
- Existing functionality for Y

### Deprecated
- Soon-to-be-removed feature

### Removed
- Removed functionality

### Fixed
- Bug fix for Z

### Security
- Security improvement
```

### Pull Request Description

Describe the change, the validation you ran, and any user-visible effect.
Include screenshots for UI changes and call out breaking changes explicitly.

### Code Review

- All checks must pass (CI, tests, linting)
- At least one reviewer (preferably with domain knowledge)
- Address all review comments or provide rationale
- Squash commits before merge unless review discussion warrants keeping them

### Branch Hygiene

- The head branch of a merged pull request is deleted automatically. GitHub's
  `delete_branch_on_merge` is enabled on this repository, so a merge leaves
  nothing behind and nobody has to remember to tidy up. Twenty-three stale
  branches had accumulated before it was turned on.
- Deleting a branch never loses the work: its commits are in `main` by
  definition, and the release tags mark every published version.
- Local branches are yours to manage. `git fetch --prune` drops the remote
  tracking refs, and `git branch -d <name>` refuses anything not merged, so it
  is safe to run in bulk.
- Do not delete a branch a worktree still has checked out. `git worktree list`
  shows which those are. Harness sessions hold their own `drover/harness-*`
  branches for as long as the session exists, so leave those to the daemon.

## Documentation

### Where to Add Documentation

- New features: Update relevant sections in `docs/`
- User-facing changes: `docs/getting-started.md` and `README.md`
- Technical details: `docs/architecture.md` or new files

### Documentation Standards

- Use existing documentation structure
- Keep sections focused and concise
- Include examples and code snippets where helpful
- Cross-link related documentation
- Test all documentation changes (links work, code examples valid)

### Documentation Commands

```bash
# Verify local Markdown links and referenced files
make docs
```

## Security

### Reporting Vulnerabilities

**Do not file security issues as public issues.**

Report security concerns via email:
- Email: `security@arniesaha.com`
- Include: vulnerability description, impact assessment, steps to reproduce
- Expect acknowledgement within 72 hours

See [Security Policy](SECURITY.md) for full disclosure process.

### Security Best Practices for Developers

- **Never commit tokens**: Use environment variables or `.env` (not tracked)
- **Don't add secrets to logs**: Sanitize sensitive data
- **Audit dependencies**: Keep packages updated
- **Use pre-commit hooks**: Catch accidental secrets before commit

## Getting Help

### Contributors

- Check existing issues/PRs for your topic
- Join discussions on GitHub
- Ask questions in the Drover discussions forum

### Maintainers

Maintainers will:
- Review PRs within 1 week
- Provide constructive feedback
- Approve or deny releases

## Recognition

The Git history and [Changelog](CHANGELOG.md) acknowledge contributors.

## Questions?

Reach out via GitHub Discussions for general guidance or in PR comments for specific questions.

Thank you for contributing to Drover! 🚀
