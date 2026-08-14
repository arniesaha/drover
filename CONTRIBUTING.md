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

We use the following formatting tools:

- **Black**: Code formatting with line length **88**
- **isort**: Import sorting
- **ruff**: Fast linting (preferred over flake8)

**Pre-commit hooks** are recommended. Install them with:

```bash
uv sync --extra dev
pre-commit install
```

Pre-commit will run automatically on each commit:
- Black formatting (format: 88)
- isort (standard mode)
- ruff linting

### Manual Formatting

If you don't use git hooks, format your code before submitting:

```bash
# Install development extra
uv sync --extra dev

# Format code
uv run ruff format .
uv run isort .
uv run ruff check .
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
# All tests
pytest

# Specific module
pytest tests/unit/test_server.py

# With coverage
pytest --cov=drover --cov-report=term-missing

# Watch mode (re-run on changes)
pytest -w src --watch
```

### Test Organization

```
tests/
├── unit/              # Unit tests for specific modules
├── integration/       # Integration tests for components
├── e2e/              # End-to-end tests
└── fixtures/         # Test data and mocks
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

1. **Ensure tests pass**: `pytest` on your changes
2. **Update documentation**: Add docs for new features
3. **Add CHANGELOG entry**: See below
4. **Lint your code**: `ruff check .` passes
5. **Format your code**: `black .` and `isort .`

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

### PR Template

We have a PR template with these sections:
- Description of changes
- Testing performed
- Screenshots (if UI changes)
- Checklist

Checklist items:
- [ ] Code follows style guidelines
- [ ] Tests added/updated
- [ ] Documentation updated
- [ ] CHANGELOG.md updated
- [ ] Breaking changes documented

### Code Review

- All checks must pass (CI, tests, linting)
- At least one reviewer (preferably with domain knowledge)
- Address all review comments or provide rationale
- Squash commits before merge unless review discussion warrants keeping them

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
# Verify markdown links
mdlinkchecker docs/

# Build and view docs locally (if using docs build tool)
# cd docs && npm run build
```

## Security

### Reporting Vulnerabilities

**Do not file security issues as public issues.**

Report security concerns via email:
- Email: `security@arniesaha.com` (placeholder)
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

Contributors are added to the `CONTRIBUTORS.md` file and acknowledged in release notes.
See the [Changelog](CHANGELOG.md) for contributor attribution.

## Questions?

Reach out via GitHub Discussions for general guidance or in PR comments for specific questions.

Thank you for contributing to Drover! 🚀
