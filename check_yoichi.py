#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ニッカ余市蒸溜所 ガイドツアー 空き枠ウォッチャー

使い方:
  # 1回目（調査モード）: ページと内部APIの中身を debug/ に保存する
  python check_yoichi.py --dump

  # 通常モード: 空きがあれば通知する
  python check_yoichi.py

環境変数:
  TARGET_DATES    監視する日付（カンマ区切り, YYYY-MM-DD）既定: 2026-10-17
  TOUR_KEYWORD    ツアー種別の絞り込み語（空なら種別を問わない）例: ガイドツアー
  NTFY_TOPIC      ntfy.sh のトピック名（スマホ通知用）
  DISCORD_WEBHOOK Discord の Webhook URL（任意）
  MIN_INTERVAL    連続通知の抑制秒数（既定 1800秒）
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

RESERVE_URL = "https://distillery.nikka.com/yoichi/reservation"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 "
      "YoichiWatch/1.0 (personal availability check; low frequency)")

JST = timezone(timedelta(hours=9))
BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "state.json"
DEBUG_DIR = BASE / "debug"

# 「空きなし」を意味しそうな語。これ以外＝空きあり候補、として判定する。
FULL_WORDS = ["満員", "満席", "受付終了", "休業", "×", "✕", "✖", "－", "-"]
OPEN_WORDS = ["○", "◯", "△", "空き", "予約する", "受付中"]


def targets():
    raw = os.environ.get("TARGET_DATES", "2026-10-17")
    return [d.strip() for d in raw.split(",") if d.strip()]


def tour_keywords():
    """監視するツアー種別。空なら種別を問わない。
    例: TOUR_KEYWORD="ガイドツアー"  /  TOUR_KEYWORD="テイスティング" """
    raw = os.environ.get("TOUR_KEYWORD", "")
    return [w.strip() for w in raw.split(",") if w.strip()]


# ---------------------------------------------------------------- 収集
def collect(dump=False):
    """予約ページを実際にレンダリングし、DOMテキストと内部APIのJSONを集める。"""
    captured = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent=UA,
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1280, "height": 1800},
        )
        page = ctx.new_page()

        def on_response(resp):
            ct = (resp.headers or {}).get("content-type", "")
            if "json" not in ct.lower():
                return
            try:
                captured.append({"url": resp.url, "body": resp.json()})
            except Exception:
                pass

        page.on("response", on_response)
        page.goto(RESERVE_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)

        html = page.content()
        text = page.inner_text("body")
        browser.close()

    if dump:
        DEBUG_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(JST).strftime("%Y%m%d-%H%M%S")
        (DEBUG_DIR / f"page-{stamp}.html").write_text(html, encoding="utf-8")
        (DEBUG_DIR / f"text-{stamp}.txt").write_text(text, encoding="utf-8")
        (DEBUG_DIR / f"api-{stamp}.json").write_text(
            json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[dump] debug/ に保存しました（API応答 {len(captured)} 件）")
        for c in captured:
            print("  -", c["url"])

    return text, captured


# ---------------------------------------------------------------- 判定
def walk(obj, path=""):
    """ネストしたJSONを (パス, 値) に平坦化する。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def judge_from_api(captured, target):
    """APIのJSONから対象日のレコードを探し、空きらしさを判定する。"""
    y, m, d = target.split("-")
    keys = {target, f"{y}/{m}/{d}", f"{int(m)}/{int(d)}", f"{y}{m}{d}"}
    hits = []

    for cap in captured:
        for node in iter_records(cap["body"]):
            blob = json.dumps(node, ensure_ascii=False)
            if not any(k in blob for k in keys):
                continue
            hits.append({"url": cap["url"], "record": node})

    if not hits:
        return None, []

    kws = tour_keywords()
    available = []
    for h in hits:
        blob = json.dumps(h["record"], ensure_ascii=False)
        if kws and not any(k in blob for k in kws):
            continue
        # 残席数らしき数値が 1 以上なら空きとみなす
        nums = re.findall(r'"(?:vacan\w*|remain\w*|stock|zan\w*|count|available\w*|free)"\s*:\s*(\d+)',
                          blob, re.I)
        if any(int(n) > 0 for n in nums):
            available.append(h)
            continue
        if any(w in blob for w in OPEN_WORDS) and not all(w in blob for w in ["満員"]):
            available.append(h)
    return (len(available) > 0), hits


def iter_records(obj):
    """dict を再帰的に走査して、末端に近い dict を取り出す。"""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_records(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_records(v)


def judge_from_text(text, target):
    """APIで判定できないときの保険：カレンダーのテキストから該当日周辺を見る。"""
    _, m, d = target.split("-")
    pat = re.compile(rf"(?<!\d){int(d)}(?!\d)")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if pat.search(ln):
            window = " ".join(lines[i:i + 3])
            if any(w in window for w in OPEN_WORDS):
                return True, window
            if any(w in window for w in FULL_WORDS):
                return False, window
    return None, ""


# ---------------------------------------------------------------- 通知
def notify(title, body):
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": title.encode("utf-8").decode("latin-1", "ignore") or "Yoichi",
                     "Priority": "urgent",
                     "Tags": "whisky",
                     "Click": RESERVE_URL},
        )
        try:
            urllib.request.urlopen(req, timeout=15)
            print("[notify] ntfy 送信")
        except Exception as e:
            print("[notify] ntfy 失敗:", e, file=sys.stderr)

    hook = os.environ.get("DISCORD_WEBHOOK")
    if hook:
        payload = json.dumps({"content": f"**{title}**\n{body}\n{RESERVE_URL}"}).encode()
        req = urllib.request.Request(hook, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=15)
            print("[notify] Discord 送信")
        except Exception as e:
            print("[notify] Discord 失敗:", e, file=sys.stderr)

    if not topic and not hook:
        print("[notify] 通知先が未設定です。NTFY_TOPIC か DISCORD_WEBHOOK を設定してください。")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true", help="調査モード：DOMとAPI応答を保存")
    args = ap.parse_args()

    text, captured = collect(dump=args.dump)
    if args.dump:
        for t in targets():
            api_v, hits = judge_from_api(captured, t)
            txt_v, window = judge_from_text(text, t)
            print(f"\n=== {t} ===")
            print(f"  API判定: {api_v} (ヒット {len(hits)} 件)")
            print(f"  TEXT判定: {txt_v} / 抜粋: {window[:120]}")
            for i, h in enumerate(hits, 1):
                blob = json.dumps(h["record"], ensure_ascii=False)
                print(f"  --- ヒット{i} ({len(blob)} 文字) from {h['url']}")
                print("  " + blob[:1500])
        print("\n=== 全APIのキー名一覧 ===")
        seen = set()
        for cap in captured:
            for path, val in walk(cap["body"]):
                key = re.sub(r"\[\d+\]", "[]", path)
                if key not in seen:
                    seen.add(key)
                    print(f"  {key} = {str(val)[:60]}")
        return

    state = load_state()
    min_interval = int(os.environ.get("MIN_INTERVAL", "1800"))
    now = time.time()
    found = []

    for t in targets():
        verdict, _ = judge_from_api(captured, t)
        if verdict is None:
            verdict, _ = judge_from_text(text, t)
        print(f"{t}: {'空きあり' if verdict else '空きなし' if verdict is False else '判定不可'}")
        if verdict:
            last = state.get(t, 0)
            if now - last >= min_interval:
                found.append(t)
                state[t] = now

    if found:
        stamp = datetime.now(JST).strftime("%m/%d %H:%M")
        notify("余市蒸溜所に空きが出ました",
               f"{stamp} 時点 / 対象日: {', '.join(found)}\nすぐ予約ページへ")
        save_state(state)
    elif state:
        save_state(state)


if __name__ == "__main__":
    main()
