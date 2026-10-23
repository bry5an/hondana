---
name: create-issue
description: File a GitHub issue in this repo to track a bug, feature request, or task, using the gh CLI. Use when asked to file/open/log an issue, track something as a bug or feature, or "make a ticket for this."
---

# Create a GitHub issue

Repo: this checkout's GitHub repo (`gh repo view --json nameWithOwner`; upstream is `bry5an/hondana`). `gh` must be authenticated (`gh auth status`).

## 1. Check for an existing duplicate

Search before filing — this repo doesn't have a big backlog, so dupes are easy to spot:
```
gh issue list --state all --search "<keywords>"
```
If a clear match exists, point the user at it instead of filing a new one (unless they want a fresh issue anyway).

## 2. Draft title + body

Pull the substance from the current conversation — don't make the user re-explain something already discussed.

- **Title:** short, specific, no leading "Bug:" / "Feature:" (the label carries that).
- **Body:** markdown, shaped by type:
  - **Bug** — What happens vs. what's expected, repro steps, relevant file/line (`path:line`), any error output.
  - **Feature/enhancement** — What it should do, why it's wanted, relevant existing code/files if known.
  - Keep it tight: concrete, no filler. Reference real paths from the repo, not placeholders.

## 3. Labels

Reuse existing labels — don't invent new ones unless the user asks:
```
gh label list
```
As of writing: `bug`, `enhancement`, `documentation`, `question`, `duplicate`, `invalid`, `wontfix`, `help wanted`, `good first issue`, `english-learner` (this repo's label for work on English-learner profiles). Pick what fits; it's fine to apply none if nothing fits well.

## 4. Confirm before creating

Filing an issue is visible, shared state — show the user the draft (title, body, labels) and get explicit go-ahead before creating it. Don't create on spec.

## 5. Create it

```
gh issue create --title "<title>" --body "<body>" --label "<label1>" --label "<label2>"
```
Report back the issue number and URL.
