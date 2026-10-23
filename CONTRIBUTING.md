# Contributing to Hondana

Thanks for helping. Bug reports, fixes and features are all welcome.

## Before you start

- For anything bigger than a small fix, open an issue first so we can agree on the approach.
- Read [docs/development.md](docs/development.md) (setup, tests, layout) and, for UI work,
  [docs/STYLE.md](docs/STYLE.md). [CLAUDE.md](CLAUDE.md) is the full developer reference.

## Setup

```bash
git clone https://github.com/bry5an/hondana.git
cd hondana
uv sync
cp config.example.toml config.toml   # point epub_dir at a folder of your own EPUBs
uv run cli.py serve --reload
```

## Pull requests

- Branch from `main`, keep the change focused, and explain *why* in the description.
- Run what CI runs before pushing:

  ```bash
  uv run ruff check .
  uv run pytest -q
  ```

- Add or update tests for behaviour you change. Fixtures must be synthetic or tiny stubs: never
  commit real books, audiobooks, scraped web pages, dictionaries or your own `data/`.
- Update the docs (`README.md`, `docs/`, `CLAUDE.md`) when you change something they describe.
- UI changes: include a screenshot, and check both themes and a phone-width window.

## License

Hondana is licensed under the GNU General Public License v3.0 or later. By contributing, you
agree that your contributions are licensed the same way.
