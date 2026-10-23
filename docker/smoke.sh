#!/usr/bin/env bash
# Smoke-test a Hondana image: start it as a non-root user on empty volumes, wait
# for /healthz, then index and analyze one Japanese and one English book (which
# loads fugashi/UniDic, MeCab/IPADIC through wordfreq, pykakasi and spaCy).
# Usage: docker/smoke.sh <image> [host-port]
set -euo pipefail
image=${1:?usage: smoke.sh <image> [host-port]}
port=${2:-18000}
work=$(mktemp -d)
name="hondana-smoke-$$"

finish() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "--- server log ---"
    docker logs "$name" 2>&1 | tail -60 || true
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  rm -rf "$work"
  exit "$rc"
}
trap finish EXIT

mkdir -p "$work/config" "$work/data" "$work/books/Japanese" "$work/books/English" "$work/audiobooks"
python3 - "$work/books" <<'PY'
import sys, zipfile
from pathlib import Path

root = Path(sys.argv[1])
container = ('<?xml version="1.0"?><container version="1.0" '
             'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
             '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
             '</rootfiles></container>')

def epub(path, title, lang, paragraphs):
    opf = (f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" '
           f'version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:title>{title}</dc:title><dc:language>{lang}</dc:language>'
           f'<dc:identifier id="id">urn:uuid:smoke-{lang}</dc:identifier></metadata>'
           f'<manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/></manifest>'
           f'<spine><itemref idref="c"/></spine></package>')
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    doc = ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
           f'<body>{body}</body></html>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/c.xhtml", doc)

ja = ["朝、駅の前で友達に会った。二人で小さな喫茶店に入り、温かいコーヒーを飲みながら話した。",
      "彼女は来月から新しい仕事を始めるそうだ。少し不安だけど、とても楽しみにしていると言った。",
      "外は雨が降り始めていたので、私たちはもう一杯ずつ注文して、窓の外を眺めていた。"] * 8
en = ["In the morning I met a friend in front of the station, and we talked over warm coffee.",
      "She said she would start a new job next month, a little nervous but mostly excited.",
      "It had started to rain outside, so we ordered one more cup each and watched the window."] * 8
epub(root / "Japanese" / "smoke-ja.epub", "雨の日の喫茶店", "ja", ja)
epub(root / "English" / "smoke-en.epub", "A Rainy Day Cafe", "en", en)
PY
chmod -R a+rwX "$work"  # the container runs as uid 1000, whoever runs this script

docker run -d --name "$name" --user 1000:1000 -p "127.0.0.1:$port:8000" \
  -v "$work/config:/config" -v "$work/data:/data" \
  -v "$work/books:/books" -v "$work/audiobooks:/audiobooks" "$image" >/dev/null

for _ in $(seq 1 90); do
  curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$port/healthz"; echo
curl -fsS "http://127.0.0.1:$port/" -o /dev/null
curl -fsS "http://127.0.0.1:$port/api/setup" -o /dev/null

docker exec "$name" hondana config
docker exec "$name" hondana scan
docker exec "$name" hondana analyze
docker exec "$name" python - <<'PY'
import json
vocab = json.load(open("/data/vocab_epub.json", encoding="utf-8"))
statuses = sorted(v.get("status") for v in vocab.values())
print("analyzed:", statuses)
assert statuses == ["ok", "ok"], statuses
PY
books=$(curl -fsS "http://127.0.0.1:$port/api/books" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
[ "$books" = "2" ] || { echo "expected 2 books, got $books"; exit 1; }
echo "smoke test passed"
