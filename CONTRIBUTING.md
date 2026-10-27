# Contributing to Voyager

These rules apply to everyone who opens a pull request, people and coding agents alike. A PR that breaks them
is closed or sent back.

## Branch names

Every PR comes from a branch named

```
<type>/<short-description>
```

- **type** says what the change is, one of:

  | type | use it for |
  |---|---|
  | `feat` | a new capability (a backend, a tool, a CLI command) |
  | `fix` | a bug fix |
  | `perf` | a speed-up with no change in behavior |
  | `refactor` | restructuring with no change in behavior |
  | `test` | tests only |
  | `docs` | documentation only |
  | `ci` | CI and repository automation |
  | `chore` | dependencies, packaging, tooling |

- **short-description** is 2 to 5 lowercase words in kebab-case (`a-z`, `0-9`, single hyphens) that name the change,
  not the ticket or the author. Start with the area it touches when that helps: `windows-backend`,
  `find-scroll-mispick`, `doctor-session-check`.
- The whole name is at most 50 characters.
- One branch, one change. Do not reuse a merged branch for new work.

Valid: `feat/windows-backend`, `fix/find-scroll-mispick`, `docs/windows-setup`, `chore/bump-comtypes`.
Invalid: `windows`, `feature/windows-backend` (unknown type), `fix/Find_Scroll` (case, underscore),
`claude/fix-stuff` (the type must say what the change is, not who made it), `fix/issue-12` (matches the pattern
but says nothing; reviewers reject it).

The rule is checked in CI (`.github/workflows/branch-name.yml`) against this pattern:

```
^(feat|fix|perf|refactor|test|docs|ci|chore)/[a-z0-9]+(-[a-z0-9]+){1,4}$
```

## Pull requests

- Branch from the latest `main`; open the PR against `main`.
- Title: the type, then a one-line summary in the imperative, e.g. `feat: add the Windows backend`.
- The description says what changed, why, and how it was tested (unit tests, live runs and on which OS).
  Name anything not tested.
- Run `pytest` before opening the PR. Changes to a backend also need a live run on that platform
  (`scripts/e2e_calculator.py`, `scripts/e2e_windows.py`).
