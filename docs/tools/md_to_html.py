"""Render docs/design.md to a print-ready HTML file (then html_to_pdf.js prints it)."""

from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

import markdown

CSS = """
@page { size: A4; margin: 14mm 14mm 16mm 14mm; }
@page landscape { size: A4 landscape; margin: 10mm; }
body { font-family: "Segoe UI", Helvetica, Arial, sans-serif; font-size: 9.8pt; line-height: 1.36; color: #1a1a1a; }
h1 { font-size: 18pt; margin: 0 0 4pt; }
h2 { font-size: 13.5pt; margin: 14pt 0 6pt; border-bottom: 1.5px solid #1F5FBF; padding-bottom: 2pt; break-after: avoid; }
h3 { font-size: 11pt; margin: 10pt 0 4pt; break-after: avoid; }
p { margin: 3pt 0 5pt; }
ul, ol { margin: 2pt 0 5pt; padding-left: 18pt; }
li { margin: 1pt 0; }
table { border-collapse: collapse; width: 100%; margin: 5pt 0 9pt; font-size: 8.7pt; break-inside: auto; }
th, td { border: 1px solid #c8c8c8; padding: 2.5pt 5pt; vertical-align: top; text-align: left; }
th { background: #eef3fb; }
tr { break-inside: avoid; }
code { font-family: Consolas, "Courier New", monospace; font-size: 8.5pt; background: #f3f3f3; padding: 0 2pt; }
hr { border: 0; border-top: 1px solid #ddd; margin: 8pt 0; }
.diagram { text-align: center; }
.diagram h2, .diagram h3 { text-align: left; margin-top: 0; }
.diagram.landscape { page: landscape; break-before: page; break-after: page; }
.diagram.landscape img { max-width: 100%; max-height: 165mm; }
.diagram.portrait { break-inside: avoid; margin: 6pt 0; }
.diagram.portrait img { max-width: 100%; max-height: 150mm; }
.diagram .caption { font-size: 9pt; color: #555; margin-top: 4pt; }
"""


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as f:
        f.read(16)
        return struct.unpack(">II", f.read(8))


def main(src: Path, dst: Path) -> None:
    md = src.read_text(encoding="utf-8")
    html = markdown.markdown(md, extensions=["tables", "fenced_code", "sane_lists"])

    # Each diagram gets its own page (landscape for wide images, portrait for tall ones).
    # Up to two headings (h2 then h3) immediately before the image move onto that page so they are never orphaned,
    # and the italic caption after the image moves with it.
    pattern = re.compile(
        r"((?:<h[23]>(?:(?!</h[23]>).)*</h[23]>\s*){0,2})<p>(<img [^>]*src=\"([^\"]+)\"[^>]*>)</p>\s*(?:<p><em>((?:(?!</em>).)*)</em></p>)?",
        re.S,
    )

    def diagram(m: re.Match) -> str:
        heading, img, src_path, caption = m.group(1) or "", m.group(2), m.group(3), m.group(4) or ""
        w, h = png_size(src.parent / src_path)
        orient = "landscape" if w / h > 1.25 else "portrait"
        caption_html = f'<div class="caption">{caption}</div>' if caption else ""
        return f'<div class="diagram {orient}">{heading}{img}{caption_html}</div>'

    html = pattern.sub(diagram, html)

    dst.write_text(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{src.stem}</title><style>{CSS}</style></head>"
        f"<body>{html}</body></html>",
        encoding="utf-8",
    )
    print(f"wrote {dst}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
