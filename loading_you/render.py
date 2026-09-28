import numpy as np, io, math, subprocess, sys, wave
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H, S, FPS, N = 360, 640, 3, 30, 450
OW, OH = W * S, H * S
R0 = np.random.default_rng(7)

def hx(h): return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))
PAPER, PINK, BLUE = hx('#F3EBDD'), hx('#F7C8D8'), hx('#BFD8E8')
CY, MG, YG, RD, PU = hx('#00FFF0'), hx('#FF00B8'), hx('#B6FF00'), hx('#FF2A1F'), hx('#7A2CFF')
INK = (74, 58, 70)
FRAME = (88, 76, 98)
CRASH = [CY, MG, YG, RD, PU]

# ---------------- paper ----------------
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
    p = base + lf + fib + vig
    return p

PAPER_BASE = make_paper()
PAPERS = [np.clip(PAPER_BASE + R0.normal(0, 4.5, (H, W, 1)), 0, 255).astype(np.uint8) for _ in range(6)]
PAPER_WHITE = np.clip(PAPER_BASE * 0.35 + 250 * 0.65 + R0.normal(0, 2.0, (H, W, 1)), 0, 255).astype(np.uint8)

# ---------------- glyphs ----------------
G = 14
FONT = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
fK = ImageFont.truetype(FONT, 12, index=0)
fS = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 12)

def cmask(c, font):
    im = Image.new('L', (G, G), 0); d = ImageDraw.Draw(im)
    d.text((G // 2, G // 2), c, font=font, fill=255, anchor='mm')
    return np.asarray(im) > 90

KANA = list('あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん')
KM = [cmask(c, fK) for c in KANA]
GL = []  # glyph bank of bool masks

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
LINE2_END = READ_ID.copy(); LINE2_END[4] = int(R0.integers(130))  # one wrong char at the end

# ---------------- window ----------------
WW, WH = 232, 104
WCACHE = {}
def make_window(progress, l1, l2, style=0):
    key = (round(progress, 3), tuple(l1), tuple(l2), style)
    if key in WCACHE: return WCACHE[key]
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
    WCACHE[key] = im
    return im

def with_alpha(im, a):
    if a >= 1: return im
    arr = np.asarray(im).copy(); arr[..., 3] = (arr[..., 3] * a).astype(np.uint8)
    return Image.fromarray(arr)

# ---------------- mascot ----------------
MC = 200
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

# ---------------- photo-like fragments ----------------
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

FRAGS = [np.clip(f(np.random.default_rng(i + 30)), 0, 255).astype(np.uint8)
         for i, f in enumerate([frag_tiles, frag_window, frag_hand, frag_curtain, frag_sky, frag_static])]

def dither2(a, c0, c1):
    b = np.asarray(Image.fromarray(a).convert('L').convert('1')).astype(bool)[..., None]
    return np.where(b, np.array(c1, np.uint8), np.array(c0, np.uint8))

def frag_full(k, mode):
    f = FRAGS[k][:, 21:75]  # 54x96 crop, 9:16
    f = np.asarray(Image.fromarray(f).resize((45, 80), Image.BILINEAR))
    if mode == 1: f = dither2(f, (10, 8, 20), PAPER)
    elif mode == 2: f = dither2(f, PU, YG)
    elif mode == 3: f = f[..., [2, 0, 1]]
    return np.asarray(Image.fromarray(f).resize((W, H), Image.NEAREST)).copy()

INTR = np.asarray(Image.fromarray(FRAGS[1]).resize((8, 8), Image.BILINEAR).resize((64, 64), Image.NEAREST))
INTR2 = np.asarray(Image.fromarray(FRAGS[2]).resize((8, 8), Image.BILINEAR).resize((64, 64), Image.NEAREST))
IX, IY = 256, 446

# ---------------- effects ----------------
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
    o = A.copy()
    for _ in range(n):
        y = int(r.integers(y0, y1)); h = int(r.integers(2, 10)); dx = int(r.integers(4, maxdx + 1)) * (1 if r.random() < .5 else -1)
        seg = np.roll(A[y:y + h, x0:x1], dx, 1)
        if dx > 0: seg[:, :dx] = B[y:y + h, x0:x0 + dx]
        else: seg[:, dx:] = B[y:y + h, x1 + dx:x1]
        o[y:y + h, x0:x1] = seg
        # layer below shows through the tear
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
            o[y:y + hh, x:x + ww] = src[:H - y, :W - x][:hh, :ww] if True else src
        elif q < 0.8 and colors:
            o[y:y + h, x:x + w] = CRASH[int(r.integers(5))]
        else:
            o[y:y + h, x:x + w] = o[y:y + h, x:x + w][..., [1, 2, 0]]
    return o

def jpeg(a, q):
    b = io.BytesIO(); Image.fromarray(a).save(b, 'JPEG', quality=int(q)); return np.asarray(Image.open(b).convert("RGB")).copy()

def crush(a, q):
    sm = a[::2, ::2]
    sm = jpeg(jpeg(sm, q), max(2, q - 3))
    return sm.repeat(2, 0).repeat(2, 1)

def zoom(a, z, cx, cy):
    w, h = int(W / z), int(H / z)
    x0 = int(np.clip(cx - w / 2, 0, W - w)); y0 = int(np.clip(cy - h / 2, 0, H - h))
    return np.asarray(Image.fromarray(a[y0:y0 + h, x0:x0 + w]).resize((W, H), Image.NEAREST)).copy()

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

# ---------------- state & scene ----------------
def progress_at(t):
    if t < 0.5: return 0.12
    if t < 2.2:
        step = int((t - 0.5) * 12); steps = int(1.7 * 12)
        rr = np.random.default_rng(99); inc = rr.random(steps + 1) ** 2; inc = np.cumsum(inc) / inc.sum()
        return 0.12 + 0.86 * inc[min(step, steps)]
    return 0.98

def sway(t):
    if 3.0 <= t < 5.0: return int(round(3 * math.sin(2 * math.pi * 0.8 * (int(t * 12) / 12 - 3))))
    return 0

def look(t):
    if 3.2 <= t < 3.8: return -3
    if 3.8 <= t < 4.4: return 3
    return 0

def state(t, f):
    st = {}
    step = int(t * 12)
    st['pidx'] = step % 6
    st['scale'] = 1.22 + 0.02 * math.sin(2 * math.pi * step / 12 * 0.9)
    sw = sway(t); swp = sway(t - 1 / 12)
    st['sway'] = sw
    st['edx'] = look(t - 1 / 12) + (swp - sw)  # eyes lag one "koma" behind body
    st['edy'] = 0
    st['blink'] = (1.9 <= t < 2.05) or (13.8 <= t < 13.95)
    st['progress'] = progress_at(t)
    rr = np.random.default_rng(step * 3 + 5)
    l1 = LINE1.copy()
    if rr.random() < 0.25: l1[int(rr.integers(9))] = int(rr.integers(NPSEUDO))
    l1c = [(g, FRAME) for g in l1]
    if 3.8 <= t < 11: l1c[3] = (l1[3], RD)
    if t < 5.0: l2 = [(g, INK) for g in READ_ID]
    elif t < 11:
        l2 = []
        for i, g in enumerate(READ_ID):
            if t > 5.0 + i * 0.05: g = int(np.random.default_rng(i * 7 + step).integers(NPSEUDO))
            l2.append((g, INK))
    else: l2 = [(g, INK) for g in LINE2_END]
    st['l1'], st['l2'] = l1c, l2
    wins = [(64, 96, 0, 1.0, st['progress'])]
    if t >= 12.5: wins.append((82, 118, 0, 1.0, st['progress']))
    elif t >= 5.3:
        spawns = [(5.3, 70, 150, 1, (22, 30)), (6.0, 30, 230, 0, (-14, 26)), (6.7, 100, 300, 3, (18, -20)), (6.7, 20, 420, 1, (26, -30))]
        for (ts, x, y, sty, v) in spawns:
            if t >= ts:
                for k in (3, 2, 1, 0):  # echoes -> ghost trail
                    tt = max(ts, t - k * 0.05)
                    wins.append((x + v[0] * (tt - ts), y + v[1] * (tt - ts), sty, [1, .45, .28, .15][k], st['progress']))
    st['wins'] = wins
    return st

def render(st, variant=0, show_windows=True):
    img = Image.fromarray(PAPERS[st['pidx']]).convert('RGBA')
    def draw_win(w):
        x, y, sty, a, pg = w
        im = make_window(pg, [g for g in st['l1']], [g for g in st['l2']], sty)
        img.alpha_composite(with_alpha(im, a), (int(x), int(y)))
    if show_windows: draw_win(st['wins'][0])
    m = mascot(st['scale'], st['edx'], st['edy'], variant, st['blink'])
    mx, my = MX0 + st['sway'], MY0
    img.alpha_composite(m, (mx, my))
    if show_windows:
        for w in st['wins'][1:]: draw_win(w)
    for i, g in enumerate(FOOT): stamp(img, 180 - len(FOOT) * 13 // 2 + i * 13, 556, g, (150, 126, 140))
    if int(st.get('t', 0) * 2) % 2 == 0:
        ImageDraw.Draw(img).rectangle([180 + len(FOOT) * 13 // 2 + 2, 558, 180 + len(FOOT) * 13 // 2 + 8, 569], fill=(150, 126, 140))
    return np.asarray(img.convert('RGB')).copy(), mx, my, np.asarray(m.split()[3])

# ---------------- special frames ----------------
def hook(k):
    st = state(1.0, 30); st['t'] = 1.0
    a, *_ = render(st)
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

SIL = None
def silence():
    global SIL
    if SIL is None:
        img = Image.fromarray(PAPER_WHITE).convert('RGBA')
        m = mascot(0.42, 0, 0, 0, False)
        img.alpha_composite(m, (80, 252))
        SIL = np.asarray(img.convert('RGB')).copy()
    return SIL

# ---------------- frame ----------------
def frame(f):
    t = f / FPS
    if f < 3: return hook(f)
    if f >= N - 3: return hook(f - (N - 3))
    if 11.0 <= t < 12.5: return silence()
    st = state(t, f); st['t'] = t
    r = np.random.default_rng(f * 17 + 3)
    rs = np.random.default_rng(int(t * 12) * 11 + 1)  # 12fps-held randomness

    if t < 3.0 or t >= 12.5:
        if t >= 12.5: st['edx'] += 2
        a, mx, my, mm = render(st)
        if 3 <= f < 7: a = rgb_shift(a, [5, 3, 2, 1][f - 3])
        if 2.7 <= t < 3.0:  # omen
            a = np.roll(a, int(r.integers(-1, 2)), 1); a = rgb_shift(a, 1)
        return a

    if t < 5.0:
        a, mx, my, mm = render(st)
        if t >= 3.4:
            a[IY:IY + 64, IX:IX + 64] = INTR if int(t * 12) % 7 else INTR2
        if 4.2 <= t < 4.2 + 1 / 30:
            y = 300; a[y:y + 6] = YG; a[y + 6:y + 9] = np.roll(a[y + 6:y + 9], 12, 1)
        if 4.6 <= t < 4.67:
            im = Image.fromarray(a); error_box(im, 150, 205, r); a = np.asarray(im).copy()
        if 4.75 <= t:
            a = np.roll(a, int(r.integers(-1, 2)), 1)
            a[400:430] = np.roll(a[400:430], int(r.integers(-4, 5)), 1)
        return a

    if t < 8.0:
        p = (t - 5.0) / 3.0
        A, mx, my, mm = render(st)
        B, *_ = render(st, variant=1)
        fy0 = my + 108 - 8 if p < 0.4 else my + 20
        A = slices(A, B, rs, int(4 + 9 * p), int(10 + 24 * p), fy0, my + 175, mx, mx + MC)
        if t > 6.4 and int(t * 6) % 3 == 0:
            y0, y1, x0, x1 = my + 30, my + 170, 180, mx + MC
            sub = A[y0:y1, x0:x1]; sb = B[y0:y1, x0:x1]
            sub[::2] = sb[::2]
        a = A
        a[IY:IY + 64, IX:IX + 64] = INTR if int(t * 12) % 5 else INTR2
        if int(t * 10) % 4 == 0:
            dx = int(r.integers(3, 8))
            a = bg_only(a, lambda z: rgb_shift(z, dx), mm, mx, my)
            for _ in range(2):
                y = int(r.integers(0, H)); a[y:y + 1] = CY if r.random() < .5 else MG
        if 7.3 <= t < 7.34:
            im = Image.fromarray(a); error_box(im, 30, 470, r); a = np.asarray(im).copy()
        if t >= 7.7:
            a = np.roll(a, (int(r.integers(-2, 3)), int(r.integers(-2, 3))), (0, 1)); a = rgb_shift(a, 2)
        return a

    # ---- 8.0 - 11.0 : breakdown ----
    p = min(1.0, (t - 8.0) / 2.6)
    for i, w in enumerate(st['wins']):
        st['wins'][i] = (w[0] + int(r.integers(-6, 7)), w[1] + int(r.integers(-6, 7)), w[2], w[3], [0.98, 0.13, 1.0, 0.61][int(rs.integers(4))])
    if 9.5 <= t < 9.6:  # a clean glimpse of the other layer
        B, *_ = render(st, variant=1, show_windows=False)
        return rgb_shift(B, 1)
    A, mx, my, mm = render(st)
    B, *_ = render(st, variant=1)
    a = slices(A, B, rs, int(6 + 12 * p), int(18 + 40 * p), my, my + MC, 0, W)
    a[IY:IY + 64, IX:IX + 64] = INTR2
    z = 1 + (0.45 + 0.25 * p) * abs(math.sin(math.pi * (t - 8.0) / 1.5))
    a = zoom(a, z, 180 + int(r.integers(-3, 4)), 330)
    fl = np.random.default_rng(int(f // 3) * 5 + 77)
    if (f % 3 == 0 and fl.random() < 0.85) or (f % 3 == 1 and fl.random() < 0.35):
        full = frag_full(int(fl.integers(len(FRAGS))), int(fl.integers(4)))
        if fl.random() < 0.6: a = full
        else:
            x0 = int(fl.integers(0, 160)); y0 = int(fl.integers(0, 380))
            a[y0:y0 + 260, x0:x0 + 200] = full[y0:y0 + 260, x0:x0 + 200]
    a = block_glitch(a, r, int(16 + 60 * p))
    for _ in range(int(r.integers(2, 6))):
        y = int(r.integers(0, H - 40)); h = int(r.integers(6, 40))
        a[y:y + h] = a[y:y + h][::-1] if r.random() < .6 else a[y:y + h, ::-1]
    if r.random() < 0.3 + 0.5 * p: a = crush(a, int(r.integers(3, 12)))
    cm = np.random.default_rng(int(f // 2) * 13 + 9).random()
    if cm < 0.22: a = duotone(a, PU, RD)
    elif cm < 0.32: a = duotone(a, (10, 6, 30), CY)
    elif cm < 0.7: a = rgb_shift(a, int(r.integers(4, 10 + int(10 * p))))
    elif cm < 0.8:
        y0 = int(r.integers(0, H - 200)); a[y0:y0 + 200] = 255 - a[y0:y0 + 200]
    im = Image.fromarray(a)
    for _ in range(int(3 + 10 * p)):
        c = [(255, 255, 255), (12, 8, 20), RD, PU, CY, MG, YG][int(r.integers(7))]
        stamp(im, r.integers(-10, 330), r.integers(0, 610), int(r.integers(NPSEUDO)), c, scale=int(r.integers(2, 6)))
    if r.random() < 0.25 + 0.3 * p: error_box(im, int(r.integers(0, 230)), int(r.integers(40, 560)), r)
    a = np.asarray(im).copy()
    if t >= 10.6: a = np.roll(rgb_shift(a, 14), int(r.integers(-8, 9)), 0)
    return a

# ---------------- output finish (CRT / scan) ----------------
yy = np.arange(OH)
SCAN = np.where(yy % S == S - 1, 0.84, 1.0).astype(np.float32)[:, None, None]
vy = np.linspace(-1, 1, OH)[:, None]; vx = np.linspace(-1, 1, OW)[None, :]
VIG = (1 - 0.16 * (vx ** 2 * 0.8 + vy ** 2 * 0.5)).astype(np.float32)[..., None]
MULT = SCAN * VIG

def finish(a):
    up = a.repeat(S, 0).repeat(S, 1)
    o = up.astype(np.float32)
    o[..., 0] = np.roll(o[..., 0], 1, 1); o[..., 2] = np.roll(o[..., 2], -1, 1)
    o *= MULT
    return np.clip(o, 0, 255).astype(np.uint8)

# ---------------- audio ----------------
def audio(path):
    sr = 48000; n = sr * 15; t = np.arange(n) / sr
    rng = np.random.default_rng(5)
    L = np.zeros(n); Rr = np.zeros(n)
    def add(sig, start, pan=0.0, g=1.0):
        i = int(start * sr); j = min(n, i + len(sig)); s = sig[:j - i] * g
        L[i:j] += s * (1 - pan) / 2 * 2 ** .5; Rr[i:j] += s * (1 + pan) / 2 * 2 ** .5
    hiss = np.convolve(rng.normal(0, 1, n), np.ones(40) / 40, 'same')
    hum = 0.018 * np.sin(2 * np.pi * 60 * t) + 0.008 * np.sin(2 * np.pi * 120 * t) + 0.006 * hiss
    env = ((t >= 0.1) & (t < 11.0)) | ((t >= 12.5) & (t < 14.9))
    ramp = np.clip((t - 12.5) / 0.03, 0, 1) * (t >= 12.5) + (t < 12.5)
    L += hum * env * ramp; Rr += hum * env * ramp
    def crushed(dur, g=0.5, seed=0):
        r = np.random.default_rng(seed); m = int(dur * sr)
        x = r.normal(0, 1, m // 24 + 1).repeat(24)[:m]
        sq = np.sign(np.sin(2 * np.pi * r.uniform(80, 900) * np.arange(m) / sr))
        return np.round((x * 0.6 + sq * 0.5) * 4) / 4 * g
    def blip(freq, dur=0.04, g=0.25, f2=None):
        m = int(dur * sr); tt = np.arange(m) / sr
        fr = freq if f2 is None else np.linspace(freq, f2, m)
        return np.sin(2 * np.pi * np.cumsum(fr) / sr) * np.exp(-tt * 60) * g
    add(crushed(0.1, 0.55, 1), 0.0); add(crushed(0.1, 0.55, 1), 14.9)
    # progress ticks
    last = None
    for f in range(15, 66):
        pg = progress_at(f / FPS)
        if pg != last: add(blip(3200, 0.006, 0.12), f / FPS, pan=0.3)
        last = pg
    add(blip(1400, 0.05, 0.22), 3.4, -0.4)
    add(blip(5200, 0.035, 0.18, 2600), 4.2, 0.5)
    add(np.concatenate([blip(880, 0.06, 0.2), blip(660, 0.06, 0.2)]), 4.6)
    for ts, pn in [(5.0, 0), (5.3, -0.5), (6.0, 0.5), (6.7, -0.3)]:
        add(blip(2400, 0.07, 0.22, 700), ts, pn)
    add(np.concatenate([blip(880, 0.05, 0.18), blip(660, 0.05, 0.18)]), 7.3, 0.4)
    for k in range(int(3 * 12)):  # crackle for the tearing face
        ts = 5.0 + k / 12
        if rng.random() < 0.3 + 0.5 * (k / 36):
            add(crushed(0.02, 0.12 + 0.12 * k / 36, 100 + k), ts, rng.uniform(-.6, .6))
    # breakdown stutter on a 0.1s grid
    for k in range(30):
        ts = 8.0 + k * 0.1; p = k / 29; g = 0.22 + 0.3 * p
        q = rng.random()
        if q < 0.6: add(crushed(0.1 if rng.random() < .5 else 0.066, g, 200 + k), ts, rng.uniform(-.8, .8))
        elif q < 0.9:
            m = int(0.1 * sr); tt = np.arange(m) / sr
            add(np.sign(np.sin(2 * np.pi * rng.uniform(90, 1800) * tt)) * g * 0.6, ts, rng.uniform(-.8, .8))
    m = int(3 * sr); tt = np.arange(m) / sr
    sub = np.sin(2 * np.pi * 48 * tt) * (0.25 * np.abs(np.sin(np.pi * tt / 1.5)))
    add(sub, 8.0)
    cut = (t >= 11.0) & (t < 12.5)
    L[cut] = 0; Rr[cut] = 0
    peak = max(np.abs(L).max(), np.abs(Rr).max()); L *= 0.89 / peak; Rr *= 0.89 / peak
    data = (np.stack([L, Rr], 1) * 32767).astype(np.int16)
    with wave.open(path, 'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr); w.writeframes(data.tobytes())

if __name__ == '__main__':
    mode = sys.argv[1]
    if mode == 'preview':
        times = [float(x) for x in sys.argv[2:]]
        tiles = [Image.fromarray(frame(int(round(x * FPS)))).resize((180, 320), Image.NEAREST) for x in times]
        sheet = Image.new('RGB', (180 * 6, 320 * ((len(tiles) + 5) // 6)), (0, 0, 0))
        for i, tl in enumerate(tiles): sheet.paste(tl, ((i % 6) * 180, (i // 6) * 320))
        sheet.save('/home/claude/preview.png')
    else:
        audio('/home/claude/audio.wav')
        out = sys.argv[2]
        p = subprocess.Popen(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{OW}x{OH}', '-r', str(FPS), '-i', '-',
                              '-i', '/home/claude/audio.wav', '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p',
                              '-profile:v', 'high', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-movflags', '+faststart', '-shortest', out], stdin=subprocess.PIPE)
        for f in range(N):
            p.stdin.write(finish(frame(f)).tobytes())
        p.stdin.close(); p.wait()
        print('done', p.returncode)
