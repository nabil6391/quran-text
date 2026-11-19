#!/usr/bin/env python3
"""Measure what each muṣḥaf's font does to its text, and write data/font-glyphs/.

    pip install -r conformance/requirements.txt
    python3 pipeline/font_glyphs.py

Reads data/mushaf/<key>.json and data/fonts/, so run it after pipeline/build.py
whenever the text or a font changed, then build once more so data/manifest.json
records the new files.  See pipeline/qurantext/font_glyphs.py.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qurantext import font_glyphs  # noqa: E402


def main() -> int:
    if not font_glyphs.available():
        raise SystemExit("font_glyphs.py needs uharfbuzz and fontTools: "
                         "pip install -r conformance/requirements.txt")
    t0 = time.time()
    layers = font_glyphs.write_all()
    bad = 0
    for key, layer in layers.items():
        changed = layer["normalization"]["words_drawn_differently"]
        bad += len(changed)
        print(f"  {key:8s} {layer['glyph_count']:3d} mark-carrying glyphs draw "
              f"{layer['word_count']:4d} words; normalisation changes "
              f"{len(changed)} of {layer['words']['checked']:,} drawings")
    print(f"done in {time.time() - t0:.0f}s -> data/font-glyphs/")
    # A word HarfBuzz draws differently after NFC or NFD is a finding: the
    # documentation states that it draws none differently.
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
