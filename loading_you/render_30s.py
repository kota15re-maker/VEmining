#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""読み込み中のあなた — 30秒版

render.py（15秒版）の素材（紙、疑似文字、ウィンドウ、マスコット、写真断片、エフェクト、
フック、仕上げ）をそのまま使い、30秒のタイムラインと破壊の型を足したもの。
破壊は5回。回ごとに違う手法を使い、同じ型の繰り返しに頼らない。
映像と音はすべて手続き的に生成する。乱数はシード固定。

    python3 render_30s.py preview 0 1.0 5.3 10.5 14.5 17.0 22.0 26.0   # コンタクトシート
    python3 render_30s.py video out.mp4                                 # 音声付きで書き出し

環境変数で上書きできるもの:
    FONT_KANA / FONT_SYM  フォントパス
    FFMPEG                ffmpeg 実行ファイル
    OUT_DIR               preview と中間 WAV の出力先（既定はこのファイルの場所）
    CRF                   H.264 の画質（既定18。小さいほど高画質・大容量）
写真素材は photos/ に画像を置くと FRAGS がそれを読む（無ければ手続き生成の代用品）。
"""
import glob
import io
import math
import os
import re
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
# 破壊は5回。回ごとに手法の系統を変え、強く長くしていく。間の「静」は短く、少しずつ間違っていく。
SEGS = [
    (0.0, 0.1, 'hook'),       # フック（3フレーム）
    (0.1, 2.4, 'calm1'),      # 静 I：本物
    (2.4, 3.6, 'tear'),       # 破壊1「裂け」：15秒版の裂け目。顔が裂けてBが出る
    (3.6, 4.4, 'calm2'),      # 静 II：偽の復旧。写真が1枚アイコンとして残る
    (4.4, 6.8, 'ui'),         # 破壊2「操作」：カーソルが目を切り取って貼り散らかす。窓とエラーが湧き、本体が跳ねて軌跡を残す
    (6.8, 7.6, 'swap'),       # 静 III：Bが何食わぬ顔で座る
    (7.6, 11.6, 'breakdown'), # 破壊3「暴走」：15秒版の暴走を下敷きに、クローズアップのジャンプカットを挟む
    (11.6, 12.0, 'freeze'),   # 静 IV：止まるが、残骸がすべて残ったまま
    (12.0, 15.0, 'paper'),    # 破壊4「紙」：網点の版ずれ → スキャナーの滑り → 紙が破れて剥がれ落ちる
    (15.0, 16.0, 'half'),     # 静 V：左半分がA、右半分がB
    (16.0, 21.5, 'climax'),   # 破壊5「全壊」：新しい型が次々に切り替わり、最後は型ごとのタイルが並ぶ
    (21.5, 22.0, 'collapse'), # 崩落：解像度が落ちきって1点になる
    (22.0, 23.8, 'silence'),  # 無音の間
    (23.8, 29.9, 'ret'),      # 帰還：冒頭と同じ画面。ただし窓2枚、目のずれ、化けた1文字
    (29.9, 30.0, 'hook'),     # ループ繋ぎ
]
DESTROY = {'tear', 'ui', 'breakdown', 'paper', 'climax'}
T_RET = 23.8
T_LOOP = 29.9
BLINKS = [1.9, 7.2, 15.5, 23.0, 26.2]
ERRORS = [(2.9, 2), (3.25, 2)]                              # 破壊1のエラー
QUIET = [(9.6, 9.7), (18.6, 18.7)]                          # B単独の静かな3フレーム
AFTERSHOCK = (27.3, 27.4)                                   # 帰還中の余震（3フレーム）
DUO_DARK, DUO_LIGHT = (176, 36, 128), (255, 176, 150)       # 赤紫のデュオトーン（明るさを周りと揃える）

# 破壊2「操作」の段取り
CUR_IN = (4.4, 4.7)          # カーソルが目へ向かう
SEL = (142, 340, 218, 370)   # 目の選択範囲
SEL_T = (4.7, 4.9)           # 範囲選択をドラッグ
CUT_T = 4.9                  # 切り取り（顔から目が消える）
PASTE_T = (4.95, 5.6)        # 目を貼り散らかす
SPAWN_T0, CASCADE0, BOUNCE_T0 = 5.5, 5.8, 6.0

# 破壊4「紙」の段取り
PAPER_PH = [(12.0, 12.9, 'halftone'), (12.9, 13.8, 'scan'), (13.8, 15.0, 'rip')]

# 破壊5「全壊」で使う型（どれも1回ずつ。後半は短いカットで一巡し、最後はタイル状に並ぶ）
CLIMAX_MODES = ['puzzle', 'raster', 'textize', 'kaleido', 'tunnel', 'rewind', 'melt', 'palette']


def seg(t):
    for t0, t1, name in SEGS:
        if t0 <= t < t1:
            return name, t0, t1
    return SEGS[-1][2], SEGS[-1][0], SEGS[-1][1]


def _climax_sched():
    out, t, i = [], 16.0, 0
    durs = [0.9, 0.8, 0.7, 0.62, 0.55, 0.5, 0.42, 0.36]
    for m, d in zip(CLIMAX_MODES, durs):
        out.append((t, t + d, m)); t += d
    k = 0
    while t < 20.9 - 1e-9:                                   # 短いカットで一巡
        e = min(20.9, t + 0.1 if k % 2 else t + 0.13)
        out.append((t, e, CLIMAX_MODES[(k * 3) % 8])); t = e; k += 1
    out.append((20.9, 21.5, 'grid'))
    return out


CLIMAX = _climax_sched()


def in_quiet(t):
    return any(a <= t < b for a, b in QUIET)


def climax_mode(t):
    for m0, m1, mode in CLIMAX:
        if m0 <= t < m1:
            return m0, m1, mode
    return CLIMAX[-1]


def is_snap(t):
    """破壊の途中に1コマ（12fps）だけ静の画面へ戻る。破壊1と全壊だけ。"""
    name, t0, t1 = seg(t)
    if in_quiet(t):
        return False
    if name == 'tear' and t - t0 > 0.25:
        p = 0.16
    elif name == 'climax' and t < 20.9:
        if climax_mode(t)[2] in ('rewind', 'palette'):
            return False
        p = 0.08 + 0.14 * (t - t0) / (t1 - t0)
    else:
        return False
    return np.random.default_rng(55000 + int(t * 12)).random() < p


def is_closeup(t):
    """暴走の途中、ときどき2コマだけ静かなクローズアップへジャンプカット。"""
    name, t0, t1 = seg(t)
    if name != 'breakdown' or in_quiet(t) or t - t0 < 0.3 or t1 - t < 0.6:
        return False
    if photo_full(t):                                        # 全面写真の最中には明るいカットを挟まない（明滅を増やさない）
        return False
    return (t * 3) % 1 < 2 / FPS * 3 - 1e-6                  # 1/3秒の区切りの頭2コマ


def photo_full(t):
    fl = np.random.default_rng(700 + int(t * 3))
    if fl.random() >= 0.6:                                   # fx_breakdown と同じ順番で乱数を引く
        return False
    fl.integers(NFRAG); fl.integers(4)
    return fl.random() < 0.45


def destroying(t):
    name, *_ = seg(t)
    if AFTERSHOCK[0] <= t < AFTERSHOCK[1]:
        return True
    return name in DESTROY and not is_snap(t) and not in_quiet(t) and not is_closeup(t)


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




# ---------------------------------------------------------------- 画質の方針
# 破壊はなるべく「最近傍・ハードエッジ」の変形（ずらす、並べ替える、折り返す、置き換える）で作る。
# 画素を溶かす JPEG 劣化とモザイクは、帯状の一部か、崩落の数フレームにだけ使う。どのコマで使ったかは LOSSY に残す。
LOSSY = set()
CUR = [0]


def crush_region(a, y0, y1, q):
    """JPEG劣化を帯状の範囲にだけかける。"""
    y0, y1 = max(0, y0), min(H, y1)
    if y1 - y0 < 4:
        return a
    LOSSY.add(CUR[0])
    a[y0:y1] = crush(np.ascontiguousarray(a[y0:y1]), q)[:y1 - y0, :W]
    return a


def mosaic(a, bs):
    if bs <= 1:
        return a
    LOSSY.add(CUR[0])
    h, w = -(-H // bs), -(-W // bs)
    sm = np.asarray(Image.fromarray(a).resize((w, h), Image.BOX))
    return sm.repeat(bs, 0).repeat(bs, 1)[:H, :W].copy()


_YY, _XX = np.mgrid[0:H, 0:W]


def smooth(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


# ---- 破壊2「操作」：カーソル、範囲選択、貼り付け、跳ねる軌跡
CURSOR_ART = ["X", "XX", "X.X", "X..X", "X...X", "X....X", "X.....X", "X......X", "X.......X", "X........X",
              "X.....XXXXX", "X..X..X", "X.X X..X", "XX  X..X", "X    X..X", "     X..X", "      XX"]
_cur = np.zeros((len(CURSOR_ART), 11), np.uint8)
for _i, _row in enumerate(CURSOR_ART):
    for _j, _c in enumerate(_row):
        _cur[_i, _j] = 1 if _c == 'X' else 2 if _c == '.' else 0
CURSOR = _cur.repeat(2, 0).repeat(2, 1)


def draw_cursor(a, x, y):
    h, w = CURSOR.shape
    x, y = int(x), int(y)
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    c = CURSOR[y0 - y:y1 - y, x0 - x:x1 - x]
    reg = a[y0:y1, x0:x1]
    reg[c == 1] = (20, 16, 26)
    reg[c == 2] = (255, 255, 255)


def marching_ants(a, x0, y0, x1, y1, phase):
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    for (xs, ys) in [(np.arange(x0, x1), np.full(x1 - x0, y0)), (np.arange(x0, x1), np.full(x1 - x0, y1)),
                     (np.full(y1 - y0, x0), np.arange(y0, y1)), (np.full(y1 - y0, x1), np.arange(y0, y1))]:
        ok = (xs >= 0) & (xs < W) & (ys >= 0) & (ys < H)
        on = ((xs + ys + phase) // 3) % 2 == 0
        a[ys[ok], xs[ok]] = np.where(on[ok][:, None], (20, 16, 26), (255, 255, 255))


_pr = np.random.default_rng(12)
PASTES = [(int(_pr.integers(-20, 300)), int(_pr.integers(20, 600))) for _ in range(11)]


def paste_times():
    n = len(PASTES)
    return [PASTE_T[0] + (PASTE_T[1] - PASTE_T[0]) * k / n for k in range(n)]


PASTE_TS = paste_times()


def cursor_pos(t):
    if t < CUR_IN[1]:
        p = smooth((t - CUR_IN[0]) / (CUR_IN[1] - CUR_IN[0]))
        return 300 + (SEL[0] - 300) * p, 560 + (SEL[1] - 560) * p
    if t < SEL_T[1]:
        p = smooth((t - SEL_T[0]) / (SEL_T[1] - SEL_T[0]))
        return SEL[0] + (SEL[2] - SEL[0]) * p, SEL[1] + (SEL[3] - SEL[1]) * p
    if t < PASTE_T[0]:
        return SEL[2], SEL[3]
    k = sum(t >= ts for ts in PASTE_TS) - 1
    if t < PASTE_T[1] + 0.2:
        x, y = PASTES[max(0, k)]
        return x + 20, y + 10
    p = smooth((t - PASTE_T[1] - 0.2) / 0.3)
    x, y = PASTES[-1]
    return x + 20 + (330 - x) * p, y + 10 + (20 - y) * p


def bounce_path():
    """本体が飛び出して跳ねる。1コマごとの位置（軌跡として全部残す）。"""
    out = []
    x, y, vx, vy = float(MX0), float(MY0), 260.0, -620.0
    for k in range(int((6.8 - BOUNCE_T0) * FPS) + 1):
        out.append((int(x), int(y)))
        dt = 1 / FPS
        vy += 2600 * dt; x += vx * dt; y += vy * dt
        if y > 430: y = 430; vy = -abs(vy) * 0.82
        if x > 200 or x < -40: vx = -vx; x = min(200, max(-40, x))
    return out


BOUNCE = bounce_path()


@lru_cache(maxsize=2)
def faceless(variant=0):
    """目の部分が切り抜かれたマスコット。穴から紙が見える。"""
    m = np.array(mascot(1.22, 0, 0, variant, False))
    x0, y0, x1, y1 = SEL[0] - MX0, SEL[1] - MY0, SEL[2] - MX0, SEL[3] - MY0
    hole = m[y0:y1, x0:x1]
    hole[..., :3] = PAPER
    hole[..., 3] = 255
    return Image.fromarray(m)


# ---- 破壊4「紙」：網点の版ずれ、スキャナーの滑り、紙の破れ
def halftone(a, cell, offs):
    """CMYの網点に分解して、版をずらす。"""
    out = np.ones((H, W, 3)) * np.array([246, 241, 232.])
    for ci, (dx, dy) in enumerate(offs):
        ink = 1 - a[..., ci] / 255.0
        cy_ = ((_YY - dy) // cell) * cell + cell // 2 + dy
        cx_ = ((_XX - dx) // cell) * cell + cell // 2 + dx
        v = ink[np.clip(cy_, 0, H - 1), np.clip(cx_, 0, W - 1)]
        rad = np.sqrt(v) * cell * 0.62
        dot = (_YY - cy_) ** 2 + (_XX - cx_) ** 2 < rad ** 2
        out[..., ci] -= dot * 215
    k = a.mean(2) < 110                                    # 墨版は線だけ
    out[k] *= 0.35
    return np.clip(out, 0, 255).astype(np.uint8)


_sr = np.random.default_rng(41)
SCAN_SRC = np.arange(H)
for _y0, _ln in [(150, 26), (330, 44), (500, 18)]:           # 紙が引っかかって同じ行が伸びる所
    SCAN_SRC = np.where(SCAN_SRC >= _y0 + _ln, SCAN_SRC - _ln, np.where(SCAN_SRC >= _y0, _y0, SCAN_SRC))
_band = np.cumsum(_sr.integers(-1, 2, H // 12 + 1)).repeat(12)[:H]
SCAN_DRIFT = _band                                          # 帯ごとに少しずつ横へ滑る


def scan_slip(a, bar, slip):
    o = (a.astype(float) * 0.22 + 236 * 0.78).astype(np.uint8)   # まだ読まれていない所は薄い
    bar = int(min(H, max(0, bar)))
    if bar > 0:
        rows = np.arange(bar)
        src = a[SCAN_SRC[rows]]
        xs = (np.arange(W)[None, :] - (SCAN_DRIFT[rows] * slip).astype(int)[:, None]) % W
        o[:bar] = src[np.arange(bar)[:, None], xs]
    if bar < H:
        o[max(0, bar - 3):bar] = np.clip(o[max(0, bar - 3):bar].astype(int) + [30, 50, 60], 0, 255)
        o[bar:bar + 2] = (225, 250, 255)
    return o


_rr = np.random.default_rng(43)
RIP_Y = (170 + np.arange(W) * 0.85 + np.cumsum(_rr.integers(-3, 4, W)) + _rr.integers(-2, 3, W)).astype(int)


def paper_rip(a, under, crack, peel):
    """斜めに裂け目が走り、上側が剥がれて落ちる。下から別の層が見える。"""
    top = _YY < RIP_Y[None, :]
    reach = int(W * min(1.0, crack))
    o = a.copy()
    if peel <= 0:
        xs = np.arange(reach)
        for d in range(2):                                   # 裂け目が左から走る
            o[np.clip(RIP_Y[xs] + d, 0, H - 1), xs] = (252, 250, 244)
        return o
    o[top] = under[top]
    edge = np.zeros((H, W), bool)
    for d in range(3):
        edge[np.clip(RIP_Y + d, 0, H - 1), np.arange(W)] = True
    o[edge & ~top] = (252, 250, 244)
    piece = np.dstack([a, (top * 255).astype(np.uint8)])
    for d in range(1, 3):
        piece[np.clip(RIP_Y - d, 0, H - 1), np.arange(W), :3] = (236, 232, 224)
    ang = 28 * peel ** 1.6
    im = Image.fromarray(piece).rotate(-ang, resample=Image.NEAREST, center=(W, int(RIP_Y[-1])),
                                       translate=(int(-30 * peel), int(760 * peel ** 2.2)))
    base = Image.fromarray(o).convert('RGBA')
    base.alpha_composite(im)
    return np.asarray(base.convert('RGB')).copy()


# ---- 破壊5「全壊」の型
TW, TH, TC, TR = 60, 64, 6, 10
_tp = np.random.default_rng(31)
PERM = _tp.permutation(TC * TR)
TROT = _tp.integers(0, 4, TC * TR)
TDELAY = _tp.random(TC * TR) * 0.5
TMISS = _tp.random(TC * TR) < 0.12


def puzzle(A, B, u):
    """画面が6×10のタイルに割れて、スライドパズルのように入れ替わる。抜けた所からBが見える。"""
    o = B.copy()
    for i in range(TC * TR):
        c, rw = i % TC, i // TC
        tc, tr = PERM[i] % TC, PERM[i] // TC
        p = smooth((u - TDELAY[i]) / 0.4)
        if TMISS[i] and p > 0.3:
            continue
        tile_ = A[rw * TH:(rw + 1) * TH, c * TW:(c + 1) * TW]
        if p > 0.5:
            tile_ = [tile_, tile_[:, ::-1], tile_[::-1], tile_[::-1, ::-1]][TROT[i]]
        x = int(c * TW + (tc - c) * TW * p); y = int(rw * TH + (tr - rw) * TH * p)
        o[y:y + TH, x:x + TW] = tile_[:H - y, :W - x]
    return o


def raster(a, t, u):
    """1行ごとに横へ波打つ（ラスタースクロール）。色の版ごとに位相がずれ、後半は斜めに流れる。"""
    o = np.empty_like(a)
    amp = 4 + 70 * u
    y = np.arange(H)
    for ci, ph in enumerate((0.5, 0.0, -0.5)):
        sh = (amp * np.sin(2 * np.pi * (y / 90 + t * 1.8) + ph) + (0.5 * u * (y - 360) if u > 0.5 else 0)).astype(int)
        xs = (np.arange(W)[None, :] - sh[:, None]) % W
        o[..., ci] = a[..., ci][y[:, None], xs]
    return o


GMASK = np.array([GL[i] for i in range(NPSEUDO)])
_dens = GMASK.reshape(NPSEUDO, -1).mean(1)
DENS_ORDER = np.argsort(_dens)


def textize(a, wipe):
    """画面を疑似文字のモザイクに置き換える。上から拭うように文字になっていく。"""
    rows, cols = H // G, W // G
    sub = a[:rows * G, :cols * G].reshape(rows, G, cols, G, 3).astype(float)
    col = sub.mean((1, 3))
    lum = col.mean(2) / 255
    gid = DENS_ORDER[np.clip(((1 - lum) * 1.6 * (NPSEUDO - 1)).astype(int), 0, NPSEUDO - 1)]
    gid = np.where(lum > 0.86, DENS_ORDER[0], gid)
    m = GMASK[gid]                                           # rows, cols, G, G
    ink = np.clip(col * 0.72, 0, 255)
    paper_c = np.array([246, 240, 230.])
    blk = np.where(m[..., None], ink[:, :, None, None, :], paper_c)
    txt = blk.transpose(0, 2, 1, 3, 4).reshape(rows * G, cols * G, 3).astype(np.uint8)
    o = a.copy()
    wy = int(min(rows * G, wipe))
    o[:wy, :cols * G] = txt[:wy]
    if wy < rows * G:
        o[wy:wy + 2] = (20, 16, 26)
    return o


def kaleido(A, B, t):
    """折り返し。左右、四方、目の壁紙を12fpsで切り替える。"""
    k = int(t * 12) % 3
    if k == 0:
        o = A.copy(); o[:, 180:] = B[:, :180][:, ::-1]
    elif k == 1:
        q = A[CY_M - 180:CY_M, 0:180]
        top = np.concatenate([q, q[:, ::-1]], 1)
        blk = np.concatenate([top, top[::-1]], 0)
        o = np.tile(blk, (2, 1, 1))[:H]
        o = np.roll(o, CY_M - 180, 0)
    else:
        e = A[SEL[1]:SEL[3], SEL[0]:SEL[2]]
        row = np.concatenate([e, e[:, ::-1]], 1)
        blk = np.concatenate([row, row[::-1]], 0)
        o = np.tile(blk, (H // blk.shape[0] + 1, W // blk.shape[1] + 1, 1))[:H, :W].copy()
        off = int(t * 60) % blk.shape[1]
        o = np.roll(o, off, 1)
    return o


CY_M = 360


def tunnel(A, B, u):
    """画面の中に画面が入れ子になって、無限に吸い込まれる。AとBが交互に出る。"""
    s = 0.64
    img = A.copy()
    layers = [A, B]
    for k in range(9, 0, -1):
        sc = s ** k
        w, h = max(2, int(W * sc)), max(2, int(H * sc))
        src = layers[k % 2]
        sm = np.asarray(Image.fromarray(src).resize((w, h), Image.NEAREST))
        x0, y0 = 180 - w // 2, CY_M - h * 360 // H
        y0 = max(0, min(H - h, y0)); x0 = max(0, min(W - w, x0))
        img[y0:y0 + h, x0:x0 + w] = sm
        for d in range(2 if w > 40 else 1):                 # 入れ子の縁
            img[y0 + d, x0:x0 + w] = FRAME; img[y0 + h - 1 - d, x0:x0 + w] = FRAME
            img[y0:y0 + h, x0 + d] = FRAME; img[y0:y0 + h, x0 + w - 1 - d] = FRAME
    z = (1 / s) ** ((u * 2.5) % 1.0)
    return zoom(img, z, 180, CY_M)


def vhs(a, rs, strength=1.0, rewind=False):
    """ビデオテープ：トラッキングの乱れの帯、下端のヘッド切り替え、早戻しの記号。"""
    o = a.copy()
    for _ in range(2 if rewind else 1):
        y0, h = int(rs.integers(0, H - 30)), int(rs.integers(6, 18))
        nz = rs.integers(90, 250, (h, W, 1)).repeat(3, 2).astype(np.uint8)
        keep = rs.random((h, W)) < 0.45
        o[y0:y0 + h][keep] = nz[keep]
        o[y0:y0 + h] = np.roll(o[y0:y0 + h], int(rs.integers(-40, 40) * strength), 1)
    o[H - 22:] = np.roll(o[H - 22:], int(24 * strength), 1)
    if rewind:
        for k in range(2):
            x = 22 + k * 30
            ImageDraw.Draw(im := Image.fromarray(o)).polygon([(x + 28, 30), (x + 28, 70), (x, 50)], fill=(255, 255, 255))
            o = np.asarray(im).copy()
    return o


def melt_fx(a, amount):
    """溶け落ち：列ごとに違う量だけ下へ垂れる。"""
    d = (amount * (0.25 + DRIP)).astype(int)
    ys = np.clip(np.arange(H)[:, None] - d[None, :], 0, H - 1)
    return a[ys, np.arange(W)[None, :]]


_dr = np.random.default_rng(88)
DRIP = np.asarray(Image.fromarray((_dr.random((1, 24)) * 255).astype(np.uint8)).resize((W, 1), Image.BICUBIC), float)[0] / 255
DRIP = DRIP ** 2


def pixel_sort(a, y0, y1, x0, x1):
    s = a[y0:y1, x0:x1]
    idx = np.argsort(s.astype(int).sum(2), axis=1)
    a[y0:y1, x0:x1] = np.take_along_axis(s, idx[..., None], 1)
    return a


PALS = [PU, MG, CY, YG, RD, BLUE, PINK]


def palette_cycle(a, t):
    """紙以外を4段階に減色し、色を12fpsで巡回させる（紙の色は固定して明滅させない）。"""
    af = a.astype(int)
    paper = np.abs(af - np.array(PAPER)).sum(2) < 42
    lum = af.mean(2)
    lv = np.clip(((lum - 60) / 200 * 4).astype(int), 0, 3)
    k = int(t * 12)
    pal = np.array([PALS[(k + i) % len(PALS)] for i in range(4)], np.uint8)
    o = pal[lv]
    o[paper] = a[paper]
    o[lum < 90] = FRAME
    return o


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

SWAY_AMP = {'tear': 7, 'breakdown': 6, 'climax': 4}


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
SWARM = [(SPAWN_T0 + i * 0.06, int(_sw.integers(-60, 200)), int(_sw.integers(40, 500)), int(_sw.integers(0, 4)),
          (float(_sw.uniform(-70, 70)), float(_sw.uniform(-60, 60)))) for i in range(20)]
SWARM_END = 6.8


def windows(t, prog):
    name, *_ = seg(t)
    wins = [(64, 96, 0, 1.0, prog)]
    if name in ('swap', 'half') or t >= T_RET:
        wins.append((82, 118, 0, 1.0, progress_front(t)))
    elif name == 'ui':
        for (ts, x, y, sty, v) in SWARM:
            if t >= ts:
                for k in (3, 2, 1, 0):                      # 残像の尾を引いて漂う
                    tt = max(ts, t - k * 0.05)
                    wins.append((x + v[0] * (tt - ts), y + v[1] * (tt - ts), sty, [1, .45, .28, .15][k], prog))
    elif name in ('breakdown', 'freeze', 'paper', 'climax', 'collapse'):
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
    st['edx'] = look(t - 1 / 12) + (swp - sw) + (2 if t >= T_RET else 4 if name == 'freeze' else 0)   # 目だけ1コマ遅れる
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


def render(st, variant=0, show_windows=True, icon=None, show_mascot=True):
    img = Image.fromarray(PAPERS[st['pidx']]).convert('RGBA')

    def draw_win(w):
        x, y, sty, a, pg = w
        im = make_window(pg, tuple(st['l1']), tuple(st['l2']), sty)
        img.alpha_composite(with_alpha(im, a), (int(x), int(y)))
    if show_windows: draw_win(st['wins'][0])
    m = mascot(st['scale'], st['edx'], st['edy'], variant, st['blink'])
    mx, my = MX0 + st['sway'], MY0
    if show_mascot:
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


def clean(t, variant=0):
    st = state(t); st['sway'] = 0
    return render(st, variant=variant)[0]


# ---------------------------------------------------------------- 場面
def hook(k):
    """15秒版のフック：反転してマゼンタとシアンに分離。巨大な疑似文字。"""
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
    """破壊1「裂け」：15秒版の裂け目。顔が裂けてBが覗き、裂け目が画面全体に走る。写真が突き刺さる。"""
    u = (t - t0) / (t1 - t0)
    A, mx, my, mm = render(st)
    B, *_ = render(st, variant=1)
    fy0 = my + 100 if u < 0.35 else my + 20
    a = slices(A, B, rs, int(6 + 18 * u), int(14 + 50 * u), fy0, my + 180, mx, mx + MC)
    if u > 0.4:
        a = slices(a, B, rs, int(2 + 10 * u), int(20 + 60 * u), 0, H)
    if t >= 2.55:
        put_tile(a, ANOM_TILES[int((t - 2.55) * 6) % 6], IX, IY)
    if 2.6 <= t < 2.6 + 1 / FPS:
        y = 300; a[y:y + 6] = YG; a[y + 6:y + 9] = np.roll(a[y + 6:y + 9], 12, 1)
    for te, nf in ERRORS:
        if te <= t < te + nf / FPS:
            a = with_error(a, [150, 40][ERRORS.index((te, nf))], [205, 430][ERRORS.index((te, nf))], np.random.default_rng(int(te * 10)))
    if int(t * 10) % 4 == 0:
        dx = int(r.integers(3, 8))
        a = bg_only(a, lambda z: rgb_shift(z, dx), mm, mx, my)
        for _ in range(2):
            y = int(r.integers(0, H)); a[y:y + 1] = CY if r.random() < .5 else MG
    if u > 0.8:
        a = np.roll(a, (int(r.integers(-2, 3)), int(r.integers(-2, 3))), (0, 1)); a = rgb_shift(a, 2)
    return a


def sc_calm2(t, st, r, rs, t0, t1):
    a, *_ = render(st, icon=ANOM_TILES[-1])
    if t >= t1 - 0.15: a = omen(a, r)
    return a


@lru_cache(maxsize=1)
def eye_patch():
    a = clean(CUT_T - 0.01)
    return a[SEL[1]:SEL[3], SEL[0]:SEL[2]].copy()


def sc_ui(t, st, r, rs, t0, t1):
    """破壊2「操作」：カーソルが目を範囲選択して切り取り、画面中に貼り散らかす。
    窓が湧き、エラーが連鎖し、目のない本体が飛び出して跳ね回り、軌跡が画面に残る。"""
    bouncing = t >= BOUNCE_T0
    a, mx, my, mm = render(st, show_mascot=not bouncing)
    if t >= CUT_T and not bouncing:                         # 切り取られた穴から紙が見える
        a[SEL[1]:SEL[3], SEL[0]:SEL[2]] = PAPERS[st['pidx']][SEL[1]:SEL[3], SEL[0]:SEL[2]]
    ep = eye_patch()
    ph, pw = ep.shape[:2]
    for k, ts in enumerate(PASTE_TS):                       # 貼り付けた目は消えない
        if t >= ts:
            x, y = PASTES[k]
            x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + pw), min(H, y + ph)
            a[y0:y1, x0:x1] = ep[y0 - y:y1 - y, x0 - x:x1 - x]
    if SEL_T[0] <= t < CUT_T + 0.1:
        cx, cy = cursor_pos(min(t, SEL_T[1] - 1e-6))
        marching_ants(a, SEL[0], SEL[1], max(SEL[0] + 2, cx), max(SEL[1] + 2, cy), int(t * FPS))
    if CASCADE0 <= t:                                        # エラーの連鎖
        n = int((t - CASCADE0) * 30)
        im = Image.fromarray(a)
        for i in range(max(0, n - 40), n):
            error_box(im, 6 + (i % 24) * 9, 70 + (i % 24) * 14 + (i // 24) * 26, np.random.default_rng(i))
        a = np.asarray(im).copy()
    if bouncing:                                            # 跳ねる本体と、消えない軌跡
        spr = faceless()
        im = Image.fromarray(a).convert('RGBA')
        for (x, y) in BOUNCE[:int((t - BOUNCE_T0) * FPS) + 1]:
            im.alpha_composite(spr, (x, y)) if 0 <= x and 0 <= y else im.paste(spr, (x, y), spr)
        a = np.asarray(im.convert('RGB')).copy()
    if t < PASTE_T[1] + 0.5:
        draw_cursor(a, *cursor_pos(t))
    for ts in PASTE_TS:                                      # 貼り付けの瞬間だけ1pxずれる
        if ts <= t < ts + 1 / FPS:
            a = rgb_shift(a, 2)
    return a


def sc_swap(t, st, r, rs, t0, t1):
    a, *_ = render(st, variant=1)
    return a


CLOSEUPS = [(4.2, 158, 356, 0), (3.0, 120, 140, 0), (3.2, 180, 562, 0), (4.4, 180, 350, 1)]


def fx_breakdown(t, st, r, rs, u, photos=True):
    """15秒版の暴走（8–11秒）を下敷きにしたもの。乱数は12fpsで保持し、画面が毎コマ入れ替わりすぎないようにする。"""
    st = dict(st)
    st['wins'] = [(w[0] + int(rs.integers(-6, 7)), w[1] + int(rs.integers(-6, 7)), w[2], w[3], [0.98, 0.13, 1.0, 0.61][int(rs.integers(4))])
                  for w in st['wins']]
    A, mx, my, mm = render(st)
    B, *_ = render(st, variant=1)
    a = slices(A, B, rs, int(6 + 12 * u), int(18 + 40 * u), my, my + MC, 0, W)
    put_tile(a, 2)
    z = 1 + (0.45 + 0.25 * u) * abs(math.sin(math.pi * (t - 7.6) / 1.5))
    a = zoom(a, z, 180 + int(rs.integers(-3, 4)), 330)
    if photos:                                               # 写真は1/3秒単位で切り替える
        fl = np.random.default_rng(700 + int(t * 3))
        if fl.random() < 0.6:
            full = frag_full(int(fl.integers(NFRAG)), int(fl.integers(4)))
            if fl.random() < 0.45: a = full.copy()
            else:
                x0 = int(fl.integers(0, 160)); y0 = int(fl.integers(0, 380))
                a[y0:y0 + 260, x0:x0 + 200] = full[y0:y0 + 260, x0:x0 + 200]
    a = block_glitch(a, rs, int(16 + 40 * u))
    for _ in range(int(rs.integers(2, 6))):
        y = int(rs.integers(0, H - 40)); h = int(rs.integers(6, 40))
        a[y:y + h] = a[y:y + h][::-1] if rs.random() < .6 else a[y:y + h, ::-1]
    if rs.random() < 0.2:
        y0 = int(rs.integers(0, H - 120)); a = crush_region(a, y0, y0 + int(rs.integers(40, 120)), int(rs.integers(4, 12)))
    cm = np.random.default_rng(900 + int(t * 3)).random()     # 色の型も1/3秒単位
    if cm < 0.2: a = duotone(a, DUO_DARK, DUO_LIGHT)
    elif cm < 0.3: a = duotone(a, (40, 90, 150), (210, 255, 248))
    elif cm < 0.7: a = rgb_shift(a, int(rs.integers(4, 10 + int(10 * u))))
    elif cm < 0.8:
        y0 = int(rs.integers(0, H - 120)); a[y0:y0 + 120] = 255 - a[y0:y0 + 120]
    im = Image.fromarray(a)
    for _ in range(int(3 + 10 * u)):
        c = [(255, 255, 255), (12, 8, 20), RD, PU, CY, MG, YG][int(rs.integers(7))]
        stamp(im, rs.integers(-10, 330), rs.integers(0, 610), int(rs.integers(NPSEUDO)), c, scale=int(rs.integers(2, 6)))
    if rs.random() < 0.25 + 0.3 * u: error_box(im, int(rs.integers(0, 230)), int(rs.integers(40, 560)), rs)
    a = np.asarray(im).copy()
    if t >= 11.0: a = np.roll(rgb_shift(a, 14), int(r.integers(-8, 9)), 0)
    return a


def sc_breakdown(t, st, r, rs, t0, t1):
    """破壊3「暴走」：15秒版の暴走。0.5秒ごとに静かなクローズアップへ2コマだけジャンプカットする。"""
    if in_quiet(t):
        return rgb_shift(render(st, variant=1, show_windows=False)[0], 1)
    if is_closeup(t):
        z, cx, cy, v = CLOSEUPS[int(t * 3) % len(CLOSEUPS)]
        return zoom(clean(t, v), z, cx, cy)
    return fx_breakdown(t, st, r, rs, (t - t0) / (t1 - t0))


def sc_freeze(t, st, r, rs, t0, t1):
    """止まる。ただし窓も写真も化けた文字も残ったまま。目は4pxずれる。"""
    a, *_ = render(st, icon=ANOM_TILES[-1])
    return a


@lru_cache(maxsize=4)
def _paper_base(pidx):
    st = state(12.0); st['pidx'] = pidx
    return render(st)[0]


def sc_paper(t, st, r, rs, t0, t1):
    """破壊4「紙」：画面が印刷物になって版がずれ、スキャナーで読まれながら滑り、最後は紙ごと破れて剥がれ落ちる。
    網点は細かい模様なので、紙の粒のちらつきを止めて毎コマの変化を減らす（圧縮で潰れにくくする）。"""
    st = dict(st, pidx=0)
    base = render(st)[0]
    if t < PAPER_PH[0][1]:
        u = (t - 12.0) / 0.9
        m = 1 + 9 * u
        tq = int(t * 12)
        jit = [(0, 0), (int(m), int(m * 0.5)), (-int(m * 0.8), int(m * 0.7))]
        return halftone(base, 4 + int(u * 6), jit)
    printed = halftone(base, 10, [(0, 0), (10, 5), (-8, 7)])
    if t < PAPER_PH[1][1]:
        u = (t - 12.9) / 0.9
        bar = H * min(1.0, u / 0.8) if u < 0.8 else H * (1 - (u - 0.8) / 0.2)
        return scan_slip(printed, bar if u < 0.8 else H, 2 + 6 * u)
    scanned = scan_slip(printed, H, 6.8)
    u = (t - 13.8) / 1.2
    under = duotone(render(st, variant=1)[0], (26, 22, 58), BLUE)
    crack = min(1.0, u / 0.25)
    peel = max(0.0, (u - 0.3) / 0.7)
    o = paper_rip(scanned, under, crack, peel)
    if u < 0.3:
        o = np.roll(o, (int(r.integers(-2, 3)), int(r.integers(-2, 3))), (0, 1))
    return o


def sc_half(t, st, r, rs, t0, t1):
    """左半分がA、右半分がB。静かに座っている。"""
    A, mx, my, mm = render(st)
    B, *_ = render(st, variant=1)
    A[my:my + MC, 180:mx + MC] = B[my:my + MC, 180:mx + MC]
    return A


def climax_mode_frame(mode, t, st, r, rs, u):
    A, mx, my, mm = render(st)
    if mode == 'puzzle':
        return puzzle(A, render(st, variant=1)[0], u)
    if mode == 'raster':
        return raster(A, t, u)
    if mode == 'textize':
        return textize(A, H * min(1.0, u * 1.4))
    if mode == 'kaleido':
        return kaleido(A, render(st, variant=1)[0], t)
    if mode == 'tunnel':
        return tunnel(A, render(st, variant=1)[0], u)
    if mode == 'rewind':                                     # 暴走を逆再生（写真は出さない）
        ts = 11.55 - u * 3.9
        src = fx_breakdown(ts, state(ts), np.random.default_rng(int(ts * 30) * 17 + 3),
                           np.random.default_rng(int(ts * 12) * 11 + 1), (ts - 7.6) / 4.0, photos=False)
        return vhs(src, rs, 1.0, rewind=True)
    if mode == 'melt':
        B, *_ = render(st, variant=1)
        A[my + 30:my + 170:2, 180:mx + MC] = 255 - B[my + 30:my + 170:2, 180:mx + MC]
        a = melt_fx(A, 30 + 300 * u ** 1.3)
        for _ in range(int(2 + 6 * u)):
            y0 = int(rs.integers(0, H - 40)); x0 = int(rs.integers(0, W // 2))
            a = pixel_sort(a, y0, y0 + int(rs.integers(10, 60)), x0, min(W, x0 + int(rs.integers(80, 300))))
        return a
    if mode == 'palette':
        return palette_cycle(A, t)
    return A


def sc_climax(t, st, r, rs, t0, t1):
    """破壊5「全壊」：新しい型が1つずつ出て、後半は短いカットで一巡し、最後は型ごとのタイルが並ぶ。"""
    if in_quiet(t):
        return render(st, variant=1, show_windows=False)[0]
    if is_snap(t):
        return render(dict(st, sway=0))[0]
    m0, m1, mode = climax_mode(t)
    u = (t - m0) / (m1 - m0)
    if mode != 'grid':
        if m1 - m0 < 0.2:                                   # 一巡の短いカットは型の見せ場から始める
            u = 0.5 + 0.5 * u
        return climax_mode_frame(mode, t, st, r, rs, u)
    # 最後：画面を3×5のタイルに割り、タイルごとに違う型を映す。12fpsで型が入れ替わる
    frames = {m: climax_mode_frame(m, t, st, r, rs, 0.6 + 0.4 * u) for m in CLIMAX_MODES}
    o = np.empty((H, W, 3), np.uint8)
    k = int(t * 12)
    for i in range(15):
        c, rw = i % 3, i // 3
        m = CLIMAX_MODES[(i * 5 + k) % 8]
        o[rw * 128:(rw + 1) * 128, c * 120:(c + 1) * 120] = frames[m][rw * 128:(rw + 1) * 128, c * 120:(c + 1) * 120]
    o[::128] = FRAME
    o[:, ::120] = FRAME
    return o


def sc_collapse(t, st, r, rs, t0, t1):
    """崩落：解像度が段階的に落ちていき、最後の1点が無音の間のマスコットになる。"""
    u = (t - t0) / (t1 - t0)
    src = sc_climax(21.45, state(21.45), r, rs, 16.0, 21.5)
    levels = [2, 3, 4, 6, 8, 12, 18, 24, 36, 60, 90, 180]
    if u < 0.8:
        bs = levels[min(len(levels) - 1, int(u / 0.8 * len(levels)))]
        a = mosaic(src, bs).astype(float)
        wv = smooth(u / 0.8)
        return (a * (1 - wv) + PAPER_WHITE.astype(float) * wv).astype(np.uint8)
    a = PAPER_WHITE.copy()
    a[CY_M - 2:CY_M + 2, 178:182] = PINK
    return a


@lru_cache(maxsize=2)
def _silence(blink):
    img = Image.fromarray(PAPER_WHITE).convert('RGBA')
    img.alpha_composite(mascot(0.42, 0, 0, 0, blink), (80, 252))
    return np.asarray(img.convert('RGB')).copy()


def sc_silence(t, st, r, rs, t0, t1):
    return _silence(blinking(t)).copy()


def sc_ret(t, st, r, rs, t0, t1):
    """冒頭と同じ画面。ただし窓2枚、目が2px横、5文字目が化けたまま。途中で一度だけ、テープの乱れのような余震。"""
    a, mx, my, mm = render(st)
    if AFTERSHOCK[0] <= t < AFTERSHOCK[1]:
        return vhs(np.roll(a, int(rs.integers(-60, -20)), 0), rs, 1.4)
    if t >= 29.4:
        a = omen(a, r)
        if 29.6 <= t < 29.6 + 1 / FPS:                    # Bが1フレームだけ覗く
            B, *_ = render(st, variant=1)
            a[my + 96:my + 112, mx:mx + MC] = B[my + 96:my + 112, mx:mx + MC]
    return a


SCENES = {'calm1': sc_calm1, 'tear': sc_tear, 'calm2': sc_calm2, 'ui': sc_ui, 'swap': sc_swap,
          'breakdown': sc_breakdown, 'freeze': sc_freeze, 'paper': sc_paper, 'half': sc_half, 'climax': sc_climax,
          'collapse': sc_collapse, 'silence': sc_silence, 'ret': sc_ret}


def frame(f):
    CUR[0] = f
    if f < 3: return hook(f)
    if f >= NF - 3: return hook(f - (NF - 3))
    t = f / FPS
    name, t0, t1 = seg(t)
    st = state(t)
    if name == 'tear' and is_snap(t):                      # 破壊の途中で一瞬だけ静に戻る
        return render(dict(st, sway=0))[0]
    r = np.random.default_rng(f * 17 + 3)
    rs = np.random.default_rng(int(t * 12) * 11 + 1)      # 12fps相当で保持する乱数
    return SCENES[name](t, st, r, rs, t0, t1)


# ---------------------------------------------------------------- 仕上げ（15秒版と同じ）
yy_ = np.arange(H * UP)
SCAN = np.where(yy_ % UP == UP - 1, 0.84, 1.0).astype(np.float32)[:, None, None]
vy_ = np.linspace(-1, 1, H * UP)[:, None]; vx_ = np.linspace(-1, 1, W * UP)[None, :]
VIG = (1 - 0.16 * (vx_ ** 2 * 0.8 + vy_ ** 2 * 0.5)).astype(np.float32)[..., None]
MULT = SCAN * VIG


def finish(a):
    up = a.repeat(UP, 0).repeat(UP, 1)
    o = up.astype(np.float32)
    o[..., 0] = np.roll(o[..., 0], 1, 1); o[..., 2] = np.roll(o[..., 2], -1, 1)
    o *= MULT
    return np.clip(o, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- 音
def audio(path):
    n = int(SR * DUR)
    tt = np.arange(n) / SR
    rng = np.random.default_rng(5)
    L, R = np.zeros(n), np.zeros(n)            # 静の層（ハム、クリック）
    DL, DR = np.zeros(n), np.zeros(n)          # 破壊の層（破壊中だけ鳴り、静に戻る瞬間に切れる）
    S, D = (L, R), (DL, DR)

    def add(buf, t0, sig, pan=0.0, g=1.0):
        bl, br = buf
        i = int(t0 * SR)
        if i >= n or i < 0 or len(sig) == 0:
            return
        s = sig[:n - i] * g
        bl[i:i + len(s)] += s * (1 - pan) / 2 * 2 ** .5
        br[i:i + len(s)] += s * (1 + pan) / 2 * 2 ** .5

    def ks(d): return np.arange(int(d * SR)) / SR

    def add2(t0, l_, r_, g=1.0):
        i = int(t0 * SR)
        if i >= n: return
        m_ = min(len(l_), n - i)
        DL[i:i + m_] += l_[:m_] * g; DR[i:i + m_] += r_[:m_] * g

    # 15秒版の音の部品
    def crushed(dur, g=0.5, seed=0):
        r = np.random.default_rng(seed); m = int(dur * SR)
        x = r.normal(0, 1, m // 24 + 1).repeat(24)[:m]
        sq = np.sign(np.sin(2 * np.pi * r.uniform(80, 900) * np.arange(m) / SR))
        return np.round((x * 0.6 + sq * 0.5) * 4) / 4 * g

    def blip(freq, dur=0.04, g=0.25, f2=None, decay=60):
        m = int(dur * SR); k = np.arange(m) / SR
        fr = freq if f2 is None else np.linspace(freq, f2, m)
        return np.sin(2 * np.pi * np.cumsum(np.broadcast_to(fr, (m,))) / SR) * np.exp(-k * decay) * g

    def sq(f, d, g=1.0):
        return np.sign(np.sin(2 * np.pi * f * ks(d))) * g

    def noise(d, g=1.0, hold=1):
        m = int(d * SR)
        return rng.uniform(-1, 1, m // hold + 1).repeat(hold)[:m] * g

    # 静の層：ハム（15秒版と同じ）。入れ替わりと半分の場面では低く濁る
    names = [seg(x)[0] for x in np.arange(int(DUR * 120)) / 120]
    f = np.array([55.0 if nm == 'swap' else 52.0 if nm == 'half' else 60.0 for nm in names]).repeat(SR // 120)[:n]
    ph = 2 * np.pi * np.cumsum(f) / SR
    hiss = np.convolve(rng.normal(0, 1, n), np.ones(40) / 40, 'same')
    hum = 0.018 * np.sin(ph) + 0.008 * np.sin(2 * ph) + 0.006 * hiss
    mute = np.array([nm in ('silence', 'collapse') for nm in names], float).repeat(SR // 120)[:n]
    hum *= (1 - mute) * (np.clip((tt - T_RET) / 0.03, 0, 1) * (tt >= T_RET) + (tt < T_RET))
    omen_ = ((tt >= 2.1) & (tt < 2.4)) | ((tt >= 4.25) & (tt < 4.4)) | ((tt >= 29.4) & (tt < T_LOOP))
    hum = np.where(omen_, np.clip(hum * 4, -0.04, 0.04), hum)
    L += hum; R += hum
    add(S, 0.0, crushed(0.1, 0.55, 1)); add(S, T_LOOP, crushed(0.1, 0.55, 1))
    for ts, _ in PROG:
        add(S, ts, blip(3200, 0.006, 0.12), pan=0.3)
    add(S, 7.2, blip(3200, 0.006, 0.1))

    # 破壊1「裂け」：15秒版の裂ける音
    add(D, 2.4, blip(140, 0.25, 0.7, 40, decay=10))
    for k in range(int(1.2 * 12)):
        ts = 2.4 + k / 12
        if rng.random() < 0.4 + 0.5 * k / 14:
            add(D, ts, crushed(0.03, 0.15 + 0.2 * k / 14, 100 + k), rng.uniform(-.6, .6))
    add(D, 2.6, blip(5200, 0.035, 0.18, 2600), 0.5)
    for te, _ in ERRORS:
        add(D, te, np.concatenate([blip(880, 0.06, 0.2), blip(660, 0.06, 0.2)]))
    for k in range(10):
        ts = 3.1 + k * 0.05
        add(D, ts, crushed(0.05, 0.25, 150 + k), rng.uniform(-.8, .8))

    # 破壊2「操作」：クリック、切り取り、貼り付け、窓、エラー、跳ねる音
    tick = noise(0.003, 0.5)
    for ts in (SEL_T[0], SEL_T[0] + 0.03, CUT_T, CUT_T + 0.03):
        add(D, ts, tick, 0.2)
    add(D, CUT_T + 0.02, noise(0.08, 0.35, 3) * np.linspace(1, 0, int(0.08 * SR)), -0.3)
    for k, ts in enumerate(PASTE_TS):
        x, _ = PASTES[k]
        add(D, ts, blip(320, 0.05, 0.4, 110, decay=45), pan=(x - 140) / 180)
        add(D, ts, tick, pan=(x - 140) / 180)
    for (ts, x, y, sty, v) in SWARM:
        add(D, ts, blip(2400, 0.07, 0.16, 700), pan=(x - 70) / 200)
    for i in range(0, int((6.8 - CASCADE0) * 30), 3):
        add(D, CASCADE0 + i / 30, np.concatenate([blip(880, 0.04, 0.12), blip(660, 0.04, 0.12)]), pan=((i % 24) - 12) / 14)
    for k in range(1, len(BOUNCE)):
        ts = BOUNCE_T0 + k / FPS
        if BOUNCE[k][1] == 430 and BOUNCE[k - 1][1] != 430:
            add(D, ts, blip(260, 0.12, 0.45, 780, decay=18), pan=(BOUNCE[k][0] - 80) / 150)
        add(D, ts, blip(4200, 0.008, 0.05), pan=(BOUNCE[k][0] - 80) / 150)

    # 破壊3「暴走」：15秒版の0.1秒グリッドのスタッターと48Hz
    for k in range(40):
        ts = 7.6 + k * 0.1; p = k / 39; g = 0.22 + 0.3 * p
        q = rng.random()
        if q < 0.6: add(D, ts, crushed(0.1 if rng.random() < .5 else 0.066, g, 200 + k), rng.uniform(-.8, .8))
        elif q < 0.9: add(D, ts, sq(rng.uniform(90, 1800), 0.1, g * 0.6), rng.uniform(-.8, .8))
    m = int(4 * SR); k4 = np.arange(m) / SR
    add(D, 7.6, np.sin(2 * np.pi * 48 * k4) * (0.25 * np.abs(np.sin(np.pi * k4 / 1.5))))

    # 破壊4「紙」：網点の干渉音 → スキャナーのモーター → 紙の破れ
    d = 0.9; k_ = ks(d)
    add(D, 12.0, np.sin(2 * np.pi * (1500 * k_ + 12 * np.sin(2 * np.pi * (30 + 60 * k_) * k_))) * 0.2 * (0.4 + k_ / d)
        + np.sin(2 * np.pi * 1500 * (1 + 0.02 * k_ / d) * k_) * 0.12)
    k_ = ks(0.9)
    motor = (2 * ((110 * (1 + 0.3 * k_) * k_) % 1) - 1) * 0.18 + np.sin(2 * np.pi * 330 * k_) * 0.06
    add(D, 12.9, motor * np.where(k_ < 0.72, 1.0, 0.5))
    for j in range(int(0.72 * 50)):
        add(D, 12.9 + j / 50, noise(0.002, 0.25))
    add(D, 12.9 + 0.72, noise(0.18, 0.2, 2) * np.linspace(1, 0, int(0.18 * SR)))
    for ts in np.arange(13.8, 14.6, 1 / 900):              # 裂ける音：密度が上がって、剥がれる
        dd = (ts - 13.8) / 0.8
        if rng.random() < 0.15 + 0.6 * min(1, dd * 2) * (1 - max(0, dd - 0.7) * 3):
            add(D, ts, rng.uniform(-1, 1, 48) * np.exp(-np.arange(48) / 10), pan=rng.uniform(-0.5, 0.5), g=rng.uniform(0.1, 0.4))
    add(D, 14.85, blip(70, 0.25, 0.7, 40, decay=12))

    # 破壊5「全壊」：型ごとに違う音
    base_bd = (DL[int(7.6 * SR):int(11.55 * SR)].copy(), DR[int(7.6 * SR):int(11.55 * SR)].copy())

    def mode_sound(mode, dur, seed):
        rr = np.random.default_rng(seed); k = ks(dur); m = len(k)
        if mode == 'puzzle':
            o = np.zeros(m)
            for _ in range(max(2, int(dur * 22))):
                i = int(rr.integers(0, max(1, m - 800)))
                c = blip(float(rr.uniform(700, 1500)), 0.015, 0.5, decay=260)
                o[i:i + len(c)] += c[:m - i]
            return o, o
        if mode == 'raster':
            v = np.sin(2 * np.pi * (220 * k + (2 + 30 * k / dur) * np.sin(2 * np.pi * 6 * k))) * 0.3
            return v, np.roll(v, 240)
        if mode == 'textize':
            o = np.zeros(m)
            for i in range(0, m, int(SR / 26)):
                o[i:i + 120] += rr.uniform(-1, 1, 120)[:m - i] * np.exp(-np.arange(120) / 25)[:m - i] * 0.4
            if dur > 0.3:
                b = blip(2000, 0.2, 0.3, decay=12); o[-len(b):] += b[:m]
            return o, o
        if mode == 'kaleido':
            l_, r_ = np.zeros(m), np.zeros(m)
            notes = [392, 440, 523, 587, 659, 784]
            step = int(SR / 12)
            for j, i in enumerate(range(0, m, step)):
                s_ = np.sign(np.sin(2 * np.pi * notes[(j * 2) % 6] * np.arange(min(step, m - i)) / SR)) * 0.25
                (l_ if j % 2 else r_)[i:i + len(s_)] += s_
            return l_, r_
        if mode == 'tunnel':
            o = np.zeros(m)
            for oc in (110, 220, 440, 880):
                fq = oc * 2 ** (-k / max(dur, 0.2))
                w = np.exp(-((np.log2(fq / 300)) ** 2) * 1.2)
                o += np.sin(2 * np.pi * np.cumsum(fq) / SR) * w * 0.3
            return o, o
        if mode == 'rewind':
            bl, br = base_bd
            idx = np.linspace(len(bl) - 1, 0, m)
            hs = noise(dur, 0.05)
            return np.interp(idx, np.arange(len(bl)), bl) + hs, np.interp(idx, np.arange(len(br)), br) + hs
        if mode == 'melt':
            fq = 180 * (35 / 180) ** (k / max(dur, 0.2)) * (1 + 0.05 * np.sin(2 * np.pi * 5 * k))
            o = np.sign(np.sin(2 * np.pi * np.cumsum(fq) / SR)) * 0.3
            return o, o
        if mode == 'palette':
            notes = [523, 659, 784, 1046]
            idx_ = (k * 24).astype(int) % 4
            o = np.sign(np.sin(2 * np.pi * np.array(notes)[idx_] * k)) * 0.22
            return o, np.roll(o, 400)
        o = crushed(dur, 0.4, seed)
        return o, o

    for j, (m0, m1, mode) in enumerate(CLIMAX):
        dur = m1 - m0
        if mode == 'grid':
            for mm_ in CLIMAX_MODES:
                l_, r_ = mode_sound(mm_, dur, 700 + j)
                add2(m0, l_, r_, g=0.2)
            continue
        l_, r_ = mode_sound(mode, dur, 700 + j)
        fade = np.minimum(1, np.arange(len(l_)) / (0.004 * SR))
        add2(m0, l_ * fade, r_ * fade, g=0.7)
    mcl = (tt >= 16.0) & (tt < 21.5)
    pul = 0.2 * np.sin(2 * np.pi * 40 * tt) * (0.5 + 0.5 * np.sign(np.sin(2 * np.pi * 8 * tt))) * np.clip((tt - 16.0) / 5.5 + 0.3, 0, 1.3)
    DL[mcl] += pul[mcl]; DR[mcl] += pul[mcl]

    # 余震：テープの乱れ
    k_ = ks(0.1)
    add(D, AFTERSHOCK[0], noise(0.1, 0.3, 4) + np.sin(2 * np.pi * 60 * (1 + 0.3 * np.sin(2 * np.pi * 9 * k_)) * k_) * 0.3)

    # マスク：破壊の層は破壊中だけ鳴る（静に戻るコマ、B単独のコマ、ジャンプカットで切れる）
    slot_t = np.arange(int(DUR * 120)) / 120
    dm = np.array([destroying(x) for x in slot_t], float).repeat(SR // 120)[:n]
    L += DL * dm; R += DR * dm

    # 崩落：音も解像度が落ちていく（ビット深度とサンプル数が減って消える）
    d = 0.4; k_ = ks(d); m = len(k_)
    chord = sum(np.sin(2 * np.pi * fq * k_) for fq in (262, 330, 392, 523)) / 4
    out = np.zeros(m)
    for j in range(12):
        i0, i1 = int(m * j / 12), int(m * (j + 1) / 12)
        hold = 2 ** (j // 2 + 1); bits = max(1, 6 - j // 2)
        seg_ = chord[i0:i1][::hold].repeat(hold)[:i1 - i0]
        out[i0:i1] = np.round(seg_ * 2 ** bits) / 2 ** bits * (1 - j / 12)
    add(S, 21.5, out, g=0.35)

    z = ((tt >= 21.9) & (tt < T_RET)) | np.array([in_quiet(x) for x in slot_t]).repeat(SR // 120)[:n]
    L[z] = 0; R[z] = 0

    peak = max(np.abs(L).max(), np.abs(R).max())
    st = np.stack([L, R], 1) * (0.89 / peak)
    write_wav(path, st)


def write_wav(path, st):
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(st, -1, 1) * 32767).astype("<i2").tobytes())


def measure_lufs(path):
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", path, "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", out)[-1])


def limit(st, ceil=0.63, block=240):
    """簡単なリミッター：5msごとのピークから利得を決め、ブロック間を直線でつなぐ。"""
    n = len(st)
    pk = np.abs(st).max(1)
    nb = -(-n // block)
    env = np.pad(pk, (0, nb * block - n)).reshape(nb, block).max(1)
    env = np.maximum(env, np.r_[env[1:], 0])                 # 次のブロックの山にも先回りする
    g = np.minimum(1.0, ceil / np.maximum(env, 1e-9))
    g = np.minimum(g, np.r_[1.0, g[:-1]])
    gs = np.interp(np.arange(n), np.arange(nb) * block + block / 2, g)
    return np.clip(st * gs[:, None], -ceil, ceil)


def normalize_loudness(path, target=-14.0):
    """統合ラウドネスを測って target LUFS に合わせる。はみ出す山はリミッターで抑える。"""
    with wave.open(path) as w:
        st = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, 2) / 32767
    for _ in range(3):
        lufs = measure_lufs(path)
        st = limit(st * 10 ** ((target - lufs) / 20))
        write_wav(path, st)
    return measure_lufs(path)


# ---------------------------------------------------------------- 書き出し
def video(out):
    wav = os.path.join(OUT_DIR, "_audio.wav")
    audio(wav)
    normalize_loudness(wav)
    cmd = [FFMPEG, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W * UP}x{H * UP}", "-r", str(FPS), "-i", "-",
           "-i", wav,
           "-c:v", "libx264", "-profile:v", "high", "-preset", "slow", "-crf", os.environ.get("CRF", "18"),
           "-tune", "animation", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", "-shortest", out]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for fi in range(NF):
        p.stdin.write(finish(frame(fi)).tobytes())
        if fi % 60 == 0:
            print(f"{fi}/{NF}", file=sys.stderr, flush=True)
    p.stdin.close()
    p.wait()
    os.remove(wav)
    print(out, "lossy frames:", len(LOSSY))


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
        preview(sys.argv[2:] or ["0", "1.0", "3.0", "5.2", "6.5", "9.0", "12.5", "14.5", "16.5", "18.0", "20.0", "26.0"])
    elif len(sys.argv) >= 3 and sys.argv[1] == "video":
        video(sys.argv[2])
    else:
        print(__doc__)
