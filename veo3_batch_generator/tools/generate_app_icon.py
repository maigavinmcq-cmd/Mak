from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = ROOT / "assets"
PNG_PATH = ASSET_DIR / "app_icon.png"
ICO_PATH = ASSET_DIR / "app_icon.ico"
SVG_PATH = ASSET_DIR / "app_icon.svg"


def lerp(a: int, b: int, t: float) -> int:
    return round(a + (b - a) * t)


def gradient_background(size: int) -> Image.Image:
    top = (18, 18, 18)
    bottom = (30, 30, 30)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x * 0.35 + y * 0.65) / size
            px[x, y] = (
                lerp(top[0], bottom[0], t),
                lerp(top[1], bottom[1], t),
                lerp(top[2], bottom[2], t),
                255,
            )
    return img


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path(r"C:\Windows\Fonts\seguisb.ttf"),
        Path(r"C:\Windows\Fonts\segoeuib.ttf"),
        Path(r"C:\Windows\Fonts\arialbd.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def draw_icon(size: int = 1024) -> Image.Image:
    img = gradient_background(size)
    radius = round(size * 0.22)
    mask = rounded_mask(size, radius)
    img.putalpha(mask)

    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse(
        (round(size * 0.12), round(size * -0.08), round(size * 1.05), round(size * 0.82)),
        fill=(0, 122, 255, 95),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(round(size * 0.08)))
    img.alpha_composite(glow)

    margin = round(size * 0.085)
    glass = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glass_draw = ImageDraw.Draw(glass)
    glass_draw.rounded_rectangle(
        (margin, margin, size - margin, size - margin),
        radius=round(size * 0.17),
        fill=(255, 255, 255, 18),
        outline=(255, 255, 255, 40),
        width=max(2, round(size * 0.006)),
    )
    img.alpha_composite(glass)

    draw = ImageDraw.Draw(img)

    center = size / 2
    ring_box = (
        round(size * 0.21),
        round(size * 0.18),
        round(size * 0.79),
        round(size * 0.76),
    )
    draw.ellipse(ring_box, outline=(0, 122, 255, 210), width=round(size * 0.035))

    for i in range(7):
        angle = math.radians(-115 + i * 38)
        x = center + math.cos(angle) * size * 0.315
        y = center + math.sin(angle) * size * 0.315
        dot = round(size * 0.022)
        fill = (0, 122, 255, 255) if i in {2, 3, 4} else (118, 184, 255, 210)
        draw.ellipse((x - dot, y - dot, x + dot, y + dot), fill=fill)

    play = [
        (round(size * 0.43), round(size * 0.34)),
        (round(size * 0.43), round(size * 0.64)),
        (round(size * 0.66), round(size * 0.49)),
    ]
    draw.polygon(play, fill=(0, 122, 255, 255))

    label_font = font(round(size * 0.18))
    label = "V3"
    bbox = draw.textbbox((0, 0), label, font=label_font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    draw.text(
        ((size - text_w) / 2, round(size * 0.74) - text_h / 2),
        label,
        font=label_font,
        fill=(245, 248, 255, 255),
    )
    return img


def write_svg() -> None:
    SVG_PATH.write_text(
        """<svg width="1024" height="1024" viewBox="0 0 1024 1024" fill="none" xmlns="http://www.w3.org/2000/svg">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1024" y2="1024" gradientUnits="userSpaceOnUse">
    <stop stop-color="#121212"/>
    <stop offset="1" stop-color="#1E1E1E"/>
  </linearGradient>
  <radialGradient id="glow" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate(525 276) rotate(103) scale(554)">
    <stop stop-color="#007AFF" stop-opacity="0.55"/>
    <stop offset="1" stop-color="#007AFF" stop-opacity="0"/>
  </radialGradient>
</defs>
<rect width="1024" height="1024" rx="224" fill="url(#bg)"/>
<rect width="1024" height="1024" rx="224" fill="url(#glow)"/>
<rect x="88" y="88" width="848" height="848" rx="176" fill="white" fill-opacity="0.07" stroke="white" stroke-opacity="0.16" stroke-width="6"/>
<circle cx="512" cy="482" r="290" stroke="#007AFF" stroke-opacity="0.86" stroke-width="36"/>
<circle cx="382" cy="223" r="23" fill="#76B8FF" fill-opacity="0.82"/>
<circle cx="548" cy="194" r="23" fill="#007AFF"/>
<circle cx="703" cy="261" r="23" fill="#007AFF"/>
<circle cx="786" cy="408" r="23" fill="#007AFF"/>
<circle cx="765" cy="575" r="23" fill="#007AFF"/>
<circle cx="650" cy="700" r="23" fill="#76B8FF" fill-opacity="0.82"/>
<circle cx="484" cy="727" r="23" fill="#76B8FF" fill-opacity="0.82"/>
<path d="M440 350V655L682 503L440 350Z" fill="#007AFF"/>
<text x="512" y="820" text-anchor="middle" font-family="Segoe UI, Arial, sans-serif" font-size="184" font-weight="700" fill="#F5F8FF">V3</text>
</svg>
""",
        encoding="utf-8",
    )


def main() -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    icon = draw_icon()
    icon.save(PNG_PATH)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    icon.save(ICO_PATH, sizes=[(s, s) for s in sizes])
    write_svg()
    print(PNG_PATH)
    print(ICO_PATH)
    print(SVG_PATH)


if __name__ == "__main__":
    main()
