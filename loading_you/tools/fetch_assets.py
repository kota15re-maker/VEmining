#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wellcome Collection と Library of Congress から、コラージュ用の素材画像を集める。

再利用条件がはっきりしたものだけを保存する。
    Wellcome Collection : ライセンスが Public Domain Mark（pdm）または CC0（cc-0）の画像
    Library of Congress : 権利表示が「No known restrictions on publication」などで、
                          制限や未評価の文言を含まない画像
それ以外（CC BY、権利未評価、許諾が必要なもの、表示が無いもの）は保存しない。

保存先:
    assets/<source>/<id>.jpg      画像（長辺1600pxまで）
    assets/manifest.json          1枚ごとの記録（元ページURL、画像URL、ライセンス、権利表示、題名、作者、年代、検索語、取得日時、SHA-256）
    assets/CREDITS.md             manifest.json から作る一覧

使い方:
    python3 tools/fetch_assets.py                     # 全カテゴリを検索して保存
    python3 tools/fetch_assets.py --only eye flower   # カテゴリを絞る
    python3 tools/fetch_assets.py --dry-run           # 保存せず候補だけ表示
    python3 tools/fetch_assets.py --credits           # CREDITS.md だけ作り直す

似た画像ばかりにならないよう、同じ資料（work / item）からは2枚まで、見た目がほぼ同じ画像
（平均ハッシュの差が小さいもの）は1枚だけにする。サイトへの負荷を避けるため、リクエストの間に待ち時間を入れる。
"""
import argparse
import datetime
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(os.path.dirname(HERE), "assets")
MANIFEST = os.path.join(ASSETS, "manifest.json")
UA = "loading-you-collage/1.0 (art video; asset collection with license records)"
MAX_SIDE = 1600

# カテゴリごとの検索語。モチーフ（kind）と、年代・質感が偏らないように両サイトに振り分ける。
QUERIES = {
    "eye":        {"wellcome": ["eye anatomy", "iris eye", "ophthalmology plate", "eye disease"],
                   "loc": ["eye close-up", "optician eye"]},
    "face":       {"wellcome": ["physiognomy", "facial expression photograph", "portrait engraving"],
                   "loc": ["daguerreotype portrait", "tintype portrait", "portrait woman 1860"]},
    "mouth":      {"wellcome": ["lips mouth anatomy", "teeth mouth"], "loc": []},
    "hand":       {"wellcome": ["hand anatomy", "hand gesture", "skeleton hand"], "loc": ["hands photograph"]},
    "body":       {"wellcome": ["anatomical figure", "ear anatomy", "skeleton"], "loc": []},
    "flower":     {"wellcome": ["botanical flower", "herbal woodcut", "flower engraving"],
                   "loc": ["flower garden", "flowers still life", "seed catalog"]},
    "plant":      {"wellcome": ["plant leaves botany", "fern botanical", "roots plant"], "loc": ["tree branches", "vines"]},
    "landscape":  {"wellcome": ["landscape engraving"], "loc": ["mountain landscape", "stereograph landscape", "field meadow"]},
    "water":      {"wellcome": [], "loc": ["lake reflection", "sea waves", "waterfall", "river water"]},
    "building":   {"wellcome": ["hospital building", "asylum building"],
                   "loc": ["building facade", "apartment house", "tenement", "skyscraper construction"]},
    "interior":   {"wellcome": ["hospital ward interior", "laboratory interior"],
                   "loc": ["hallway interior", "staircase interior", "empty room interior"]},
    "instrument": {"wellcome": ["surgical instruments", "microscope", "optical instrument", "anatomical model"],
                   "loc": ["scientific apparatus"]},
    "diagram":    {"wellcome": ["scientific diagram", "optics diagram", "crystal", "cells microscope"],
                   "loc": ["chart diagram print"]},
    "print":      {"wellcome": ["broadside", "advertisement print", "specimen type"], "loc": ["advertisement lithograph", "poster"]},
}

WELLCOME_OK = {"pdm", "cc-0"}
LOC_OK = ["no known restrictions", "public domain", "no known copyright"]
LOC_NG = ["not evaluated", "restricted", "restriction", "permission", "copyright is held", "may be protected", "undetermined"]


def get(url, binary=False, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read()
            return data if binary else json.loads(data.decode("utf-8"))
        except Exception as e:                                # 403（ポリシーで遮断）はやり直しても通らない
            if "403" in str(e) or i == tries - 1:
                raise
            time.sleep(2 ** i)


def ahash(im):
    g = np.asarray(im.convert("L").resize((12, 12), Image.BILINEAR), float)
    return (g > g.mean()).flatten()


def save_image(data, path):
    im = Image.open(io.BytesIO(data)).convert("RGB")
    if max(im.size) > MAX_SIDE:
        k = MAX_SIDE / max(im.size)
        im = im.resize((int(im.width * k), int(im.height * k)), Image.LANCZOS)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path, "JPEG", quality=92)
    return im


# ---------------------------------------------------------------- Wellcome Collection
def wellcome_candidates(query, n):
    q = urllib.parse.urlencode({"query": query, "pageSize": min(100, n * 3), "locations.license": "pdm,cc-0",
                                "include": "source.contributors,source.production,source.genres"})
    data = get("https://api.wellcomecollection.org/catalogue/v2/images?" + q)
    for it in data.get("results", []):
        loc = (it.get("locations") or [{}])[0]
        lic = (loc.get("license") or {})
        if lic.get("id") not in WELLCOME_OK:
            continue
        info = loc.get("url", "")
        if not info.endswith("/info.json"):
            continue
        src = it.get("source") or {}
        contrib = [c.get("agent", {}).get("label") for c in src.get("contributors", []) if c.get("agent")]
        dates = [d.get("label") for p in src.get("production", []) for d in p.get("dates", [])]
        yield {
            "source": "wellcome", "id": it["id"], "work_id": src.get("id"),
            "title": src.get("title"), "creator": "; ".join(filter(None, contrib)) or None,
            "date": "; ".join(filter(None, dates)) or None,
            "page_url": f"https://wellcomecollection.org/works/{src.get('id')}/images?id={it['id']}",
            "image_url": info[:-len("/info.json")] + f"/full/!{MAX_SIDE},{MAX_SIDE}/0/default.jpg",
            "license": {"id": lic.get("id"), "label": lic.get("label"), "url": lic.get("url")},
            "rights_statement": lic.get("label"),
        }


# ---------------------------------------------------------------- Library of Congress
def loc_candidates(query, n):
    q = urllib.parse.urlencode({"q": query, "fo": "json", "c": min(100, n * 3), "fa": "online-format:image"})
    data = get("https://www.loc.gov/photos/?" + q)
    for it in data.get("results", []):
        item_url = it.get("id") or it.get("url")
        if not item_url:
            continue
        time.sleep(0.8)
        try:
            d = get(item_url.rstrip("/") + "/?fo=json")
        except Exception:
            continue
        item = d.get("item", {})
        rights = " ".join(item.get("rights_advisory", []) or []) + " " + " ".join(
            item.get("rights", []) if isinstance(item.get("rights"), list) else [str(item.get("rights", ""))])
        rl = rights.lower()
        if not any(k in rl for k in LOC_OK) or any(k in rl for k in LOC_NG):
            continue                                        # 権利がはっきりしないものは使わない
        best = None
        for res in d.get("resources", []):
            for group in res.get("files", []):
                for f in group:
                    if f.get("mimetype") == "image/jpeg" and f.get("width") and f["width"] <= 2400:
                        if best is None or f["width"] > best["width"]:
                            best = f
        url = best["url"] if best else (it.get("image_url") or [None])[-1]
        if not url:
            continue
        url = url.split("#")[0]
        lccn = item_url.rstrip("/").split("/")[-1]
        yield {
            "source": "loc", "id": lccn, "work_id": lccn,
            "title": item.get("title") or it.get("title"),
            "creator": "; ".join(item.get("contributor_names", []) or it.get("contributor", []) or []) or None,
            "date": item.get("date") or it.get("date"),
            "page_url": item_url, "image_url": url,
            "license": {"id": "loc-no-known-restrictions", "label": "No known restrictions on publication (per Library of Congress rights advisory)",
                        "url": "https://www.loc.gov/legal/"},
            "rights_statement": rights.strip(),
        }


# ---------------------------------------------------------------- 収集
def load_manifest():
    if os.path.exists(MANIFEST):
        return json.load(open(MANIFEST, encoding="utf-8"))
    return []


def write_credits(man):
    lines = ["# 素材画像のクレジット", "",
             "動画内で使う素材画像の出典と再利用条件です。`manifest.json` から生成しています。", "",
             "| ファイル | カテゴリ | 題名 | 作者 | 年代 | 出典 | ライセンス / 権利表示 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for m in man:
        src = "Wellcome Collection" if m["source"] == "wellcome" else "Library of Congress"
        esc = lambda s: (s or "").replace("|", "／").replace("\n", " ")[:120]
        lines.append(f"| `{m['file']}` | {m['category']} | {esc(m['title'])} | {esc(m['creator'])} | {esc(m['date'])} | "
                     f"[{src}]({m['page_url']}) | {esc(m['license']['label'])} |")
    open(os.path.join(ASSETS, "CREDITS.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="カテゴリを絞る")
    ap.add_argument("--per-query", type=int, default=3, help="1つの検索語から保存する最大枚数")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--credits", action="store_true")
    args = ap.parse_args()
    man = load_manifest()
    if args.credits:
        write_credits(man); return
    seen = {(m["source"], m["id"]) for m in man}
    per_work = {}
    for m in man:
        per_work[(m["source"], m["work_id"])] = per_work.get((m["source"], m["work_id"]), 0) + 1
    hashes = [np.array(m["ahash"], bool) for m in man if m.get("ahash")]
    cats = args.only or list(QUERIES)
    for cat in cats:
        for site, queries in QUERIES[cat].items():
            for query in queries:
                got = 0
                try:
                    cands = list((wellcome_candidates if site == "wellcome" else loc_candidates)(query, args.per_query))
                except Exception as e:
                    print(f"[{site}] {query}: 取得できませんでした ({e})", file=sys.stderr)
                    continue
                for c in cands:
                    if got >= args.per_query:
                        break
                    key = (c["source"], c["id"]); wk = (c["source"], c["work_id"])
                    if key in seen or per_work.get(wk, 0) >= 2:
                        continue
                    if args.dry_run:
                        print(f"[{cat}] {c['title']!r} {c['license']['label']} {c['page_url']}"); got += 1; continue
                    try:
                        data = get(c["image_url"], binary=True)
                    except Exception as e:
                        print(f"  画像を取得できませんでした: {c['image_url']} ({e})", file=sys.stderr); continue
                    im = Image.open(io.BytesIO(data)).convert("RGB")
                    h = ahash(im)
                    if any((h != x).sum() < 10 for x in hashes):
                        continue                            # ほぼ同じ見た目の画像は1枚だけ
                    fn = f"{c['source']}/{re.sub(r'[^A-Za-z0-9_-]', '_', c['id'])}.jpg"
                    im = save_image(data, os.path.join(ASSETS, fn))
                    c.update({"file": fn, "category": cat, "query": query, "size": list(im.size),
                              "retrieved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                              "sha256": hashlib.sha256(open(os.path.join(ASSETS, fn), "rb").read()).hexdigest(),
                              "ahash": h.tolist()})
                    man.append(c); seen.add(key); hashes.append(h)
                    per_work[wk] = per_work.get(wk, 0) + 1
                    got += 1
                    print(f"[{cat}] {fn}  {c['license']['label']}  {c['page_url']}")
                    json.dump(man, open(MANIFEST, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                    time.sleep(0.5)
                time.sleep(1.0)
    if not args.dry_run:
        write_credits(man)
    print(f"{len(man)} images in manifest")


if __name__ == "__main__":
    main()
