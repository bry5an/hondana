# docs/dev

Notes for developers (and Claude Code sessions): the things that cost real effort to establish
and can't be read straight off the code.

This is deliberately *not* a second copy of `CLAUDE.md`. `CLAUDE.md` says what the code is and
how to run it; these pages say what surprised someone who went looking. If a fact is obvious from
a function signature, it doesn't belong here. If it took a measurement, a browser session, or a
bug to learn, it does.

| Page | Read it when |
|---|---|
| [02-vocab-pipeline.md](02-vocab-pipeline.md) | Touching analyze/known/build, or looking for where word data actually lives |
| [03-known-words.md](03-known-words.md) | Touching the known-words list, Known %, or reading load |
| [04-perf-baselines.md](04-perf-baselines.md) | Before designing around "this will be too slow" |
| [05-gotchas.md](05-gotchas.md) | Before trusting a word count, or after a change that quietly did nothing |

See also `CLAUDE.md` (repo root) for the full developer reference, and `docs/README.md` for the
user and operator docs.
