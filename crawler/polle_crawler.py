"""
polle_crawler.py
뽈레 맛집 리뷰 크롤러

사용법:
  python polle_crawler.py --query "성수동" --limit 30
  python polle_crawler.py --ids "5c2eQA" "1oZ90R"

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


# ── 설정 ──────────────────────────────────────────────────────────────
OUTPUT_DIR = Path("data/raw/polle")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
BASE_URL = "https://polle.com"

# 뽈레 어뷰징 필터
# 신규 계정 의심: 가입 30일 이내 + 리뷰 3건 이하
NEW_ACCOUNT_DAYS = 30
NEW_ACCOUNT_REVIEW_THRESHOLD = 3
# 인플루언서 의심 키워드 (협찬 표기)
AD_KEYWORDS = ["협찬", "제공받", "광고", "초대", "무료"]

# 영업 상태 텍스트 — 가게명으로 오인되는 문자열 제외
STATUS_WORDS = {
    "영업마감", "영업 마감", "곧 영업마감", "곧 영업 마감",
    "휴무일", "임시휴업", "폐업", "영업 전", "영업전",
    "브레이크타임", "오픈 예정", "준비중",
}
# 뽈레 기준 완화: 리뷰 10건 이상이면 유효 (카카오 50건보다 낮게)
MIN_REVIEWS_FOR_VERDICT = 10
# 긍정률 기준도 완화 (카카오 75% → 뽈레 70%)
POSITIVE_RATE_THRESHOLD = 70


# ── 장소 검색 ──────────────────────────────────────────────────────────
async def search_places(query: str, limit: int = 30) -> list[dict]:
    """polle.com 검색으로 장소 목록 수집"""
    places = []
    print(f"[polle] '{query}' 검색 중...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # Polle는 SPA이므로 여러 URL 패턴 시도
        SEARCH_URLS = [
            f"{BASE_URL}/search?q={query.replace(' ', '+')}",
            f"{BASE_URL}/search?keyword={query.replace(' ', '+')}",
            f"{BASE_URL}/places?q={query.replace(' ', '+')}",
        ]
        for url in SEARCH_URLS:
            try:
                await page.goto(url, wait_until="load", timeout=20000)
                await asyncio.sleep(4)   # SPA 렌더링 대기
                test = await page.query_selector("a[href*='/place/']")
                if test:
                    print(f"  [polle] URL 성공: {url}")
                    break
            except:
                continue

        # place 링크를 포함하는 모든 a 태그 수집
        LINK_SELECTORS = [
            "a[href*='/place/']",
            ".place_item a",
            ".search_result_item a",
            "li.place a",
            "[class*='place'] a",
        ]
        items = []
        for sel in LINK_SELECTORS:
            try:
                await page.wait_for_selector(sel, timeout=5000)
                items = await page.query_selector_all(sel)
                if items:
                    print(f"  [polle] 셀렉터 '{sel}' 로 {len(items)}개 항목 발견")
                    break
            except:
                continue

        if not items:
            print(f"  [polle] 장소 목록을 찾지 못함 (현재 URL: {page.url})")
            # 스크린샷 저장으로 디버그
            try:
                debug_path = OUTPUT_DIR.parent / "polle_debug.png"
                await page.screenshot(path=str(debug_path))
                print(f"  [polle] 디버그 스크린샷: {debug_path}")
            except:
                pass

        seen = set()
        for item in items:
            try:
                href = await item.get_attribute("href") or ""
                m = re.search(r'/place/([^/?#]+)', href)
                if not m:
                    continue
                place_id = m.group(1)
                if place_id in seen:
                    continue
                seen.add(place_id)

                # 이름: 직접 텍스트 또는 자식 요소에서, 영업상태 텍스트 제외
                name = ""
                for sel in [".place_name", ".name", "h3", "h2", "strong"]:
                    el = await item.query_selector(sel)
                    if el:
                        candidate = (await el.inner_text()).strip()
                        if candidate and candidate not in STATUS_WORDS:
                            name = candidate
                            break
                if not name:
                    full_text = (await item.inner_text()).strip()
                    for line in full_text.split('\n'):
                        line = line.strip()
                        if line and line not in STATUS_WORDS and len(line) >= 2:
                            name = line
                            break

                if place_id:
                    places.append({"id": place_id, "name": name, "url": f"{BASE_URL}{href}"})

                if len(places) >= limit:
                    break
            except:
                continue

        await browser.close()

    print(f"[polle] {len(places)}개 장소 발견")
    return places


# ── 개별 장소 크롤링 ──────────────────────────────────────────────────
async def crawl_place(place_id: str, browser) -> dict | None:
    """
    polle.com/place/{id} 에서 리뷰 수집
    뽈레는 웹에서 리뷰 내용이 공개되므로 직접 파싱 가능
    """
    # URL 인코딩된 가게명 포함 또는 ID만으로도 리다이렉트
    url = f"{BASE_URL}/place/{place_id}"
    page = await browser.new_page()

    try:
        await page.goto(url, wait_until="load", timeout=20000)
        await asyncio.sleep(1.5)

        # 앱으로 열기 배너 닫기
        try:
            close_btn = await page.query_selector("a.close, .app_banner .close")
            if close_btn:
                await close_btn.click()
        except:
            pass

        # 기본 정보
        name = await _get_text(page, "h2, .place_name, .tit")
        category = await _get_text(page, ".category, .place_type")
        address = await _get_text(page, ".address, .place_address")
        rating_text = await _get_text(page, ".rating, .score, .grade")
        rating = _parse_float(rating_text)

        # 추천/좋음/보통/별로 카운트
        counts = await _parse_reaction_counts(page)

        # 리뷰 파싱
        reviews = await _parse_reviews(page)

        # 어뷰징 필터
        filtered = filter_reviews(reviews)

        result = {
            "id": place_id,
            "name": name,
            "category": category,
            "address": address,
            "polle_rating": rating,
            "reactions": counts,
            "total_reviews": len(reviews),
            "filtered_reviews": len(filtered["valid"]),
            "ad_count": filtered["ad_count"],
            "new_account_count": filtered["new_account_count"],
            "positive_rate": filtered["positive_rate"],
            "reviews": filtered["valid"],
            "crawled_at": datetime.now().isoformat()
        }

        print(f"  ✓ {name} — 리뷰 {len(reviews)}건 → 유효 {len(filtered['valid'])}건")
        return result

    except Exception as e:
        print(f"  ✗ {place_id} 실패: {e}")
        return None
    finally:
        await page.close()


async def _get_text(page, selector: str) -> str:
    try:
        el = await page.query_selector(selector)
        return (await el.inner_text()).strip() if el else ""
    except:
        return ""


async def _parse_reaction_counts(page) -> dict:
    """추천/좋음/보통/별로 반응 수 파싱"""
    counts = {"추천": 0, "좋음": 0, "보통": 0, "별로": 0}
    try:
        labels = await page.query_selector_all(".reaction_label, .eval_label")
        values = await page.query_selector_all(".reaction_count, .eval_count")
        for label_el, value_el in zip(labels, values):
            label = (await label_el.inner_text()).strip()
            value = _parse_int(await value_el.inner_text())
            if label in counts:
                counts[label] = value
    except:
        pass
    return counts


async def _parse_reviews(page) -> list[dict]:
    """리뷰 목록 파싱 — 뽈레는 포스트 형식"""
    reviews = []
    items = await page.query_selector_all(
        ".review_item, .post_item, .list_review li"
    )

    for item in items:
        try:
            # 작성자
            author_el = await item.query_selector(".author, .nick, .user_name")
            author = (await author_el.inner_text()).strip() if author_el else ""

            # 별점 (뽈레는 0.5~5.0)
            star_el = await item.query_selector(".star, .rating_score, .score_num")
            star_text = await star_el.inner_text() if star_el else "0"
            star = _parse_float(star_text)

            # 리뷰 텍스트
            text_el = await item.query_selector(
                ".review_text, .post_text, .content, .description"
            )
            text = (await text_el.inner_text()).strip() if text_el else ""

            # 작성일 (신규 계정 판단에 사용)
            date_el = await item.query_selector(".date, .created_at, .time")
            date = (await date_el.inner_text()).strip() if date_el else ""

            # 작성자 리뷰 수 (계정 신뢰도 판단)
            author_review_el = await item.query_selector(".author_review_count, .post_count")
            author_review_count = _parse_int(
                await author_review_el.inner_text() if author_review_el else "0"
            )

            if star > 0 or text:
                reviews.append({
                    "author": author,
                    "star": star,
                    "text": text,
                    "date": date,
                    "author_review_count": author_review_count
                })
        except:
            continue

    return reviews


def _parse_float(text: str) -> float:
    try:
        return float(re.sub(r'[^\d.]', '', text))
    except:
        return 0.0


def _parse_int(text: str) -> int:
    try:
        return int(re.sub(r'[^\d]', '', text) or "0")
    except:
        return 0


# ── 어뷰징 필터링 ──────────────────────────────────────────────────────
def filter_reviews(reviews: list[dict]) -> dict:
    """
    뽈레 특화 필터:
    1. 광고성 키워드 제거
    2. 신규 계정 의심 리뷰 제거 (리뷰 수 3건 이하인 작성자)
    """
    ad_count = 0
    new_account_count = 0
    valid = []

    for r in reviews:
        # 광고 키워드
        if any(kw in r["text"] for kw in AD_KEYWORDS):
            ad_count += 1
            continue

        # 신규/일회성 계정 의심
        if r["author_review_count"] <= NEW_ACCOUNT_REVIEW_THRESHOLD and r["star"] >= 4.5:
            new_account_count += 1
            continue

        valid.append(r)

    # 긍정률: 뽈레는 4.0점 이상을 긍정으로
    positive = [r for r in valid if r["star"] >= 4.0]
    positive_rate = round(len(positive) / len(valid) * 100) if valid else 0

    return {
        "valid": valid,
        "ad_count": ad_count,
        "new_account_count": new_account_count,
        "positive_rate": positive_rate
    }


# ── 메인 ──────────────────────────────────────────────────────────────
async def main(args):
    place_ids = list(args.ids or [])

    if args.query:
        places = await search_places(args.query, args.limit)
        place_ids.extend([p["id"] for p in places])

    if not place_ids:
        print("장소 ID 또는 검색어를 입력하세요.")
        return

    print(f"\n[polle] {len(place_ids)}개 장소 크롤링 시작\n")
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for pid in place_ids:
            r = await crawl_place(str(pid), browser)
            if r:
                results.append(r)
            await asyncio.sleep(1.5)
        await browser.close()

    out_path = OUTPUT_DIR / f"polle_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n[polle] 완료 — {len(results)}개 저장: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="뽈레 크롤러")
    parser.add_argument("--query", type=str, help="검색어 (예: 성수동)")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--ids", nargs="+", help="뽈레 장소 ID (예: 5c2eQA)")
    args = parser.parse_args()
    asyncio.run(main(args))
