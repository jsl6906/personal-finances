"""Draw the Ledger mark used in emails: src/ledger/alerts/assets/logo.png.

Run: uv run --with pillow python scripts/make_email_logo.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "src" / "ledger" / "alerts" / "assets" / "logo.png"
SIZE, SCALE = 128, 4
NAVY, LINE, BARS = "#1d2d3d", "#486077", ("#749dc4", "#94bce3", "#d6ebff")


def main() -> None:
    s = SIZE * SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=26 * SCALE, fill=NAVY)
    # Ledger ruling behind the bars.
    for y in (40, 58, 76):
        d.line((24 * SCALE, y * SCALE, 104 * SCALE, y * SCALE), fill=LINE, width=2 * SCALE)
    base, width, gap, left = 96, 18, 9, 28
    for i, (h, color) in enumerate(zip((26, 42, 60), BARS, strict=True)):
        x = left + i * (width + gap)
        d.rectangle((x * SCALE, (base - h) * SCALE, (x + width) * SCALE, base * SCALE), fill=color)
    d.line((22 * SCALE, (base + 1) * SCALE, 106 * SCALE, (base + 1) * SCALE), fill="#eef6ff", width=3 * SCALE)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.resize((SIZE, SIZE), Image.Resampling.LANCZOS).save(OUT, optimize=True)
    print(OUT, OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
