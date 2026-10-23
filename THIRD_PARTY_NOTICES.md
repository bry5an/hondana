# Third-party notices

Hondana is licensed under the GNU General Public License v3.0 or later (see `LICENSE`). It
depends on, and binary releases (the Docker image and desktop builds) ship, the software and data
below under their own licenses. Each Python package's full license text is in its installed
`*.dist-info` folder; `uv.lock` lists every dependency and version.

## Language data

| Component | Used for | License |
|---|---|---|
| [UniDic](https://clrd.ninjal.ac.jp/unidic/) via [unidic-lite](https://github.com/polm/unidic-lite) | Japanese tokenization (fugashi/MeCab) | UniDic: BSD 3-Clause, © The UniDic Consortium. unidic-lite: MIT |
| [IPADIC](https://github.com/polm/ipadic-py) | MeCab dictionary wordfreq tokenizes Japanese with | IPADIC license, © Nara Institute of Science and Technology. ipadic-py: MIT/WTFPL |
| [wordfreq](https://github.com/rspeer/wordfreq) | Word frequencies (difficulty, seeding known words) | Code: Apache-2.0. Data: CC BY-SA 4.0, see below |
| [en_core_web_sm](https://github.com/explosion/spacy-models) | English tokenization (spaCy) | MIT |
| [pykakasi](https://pypi.org/project/pykakasi/) | Readings and romaji for search and sorting | GPL-3.0-or-later |

**wordfreq data attribution.** wordfreq's frequency lists are redistributed under the
[Creative Commons Attribution-ShareAlike 4.0](https://creativecommons.org/licenses/by-sa/4.0/)
license. They are built from Google Books Ngrams, the Leeds Internet Corpus, Wikipedia, ParaCrawl,
OPUS OpenSubtitles 2018 (data from opensubtitles.org), the SUBTLEX word lists by Marc Brysbaert et
al. (freely available data, credited here as their terms ask), Twitter, Reddit and other sources;
see [wordfreq's README](https://github.com/rspeer/wordfreq#license) for the full list and
citations.

## Libraries

| Component | License |
|---|---|
| [MeCab](https://taku910.github.io/mecab/) (bundled in fugashi and mecab-python3 wheels) | BSD 3-Clause / LGPL / GPL (triple license, used under BSD) |
| [fugashi](https://github.com/polm/fugashi) | MIT |
| [FastAPI](https://github.com/fastapi/fastapi), [Starlette](https://github.com/encode/starlette), [Uvicorn](https://github.com/encode/uvicorn) | MIT / BSD 3-Clause |
| [spaCy](https://github.com/explosion/spaCy) | MIT |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU |
| [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf) | LGPL-2.1-or-later |
| [segno](https://github.com/heuer/segno) | BSD 3-Clause |
| [genanki](https://github.com/kerrickstaley/genanki) | MIT |
| [mutagen](https://github.com/quodlibet/mutagen) | GPL-2.0-or-later |
| [Requests](https://github.com/psf/requests) | Apache-2.0 |

## Fonts and icons

| Component | License |
|---|---|
| [Material Symbols](https://github.com/google/material-design-icons) (subset in `static/material-symbols.woff2`) | Apache-2.0 |
| Baloo 2, Hanken Grotesk, Noto Sans JP, Space Mono (loaded from Google Fonts) | SIL Open Font License 1.1 |

## Online services

Hondana can look things up on services it doesn't bundle. Each is optional, and their data stays
subject to their own terms:

- **Audible catalog API** (`api.audible.co.jp`): audiobook metadata by ASIN.
- **[Natively](https://learnnatively.com)**: community difficulty levels and ratings.
- **Google Books API**: genres and descriptions by ISBN (only with your own API key).
