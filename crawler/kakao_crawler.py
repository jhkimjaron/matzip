"""
kakao_crawler.py
카카오맵 장소 + 리뷰 크롤러

사용법:
  python kakao_crawler.py --query "마포구 맛집" --limit 50
  python kakao_crawler.py --ids 12345678 87654321

의존성:
  pip install playwright aiohttp asyncio
  playwright install chromium
"""

import asyncio
import json
import re
import argparse
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright


# ── 설정 ──────────────────────────────────────────────────────────────
OUTPUT_DIR = Path("data/raw/kakao")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 칭찬봇 판정: 리뷰 작성 수 3건 이하
PRAISE_BOT_THRESHOLD = 3
# 광고성 키워드
AD_KEYWORDS = ["제공받았습니다", "협찬", "무료로", "초대받아", "체험단", "이벤트 당첨"]
# 카카오는 별점 없는 업소가 있으므로 별점 관련 기준 미사용
# 긍정 판단 기준: 별점이 있을 때만 4점 이상으로 판정
POSITIVE_STAR = 4.0

# 영업 상태 텍스트 — 가게명으로 오인되는 문자열 제외
STATUS_WORDS = {
    "영업마감", "영업 마감", "곧 영업마감", "곧 영업 마감",
    "휴무일", "임시휴업", "폐업", "영업 전", "영업전",
    "브레이크타임", "오픈 예정", "준비중",
}


# ── 장소 검색 ──────────────────────────────────────────────────────────
async def search_places(query: str, limit: int = 50) -> list[dict]:
    """카카오맵 검색으로 장소 목록 수집"""
    places = []
    print(f"[kakao] '{query}' 검색 중...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        encoded = query.replace(" ", "+")
        await page.goto(
            f"https://map.kakao.com/?q={encoded}",
            wait_until="load", timeout=30000
        )
        await asyncio.sleep(3)

        # 여러 버전의 셀렉터 시도
        LIST_SELECTORS = [
            "#info\\.search\\.place\\.list li",
            ".placelist li",
            "ul.placelist > li",
            ".wrap_place_item",
        ]
        items = []
        for sel in LIST_SELECTORS:
            try:
                await page.wait_for_selector(sel, timeout=5000)
                items = await page.query_selector_all(sel)
                if items:
                    print(f"  [kakao] 셀렉터 '{sel}' 로 {len(items)}개 항목 발견")
                    break
            except:
                continue

        if not items:
            # 디버그: 현재 URL과 일부 HTML 출력
            print(f"  [kakao] 장소 목록을 찾지 못함 (현재 URL: {page.url})")
            html_snippet = (await page.content())[:500]
            print(f"  [kakao] HTML 앞부분: {html_snippet[:200]}")

        for item in items[:limit]:
            try:
                # 이름 — JS로 screen_out 제거 후 추출
                name = await item.evaluate('''el => {
                    const sels = [".link_name", ".place_name", "a.name", ".tit"];
                    for (const sel of sels) {
                        const child = el.querySelector(sel);
                        if (!child) continue;
                        const clone = child.cloneNode(true);
                        clone.querySelectorAll(".screen_out, .sr_only").forEach(e => e.remove());
                        const t = clone.innerText.trim();
                        if (t) return t;
                    }
                    // 전체 텍스트에서 첫 유효 줄
                    const lines = el.innerText.split("\\n").map(s => s.trim()).filter(Boolean);
                    return lines[0] || "";
                }''')
                name = name.strip() if name else ""
                if name in STATUS_WORDS:
                    name = ""

                # 주소
                address = ""
                for sel in [".addr", ".address", ".txt_address"]:
                    el = await item.query_selector(sel)
                    if el:
                        address = (await el.inner_text()).strip()
                        if address:
                            break

                # place_id: data-id 또는 href에서 추출
                place_id = await item.get_attribute("data-id") or ""
                if not place_id:
                    a = await item.query_selector("a[href*='place']")
                    if a:
                        href = await a.get_attribute("href") or ""
                        m = re.search(r'/(\d{6,})', href)
                        if m:
                            place_id = m.group(1)

                if name and place_id:
                    places.append({"id": place_id, "name": name, "address": address})
            except Exception as e:
                print(f"  [warn] 파싱 오류: {e}")

        await browser.close()

    print(f"[kakao] {len(places)}개 장소 발견")
    return places


# ── 개별 장소 리뷰 크롤링 ──────────────────────────────────────────────
async def crawl_place(place_id: str, browser) -> dict | None:
    """
    place.map.kakao.com/{id} 에서 리뷰 데이터 수집
    """
    url = f"https://place.map.kakao.com/{place_id}"
    page = await browser.new_page()

    try:
        await page.goto(url, wait_until="load", timeout=20000)
        await asyncio.sleep(1.5)

        # 기본 정보 — JS로 screen_out(스크린리더) 스팬 제거 후 텍스트 추출
        name = await page.evaluate('''() => {
            const sels = ["h2.tit_place", ".tit_place", "h2[class*='place']", ".place_name", ".tit_detail"];
            for (const sel of sels) {
                const el = document.querySelector(sel);
                if (!el) continue;
                const clone = el.cloneNode(true);
                clone.querySelectorAll(".screen_out, .sr_only, [aria-hidden='true']").forEach(e => e.remove());
                const t = clone.innerText.trim();
                if (t) return t;
            }
            return "";
        }''')
        # 여전히 없으면 페이지 제목에서 추출
        if not name or name in STATUS_WORDS:
            title = await page.title()
            name = title.split(" - ")[0].split(" | ")[0].strip()

        category = await _get_text(page, ".category_group, .txt_category, .link_category")
        address = await _get_text(page, ".txt_address, .address_new, .addr_area")
        rating_text = await _get_text(page, ".num_rate, .rating_score")
        rating = float(rating_text) if rating_text and rating_text.replace('.','').isdigit() else 0.0

        # 위치 좌표
        lat, lng = await _extract_coords(page)

        # 리뷰 탭 클릭 (탭 방식 페이지에서 리뷰 섹션 활성화)
        await _click_review_tab(page)
        await asyncio.sleep(1)

        # 리뷰 전체 로드 (더보기 클릭)
        await _load_all_reviews(page)

        # 리뷰 파싱
        reviews = await _parse_reviews(page)

        # 어뷰징 필터링
        filtered = filter_reviews(reviews)

        result = {
            "id": place_id,
            "name": name,
            "category": category,
            "address": address,
            "lat": lat,
            "lng": lng,
            "kakao_rating": rating,
            "total_reviews": len(reviews),
            "filtered_reviews": len(filtered["valid"]),
            "rated_review_count": filtered["rated_review_count"],
            "praise_bot_count": filtered["praise_bot_count"],
            "positive_rate": filtered["positive_rate"],
            "bubble_level": filtered["bubble_level"],
            "reviews": filtered["valid"],
            "crawled_at": datetime.now().isoformat()
        }

        print(f"  ✓ {name} — 리뷰 {len(reviews)}건 → 유효 {len(filtered['valid'])}건")
        return result

    except Exception as e:
        print(f"  ✗ {place_id} 크롤링 실패: {e}")
        return None
    finally:
        await page.close()


async def _get_text(page, selector: str) -> str:
    try:
        el = await page.query_selector(selector)
        return (await el.inner_text()).strip() if el else ""
    except:
        return ""


async def _extract_coords(page) -> tuple[float, float]:
    """페이지 소스·메타태그에서 위도/경도 추출 (다중 패턴)"""
    try:
        # 1. 메타 태그 (og:latitude / og:longitude)
        lat = await page.evaluate('''() => {
            const m = document.querySelector('meta[property="og:latitude"], meta[name="latitude"]');
            return m ? m.content : "";
        }''')
        lng = await page.evaluate('''() => {
            const m = document.querySelector('meta[property="og:longitude"], meta[name="longitude"]');
            return m ? m.content : "";
        }''')
        if lat and lng:
            return float(lat), float(lng)
    except:
        pass

    try:
        content = await page.content()
        # 2. 다양한 JSON 패턴 시도
        PATTERNS = [
            (r'"lat"\s*:\s*([\d.]+)',       r'"lng"\s*:\s*([\d.]+)'),
            (r'"latitude"\s*:\s*([\d.]+)',   r'"longitude"\s*:\s*([\d.]+)'),
            (r'"mapy"\s*:\s*"([\d.]+)"',     r'"mapx"\s*:\s*"([\d.]+)"'),
            (r'"y"\s*:\s*"([\d.]+)"',        r'"x"\s*:\s*"([\d.]+)"'),
            (r'lat[=:]\s*([\d.]+)',          r'lng[=:]\s*([\d.]+)'),
        ]
        for lat_pat, lng_pat in PATTERNS:
            lat_m = re.search(lat_pat, content)
            lng_m = re.search(lng_pat, content)
            if lat_m and lng_m:
                lat_val = float(lat_m.group(1))
                lng_val = float(lng_m.group(1))
                # 한국 좌표 범위 검증
                if 33 <= lat_val <= 39 and 124 <= lng_val <= 132:
                    return lat_val, lng_val
    except:
        pass

    return 0.0, 0.0


async def _click_review_tab(page):
    """Kakao 장소 페이지 리뷰 탭 클릭"""
    TAB_SELS = [
        "a[href*='review']",
        ".tab_area a",
        ".link_tab",
        "button:has-text('리뷰')",
        "a:has-text('리뷰')",
    ]
    for sel in TAB_SELS:
        try:
            els = await page.query_selector_all(sel)
            for el in els:
                text = (await el.inner_text()).strip()
                if "리뷰" in text:
                    await el.click()
                    await asyncio.sleep(1)
                    return
        except:
            continue


async def _load_all_reviews(page, max_clicks: int = 15):
    """'더보기' 버튼 반복 클릭 또는 스크롤로 전체 리뷰 로드"""
    MORE_BTNS = [
        ".btn_more_review",
        ".more_review button",
        ".link_more_review",
        "a.link_more",
        ".wrap_more_review a",
        "button[class*='more']",
        "a[class*='more'][class*='review']",
    ]
    prev_count = 0
    for _ in range(max_clicks):
        clicked = False
        for sel in MORE_BTNS:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(1.2)
                    clicked = True
                    break
            except:
                continue
        if not clicked:
            # 버튼 없으면 리뷰 컨테이너 스크롤 시도
            try:
                await page.evaluate('''() => {
                    const el = document.querySelector(".cont_review, .list_review, .review_wrap");
                    if (el) el.scrollBy(0, 1000);
                    else window.scrollBy(0, 800);
                }''')
                await asyncio.sleep(0.8)
            except:
                pass
        # 리뷰 수가 늘지 않으면 종료
        cur_count = len(await page.query_selector_all(".review_item, .list_review li"))
        if cur_count == prev_count:
            break
        prev_count = cur_count


async def _parse_reviews(page) -> list[dict]:
    """리뷰 목록 파싱"""
    reviews = []
    # 현재 Kakao Maps 리뷰 컨테이너 셀렉터 (버전별 폴백)
    items = []
    for sel in [
        ".list_evaluation li",   # 최신 평가 리스트
        ".review_item",
        ".list_review li",
        ".cont_review li",
        "[data-type='review']",
    ]:
        items = await page.query_selector_all(sel)
        if items:
            break

    for item in items:
        try:
            # 리뷰어 정보
            reviewer_el = await item.query_selector(".name_reviewer, .nick_name")
            reviewer = await reviewer_el.inner_text() if reviewer_el else "익명"

            # 리뷰어 총 리뷰 수
            review_count_el = await item.query_selector(".num_review, .review_count")
            review_count_text = await review_count_el.inner_text() if review_count_el else "0"
            reviewer_review_count = int(re.sub(r'[^\d]', '', review_count_text) or "0")

            # 리뷰어 평균 별점
            avg_rating_el = await item.query_selector(".avg_rating, .reviewer_avg")
            avg_rating_text = await avg_rating_el.inner_text() if avg_rating_el else "0"
            reviewer_avg_rating = float(avg_rating_text) if avg_rating_text.replace('.','').isdigit() else 0.0

            # 이 가게에 준 별점
            star_el = await item.query_selector(".num_star, .star_score")
            star_text = await star_el.inner_text() if star_el else "0"
            star = float(star_text) if star_text.replace('.','').isdigit() else 0.0

            # 리뷰 텍스트
            text_el = await item.query_selector(".txt_review, .review_text")
            text = await text_el.inner_text() if text_el else ""

            reviews.append({
                "reviewer": reviewer,
                "reviewer_review_count": reviewer_review_count,
                "reviewer_avg_rating": reviewer_avg_rating,
                "star": star,
                "text": text.strip()
            })
        except:
            continue

    return reviews


# ── 어뷰징 필터링 ──────────────────────────────────────────────────────
def filter_reviews(reviews: list[dict]) -> dict:
    """
    카카오맵 어뷰징 필터 (별점 없는 업소 고려):
    1. 칭찬봇 제거: 리뷰 수 3건 이하 + 별점이 있는 경우에만 4.5점 이상 리뷰 제거
    2. 광고성 키워드 포함 리뷰 제거
    별점 기반 Gold/버블 판정은 카카오에서 제거 (별점 없는 업소 누락 방지)
    """
    valid = []
    praise_bot_count = 0

    for r in reviews:
        # 칭찬봇: 별점이 존재할 때만 적용 (0은 별점 없음으로 간주)
        if r["star"] > 0:
            is_praise_bot = (
                r["reviewer_review_count"] <= PRAISE_BOT_THRESHOLD
                and r["star"] >= 4.5
            )
            if is_praise_bot:
                praise_bot_count += 1
                continue

        # 광고성 키워드 필터
        if any(kw in r["text"] for kw in AD_KEYWORDS):
            continue

        valid.append(r)

    # 긍정률: 별점이 있는 리뷰 기준으로만 계산
    rated = [r for r in valid if r["star"] > 0]
    positive = [r for r in rated if r["star"] >= POSITIVE_STAR]
    positive_rate = round(len(positive) / len(rated) * 100) if rated else 0

    # 거품 레벨 (칭찬봇 비율, 참고용)
    praise_bot_ratio = praise_bot_count / len(reviews) if reviews else 0
    if praise_bot_ratio > 0.3:
        bubble_level = "주의"
    elif praise_bot_ratio > 0.15:
        bubble_level = "의심"
    else:
        bubble_level = "깨끗"

    return {
        "valid": valid,
        "praise_bot_count": praise_bot_count,
        "positive_rate": positive_rate,
        "rated_review_count": len(rated),
        "bubble_level": bubble_level
    }


# ── 판별 로직 ──────────────────────────────────────────────────────────
def determine_verdict(kakao: dict, naver: dict, polle: dict) -> str:
    """
    OR 방식: 한 소스라도 통과 시 맛집
    카카오는 별점 없는 업소 포함을 위해 별점 기준 미사용
    """
    kakao_pass = (
        kakao is not None
        and kakao.get("positive_rate", 0) >= 70
        and kakao.get("filtered_reviews", 0) >= 10
        and kakao.get("bubble_level") != "주의"
    )
    naver_pass = (
        naver is not None
        and naver.get("positive_rate", 0) >= 75
        and naver.get("filtered_reviews", 0) >= 30
    )
    polle_pass = (
        polle is not None
        and polle.get("positive_rate", 0) >= 70
        and polle.get("filtered_reviews", 0) >= 10
    )
    cross_pass = (
        kakao is not None and naver is not None
        and kakao.get("positive_rate", 0) >= 60
        and naver.get("positive_rate", 0) >= 60
    )
    # 숨은맛집: 평점 낮지만 긍정 리뷰 높음
    hidden_gem = (
        kakao is not None
        and kakao.get("kakao_rating", 0) > 0
        and kakao.get("kakao_rating", 0) <= 3.7
        and kakao.get("positive_rate", 0) >= 75
        and kakao.get("filtered_reviews", 0) >= 15
    )

    if hidden_gem:
        return "숨은맛집"
    if kakao_pass or naver_pass or polle_pass or cross_pass:
        # 긍정률로 맛집/괜찮음 구분
        rates = []
        if kakao: rates.append(kakao.get("positive_rate", 0))
        if naver: rates.append(naver.get("positive_rate", 0))
        if polle: rates.append(polle.get("positive_rate", 0))
        avg_rate = sum(rates) / len(rates) if rates else 0
        return "맛집" if avg_rate >= 75 else "괜찮음"

    rates = []
    if kakao: rates.append(kakao.get("positive_rate", 0))
    if naver: rates.append(naver.get("positive_rate", 0))
    if polle: rates.append(polle.get("positive_rate", 0))
    avg_rate = sum(rates) / len(rates) if rates else 0

    if avg_rate >= 30:
        return "보통"
    return "주의"


# ── 메인 ──────────────────────────────────────────────────────────────
async def main(args):
    place_ids = args.ids or []

    if args.query:
        places = await search_places(args.query, args.limit)
        place_ids.extend([p["id"] for p in places])

    if not place_ids:
        print("장소 ID 또는 검색어를 입력해주세요.")
        return

    print(f"\n[kakao] {len(place_ids)}개 장소 크롤링 시작\n")
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for place_id in place_ids:
            result = await crawl_place(str(place_id), browser)
            if result:
                results.append(result)
            # 요청 간격 (서버 부하 방지)
            await asyncio.sleep(1.5)
        await browser.close()

    # 저장
    out_path = OUTPUT_DIR / f"kakao_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n[kakao] 완료 — {len(results)}개 저장: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="카카오맵 맛집 크롤러")
    parser.add_argument("--query", type=str, help="검색어 (예: 마포구 맛집)")
    parser.add_argument("--limit", type=int, default=50, help="검색 결과 최대 수")
    parser.add_argument("--ids", nargs="+", help="장소 ID 직접 지정")
    args = parser.parse_args()
    asyncio.run(main(args))
