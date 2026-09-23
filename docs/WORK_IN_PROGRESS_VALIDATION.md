# Saved validation work — step 1.7

The user stopped work after the current extraction step (1.6). Validation is saved for a later
resume; do not publish its PR or continue implementation without resumed authorization.

- Worktree: `/tmp/invoiceops-validation`
- Branch: `p1/07-deterministic-validation`, started from main `5231ea0`.
- Implementation: `99cc819` — deterministic required-field, sign, line-math, subtotal, tax, and
  gross-total checks; versioned Decimal policy; audited node; Pydantic/Zod contracts; tests and docs.
- Hardening: `93039ad` — arithmetic independent of mutable Decimal default-context settings.
- This branch is backed up remotely on `origin`. No validation PR has been created.

Verification completed before the extraction dependency merged: 52 focused offline unit tests,
three real restricted-role Postgres/audit integration tests, scoped Ruff and strict mypy, strict
TypeScript with TypeScript 5.9.3/Zod 4.1.12, and a Python-result/Zod roundtrip. The earlier full offline
suite passed 245 tests before the additional Decimal-default regression was added. Full checks
against the final merged extraction implementation remain pending.

Temporary copies of `schemas/__init__.py`, `schemas/extraction.py`, and `ledger/audit.py`
were borrowed from the extraction worktree to run the checks above. They contained no original
validation edits and have been removed from this checkout. Their implementation is preserved in
extraction commit `9dcec20` and its PR #16; merge updated `main` to obtain the dependency before
running validation checks. The saved validation branch remains based on `5231ea0`, with no
uncommitted files and no validation PR.

On a later authorized resume, merge current `main` (preserving both sides’ progress-log rows and architecture additions),
then install the merged locked
dependencies with `UV_CACHE_DIR=/tmp/invoiceops-uv-cache uv sync --locked --all-groups`, and run full
Ruff/format, strict mypy, offline unit, and real integration checks before publication. The plan's
1.7 checkbox describes the implemented branch work; it is not evidence that 1.7 has merged to main.
See [validation design and behavior](VALIDATION.md) for the implemented policy and scope.
