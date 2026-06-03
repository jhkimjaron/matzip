"""
google_crawler.py
구글맵 장소 + 리뷰 크롤러

사용법:
  python google_crawler.py --query "마포구 맛집" --limit 30
  python google_crawler.py --ids "ChIJN1t_tDeuEmsRUsoyG83frY4"

의존성:
  pip install playwright asyncio
  playwright install chromium
"""

import asyncio
import json
import re
import argparse
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright


OUTPUT_DIR = Path("data/raw/google")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 구글맵은 모든 업소에 별점이 있으므로 별점 기준 유지
AD_KEYWORDS = ["협찬", "제공받", "광고", "체험단", "초대받아", "무료로", "서포터즈"]

# 영업 상태 텍스트 — 가게명으로 오인되는 문자열 제외
STATUS_WORDS = {
    "영업마감", "영업 마감", "곧 영업마감", "곧 영업 마감",
    "휴무일", "임시휴업", "폐업", "영업 전", "영업전",
    "브레이크타임", "오픈 예정", "준비중",
    "closes soon", "closed temporarily", "permanently closed",
}
# 일회성 리뷰어: 리뷰 1~2건 + 5점 → 칭찬봇 의심
PRAISE_BOT_THRESHOLD = 2
# 구글 긍정 기준: 4점 이상
POSITIVE_STAR = 4.0


async def search_places(query: str, limit: int = 30) -> list[dict]:
    """구글맵 검색으로 장소 목록 수집"""
    places = []
    print(f"[google] '{query}' 검색 중...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="ko-KR",
            extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9"}
        )
        page = await context.new_page()

        encoded = query.replace(" ", "+")
        await page.goto(
            f"https://www.google.com/maps/search/{encoded}",
            wait_until="domcontentloaded",
            timeout=30000
        )
        # 구글맵은 networkidle이 절대 충족되지 않으므로 명시적 대기
        await asyncio.sleep(5)

        # 검색 결과 피드 대기
        FEED_SELECTORS = ['[role="feed"]', '.m6QErb', 'div[aria-label*="결과"]']
        result_panel = None
        for sel in FEED_SELECTORS:
            try:
                await page.wait_for_selector(sel, timeout=8000)
                result_panel = await page.query_selector(sel)
                if result_panel:
                    break
            except:
                continue

        # 스크롤로 더 많은 결과 로드
        if result_panel:
            for _ in range(5):
                await result_panel.evaluate("el => el.scrollBy(0, 800)")
                await asyncio.sleep(0.8)

        # 장소 카드 파싱
        # .Nv2PK = 카드 컨테이너, a.hfpxzc = 카드 메인 링크
        # a[href*="/maps/place/"] 는 포토링크 등 비카드까지 포함하므로 카드 단위로 접근
        items = []
        CARD_SELS = ['.Nv2PK', 'div.THOPZb', 'div[data-result-index]', '.lI9IFe']
        for sel in CARD_SELS:
            items = await page.query_selector_all(sel)
            if items:
                print(f"  [google] 셀렉터 '{sel}' 로 {len(items)}개 카드 발견")
                break

        if not items:
            # 폴백: 링크 기반 (중복 가능성 있으나 없는 것보다 낫다)
            items = await page.query_selector_all('a[href*="/maps/place/"]')
            if items:
                print(f"  [google] 폴백 셀렉터로 {len(items)}개 항목 발견")
            else:
                print(f"  [google] 장소 목록을 찾지 못함 (현재 URL: {page.url})")

        seen_ids = set()
        for item in items:
            try:
                # 카드 내 메인 링크 href 추출
                href = ""
                for link_sel in ['a.hfpxzc', 'a[href*="/maps/place/"]', 'a[href*="/maps/"]']:
                    a = await item.query_selector(link_sel)
                    if a:
                        href = await a.get_attribute("href") or ""
                        if "/maps/place/" in href:
                            break
                # 요소 자체가 a 태그인 경우
                if not href:
                    href = await item.get_attribute("href") or ""

                if not href or "/maps/place/" not in href:
                    continue

                place_id_m = re.search(r'/place/([^/?&@\s]+)', href)
                if not place_id_m:
                    continue
                place_id = place_id_m.group(1)
                if place_id in seen_ids:
                    continue
                seen_ids.add(place_id)

                visit_url = href if href.startswith("http") else f"https://www.google.com{href}"

                # 이름 — screen_out 제거 후 추출
                name = await item.evaluate('''el => {
                    const sels = [
                        ".fontHeadlineSmall", ".qBF1Pd", "h3",
                        ".NrDZNb", "[data-value]"
                    ];
                    for (const sel of sels) {
                        const child = el.querySelector(sel);
                        if (!child) continue;
                        const clone = child.cloneNode(true);
                        clone.querySelectorAll(".screen_out, .sr_only, [aria-hidden]").forEach(e => e.remove());
                        const t = clone.innerText.trim();
                        if (t && t.length >= 2) return t;
                    }
                    // 전체 텍스트 첫 줄
                    const lines = el.innerText.split("\\n").map(s=>s.trim()).filter(Boolean);
                    return lines[0] || "";
                }''')
                name = (name or "").strip()
                if name in STATUS_WORDS:
                    name = ""

                if name and place_id:
                    places.append({"id": place_id, "name": name, "url": visit_url})

                if len(places) >= limit:
                    break
            except:
                continue

        await browser.close()

    print(f"[google] {len(places)}개 장소 발견")
    return places


async def crawl_place(place_info: dict, browser) -> dict | None:
    """
    구글맵 개별 장소 페이지에서 정보 + 리뷰 수집
    """
    url = place_info.get("url") or f"https://www.google.com/maps/place/{place_info['id']}"
    page = await browser.new_page()

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # 기본 정보
        name = await _get_text_multi(page, [
            'h1.DUwDvf', 'h1[data-attrid]', '.fontHeadlineLarge'
        ])
        if not name:
            name = place_info.get("name", "")

        category = await _get_text_multi(page, [
            'button[jsaction*="category"]', '.DkEaL', '.mgr77e'
        ])

        address = await _get_text_multi(page, [
            'button[data-item-id="address"]', '[data-tooltip="주소 복사"] .Io6YTe',
            '.rogA2c .Io6YTe'
        ])

        # 평점 + 리뷰 수
        rating_text = await _get_text_multi(page, [
            '.F7nice span[aria-hidden]', '.MW4etd', 'span.ceNzKf'
        ])
        rating = _parse_float(rating_text)

        review_count_text = await _get_text_multi(page, [
            '.F7nice span[aria-label]', '.UY7F9', 'span[aria-label*="개의 리뷰"]'
        ])
        total_count = _parse_int(review_count_text)

        # 좌표 (URL에서 추출)
        lat, lng = _extract_coords_from_url(page.url)

        # 리뷰 탭 클릭
        await _click_reviews_tab(page)
        await asyncio.sleep(1.5)

        # 한국어 정렬 (최신순 or 관련성순)
        await _load_all_reviews(page)

        reviews = await _parse_reviews(page)
        filtered = filter_reviews(reviews)

        result = {
            "id": place_info["id"],
            "name": name,
            "category": category,
            "address": address,
            "lat": lat,
            "lng": lng,
            "google_rating": rating,
            "total_reviews": total_count or len(reviews),
            "filtered_reviews": len(filtered["valid"]),
            "praise_bot_count": filtered["praise_bot_count"],
            "ad_count": filtered["ad_count"],
            "positive_rate": filtered["positive_rate"],
            "reviews": filtered["valid"],
            "crawled_at": datetime.now().isoformat()
        }

        print(f"  ✓ {name} ★{rating} — 리뷰 {len(reviews)}건 → 유효 {len(filtered['valid'])}건")
        return result

    except Exception as e:
        print(f"  ✗ {place_info.get('name', place_info['id'])} 실패: {e}")
        return None
    finally:
        await page.close()


async def _get_text_multi(page, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if text:
                    return text
        except:
            continue
    return ""


def _extract_coords_from_url(url: str) -> tuple[float, float]:
    try:
        m = re.search(r'@([-\d.]+),([-\d.]+)', url)
        if m:
            return float(m.group(1)), float(m.group(2))
        m = re.search(r'!3d([-\d.]+)!4d([-\d.]+)', url)
        if m:
            return float(m.group(1)), float(m.group(2))
    except:
        pass
    return 0.0, 0.0


async def _click_reviews_tab(page):
    try:
        buttons = await page.query_selector_all('button[role="tab"]')
        for btn in buttons:
            text = (await btn.inner_text()).strip()
            if "리뷰" in text:
                await btn.click()
                await asyncio.sleep(1)
                return
        # 영문 fallback
        for btn in buttons:
            text = (await btn.inner_text()).strip()
            if "Review" in text:
                await btn.click()
                await asyncio.sleep(1)
                return
    except:
        pass


async def _load_all_reviews(page, max_clicks: int = 10):
    for _ in range(max_clicks):
        try:
            # 리뷰 컨테이너 스크롤
            container = await page.query_selector('.m6QErb[data-scroll-hide]')
            if container:
                await container.evaluate("el => el.scrollBy(0, 1000)")
                await asyncio.sleep(0.7)
            else:
                break
        except:
            break


async def _parse_reviews(page) -> list[dict]:
    reviews = []
    items = await page.query_selector_all('.jftiEf, .GHT2ce')

    for item in items:
        try:
            # 별점 (aria-label에서 추출)
            star_el = await item.query_selector('span[role="img"]')
            star_label = await star_el.get_attribute("aria-label") if star_el else ""
            star_m = re.search(r'(\d+(?:\.\d+)?)', star_label or "")
            star = float(star_m.group(1)) if star_m else 0.0

            # 리뷰 텍스트
            text_el = await item.query_selector('.wiI7pd, .MyEned span')
            text = (await text_el.inner_text()).strip() if text_el else ""

            # 더보기 버튼 (펼쳐진 경우 이미 처리됨)
            date_el = await item.query_selector('.rsqaWe, .DU9Pgb')
            date = (await date_el.inner_text()).strip() if date_el else ""

            # 작성자 리뷰 수 (프로필에서)
            reviewer_count_el = await item.query_selector('.RfnDt span, .y7Z2oe')
            reviewer_count_text = await reviewer_count_el.inner_text() if reviewer_count_el else "0"
            reviewer_count = _parse_int(reviewer_count_text)

            if star > 0 or text:
                reviews.append({
                    "star": star,
                    "text": text,
                    "date": date,
                    "reviewer_review_count": reviewer_count
                })
        except:
            continue

    return reviews


def _parse_float(text: str) -> float:
    try:
        cleaned = re.sub(r'[^\d.]', '', text.replace(',', '.'))
        return float(cleaned) if cleaned else 0.0
    except:
        return 0.0


def _parse_int(text: str) -> int:
    try:
        cleaned = re.sub(r'[^\d]', '', text)
        return int(cleaned) if cleaned else 0
    except:
        return 0


def filter_reviews(reviews: list[dict]) -> dict:
    """
    구글맵 특화 필터 (별점 기준 유지 - 모든 업소에 별점 있음):
    1. 칭찬봇 제거: 리뷰 수 2건 이하 + 5점 리뷰
    2. 광고성 키워드 제거
    """
    valid = []
    praise_bot_count = 0
    ad_count = 0

    for r in reviews:
        # 광고 키워드
        if any(kw in r.get("text", "") for kw in AD_KEYWORDS):
            ad_count += 1
            continue

        # 칭찬봇 (별점 기준 유지)
        if (r["reviewer_review_count"] <= PRAISE_BOT_THRESHOLD
                and r["star"] >= 4.8):
            praise_bot_count += 1
            continue

        valid.append(r)

    positive = [r for r in valid if r["star"] >= POSITIVE_STAR]
    positive_rate = round(len(positive) / len(valid) * 100) if valid else 0

    return {
        "valid": valid,
        "praise_bot_count": praise_bot_count,
        "ad_count": ad_count,
        "positive_rate": positive_rate
    }


async def main(args):
    place_infos = []

    if args.ids:
        place_infos = [{"id": pid, "name": "", "url": ""} for pid in args.ids]

    if args.query:
        found = await search_places(args.query, args.limit)
        place_infos.extend(found)

    if not place_infos:
        print("장소 ID 또는 검색어를 입력해주세요.")
        return

    print(f"\n[google] {len(place_infos)}개 장소 크롤링 시작\n")
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="ko-KR",
            extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9"}
        )
        browser_ctx = context

        for info in place_infos:
            page = await browser_ctx.new_page()
            result = await crawl_place(info, browser_ctx)
            if result:
                results.append(result)
            await asyncio.sleep(2)

        await browser_ctx.close()

    out_path = OUTPUT_DIR / f"google_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n[google] 완료 — {len(results)}개 저장: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="구글맵 맛집 크롤러")
    parser.add_argument("--query", type=str, help="검색어 (예: 마포구 맛집)")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--ids", nargs="+", help="구글맵 place ID 직접 지정")
    args = parser.parse_args()
    asyncio.run(main(args))
