# Rules for coding agents

Before you create a branch, commit, or open a pull request in this repository, read and follow
[CONTRIBUTING.md](CONTRIBUTING.md). In short:

- Name the branch `<type>/<short-description>`, with type one of `feat`, `fix`, `perf`, `refactor`, `test`, `docs`,
  `ci`, `chore`, and a 2 to 5 word lowercase kebab-case description, 50 characters at most
  (e.g. `feat/windows-backend`). CI fails any PR whose branch does not match.
- Never commit or push to `main` directly; every change goes through a PR from such a branch.
- Title PRs `<type>: <imperative summary>` and say how the change was tested.

`claude/CLAUDE.md` is something else: the planner rules for driving Voyager, not rules for working on its code.
