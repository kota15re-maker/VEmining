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
    CRF                   H.264 の画質（既定20。小さいほど高画質・大容量）
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
# 破壊は5回。回を追うごとに強く、長く、間の「静」は短く、少しずつ間違っていく。
SEGS = [
    (0.0, 0.1, 'hook'),       # フック（3フレーム）
    (0.1, 2.4, 'calm1'),      # 静 I：本物
    (2.4, 3.6, 'tear'),       # 破壊1「裂け」：顔が裂けてBが出る。静へのフラッシュバックを挟む
    (3.6, 4.4, 'calm2'),      # 静 II：偽の復旧。写真が1枚アイコンとして残る
    (4.4, 6.8, 'swarm'),      # 破壊2「増殖」：ウィンドウとエラーが画面を埋め尽くす
    (6.8, 7.6, 'swap'),       # 静 III：Bが何食わぬ顔で座る
    (7.6, 11.6, 'rampage'),   # 破壊3「暴走」：拡大縮小、写真、JPEG、文字の雨
    (11.6, 12.0, 'freeze'),   # 静 IV：止まるが、残骸がすべて残ったまま
    (12.0, 15.0, 'melt'),     # 破壊4「溶解」：画面が溶け落ち、ピクセルソート、縦ロール
    (15.0, 16.0, 'half'),     # 静 V：左半分がA、右半分がB
    (16.0, 21.5, 'climax'),   # 破壊5「全壊」：これまでの破壊が加速しながら全部戻ってくる
    (21.5, 22.0, 'collapse'), # 崩落：CRTが点に潰れる
    (22.0, 23.8, 'silence'),  # 無音の間
    (23.8, 29.9, 'ret'),      # 帰還：冒頭と同じ画面。ただし窓2枚、目のずれ、化けた1文字
    (29.9, 30.0, 'hook'),     # ループ繋ぎ
]
DESTROY = {'tear', 'swarm', 'rampage', 'melt', 'climax'}
T_RET = 23.8
T_LOOP = 29.9
BLINKS = [1.9, 7.2, 15.5, 23.0, 26.2]
ERRORS = [(2.9, 2), (3.25, 2)]                              # 破壊1のエラー
CASCADE0 = 5.2                                              # エラーの連鎖が始まる時刻
QUIET = [(9.6, 9.7), (18.5, 18.6)]                          # B単独の静かな3フレーム
AFTERSHOCK = (27.3, 27.4)                                   # 帰還中の余震（3フレーム）
DUOTONE = [(8.8, 9.0), (10.6, 10.8), (17.3, 17.5), (19.6, 19.8), (21.1, 21.3)]
MODES = ['tear', 'swarm', 'rampage', 'melt']
DUO_DARK, DUO_LIGHT = (176, 36, 128), (255, 176, 150)       # 赤紫のデュオトーン（明るさを周りと揃える）


def seg(t):
    for t0, t1, name in SEGS:
        if t0 <= t < t1:
            return name, t0, t1
    return SEGS[-1][2], SEGS[-1][0], SEGS[-1][1]


def _climax_sched():
    out, t, d, i = [], 16.0, 0.7, 0
    while t < 20.8 - 1e-9:
        e = min(20.8, t + d)
        out.append((t, e, MODES[i % 4]))
        t, d, i = e, max(0.15, d * 0.84), i + 1
    out.append((20.8, 21.5, 'max'))
    return out


CLIMAX = _climax_sched()


def in_quiet(t):
    return any(a <= t < b for a, b in QUIET)


def is_snap(t):
    """破壊の途中に1コマ（12fps）だけ静の画面へ戻る。"""
    name, t0, t1 = seg(t)
    if in_quiet(t):
        return False
    if name == 'tear' and t - t0 > 0.25:
        p = 0.16
    elif name == 'climax' and t < 20.8:
        if any(m0 <= t < m1 and mode == 'rampage' for m0, m1, mode in CLIMAX):
            return False                                  # 写真が出る型の途中では戻らない（明滅を増やさない）
        p = 0.1 + 0.16 * (t - t0) / (t1 - t0)
    else:
        return False
    return np.random.default_rng(55000 + int(t * 12)).random() < p


def is_stutter(t):
    """0.1秒単位で映像と音が引っかかる（最初のコマで止まる）。"""
    name, *_ = seg(t)
    if name not in ('melt', 'climax') or in_quiet(t) or is_snap(t):
        return False
    return np.random.default_rng(777 + int(t * 10)).random() < 0.25


def destroying(t):
    name, *_ = seg(t)
    if AFTERSHOCK[0] <= t < AFTERSHOCK[1]:
        return True
    return name in DESTROY and not is_snap(t) and not in_quiet(t)




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


# ---------------------------------------------------------------- 追加のエフェクト
_dr = np.random.default_rng(88)
DRIP = np.asarray(Image.fromarray((_dr.random((1, 24)) * 255).astype(np.uint8)).resize((W, 1), Image.BICUBIC), float)[0] / 255
DRIP = DRIP ** 2


def melt_fx(a, amount):
    """溶け落ち：列ごとに違う量だけ下へ垂れる。"""
    d = (amount * (0.25 + DRIP)).astype(int)
    ys = np.clip(np.arange(H)[:, None] - d[None, :], 0, H - 1)
    return a[ys, np.arange(W)[None, :]]


def pixel_sort(a, y0, y1, x0, x1):
    seg_ = a[y0:y1, x0:x1]
    idx = np.argsort(seg_.astype(int).sum(2), axis=1)
    a[y0:y1, x0:x1] = np.take_along_axis(seg_, idx[..., None], 1)
    return a


def mosaic(a, bs):
    return np.asarray(Image.fromarray(a).resize((W // bs, H // bs), Image.BOX).resize((W, H), Image.NEAREST)).copy()


def vroll(a, off):
    """CRTの縦ロール。継ぎ目に暗い帯。"""
    off %= H
    o = np.roll(a, off, 0)
    o[off:off + 6] = (28, 18, 34)
    return o


def big_tile(a, k, x, y, s):
    t = np.asarray(Image.fromarray(FRAGS[k % NFRAG]).resize((8, 8), Image.BILINEAR).resize((s, s), Image.NEAREST))
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + s), min(H, y + s)
    if x1 > x0 and y1 > y0:
        a[y0:y1, x0:x1] = t[y0 - y:y1 - y, x0 - x:x1 - x]


def photo_pass(a, t, p=0.65):
    """写真。全面・上下分割・帯は1/3秒単位でしか切り替えない（全面の明滅を毎秒3回以下に保つ）。"""
    slot = int(t * 3)
    ps = np.random.default_rng(700 + slot)
    q = ps.random()
    i, j, mode = int(ps.integers(NFRAG)), int(ps.integers(NFRAG)), int(ps.integers(4))
    if q < p / 3:
        a = frag_full(i, mode).copy()
    elif q < 2 * p / 3:
        ys = int(ps.integers(200, 440))
        a[:ys] = frag_full(i, mode)[:ys]; a[ys:] = frag_full(j, (mode + 1) % 4)[ys:]
    elif q < p:
        y0 = int(ps.integers(40, 400))
        a[y0:y0 + 220] = frag_full(i, mode)[y0:y0 + 220]
    rr = np.random.default_rng(int(t * 12) + 4321)             # 細い帯はコマ単位で差し込む
    for _ in range(int(rr.integers(1, 4))):
        y0, h = int(rr.integers(0, H - 60)), int(rr.integers(8, 60))
        a[y0:y0 + h] = frag_full(int(rr.integers(NFRAG)), int(rr.integers(4)))[y0:y0 + h]
    return a


def text_rain(a, t, u, rs, cols=20, big=3):
    im = Image.fromarray(a)
    c = [CRASH[int(rs.integers(5))], CRASH[int(rs.integers(5))]]  # 1コマに事故色は2色まで
    for k in range(cols):
        if rs.random() < 0.35: continue
        rk = np.random.default_rng(k + 50)
        y = int((rk.random() * 700 + (200 + 400 * rk.random()) * t) % 760) - 60
        sc = 1 + (k * 7) % big
        for n_ in range(3):
            stamp(im, (8 + k * 18) % W, y + n_ * G * sc, int(rs.integers(NPSEUDO)), c[k % 2], scale=sc)
    return np.asarray(im).copy()


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
    if t < 24.4: return 0.0
    if t < 26.6: return 0.98 * EASE2[min(int((t - 24.4) * 12), len(EASE2) - 1)]
    return 0.99 if t >= 28.6 else 0.98


def click_times():
    out, prev = [], None
    for i in range(int(DUR * 120)):
        t = i / 120
        p = progress_main(t) if t < 2.4 else progress_front(t) if T_RET <= t < T_LOOP else None
        if p is not None and prev is not None and p != prev:
            out.append((t, p))
        prev = p
    return out


PROG = click_times()

SWAY_AMP = {'tear': 7, 'swarm': 4, 'rampage': 6, 'melt': 3, 'climax': 8}


def sway(t):
    name, *_ = seg(t)
    amp = SWAY_AMP.get(name, 0)
    tq = int(t * 12) / 12
    return int(round(amp * math.sin(2 * math.pi * 1.4 * tq)))


def look(t):
    if 2.1 <= t < 2.25: return -3        # 最初の破壊の直前、目が左右を見る
    if 2.25 <= t < 2.4: return 3
    return 0


def blinking(t, delay=0.0):
    return any(tb + delay <= t < tb + delay + 0.15 for tb in BLINKS)


def line2_ids(t, step):
    name, t0, t1 = seg(t)
    if t >= T_RET:
        return LINE2_END
    ids = READ_ID.copy()
    if name in ('calm1', 'swap', 'half', 'hook'):
        return ids
    if name == 'calm2':
        for i in CORR_ORDER[:3]: ids[i] = CH_FIX[i]
        return ids
    if name == 'freeze':
        return [CH_FIX[i] for i in range(11)]
    n = min(11, 1 + int((t - t0) / 0.15)) if name == 'tear' else 11
    for i in CORR_ORDER[:n]:
        ids[i] = int(np.random.default_rng(i * 7 + step).integers(NPSEUDO))
    return ids


# 破壊2で湧き出るウィンドウ： (出現時刻, x, y, style, 漂う速度)
_sw = np.random.default_rng(321)
SWARM = [(4.5 + i * 0.11, int(_sw.integers(-60, 200)), int(_sw.integers(40, 500)), int(_sw.integers(0, 4)),
          (float(_sw.uniform(-70, 70)), float(_sw.uniform(-60, 60)))) for i in range(20)]
SWARM_END = 6.8


def windows(t, prog):
    name, *_ = seg(t)
    wins = [(64, 96, 0, 1.0, prog)]
    if name in ('swap', 'half') or t >= T_RET:
        wins.append((82, 118, 0, 1.0, progress_front(t)))
    elif name == 'swarm':
        for (ts, x, y, sty, v) in SWARM:
            if t >= ts:
                for k in (3, 2, 1, 0):                      # 残像の尾を引いて漂う
                    tt = max(ts, t - k * 0.05)
                    wins.append((x + v[0] * (tt - ts), y + v[1] * (tt - ts), sty, [1, .45, .28, .15][k], prog))
    elif name in ('rampage', 'freeze', 'melt', 'climax', 'collapse'):
        for i, (ts, x, y, sty, v) in enumerate(SWARM):     # 増殖した窓の半分は居座る
            if i % 2 == 0:
                wins.append((x + v[0] * (SWARM_END - ts), y + v[1] * (SWARM_END - ts), sty, 1.0, prog))
    return wins


def state(t):
    name, *_ = seg(t)
    st = {'t': t}
    step = int(t * 12)
    st['pidx'] = step % 6
    st['scale'] = round(1.22 + 0.02 * math.sin(2 * math.pi * step / 12 * 0.9), 4)
    sw, swp = sway(t), sway(t - 1 / 12)
    st['sway'] = sw
    st['edx'] = look(t - 1 / 12) + (swp - sw) + (2 if t >= T_RET else 4 if name == 'freeze' else 0)
    st['edy'] = 0
    st['blink'] = blinking(t)
    st['progress'] = progress_main(t)
    rr = np.random.default_rng(step * 3 + 5)
    l1 = LINE1.copy()
    live = name in DESTROY
    for _ in range(4 if live else 1):
        if rr.random() < 0.25 or live: l1[int(rr.integers(9))] = int(rr.integers(NPSEUDO))
    l1c = [(g, FRAME) for g in l1]
    if live or name == 'freeze': l1c[3] = (l1[3], RD)
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
        for w in st['wins'][1:]:
            x, y = int(w[0]), int(w[1])
            if -WW < x < W and -WH < y < H: draw_win(w)
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


# ---------------------------------------------------------------- 破壊の型（climax でも使い回す）
def fx_tear(t, st, r, rs, u, power=1.0):
    """顔が裂けてBが覗き、裂け目が画面全体に走る。写真が突き刺さる。"""
    A, mx, my, mm = render(st)
    B, *_ = render(st, variant=1)
    a = slices(A, B, rs, int((8 + 18 * u) * power), int((20 + 50 * u) * power), my + 20, my + 180, mx, mx + MC)
    a = slices(a, B, r, int((3 + 10 * u) * power), int(20 + 60 * u), 0, H)
    for k in range(1 + int(2 * u * power)):
        s = [64, 96, 128][int(rs.integers(3))]
        big_tile(a, int(rs.integers(NFRAG)), int(rs.integers(-30, W - 40)), int(rs.integers(0, H - 60)), s)
    if 2.6 <= t < 2.6 + 1 / FPS or rs.random() < 0.1:
        y = int(rs.integers(120, 520)); a[y:y + 6] = YG; a[y + 6:y + 9] = np.roll(a[y + 6:y + 9], 12, 1)
    for te, nf in ERRORS:
        if te <= t < te + nf / FPS:
            a = with_error(a, int(rs.integers(20, 220)), int(rs.integers(150, 420)), np.random.default_rng(int(te * 10)))
    a = block_glitch(a, r, int((10 + 30 * u) * power))
    a = rgb_shift(a, int((4 + 10 * u) * power))
    if rs.random() < 0.15:                                 # 目にズームで突っ込む
        a = zoom(a, 2.4, 180, 356)
    if u > 0.75:
        a = crush(a, 10)
    return a


def fx_swarm(t, st, r, rs, u, tl, power=1.0):
    """ウィンドウが湧き、エラーが連鎖し、背景だけRGBがずれる。"""
    A, mx, my, mm = render(st)
    B, *_ = render(st, variant=1)
    A = slices(A, B, rs, int((6 + 10 * u) * power), 30, my + 60, my + 175, mx, mx + MC)
    A[my + 30:my + 170:2, 180:mx + MC] = B[my + 30:my + 170:2, 180:mx + MC]
    n = int(tl * 22 * power)                               # エラーの連鎖
    if n > 0:
        im = Image.fromarray(A)
        for i in range(max(0, n - 40), n):
            error_box(im, 6 + (i % 24) * 9, 70 + (i % 24) * 14 + (i // 24) * 26, np.random.default_rng(i))
        A = np.asarray(im).copy()
    dx = int((6 + 8 * u) * power)
    a = bg_only(A, lambda z: rgb_shift(z, dx), mm, mx, my)
    for _ in range(int(3 + 6 * u)):
        y = int(r.integers(0, H)); a[y:y + 1 + int(r.integers(0, 3))] = CY if r.random() < .5 else MG
    if int(t * 10) % 4 == 0 and int(t * FPS) % 3 < 2:     # 拍に合わせたズームの突き
        cr = np.random.default_rng(int(t * 10))
        a = zoom(a, 1.8, int(cr.integers(80, 280)), int(cr.integers(150, 500)))
    a = block_glitch(a, r, int((8 + 14 * u) * power))
    if u > 0.87:
        a = crush(vroll(a, int((u - 0.87) * 3000)), 12)
    return a


def fx_rampage(t, st, r, rs, u, power=1.0, photos=True):
    """拡大縮小、写真、文字の雨、帯の反転、ピクセル破損、JPEG。"""
    A, mx, my, mm = render(st)
    B, *_ = render(st, variant=1)
    A = slices(A, B, rs, 12, 40, my + 20, my + 175, mx, mx + MC)
    A[my + 30:my + 170:2, 180:mx + MC] = B[my + 30:my + 170:2, 180:mx + MC]
    a = zoom(A, 1 + 1.1 * math.sin(math.pi * u * 3) ** 2, 180, 360)
    if photos:
        a = photo_pass(a, t)
    a = text_rain(a, t, u, rs)
    if rs.random() < 0.5:
        im = Image.fromarray(a); error_box(im, int(rs.integers(10, 230)), int(rs.integers(60, 540)), rs); a = np.asarray(im).copy()
    for _ in range(int(rs.integers(1, 4))):                # 帯の反転（面積は小さく）
        y0, hb = int(rs.integers(0, H - 30)), int(rs.integers(6, 34))
        a[y0:y0 + hb] = 255 - a[y0:y0 + hb]
    a = block_glitch(a, r, int((18 + 20 * u) * power))
    for _ in range(int(6 * power)):
        y, h = int(r.integers(0, H)), int(r.integers(2, 24))
        a[y:y + h] = np.roll(a[y:y + h], int(r.integers(-60, 61)), 1)
    if rs.random() < 0.3:
        a = mosaic(a, [6, 10, 16][int(rs.integers(3))])
    a = crush(a, 6 + int(rs.integers(0, 16)))
    return rgb_shift(a, int((4 + 8 * rs.random()) * power))


def fx_melt(t, st, r, rs, u, power=1.0):
    """溶け落ちる。ピクセルソートの筋、後半は縦ロール。"""
    A, mx, my, mm = render(st)
    B, *_ = render(st, variant=1)
    A[my + 30:my + 170:2, 180:mx + MC] = 255 - B[my + 30:my + 170:2, 180:mx + MC]   # 反転したBが縞で覗く
    a = melt_fx(A, (40 + 260 * u ** 1.3) * power)
    for _ in range(int(2 + 6 * u)):
        y0 = int(rs.integers(0, H - 40)); x0 = int(rs.integers(0, W // 2))
        a = pixel_sort(a, y0, y0 + int(rs.integers(10, 60)), x0, min(W, x0 + int(rs.integers(80, 300))))
    a = block_glitch(a, r, int(8 * power))
    if u > 0.45:
        a = vroll(a, int(((u - 0.45) / 0.55) ** 1.5 * H * 1.6))
    a = rgb_shift(a, int(3 + 6 * u))
    if u > 0.92:
        a = mosaic(a, 16)
    return a


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


def sc_calm1(t, st, r, rs, t0, t1):
    a, *_ = render(st)
    if 3 <= int(t * FPS) < 7: a = rgb_shift(a, [5, 3, 2, 1][int(t * FPS) - 3])   # フックの余韻
    if t >= 2.1: a = omen(a, r)
    return a


def sc_tear(t, st, r, rs, t0, t1):
    return fx_tear(t, st, r, rs, (t - t0) / (t1 - t0))


def sc_calm2(t, st, r, rs, t0, t1):
    a, *_ = render(st, icon=ANOM_TILES[-1])
    if t >= t1 - 0.15: a = omen(a, r)
    return a


def sc_swarm(t, st, r, rs, t0, t1):
    return fx_swarm(t, st, r, rs, (t - t0) / (t1 - t0), max(0.0, t - CASCADE0))


def sc_swap(t, st, r, rs, t0, t1):
    a, *_ = render(st, variant=1)
    return a


def sc_rampage(t, st, r, rs, t0, t1):
    if in_quiet(t):
        return render(st, variant=1, show_windows=False)[0]
    a = fx_rampage(t, st, r, rs, (t - t0) / (t1 - t0))
    for d0, d1 in DUOTONE:
        if d0 <= t < d1: a = duotone(a, DUO_DARK, DUO_LIGHT)
    return a


def sc_freeze(t, st, r, rs, t0, t1):
    """止まる。ただし窓も写真も化けた文字も残ったまま。"""
    a, *_ = render(st, icon=ANOM_TILES[-1])
    return a


def sc_melt(t, st, r, rs, t0, t1):
    return fx_melt(t, st, r, rs, (t - t0) / (t1 - t0))


def sc_half(t, st, r, rs, t0, t1):
    """左半分がA、右半分がB。静かに座っている。"""
    A, mx, my, mm = render(st)
    B, *_ = render(st, variant=1)
    A[my:my + MC, 180:mx + MC] = B[my:my + MC, 180:mx + MC]
    return A


def sc_climax(t, st, r, rs, t0, t1):
    """これまでの破壊が、短く速く、全部戻ってくる。"""
    if in_quiet(t):
        return render(st, variant=1, show_windows=False)[0]
    if is_snap(t):
        st = dict(st, sway=0)
        return render(st)[0]
    for m0, m1, mode in CLIMAX:
        if m0 <= t < m1: break
    u = (t - m0) / (m1 - m0)
    if mode == 'tear': a = fx_tear(t, st, r, rs, 0.5 + 0.5 * u, 1.3)
    elif mode == 'swarm': a = fx_swarm(t, st, r, rs, 0.5 + 0.5 * u, 1.0 + u, 1.3)
    elif mode == 'rampage': a = fx_rampage(t, st, r, rs, u, 1.3, photos=False)
    elif mode == 'melt': a = fx_melt(t, st, r, rs, 0.3 + 0.6 * u, 1.2)
    else:                                                   # 最大：全部重ねる
        a = fx_rampage(t, st, r, rs, u, 1.6, photos=False)
        a = melt_fx(a, 80 + 200 * u)
        B, *_ = render(st, variant=1)
        a = slices(a, B, r, 20, 90, 0, H)
        a = rgb_shift(a, int(10 + 10 * u))
    if mode in ('rampage', 'max'):
        a = photo_pass(a, t, 0.55)
    for d0, d1 in DUOTONE:
        if d0 <= t < d1: a = duotone(a, DUO_DARK, DUO_LIGHT)
    return a


def sc_collapse(t, st, r, rs, t0, t1):
    """縦に潰れて横線、横線が縮んで点、点が無音の間のマスコットになる。"""
    u = (t - t0) / (t1 - t0)
    a = PAPER_WHITE.copy()
    cy = 360
    if u < 0.5:
        src = sc_climax(20.8 + u, state(20.8 + u), r, rs, 16.0, 21.5)
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


def sc_silence(t, st, r, rs, t0, t1):
    return _silence(blinking(t)).copy()


def sc_ret(t, st, r, rs, t0, t1):
    """冒頭と同じ画面。ただし窓2枚、目が2px横、5文字目が化けたまま。途中で一度だけ余震。"""
    if AFTERSHOCK[0] <= t < AFTERSHOCK[1]:
        return fx_tear(t, st, r, rs, 0.9, 1.2)
    a, mx, my, mm = render(st)
    if t >= 29.4:
        a = omen(a, r)
        if 29.6 <= t < 29.6 + 1 / FPS:                    # Bが1フレームだけ覗く
            B, *_ = render(st, variant=1)
            a[my + 96:my + 112, mx:mx + MC] = B[my + 96:my + 112, mx:mx + MC]
    return a


SCENES = {'calm1': sc_calm1, 'tear': sc_tear, 'calm2': sc_calm2, 'swarm': sc_swarm, 'swap': sc_swap,
          'rampage': sc_rampage, 'freeze': sc_freeze, 'melt': sc_melt, 'half': sc_half, 'climax': sc_climax,
          'collapse': sc_collapse, 'silence': sc_silence, 'ret': sc_ret}


def frame(f):
    if f < 3: return hook(f)
    if f >= NF - 3: return hook(f - (NF - 3))
    t = f / FPS
    if is_stutter(t):                                      # 0.1秒の頭のコマで引っかかる
        f = int(t * 10) * 3
        t = f / FPS
    name, t0, t1 = seg(t)
    if name == 'tear' and is_snap(t):                      # 破壊の途中で一瞬だけ静に戻る
        st = state(t); st['sway'] = 0
        return render(st)[0]
    st = state(t)
    r = np.random.default_rng(f * 17 + 3)
    rs = np.random.default_rng(int(t * 12) * 11 + 1)      # 12fps相当で保持する乱数
    return SCENES[name](t, st, r, rs, t0, t1)


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
    rng = np.random.default_rng(7)
    L, R = np.zeros(n), np.zeros(n)            # 静の層（ハム、クリック）
    DL, DR = np.zeros(n), np.zeros(n)          # 破壊の層（破壊中だけ鳴り、静に戻る瞬間に切れる）

    def add(buf, t0, sig, pan=0.0, g=1.0):
        bl, br = buf
        i = int(t0 * SR)
        if i >= n or i < 0:
            return
        s = sig[:n - i] * g
        bl[i:i + len(s)] += s * (1 - max(0, pan))
        br[i:i + len(s)] += s * (1 + min(0, pan))

    def env(d, a=0.002, r=None):
        k = np.arange(int(d * SR)) / SR
        e = np.minimum(1, k / a)
        return e * (np.exp(-k / r) if r else 1)

    def sq(f, d):
        return np.sign(np.sin(2 * np.pi * f * np.arange(int(d * SR)) / SR))

    def sweep(f0, f1, d, wave="sq"):
        k = np.arange(int(d * SR)) / SR
        ph = 2 * np.pi * np.cumsum(f0 * (f1 / f0) ** (k / d)) / SR
        return np.sign(np.sin(ph)) if wave == "sq" else np.sin(ph)

    def crushed_noise(d, hold=24, bits=3):
        m = int(d * SR)
        x = rng.uniform(-1, 1, m // hold + 1).repeat(hold)[:m]
        q = 2 ** bits
        return np.round(x * q) / q

    S, D = (L, R), (DL, DR)

    # マスク（12fps単位で映像と同じ判定を使う）
    slot_t = np.arange(int(DUR * 120)) / 120
    dm = np.array([destroying(x) for x in slot_t], float).repeat(SR // 120)[:n]
    names = [seg(x)[0] for x in slot_t]
    sil = np.array([nm in ('silence', 'collapse') for nm in names], float).repeat(SR // 120)[:n]
    quiet = np.array([in_quiet(x) for x in slot_t], float).repeat(SR // 120)[:n]

    # 床：ハム。入れ替わりの場面では低く濁る。破壊中は奥に沈む
    f = np.array([55.0 if nm == 'swap' else 52.0 if nm == 'half' else 60.0 for nm in names]).repeat(SR // 120)[:n]
    ph = 2 * np.pi * np.cumsum(f) / SR
    hum = 0.05 * np.sin(ph) + 0.03 * np.sin(2 * ph) + 0.008 * rng.normal(0, 1, n)
    hum *= np.where(dm > 0, 0.35, 1.0) * (1 - sil) * (1 - quiet)
    hum *= np.clip((tt - 23.8) / 0.03, 0, 1) * (tt >= 23.8) + (tt < 23.8)          # 帰還は30msで復帰
    omen_ = ((tt >= 2.1) & (tt < 2.4)) | ((tt >= 4.25) & (tt < 4.4)) | ((tt >= 29.4) & (tt < T_LOOP))
    hum = np.where(omen_, np.clip(hum * 4, -0.09, 0.09), hum)
    L += hum; R += hum

    # フック / ループ繋ぎ
    roar = (crushed_noise(0.1) * 0.6 + 0.35 * sq(45, 0.1)) * env(0.1, 0.001)
    add(S, 0.0, roar); add(S, T_LOOP, roar)

    click = (rng.uniform(-1, 1, int(0.004 * SR)) * 0.5 + np.sin(2 * np.pi * 2200 * np.arange(int(0.004 * SR)) / SR)) * env(0.004, 0.0003, 0.0012)
    for ts, _ in PROG:
        add(S, ts, click, pan=-0.2, g=0.3)
    add(S, 7.2, click, g=0.2)

    # 破壊の層：0.1秒（全壊の後半は0.05秒）グリッドのスタッター
    gain = {'tear': 0.34, 'swarm': 0.32, 'rampage': 0.42, 'melt': 0.36, 'climax': 0.45}
    for t0, t1, nm in SEGS:
        if nm not in DESTROY: continue
        t = t0
        while t < t1 - 1e-6:
            u = (t - t0) / (t1 - t0)
            step = 0.05 if (nm == 'climax' and u > 0.5) else 0.1
            q = rng.random()
            if q < 0.4:
                s = crushed_noise(step, int(rng.integers(4, 40)), int(rng.integers(2, 4)))
            elif q < 0.75:
                s = sq(float(rng.uniform(80, 2000)), step) * 0.6
            elif q < 0.92:
                s = sweep(float(rng.uniform(200, 3000)), float(rng.uniform(60, 3000)), step) * 0.6
            else:
                s = np.zeros(int(step * SR))
            add(D, t, s * env(step, 0.001), pan=float(rng.uniform(-0.6, 0.6)), g=gain[nm] * (1 + 0.4 * u))
            t += step
    # 破壊1：衝撃と裂けるクラックル、エラー
    add(D, 2.4, np.sin(2 * np.pi * np.cumsum(np.linspace(120, 30, int(0.25 * SR))) / SR) * env(0.25, 0.001, 0.12), g=0.6)
    for ts in np.arange(2.4, 3.6, 1 / 500):
        if rng.random() < 0.35:
            add(D, ts, rng.uniform(-1, 1, 96) * np.exp(-np.arange(96) / 20), pan=rng.uniform(-0.7, 0.7), g=rng.uniform(0.15, 0.45))
    err = np.concatenate([sq(880, 0.09) * env(0.09, 0.002), sq(660, 0.12) * env(0.12, 0.002, 0.08)])
    for te, _ in ERRORS:
        add(D, te, err, g=0.2)
    # 破壊2：窓が湧くたびのブリップ、エラー連鎖の音
    for (ts, x, y, sty, v) in SWARM:
        add(D, ts, sq(1400 + 60 * sty, 0.03) * env(0.03, 0.001, 0.01), pan=(x - 70) / 200, g=0.18)
    for i in range(0, int((6.8 - CASCADE0) * 22), 3):
        add(D, CASCADE0 + i / 22, sq(880 if (i // 3) % 2 == 0 else 660, 0.05) * env(0.05, 0.001, 0.03), g=0.14)
    # 破壊3：拡大縮小に同期した48Hz
    m = (tt >= 7.6) & (tt < 11.6)
    DL[m] += (0.35 * np.sin(2 * np.pi * 48 * tt) * np.sin(np.pi * (tt - 7.6) / 4.0 * 3) ** 2)[m]
    DR[m] += (0.35 * np.sin(2 * np.pi * 48 * tt) * np.sin(np.pi * (tt - 7.6) / 4.0 * 3) ** 2)[m]
    # 破壊4：落ちていくドローン
    d = 3.0
    wob = np.sin(2 * np.pi * np.cumsum(220 * (30 / 220) ** (np.arange(int(d * SR)) / SR / d) * (1 + 0.04 * np.sin(2 * np.pi * 5 * np.arange(int(d * SR)) / SR))) / SR)
    add(D, 12.0, np.sign(wob) * 0.5 + wob * 0.3, g=0.35)
    # 破壊5：8Hzで刻む40Hz
    m = (tt >= 16.0) & (tt < 21.5)
    pul = 0.4 * np.sin(2 * np.pi * 40 * tt) * (0.5 + 0.5 * np.sign(np.sin(2 * np.pi * 8 * tt))) * np.clip((tt - 16.0) / 5.5 + 0.4, 0, 1.4)
    DL[m] += pul[m]; DR[m] += pul[m]
    # 余震
    add(D, AFTERSHOCK[0], crushed_noise(0.1, 6, 2) * env(0.1, 0.001), g=0.6)

    # 音の引っかかり：映像と同じ0.1秒で、頭の20msを繰り返す
    for k in range(int(DUR * 10)):
        tk = k / 10
        if is_stutter(tk + 0.001):
            i0 = int(tk * SR); ln = int(0.02 * SR)
            for buf in (DL, DR):
                buf[i0:i0 + int(0.1 * SR)] = np.tile(buf[i0:i0 + ln], 5)[:len(buf[i0:i0 + int(0.1 * SR)])]

    L += DL * dm; R += DR * dm

    # 崩落：テープが止まるように落ちる
    d = 0.5
    stop = (sweep(300, 20, d) * 0.6 + crushed_noise(d, 16, 3) * 0.4) * np.linspace(1, 0, int(d * SR)) ** 1.5
    add(S, 21.5, stop, g=0.45)

    # 無音の間とB単独のコマは完全にゼロ
    z = ((tt >= 22.0) & (tt < 23.8)) | (quiet > 0)
    L[z] = 0; R[z] = 0

    peak = max(np.abs(L).max(), np.abs(R).max())
    st = np.stack([L, R], 1) * (0.63 / peak)          # 約-14 LUFS（ショート動画の配信基準付近）
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
           "-c:v", "libx264", "-profile:v", "high", "-crf", os.environ.get("CRF", "20"), "-preset", "medium", "-pix_fmt", "yuv420p",
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
        preview(sys.argv[2:] or ["0", "1.0", "3.0", "5.5", "9.0", "13.5", "18.0", "26.0"])
    elif len(sys.argv) >= 3 and sys.argv[1] == "video":
        video(sys.argv[2])
    else:
        print(__doc__)
