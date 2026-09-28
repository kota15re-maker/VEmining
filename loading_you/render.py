#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""読み込み中のあなた — 30秒版

素材（紙、疑似文字、ウィンドウ、マスコット、写真断片、エフェクト、フック）は15秒版の
生成コードをそのまま引き継ぎ、タイムラインだけを30秒用に組み直している。
映像と音はすべて手続き的に生成する。乱数はシード固定。

    python3 render.py preview 0 1.0 5.3 10.5 14.5 17.0 22.0 26.0   # コンタクトシート
    python3 render.py video out.mp4                                 # 音声付きで書き出し

環境変数で上書きできるもの:
    FONT_KANA / FONT_SYM  フォントパス
    FFMPEG                ffmpeg 実行ファイル
    OUT_DIR               preview と中間 WAV の出力先（既定はこのファイルの場所）
写真素材は photos/ に画像を置くと FRAGS がそれを読む（無ければ手続き生成の代用品）。
"""
import glob
import io
import math
import os
import subprocess
import sys
import wave
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("OUT_DIR", HERE)
FONT = os.environ.get("FONT_KANA", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
FONT_SYM = os.environ.get("FONT_SYM", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
PHOTO_DIR = os.path.join(HERE, "photos")

W, H, UP, FPS = 360, 640, 3, 30
DUR = 30.0
NF = int(round(DUR * FPS))
SR = 48000
R0 = np.random.default_rng(7)

# ---------------------------------------------------------------- タイムライン
T_HOOK = 0.1             # 0.0–0.1   フック（3フレーム）
T_ANOM = 4.0             # 4.0–7.0   第1の異変
T_CALM2 = 7.0            # 7.0–9.5   偽の復旧（写真が1枚アイコンとして残る）
T_INV = 9.5              # 9.5–14.0  侵入
T_SWAP = 14.0            # 14.0–15.5 入れ替わり（Bが何食わぬ顔で座る）
T_RAMP = 15.5            # 15.5–20.5 暴走
T_COLL = 20.5            # 20.5–21.0 崩落（CRTが点に潰れる）
T_SIL = 21.0             # 21.0–23.5 無音の間
T_RET = 23.5             # 23.5–29.9 帰還
T_LOOP = 29.9            # 29.9–30.0 ループ繋ぎ（冒頭のフック3フレーム）

BLINKS = [1.9, 8.6, 15.0, 22.6, 26.0]
ERRORS = [(6.2, 2), (10.8, 2), (12.1, 2), (13.3, 1)]       # (時刻, フレーム数)
SPAWNS = [10.2, 11.0, 11.8, 12.6]                           # ウィンドウ増殖
RAMP_QUIET = (18.0, 18.1)                                   # B単独の静かな3フレーム
DUOTONE = [(16.7, 16.9), (19.1, 19.3), (19.7, 19.9)]


def hx(h): return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


PAPER, PINK, BLUE = hx('#F3EBDD'), hx('#F7C8D8'), hx('#BFD8E8')
CY, MG, YG, RD, PU = hx('#00FFF0'), hx('#FF00B8'), hx('#B6FF00'), hx('#FF2A1F'), hx('#7A2CFF')
INK = (74, 58, 70)
FRAME = (88, 76, 98)
FOOTC = (150, 126, 140)
CRASH = [CY, MG, YG, RD, PU]


# ---------------------------------------------------------------- 紙
def make_paper(light=0.0):
    base = np.ones((H, W, 3)) * np.array(PAPER, float)
    lf = Image.fromarray((R0.random((16, 9)) * 255).astype(np.uint8)).resize((W, H), Image.BICUBIC)
    lf = (np.asarray(lf) / 255.0 - 0.5)[..., None] * 12
    fib = Image.new('L', (W, H), 0); d = ImageDraw.Draw(fib)
    for _ in range(140):
        x, y = R0.random() * W, R0.random() * H; a = R0.random() * 6.28; l = R0.random() * 18 + 4
        d.line([x, y, x + math.cos(a) * l, y + math.sin(a) * l], fill=int(R0.random() * 40 + 10))
    fib = np.asarray(fib)[..., None] / 255 * -20
    yy = np.linspace(-1, 1, H)[:, None]; xx = np.linspace(-1, 1, W)[None, :]
    vig = -(xx ** 2 * 0.7 + yy ** 2 * 0.35)[..., None] * 14
    return base + lf + fib + vig


PAPER_BASE = make_paper()
PAPERS = [np.clip(PAPER_BASE + R0.normal(0, 4.5, (H, W, 1)), 0, 255).astype(np.uint8) for _ in range(6)]
PAPER_WHITE = np.clip(PAPER_BASE * 0.35 + 250 * 0.65 + R0.normal(0, 2.0, (H, W, 1)), 0, 255).astype(np.uint8)

# ---------------------------------------------------------------- 疑似文字
G = 14
fK = ImageFont.truetype(FONT, 12, index=0)
fS = ImageFont.truetype(FONT_SYM, 12)


def cmask(c, font):
    im = Image.new('L', (G, G), 0); d = ImageDraw.Draw(im)
    d.text((G // 2, G // 2), c, font=font, fill=255, anchor='mm')
    return np.asarray(im) > 90


KANA = list('あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん')
KM = [cmask(c, fK) for c in KANA]
GL = []


def chimera(r):
    a = KM[r.integers(len(KM))]; b = KM[r.integers(len(KM))]
    m = r.integers(4); out = a.copy()
    if m == 0: out[:, G // 2:] = b[:, G // 2:]
    elif m == 1: out[G // 2:] = b[G // 2:]
    elif m == 2: out[:, G // 2:] = b[:, :G // 2][:, ::-1]
    else: out = a[:, ::-1].copy(); out[:G // 2] |= b[:G // 2]
    return out


def blockglyph(r):
    g = r.random((7, 4)) < 0.42
    g = np.concatenate([g, g[:, ::-1][:, 1:]], axis=1)
    g[r.integers(7)] = True
    return np.kron(g, np.ones((2, 2), bool))


for _ in range(130): GL.append(chimera(R0))
for _ in range(50): GL.append(blockglyph(R0))
for c in '▓░◆◇□■※¤§¶∴∵⌘◐◑△▽': GL.append(cmask(c, fS))
NPSEUDO = len(GL)
READ = 'しばらくおまちください'
READ_ID = []
for c in READ:
    GL.append(cmask(c, fK)); READ_ID.append(len(GL) - 1)

GL_IMG = {}


def gimg(gid, scale):
    k = (gid, scale)
    if k not in GL_IMG:
        m = Image.fromarray((GL[gid] * 255).astype(np.uint8))
        if scale != 1: m = m.resize((G * scale, G * scale), Image.NEAREST)
        GL_IMG[k] = m
    return GL_IMG[k]


def stamp(img, x, y, gid, color, scale=1):
    m = gimg(gid, scale)
    img.paste(color + (255,) if img.mode == 'RGBA' else color, (int(x), int(y), int(x) + m.size[0], int(y) + m.size[1]), m)


def pseudo_ids(r, n): return [int(r.integers(NPSEUDO)) for _ in range(n)]


TITLE = pseudo_ids(R0, 6)
LINE1 = pseudo_ids(R0, 9)
FOOT = pseudo_ids(R0, 11)
LINE2_END = READ_ID.copy(); LINE2_END[4] = int(R0.integers(130))   # ラストまで残る1文字
ICON_LABEL = pseudo_ids(np.random.default_rng(4), 4)
CORR_ORDER = [4, 9, 1, 7, 2, 10, 0, 6, 3, 8, 5]                    # 最初に化けるのも5文字目
CH_FIX = {i: (LINE2_END[4] if i == 4 else (i * 11 + 3) % 130) for i in range(11)}

# ---------------------------------------------------------------- ウィンドウ
WW, WH = 232, 104


@lru_cache(maxsize=1024)
def make_window(progress, l1, l2, style=0):
    im = Image.new('RGBA', (WW + 3, WH + 3), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    body = (238, 236, 244) if style != 2 else (250, 236, 236)
    title = {0: BLUE, 1: PINK, 2: RD, 3: PU}[style]
    d.rectangle([3, 3, WW + 2, WH + 2], fill=FRAME + (255,))
    d.rectangle([0, 0, WW - 1, WH - 1], fill=body + (255,), outline=FRAME + (255,))
    d.rectangle([1, 1, WW - 2, 19], fill=title + (255,))
    d.line([1, 20, WW - 2, 20], fill=FRAME + (255,))
    for i in range(3):
        bx = WW - 16 - i * 12
        d.rectangle([bx, 5, bx + 8, 13], fill=body + (255,), outline=FRAME + (255,))
    tcol = (255, 255, 255) if style >= 2 else FRAME
    for i, g in enumerate(TITLE): stamp(im, 6 + i * 12, 3, g, tcol)
    for i, (g, c) in enumerate(l1): stamp(im, 10 + i * 13, 28, g, c)
    for i, (g, c) in enumerate(l2): stamp(im, 10 + i * 13, 48, g, c)
    x0, x1 = 10, WW - 52
    d.rectangle([x0, 74, x1, 88], outline=FRAME + (255,), fill=(255, 255, 255, 255))
    fx = x0 + 2 + int((x1 - x0 - 4) * progress)
    if fx > x0 + 2:
        d.rectangle([x0 + 2, 76, fx, 86], fill=PINK + (255,))
        for sx in range(x0 + 4, fx, 6): d.line([sx, 76, sx, 86], fill=(232, 160, 186, 255))
    pid = [(int(progress * 97) * 7 + k * 13) % NPSEUDO for k in range(2)]
    for k, g in enumerate(pid): stamp(im, WW - 44 + k * 13, 74, g, FRAME)
    return im


def with_alpha(im, a):
    if a >= 1: return im
    arr = np.asarray(im).copy(); arr[..., 3] = (arr[..., 3] * a).astype(np.uint8)
    return Image.fromarray(arr)


# ---------------------------------------------------------------- マスコット
MC = 200


@lru_cache(maxsize=1024)
def mascot(scale=1.0, edx=0, edy=0, variant=0, blink=False):
    im = Image.new('RGBA', (MC, MC), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    cx, cy = 100, 108; r = 56 * scale
    body = PINK if variant == 0 else BLUE
    line = (122, 78, 98) if variant == 0 else (38, 56, 104)
    for sx in (-1, 1):
        ex, ey, er = cx + sx * r * 0.62, cy - r * 0.8, r * 0.27
        d.ellipse([ex - er, ey - er, ex + er, ey + er], fill=body, outline=line, width=2)
        d.ellipse([ex - er * 0.5, ey - er * 0.5, ex + er * 0.5, ey + er * 0.5], fill=(252, 226, 234) if variant == 0 else (220, 236, 246))
    for sx in (-1, 1):
        fx = cx + sx * r * 0.45
        d.ellipse([fx - r * 0.22, cy + r * 0.72, fx + r * 0.22, cy + r * 1.0], fill=body, outline=line, width=2)
    d.ellipse([cx - r, cy - r * 0.9, cx + r, cy + r * 0.9], fill=body, outline=line, width=2)
    ey = cy - r * 0.05 + edy
    if variant == 0:
        for sx in (-1, 1):
            d.ellipse([cx + sx * r * 0.55 - 9, cy + r * 0.2 - 4, cx + sx * r * 0.55 + 9, cy + r * 0.2 + 4], fill=(255, 150, 176))
            ex = cx + sx * r * 0.33 + edx
            if blink:
                d.line([ex - 5, ey, ex + 5, ey], fill=INK, width=2)
            else:
                d.ellipse([ex - 5, ey - 7, ex + 5, ey + 7], fill=INK)
                d.ellipse([ex - 3, ey - 5, ex, ey - 2], fill=(255, 255, 255))
        my = cy + r * 0.28
        d.arc([cx - 7, my - 4, cx, my + 3], 0, 180, fill=INK, width=2)
        d.arc([cx, my - 4, cx + 7, my + 3], 0, 180, fill=INK, width=2)
    else:
        er = r * 0.3
        if blink:
            d.line([cx - er + edx, ey, cx + er + edx, ey], fill=line, width=2)
        else:
            d.ellipse([cx - er + edx, ey - er, cx + er + edx, ey + er], fill=(250, 250, 252), outline=line, width=2)
            d.ellipse([cx - 9 + edx * 1.5, ey - 9, cx + 9 + edx * 1.5, ey + 9], fill=(20, 22, 40))
            d.ellipse([cx - 2 + edx * 1.5, ey - 2, cx + 2 + edx * 1.5, ey + 2], fill=RD)
        d.line([cx - 10, cy + r * 0.45, cx + 10, cy + r * 0.45], fill=line, width=2)
        for sx in (-1, 1):
            px, py = cx + sx * r * 0.62, cy + r * 0.18
            d.line([px - 4, py - 4, px + 4, py + 4], fill=line, width=2)
            d.line([px - 4, py + 4, px + 4, py - 4], fill=line, width=2)
    return im


MX0, MY0 = 180 - 100, 360 - 108


# ---------------------------------------------------------------- 写真断片
def blur(a, r): return np.asarray(Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(r))).astype(float)


def vnoise(h, w, cell, r):
    sm = r.random((max(2, h // cell), max(2, w // cell)))
    return np.asarray(Image.fromarray((sm * 255).astype(np.uint8)).resize((w, h), Image.BICUBIC)) / 255.


def frag_tiles(r, s=96):
    yy, xx = np.mgrid[0:s, 0:s].astype(float)
    z = 1 / (0.18 + yy / s * 0.82)
    X = (xx - s / 2) / s * z * 3; Y = z * 2.2
    gr = ((X % 1) < 0.07) | ((Y % 1) < 0.07)
    a = np.where(gr[..., None], np.array([110, 112, 108.]), np.array([205, 210, 204.]))
    a *= (0.55 + 0.6 * (yy / s))[..., None]
    a += r.normal(0, 9, (s, s, 1))
    return blur(a, 0.8)


def frag_window(r, s=96):
    a = np.ones((s, s, 3)) * np.array([34, 38, 50.]) + r.normal(0, 8, (s, s, 1))
    a[14:66, 26:72] = [236, 238, 246]
    a[38:41, 26:72] = [60, 60, 70]; a[14:66, 47:50] = [60, 60, 70]
    a = blur(a, 2.6) * 1.05 + r.normal(0, 7, (s, s, 1))
    return a


def frag_hand(r, s=96):
    yy = np.mgrid[0:s, 0:s][0]
    a = np.ones((s, s, 3)) * np.array([150, 140, 132.]) * (0.7 + 0.4 * yy / s)[..., None]
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    skin = (206, 166, 146)
    d.ellipse([28, 52, 76, 110], fill=skin)
    for (x0, y0, x1, y1) in [(34, 60, 24, 18), (46, 56, 42, 8), (58, 56, 60, 10), (68, 62, 76, 22), (32, 80, 10, 66)]:
        d.line([x0, y0, x1, y1], fill=skin, width=11)
        d.ellipse([x1 - 5, y1 - 5, x1 + 5, y1 + 5], fill=skin)
    a = blur(np.asarray(im).astype(float), 1.8)
    a *= (1.1 - 0.5 * (np.mgrid[0:s, 0:s][1] / s))[..., None]
    return a + r.normal(0, 8, (s, s, 1))


def frag_curtain(r, s=96):
    yy, xx = np.mgrid[0:s, 0:s].astype(float)
    v = 0.5 + 0.5 * np.sin(xx * 0.33 + 4 * vnoise(s, s, 12, r))
    a = np.array([110, 34, 56.]) * (1 - v[..., None]) + np.array([226, 150, 166.]) * v[..., None]
    return blur(a, 1.2) + r.normal(0, 7, (s, s, 1))


def frag_sky(r, s=96):
    n = vnoise(s, s, 24, r) * 0.6 + vnoise(s, s, 8, r) * 0.3 + vnoise(s, s, 3, r) * 0.1
    a = np.array([60, 90, 150.]) + n[..., None] * np.array([190, 160, 100.])
    return a + r.normal(0, 6, (s, s, 1))


def frag_static(r, s=96):
    a = r.random((s, s, 1)) * 255 * np.ones((1, 1, 3))
    y = r.integers(s); a[y:y + 14] *= 0.4
    return a


PHOTO_FILES = sorted(p for p in glob.glob(os.path.join(PHOTO_DIR, "*"))
                     if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp")))
if PHOTO_FILES:
    # 本物の写真：正方形（タイル用）と 9:16（全面用）に切り出す
    _ims = [Image.open(p).convert('RGB') for p in PHOTO_FILES]
    FRAGS = [np.asarray(ImageOps.fit(im, (96, 96), Image.LANCZOS)).copy() for im in _ims]
    FULLS = [np.asarray(ImageOps.fit(im, (54, 96), Image.LANCZOS)).copy() for im in _ims]
else:
    FRAGS = [np.clip(f(np.random.default_rng(i + 30)), 0, 255).astype(np.uint8)
             for i, f in enumerate([frag_tiles, frag_window, frag_hand, frag_curtain, frag_sky, frag_static])]
    FULLS = [f[:, 21:75] for f in FRAGS]   # 54x96、9:16
NFRAG = len(FRAGS)


def dither2(a, c0, c1):
    b = np.asarray(Image.fromarray(a).convert('L').convert('1')).astype(bool)[..., None]
    return np.where(b, np.array(c1, np.uint8), np.array(c0, np.uint8))


@lru_cache(maxsize=64)
def frag_full(k, mode):
    f = np.asarray(Image.fromarray(FULLS[k % NFRAG]).resize((45, 80), Image.BILINEAR))
    if mode == 1: f = dither2(f, (10, 8, 20), PAPER)
    elif mode == 2: f = dither2(f, PU, YG)
    elif mode == 3: f = f[..., [2, 0, 1]]
    return np.asarray(Image.fromarray(np.ascontiguousarray(f)).resize((W, H), Image.NEAREST)).copy()


@lru_cache(maxsize=32)
def tile(k):
    return np.asarray(Image.fromarray(FRAGS[k % NFRAG]).resize((8, 8), Image.BILINEAR).resize((64, 64), Image.NEAREST))


IX, IY = 256, 446
ANOM_TILES = [0, 1, 4, 5, 3, 2]      # 床、窓、空、砂嵐、カーテン、手。最後の手がアイコンとして残る


def put_tile(a, k, x=IX, y=IY):
    t = tile(k)
    x0, x1 = max(0, x), min(W, x + 64)
    if x1 > x0:
        a[y:y + 64, x0:x1] = t[:, x0 - x:x1 - x]


# ---------------------------------------------------------------- エフェクト
def rgb_shift(a, dx, dy=0):
    o = a.copy(); o[..., 0] = np.roll(np.roll(a[..., 0], dx, 1), dy, 0); o[..., 2] = np.roll(np.roll(a[..., 2], -dx, 1), -dy, 0); return o


def bg_only(a, fn, mmask, mx, my):
    o = fn(a)
    y0, x0 = max(0, my), max(0, mx)
    y1, x1 = min(H, my + MC), min(W, mx + MC)
    mm = mmask[y0 - my:y1 - my, x0 - mx:x1 - mx, None] > 0
    o[y0:y1, x0:x1] = np.where(mm, a[y0:y1, x0:x1], o[y0:y1, x0:x1])
    return o


def slices(A, B, r, n, maxdx, y0, y1, x0=0, x1=W):
    """裂け目。ずれた隙間から下の層（B）が見える。"""
    o = A.copy()
    for _ in range(n):
        y = int(r.integers(y0, y1)); h = int(r.integers(2, 10)); dx = int(r.integers(4, maxdx + 1)) * (1 if r.random() < .5 else -1)
        seg = np.roll(A[y:y + h, x0:x1], dx, 1)
        if dx > 0: seg[:, :dx] = B[y:y + h, x0:x0 + dx]
        else: seg[:, dx:] = B[y:y + h, x1 + dx:x1]
        o[y:y + h, x0:x1] = seg
        if r.random() < 0.6:
            yy = y + h; hh = int(r.integers(2, 6))
            o[yy:yy + hh, x0:x1] = B[yy:yy + hh, x0:x1]
    return o


def block_glitch(a, r, n, bs=8, colors=True):
    o = a.copy()
    for _ in range(n):
        x, y = int(r.integers(0, W // bs)) * bs, int(r.integers(0, H // bs)) * bs
        w, h = bs * int(r.integers(1, 5)), bs * int(r.integers(1, 3))
        q = r.random()
        if q < 0.55:
            sx, sy = int(r.integers(0, W // bs)) * bs, int(r.integers(0, H // bs)) * bs
            src = a[sy:sy + h, sx:sx + w]; hh, ww = src.shape[:2]
            o[y:y + hh, x:x + ww] = src[:H - y, :W - x][:hh, :ww]
        elif q < 0.8 and colors:
            o[y:y + h, x:x + w] = CRASH[int(r.integers(5))]
        else:
            o[y:y + h, x:x + w] = o[y:y + h, x:x + w][..., [1, 2, 0]]
    return o


def jpeg(a, q):
    b = io.BytesIO(); Image.fromarray(a).save(b, 'JPEG', quality=int(q)); return np.asarray(Image.open(b).convert("RGB")).copy()


def crush(a, q):
    sm = np.ascontiguousarray(a[::2, ::2])
    sm = jpeg(jpeg(sm, q), max(2, q - 3))
    return sm.repeat(2, 0).repeat(2, 1)


def zoom(a, z, cx, cy):
    w, h = int(W / z), int(H / z)
    x0 = int(np.clip(cx - w / 2, 0, W - w)); y0 = int(np.clip(cy - h / 2, 0, H - h))
    return np.asarray(Image.fromarray(np.ascontiguousarray(a[y0:y0 + h, x0:x0 + w])).resize((W, H), Image.NEAREST)).copy()


def duotone(a, c0, c1):
    l = a.mean(2, keepdims=True) / 255
    return (np.array(c0) * (1 - l) + np.array(c1) * l).astype(np.uint8)


def error_box(img, x, y, r):
    d = ImageDraw.Draw(img)
    w, h = 120, 52
    d.rectangle([x + 3, y + 3, x + w + 3, y + h + 3], fill=(20, 10, 20))
    d.rectangle([x, y, x + w, y + h], fill=(250, 246, 246), outline=(20, 10, 20))
    d.rectangle([x + 1, y + 1, x + w - 1, y + 14], fill=RD)
    for i, g in enumerate(pseudo_ids(r, 4)): stamp(img, x + 4 + i * 11, y + 1, g, (255, 255, 255))
    d.rectangle([x + 8, y + 22, x + 22, y + 36], fill=YG, outline=(20, 10, 20))
    for i, g in enumerate(pseudo_ids(r, 6)): stamp(img, x + 28 + i * 13, y + 22, g, (20, 10, 20))


def with_error(a, x, y, r):
    im = Image.fromarray(a); error_box(im, x, y, r); return np.asarray(im).copy()


def omen(a, r):
    """予兆：1pxの振動と滲み。"""
    return rgb_shift(np.roll(a, int(r.integers(-1, 2)), 1), 1)


# ---------------------------------------------------------------- 状態
def _ease(n, seed):
    rr = np.random.default_rng(seed); inc = rr.random(n + 1) ** 2
    return np.cumsum(inc) / inc.sum()


EASE1 = _ease(int(1.7 * 12), 99)
EASE2 = _ease(int(2.2 * 12), 98)


def progress_main(t):
    """1枚目の窓。0.5–2.2秒で12%→98%、あとは止まる。"""
    if t < 0.5: return 0.12
    if t < 2.2: return 0.12 + 0.86 * EASE1[min(int((t - 0.5) * 12), len(EASE1) - 1)]
    return 0.98


def progress_front(t):
    """帰還で手前に出る窓。0から溜まり直し、98%で止まり、最後に1%だけ進む。"""
    if t < T_RET: return 0.98
    if t < 24.3: return 0.0
    if t < 26.5: return 0.98 * EASE2[min(int((t - 24.3) * 12), len(EASE2) - 1)]
    return 0.99 if t >= 28.6 else 0.98


def click_times():
    out, prev = [], None
    for i in range(int(DUR * 120)):
        t = i / 120
        p = progress_main(t) if t < T_ANOM else progress_front(t) if T_RET <= t < T_LOOP else None
        if p is not None and prev is not None and p != prev:
            out.append((t, p))
        prev = p
    return out


PROG = click_times()


def sway(t):
    tq = int(t * 12) / 12
    if T_ANOM <= t < T_CALM2: amp = 3
    elif T_INV <= t < T_SWAP: amp = 3
    elif T_RAMP <= t < T_COLL: amp = 5
    else: return 0
    return int(round(amp * math.sin(2 * math.pi * 0.8 * (tq - T_ANOM))))


def look(t):
    if 4.3 <= t < 4.9: return -3
    if 4.9 <= t < 5.5: return 3
    return 0


def blinking(t, delay=0.0):
    return any(tb + delay <= t < tb + delay + 0.15 for tb in BLINKS)


def line2_ids(t, step):
    if t >= T_RET:
        return LINE2_END
    ids = READ_ID.copy()
    if t < 8.0 or T_SWAP <= t < T_RAMP:
        if ERRORS[0][0] <= t < ERRORS[0][0] + 2 / FPS:     # エラーの瞬間だけ5文字目が化ける
            ids[4] = CH_FIX[4]
        return ids
    if t < T_INV:                                          # 偽の復旧：1文字ずつ化けて、そのまま
        for i in CORR_ORDER[:1 + int((t - 8.0) / 0.35)]:
            ids[i] = CH_FIX[i]
        return ids
    n = 11 if t >= T_RAMP else min(11, 5 + int((t - T_INV) / 0.3))
    for i in CORR_ORDER[:n]:
        ids[i] = int(np.random.default_rng(i * 7 + step).integers(NPSEUDO))
    return ids


# ウィンドウ増殖： (時刻, x, y, style, 漂う速度)
SPAWN_WINS = [(SPAWNS[0], 70, 150, 1, (22, 30)), (SPAWNS[1], 30, 230, 0, (-14, 26)),
              (SPAWNS[2], 100, 300, 3, (18, -20)), (SPAWNS[3], 20, 420, 1, (26, -30))]


def windows(t, prog):
    wins = [(64, 96, 0, 1.0, prog)]
    if T_SWAP <= t < T_RAMP or t >= T_RET:
        wins.append((82, 118, 0, 1.0, progress_front(t)))
    elif T_INV <= t < T_SIL:
        te = min(t, T_SWAP)
        trail = t < T_SWAP
        for (ts, x, y, sty, v) in SPAWN_WINS:
            if t >= ts:
                for k in ((3, 2, 1, 0) if trail else (0,)):        # 残像の尾を引いて漂う
                    tt = max(ts, te - k * 0.05)
                    wins.append((x + v[0] * (tt - ts), y + v[1] * (tt - ts), sty, [1, .45, .28, .15][k], prog))
    return wins


def state(t):
    st = {'t': t}
    step = int(t * 12)
    st['pidx'] = step % 6
    st['scale'] = round(1.22 + 0.02 * math.sin(2 * math.pi * step / 12 * 0.9), 4)
    sw, swp = sway(t), sway(t - 1 / 12)
    st['sway'] = sw
    st['edx'] = look(t - 1 / 12) + (swp - sw) + (2 if t >= T_RET else 0)   # 目だけ1コマ遅れる
    st['edy'] = 0
    st['blink'] = blinking(t)
    st['progress'] = progress_main(t)
    rr = np.random.default_rng(step * 3 + 5)
    l1 = LINE1.copy()
    live = T_INV <= t < T_SWAP or T_RAMP <= t < T_SIL
    for _ in range(3 if live else 1):
        if rr.random() < 0.25 or live: l1[int(rr.integers(9))] = int(rr.integers(NPSEUDO))
    l1c = [(g, FRAME) for g in l1]
    if 4.4 <= t < T_SWAP or T_RAMP <= t < T_SIL: l1c[3] = (l1[3], RD)
    st['l1'] = l1c
    st['l2'] = [(g, INK) for g in line2_ids(t, step)]
    st['wins'] = windows(t, st['progress'])
    return st


def render(st, variant=0, show_windows=True, icon=None):
    img = Image.fromarray(PAPERS[st['pidx']]).convert('RGBA')

    def draw_win(w):
        x, y, sty, a, pg = w
        im = make_window(pg, tuple(st['l1']), tuple(st['l2']), sty)
        img.alpha_composite(with_alpha(im, a), (int(x), int(y)))
    if show_windows: draw_win(st['wins'][0])
    m = mascot(st['scale'], st['edx'], st['edy'], variant, st['blink'])
    mx, my = MX0 + st['sway'], MY0
    img.alpha_composite(m, (mx, my))
    if show_windows:
        for w in st['wins'][1:]: draw_win(w)
    for i, g in enumerate(FOOT): stamp(img, 180 - len(FOOT) * 13 // 2 + i * 13, 556, g, FOOTC)
    if int(st.get('t', 0) * 2) % 2 == 0:
        ImageDraw.Draw(img).rectangle([180 + len(FOOT) * 13 // 2 + 2, 558, 180 + len(FOOT) * 13 // 2 + 8, 569], fill=FOOTC)
    a = np.asarray(img.convert('RGB')).copy()
    if icon is not None:                                   # 偽の復旧で残る写真アイコン
        put_tile(a, icon)
        im = Image.fromarray(a)
        for i, g in enumerate(ICON_LABEL): stamp(im, IX + 32 - len(ICON_LABEL) * 13 // 2 + i * 13, IY + 68, g, FOOTC)
        a = np.asarray(im).copy()
    return a, mx, my, np.asarray(m.split()[3])


# ---------------------------------------------------------------- 場面
def hook(k):
    """反転してマゼンタとシアンに分離。巨大な疑似文字。"""
    a, *_ = render(state(1.0))
    inv = 255 - a.astype(float); l = inv.mean(2)
    r = np.random.default_rng(1000 + k)
    d = [9, 13, 6][k]
    L1, L2 = np.roll(l, d, 1), np.roll(l, -d, 1)
    o = np.stack([np.maximum(L1, 40), np.minimum(L1, L2) * 0.35, np.maximum(L2, 40)], 2)
    o = np.clip(o * 1.25, 0, 255).astype(np.uint8)
    o = block_glitch(o, r, 24, colors=True)
    im = Image.fromarray(o)
    for j in range(3):
        stamp(im, r.integers(-20, 250), r.integers(40, 520), int(r.integers(NPSEUDO)), [(255, 255, 255), (10, 6, 16), YG][j], scale=int(r.integers(5, 8)))
    return np.asarray(im)


def calm1(t, st, r, rs):
    a, *_ = render(st)
    if 3 <= int(t * FPS) < 7: a = rgb_shift(a, [5, 3, 2, 1][int(t * FPS) - 3])   # フックの余韻
    if t >= 3.6: a = omen(a, r)
    return a


def anomaly(t, st, r, rs):
    """4.0–7.0：体が揺れ、目だけ1コマ遅れる。写真が侵入、黄緑の帯、エラー。"""
    a, mx, my, mm = render(st)
    if t >= 4.5:
        put_tile(a, ANOM_TILES[min(5, int((t - 4.5) / 0.5))], int(360 - min(1.0, (t - 4.5) / 0.2) * (360 - IX)))
    if 5.3 <= t < 5.3 + 1 / FPS:
        y = 300; a[y:y + 6] = YG; a[y + 6:y + 9] = np.roll(a[y + 6:y + 9], 12, 1)
    if ERRORS[0][0] <= t < ERRORS[0][0] + ERRORS[0][1] / FPS:
        a = with_error(a, 150, 205, np.random.default_rng(62))
    if t >= 6.4:
        a = np.roll(a, int(r.integers(-1, 2)), 1)
        a[400:430] = np.roll(a[400:430], int(r.integers(-4, 5)), 1)
    if t >= 6.85:                                          # 破損が噴き出して、切れる
        B, *_ = render(st, variant=1)
        a = slices(a, B, r, 10, 30, my + 40, my + 175, mx, mx + MC)
        a = rgb_shift(block_glitch(a, r, 14), 4)
    return a


def calm2(t, st, r, rs):
    """7.0–9.5：何事もなかったように戻る。ただし写真が1枚アイコンとして残る。右目の瞬きが1コマ遅い。"""
    st = dict(st)
    a, mx, my, mm = render(st, icon=ANOM_TILES[-1])
    if blinking(t) != blinking(t, 1 / 12):                 # 片目だけ開いている/閉じているコマ
        m2 = mascot(st['scale'], st['edx'], st['edy'], 0, blinking(t, 1 / 12))
        half = np.asarray(m2)[:, 100:]
        sub = a[my:my + MC, mx + 100:mx + MC]
        msk = half[..., 3:] > 0
        sub[:] = np.where(msk, half[..., :3], sub)
    if t >= 9.2: a = omen(a, r)
    return a


def invasion(t, st, r, rs):
    """9.5–14.0：顔が裂けてBが覗く。右半分が1行おきにB。ウィンドウが残像を引いて増殖。"""
    p = (t - T_INV) / (T_SWAP - T_INV)
    A, mx, my, mm = render(st)
    B, *_ = render(st, variant=1)
    fy0 = my + 108 - 8 if p < 0.4 else my + 20
    A = slices(A, B, rs, int(4 + 9 * p), int(10 + 24 * p), fy0, my + 175, mx, mx + MC)
    if t > 11.5 and int(t * 6) % 3 == 0:
        y0, y1, x0, x1 = my + 30, my + 170, 180, mx + MC
        A[y0:y1:2, x0:x1] = B[y0:y1:2, x0:x1]
    a = A
    put_tile(a, ANOM_TILES[int(t * 4) % 6] if int(t * 12) % 5 else 2)
    if int(t * 10) % 4 == 0:
        dx = int(r.integers(3, 8))
        a = bg_only(a, lambda z: rgb_shift(z, dx), mm, mx, my)
        for _ in range(2):
            y = int(r.integers(0, H)); a[y:y + 1] = CY if r.random() < .5 else MG
    for te, nf in ERRORS[1:]:
        if te <= t < te + nf / FPS:
            er = np.random.default_rng(int(te * 10))
            a = with_error(a, int(er.integers(20, 220)), int(er.integers(180, 460)), er)
    if t >= 13.5:
        k = (t - 13.5) / 0.5
        a = slices(a, B, r, int(4 + 14 * k), int(10 + 40 * k), 0, H)
        a = block_glitch(a, r, int(4 + 16 * k))
    if t >= 13.9:
        a = crush(rgb_shift(a, 8), 10)
    return a


def swap(t, st, r, rs):
    """14.0–15.5：整った静かな画面に、Bが何食わぬ顔で座っている。窓は2枚、文字は全部読める。"""
    a, *_ = render(st, variant=1)
    return a


def rampage(t, st, r, rs):
    """15.5–20.5：拡大縮小3往復。写真、JPEG劣化、帯の反転、赤紫のデュオトーン、文字の雨。"""
    if RAMP_QUIET[0] <= t < RAMP_QUIET[1]:                 # B単独の静かな3フレーム
        a, *_ = render(st, variant=1, show_windows=False)
        return a
    u = t - T_RAMP
    heat = max(0.0, (t - 19.7) / 0.8)
    A, mx, my, mm = render(st)
    B, *_ = render(st, variant=1)
    A = slices(A, B, rs, 10, 36, my + 20, my + 175, mx, mx + MC)
    A[my + 30:my + 170:2, 180:mx + MC] = B[my + 30:my + 170:2, 180:mx + MC]
    a = zoom(A, 1 + 0.7 * math.sin(math.pi * u / (5 / 3)) ** 2, 180, 360)
    slot = int(u * 3)                                      # 写真の切替は1/3秒単位（毎秒3回以下）
    ps = np.random.default_rng(700 + slot)
    q = ps.random() if slot else 0.65
    i, j, mode = int(ps.integers(NFRAG)), int(ps.integers(NFRAG)), int(ps.integers(4))
    if 0.6 <= q < 0.75:
        a = frag_full(i, mode).copy()
    elif 0.75 <= q < 0.9:
        ys = int(ps.integers(200, 440))
        a[:ys] = frag_full(i, mode)[:ys]; a[ys:] = frag_full(j, (mode + 1) % 4)[ys:]
    elif q >= 0.9:
        y0 = int(ps.integers(60, 420))
        a[y0:y0 + 200] = frag_full(i, mode)[y0:y0 + 200]
    im = Image.fromarray(a)
    c = [CRASH[int(rs.integers(5))], CRASH[int(rs.integers(5))]]  # 1コマに事故色は2色まで
    for k in range(14):                                    # 文字の雨
        if rs.random() < 0.4: continue
        rk = np.random.default_rng(k + 50)
        y = int((rk.random() * 700 + (150 + 250 * rk.random()) * u) % 720) - 40
        sc = 2 if k % 3 == 0 else 1
        for n_ in range(2):
            stamp(im, 8 + k * 25, y + n_ * G * sc, int(rs.integers(NPSEUDO)), c[k % 2], scale=sc)
    if rs.random() < 0.35:
        error_box(im, int(rs.integers(10, 230)), int(rs.integers(60, 540)), rs)
    a = np.asarray(im).copy()
    for _ in range(int(rs.integers(1, 3))):                # 帯の反転（面積は小さく）
        y0, hb = int(rs.integers(0, H - 30)), int(rs.integers(6, 30))
        a[y0:y0 + hb] = 255 - a[y0:y0 + hb]
    a = block_glitch(a, r, int(6 + heat * 20))
    for _ in range(int(3 + heat * 14)):
        y, h = int(r.integers(0, H)), int(r.integers(2, 18))
        a[y:y + h] = np.roll(a[y:y + h], int(r.integers(-12 - int(heat * 40), 13 + int(heat * 40))), 1)
    for d0, d1 in DUOTONE:
        if d0 <= t < d1: a = duotone(a, PU, RD)
    if rs.random() < 0.7:
        a = crush(a, 8 + int(rs.integers(0, 20)))
    return rgb_shift(a, int(2 + heat * 8))


def collapse(t, st, r, rs):
    """20.5–21.0：縦に潰れて横線、横線が縮んで点、点が無音の間のマスコットになる。"""
    u = (t - T_COLL) / 0.5
    a = PAPER_WHITE.copy()
    cy = 360
    if u < 0.5:
        src = rampage(t, st, r, rs)
        hh = max(1, int(320 * (1 - u / 0.5) ** 2))
        idx = np.linspace(0, H - 1, 2 * hh).astype(int)
        band = src[idx].astype(float) * (1 - u) + 250 * u
        y0 = cy - hh
        a[max(0, y0):y0 + 2 * hh] = band[max(0, -y0):][:H - max(0, y0)].astype(np.uint8)
    else:
        hw = max(2, int(180 * (1 - (u - 0.5) / 0.5) ** 2))
        if hw > 3: a[cy - 1:cy + 1, 180 - hw:180 + hw] = FRAME
        else: a[cy - 2:cy + 2, 178:182] = PINK
    return a


@lru_cache(maxsize=2)
def _silence(blink):
    img = Image.fromarray(PAPER_WHITE).convert('RGBA')
    img.alpha_composite(mascot(0.42, 0, 0, 0, blink), (80, 252))
    return np.asarray(img.convert('RGB')).copy()


def silence(t, st, r, rs):
    """21.0–23.5：白に近い紙に小さなマスコット1体。完全静止、一度だけ瞬く。"""
    return _silence(blinking(t)).copy()


def ret(t, st, r, rs):
    """23.5–29.9：冒頭と同じ画面。ただし窓が2枚、目が2px横、5文字目が化けたまま。"""
    a, mx, my, mm = render(st)
    if t >= 29.4:
        a = omen(a, r)
        if 29.6 <= t < 29.6 + 1 / FPS:                    # Bが1フレームだけ覗く
            B, *_ = render(st, variant=1)
            a[my + 96:my + 112, mx:mx + MC] = B[my + 96:my + 112, mx:mx + MC]
    return a


SCENES = [(T_RET, ret), (T_SIL, silence), (T_COLL, collapse), (T_RAMP, rampage), (T_SWAP, swap),
          (T_INV, invasion), (T_CALM2, calm2), (T_ANOM, anomaly), (0.0, calm1)]


def frame(f):
    t = f / FPS
    if f < 3: return hook(f)
    if f >= NF - 3: return hook(f - (NF - 3))
    st = state(t)
    r = np.random.default_rng(f * 17 + 3)
    rs = np.random.default_rng(int(t * 12) * 11 + 1)      # 12fps相当で保持する乱数
    for t0, fn in SCENES:
        if t >= t0:
            return fn(t, st, r, rs)


# ---------------------------------------------------------------- 仕上げ
_yy, _xx = np.mgrid[0:H * UP, 0:W * UP]
_r = np.sqrt(((_xx - W * UP / 2) / (W * UP / 2)) ** 2 + ((_yy - H * UP / 2) / (H * UP / 2)) ** 2)
VIG = (1 - 0.2 * np.clip(_r - 0.35, 0, None) ** 1.6).astype(np.float32)[..., None]
SCAN = np.ones((H * UP, 1, 1), np.float32)
SCAN[2::3] = 0.84
VIG = VIG * SCAN
del _yy, _xx, _r


def finish(a):
    """3倍拡大、走査線、色収差1px、周辺減光。"""
    big = a.repeat(UP, 0).repeat(UP, 1)
    f = big.astype(np.float32)
    f[..., 0] = np.roll(f[..., 0], 1, 1)
    f[..., 2] = np.roll(f[..., 2], -1, 1)
    f *= VIG
    return np.clip(f, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- 音
def audio(path):
    n = int(SR * DUR)
    tt = np.arange(n) / SR
    L = np.zeros(n)
    R = np.zeros(n)
    rng = np.random.default_rng(7)

    def add(t0, sig, pan=0.0, g=1.0):
        i = int(t0 * SR)
        if i >= n:
            return
        s = sig[:n - i] * g
        L[i:i + len(s)] += s * (1 - max(0, pan))
        R[i:i + len(s)] += s * (1 + min(0, pan))

    def env(d, a=0.002, r=None):
        k = np.arange(int(d * SR)) / SR
        e = np.minimum(1, k / a)
        return e * (np.exp(-k / r) if r else 1)

    def sq(f, d):
        k = np.arange(int(d * SR)) / SR
        return np.sign(np.sin(2 * np.pi * f * k))

    def sweep(f0, f1, d, wave="sq"):
        k = np.arange(int(d * SR)) / SR
        f = f0 * (f1 / f0) ** (k / d)
        ph = 2 * np.pi * np.cumsum(f) / SR
        return np.sign(np.sin(ph)) if wave == "sq" else np.sin(ph)

    def crushed_noise(d, hold=24, bits=3):
        m = int(d * SR)
        x = rng.uniform(-1, 1, m // hold + 1).repeat(hold)[:m]
        q = 2 ** bits
        return np.round(x * q) / q

    # 床：ハムとヒス。入れ替わりの間だけ少し低く濁る
    f = np.where((tt >= T_SWAP) & (tt < T_RAMP), 55.0, 60.0)
    ph = 2 * np.pi * np.cumsum(f) / SR
    hum = 0.05 * np.sin(ph) + 0.03 * np.sin(2 * ph) + 0.008 * rng.normal(0, 1, n)
    trem = np.where((tt >= T_SWAP) & (tt < T_RAMP), 0.75 + 0.25 * np.sin(2 * np.pi * 3 * tt), 1.0)
    hum_env = np.where(tt < T_RAMP, 1.0, 0.0) + np.where(tt >= T_RET, 1.0, 0.0)
    hum_env = hum_env * np.where((tt >= T_INV) & (tt < T_SWAP), 1.3, 1.0)
    fade = np.clip((tt - T_RET) / 0.03, 0, 1)
    hum_env = np.where(tt >= T_RET, fade, hum_env)
    omen_ = ((tt >= 3.5) & (tt < 4.0)) | ((tt >= 9.2) & (tt < 9.5)) | ((tt >= 29.4) & (tt < T_LOOP))
    h = hum * trem * hum_env
    h = np.where(omen_, np.clip(h * 4, -0.09, 0.09), h)            # 予兆で歪む
    L += h
    R += h

    # フック / ループ繋ぎ：ビットクラッシュの轟音
    roar = (crushed_noise(T_HOOK) * 0.6 + 0.35 * sq(45, T_HOOK)) * env(T_HOOK, 0.001)
    add(0.0, roar)
    add(T_LOOP, roar[:int((DUR - T_LOOP) * SR)])

    # 進行クリック
    click = (rng.uniform(-1, 1, int(0.004 * SR)) * 0.5 + np.sin(2 * np.pi * 2200 * np.arange(int(0.004 * SR)) / SR)) * env(0.004, 0.0003, 0.0012)
    for ts, _ in PROG:
        add(ts, click, pan=-0.2, g=0.3)

    # 第1の異変
    blip = np.sin(2 * np.pi * 1200 * np.arange(int(0.06 * SR)) / SR) * env(0.06, 0.001, 0.02)
    add(4.5, blip, pan=0.4, g=0.3)
    for k in range(1, 5):
        add(4.5 + k * 0.5, click, pan=0.5, g=0.18)                  # 写真が切り替わる小さな音
    add(5.3, sweep(3000, 7000, 0.035, "sin") * env(0.035, 0.001), g=0.25)
    err = np.concatenate([sq(880, 0.09) * env(0.09, 0.002), sq(660, 0.12) * env(0.12, 0.002, 0.08)])
    for te, _ in ERRORS:
        add(te, err, g=0.16)
    add(6.85, crushed_noise(0.15, 12, 2) * env(0.15, 0.001), g=0.4)

    # 侵入：増殖の降下音、裂けるクラックル
    for k, ts in enumerate(SPAWNS):
        add(ts, sweep(1400 * 0.85 ** k, 300 * 0.85 ** k, 0.35) * env(0.35, 0.002, 0.2), pan=-0.3 + 0.2 * k, g=0.16)
    for ts in np.arange(T_INV, T_SWAP, 1 / 400):
        d = (ts - T_INV) / (T_SWAP - T_INV)
        if rng.random() < 0.04 + 0.4 * d * d:
            imp = rng.uniform(-1, 1, 96) * np.exp(-np.arange(96) / 20)
            add(ts, imp, pan=rng.uniform(-0.6, 0.6), g=rng.uniform(0.1, 0.4))
    add(13.9, crushed_noise(0.1, 8, 2) * env(0.1, 0.001), g=0.5)

    # 入れ替わり：低いポップと、Bの瞬きに1回だけクリック
    add(T_SWAP, np.sin(2 * np.pi * 80 * np.arange(int(0.08 * SR)) / SR) * env(0.08, 0.001, 0.03), g=0.35)
    add(15.0, click, g=0.2)

    # 暴走：0.1秒グリッドのスタッターと、拡大縮小に同期した48Hz
    for ci in range(int((T_COLL - T_RAMP) * 10)):
        ts = T_RAMP + ci * 0.1
        if RAMP_QUIET[0] - 0.05 <= ts < RAMP_QUIET[1]:
            continue
        heat = max(0.0, (ts - 19.7) / 0.8)
        r = rng.random()
        if r < 0.4:
            s = crushed_noise(0.1, int(rng.integers(6, 40)), int(rng.integers(2, 4)))
        elif r < 0.8:
            s = sq(float(rng.uniform(100, 1600)), 0.1) * 0.6
        else:
            continue
        add(ts, s * env(0.1, 0.002), pan=float(rng.uniform(-0.5, 0.5)), g=0.28 + 0.15 * heat)
    m = (tt >= T_RAMP) & (tt < T_COLL) & ~((tt >= RAMP_QUIET[0]) & (tt < RAMP_QUIET[1]))
    bass = 0.35 * np.sin(2 * np.pi * 48 * tt) * np.sin(np.pi * (tt - T_RAMP) / (5 / 3)) ** 2
    L += np.where(m, bass, 0)
    R += np.where(m, bass, 0)

    # 崩落：テープが止まるように落ちる
    d = T_SIL - T_COLL
    stop = (sweep(300, 20, d) * 0.6 + crushed_noise(d, 16, 3) * 0.4) * np.linspace(1, 0, int(d * SR)) ** 1.5
    add(T_COLL, stop, g=0.4)

    # 無音の間：完全にゼロ
    L[(tt >= T_SIL) & (tt < T_RET)] = 0
    R[(tt >= T_SIL) & (tt < T_RET)] = 0
    L[(tt >= RAMP_QUIET[0]) & (tt < RAMP_QUIET[1])] = 0
    R[(tt >= RAMP_QUIET[0]) & (tt < RAMP_QUIET[1])] = 0

    peak = max(np.abs(L).max(), np.abs(R).max())
    st = np.stack([L, R], 1) * (0.89 / peak)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((st * 32767).astype("<i2").tobytes())


# ---------------------------------------------------------------- 書き出し
def video(out):
    wav = os.path.join(OUT_DIR, "_audio.wav")
    audio(wav)
    cmd = [FFMPEG, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W * UP}x{H * UP}", "-r", str(FPS), "-i", "-",
           "-i", wav,
           "-c:v", "libx264", "-profile:v", "high", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", out]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for fi in range(NF):
        p.stdin.write(finish(frame(fi)).tobytes())
        if fi % 60 == 0:
            print(f"{fi}/{NF}", file=sys.stderr, flush=True)
    p.stdin.close()
    p.wait()
    os.remove(wav)
    print(out)


def preview(times):
    tiles = [Image.fromarray(finish(frame(int(round(float(s) * FPS))))).resize((270, 480), Image.BILINEAR)
             for s in times]
    cols = min(6, len(tiles))
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (270 * cols, 480 * rows), (0, 0, 0))
    for i, im in enumerate(tiles):
        sheet.paste(im, ((i % cols) * 270, (i // cols) * 480))
    path = os.path.join(OUT_DIR, "preview.png")
    sheet.save(path)
    print(path)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "preview":
        preview(sys.argv[2:] or ["0", "1.0", "5.3", "10.5", "14.5", "17.0", "22.0", "26.0"])
    elif len(sys.argv) >= 3 and sys.argv[1] == "video":
        video(sys.argv[2])
    else:
        print(__doc__)
