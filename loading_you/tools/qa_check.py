#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""書き出した動画の品質チェック。書き出しのたびに回して、数値で確かめる。

    python3 tools/qa_check.py render_45s.py loading_you_45s_1080x1920.mp4

測るもの:
    1. 光の点滅  画面全体の平均輝度が40段階以上変わる回数。どの1秒間でも4回（明滅2回）以下にする
    2. 劣化処理  JPEG劣化・モザイクを使ったフレーム数と面積（画面何枚分か）
    3. 圧縮の画質 元の画（finish(frame(f))）と書き出し後の画のPSNR（全体、1秒ごと）
    4. 動きの密度 縮小したグレー画像の隣り合うコマの差。破壊区間の強さを元の版と比べるのに使う
    5. 音        統合ラウドネスとトゥルーピーク（ffmpeg の ebur128）

生成スクリプトは、frame(f)・finish(a)・NF・crush()・mosaic() を持っていればよい（render_45s.py の形）。
"""
import importlib.util
import os
import re
import subprocess
import sys

import numpy as np
from PIL import Image


def load(path):
    sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
    spec = importlib.util.spec_from_file_location("m", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main(mod_path, vid, ffmpeg="ffmpeg"):
    m = load(mod_path)
    cur, lossy = [0], {"frames": set(), "px": 0}
    oc, om = m.crush, getattr(m, "mosaic", None)

    def crush(a, q):
        lossy["px"] += a.shape[0] * a.shape[1]; lossy["frames"].add(cur[0]); return oc(a, q)
    m.crush = crush
    if om:
        def mosaic(a, bs):
            if bs > 1: lossy["px"] += a.shape[0] * a.shape[1]; lossy["frames"].add(cur[0])
            return om(a, bs)
        m.mosaic = mosaic
    p = subprocess.Popen([ffmpeg, "-loglevel", "error", "-i", vid, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    k = np.array([.2126, .7152, .0722], np.float32)
    ps, Y, G = [], [], []
    for f in range(m.NF):
        cur[0] = f
        raw = m.finish(m.frame(f)).astype(np.float32)
        dec = np.frombuffer(p.stdout.read(raw.size), np.uint8).reshape(raw.shape).astype(np.float32)
        mse = ((raw - dec) ** 2).mean()
        ps.append(99 if mse == 0 else 10 * np.log10(255 ** 2 / mse))
        Y.append((raw[::4, ::4] @ k).mean())
        G.append(np.asarray(Image.fromarray(dec.astype(np.uint8)).convert("L").resize((90, 160))).astype(np.float32))
    ps, Y = np.array(ps), np.array(Y)
    fps = 30
    big = np.where(np.abs(np.diff(Y)) > 40)[0]
    worst = max([((big >= s) & (big < s + fps)).sum() for s in range(max(1, m.NF - fps))] or [0])
    energy = np.abs(np.diff(np.array(G), axis=0)).mean((1, 2))
    W, H = m.W, m.H
    print(f"[点滅] 大きな明暗の反転 最大 {worst} 回/秒（4以下なら明滅2回/秒以下で合格） 位置: {[round((b + 1) / fps, 2) for b in big]}")
    print(f"[劣化] JPEG・モザイク {len(lossy['frames'])} フレーム / 面積 画面 {lossy['px'] / (W * H):.1f} 枚分")
    print(f"[画質] PSNR 平均 {ps.mean():.2f} dB / 下位5% {np.percentile(ps, 5):.2f} dB")
    print("[画質] 1秒ごと " + " ".join(f"{ps[i * fps:(i + 1) * fps].mean():.0f}" for i in range(m.NF // fps)))
    print(f"[動き] 大きく動くコマ {100 * (energy > 8).mean():.0f}% / 平均 {energy.mean():.1f}")
    print("[動き] 1秒ごと " + " ".join(f"{energy[i * fps:(i + 1) * fps].mean():.0f}" for i in range(m.NF // fps)))
    out = subprocess.run([ffmpeg, "-hide_banner", "-i", vid, "-af", "ebur128=peak=true", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", out)
    peak = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", out)
    print(f"[音] 統合ラウドネス {lufs[-1] if lufs else '?'} LUFS / トゥルーピーク {peak[-1] if peak else '?'} dBFS（目安 -14 LUFS、-1 dBFS前後）")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    main(sys.argv[1], sys.argv[2], os.environ.get("FFMPEG", "ffmpeg"))
