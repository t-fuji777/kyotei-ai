# -*- coding: utf-8 -*-
"""PWAアイコン生成(存在すればスキップ)。6艇カラーのチップデザイン。"""
from pathlib import Path

ROOT = Path(__file__).parent.parent
COLORS = ["#FFFFFF", "#1A1A1A", "#E5384C", "#2D6CDF", "#F2C53D", "#3BAE6A"]


def make(size, path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (size, size), "#0B1B2B")
    d = ImageDraw.Draw(img)
    m = size * 0.16
    w = (size - 2 * m) / 6
    top, bot = size * 0.30, size * 0.74
    for i, c in enumerate(COLORS):
        d.rounded_rectangle([m + i * w + w * 0.12, top, m + (i + 1) * w - w * 0.12, bot],
                            radius=w * 0.18, fill=c)
    d.rectangle([m, size * 0.80, size - m, size * 0.83], fill="#1E4D7A")
    d.rectangle([m, size * 0.86, size - m, size * 0.88], fill="#163B5E")
    img.save(path, "PNG")


if __name__ == "__main__":
    for s in (192, 512):
        p = ROOT / "docs" / f"icon-{s}.png"
        if p.exists():
            print(f"{p.name}: exists")
        else:
            make(s, p)
            print(f"{p.name}: created")
