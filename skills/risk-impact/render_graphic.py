#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Tobias Hofmann
"""Render the risk & impact assessment as one SVG (optionally PNG).

The agent writes the numbers it already works out for the report into a small
JSON file; this script draws them the same way every time, so graphics from
different runs are comparable and cannot disagree with the report.

Panels:
  1. Matrix: risk (y) vs business impact (x); cell colour = max(risk, business),
     dot size = change impact, faded dashed dots = earlier runs on this branch.
  2. Breakdown bars per rating: base level, what raises it, what lowers it.
  3. Hotspots (only if `hotspots` has >= 3 entries): changed files by test
     fan-in (x), changed-line coverage (y), dot size = lines changed.

Standard library only. PNG needs `cairosvg` or `rsvg-convert` (optional).

Usage:
  render_graphic.py report.json [-o out.svg] [--prev previous-report.json] [--png]

Input JSON (all keys except `title` and `ratings` optional):
{
  "title": "feature/discount-rules",
  "scope": "since fork from main · 6 commits · 14 files · +412/−96",
  "overall": "High",              # default: derived from max(ratings); may be one level
                                  # lower than max if mitigation is evidenced
  "provisional": false,
  "ratings": {
    "risk":     {"score": 4, "steps": [{"label": "Base: shared pricing module", "delta": 3},
                                       {"label": "Fan-in: 12 test files", "delta": 1},
                                       {"label": "Changed lines 86% covered", "delta": -1}]},
    "business": {"score": 3, "direction": "positive", "steps": [...]},
    "change":   {"score": 3, "steps": [...]}
  },
  "hotspots": [{"path": "src/price.py", "test_files": 12, "coverage": 40, "lines": 120}],
  "history":  [{"risk": 5, "business": 3, "change": 3}],
  "labels":   {"risk": "Risiko", ...}     # optional translations, see LABELS
}
The first step of each rating is its base level; later steps are modifiers
(positive raises, negative lowers). base + sum(modifiers), clamped to 1..5,
must equal `score` (a warning is printed otherwise).
"""
import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

LEVELS = {1: "Low", 2: "Low", 3: "Medium", 4: "High", 5: "Critical"}
LABELS = {
    "risk": "Risk", "business": "Business impact", "change": "Change impact",
    "overall": "Overall", "provisional": "provisional", "base": "base", "raises": "raises",
    "lowers": "lowers", "positive": "positive", "negative": "negative", "mixed": "mixed",
    "Low": "Low", "Medium": "Medium", "High": "High", "Critical": "Critical",
    "note": "Dot size = change impact ({c}/5). Cell = max(risk, business).",
    "note_history": "Faded dots = earlier runs on this branch.",
    "hotspots": "Hotspots: where review effort should go",
    "fanin": "Test fan-in (test files referencing the changed code)",
    "cov": "Changed-line coverage %", "nocov": "no coverage data",
    "hot_note": "Dot size = changed lines. Shaded corner: heavily used, poorly covered.",
    "fan": "fan-in", "covs": "cov", "lines": "lines",
}
# palette (dark card; readable on light and dark host pages)
BG, CARD_STROKE, TEXT, MUTED = "#1c1c1b", "#3a3a38", "#f0efec", "#a8a8a4"
CELL = {"Low": "#19301a", "Medium": "#3d3418", "High": "#4d3329", "Critical": "#45201f"}
ACCENT = {"Low": "#3fa34d", "Medium": "#c9a227", "High": "#e8825c", "Critical": "#e5534b"}
BASE_C, RAISE_C, LOWER_C, TRACK_C = "#8e8e88", "#ec8059", "#13a10e", "#262625"
FONT = "system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"


def level_of(score):
    return LEVELS[max(1, min(5, score))]


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate(d):
    """Return (errors, warnings)."""
    err, warn = [], []
    if not d.get("title"):
        err.append("`title` is required")
    r = d.get("ratings") or {}
    for k in ("risk", "business", "change"):
        s = (r.get(k) or {}).get("score")
        if not isinstance(s, int) or isinstance(s, bool) or not 1 <= s <= 5:
            err.append(f"ratings.{k}.score must be an integer 1..5")
            continue
        steps = r[k].get("steps") or []
        if steps:
            total = sum(int(x.get("delta", 0)) for x in steps)
            if max(1, min(5, total)) != s:
                warn.append(f"ratings.{k}: steps add up to {max(1, min(5, total))} but score is {s}")
        else:
            warn.append(f"ratings.{k}: no steps, so no breakdown can be drawn")
    if not err:
        mx = max(r[k]["score"] for k in ("risk", "business", "change"))
        ov = d.get("overall")
        if ov is not None:
            if ov not in ("Low", "Medium", "High", "Critical"):
                err.append("`overall` must be Low, Medium, High or Critical")
            else:
                order = ["Low", "Medium", "High", "Critical"]
                floor = order.index(level_of(mx))
                if not floor - 1 <= order.index(ov) <= floor:
                    err.append(f"`overall` {ov} is not allowed: max rating {mx} gives {level_of(mx)} "
                               f"(at most one level lower with evidenced mitigation)")
                elif order.index(ov) < floor:
                    warn.append(f"overall {ov} is one level below max rating ({level_of(mx)}); "
                                "the report must state the mitigation")
    return err, warn


def merge_history(d, prev):
    """Add the previous run (and its history) to d['history']; returns the new list."""
    hist = list(prev.get("history") or [])
    pr = prev.get("ratings") or {}
    try:
        hist.append({k: pr[k]["score"] for k in ("risk", "business", "change")})
    except (KeyError, TypeError):
        pass
    own = list(d.get("history") or [])
    out = []
    for h in hist + own:
        if h not in out:
            out.append(h)
    return out[-5:]


# ------------------------------------------------------------------ drawing

def esc(s):
    return escape(str(s))


def trunc(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def shape(level, cx, cy, size, color):
    """Level glyph so the level is not carried by colour alone."""
    r = size / 2
    if level == "Low":
        return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="2.5"/>'
    if level == "Medium":
        pts = f"{cx},{cy - r} {cx + r},{cy} {cx},{cy + r} {cx - r},{cy}"
        return f'<polygon points="{pts}" fill="none" stroke="{color}" stroke-width="2.5"/>'
    if level == "High":
        return (f'<rect x="{cx - r}" y="{cy - r}" width="{size}" height="{size}" fill="none" '
                f'stroke="{color}" stroke-width="2.5"/>')
    pts = f"{cx},{cy - r} {cx + r},{cy + r} {cx - r},{cy + r}"
    return f'<polygon points="{pts}" fill="{color}" stroke="{color}" stroke-width="2.5" stroke-linejoin="round"/>'


def dot_radius(c):
    return 9 + 4 * c


def matrix(o, d, L, x0, y0, cw=114, gap=4):
    r = d["ratings"]
    n = 5
    size = n * cw + (n - 1) * gap
    for ri in range(n):               # row 0 = risk 5
        for ci in range(n):
            risk, biz = n - ri, ci + 1
            lvl = level_of(max(risk, biz))
            o.append(f'<rect x="{x0 + ci * (cw + gap)}" y="{y0 + ri * (cw + gap)}" width="{cw}" '
                     f'height="{cw}" rx="4" fill="{CELL[lvl]}"/>')

    def centre(risk, biz):
        return (x0 + (biz - 1) * (cw + gap) + cw / 2, y0 + (n - risk) * (cw + gap) + cw / 2)

    for h in d.get("history") or []:
        try:
            cx, cy = centre(h["risk"], h["business"])
            o.append(f'<circle cx="{cx}" cy="{cy}" r="{dot_radius(h["change"])}" fill="none" '
                     f'stroke="{TEXT}" stroke-opacity="0.55" stroke-width="2" stroke-dasharray="4 4"/>')
        except (KeyError, TypeError):
            continue
    cx, cy = centre(r["risk"]["score"], r["business"]["score"])
    c = r["change"]["score"]
    o.append(f'<circle cx="{cx}" cy="{cy}" r="{dot_radius(c)}" fill="#f0efec" stroke="#111" stroke-width="4"/>')
    o.append(f'<text x="{cx}" y="{cy + 7}" text-anchor="middle" font-size="20" font-weight="700" '
             f'fill="#111">{c}</text>')
    for i in range(n):                # tick labels
        o.append(f'<text x="{x0 + i * (cw + gap) + cw / 2}" y="{y0 + size + 28}" text-anchor="middle" '
                 f'font-size="20" fill="{MUTED}">{i + 1}</text>')
        o.append(f'<text x="{x0 - 14}" y="{y0 + i * (cw + gap) + cw / 2 + 7}" text-anchor="end" '
                 f'font-size="20" fill="{MUTED}">{n - i}</text>')
    o.append(f'<text x="{x0 + size / 2}" y="{y0 + size + 62}" text-anchor="middle" font-size="22" '
             f'fill="{TEXT}">{esc(L["business"])}</text>')
    ax = x0 - 54
    o.append(f'<text transform="translate({ax},{y0 + size / 2}) rotate(-90)" text-anchor="middle" '
             f'font-size="22" fill="{TEXT}">{esc(L["risk"])}</text>')
    note = L["note"].format(c=c) + ((" " + L["note_history"]) if d.get("history") else "")
    lines, cur = [], ""
    for w in note.split(" "):
        if len(cur) + len(w) + 1 > 56:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    lines.append(cur)
    ny = y0 + size + 108
    for i, ln in enumerate(lines):
        o.append(f'<text x="{x0 - 42}" y="{ny + i * 28}" font-size="20" fill="{MUTED}">{esc(ln)}</text>')
    return ny + (len(lines) - 1) * 28


def breakdown(o, d, L, x, y, w=713, track=302, unit=None):
    unit = unit or track / 5
    label_x = x + track + 18
    for key in ("risk", "business", "change"):
        rt = d["ratings"][key]
        score = rt["score"]
        lvl = level_of(score)
        extra = ""
        if key == "business" and rt.get("direction") in ("positive", "negative", "mixed"):
            extra = f" · {L[rt['direction']]}"
        o.append(f'<text x="{x}" y="{y + 12}" font-size="25" font-weight="600" fill="{TEXT}">{esc(L[key])}</text>')
        o.append(f'<text x="{x + w}" y="{y + 12}" text-anchor="end" font-size="25" fill="{TEXT}">'
                 f'{score}/5 · {esc(L[lvl])}{esc(extra)}</text>')
        y += 38
        cum = 0
        for i, st in enumerate(rt.get("steps") or []):
            delta = int(st.get("delta", 0))
            lab = trunc(st.get("label", ""), 36)
            o.append(f'<rect x="{x}" y="{y}" width="{track}" height="20" rx="4" fill="{TRACK_C}"/>')
            if i == 0:
                new = max(0, min(5, delta))
                o.append(f'<rect x="{x}" y="{y}" width="{new * unit}" height="20" rx="4" fill="{BASE_C}"/>')
                text = f"{delta}  {lab}"
                cum = new
            else:
                new = max(0, min(5, cum + delta))
                lo, hi = min(cum, new), max(cum, new)
                if hi > lo:
                    col = RAISE_C if delta > 0 else LOWER_C
                    o.append(f'<rect x="{x + lo * unit}" y="{y}" width="{(hi - lo) * unit}" height="20" '
                             f'rx="4" fill="{col}"/>')
                text = f"{'+' if delta > 0 else '−' if delta < 0 else '±'}{abs(delta)}  {lab}"
                cum = new
            o.append(f'<text x="{label_x}" y="{y + 16}" font-size="20" fill="#cfcfca">{esc(text)}</text>')
            y += 36
        y += 24
    return y


def legend(o, L, x, y):
    for col, key, w in ((BASE_C, "base", 100), (RAISE_C, "raises", 120), (LOWER_C, "lowers", 120)):
        o.append(f'<rect x="{x}" y="{y}" width="16" height="16" rx="3" fill="{col}"/>')
        o.append(f'<text x="{x + 26}" y="{y + 14}" font-size="19" fill="{MUTED}">{esc(L[key])}</text>')
        x += w
    return y + 30


def hotspots(o, d, L, x0, y0, plot_w=586, plot_h=320, right_x=746, right_w=713):
    hs = d["hotspots"]
    maxfan = max([h.get("test_files") or 0 for h in hs] + [6])
    maxlines = max([h.get("lines") or 1 for h in hs] + [1])
    o.append(f'<text x="{x0 - 42}" y="{y0 - 14}" font-size="25" font-weight="600" fill="{TEXT}">'
             f'{esc(L["hotspots"])}</text>')
    px, py = x0, y0 + 12
    o.append(f'<rect x="{px}" y="{py}" width="{plot_w}" height="{plot_h}" rx="4" fill="#232322"/>')
    # danger corner: fan-in >= 3 and coverage < 80%
    fx = px + 3 / maxfan * plot_w
    o.append(f'<rect x="{fx}" y="{py + plot_h * 0.2}" width="{px + plot_w - fx}" height="{plot_h * 0.8}" '
             f'fill="{CELL["Critical"]}" opacity="0.9"/>')

    def pos(h):
        fan = h.get("test_files") or 0
        cov = h.get("coverage")
        cx = px + 16 + fan / maxfan * (plot_w - 32)
        cy = py + plot_h + 34 if cov is None else py + plot_h - 14 - (cov / 100) * (plot_h - 28)
        return cx, cy

    ranked = sorted(hs, key=lambda h: -((h.get("test_files") or 0) * (100 - (h.get("coverage") if h.get("coverage") is not None else 100) + 1)
                                       + (h.get("lines") or 0) / 1000))
    top = ranked[:6]
    for h in hs:
        cx, cy = pos(h)
        rad = 5 + 11 * math.sqrt((h.get("lines") or 1) / maxlines)
        col = "#8e8e88" if h.get("coverage") is None else (ACCENT["High"] if h in top[:3] else "#cfcfca")
        o.append(f'<circle cx="{cx}" cy="{cy}" r="{rad:.1f}" fill="{col}" fill-opacity="0.85" stroke="#111" stroke-width="1.5"/>')
    for i, h in enumerate(top[:4]):
        cx, cy = pos(h)
        name = trunc(Path(h["path"]).name, 22)
        anchor = "end" if cx > px + plot_w * 0.7 else "start"
        dx = -14 if anchor == "end" else 14
        o.append(f'<text x="{cx + dx}" y="{cy - 12}" text-anchor="{anchor}" font-size="17" fill="{TEXT}">{esc(name)}</text>')
    for v in (0, 50, 100):
        yy = py + plot_h - 14 - v / 100 * (plot_h - 28)
        o.append(f'<text x="{px - 8}" y="{yy + 6}" text-anchor="end" font-size="17" fill="{MUTED}">{v}</text>')
    for v in sorted({0, 3, maxfan}):
        xx = px + 16 + v / maxfan * (plot_w - 32)
        o.append(f'<text x="{xx}" y="{py + plot_h + 22}" text-anchor="middle" font-size="17" fill="{MUTED}">{v}</text>')
    o.append(f'<text x="{px}" y="{py + plot_h + 66}" font-size="19" fill="{MUTED}">{esc(L["fanin"])}</text>')
    o.append(f'<text transform="translate({px - 54},{py + plot_h / 2}) rotate(-90)" text-anchor="middle" '
             f'font-size="19" fill="{MUTED}">{esc(L["cov"])}</text>')
    if any(h.get("coverage") is None for h in hs):
        o.append(f'<text x="{px + plot_w - 6}" y="{py + plot_h + 44}" text-anchor="end" font-size="16" '
                 f'fill="{MUTED}">{esc(L["nocov"])} (row below axis)</text>')
    o.append(f'<text x="{px}" y="{py + plot_h + 96}" font-size="19" fill="{MUTED}">{esc(L["hot_note"])}</text>')
    # text list of the top files (the same information, readable without the plot)
    ty = y0 + 36
    for h in ranked[:8]:
        cov = "n/a" if h.get("coverage") is None else f'{h["coverage"]:g}%'
        txt = (f'{trunc(h["path"], 40)}   {L["fan"]} {h.get("test_files") or 0} · {L["covs"]} {cov} · '
               f'{h.get("lines") or 0} {L["lines"]}')
        o.append(f'<text x="{right_x}" y="{ty}" font-size="19" fill="#cfcfca">{esc(txt)}</text>')
        ty += 34
    return py + plot_h + 96


def render(d, labels_override=None):
    L = {**LABELS, **(d.get("labels") or {})}
    r = d["ratings"]
    mx = max(r[k]["score"] for k in ("risk", "business", "change"))
    overall = d.get("overall") or level_of(mx)
    o = []
    X0, Y0 = 116, 168
    # right column first, to know its height
    right = []
    ry = breakdown(right, d, L, 746, Y0 + 6)
    ry = legend(right, L, 746, ry - 6)
    left = []
    ly = matrix(left, d, L, X0, Y0)
    bottom = max(ly, ry) + 30
    hot = []
    if len(d.get("hotspots") or []) >= 3:
        hy = bottom + 70
        hb = hotspots(hot, d, L, X0, hy)
        hot.insert(0, f'<line x1="74" y1="{bottom + 10}" x2="1462" y2="{bottom + 10}" stroke="{CARD_STROKE}" stroke-width="2"/>')
        bottom = hb + 40
    H = int(bottom + 32 + 20)
    W = 1536
    o.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
             f'font-family="{FONT}" role="img" aria-label="{esc(d["title"])}: {esc(L["overall"])} {esc(L[overall])}">')
    o.append(f'<title>{esc(d["title"])} – {esc(L["overall"])}: {esc(L[overall])}</title>')
    o.append(f'<rect width="{W}" height="{H}" fill="{BG}" rx="0"/>')
    o.append(f'<rect x="32" y="32" width="{W - 64}" height="{H - 64}" rx="26" fill="{BG}" stroke="{CARD_STROKE}" stroke-width="2"/>')
    o.append(f'<text x="74" y="100" font-size="31" font-weight="600" fill="{TEXT}">{esc(trunc(d["title"], 54))}</text>')
    if d.get("scope"):
        o.append(f'<text x="74" y="134" font-size="23" fill="{MUTED}">{esc(trunc(d["scope"], 80))}</text>')
    badge = f'{L["overall"]}: {L[overall]}' + (f' ({L["provisional"]})' if d.get("provisional") else "")
    bw = max(244, 40 + 15.5 * len(badge) + 36)
    bx = W - 72 - bw
    col = ACCENT[overall]
    o.append(f'<rect x="{bx}" y="66" width="{bw}" height="52" rx="12" fill="{CELL[overall]}" stroke="{col}" stroke-width="2"/>')
    o.append(shape(overall, bx + 30, 92, 16, col))
    o.append(f'<text x="{bx + 54}" y="101" font-size="26" fill="{TEXT}">{esc(badge)}</text>')
    o += left + right + hot
    o.append("</svg>")
    return "\n".join(o)


def to_png(svg_path):
    png = str(Path(svg_path).with_suffix(".png"))
    try:
        import cairosvg  # type: ignore
        cairosvg.svg2png(url=str(svg_path), write_to=png)
        return png
    except ImportError:
        pass
    exe = shutil.which("rsvg-convert")
    if exe and subprocess.run([exe, str(svg_path), "-o", png]).returncode == 0:
        return png
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report")
    ap.add_argument("-o", "--out", help="SVG path (default: next to the JSON)")
    ap.add_argument("--prev", help="previous report JSON; adds it to `history` (and updates the JSON)")
    ap.add_argument("--png", action="store_true", help="also write a PNG (needs cairosvg or rsvg-convert)")
    args = ap.parse_args(argv)
    try:
        d = load(args.report)
    except (OSError, ValueError) as e:
        print(f"error: cannot read {args.report}: {e}", file=sys.stderr)
        return 2
    err, warn = validate(d)
    for w in warn:
        print(f"warning: {w}", file=sys.stderr)
    if err:
        for e in err:
            print(f"error: {e}", file=sys.stderr)
        return 2
    if args.prev:
        try:
            d["history"] = merge_history(d, load(args.prev))
            Path(args.report).write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except (OSError, ValueError) as e:
            print(f"warning: ignoring --prev ({e})", file=sys.stderr)
    out = Path(args.out) if args.out else Path(args.report).with_suffix(".svg")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(d), encoding="utf-8")
    print(out)
    if args.png:
        png = to_png(out)
        print(png if png else "note: no PNG written (install cairosvg or rsvg-convert); the SVG is enough",
              file=sys.stderr if not png else sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
