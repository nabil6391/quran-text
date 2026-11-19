"""The display gate: what each muṣḥaf's own font does to its text.

Two statements are made to anyone who displays this text, and both are
measured here from data/mushaf/, data/fonts/ and nothing else.

*HarfBuzz draws a normalised word as it draws the stored one.*  The words are
stored as the release writes them, not in NFC, and docs/format.md says to
display them untouched, because a shaper that does not decompose NFC's letters
again draws them from a fallback font.  HarfBuzz does: every word of every
muṣḥaf is shaped as stored, after NFC and after NFD, and the glyphs and their
positions must be identical.  This says nothing about CoreText or DirectWrite.

*The font-glyph layer is complete and current.*  data/font-glyphs/<key>.json
lists the words the font draws with one glyph for a stretch of letters and
their marks.  It is checked two ways: against a second derivation written here,
which shares no code with the pipeline's, and against what the pipeline
produces now.

    pip install -r conformance/requirements.txt
    pytest conformance/display_test.py
"""
import hashlib
import json
import pathlib
import sys
import unicodedata

import pytest
import uharfbuzz as hb
from fontTools.ttLib import TTFont

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from qurantext import font_glyphs  # noqa: E402

MUSHAF = ROOT / "data" / "mushaf"
FONTS = ROOT / "data" / "fonts"
LAYERS = ROOT / "data" / "font-glyphs"
KASHIDA = "\u0640"
KEYS = sorted(p.stem for p in MUSHAF.glob("*.json")
              if "font" in json.loads(p.read_text(encoding="utf-8")))


class Case:
    def __init__(self, key):
        self.key = key
        self.doc = json.loads((MUSHAF / f"{key}.json").read_text(encoding="utf-8"))
        self.committed = (LAYERS / f"{key}.json").read_text(encoding="utf-8")
        self.layer = json.loads(self.committed)
        self.path = FONTS / self.doc["font"]["file"]
        self.tt = TTFont(self.path)
        self.hb = hb.Font(hb.Face(hb.Blob.from_file_path(str(self.path))))
        self.words = list(self.doc["words"])
        for position, kind in self.doc["marks"]:
            mark = self.doc["mark_types"][kind]
            if mark["side"] == "after":
                self.words[position] += mark["sign"]

    def glyph_ids(self, text):
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.hb, buf)
        return [i.codepoint for i in buf.glyph_infos]

    def listed(self):
        return {p for g in self.layer["glyphs"] for p in g["words"]}


@pytest.fixture(scope="module", params=KEYS)
def case(request):
    return Case(request.param)


def second_derivation(case):
    """``{glyph name: [positions]}`` by a second implementation of the definition.

    The pipeline opens a ligature recursively and asks each plain glyph for one
    codepoint.  This works bottom-up and shares no code with it: every glyph is
    given the list of Unicode categories it draws, starting from the cmap and
    growing through the single substitutions and the ligature rules until
    nothing changes.  As in the definition, a ligature rule outranks a
    codepoint of the glyph's own, so the alif-with-maddah the fonts build from
    alif and maddah is a letter and a mark.
    """
    tt = case.tt
    singles, rules = {}, {}
    for lookup in tt["GSUB"].table.LookupList.Lookup:
        for sub in lookup.SubTable:
            sub = getattr(sub, "ExtSubTable", sub)
            if type(sub).__name__ == "SingleSubst":
                for src, dst in sub.mapping.items():
                    singles.setdefault(dst, src)
            elif type(sub).__name__ == "LigatureSubst":
                for first, ligs in sub.ligatures.items():
                    for lig in ligs:
                        rules.setdefault(lig.LigGlyph, []).append([first, *lig.Component])
    cmap = {}
    for cp, name in sorted(tt.getBestCmap().items()):
        cmap.setdefault(name, cp)
    cats = {name: [[unicodedata.category(chr(cp))]] for name, cp in cmap.items()
            if name not in rules}          # glyph -> one category list per way of making it
    changed = True
    while changed:
        changed = False
        for dst, src in singles.items():
            if dst not in cats and dst not in rules and src in cats:
                cats[dst] = cats[src]
                changed = True
        for out, runs in rules.items():
            if out not in cats and all(g in cats for run in runs for g in run):
                ways = []
                for run in runs:
                    acc = [[]]
                    for g in run:
                        acc = [head + tail for head in acc for tail in cats[g]]
                    ways += acc
                cats[out] = ways
                changed = True
    wanted = {tt.getGlyphID(out) for out in rules if out in cats
              and any(way.count("Lo") >= 2 and "Mn" in way for way in cats[out])}
    found = {}
    for position, text in enumerate(case.words):
        for gid in sorted(set(case.glyph_ids(text)) & wanted):
            found.setdefault(tt.getGlyphName(gid), []).append(position)
    return found


def measured_superset(case):
    """``{glyph name: {positions}}`` read from shaping alone, no GSUB rule opened.

    HarfBuzz says which characters each cluster came from.  A cluster holding
    two or more letters and a mark is the only place a listed glyph can be, so
    every glyph in such a cluster is recorded.  Several glyphs can share a
    cluster, so this over-counts; it cannot under-count.
    """
    tt = case.tt
    found = {}
    for position, text in enumerate(case.words):
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(case.hb, buf)
        starts = sorted({i.cluster for i in buf.glyph_infos})
        for info in buf.glyph_infos:
            end = next((c for c in starts if c > info.cluster), len(text))
            cats = [unicodedata.category(c) for c in text[info.cluster:end]]
            if cats.count("Lo") >= 2 and "Mn" in cats:
                found.setdefault(tt.getGlyphName(info.codepoint), set()).add(position)
    return found


def test_every_mushaf_with_a_font_is_covered():
    assert KEYS, "no muṣḥaf file names a font"
    assert sorted(p.stem for p in LAYERS.glob("*.json")) == KEYS


def test_the_font_is_the_one_the_mushaf_names(case):
    assert hashlib.sha256(case.path.read_bytes()).hexdigest() == case.doc["font"]["sha256"]
    assert case.layer["font"] == {"file": case.doc["font"]["file"],
                                  "sha256": case.doc["font"]["sha256"]}


def test_the_layer_agrees_with_a_second_derivation(case):
    """Nothing missed and nothing extra, glyph by glyph and word by word."""
    committed = {g["glyph"]: g["words"] for g in case.layer["glyphs"]}
    assert committed == second_derivation(case)


def test_every_listed_word_is_where_shaping_says_it_can_be(case):
    """Nothing extra: each listed glyph, in each listed word, sits in a cluster
    of two or more letters and a mark, by HarfBuzz's own account of the word."""
    superset = measured_superset(case)
    for g in case.layer["glyphs"]:
        assert set(g["words"]) <= superset.get(g["glyph"], set()), (case.key, g["glyph"])


def test_the_committed_layer_is_what_the_pipeline_produces(case):
    assert case.committed == font_glyphs.serialise(font_glyphs.derive(case.doc)), (
        f"data/font-glyphs/{case.key}.json is stale: run python3 pipeline/font_glyphs.py")


def test_the_counts_are_the_lists(case):
    layer = case.layer
    assert layer["glyph_count"] == len(layer["glyphs"])
    assert layer["word_count"] == len(case.listed())
    assert layer["words"]["checked"] == case.doc["mushaf"]["word_count"] == len(case.words)
    for g in layer["glyphs"]:
        assert g["word_count"] == len(g["words"]) > 0
        assert g["words"] == sorted(set(g["words"]))


def test_a_listed_word_is_drawn_with_fewer_glyphs_than_it_has_codepoints(case):
    for position in case.listed():
        text = case.words[position]
        assert len(case.glyph_ids(text)) < len(text), (case.key, position)


def _within(part, whole):
    """Whether ``part``'s characters occur in ``whole`` in order (a mark the
    glyph does not take can stand between two it does)."""
    it = iter(whole)
    return all(ch in it for ch in part)


def test_the_replaced_text_is_part_of_the_word(case):
    for g in case.layer["glyphs"]:
        for position in g["words"]:
            assert any(_within(t, case.words[position]) for t in g["text"]), (g["glyph"], position)


def test_the_replaced_text_is_the_run_codepoint_for_glyph(case):
    """`text` is exactly what the glyph took: one codepoint for each plain glyph
    of `replaces`, each a codepoint that glyph can draw, and none besides.  A waqf sign
    that only shares the cluster, or a character lost, breaks the count; a run
    left with a ligature in it has a glyph no codepoint reaches."""
    tt = case.tt
    draws = {}                             # glyph name -> every codepoint it can draw
    for cp, name in tt.getBestCmap().items():
        draws.setdefault(name, set()).add(cp)
    forms = {}
    for lookup in tt["GSUB"].table.LookupList.Lookup:
        for sub in lookup.SubTable:
            sub = getattr(sub, "ExtSubTable", sub)
            if type(sub).__name__ == "SingleSubst":
                for src, dst in sub.mapping.items():
                    forms.setdefault(dst, set()).add(src)

    def reach(name, seen=None):
        seen = set() if seen is None else seen
        if name in seen:
            return set()
        seen.add(name)
        out = set(draws.get(name, set()))
        for src in forms.get(name, ()):
            out |= reach(src, seen)
        return out

    def is_the_run(text, run):
        """Every glyph of the run takes one character it can draw, none left
        over.  Not position by position: HarfBuzz puts the marks of a letter
        in its own order before the font's rules see them."""
        left = list(text)
        for name in run:
            hit = next((ch for ch in left if ord(ch) in reach(name)), None)
            if hit is None:
                return False
            left.remove(hit)
        return not left

    for g in case.layer["glyphs"]:
        for text in g["text"]:
            # NFD: HarfBuzz may have opened a precomposed letter to match the rule
            spellings = {text, unicodedata.normalize("NFD", text)}
            assert any(is_the_run(t, run) for t in spellings for run in g["replaces"]), (
                case.key, g["glyph"], text)


def test_normalisation_does_not_change_a_drawing(case):
    """Shaped here, not read from the layer: glyph ids and positions, three ways."""
    def drawing(text):
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(case.hb, buf)
        return [(i.codepoint, p.x_advance, p.y_advance, p.x_offset, p.y_offset)
                for i, p in zip(buf.glyph_infos, buf.glyph_positions)]
    compared, differ = 0, []
    for position, text in enumerate(case.words):
        stored = None
        for form in ("NFC", "NFD"):
            other = unicodedata.normalize(form, text)
            if other == text:
                continue
            stored = stored or drawing(text)
            compared += 1
            if drawing(other) != stored:
                differ.append((position, form))
    assert compared > 10000, "the comparison must not be vacuous"
    assert differ == [], f"{case.key}: drawn differently after normalisation: {differ[:5]}"
    assert case.layer["normalization"]["words_drawn_differently"] == []


def test_every_word_under_a_sajdah_line_is_listed(case):
    """docs/format.md says so.  Qālūn and Warsh print no sajdah line."""
    kinds = [m["kind"] for m in case.doc["mark_types"]]
    under = {p for p, k in case.doc["marks"] if kinds[k] == "sajdah_line"}
    assert under <= case.listed()


def test_assembled_glyphs_are_named_as_such(case):
    """docs/format.md: a glyph assembled from other glyphs says which; the rest
    are outlines of their own, whose signs cannot be read out of the font."""
    glyf = case.tt["glyf"]
    for g in case.layer["glyphs"]:
        parts = ([c.glyphName for c in glyf[g["glyph"]].components]
                 if glyf[g["glyph"]].isComposite() else None)
        assert g.get("assembled_from") == parts, (case.key, g["glyph"])


def test_the_kashida_is_part_of_what_the_font_matches():
    """docs/format.md, the 17:7 example in Ḥafṣ: found in the data, not typed here."""
    case = Case("hafs")
    surah = next(s for s in case.doc["surahs"] if s["number"] == 17)
    ayah = surah["first_ayah"] + 7 - 1
    span = range(case.doc["ayah_starts"][ayah], case.doc["ayah_starts"][ayah + 1])
    here = [p for p in span if p in case.listed() and KASHIDA in case.words[p]]
    assert len(here) == 1
    word = case.words[here[0]]
    glyph = next(g for g in case.layer["glyphs"] if here[0] in g["words"])
    gid = case.tt.getGlyphID(glyph["glyph"])
    assert gid in case.glyph_ids(word)
    bare = case.glyph_ids(word.replace(KASHIDA, ""))
    listed = {case.tt.getGlyphID(g["glyph"]) for g in case.layer["glyphs"]}
    assert not listed.intersection(bare), "no listed glyph, once the kashida is out"
    # the two signs the glyph is said to add are in no codepoint of the word
    assert "\u06e5" not in word and "\u0653" not in word
