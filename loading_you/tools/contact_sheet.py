#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集めた素材画像をカテゴリごとに一覧にして、切り抜き範囲（crops.json の box）を決めやすくする。

    python3 tools/contact_sheet.py            # assets/_contact/<category>.png を作る
    python3 tools/contact_sheet.py --grid FILE  # 1枚を100pxごとの目盛り付きで assets/_contact/grid.png に書き出す
"""
import argparse
import json
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(os.path.dirname(HERE), "assets")
OUT = os.path.join(ASSETS, "_contact")


def sheets():
    man = json.load(open(os.path.join(ASSETS, "manifest.json"), encoding="utf-8"))
    os.makedirs(OUT, exist_ok=True)
    cats = {}
    for m in man:
        cats.setdefault(m["category"], []).append(m)
    for cat, ms in cats.items():
        tw, th, cols = 260, 300, 5
        rows = (len(ms) + cols - 1) // cols
        sheet = Image.new("RGB", (tw * cols, th * rows), (30, 30, 30))
        d = ImageDraw.Draw(sheet)
        for i, m in enumerate(ms):
            im = Image.open(os.path.join(ASSETS, m["file"])).convert("RGB")
            im.thumbnail((tw - 10, th - 40))
            x, y = (i % cols) * tw + 5, (i // cols) * th + 5
            sheet.paste(im, (x, y))
            d.text((x, y + th - 32), f"{i}: {m['file']}  {im.size[0]}x{im.size[1]}", fill=(230, 230, 230))
            d.text((x, y + th - 18), (m.get("title") or "")[:40], fill=(170, 170, 170))
        sheet.save(os.path.join(OUT, f"{cat}.png"))
        print(os.path.join(OUT, f"{cat}.png"))


def grid(f):
    im = Image.open(os.path.join(ASSETS, f)).convert("RGB")
    d = ImageDraw.Draw(im)
    for x in range(0, im.width, 100):
        d.line([x, 0, x, im.height], fill=(255, 0, 160)); d.text((x + 2, 2), str(x), fill=(255, 0, 160))
    for y in range(0, im.height, 100):
        d.line([0, y, im.width, y], fill=(0, 220, 255)); d.text((2, y + 2), str(y), fill=(0, 220, 255))
    os.makedirs(OUT, exist_ok=True)
    im.save(os.path.join(OUT, "grid.png"))
    print(os.path.join(OUT, "grid.png"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid")
    a = ap.parse_args()
    grid(a.grid) if a.grid else sheets()
