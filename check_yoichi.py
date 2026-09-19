#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ニッカ余市蒸溜所 ガイドツアー 空き枠ウォッチャー

判定根拠:
  api/reserveSlot/list の data.date_info[] を読み、
  rsv_slot[].rsv_course[].rsv_remaining_num > 0 を「空き」とする。

環境変数:
  TARGET_DATES  監視日（カンマ区切り, YYYY-MM-DD）既定: 2026-10-17
  TOUR_KEYWORD  コース種別の絞り込み。既定: 通常見学
                rsv_course_type_name か rsv_course_name に含まれれば対象。
                空にすると全コース（有料イベント含む）が対象。
  NTFY_TOPIC / DISCORD_WEBHOOK  通知先
  MIN_INTERVAL  同一内容の再通知を抑制する秒数（既定 1800）
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

VERSION = "v4"
RESERVE_URL = "https://distillery.nikka.com/yoichi/reservation"
API_HINT = "/api/reserveSlot/list"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

JST = timezone(timedelta(hours=9))
BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "state.json"
DEBUG_DIR = BASE / "debug"


def targets():
    raw = os.environ.get("TARGET_DATES", "2026-10-17")
    return [d.strip() for d in raw.split(",") if d.strip()]


def keyword():
    return os.environ.get("TOUR_KEYWORD", "通常見学").strip()


def hhmm(t):
    t = str(t).zfill(4)
    return f"{t[:2]}:{t[2:]}"


# ---------------------------------------------------------------- 取得
def fetch_date_info():
    """予約ページを開き、reserveSlot/list の data.date_info を取り出す。"""
    found = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=UA, locale="ja-JP",
                                  timezone_id="Asia/Tokyo",
                                  viewport={"width": 1280, "height": 1600})
        page = ctx.new_page()

        def on_response(resp):
            if API_HINT not in resp.url:
                return
            try:
                body = resp.json()
            except Exception:
                return
            info = (body.get("data") or {}).get("date_info")
            if isinstance(info, list):
                found.append(info)

        page.on("response", on_response)
        page.goto(RESERVE_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)
        browser.close()

    if not found:
        raise RuntimeError("date_info を取得できませんでした（サイト構造変更の可能性）")
    return max(found, key=len)


# ---------------------------------------------------------------- 判定
def slots_for(date_info, target):
    """対象日の (時刻, コース名, 種別, 料金, 残数) を列挙する。"""
    for day in date_info:
        if day.get("date") != target:
            continue
        rows = []
        for slot in day.get("rsv_slot") or []:
            for c in slot.get("rsv_course") or []:
                rows.append({
                    "time": hhmm(slot.get("start_time")),
                    "name": c.get("rsv_course_name", ""),
                    "type": c.get("rsv_course_type_name", ""),
                    "fee": c.get("rsv_course_fee", ""),
                    "remain": c.get("rsv_remaining_num", 0),
                    "web": slot.get("web_disp_flg"),
                })
        return day, rows
    return None, None


def open_slots(rows, kw):
    out = []
    for r in rows:
        if r["remain"] is None or int(r["remain"]) <= 0:
            continue
        if kw and kw not in r["type"] and kw not in r["name"]:
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------- 通知
def notify(title, body):
    sent = False
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": "Yoichi slot open",
                     "Priority": "urgent",
                     "Tags": "whisky",
                     "Click": RESERVE_URL},
        )
        try:
            urllib.request.urlopen(req, timeout=15)
            print("[notify] ntfy 送信")
            sent = True
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
            sent = True
        except Exception as e:
            print("[notify] Discord 失敗:", e, file=sys.stderr)

    if not sent:
        print("[notify] 通知先が未設定か、送信に失敗しました。")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true", help="全枠を表示（通知しない）")
    args = ap.parse_args()

    kw = keyword()
    print(f"===== check_yoichi {VERSION} =====")
    date_info = fetch_date_info()
    print(f"date_info: {len(date_info)} 日分 / 絞り込み: {kw or '(なし)'}")

    if args.dump:
        DEBUG_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(JST).strftime("%Y%m%d-%H%M%S")
        (DEBUG_DIR / f"date_info-{stamp}.json").write_text(
            json.dumps(date_info, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.dump:
        notify("接続テスト",
               f"{datetime.now(JST).strftime('%m/%d %H:%M')} check_yoichi {VERSION} "
               f"からのテスト送信です。これが届けば通知経路は正常です。")

        print("\n===== 空きのある日 一覧 =====")
        hit_any = False
        for day in date_info:
            d = day.get("date")
            _, rows = slots_for(date_info, d)
            avail = open_slots(rows or [], kw)
            if avail:
                hit_any = True
                print(f"  {d}: " + " / ".join(
                    f"{r['time']}({r['name']}/残{r['remain']})" for r in avail))
        if not hit_any:
            print(f"  絞り込み「{kw}」に該当する空きは全期間でありません")

    state = load_state()
    now = time.time()
    min_interval = int(os.environ.get("MIN_INTERVAL", "1800"))
    messages = []

    for t in targets():
        day, rows = slots_for(date_info, t)
        print(f"\n=== {t} ===")
        if day is None:
            print("  この日付はカレンダーにありません（未解禁 or 範囲外）")
            continue
        print(f"  rest_flg={day.get('rest_flg')} holiday_flg={day.get('holiday_flg')} "
              f"枠数={len(rows)}")

        if args.dump:
            for r in rows:
                print(f"  {r['time']} [{r['type']}] {r['name']} "
                      f"残{r['remain']} web_disp={r['web']} {r['fee'][:20]}")

        avail = open_slots(rows, kw)
        if not avail:
            print("  → 空きなし")
            continue

        lines = [f"{r['time']} {r['name']} (残{r['remain']})" for r in avail]
        print("  → 空きあり: " + " / ".join(lines))

        sig = "|".join(lines)
        last = state.get(t, {})
        if not args.dump and (sig != last.get("sig") or now - last.get("at", 0) >= min_interval):
            messages.append(f"【{t}】\n" + "\n".join(lines))
            state[t] = {"sig": sig, "at": now}

    if messages:
        stamp = datetime.now(JST).strftime("%m/%d %H:%M")
        notify("余市蒸溜所に空きが出ました",
               f"{stamp} 時点\n" + "\n\n".join(messages))

    if not args.dump:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")


if __name__ == "__main__":
    main()
