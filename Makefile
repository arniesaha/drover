.PHONY: help docs check-release-ready build build-sdist build-wheel clean-build clean pr-setup pr-commit pr-push pr-create release-tag release-validate example-one-click-release status version

VERSION = $(shell grep "^version" pyproject.toml | awk '{print $$3}' | tr -d '"')
TAG ?= v$(VERSION)
export TAG
BUILD_DIR := dist

.DEFAULT_GOAL := help

help: ## Show this help message
	@printf '\n=== Drover v$(VERSION) - Development Makefile ===\n'
	@printf 'Usage: make [target]\n\n'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-25s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf '\n'

docs: ## Validate all documentation files
	@echo "=== Validating Documentation ==="; \
	docs="$$(git ls-files -- '*.md')"; \
	for doc in $$docs; do \
		test -f "$$doc" || exit 1; \
		echo "✓ $$doc"; \
	done; \
	python3 scripts/check_markdown_links.py $$docs || exit 1; \
	echo "✓ All documentation files validated"

check-release-ready: docs ## Verify repo is ready for release
	@echo "=== Checking Release Readiness ==="
	@echo "1. Documentation: ✓ local links and files validated"
	@echo "2. Version consistency:"; \
	tags="$$(git tag --points-at HEAD --list 'v*')"; \
	if [ -z "$$tags" ]; then \
		echo "  ✓ Release tag is pending for v$(VERSION)."; \
	elif [ "$$tags" = "v$(VERSION)" ]; then \
		echo "  ✓ Head is tagged v$(VERSION)."; \
	else \
		echo "  ✗ Release tag '$$tags' does not match pyproject.toml v$(VERSION)."; \
		exit 1; \
	fi

	@echo "3. Codebase health:" && \
	if test -f src/drover/__init__.py; then \
		TODOS=$$(find src/drover -name "*.py" -exec grep -r "TODO\|FIXME\|XXX\|HACK" {} \+ 2>/dev/null | grep -v "^\s*# " || true); \
		if [ -z "$$TODOS" ]; then echo "  ✓ No TODOs/FIXMEs in codebase"; \
		else echo "  ⚠ Found issues: $$TODOS"; fi; \
	fi

	@echo "4. Nothing private in the tracked tree:"; \
	python3 scripts/check_public_release.py >/dev/null \
		|| { echo "  ✗ scripts/check_public_release.py reported findings."; exit 1; }
	@echo "  ✓ Public release audit clean"

	@echo "5. The changelog names this version:"; \
	grep -q "^## \[$(VERSION)\]" CHANGELOG.md \
		|| { echo "  ✗ CHANGELOG.md has no '## [$(VERSION)]' section."; exit 1; }
	@echo "  ✓ CHANGELOG.md documents $(VERSION)"

	@echo "=== Release Readiness: PASSED ==="
	@echo
	@echo "This gate checks the repository, not the build. It does not run the"
	@echo "test suite: run 'uv run pytest' (needs 'uv sync --extra dev'), or"
	@echo "read CI on the commit you intend to tag. A release cut over a red"
	@echo "suite passes this target."

build: ## Build distribution (sdist and wheel)
	@echo "=== Building Distribution ===" && \
	mkdir -p $(BUILD_DIR) && \
	uv build --sdist --wheel --out-dir=$(BUILD_DIR) && \
	echo "Source dist: $(BUILD_DIR)/drover-$(VERSION).tar.gz" && \
	ls -lh $(BUILD_DIR)/ && echo "✓ Distribution built"

build-sdist: ## Build source distribution only
	@echo "=== Building Source Distribution ===" && mkdir -p $(BUILD_DIR) && uv build --sdist --out-dir=$(BUILD_DIR) && echo "✓ $(BUILD_DIR)/drover-$(VERSION).tar.gz"

build-wheel: ## Build wheel only
	@echo "=== Building Wheel Package ===" && mkdir -p $(BUILD_DIR) && uv build --wheel --out-dir=$(BUILD_DIR) && echo "✓ $(BUILD_DIR)/drover-$(VERSION)-*.whl"

clean-build: ## Clean build artifacts
	@echo "=== Cleaning Build Artifacts ===" && rm -rf $(BUILD_DIR) src/drover.egg-info .mypy_cache .pytest_cache build/ && echo "✓ Build cleaned"

clean: clean-build ## Clean all temporary files
	@echo "=== Comprehensive Cleanup ===" && find . -name "*.pyc" -delete && find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true && find . -maxdepth 2 -name ".cache" -type d -exec rm -rf {} + 2>/dev/null || true && echo "✓ Cleanup complete"

pr-setup: ## Setup files for documentation PR
	@echo "=== Files for Documentation PR ===" && \
	echo "✓ docs/context-store.md (local-first lakehouse)" && \
	echo "✓ docs/threat-model.md (security & risk model)" && \
	echo "✓ CHANGELOG.md (semantic versioning)" && \
	echo "✓ SECURITY.md (security policy)" && \
	echo "✓ CONTRIBUTING.md (contribution guidelines)" && \
	echo "✓ .github/CODEOWNERS (PR reviewer assignment)"

pr-commit: ## Create commit ready for documentation PR
	@git add docs/context-store.md docs/threat-model.md CHANGELOG.md SECURITY.md CONTRIBUTING.md .github/CODEOWNERS Makefile
	@if git diff --cached --quiet; then echo "No documentation changes are staged."; exit 1; fi
	@git commit -m "docs: complete public release documentation"
	@echo "✓ Commit created for PR"

pr-push: ## Push documentation PR branch
	@branch="$$(git branch --show-current)"; \
	if [ "$$branch" != "docs-and-build-for-v0.2.0" ]; then \
		echo "Run this target from docs-and-build-for-v0.2.0; current branch is $$branch."; \
		exit 1; \
	fi; \
	git push -u origin "$$branch"; \
	echo "✓ PR pushed: https://github.com/arniesaha/drover/pulls"

pr-create: ## Create GitHub PR (requires gh CLI)
	@echo "=== Creating GitHub PR ===" && \
	gh pr create --base main --head docs-and-build-for-v0.2.0 --title "docs: complete public release documentation" --body "Complete documentation suite for Drover v0.2.0 public release"

release-tag: ## Create annotated git tag for release
	@if [ "$$TAG" != "v$(VERSION)" ]; then \
		echo "release-tag derives its tag from pyproject.toml; expected v$(VERSION)."; \
		exit 1; \
	fi
	@git tag -a "$$TAG" -m "Drover $$TAG - public release"
	@git push origin "$$TAG"
	@echo "✓ Tag $$TAG created"

release-validate: ## Validate release artifacts before publishing
	@echo "=== Validating Release Artifacts ===" && \
	test -f $(BUILD_DIR)/drover-$(VERSION).tar.gz && echo "✓ Source dist: $(BUILD_DIR)/drover-$(VERSION).tar.gz" || exit 1 && \
	WHL=$$(ls $(BUILD_DIR)/drover-$(VERSION)-*.whl 2>/dev/null | head -1) && \
	[ -n "$$WHL" ] && echo "✓ Wheel: $$WHL" || exit 1 && \
	unzip -p $$WHL drover-$(VERSION).dist-info/METADATA | grep "Version: $(VERSION)" && echo "✓ Metadata verified" && \
	echo "✓ Release validation PASSED"

example-one-click-release: ## Example: Check, package, and prepare for release
	@echo "=== One-Click Release Preparation ===" && \
	$(MAKE) check-release-ready && \
	$(MAKE) build && \
	$(MAKE) release-validate && \
	echo "Ready to publish the validated artifacts through your chosen release channel."

status: ## Show Git repository status
	@echo "=== Git Repository Status ===" && git branch --show-current && git log -1 --oneline && git status --short

version: ## Show current version
	@echo "=== Version: $(VERSION) ===" && git describe --tags 2>/dev/null || echo "No tags"
