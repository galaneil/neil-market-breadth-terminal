"""
flags.py — small inline SVG country flags for the page headers.

Deliberately NOT emoji. Windows ships no flag glyphs, so a regional-indicator
pair like the US flag emoji falls back to rendering the plain letters "US" —
verified in-browser before writing this (a canvas test showed zero coloured
pixels for the emoji, i.e. letter fallback, not a flag). Inline SVG renders
identically on every OS and inside a Notion embed, and costs nothing extra to
serve since it is part of the HTML.

Drawn at a 24x16 viewBox and scaled down by CSS, so they stay crisp at any size.
Simplified on purpose: at 18px wide the US canton's 50 stars would be mud, so it
is a solid blue canton over the correct 13 stripes, which is what actually reads
at this size.
"""

# 13 stripes, 7 of them red, canton covering the top 7 stripes.
_STRIPE = 16 / 13
_US_STRIPES = "".join(
    f'<rect y="{i * _STRIPE:.3f}" width="24" height="{_STRIPE:.3f}"/>'
    for i in range(0, 13, 2)
)

FLAGS = {
    "US": (
        '<svg class="flag" viewBox="0 0 24 16" role="img" aria-label="United States">'
        '<rect width="24" height="16" fill="#fff"/>'
        f'<g fill="#B22234">{_US_STRIPES}</g>'
        f'<rect width="10" height="{7 * _STRIPE:.3f}" fill="#3C3B6E"/>'
        "</svg>"
    ),
    "IN": (
        '<svg class="flag" viewBox="0 0 24 16" role="img" aria-label="India">'
        '<rect width="24" height="5.333" fill="#FF9933"/>'
        '<rect y="5.333" width="24" height="5.334" fill="#fff"/>'
        '<rect y="10.667" width="24" height="5.333" fill="#138808"/>'
        '<circle cx="12" cy="8" r="2.1" fill="none" stroke="#000080" stroke-width="0.45"/>'
        '<circle cx="12" cy="8" r="0.45" fill="#000080"/>'
        "</svg>"
    ),
}


def flag(country):
    return FLAGS.get(country, "")


if __name__ == "__main__":
    page = "<body style='background:#fff;font:14px sans-serif'>"
    for code, svg in FLAGS.items():
        page += f"<p>{svg.replace('class=', 'width=32 height=21 class=')} {code}</p>"
    print(page + "</body>")
