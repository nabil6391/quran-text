"""What the named font does to the text when it draws it.

A muṣḥaf file holds the release's own codepoints and names the font they were
written for (``fonts.py``).  The two are one object: the font does not draw
some words sign by sign from their codepoints.  It replaces a run of letters
*and their marks* with one glyph drawn for that run — in the five muṣḥafs that
print a sajdah line, every word under one — and what that glyph shows is the
font's drawing, not a rendering of each codepoint.

Two things follow, and this module measures both from the font and the text,
with no list kept by hand:

*The mark-carrying glyphs.*  For each muṣḥaf, which words the font draws with
such a glyph.  A consumer that counts signs from codepoints, or that swaps the
font, is told exactly where the codepoints no longer describe the picture.  In
17:7 of Ḥafṣ the glyph draws a small wāw and a maddah that no codepoint
writes; the word is correct, and only the font knows it.

*Normalisation.*  The text is kept unnormalised because NFC is not what the
release writes, and ``docs/format.md`` tells applications to display the
published string untouched: NFC composes letters some of these fonts have no
glyph for, and a shaper that does not decompose them again draws them from a
fallback font.  HarfBuzz does decompose them.  How far that goes is measured
here: every word is shaped as stored, after NFC and after NFD, and the glyphs
and positions compared.  It is a statement about HarfBuzz, which is what
Chromium, Firefox and Android shape with, and about no other shaper.

Shaping needs HarfBuzz (``uharfbuzz``) and ``fontTools``.  The rest of the
build needs neither, so this module imports them only when asked to measure;
``conformance/display_test.py`` is the gate that proves the committed layer is
what they produce.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from . import stamp
from .paths import DATA

FORMAT = "quran-font-glyphs"
FORMAT_VERSION = "1.0"

GLYPH_DIR = DATA / "font-glyphs"
MUSHAF_DIR = DATA / "mushaf"
FONT_DIR = DATA / "fonts"

FORMS = ("NFC", "NFD")

DEFINITION = (
    "A glyph is listed when the font's GSUB table substitutes it for a run of "
    "glyphs holding two or more letters and at least one combining mark. Each "
    "glyph of the run is traced back to a codepoint — through the font's cmap, "
    "its single substitutions (the positional forms) and, where a ligature is "
    "built from ligatures, through those — and takes that codepoint's Unicode "
    "category: Lo is a letter, Mn a mark, and anything else (the kashida and the "
    "small wāw and yāʾ, which are Lm; digits; signs) is neither. Such a glyph "
    "draws a stretch of a word, marks included, as one picture, so the signs it "
    "shows are those of the drawing and need not be those of the codepoints it "
    "replaces. A letter fused with its own mark, two marks fused together, and "
    "a ligature of letters alone are not listed."
)


def available() -> bool:
    """Whether the two shaping libraries can be imported here."""
    try:
        import fontTools  # noqa: F401
        import uharfbuzz  # noqa: F401
    except ImportError:
        return False
    return True


def printed(doc: dict) -> list[str]:
    """Each word as the release prints it: its ``after`` marks re-attached.

    The sajdah line and the waqf signs follow their word with no space, and the
    font's sajdah glyphs match on the line, so a word must be shaped with them.
    ۞ stands before its word as a token of its own and joins nothing.
    """
    out = list(doc["words"])
    for position, kind in doc["marks"]:
        mark = doc["mark_types"][kind]
        if mark["side"] == "after":
            out[position] += mark["sign"]
    return out


class Font:
    """What one font's tables say about its glyphs, read with fontTools.

    Glyphs are handled by id throughout.  HarfBuzz reports a glyph's name cut
    to 63 characters and these fonts have names of over a hundred, so a name
    is only ever something to print.
    """

    def __init__(self, path: Path) -> None:
        from fontTools.ttLib import TTFont
        self.tt = TTFont(path)
        self.name = self.tt.getGlyphName
        self._id = self.tt.getGlyphID
        # One glyph can serve several codepoints (Warsh draws U+06D6 and U+08CC
        # with the same one), so all of them are kept, lowest first.
        self._cps: dict[int, list[int]] = {}
        for cp, glyph in sorted(self.tt.getBestCmap().items()):
            self._cps.setdefault(self._id(glyph), []).append(cp)
        self._single: dict[int, list[int]] = {}   # a form -> the glyphs it can stand for
        self.rules: dict[int, list[list[int]]] = {}   # ligature -> the runs it replaces
        for lookup in self.tt["GSUB"].table.LookupList.Lookup:
            for sub in lookup.SubTable:
                sub = getattr(sub, "ExtSubTable", sub)
                kind = type(sub).__name__
                if kind == "SingleSubst":
                    for src, dst in sub.mapping.items():
                        self._single.setdefault(self._id(dst), []).append(self._id(src))
                elif kind == "LigatureSubst":
                    for first, ligatures in sub.ligatures.items():
                        for lig in ligatures:
                            run = [self._id(g) for g in (first, *lig.Component)]
                            self.rules.setdefault(self._id(lig.LigGlyph), []).append(run)

    def codepoints(self, glyph: int) -> list[int]:
        """The codepoints a glyph can draw: its own in the cmap, then those of
        every glyph it is a form of, and so on down the chain.  Warsh and
        Qālūn map their positional forms at the presentation-form codepoints as
        well, so the chain is followed even from a mapped glyph; and one final
        form serves both yāʾ and alif maqṣūrah, so a glyph can have two sources.
        """
        out: list[int] = []
        seen, queue = set(), [glyph]
        while queue:
            glyph = queue.pop(0)
            if glyph in seen:
                continue
            seen.add(glyph)
            out += [cp for cp in self._cps.get(glyph, []) if cp not in out]
            queue += self._single.get(glyph, [])    # one form can serve two letters
        return out

    def kind(self, glyph: int) -> str:
        """``letter``, ``mark`` or ``other``, by the category of its codepoint."""
        cps = self.codepoints(glyph)
        category = unicodedata.category(chr(cps[0])) if cps else ""
        return {"Lo": "letter", "Mn": "mark"}.get(category, "other")

    def expand(self, run: list[int], _seen: frozenset = frozenset()) -> list[list[int]]:
        """Every run of plain glyphs a run stands for, ligatures inside it opened."""
        out: list[list[int]] = [[]]
        for glyph in run:
            if glyph in self.rules and glyph not in _seen:
                parts = [e for r in self.rules[glyph]
                         for e in self.expand(r, _seen | {glyph})]
            else:
                parts = [[glyph]]
            out = [head + tail for head in out for tail in parts]
        return out

    def assembled_from(self, glyph: int) -> list[str] | None:
        """The glyphs a composite glyph is put together from, or None for an outline."""
        g = self.tt["glyf"][self.name(glyph)]
        return [c.glyphName for c in g.components] if g.isComposite() else None

    def carries_a_mark(self, run: list[int]) -> bool:
        """Whether a run of plain glyphs holds two or more letters and a mark."""
        kinds = [self.kind(g) for g in run]
        return kinds.count("letter") >= 2 and "mark" in kinds

    def mark_ligatures(self) -> dict[int, list[list[int]]]:
        """``{glyph: the runs it can replace}`` for runs of 2+ letters with a mark."""
        out = {}
        for glyph in self.rules:
            runs = [r for r in self.expand([glyph]) if self.carries_a_mark(r)]
            if runs:
                out[glyph] = runs
        return out


class Shaper:
    """One font, shaped with HarfBuzz at its default features."""

    def __init__(self, path: Path) -> None:
        import uharfbuzz as hb
        self._hb = hb
        self.font = hb.Font(hb.Face(hb.Blob.from_file_path(str(path))))

    def shape(self, text: str) -> list[tuple]:
        """``[(glyph id, cluster, x advance, y advance, x offset, y offset)]``, visual order."""
        buf = self._hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        self._hb.shape(self.font, buf)
        return [(i.codepoint, i.cluster, p.x_advance, p.y_advance, p.x_offset, p.y_offset)
                for i, p in zip(buf.glyph_infos, buf.glyph_positions)]


def _drawing(shaped: list[tuple]) -> list[tuple]:
    """A shaped word without its cluster numbers.

    Clusters index the input string, which normalisation re-writes; glyphs and
    positions are what reaches the screen.
    """
    return [(g, *rest) for g, _, *rest in shaped]


def _replaced_text(font: Font, text: str, shaped: list[tuple], index: int) -> str:
    """The codepoints the glyph at ``index`` was shaped from.

    HarfBuzz reports clusters, and a cluster can hold more than one glyph: a
    waqf sign after the word joins the cluster of the glyph before it and is
    still drawn as itself.  So the cluster's text is taken, and the codepoint of
    every other glyph sharing the cluster is taken back out of it, from the end.
    """
    cluster = shaped[index][1]
    later = [c for _, c, *_ in shaped if c > cluster]
    run = text[cluster:min(later) if later else len(text)]
    for i, (glyph, c, *_) in enumerate(shaped):
        if i == index or c != cluster:
            continue
        for plain in font.expand([glyph])[0]:
            at = max((run.rfind(chr(cp)) for cp in font.codepoints(plain)), default=-1)
            if at >= 0:
                run = run[:at] + run[at + 1:]
    return run


def derive(doc: dict) -> dict:
    """The font-glyph layer of one muṣḥaf, measured from its file and its font."""
    path = FONT_DIR / doc["font"]["file"]
    font = Font(path)
    ligatures = font.mark_ligatures()
    shaper = Shaper(path)

    found: dict[int, dict] = {}
    changed: list[dict] = []
    words = printed(doc)
    for position, text in enumerate(words):
        shaped = shaper.shape(text)
        for index, (glyph, *_) in enumerate(shaped):
            if glyph in ligatures:
                entry = found.setdefault(glyph, {"texts": [], "words": []})
                run = _replaced_text(font, text, shaped, index)
                if run not in entry["texts"]:
                    entry["texts"].append(run)
                if not entry["words"] or entry["words"][-1] != position:
                    entry["words"].append(position)
        drawn = _drawing(shaped)
        for form in FORMS:
            other = unicodedata.normalize(form, text)
            if other != text and _drawing(shaper.shape(other)) != drawn:
                changed.append({"position": position, "form": form})

    glyphs = []
    for glyph, entry in sorted(found.items(), key=lambda kv: (kv[1]["words"][0], kv[0])):
        row = {"glyph": font.name(glyph),
               "replaces": [[font.name(g) for g in run] for run in ligatures[glyph]]}
        parts = font.assembled_from(glyph)
        if parts is not None:
            row["assembled_from"] = parts
        row |= {"text": entry["texts"], "word_count": len(entry["words"]),
                "words": entry["words"]}
        glyphs.append(row)
    listed = sorted({p for g in glyphs for p in g["words"]})
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "generated": stamp.generated(),
        "mushaf": doc["mushaf"]["key"],
        "font": {"file": doc["font"]["file"], "sha256": doc["font"]["sha256"]},
        "shaper": "HarfBuzz, default features",
        "definition": DEFINITION,
        "words": {
            "addressed_by": "position in the muṣḥaf file's `words`, from 0",
            "shaped_as": "the word with its `after` marks re-attached, as the release prints it",
            "checked": len(words),
        },
        "normalization": {
            "forms": list(FORMS),
            "question": "does HarfBuzz draw the word differently with this font after normalisation?",
            "words_drawn_differently": changed,
        },
        "glyph_count": len(glyphs),
        "word_count": len(listed),
        "glyphs": glyphs,
    }


def serialise(layer: dict) -> str:
    """One glyph per line: a changed word list is a one-line diff."""
    head = {k: v for k, v in layer.items() if k != "glyphs"}
    body = json.dumps(head, ensure_ascii=False, indent=1)
    rows = ",\n".join("  " + json.dumps(g, ensure_ascii=False, separators=(",", ":"))
                      for g in layer["glyphs"])
    return body[:-2] + ',\n "glyphs": [\n' + rows + "\n ]\n}\n"


def write_all(docs: dict[str, dict] | None = None) -> dict[str, dict]:
    """Write ``data/font-glyphs/<key>.json`` for every muṣḥaf that names a font."""
    if docs is None:
        docs = {p.stem: json.loads(p.read_text(encoding="utf-8"))
                for p in sorted(MUSHAF_DIR.glob("*.json"))
                if p.name != "manifest.json" and ".nested" not in p.name}
    GLYPH_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for key, doc in docs.items():
        if "font" not in doc or "marks" not in doc:
            continue
        out[key] = derive(doc)
        (GLYPH_DIR / f"{key}.json").write_text(serialise(out[key]), encoding="utf-8")
    return out

