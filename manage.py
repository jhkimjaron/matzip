"""
manage.py — 맛집지도 통합 관리 CLI

명령어:
  python manage.py scan                   config.json의 areas 스캔
  python manage.py scan --area "강남구 음식점"  단일 지역 스캔
  python manage.py crawl                  DB의 미크롤링/오래된 장소 크롤링
  python manage.py export                 DB → places.json / places.js
  python manage.py push                   GitHub 자동 push
  python manage.py update                 scan + crawl + export + push 한 번에
  python manage.py status                 DB 현황 출력
"""

import asyncio
import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "crawler"))

import db as _db
from naver_crawler import (
    search_places, crawl_place, check_recent_activity,
    _stealth, MIN_REVIEWS,
)
from playwright.async_api import async_playwright

CONFIG_PATH = ROOT / "config.json"
OUTPUT_JSON = ROOT / "data" / "places.json"
OUTPUT_JS   = ROOT / "data" / "places.js"


# ── 설정 로드 ──────────────────────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "limit_per_area": 100,
        "min_reviews": 100,
        "active_days": 90,
        "update_interval_days": 30,
        "min_valid_reviews": 50,
        "areas": [],
    }


# ── SCAN ──────────────────────────────────────────────────────────────
async def cmd_scan(args):
    cfg = load_config()
    areas = [args.area] if args.area else cfg.get("areas", [])
    if not areas:
        print("스캔할 지역이 없습니다. config.json의 areas를 설정하거나 --area 옵션을 사용하세요.")
        return

    limit       = args.limit or cfg["limit_per_area"]
    min_reviews = cfg["min_reviews"]
    active_days = cfg["active_days"]
    cutoff_date = (datetime.now() - timedelta(days=active_days)).strftime("%Y%m%d")

    _db.init_db()
    total_new = 0

    for area in areas:
        print(f"\n[scan] {area}")
        places = await search_places(area, limit=limit, min_reviews=min_reviews)

        added = 0
        for p in places:
            # API 제공 날짜로 활성도 필터 (있을 경우)
            lrd = p.get("last_review_date", "")
            if lrd and lrd < cutoff_date:
                print(f"  [skip] {p['name']} — 마지막 리뷰 {lrd} (3개월 초과)")
                continue

            _db.upsert_scan(p, area)
            added += 1

        print(f"  → {added}개 저장 (전체 발견 {len(places)}개)")
        total_new += added

    st = _db.status()
    print(f"\n[scan 완료] 누적 {st['total']}곳 (스캔대기 {st['scan_only']}곳, 크롤링완료 {st['crawled']}곳)")


# ── CRAWL ──────────────────────────────────────────────────────────────
async def cmd_crawl(args):
    cfg = load_config()
    _db.init_db()

    older_than  = args.older_than or cfg["update_interval_days"]
    active_days = cfg["active_days"]
    min_valid   = cfg["min_valid_reviews"]
    cutoff_date = (datetime.now() - timedelta(days=active_days)).strftime("%Y%m%d")

    pending = _db.get_pending_crawl(older_than_days=older_than)
    if not pending:
        print("크롤링할 장소가 없습니다.")
        return

    print(f"[crawl] 대상 {len(pending)}곳 (스캔대기 + {older_than}일 이상 된 곳)")

    done = skip_active = skip_valid = 0

    async with _stealth.use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="ko-KR",
            extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9"},
        )

        for i, place in enumerate(pending, 1):
            pid  = place["id"]
            name = place["name"] or pid
            print(f"\n[{i}/{len(pending)}] {name}")

            # ── 활성도 체크 (API 날짜 없으면 경량 방문) ──
            lrd = place.get("last_review_date", "")
            if lrd:
                # DB에 저장된 API 날짜로 확인
                if lrd < cutoff_date:
                    print(f"  [skip] 마지막 리뷰 {lrd} — {active_days}일 초과")
                    skip_active += 1
                    continue
            else:
                # 경량 방문으로 확인 (~6초)
                active = await check_recent_activity(pid, ctx, active_days)
                if not active:
                    print(f"  [skip] 최근 {active_days}일 내 리뷰 없음")
                    skip_active += 1
                    continue

            # ── 전체 크롤링 ──
            place_for_crawl = {
                "id":                   pid,
                "name":                 name,
                "category":             place.get("category", ""),
                "address":              place.get("address", ""),
                "x":                    str(place.get("lng", 0)),
                "y":                    str(place.get("lat", 0)),
                "review_count":         place.get("visitor_total", 0) + place.get("blog_total", 0),
                "visitor_review_count": place.get("visitor_total", 0),
                "blog_review_count":    place.get("blog_total", 0),
                "business_status":      place.get("business_status", ""),
                "business_hours_today": "",
                "break_time_today":     "",
                "last_order":           "",
            }
            result = await crawl_place(place_for_crawl, ctx)
            if not result:
                continue

            # 최종 유효리뷰 필터
            if result["filtered_reviews"] < min_valid:
                print(f"  [skip] 유효리뷰 {result['filtered_reviews']}건 < {min_valid}건")
                skip_valid += 1
                continue

            result["area"] = place.get("area", "")
            _db.upsert_crawl(result)
            done += 1
            print(f"  [저장] {name} — 유효리뷰 {result['filtered_reviews']}건, 긍정률 {result.get('positive_rate',0)}%")

            await asyncio.sleep(1)

        await ctx.close()

    print(f"\n[crawl 완료] 저장 {done}곳 / 활성도제외 {skip_active}곳 / 리뷰부족 {skip_valid}곳")


# ── EXPORT ────────────────────────────────────────────────────────────
def cmd_export(args):
    cfg = load_config()
    _db.init_db()
    places = _db.export_places(min_valid=cfg["min_valid_reviews"])

    if not places:
        print("내보낼 데이터가 없습니다.")
        return

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(
        json.dumps(places, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    OUTPUT_JS.write_text(
        "window.PLACES_DATA = " + json.dumps(places, ensure_ascii=False, indent=2) + ";\n",
        encoding="utf-8",
    )
    print(f"[export] {len(places)}곳 → {OUTPUT_JSON}")
    print(f"[export] {len(places)}곳 → {OUTPUT_JS}")


# ── PUSH ──────────────────────────────────────────────────────────────
def cmd_push(args):
    today = datetime.now().strftime("%Y-%m-%d")
    cmds = [
        ["git", "add", "data/places.json", "data/places.js"],
        ["git", "commit", "-m", f"데이터 업데이트: {today}"],
        ["git", "push"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, cwd=str(ROOT))
        if result.returncode != 0:
            print(f"[push] 실패: {' '.join(cmd)}")
            return
    print(f"[push] GitHub 업로드 완료 — 1~2분 후 Pages 반영")


# ── STATUS ────────────────────────────────────────────────────────────
def cmd_status(args):
    _db.init_db()
    st = _db.status()
    print(f"\n{'='*40}")
    print(f"  전체:          {st['total']:>5}곳")
    print(f"  스캔대기:      {st['scan_only']:>5}곳")
    print(f"  크롤링완료:    {st['crawled']:>5}곳")
    print(f"  최종등재가능:  {st['qualified']:>5}곳 (유효리뷰 50건+)")
    print(f"{'='*40}")
    print("  지역별:")
    for area, cnt in st["areas"]:
        print(f"    {area:<30} {cnt:>4}곳")
    print()


# ── UPDATE (scan + crawl + export + push) ─────────────────────────────
async def cmd_update(args):
    print("=== UPDATE 시작 ===")
    await cmd_scan(args)
    await cmd_crawl(args)
    cmd_export(args)
    if not args.no_push:
        cmd_push(args)
    print("=== UPDATE 완료 ===")


# ── 진입점 ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="맛집지도 관리 CLI")
    sub = parser.add_subparsers(dest="cmd")

    # scan
    p_scan = sub.add_parser("scan", help="지역 스캔")
    p_scan.add_argument("--area",  type=str, help="단일 지역 (예: '강남구 음식점')")
    p_scan.add_argument("--limit", type=int, help="지역당 최대 수집 수")

    # crawl
    p_crawl = sub.add_parser("crawl", help="DB의 장소 전체 크롤링")
    p_crawl.add_argument("--older-than", type=int, dest="older_than",
                         help="N일 이상 된 장소 재크롤링 (기본: config 값)")

    # export
    sub.add_parser("export", help="DB → places.json/js 생성")

    # push
    sub.add_parser("push", help="GitHub push")

    # status
    sub.add_parser("status", help="DB 현황 출력")

    # update
    p_update = sub.add_parser("update", help="scan+crawl+export+push 일괄 실행")
    p_update.add_argument("--area",       type=str)
    p_update.add_argument("--limit",      type=int)
    p_update.add_argument("--older-than", type=int, dest="older_than")
    p_update.add_argument("--no-push",    action="store_true", dest="no_push")

    args = parser.parse_args()

    if args.cmd == "scan":
        asyncio.run(cmd_scan(args))
    elif args.cmd == "crawl":
        asyncio.run(cmd_crawl(args))
    elif args.cmd == "export":
        cmd_export(args)
    elif args.cmd == "push":
        cmd_push(args)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "update":
        asyncio.run(cmd_update(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
