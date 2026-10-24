# Hondana documentation

Start with the [README](../README.md): what Hondana is, how to install it, and how to use it day
to day. These pages go deeper.

## Setting up and running

| Page | What's in it |
|---|---|
| [configuration.md](configuration.md) | Every config key, how the config file is chosen, the `.env` secrets, and which files under `data/` to back up |
| [desktop.md](desktop.md) | The tray app: first run, where it keeps things, how it restarts, `--self-test` |
| [docker.md](docker.md) | Running the Docker image: compose, folders, permissions, the phone and the network, updating |
| [networking.md](networking.md) | Reaching Hondana from your phone: IP and QR code, `.local` names, your own DNS, a reverse proxy, Tailscale |
| [running-a-server.md](running-a-server.md) | Running as a systemd service, a reverse proxy, updating, scheduled reindexing, backups and their daily timer, security |

## How it works

| Page | What's in it |
|---|---|
| [pipeline.md](pipeline.md) | The stages that build the library, what each writes, and when to run them; how editions are paired and how search works |
| [audiobooks.md](audiobooks.md) | Getting audiobooks in, the catalog, linking and pruning, Natively levels, consolidating folders, files iOS won't play |
| [audio-sync.md](audio-sync.md) | Making an audiobook follow its text in Hondana for iPhone (SubPlz, `align-remote`) |
| [api.md](api.md) | The HTTP API, including the Reader API the iPhone app uses |

## Developing

| Page | What's in it |
|---|---|
| [development.md](development.md) | Dev setup, tests, the code layout and conventions |
| [STYLE.md](STYLE.md) | The UI style guide. Prescriptive: read it before UI work |
| [`../CLAUDE.md`](../CLAUDE.md) | The full developer reference: modules, data files, routes, metrics |
| [`../PRODUCT.md`](../PRODUCT.md) | Who it's for and the product principles |
| [dev/](dev/README.md) | Developer notes: the vocab pipeline, known words, perf baselines, data traps |

