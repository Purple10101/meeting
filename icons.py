"""Tray and application icons, drawn at runtime so there are no image assets to ship."""
from pathlib import Path

from PIL import Image, ImageDraw

# Background colour per recorder state; the white microphone glyph reads on light and dark taskbars.
STATE_COLORS = {
    "idle": (71, 85, 105),
    "recording": (220, 38, 38),
    "paused": (217, 119, 6),
}


def make_icon(state: str = "idle", size: int = 64) -> Image.Image:
    # Draw at 4x and downsample for smooth anti-aliased edges.
    s = size * 4
    u = s / 64  # one unit on a 64x64 design grid
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    stroke = round(4 * u)

    d.ellipse((0, 0, s - 1, s - 1), fill=STATE_COLORS[state])
    d.rounded_rectangle((24 * u, 11 * u, 40 * u, 37 * u), radius=8 * u, fill=white)  # capsule
    d.arc((17 * u, 19 * u, 47 * u, 45 * u), start=0, end=180, fill=white, width=stroke)  # cradle
    d.line((32 * u, 45 * u, 32 * u, 52 * u), fill=white, width=stroke)  # stem
    d.line((24 * u, 52 * u, 40 * u, 52 * u), fill=white, width=stroke)  # base
    return img.resize((size, size), Image.LANCZOS)


def write_ico(path: Path) -> None:
    """Multi-resolution .ico for the packaged .exe."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    make_icon("idle", 256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
