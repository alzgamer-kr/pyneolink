# Release Checklist

Publishing a GitHub Release triggers `.github/workflows/publish.yml`. A normal
branch push or tag push alone does not publish to PyPI.

1. Review the release changes and merge them into `main`. Keep camera configs,
   credentials, databases, captures, and `.tmp` artifacts out of the commit.
2. Keep `pyproject.toml`, `pyneolink.__version__`, README, and the finalized
   CHANGELOG heading on the same version. Version 0.6.1 uses `## 0.6.1`.
3. Run the local checks below, then push `main` and wait for the Tests workflow.
4. Create tag `v0.6.1` at the validated main commit and push that tag. Publish a
   GitHub Release for that exact tag, using the 0.6.1 changelog section as notes.
5. Confirm the Publish workflow completes: version validation, the full test
   matrix, both CLI assets, and finally PyPI publication.

## Local Checks

Run from the repository root with the development dependencies installed:

```powershell
python -m ruff check pyneolink tests examples
python -m ruff format pyneolink tests examples --check
python -m ruff check pyneolink --select D101,D102,D103,D107,D201,D202,D204,D205,D209,D210,D211,D300,D400,D403,D419
python -m pytest
python -m pip install ".[push]"
python -m pytest tests/test_push_fcm_crypto_roundtrip.py tests/test_push_gcm_retry.py
python -m build
python -m twine check dist/*
```

The optional push tests use local encrypted fixtures and mocked HTTP; they do
not register receivers, change subscriptions, or contact cameras. Test the core
suite without the push extra first to preserve optional-dependency coverage.

## Workflow Requirements

- Tags must use the `v` prefix and match package metadata exactly.
- Regression tests run on Python 3.11-3.14 on Windows and Linux, with additional
  optional push integration checks on every matrix entry.
- Standalone CLI assets include voice and push dependencies. FFmpeg remains an
  external runtime requirement for voice file conversion.
- The repository's `pypi` environment and PyPI trusted publisher must authorize
  this repository and `publish.yml`. Environment approvals may delay publication.
- Release assets require `contents: write`; PyPI publishing uses OIDC via
  `id-token: write`. No PyPI API token is stored in the project.

Local preflight cannot guarantee hosted runner availability, remote dependency
resolution, or publisher permissions. Do not describe a release as published
until the workflow and the PyPI version are both confirmed.
