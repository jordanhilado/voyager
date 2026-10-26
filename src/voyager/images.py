"""Image helpers: TypeSafe size rules, Box geometry, crops and downscaling."""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image

MAX_SIDE, MIN_SIDE = 1568, 280
MAX_AREA = 2_458_624


def load(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img.convert("RGB")


def fit(img: Image.Image) -> Image.Image:
    """Longest side <= 1568, both sides >= 280 (pad with white, never stretch), area within limit."""
    img = img.convert("RGB").copy()
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    while img.width * img.height > MAX_AREA:
        img = img.resize((int(img.width * 0.9), int(img.height * 0.9)))
    if min(img.size) < MIN_SIDE:
        canvas = Image.new("RGB", (max(img.width, MIN_SIDE), max(img.height, MIN_SIDE)), "white")
        canvas.paste(img, (0, 0))
        img = canvas
    return img


def to_jpeg(img: Image.Image, quality: int = 82) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, progressive=False)
    return buf.getvalue()


def downscale(data: bytes, max_side: int = 1280) -> bytes:
    img = load(data)
    img.thumbnail((max_side, max_side))
    return to_jpeg(img, 80)


@dataclass
class Box:
    top: float
    bottom: float
    left: float
    right: float

    @classmethod
    def from_answer(cls, b: dict) -> "Box":
        return cls(float(b["top"]), float(b["bottom"]), float(b["left"]), float(b["right"]))

    def center_px(self, w: int, h: int) -> tuple[int, int]:
        return int((self.left + self.right) / 2 * w), int((self.top + self.bottom) / 2 * h)

    def px(self, w: int, h: int) -> tuple[int, int, int, int]:
        return int(self.left * w), int(self.top * h), int(self.right * w), int(self.bottom * h)

    def area(self) -> float:
        return max(0.0, self.right - self.left) * max(0.0, self.bottom - self.top)


@dataclass
class Crop:
    image: Image.Image
    x0: int
    y0: int
    scale: float  # crop pixels -> original pixels multiplier (1.0 when not padded/resized)

    def to_original(self, x: int, y: int) -> tuple[int, int]:
        return self.x0 + int(x * self.scale), self.y0 + int(y * self.scale)


def padded_crop(img: Image.Image, box: Box, factor: float = 3.5) -> Crop:
    """Crop around a box at full resolution, about ``factor`` times the box, at least 280x280."""
    w, h = img.size
    l, t, r, b = box.px(w, h)
    bw, bh = max(r - l, 8), max(b - t, 8)
    cw, ch = max(int(bw * factor), MIN_SIDE), max(int(bh * factor), MIN_SIDE)
    cw, ch = min(cw, MAX_SIDE, w), min(ch, MAX_SIDE, h)
    cx, cy = (l + r) // 2, (t + b) // 2
    x0 = min(max(cx - cw // 2, 0), w - cw)
    y0 = min(max(cy - ch // 2, 0), h - ch)
    crop = img.crop((x0, y0, x0 + cw, y0 + ch))
    scale = 1.0
    if min(crop.size) < MIN_SIDE:
        canvas = Image.new("RGB", (max(crop.width, MIN_SIDE), max(crop.height, MIN_SIDE)), "white")
        canvas.paste(crop, (0, 0))
        crop = canvas
    return Crop(crop, x0, y0, scale)
