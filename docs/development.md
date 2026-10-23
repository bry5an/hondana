# Developing Hondana

## Setup

```bash
uv sync                                    # Python 3.11+, every dependency incl. dev tools
cp config.example.toml config.toml        # then point epub_dir at a folder of EPUBs
uv run cli.py serve --reload               # http://localhost:8000, reloads on Python edits
```

There is no build step. Templates under `templates/` and files under `static/` are served from
disk, and every asset URL is stamped with its modification time, so edits show on reload.

## Tests and lint

```bash
uv run ruff check .
uv run pytest -q
```

CI (`.github/workflows/ci.yml`) runs exactly these two on every pull request and on pushes to
`main`, with `uv sync --frozen`.

`tests/fixtures/epubs/` holds four tiny stub EPUBs (metadata only, one deliberately broken) that
`tests/test_pipeline_smoke.py` runs the pipeline over. Keep them small and committed. Point your
dev `epub_dir` at a real folder of your own books, never at that one.

## Where things are

| Path | What |
|---|---|
| `epublib/cli.py` | The command line (`hondana`, or `uv run cli.py` through the root shim): one subcommand per pipeline stage, plus `serve` and the maintenance commands |
| `epublib/` | Everything else. `server.py` (routes), `pipeline.py` (the stages), and one module per job |
| `templates/` | Page templates, expanded from `templates/partials/` (no Jinja) |
| `static/` | Page JS and CSS. `ui.js` holds the helpers every page shares |
| `tests/` | pytest |

## Read these before changing things

- **`CLAUDE.md`**: the developer reference. It covers every module, data file and route, the
  pipeline, the reading-load metrics, the Reader API contract with the iOS app, Edit mode, and
  the conventions pages follow. It is written for Claude Code sessions and is just as useful to
  people.
- **`docs/STYLE.md`**: the UI style guide (tokens, type, cards, buttons, chips, dark theme). It
  is prescriptive: match it, and update it when you add a pattern.
- **`PRODUCT.md`**: who the app is for and the principles behind design decisions.
- **`docs/dev/`**: notes on things that took effort to learn: measured performance, traps in
  the vocabulary data.

## Conventions

- **The stage list lives only in `epublib/pipeline.py`.** The CLI and Admin are generated from
  it.
- **A new page** is three template includes (`head`, `shell`, `header`) plus one `PAGES` entry in
  `server.py`. Never copy the shell, and never put `<style>` in a template.
- **Hand-made data is precious.** Anything a person corrects goes in an override file under
  `data/`, which survives rescans and renames (`rekey.py`), and is added to `data_mirror.py` so
  it gets backed up.
- **Never `alert()`/`confirm()`/`prompt()`.** Use `confirmDialog()`, `promptDialog()` and
  `toast()` from `ui.js`.
- The iOS app depends on the Reader API. Change its shapes together with
  [hondana-ios](https://github.com/bry5an/hondana-ios), and bump the schema constants
  (`ANNOTATE_SCHEMA`, `ALIGN_SCHEMA`) when a format changes.

## Issues and releases

Work is tracked in [GitHub issues](https://github.com/bry5an/hondana/issues). If you use Claude
Code, the repo's skills (`.claude/skills/`) encode the workflow: `work-issue` (branch, plan,
implement, open a PR) and `create-issue`. See [CONTRIBUTING.md](../CONTRIBUTING.md).
