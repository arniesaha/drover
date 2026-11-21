## Summary

<!-- What changes and why. Link the issue it closes, e.g. "Closes #123". -->

## Verification

<!-- Commands you ran and their results. Say what you could not verify. -->

- [ ] `uv run pytest -q` (or the focused suites this change touches)
- [ ] `uv run black --check src tests` and `uv run isort --check-only src tests`
- [ ] `python3 scripts/check_public_release.py`
- [ ] Web client (`apps/web`) or site (`site/`) built and tested, if touched
- [ ] Docs, changelog, or config reference updated, if behavior changed

## Risk

<!-- Migrations, protocol or config changes, upgrade steps, rollback. -->

By submitting this pull request I confirm that it contains no secrets or
private values and that I agree to the project's
[Code of Conduct](https://github.com/arniesaha/drover/blob/main/CODE_OF_CONDUCT.md).
