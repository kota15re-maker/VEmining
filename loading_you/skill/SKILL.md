---
name: loading-you-style
description: 「読み込み中のあなた」系の縦型ショート動画（パステルのロード画面、グリッチ、古いデジタル質感、素材画像のコラージュ）を作る・直す・尺を変える・素材を集めるときに使う。手続き生成（Python、numpy、Pillow、ffmpeg）で映像と音を作り、Wellcome Collection と Library of Congress の再利用条件が明確な素材だけを使う。
---

# 「読み込み中のあなた」スタイルで作る

作業を始める前に、このフォルダ（または元の `loading_you/`）の `HANDBOOK.md` を全部読むこと。技法、素材の扱い、品質の測り方、つまずいた点がすべて書いてある。生成スクリプトの実例は `render_45s.py`、15秒版の原本は `render.py`。

## 必ず守ること

1. 既存の作家名・作品名を出さない。技法として組み直す。
2. 「かわいい・静か・整った画面」と「壊れた・不穏・異質な画面」を行き来させる。静の画面は、戻るたびに何かが1つ増えているか、少し間違っている。
3. **文字は2種類を必ず両方使う。**
   - A. 読みづらいが読める文章：一瞬・小さい・一部が欠ける・目の行きにくい場所。でも一時停止すれば読める本物の日本語の文。欠けさせるのは2割まで。出るフレームを静止画で書き出して、読めることを確かめる。
   - B. 完全に読めない文字：合成かな、ドット記号、図形記号。偶然に実在の語にならないこと。
4. 花、植物、人の体の断片、風景、空間、建物、水面、写真断片、図形、UI、文字断片を併置する。馴染ませず、置換・侵食・切断・断片化・異物の挿入で関係づける。
5. 同じ技法の繰り返しに頼らない。破壊の回ごとに手法の系統を変える（HANDBOOK 第5章）。
6. 素材画像は Wellcome Collection（Public Domain Mark / CC0）と Library of Congress（権利表示が「No known restrictions」または public domain）だけ。権利が曖昧なものは使わない。1枚ずつ元ページURL、画像URL、ライセンス、権利表示を `assets/manifest.json` に記録し、`assets/CREDITS.md` を作る。
7. 破壊は最近傍・ハードエッジの変形で作る。JPEG劣化とモザイクは最小限。変化は12fpsで保持する。
8. 画面全体の大きな明暗反転（平均輝度で40段階以上）は、どの1秒間でも4回以下。
9. 音は -14 LUFS 前後。無音の間は完全な無音。破壊の音は映像と同時に切る。

## 手順

1. 区間表（`SEGS`）を作り、破壊の回ごとに手法の系統を割り当てる。読める文章（A）の出し方も決める。
2. 素材：`python3 tools/fetch_assets.py`（検索語は `QUERIES` を内容に合わせて変える）→ `python3 tools/contact_sheet.py` で一覧を見て選ぶ → `assets/crops.json` に切り抜きを書く（目や口は拡大して目盛りで測る）→ 切り抜いた結果を全部目で確かめる。
3. `python3 render_45s.py preview <秒> …` で主要なコマを確かめながら作る。
4. `python3 render_45s.py video out.mp4` で書き出す（H.264 CRF 18、preset slow、tune animation）。
5. `python3 tools/qa_check.py render_45s.py out.mp4` で、点滅・劣化・画質・動き・音量を確かめる。HANDBOOK 第11章のチェックリストを全部通す。
6. `python3 tools/fetch_assets.py --prune` で使わない候補を片付け、クレジットを作り直す。

## ネットワーク

素材を集めるには、クラウド環境のネットワーク設定で `api.wellcomecollection.org`、`iiif.wellcomecollection.org`、`www.loc.gov`、`tile.loc.gov` を許可しておく。LoC には用途を書いた User-Agent と `Accept: application/json` で問い合わせる（`fetch_assets.py` はそうしている）。遮断されたら、ユーザーに許可を頼み、繋がらない部分は後回しにして他を進める。
