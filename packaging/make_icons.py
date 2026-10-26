"""Make the app icons from static/icon.png: `hondana.icns` (macOS) and `hondana.ico`
(Windows) in the folder given as the first argument."""
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    src = Image.open(ROOT / "static" / "icon.png").convert("RGBA")
    big = src.resize((512, 512), Image.LANCZOS)  # the source is 200 px; icon sets want more
    big.save(out / "hondana.icns")
    big.save(out / "hondana.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])


if __name__ == "__main__":
    main(Path(sys.argv[1]))
