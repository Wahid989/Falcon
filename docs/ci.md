# Continuous Integration

CI runs on GitHub Actions. The workflow is defined in
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

## When it runs

- On **every push** to any branch.
- On **every pull request**.

Status (pass/fail) is reported on the commit and on the PR.

## What it checks

**Python job (`python`)**
- Sets up Python 3.12.
- Installs `requirements.txt` / `requirements-dev.txt` when present, and always
  installs `pytest`, `black`, and `flake8`.
- `black --check .` — fails if any file is not formatted.
- `flake8 .` — lints (config in [`.flake8`](../.flake8), aligned with black).
- `python -m pytest tests/ -v` — runs the test suite.

**.NET job (`dotnet`)**
- Sets up .NET 8.
- Restores and builds `storage/dotnet` **only if** a `.csproj`/`.sln` exists.
  Until the .NET service is scaffolded, this job passes without building.

## Making a failing check block merges

The workflow reports status, but GitHub only *blocks* a merge when branch
protection requires the check. To enable it (repo admin):

1. **Settings → Branches → Add branch ruleset** (or classic branch protection)
   targeting `main`.
2. Enable **Require a pull request before merging** and **Require status checks
   to pass before merging**.
3. Add `Python (lint + tests)` and `.NET (build)` as required checks.

Once set, a red check on a PR prevents merging into `main`.

## Running the checks locally

```bash
pip install pytest black flake8
black --check .
flake8 .
python -m pytest tests/ -v
```

Run from the repository root with the project virtual environment active.
Using `python -m pytest` includes the repository root on Python's import path,
so tests can import `src.<package>`. This matches the `make test` command.
