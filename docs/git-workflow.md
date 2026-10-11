# Git & GitHub Workflow

How the team branches, reviews, and merges. Summarized in the README; this is
the full version.

## Branching model

- **`main`** — always releasable; protected. No direct pushes.
- **Feature branches** — all work happens on a short-lived branch off `main`,
  merged back via a pull request.
- An optional long-lived **`develop`** branch is *not* used this term; we keep
  it simple with feature-branches-to-`main`.

### Branch naming

```
<type>/falcon-<ticket>-<short-slug>
```

Examples:

```
feat/falcon-019-protocol-validator
fix/falcon-028-framing-partial-read
docs/falcon-007-database-setup
```

`<type>` is one of: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`.

## Commit conventions

- Imperative mood, lowercase subject, ≤ ~72 chars:
  `add schema validator module`.
- Reference the ticket in the body where useful (`FALCON-019`).
- Small commits that each leave the system runnable. 1–3 per day when active on
  a ticket; don't wait to finish a whole ticket before committing.
- Never commit secrets (use environment variables; see `config/.env.example`).

## Pull requests

- Open a PR from your feature branch into `main`; fill in the PR template.
- Link the FALCON ticket it closes.
- Keep PRs small and ticket- or module-scoped so ownership stays visible.
- CI (lint + tests + build) must be green before merge.

## Review requirements

- At least **one approving review** from another team member before merge.
- Address review comments with follow-up commits on the same branch.

## Merge policy

- **Squash merge** into `main` (one tidy commit per PR); delete the branch
  after merge.
- `main` must stay green — a failing required check blocks the merge.

## Branch protection (repo setting)

Enable on `main` (Settings → Branches → Add branch ruleset):

1. Require a pull request before merging.
2. Require at least one approval.
3. Require status checks to pass — add the CI jobs
   (`Python (lint + tests)`, `.NET (build)`).
4. Block direct pushes to `main`.
