---
name: pr-workflow
description: 'Use when the owner says commit, push, open a PR, merge, or clean up a branch, and when starting any change. Covers the issue, branch name <issue>-short-slug, commit message with Refs #N, PR body with Closes #N, squash merge, local cleanup, and the never-push-to-main rule.'
---

# PR workflow

Rules are in [docs/process.md](../../../docs/process.md). This is the step list.

## Procedure

1. Confirm the issue exists (`gh issue view <N>`). If not, create it with a clear "Done when" line. Use the milestone.
2. Start from fresh main: `git checkout main && git pull`.
3. Create the branch: `git checkout -b <N>-short-slug`.
4. Make the change. One issue, one PR, one purpose. Update `docs/STATUS.md` (tick the item, fix the next action).
5. Commit with a short subject and a `Refs #<N>` line:

   ```text
   docs: short summary

   Refs #<N>
   ```

6. Push the branch: `git push -u origin <N>-short-slug`. Never push `main`.
7. Open the PR: `gh pr create --base main --title "<short title>" --body "Closes #<N> ..."`. The title and body become the squash commit, so write them well.
8. The owner reviews and merges (squash). Do not merge for the owner.
9. After merge: `git checkout main && git pull`, then delete the local branch with `git branch -D <branch>`. Use `-D`: squash merge leaves the branch looking unmerged.
10. Check the project board: `uv run --no-sync python scripts/check_project_board.py`. If it lists a closed issue that is not Done, run it again with `--fix`. The "Pull request linked to issue" workflow can move a just-closed issue back to In Progress (#123). An open issue marked Done is only reported; fix it by hand.

## Checks before opening the PR

- Branch name starts with the issue number.
- Every commit has `Refs #<N>`. The PR has `Closes #<N>`.
- `Closes #<N>` is required (CI checks it) and closes the issue at merge, so every "Done when" line must be true before the owner merges. Do live or owner steps (a deploy, a scan, cleanup) on the branch before merge and add their results to the same PR. If a step can only happen after merge (it needs the code on `main`), split it into a follow-up issue before opening the PR; that issue gets its own PR with `Closes`. Lesson from #142: the PR closed the issue while its live scan and cleanup were still open.
- The PR-body check runs only in GitHub Actions; a plain `make ci` skips it. After opening or editing the PR, run it locally (lesson from #147: the PR said `Refs` instead of `Closes`):

  ```bash
  CI_PULL_REQUEST=1 PR_BODY="$(gh pr view --json body --jq .body)" uv run --no-sync python scripts/check_traceability.py
  ```

- `make ci` passes (when it exists, issue #8).
- No secrets, no real customer data.
- Every decision made in this change is in an ADR, the facts register or STATUS.
