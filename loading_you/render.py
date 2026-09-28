#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""読み込み中のあなた — 30秒版

映像と音をすべて手続き的に生成する。乱数はフレーム番号でシード固定。

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
from PIL import Image, ImageDraw, ImageFont, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("OUT_DIR", HERE)
FONT = os.environ.get("FONT_KANA", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
fS = os.environ.get("FONT_SYM", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
PHOTO_DIR = os.path.join(HERE, "photos")

W, H = 360, 640          # 内部描画
UP = 3                   # 最近傍で3倍 → 1080x1920
FPS = 30
DUR = 30.0
NF = int(round(DUR * FPS))
SR = 48000

# ---------------------------------------------------------------- タイムライン
T_HOOK = 0.2             # 0.0–0.2   フック
T_ANOM = 4.0             # 4.0–7.0   第1の異変
T_CALM2 = 7.0            # 7.0–9.5   偽の復旧（写真タイルが1つ残る）
T_INV = 9.5              # 9.5–14.0  侵入
T_SWAP = 14.0            # 14.0–15.5 入れ替わり（Bが何食わぬ顔で座る）
T_RAMP = 15.5            # 15.5–20.5 暴走
T_COLL = 20.5            # 20.5–21.0 崩落（CRTが点に潰れる）
T_SIL = 21.0             # 21.0–23.5 無音の間
T_RET = 23.5             # 23.5–29.9 帰還
T_LOOP = 29.9            # 29.9–30.0 ループ繋ぎ

BLINKS = [2.0, 8.6, 15.0, 22.6, 26.0]
ERRORS = [(6.2, 2), (10.8, 2), (12.1, 2), (13.3, 1)]       # (時刻, フレーム数)
SPAWNS = [10.2, 11.0, 11.8, 12.6]                           # ウィンドウ増殖
RAMP_QUIET = (18.0, 18.1)                                   # B単独の静かな3フレーム
DUOTONE = [(16.7, 16.9), (19.1, 19.3), (19.7, 19.9)]


def prog_steps():
    """進行バーが1段進む時刻と、その時点の進行率。音のクリックにも使う。"""
    out = []
    for k in range(1, 15):                      # 0.2–3.0s で 98% まで
        out.append((0.2 + k * 0.2, 0.98 * k / 14))
    for k in range(1, 12):                      # 帰還：24.3–26.5s で再充填
        out.append((24.3 + k * 0.2, 0.98 * k / 11))
    out.append((28.6, 0.99))                    # 帰還の最後に1%だけ進む
    return out


PROG = prog_steps()

# ---------------------------------------------------------------- 色
PAPER = (243, 235, 221)
PINK = (247, 200, 216)
BLUE = (191, 216, 232)
LINE = (88, 76, 98)
CYAN = (0, 255, 240)
MAG = (255, 0, 184)
LIME = (182, 255, 0)
RED = (255, 42, 31)
PURP = (122, 44, 255)
ACC = [CYAN, MAG, LIME, RED, PURP]
CHEEK = (240, 150, 176)
EYE = (40, 32, 46)
WBODY = (237, 237, 243)
MUTED = (150, 140, 156)

CX, CY = 180, 357        # マスコット中心
WIN0 = (64, 96)
WIN_POS = [(64, 96), (80, 117), (34, 196), (104, 244), (26, 292)]
WIN_TITLE = [BLUE, PINK, BLUE, PINK, BLUE]
TILE = (256, 445, 64)    # 写真タイル x, y, 辺

# ---------------------------------------------------------------- 疑似文字
KANA = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"
READABLE = "しばらくおまちください"
SYMS = "◆◇▣▤▥▦§¤⌘☐☒◎●○△▽◁▷⊕⊗♯♭∴∵≒"
CELL = 12

_fk = ImageFont.truetype(FONT, 11, index=0)
_fs = ImageFont.truetype(fS, 10)


def render_char(ch, font):
    im = Image.new("L", (CELL, CELL), 0)
    d = ImageDraw.Draw(im)
    d.fontmode = "1"
    bb = d.textbbox((0, 0), ch, font=font)
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    d.text(((CELL - w) // 2 - bb[0], (CELL - h) // 2 - bb[1]), ch, fill=255, font=font)
    return np.asarray(im) > 127


def chimera(rng):
    """実在のかな2文字を左右か上下で切って繋ぐ。"""
    a = render_char(KANA[rng.integers(len(KANA))], _fk)
    b = render_char(KANA[rng.integers(len(KANA))], _fk)
    g = a.copy()
    if rng.random() < 0.5:
        g[:, CELL // 2:] = np.roll(b, rng.integers(-1, 2), axis=0)[:, CELL // 2:]
    else:
        g[CELL // 2:, :] = np.roll(b, rng.integers(-1, 2), axis=1)[CELL // 2:, :]
    return g


def blockglyph(rng):
    """左右対称のドット記号。"""
    half = rng.random((6, 3)) < 0.5
    half[:, 2] |= rng.random(6) < 0.35
    m = np.concatenate([half, half[:, 1::-1]], axis=1)       # 6x5
    m = m.repeat(2, 0).repeat(2, 1)                            # 12x10
    g = np.zeros((CELL, CELL), bool)
    g[:, 1:11] = m
    return g


GLY = []
_rng = np.random.default_rng(20260928)
READ_IDS = []
for ch in READABLE:
    READ_IDS.append(len(GLY))
    GLY.append(render_char(ch, _fk))
READ_IDS = tuple(READ_IDS)
CHIM = []
for _ in range(90):
    CHIM.append(len(GLY))
    GLY.append(chimera(_rng))
BLK = []
for _ in range(40):
    BLK.append(len(GLY))
    GLY.append(blockglyph(_rng))
SYM = []
for ch in SYMS:
    g = render_char(ch, _fs)
    if g.sum() > 4:
        SYM.append(len(GLY))
        GLY.append(g)


def pseudo(rng, n, p_blk=0.2, p_sym=0.15):
    out = []
    for _ in range(n):
        r = rng.random()
        pool = BLK if r < p_blk else SYM if r < p_blk + p_sym else CHIM
        out.append(int(pool[rng.integers(len(pool))]))
    return tuple(out)


TITLE = pseudo(np.random.default_rng(1), 6, 0, 0)
LINE1 = pseudo(np.random.default_rng(2), 12, 0.35, 0.05)
CAPTION = pseudo(np.random.default_rng(3), 13, 0.15, 0.1)
ICON_LABEL = pseudo(np.random.default_rng(4), 4, 0, 0)
CORR_ORDER = [4, 9, 1, 7, 2, 10, 0, 6, 3, 8, 5]      # 最初に化ける5文字目がラストまで残る
CH_FIX = {i: int(CHIM[(i * 7 + 3) % len(CHIM)]) for i in range(11)}


def blit_mask(a, m, x, y, color):
    h, w = m.shape
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(a.shape[1], x + w), min(a.shape[0], y + h)
    if x1 <= x0 or y1 <= y0:
        return
    reg = a[y0:y1, x0:x1]
    reg[m[y0 - y:y1 - y, x0 - x:x1 - x]] = color


def draw_ids(a, ids, x, y, color, adv=CELL, scale=1):
    for k, gid in enumerate(ids):
        g = GLY[gid]
        if scale > 1:
            g = g.repeat(scale, 0).repeat(scale, 1)
        blit_mask(a, g, x + k * adv * scale, y, color)


def blit_rgba(dst, src, x, y):
    h, w = src.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(dst.shape[1], x + w), min(dst.shape[0], y + h)
    if x1 <= x0 or y1 <= y0:
        return
    s = src[y0 - y:y1 - y, x0 - x:x1 - x]
    m = s[..., 3] > 0
    d = dst[y0:y1, x0:x1]
    d[m] = s[m][:, :d.shape[2]] if d.shape[2] == 3 else s[m]


# ---------------------------------------------------------------- 紙背景
def make_paper(seed):
    rng = np.random.default_rng(seed)
    a = np.ones((H, W, 3)) * np.array(PAPER, float)
    low = Image.fromarray(((rng.random((10, 6)) * 255).astype(np.uint8))).resize((W, H), Image.BICUBIC)
    a += (np.asarray(low, float)[..., None] / 255 - 0.5) * 12                  # ムラ
    a += rng.normal(0, 4.5, (H, W, 1))                                        # 粒状
    a += rng.normal(0, 1.5, (H, W, 3))
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    for _ in range(160):                                                      # 繊維
        x, y = rng.integers(0, W), rng.integers(0, H)
        ang = rng.random() * math.pi
        ln = rng.integers(3, 12)
        c = tuple(int(v - rng.integers(8, 20)) for v in PAPER)
        d.line([x, y, x + ln * math.cos(ang), y + ln * math.sin(ang)], fill=c)
    a = np.asarray(im, float)
    yy, xx = np.mgrid[0:H, 0:W]
    r = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)
    a *= (1 - 0.10 * np.clip(r - 0.55, 0, None) ** 1.5)[..., None]              # スキャナー減光
    return np.clip(a, 0, 255).astype(np.uint8)


PAPERS = [make_paper(100 + i) for i in range(6)]
PAPER_SIL = np.clip(PAPERS[0].astype(float) * 0.45 + np.array([251, 249, 245]) * 0.55, 0, 255).astype(np.uint8)


def paper(t):
    return PAPERS[int(t * 12) % 6].copy()


# ---------------------------------------------------------------- マスコット
def _e(d, cx, cy, rx, ry, **kw):
    d.ellipse([round(cx - rx), round(cy - ry), round(cx + rx), round(cy + ry)], **kw)


def draw_mascot(d, kind, cx, cy, s=1.0, sx=1.0, sy=1.0, edx=0, edy=0, bL=False, bR=False):
    col = PINK if kind == "A" else BLUE
    inner = (238, 166, 190) if kind == "A" else (158, 190, 214)
    lw = max(1, round(2 * s))
    r = 66 * s
    rx, ry = r * sx, r * sy
    for sg in (-1, 1):                                                       # 足
        _e(d, cx + sg * 30 * s, cy + ry - 4 * s, 13 * s, 8 * s, fill=col, outline=LINE, width=lw)
    for sg in (-1, 1):                                                       # 耳
        ex, ey = cx + sg * 44 * s * sx, cy - ry * 0.8
        _e(d, ex, ey, 17 * s, 17 * s, fill=col, outline=LINE, width=lw)
        _e(d, ex, ey, 9 * s, 9 * s, fill=inner)
    _e(d, cx, cy, rx, ry, fill=col, outline=LINE, width=lw)                   # 体
    if kind == "A":
        for sg in (-1, 1):
            _e(d, cx + sg * 40 * s, cy + 10 * s, 8 * s, 4 * s, fill=CHEEK)
        for sg, b in ((-1, bL), (1, bR)):
            ex, ey = cx + sg * 23 * s + edx, cy - 4 * s + edy
            if b:
                d.line([round(ex - 5 * s), round(ey), round(ex + 5 * s), round(ey)], fill=EYE, width=lw)
            else:
                _e(d, ex, ey, 4 * s, 5 * s, fill=EYE)
                if s > 0.5:
                    d.rectangle([round(ex - 2 * s), round(ey - 3 * s), round(ex - 1 * s), round(ey - 2 * s)],
                                fill=(255, 255, 255))
        my = cy + 10 * s
        d.arc([round(cx - 8 * s), round(my - 4 * s), round(cx), round(my + 4 * s)], 0, 180, fill=LINE, width=lw)
        d.arc([round(cx), round(my - 4 * s), round(cx + 8 * s), round(my + 4 * s)], 0, 180, fill=LINE, width=lw)
    else:
        for sg in (-1, 1):                                                   # ×のほっぺ
            hx, hy, k = cx + sg * 40 * s, cy + 14 * s, 4 * s
            d.line([round(hx - k), round(hy - k), round(hx + k), round(hy + k)], fill=LINE, width=lw)
            d.line([round(hx - k), round(hy + k), round(hx + k), round(hy - k)], fill=LINE, width=lw)
        ex, ey = cx + edx, cy - 6 * s + edy
        if bL:
            d.line([round(ex - 16 * s), round(ey), round(ex + 16 * s), round(ey)], fill=LINE, width=lw)
        else:
            _e(d, ex, ey, 18 * s, 18 * s, fill=(250, 250, 252), outline=LINE, width=lw)
            _e(d, ex, ey, 10 * s, 10 * s, fill=EYE)
            _e(d, ex, ey, 4.5 * s, 4.5 * s, fill=RED)
            d.rectangle([round(ex - 5 * s), round(ey - 6 * s), round(ex - 3 * s), round(ey - 4 * s)],
                        fill=(255, 255, 255))
        d.line([round(cx - 7 * s), round(cy + 22 * s), round(cx + 7 * s), round(cy + 22 * s)], fill=LINE, width=lw)


@lru_cache(maxsize=256)
def mascot(kind, cx, cy, s=1.0, sx=1.0, sy=1.0, edx=0, edy=0, bL=False, bR=False):
    """マスコットA/Bを全画面RGBAレイヤーに描く。"""
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_mascot(ImageDraw.Draw(im), kind, cx, cy, s, sx, sy, edx, edy, bL, bR)
    a = np.asarray(im)
    a.flags.writeable = False
    return a


def pulse(t):
    tq = math.floor(t * 12) / 12                         # 12fps相当
    ph = math.sin(2 * math.pi * 0.85 * tq)
    sx, sy = round(1 - 0.018 * ph, 3), round(1 + 0.028 * ph, 3)
    return sx, sy, int(round(CY - (sy - 1) * 66))        # 足元は固定


def blinking(t, delay=0.0):
    return any(tb + delay <= t < tb + delay + 0.1 for tb in BLINKS)


def sway(tq, amp):
    return amp * math.sin(2 * math.pi * 1.1 * tq)


# ---------------------------------------------------------------- ウィンドウ / エラー
@lru_cache(maxsize=512)
def make_window(title_col, line1, line2, prog, w=232, h=104):
    im = Image.new("RGBA", (w + 3, h + 3), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle([3, 3, w + 2, h + 2], fill=LINE + (255,))                  # 影
    d.rectangle([0, 0, w - 1, h - 1], fill=WBODY + (255,), outline=LINE + (255,))
    d.rectangle([1, 1, w - 2, 15], fill=title_col + (255,))
    d.line([1, 16, w - 2, 16], fill=LINE + (255,))
    for k in range(3):
        x = w - 34 + k * 10
        d.rectangle([x, 4, x + 7, 11], fill=WBODY + (255,), outline=LINE + (255,))
    bx, by, bw, bh = 10, 74, 168, 12
    d.rectangle([bx, by, bx + bw, by + bh], fill=(250, 250, 252, 255), outline=LINE + (255,))
    fw = int((bw - 3) * prog)
    if fw > 0:
        d.rectangle([bx + 2, by + 2, bx + 1 + fw, by + bh - 2], fill=PINK + (255,))
        for x in range(bx + 2, bx + 2 + fw, 3):
            d.line([x, by + 2, x, by + bh - 2], fill=(236, 168, 194, 255))
    a = np.array(im)
    lc = LINE + (255,)
    draw_ids(a, TITLE, 5, 2, lc)
    draw_ids(a, line1, 10, 28, lc)
    draw_ids(a, line2, 10, 48, lc)
    pct = pseudo(np.random.default_rng(int(prog * 1000)), 2, 0, 0)
    draw_ids(a, pct, bx + bw + 10, by, lc)
    a.flags.writeable = False
    return a


@lru_cache(maxsize=64)
def error_box(seed):
    rng = np.random.default_rng(seed)
    w, h = 124, 46
    im = Image.new("RGBA", (w + 3, h + 3), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle([3, 3, w + 2, h + 2], fill=LINE + (255,))
    d.rectangle([0, 0, w - 1, h - 1], fill=(244, 244, 244, 255), outline=LINE + (255,))
    d.rectangle([1, 1, w - 2, 13], fill=RED + (255,))
    d.rectangle([8, 22, 19, 33], fill=LIME + (255,), outline=LINE + (255,))
    a = np.array(im)
    draw_ids(a, pseudo(rng, 4, 0.3, 0), 4, 1, (255, 255, 255, 255))
    draw_ids(a, pseudo(rng, 7, 0.2, 0.2), 26, 22, LINE + (255,))
    a.flags.writeable = False
    return a


# ---------------------------------------------------------------- 写真断片
def _grid(h, w):
    return np.mgrid[0:h, 0:w].astype(float)


def frag_floor(w, h, rng):
    y, x = _grid(h, w)
    ts = max(6, w // 4)
    var = rng.normal(0, 9, (h // ts + 2, w // ts + 2))[(y // ts).astype(int), (x // ts).astype(int)]
    a = np.array([206, 199, 186], float) + var[..., None]
    g = max(1, ts // 9)
    a[((x % ts) < g) | ((y % ts) < g)] = [118, 110, 104]
    return a * (0.7 + 0.4 * (y / h))[..., None] + rng.normal(0, 6, (h, w, 1))


def frag_window(w, h, rng):
    y, x = _grid(h, w)
    a = np.ones((h, w, 3)) * np.array([72, 66, 78], float)
    x0, x1, y0, y1 = w * 0.2, w * 0.8, h * 0.15, h * 0.75
    inside = (x > x0) & (x < x1) & (y > y0) & (y < y1)
    sky = np.array([170, 205, 235]) + (np.array([235, 240, 245]) - np.array([170, 205, 235])) * ((y - y0) / (y1 - y0))[..., None]
    a[inside] = sky[inside]
    fr = max(1, w // 24)
    a[inside & ((np.abs(x - w / 2) < fr) | (np.abs(y - (y0 + y1) / 2) < fr))] = [60, 55, 64]
    a[(y > y1) & (np.abs(x - w / 2) < w * 0.4)] += 40 * np.exp(-((y - y1) / (h * 0.15)))[(y > y1) & (np.abs(x - w / 2) < w * 0.4)][..., None]
    return a + rng.normal(0, 5, (h, w, 1))


def frag_hand(w, h, rng):
    y, x = _grid(h, w)
    a = np.ones((h, w, 3)) * np.array([46, 38, 44], float)
    cx, cy = w * (0.45 + rng.random() * 0.1), h * 0.62
    palm = ((x - cx) / (w * 0.28)) ** 2 + ((y - cy) / (h * 0.24)) ** 2 < 1
    fing = np.zeros_like(palm)
    for k in range(4):
        fx = cx - w * 0.2 + k * w * 0.13
        fing |= (np.abs(x - fx) < w * 0.05) & (y < cy) & (y > h * (0.12 + 0.06 * abs(k - 1.5)))
    m = palm | fing
    skin = np.array([214, 168, 146]) * (0.75 + 0.35 * (1 - y / h))[..., None]
    a[m] = skin[m]
    return a + rng.normal(0, 7, (h, w, 1))


def frag_curtain(w, h, rng, c1=(146, 64, 88), c2=(232, 168, 182)):
    y, x = _grid(h, w)
    k = 2 * math.pi / (w / 3.2)
    v = 0.5 + 0.5 * np.cos(x * k + 1.4 * np.sin(y / h * 5 + rng.random() * 6))
    return np.array(c1) * (1 - v[..., None]) + np.array(c2) * v[..., None] + rng.normal(0, 6, (h, w, 1))


def frag_water(w, h, rng):
    return frag_curtain(w, h, rng, (54, 128, 120), (150, 208, 196))


def frag_sky(w, h, rng):
    y, x = _grid(h, w)
    a = np.array([118, 168, 222]) + (np.array([226, 234, 244]) - np.array([118, 168, 222])) * (y / h)[..., None]
    low = np.asarray(Image.fromarray((rng.random((6, 4)) * 255).astype(np.uint8)).resize((w, h), Image.BICUBIC), float) / 255
    cl = np.clip((low - 0.55) * 4, 0, 1)[..., None]
    return a * (1 - cl) + 246 * cl


def frag_static(w, h, rng):
    return rng.integers(30, 225, (h, w, 1)).repeat(3, 2).astype(float)


PROC_FRAGS = [frag_floor, frag_window, frag_hand, frag_curtain, frag_sky, frag_static, frag_water]
PHOTO_FILES = sorted(p for p in glob.glob(os.path.join(PHOTO_DIR, "*"))
                     if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp")))
NFRAG = len(PHOTO_FILES) or len(PROC_FRAGS)


@lru_cache(maxsize=128)
def frag(idx, w, h, px, seed=0):
    """写真断片。photos/ があればそれを、無ければ代用品を低解像度化して返す。"""
    idx %= NFRAG
    if PHOTO_FILES:
        im = ImageOps.fit(Image.open(PHOTO_FILES[idx]).convert("RGB"), (w, h), Image.LANCZOS)
    else:
        a = PROC_FRAGS[idx](w, h, np.random.default_rng(500 + idx * 31 + seed))
        im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    im = im.resize((max(1, w // px), max(1, h // px)), Image.BOX).resize((w, h), Image.NEAREST)
    a = np.asarray(im)
    a.flags.writeable = False
    return a


# ---------------------------------------------------------------- エフェクト
def acc_pair(t):
    r = np.random.default_rng(9000 + int(t * 5))           # 0.2秒ごとに1〜2色
    i = r.choice(len(ACC), 2, replace=False)
    return ACC[i[0]], ACC[i[1]]


def rgb_shift(a, dx, dy=0):
    o = a.copy()
    o[..., 0] = np.roll(np.roll(a[..., 0], dx, 1), dy, 0)
    o[..., 2] = np.roll(np.roll(a[..., 2], -dx, 1), -dy, 0)
    return o


def slices(a, rng, n, maxdx, y0=0, y1=H):
    for _ in range(n):
        y = int(rng.integers(y0, y1))
        h = int(rng.integers(2, 18))
        a[y:y + h] = np.roll(a[y:y + h], int(rng.integers(-maxdx, maxdx + 1)), 1)
    return a


def block_glitch(a, rng, n, t):
    c1, c2 = acc_pair(t)
    for _ in range(n):
        bw, bh = int(rng.integers(8, 90)), int(rng.integers(3, 28))
        sx, sy = int(rng.integers(0, W - bw)), int(rng.integers(0, H - bh))
        dx, dy = int(rng.integers(0, W - bw)), int(rng.integers(0, H - bh))
        r = rng.random()
        blk = a[sy:sy + bh, sx:sx + bw].copy()
        if r < 0.25:
            blk[:] = c1 if rng.random() < 0.6 else c2
        elif r < 0.45:
            blk = blk[..., ::-1]
        a[dy:dy + bh, dx:dx + bw] = blk
    return a


def crush(a, q, down=2):
    im = Image.fromarray(a).resize((W // down, H // down), Image.NEAREST)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=int(q))
    im = Image.open(io.BytesIO(buf.getvalue())).convert("RGB").resize((W, H), Image.NEAREST)
    return np.array(im)


def zoom(a, s, cx=CX, cy=CY):
    xs = np.clip(((np.arange(W) - cx) / s + cx).astype(int), 0, W - 1)
    ys = np.clip(((np.arange(H) - cy) / s + cy).astype(int), 0, H - 1)
    return a[ys][:, xs]


def duotone(a, c1, c2):
    l = (a @ np.array([0.299, 0.587, 0.114]) / 255)[..., None]
    return np.clip(np.array(c1) * (1 - l) + np.array(c2) * l, 0, 255).astype(np.uint8)


def comp(base, layer, dx=0):
    """RGBAレイヤーを重ねる。dx があれば R と B を逆方向にずらす。"""
    for c, sh in ((0, dx), (1, 0), (2, -dx)):
        lc = np.roll(layer, sh, 1) if sh else layer
        m = lc[..., 3] > 0
        base[..., c][m] = lc[..., c][m]
    return base


def omen(a, rng):
    """予兆：1pxの振動と滲み。"""
    a = np.roll(a, (int(rng.integers(-1, 2)), int(rng.integers(-1, 2))), (0, 1))
    return rgb_shift(a, 1)


# ---------------------------------------------------------------- 状態
def progress(t, window):
    """window 0 = 最初の窓、1 = 帰還時に前に出る窓。"""
    if t >= T_RET and window == 0:
        return 0.98
    p = 0.0
    for ts, v in PROG:
        if (t >= T_RET) != (ts >= T_RET):
            continue
        if t >= ts:
            p = v
    if T_RET > t >= 3.0:
        p = 0.98
    return round(p, 3)


def line2(t):
    ids = list(READ_IDS)
    if T_RET <= t:
        ids[4] = CH_FIX[4]
        return tuple(ids)
    if t < T_CALM2 + 1.0 or T_SWAP <= t < T_RAMP:
        if 6.2 <= t < 6.2 + 2 / FPS:                            # エラーの瞬間だけ5文字目が化ける
            ids[4] = CH_FIX[4]
        return tuple(ids)
    n = 11 if t >= T_RAMP else min(11, 1 + int((t - 8.0) / 0.35))
    live = t >= T_INV
    slot = int(t * 12)
    for i in CORR_ORDER[:n]:
        if live and np.random.default_rng(slot * 13 + i).random() < 0.5:
            ids[i] = int(CHIM[np.random.default_rng(slot * 17 + i).integers(len(CHIM))])
        else:
            ids[i] = CH_FIX[i]
    return tuple(ids)


def line1(t, k=0):
    if T_INV <= t < T_SWAP or T_RAMP <= t < T_COLL + 0.5:
        r = np.random.default_rng(int(t * 12) * 7 + k)
        p = 1.0 if t >= T_RAMP else 0.3
        return tuple(int(g) if r.random() > p else pseudo(r, 1, 0.35, 0.1)[0] for g in LINE1)
    return LINE1


def windows_layer(t, n, title_override=None):
    lay = np.zeros((H, W, 4), np.uint8)
    for i in range(n):
        tc = WIN_TITLE[i % len(WIN_TITLE)]
        if title_override and i in title_override:
            tc = title_override[i]
        wi = make_window(tc, line1(t, i), line2(t), progress(t, 1 if n == 2 and i == 1 else 0))
        blit_rgba(lay, wi, *WIN_POS[i])
    return lay


def caption(a, t, live=False):
    ids = CAPTION
    if live:
        r = np.random.default_rng(int(t * 12) + 77)
        ids = tuple(int(g) if r.random() > 0.25 else pseudo(r, 1)[0] for g in CAPTION)
    x = (W - len(ids) * CELL) // 2
    draw_ids(a, ids, x, 556, MUTED)


def photo_tile(a, idx, x, y, s, label=False):
    tile = np.dstack([frag(idx, s, s, 8), np.full((s, s), 255, np.uint8)])
    tile[[0, -1], :, :3] = LINE
    tile[:, [0, -1], :3] = LINE
    blit_rgba(a, tile, x, y)
    if label:
        draw_ids(a, ICON_LABEL, x + (s - len(ICON_LABEL) * CELL) // 2, y + s + 4, MUTED)


# ---------------------------------------------------------------- 場面
def calm(t, kind="A", n_win=1, edx=0, tile=None, show_caption=True, bR_delay=0.0):
    a = paper(t)
    if show_caption:
        caption(a, t)
    if tile is not None:
        photo_tile(a, tile, TILE[0], TILE[1], TILE[2], label=True)
    sx, sy, cy = pulse(t)
    m = mascot(kind, CX, cy, 1.0, sx, sy, edx, 0, blinking(t), blinking(t, bR_delay))
    comp(a, m)
    if n_win:
        comp(a, windows_layer(t, n_win))
    return a


def hook(k):
    """0.0–0.2 / 29.9–30.0：反転してマゼンタとシアンに分離、巨大な疑似文字。"""
    rng = np.random.default_rng(4242 + k)
    base = calm(1.0)
    inv = 255 - base.astype(int)
    m = (inv @ np.array([0.299, 0.587, 0.114])) > 90
    out = np.ones((H, W, 3)) * np.array([36, 10, 42], float)
    dx = 6 + (k * 3) % 8
    out[np.roll(m, dx, 1)] += np.array(MAG) * 0.85
    out[np.roll(m, -dx, 1)] += np.array(CYAN) * 0.85
    out = np.clip(out, 0, 255).astype(np.uint8)
    j = lambda: int(rng.integers(-3, 4))
    big = GLY[BLK[5]].repeat(8, 0).repeat(8, 1)
    blit_mask(out, big, 132 + j(), 64 + j(), (255, 255, 255))
    blit_mask(out, GLY[BLK[11]].repeat(7, 0).repeat(7, 1), 138 + j(), 392 + j(), (8, 6, 10))
    blit_mask(out, GLY[CHIM[3]].repeat(3, 0).repeat(3, 1), 28 + j(), 44 + j(), LIME)
    for _ in range(6):
        c = [MAG, PURP, RED][int(rng.integers(3))]
        x, y, s = int(rng.integers(10, 340)), int(rng.integers(20, 600)), int(rng.integers(6, 16))
        out[y:y + s // 2 + 3, x:x + s] = c
    out = block_glitch(out, rng, 5, 0.0)
    return rgb_shift(out, 2)


ANOM_TILES = [0, 1, 4, 5, 3, 2]      # 床、窓、空、砂嵐、カーテン、手。最後の手が残る


def anomaly(t, rng):
    """4.0–7.0：体が揺れ、目だけ1コマ遅れる。写真が侵入、黄緑の帯、エラー。"""
    tq = math.floor(t * 12) / 12
    amp = 5 * min(1.0, (t - T_ANOM) / 0.5)
    sw = sway(tq, amp)
    lag = sway(tq - 1 / 12, amp) - sw
    a = paper(t)
    caption(a, t)
    if t >= 4.5:
        x = int(360 - min(1.0, (t - 4.5) / 0.2) * (360 - TILE[0]))
        photo_tile(a, ANOM_TILES[min(5, int((t - 4.5) / 0.5))], x, TILE[1], TILE[2])
    sx, sy, cy = pulse(t)
    comp(a, mascot("A", int(round(CX + sw)), cy, 1.0, sx, sy, int(round(lag)), 0, False, False))
    comp(a, windows_layer(t, 1))
    if 5.3 <= t < 5.3 + 1 / FPS:
        a[300:303] = LIME
    for te, nf in ERRORS[:1]:
        if te <= t < te + nf / FPS:
            blit_rgba(a, error_box(1), 118, 330)
    if t >= 6.5:
        if int(t * FPS) % 3 == 0:
            slices(a, rng, 3, 8, 290, 430)
        a = omen(a, rng)
    if t >= 6.85:
        a = block_glitch(a, rng, 10, t)
        slices(a, rng, 8, 30)
        a = rgb_shift(a, 4)
    return a


def calm2(t, rng):
    """7.0–9.5：何事もなかったように戻る。ただし写真が1枚、アイコンとして残る。"""
    a = calm(t, tile=ANOM_TILES[-1], bR_delay=1 / 12)
    if t >= 9.2:
        a = omen(a, rng)
    return a


def split_mascot(t, cy, sx, sy, sw, lag, gap, interlace):
    """AとBを重ね、顔の裂け目と右半分の1行おきにBを出す。"""
    A = mascot("A", int(round(CX + sw)), cy, 1.0, sx, sy, int(round(lag)), 0, False, False)
    B = mascot("B", int(round(CX + sw)), cy, 1.0, sx, sy, int(round(lag)), 0, False, False)
    out = B.copy()
    ey = cy - 4
    g2 = gap // 2
    up = np.roll(A, -g2, 0)
    dn = np.roll(A, gap - g2, 0)
    rows = np.arange(H)
    top = rows < ey - g2
    bot = rows >= ey + (gap - g2)
    out[top] = np.where(up[top][..., 3:] > 0, up[top], B[top])
    out[bot] = np.where(dn[bot][..., 3:] > 0, dn[bot], B[bot])
    if gap:
        r = np.random.default_rng(int(t * 12))
        for yy in (ey - g2 - 2, ey - g2 - 1, ey + gap - g2, ey + gap - g2 + 1):
            if 0 <= yy < H:
                out[yy] = np.roll(out[yy], int(r.integers(-4, 5)), 0)
    if interlace:
        xs = np.arange(W) > CX + sw
        odd = (rows % 2 == 1)[:, None] & xs[None, :]
        out[odd] = np.where(B[odd][..., 3:] > 0, B[odd], out[odd])
    return out


def invasion(t, rng):
    """9.5–14.0：顔が裂けてBが覗く。ウィンドウ増殖、背景だけRGBずれ、残像。"""
    u = t - T_INV
    tq = math.floor(t * 12) / 12
    sw = sway(tq, 4)
    lag = sway(tq - 1 / 12, 4) - sw
    sx, sy, cy = pulse(t)
    a = paper(t)
    caption(a, t, live=True)
    photo_tile(a, int(u / 0.25) + 2, TILE[0], TILE[1], TILE[2])
    if t >= 12.8:
        photo_tile(a, int(u / 0.25) + 5, 40, 470, 48)
    dx = 2 + int(u) + int(np.random.default_rng(int(t * 12)).integers(0, 2))
    a = rgb_shift(a, dx)
    gap = int(min(1.0, u / 2.0) * 16) + (int(np.random.default_rng(int(t * 12) + 5).integers(-2, 3)) if u > 0.3 else 0)
    gap = max(0, gap)
    m = split_mascot(t, cy, sx, sy, sw, lag, gap, t >= 11.5)
    for k, col in ((2, CYAN), (4, MAG)):                                      # 残像
        off = int(round(sway(tq - k / 12, 4) - sw))
        gm = np.roll(m[..., 3] > 0, off, 1)
        a[gm] = (a[gm] * 0.6 + np.array(col) * 0.4).astype(np.uint8)
    comp(a, m)
    n = 1 + sum(t >= s for s in SPAWNS)
    over = None
    r = np.random.default_rng(int(t * 12) + 31)
    if r.random() < 0.15:
        over = {int(r.integers(n)): PURP}
    comp(a, windows_layer(t, n, over), dx)
    for te, nf in ERRORS[1:]:
        if te <= t < te + nf / FPS:
            blit_rgba(a, error_box(int(te * 10)), int(rng.integers(40, 200)), int(rng.integers(200, 460)))
    if t >= 13.5:
        k = (t - 13.5) / 0.5
        slices(a, rng, int(4 + k * 16), int(10 + k * 40))
        a = block_glitch(a, rng, int(k * 14), t)
    if t >= 13.9:
        a = crush(rgb_shift(a, 8), 10)
    return a


def swap(t, rng):
    """14.0–15.5：静かな画面にBが何食わぬ顔で座っている。窓は2枚。"""
    return calm(t, kind="B", n_win=2)


def rampage(t, rng):
    """15.5–20.5：拡大縮小3往復、写真、JPEG劣化、帯の反転、赤紫、文字の雨。"""
    if RAMP_QUIET[0] <= t < RAMP_QUIET[1]:
        a = paper(t)
        caption(a, t)
        sx, sy, cy = pulse(t)
        return comp(a, mascot("B", CX, cy, 1.0, sx, sy))
    u = t - T_RAMP
    heat = max(0.0, (t - 19.7) / 0.8) if t >= 19.7 else 0.0
    tq = math.floor(t * 12) / 12
    sw = sway(tq, 6)
    sx, sy, cy = pulse(t)
    c1, c2 = acc_pair(t)
    a = paper(t)
    caption(a, t, live=True)
    m = split_mascot(t, cy, sx, sy, sw, sway(tq - 1 / 12, 6) - sw, 16, True)
    comp(a, m)
    comp(a, windows_layer(t, 5), 3)
    P = 5 / 3
    a = zoom(a, 1 + 0.7 * math.sin(math.pi * u / P) ** 2)
    slot = int(u * 3)                                   # 写真の切替は1/3秒単位（毎秒3回以下）
    rs = np.random.default_rng(700 + slot)
    r = rs.random() if slot else 0.65
    i, j = int(rs.integers(NFRAG)), int(rs.integers(NFRAG))
    if 0.6 <= r < 0.75:
        a = frag(i, W, H, 10).copy()
    elif 0.75 <= r < 0.9:
        ys = int(rs.integers(200, 440))
        a[:ys] = frag(i, W, H, 10)[:ys]
        a[ys:] = frag(j, W, H, 10, 1)[ys:]
    elif r >= 0.9:
        y0 = int(rs.integers(60, 420))
        a[y0:y0 + 200] = frag(i, W, H, 10)[y0:y0 + 200]
    rr = np.random.default_rng(int(t * 12) + 1234)
    for k in range(14):                                  # 文字の雨
        if rr.random() < 0.4:
            continue
        rk = np.random.default_rng(k + 50)
        v, o = 150 + 250 * rk.random(), rk.random() * 700
        y = int((o + v * u) % 720) - 40
        sc = 2 if k % 3 == 0 else 1
        draw_ids(a, pseudo(rr, 2, 0.3, 0.2), 8 + k * 25, y, c1 if k % 2 else c2, adv=CELL, scale=sc)
    if rr.random() < 0.35:
        blit_rgba(a, error_box(int(rr.integers(40))), int(rr.integers(10, 230)), int(rr.integers(60, 560)))
    for _ in range(int(rr.integers(1, 3))):              # 帯の反転（面積は小さく）
        y0, hb = int(rr.integers(0, H - 30)), int(rr.integers(6, 30))
        a[y0:y0 + hb] = 255 - a[y0:y0 + hb]
    a = block_glitch(a, rng, int(6 + heat * 20), t)
    slices(a, rng, int(3 + heat * 14), int(12 + heat * 40))
    for d0, d1 in DUOTONE:
        if d0 <= t < d1:
            a = duotone(a, PURP, RED)
    if rr.random() < 0.7:
        a = crush(a, 12 + int(rr.integers(0, 28)))
    return rgb_shift(a, int(2 + heat * 8))


def collapse(t, rng):
    """20.5–21.0：縦に潰れて横線、横線が点になり、点がマスコットになる。"""
    u = (t - T_COLL) / 0.5
    src = rampage(t, rng) if u < 0.5 else None
    a = PAPER_SIL.copy()
    if u < 0.5:
        hh = max(1, int(320 * (1 - u / 0.5) ** 2))
        idx = np.linspace(0, H - 1, 2 * hh).astype(int)
        band = src[idx].astype(float)
        band = band * (1 - u) + 250 * u
        y0 = CY - hh
        a[max(0, y0):y0 + 2 * hh] = band[max(0, -y0):][:H - max(0, y0)].astype(np.uint8)
    else:
        k = (1 - (u - 0.5) / 0.5) ** 2
        hw = max(2, int(180 * k))
        a[CY - 1:CY + 1, CX - hw:CX + hw] = LINE if hw > 3 else PINK
        if hw <= 3:
            a[CY - 2:CY + 2, CX - 2:CX + 2] = PINK
    return a


def silence(t, rng):
    """21.0–23.5：白に近い紙に小さなマスコットが1体だけ。一度だけ瞬く。"""
    a = PAPER_SIL.copy()
    return comp(a, mascot("A", CX, CY, 0.28, 1.0, 1.0, 0, 0, blinking(t), blinking(t)))


def ret(t, rng):
    """23.5–29.9：冒頭と同じ画面。窓が2枚、目が2px横、5文字目が化けたまま。"""
    a = calm(t, n_win=2, edx=2)
    if t >= 29.4:
        a = omen(a, rng)
        if 29.6 <= t < 29.6 + 1 / FPS:                  # Bが1フレームだけ覗く
            sx, sy, cy = pulse(t)
            B = mascot("B", CX, cy, 1.0, sx, sy)
            rows = slice(cy - 8, cy + 2)
            m = B[rows][..., 3] > 0
            a[rows][m] = B[rows][m][:, :3]
    return a


def frame(fi):
    t = fi / FPS
    rng = np.random.default_rng(10007 + fi)
    if t < T_HOOK:
        return hook(fi)
    if t >= T_LOOP:
        return hook(fi - int(round(T_LOOP * FPS)))
    if t < T_ANOM:
        a = calm(t)
        return omen(a, rng) if t >= 3.5 else a
    for t0, fn in ((T_RET, ret), (T_SIL, silence), (T_COLL, collapse), (T_RAMP, rampage),
                   (T_SWAP, swap), (T_INV, invasion), (T_CALM2, calm2), (T_ANOM, anomaly)):
        if t >= t0:
            return fn(t, rng)


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
