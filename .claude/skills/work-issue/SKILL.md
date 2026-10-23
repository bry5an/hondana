---
name: work-issue
description: Pick up a GitHub issue in this repo, plan it if it's non-trivial, and implement it. Use when asked to work an issue, pick up a ticket, "do issue #N," or start on something filed in GitHub.
---

# Work a GitHub issue

Repo: this checkout's GitHub repo (`gh repo view --json nameWithOwner`; upstream is `bry5an/hondana`). `gh` must be authenticated (`gh auth status`).

## 1. Pick the issue

If an issue number or URL was given, use it directly. Otherwise list what's open and ask:
```
gh issue list --state open
```
Use AskUserQuestion to let the user pick if there's more than one live candidate — don't guess which one they mean.

## 2. Read full context, not just the title

```
gh issue view <n> --comments
```
Read the whole body **and** the comments — a comment can resolve a question the issue itself left open, or narrow scope after filing.

Issues filed via the `create-issue` skill cite concrete `path:line` pointers and, for anything non-trivial, carry an explicit **"Open question" / "What needs deciding"** section. Both are load-bearing:
- The line numbers are a claim about the code *when the issue was filed* — re-read the actual current file before trusting them, same as you would a stale memory. Code moves.
- An open question is a deliberate signal that the filer didn't want a design decision guessed at. Don't resolve it by picking whichever option is easiest to code.

## 3. Decide whether this needs planning first

Skip straight to implementation only when the issue already pins down a single, unambiguous fix (e.g. a bug report with root cause traced to specific lines, nothing left to decide).

Plan first when any of these hold:
- The issue has an open question / multiple viable approaches (its own "Open question" section, or you can see more than one reasonable way to do it that it didn't rule out).
- The change touches more than one module or crosses the boundary described in `CLAUDE.md`'s architecture/pipeline sections.
- It's filed as a `[Epic]` or otherwise clearly multi-step.

For a narrow fork (pick option A vs B), AskUserQuestion is enough. For anything bigger, use EnterPlanMode and come back with a real plan before touching code — don't narrate the plan as chat text and start editing anyway.

## 4. Branch

**Mandatory gate: do not call Edit/Write/NotebookEdit for this issue until this step has run.** Reading files to plan or scope the change is fine on `main`; the first *write* is not.

`git status` first — if the tree already carries unrelated uncommitted work, stop and ask rather than building on top of it or stashing it away. Otherwise branch off `main`:
```
git checkout main && git pull --ff-only origin main
git checkout -b issue-<n>-<short-slug>
```
If already sitting on a feature branch that's clearly *for* this issue, it's fine to keep working on it instead of branching again — use judgment, don't create branch clutter. Working directly on `main` is not a valid alternative, even for a one-line fix: `main` only ever receives reviewed work through a pull request.

## 5. Implement

Follow the `CLAUDE.md` conventions for whichever layer you're touching (data flow, the pipeline in `epublib/pipeline.py`, per-module ownership). Keep the diff scoped to the issue — no drive-by refactors, no fixing unrelated things you notice along the way (file a follow-up issue instead if something else looks broken).

## 6. Verify before calling it done

Run the regression suite first (`uv run ruff check . && uv run pytest -q`; CI runs the same on every PR). It covers vocab/known/corpus/build/study logic only, so the rest of verification is manual:
- **Backend-only logic** — exercise the affected function/endpoint directly (a short script via `uv run python -c ...`, or `curl` against a locally running dev server).
- **Anything UI-visible** — start the dev server and check the actual page in a browser (the `run` skill knows how to launch this project). Don't report a frontend fix done on the strength of reading the diff alone.

## 7. Stop before anything shared or irreversible

Do **not**, without being asked:
- commit (only commit when the user explicitly says so — a working tree can carry unrelated files, so a careless `git add -A` is a real risk)
- push, comment on the issue, or close it
- open a pull request

Finish by summarizing what changed and what you verified, and ask how the user wants to proceed (commit and open a PR? comment back on the issue with a summary? leave it as-is for review?).
