"""
naver_crawler.py — 네이버 플레이스 크롤러 (stealth + GraphQL 인터셉트)

수집 항목:
  - 방문자 리뷰: 최신순 100건 이상 (실제 텍스트 분석)
  - 블로그 리뷰: 최신순 50건 이상 (실제 텍스트 분석)
  - 영업시간·휴무일·브레이크타임 (홈 페이지 GraphQL)
  - 좌표·카테고리·주소 (allSearch API)

광고 필터:
  - 광고·협찬·이벤트 키워드 포함 리뷰 제거 (방문자·블로그 각각)
  - 중복 복붙 제거

사용법:
  python naver_crawler.py --query "천안 맛집" --limit 30
"""

import asyncio
import json
import re
import argparse
from collections import Counter
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

try:
    from kiwipiepy import Kiwi as _KiwiClass
    _kiwi = _KiwiClass()
    NLP_AVAILABLE = True
    print("[NLP] kiwipiepy 초기화 성공")
except Exception as e:
    NLP_AVAILABLE = False
    print(f"[NLP] 초기화 실패: {e}")

OUTPUT_DIR = Path("data/raw/naver")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

BASE_PCMAP      = "https://pcmap.place.naver.com"
MIN_REVIEWS     = 50   # 등재 기준 (방문자 유효 리뷰 기준)
VISITOR_TARGET  = 100  # 방문자 리뷰 기본 수집 목표
BLOG_TARGET     = 25   # 블로그 리뷰 기본 수집 목표

_stealth = Stealth()

# 광고·협찬·이벤트 키워드
AD_KEYWORDS = [
    "제공받았습니다", "제공 받았습니다", "협찬", "무료로 제공",
    "초대받아", "체험단", "이벤트 당첨", "서포터즈", "기자단",
    "방문권", "무료 체험", "댓가로", "대가로",
    "광고", "홍보", "리뷰 이벤트", "리뷰이벤트",
    "영수증 리뷰", "포토 리뷰", "sns 이벤트",
]

# 중복 감지 설정
DUP_MIN_LEN   = 15
DUP_THRESHOLD = 3

# ── 감성 분석 키워드 (카테고리별 구조화) ────────────────────────────
# 긍정: 맛·품질
_POS_TASTE = [
    "맛있", "맛나", "맛좋", "맛이 좋", "맛이 훌륭", "맛이 최고",
    "맛있었", "맛있어요", "맛있네요", "맛있다", "맛있는",
    "너무 맛", "정말 맛", "진짜 맛", "완전 맛", "존맛", "맛집",
    "신선", "신선해", "신선하고", "신선한", "재료가 좋", "재료 신선",
    "푸짐", "양이 많", "양이 넉넉", "가득", "넉넉",
]
# 긍정: 재방문·추천
_POS_REVISIT = [
    "재방문", "또 왔", "또 올", "또 오고", "또 오게", "다시 왔",
    "다시 올", "다시 오고", "단골", "단골집", "자주 와", "자주 오",
    "추천", "강추", "강력 추천", "꼭 가", "꼭 가보", "무조건 추천",
    "가보세요", "추천해요", "추천합니다", "인생맛집", "최애",
]
# 긍정: 서비스·분위기
_POS_SERVICE = [
    "친절", "친절해", "친절하고", "친절한", "친절하게", "서비스 좋",
    "서비스가 좋", "직원이 친", "직원분이 친", "사장님이 친",
    "깔끔", "깔끔해", "깔끔하고", "깔끔한", "청결", "청결해", "위생적",
    "분위기 좋", "분위기가 좋", "분위기가 너무", "분위기가 정말",
    "아늑", "예쁘고", "예쁜 곳", "인테리어가 좋",
]
# 긍정: 가성비·만족
_POS_VALUE = [
    "가성비", "가성비 좋", "가성비가 좋", "가격 대비", "합리적",
    "저렴", "저렴하고", "저렴해", "가격이 착", "착한 가격",
    "만족", "만족해", "만족스럽", "매우 만족", "너무 만족",
    "행복", "감동", "완벽", "훌륭", "최고", "대박", "굿",
]

POSITIVE_KEYWORDS = _POS_TASTE + _POS_REVISIT + _POS_SERVICE + _POS_VALUE

# 부정: 맛·품질
_NEG_TASTE = [
    "맛없", "맛이 없", "맛도 없", "별로", "별로였", "별로예요",
    "별로네요", "실망", "실망했", "실망스럽", "기대 이하",
    "짜다", "짜요", "짜네요", "너무 짜", "엄청 짜",
    "싱겁", "싱거워", "싱겁네", "싱거운",
    "느끼", "느끼해", "느끼하고",
    "퍽퍽", "퍽퍽해", "퍽퍽하고", "질기", "질겨",
]
# 부정: 재방문 거절·비추
_NEG_REVISIT = [
    "다시 안", "다시는 안", "다시는 오지", "안 올", "오지 않을",
    "비추", "비추천", "노추천", "추천 안", "추천하지 않",
    "후회", "후회했", "후회스럽",
]
# 부정: 서비스·위생
_NEG_SERVICE = [
    "불친절", "불친절해", "불친절하고", "불친절한",
    "직원이 불", "직원이 무", "직원이 불친", "서비스가 별",
    "서비스가 너무 별", "서비스 최악",
    "위생", "위생이 별", "위생 상태", "불결", "더럽", "더러워",
    "냄새", "냄새가 나", "냄새가 심", "곰팡이",
    "기다림이 길", "오래 기다", "웨이팅이 너무",
]
# 부정: 가격·최악
_NEG_VALUE = [
    "바가지", "비싸", "너무 비싸", "가격이 비", "가격 대비 별",
    "가격 대비 너무", "돈이 아깝", "돈 아깝",
    "최악", "형편없", "최하", "별점 1", "최저",
    "속았", "속은 기분", "사기",
]

NEGATIVE_KEYWORDS = _NEG_TASTE + _NEG_REVISIT + _NEG_SERVICE + _NEG_VALUE


# ── KoNLPy 리뷰 분석 ──────────────────────────────────────────────────
# 분석에서 제외할 일반 명사 (stop words)
_STOP_NOUNS = {
    '음식', '식당', '가게', '레스토랑', '맛집', '리뷰', '후기', '방문', '이번',
    '정도', '이상', '이하', '느낌', '생각', '경우', '부분', '때문', '이유',
    '서비스', '주문', '예약', '웨이팅', '줄', '시간', '분', '초', '번',
    '위치', '주차', '인테리어', '테이블', '자리', '층', '호', '평수',
    '가격', '금액', '비용', '메뉴판', '메뉴', '사진',
    '곳', '데', '편', '때', '것', '점', '님', '저', '제', '우리',
    '오늘', '어제', '지난', '다음', '처음', '매번', '항상', '언제나',
    '완전', '너무', '정말', '진짜', '매우',
}

# 음식 관련 접미사 → 붙어있으면 메뉴로 분류
_FOOD_SUFFIXES = (
    '탕', '찌개', '구이', '볶음', '면', '밥', '죽', '전', '회', '롤',
    '초밥', '카츠', '파스타', '피자', '버거', '스테이크', '샐러드',
    '냉면', '국수', '우동', '소바', '덮밥', '갈비', '삼겹', '오겹',
    '항정', '곱창', '막창', '순대', '족발', '보쌈', '육회', '만두',
    '떡볶이', '순두부', '해물', '라면', '튀김', '돈까스',
)

# 서비스·위생 관련 키워드
_SERVICE_CONTEXTS = ['직원', '사장', '사장님', '친절', '응대', '서비스', '주문', '안내']
_HYGIENE_CONTEXTS = ['위생', '청결', '깔끔', '더럽', '불결', '냄새', '곰팡이']
_HYGIENE_POS      = ['깔끔', '청결', '위생적', '깨끗', '청소', '깔끔하']
_HYGIENE_NEG      = ['더럽', '불결', '냄새', '곰팡이', '위생이 별', '불결']


def _extract_nouns_kiwi(text: str) -> list[str]:
    """단일 명사(NNG·NNP) 추출"""
    try:
        return [t.form for t in _kiwi.tokenize(text)
                if t.tag in ("NNG", "NNP") and len(t.form) >= 2
                and t.form not in _STOP_NOUNS]
    except Exception:
        return []


def _extract_compound_nouns(text: str) -> list[str]:
    """연속된 명사 토큰을 붙여 복합명사(메뉴명) 추출.
    예: '불고기' + '버거' → '불고기버거'
    """
    try:
        tokens = _kiwi.tokenize(text)
        results: list[str] = []
        buf: list[str] = []
        for t in tokens:
            if t.tag in ("NNG", "NNP") and len(t.form) >= 1:
                buf.append(t.form)
            else:
                if len(buf) >= 2:
                    compound = ''.join(buf)
                    if len(compound) >= 3 and compound not in _STOP_NOUNS:
                        results.append(compound)
                for b in buf:
                    if len(b) >= 2 and b not in _STOP_NOUNS:
                        results.append(b)
                buf = []
        if buf:
            if len(buf) >= 2:
                compound = ''.join(buf)
                if len(compound) >= 3 and compound not in _STOP_NOUNS:
                    results.append(compound)
            for b in buf:
                if len(b) >= 2 and b not in _STOP_NOUNS:
                    results.append(b)
        return results
    except Exception:
        return []


def _find_example(keyword: str, texts: list[str], max_len: int = 55) -> str:
    """keyword가 포함된 리뷰에서 해당 문장을 추출"""
    for text in texts:
        if keyword not in text:
            continue
        # 마침표·느낌표·물음표 또는 줄바꿈으로 문장 분리
        parts = re.split(r'(?<=[.!?요다네])\s+|[\n\r]+', text)
        for part in parts:
            if keyword in part and len(part.strip()) >= 10:
                s = part.strip()
                return (s[:max_len] + '…') if len(s) > max_len else s
        # 분리 안 될 경우 키워드 앞뒤 문맥
        idx = text.find(keyword)
        start = max(0, idx - 12)
        end   = min(len(text), idx + len(keyword) + 28)
        snippet = text[start:end].strip()
        return (snippet[:max_len] + '…') if len(snippet) > max_len else snippet
    return ""


def analyze_reviews_konlpy(visitor_valid: list[str], blog_valid: list[str]) -> dict:
    """kiwipiepy로 유효 리뷰를 분석.
    반환 형식:
      top_menus     : list[str]           — 복합명사 기반 구체적 메뉴명
      top_keywords  : list[{word, count, example}]
      neg_topics    : list[{word, count, example}]
      service_score / hygiene_score : int | None
    """
    if not NLP_AVAILABLE:
        return {}

    all_reviews = (visitor_valid or []) + (blog_valid or [])
    if len(all_reviews) < 3:
        return {}

    analyze_limit = min(len(all_reviews), 150)
    sample = all_reviews[:analyze_limit]

    # ── 복합명사 포함 전체 명사 추출 ──
    all_nouns: list[str] = []
    for text in sample:
        all_nouns.extend(_extract_compound_nouns(text))

    noun_freq = Counter(all_nouns)
    top_nouns = noun_freq.most_common(50)

    # ── 대표 메뉴 (음식 접미사 매칭, 복합명사 우선) ──
    top_menus: list[str] = []
    seen_menu: set[str] = set()
    for noun, _cnt in top_nouns:
        if any(noun.endswith(s) or noun == s for s in _FOOD_SUFFIXES):
            # 이미 포함된 단어가 부분문자열이면 더 긴 것(복합명사)으로 교체
            dominated = False
            for existing in list(top_menus):
                if existing in noun:          # 기존이 현재의 부분 → 교체
                    top_menus.remove(existing)
                    seen_menu.discard(existing)
                elif noun in existing:        # 현재가 기존의 부분 → 건너뜀
                    dominated = True
                    break
            if not dominated and noun not in seen_menu:
                top_menus.append(noun)
                seen_menu.add(noun)
        if len(top_menus) >= 5:
            break

    menu_set = set(top_menus)

    # ── 대표 특징 키워드 (언급 횟수 + 예시문장) ──
    # stop words·음식명 제외, 상위 5개
    top_keywords = []
    for noun, count in top_nouns:
        if noun in _STOP_NOUNS or any(noun.endswith(s) for s in _FOOD_SUFFIXES) or noun in menu_set:
            continue
        example = _find_example(noun, sample)
        top_keywords.append({'word': noun, 'count': count, 'example': example})
        if len(top_keywords) >= 5:
            break

    # ── 부정 리뷰 주요 불만 (언급 횟수 + 예시문장) ──
    neg_reviews = [t for t in sample if any(kw in t for kw in NEGATIVE_KEYWORDS)]
    neg_nouns: list[str] = []
    for text in neg_reviews[:50]:
        neg_nouns.extend(n for n in _extract_compound_nouns(text) if n not in menu_set)
    neg_freq = Counter(neg_nouns)
    neg_topics = []
    for noun, count in neg_freq.most_common(8):
        if noun in _STOP_NOUNS:
            continue
        example = _find_example(noun, neg_reviews[:30])
        neg_topics.append({'word': noun, 'count': count, 'example': example})
        if len(neg_topics) >= 4:
            break

    # ── 서비스 평가 ──
    svc_reviews = [t for t in sample if any(k in t for k in _SERVICE_CONTEXTS)]
    svc_pos   = sum(1 for t in svc_reviews if any(k in t for k in _POS_SERVICE))
    svc_neg   = sum(1 for t in svc_reviews if any(k in t for k in _NEG_SERVICE))
    svc_total = svc_pos + svc_neg

    # ── 위생 평가 ──
    hyg_reviews = [t for t in sample if any(k in t for k in _HYGIENE_CONTEXTS)]
    hyg_pos   = sum(1 for t in hyg_reviews if any(k in t for k in _HYGIENE_POS))
    hyg_neg   = sum(1 for t in hyg_reviews if any(k in t for k in _HYGIENE_NEG))
    hyg_total = hyg_pos + hyg_neg

    return {
        'top_menus':      top_menus,
        'top_keywords':   top_keywords,
        'neg_topics':     neg_topics,
        'service_score':  round(svc_pos / svc_total * 100) if svc_total >= 3 else None,
        'service_count':  svc_total,
        'hygiene_score':  round(hyg_pos / hyg_total * 100) if hyg_total >= 3 else None,
        'hygiene_count':  hyg_total,
        'analyzed_count': analyze_limit,
    }


# ── 유틸 ──────────────────────────────────────────────────────────────
def _safe_float(v, fallback=0.0):
    try:
        return float(v)
    except:
        return fallback


_REVIEW_TEXT_KEYS = ("body", "reviewContent", "reviewText", "reviewBody",
                     "content", "description", "text")
_REVIEW_DATE_KEYS = ("createDate", "visitDate", "created", "date",
                     "registDate", "updatedDate", "createdAt", "timestamp")


def _is_korean(s: str) -> bool:
    return any('가' <= c <= '힣' for c in s)


def _parse_review_arrays_from_gql(gql_text: str, target: int) -> list[tuple[str, str]]:
    """GraphQL 응답 JSON에서 리뷰 배열을 찾아 (텍스트, 날짜) 쌍으로 추출.

    핵심 아이디어:
      - list를 만나면 항목들이 dict이고 텍스트 필드 2개 이상 → 리뷰 배열로 판단
      - 한 번 추출 성공한 list는 더 깊이 탐색 안 함 (중복 방지)
    반환: [(text, date_str), ...]  날짜 없으면 date_str = ""
    """
    try:
        obj = json.loads(gql_text)
    except Exception:
        return []

    collected: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(t: str, d: str):
        t = t.strip()
        if len(t) >= 10 and t not in seen and _is_korean(t):
            seen.add(t)
            collected.append((t, d))

    def get_date(item: dict) -> str:
        for key in _REVIEW_DATE_KEYS:
            val = item.get(key)
            if isinstance(val, str) and val:
                return val[:10]  # YYYY-MM-DD 앞 10자만
        return ""

    def try_extract_list(node: list) -> bool:
        if len(node) < 2:
            return False
        valid_count = 0
        for item in node:
            if not isinstance(item, dict):
                continue
            for key in _REVIEW_TEXT_KEYS:
                val = item.get(key)
                if isinstance(val, str) and len(val) >= 10 and _is_korean(val):
                    valid_count += 1
                    break
        if valid_count < 2:
            return False
        for item in node:
            if len(collected) >= target:
                break
            if not isinstance(item, dict):
                continue
            for key in _REVIEW_TEXT_KEYS:
                val = item.get(key)
                if isinstance(val, str) and len(val) >= 10 and _is_korean(val):
                    add(val, get_date(item))
                    break
        return True

    def walk(node, depth: int = 0):
        if depth > 10 or len(collected) >= target:
            return
        if isinstance(node, list):
            if not try_extract_list(node):
                for item in node:
                    walk(item, depth + 1)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v, depth + 1)

    walk(obj)
    return collected


def _normalize_date(d: str) -> str:
    """다양한 날짜 형식 → 'YYYY-MM-DD' 정규화 (정렬 비교용)"""
    from datetime import date as _date
    d = d.strip()
    if not d:
        return ''
    today = _date.today()

    # YYYY.MM.DD  또는  YYYY-MM-DD
    m = re.match(r'(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})', d)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    # M.D.요일  (예: '6.1.월', '5.31.일')
    m = re.match(r'(\d{1,2})\.(\d{1,2})\.[월화수목금토일]', d)
    if m:
        mon, day = int(m.group(1)), int(m.group(2))
        year = today.year
        try:
            if _date(year, mon, day) > today:
                year -= 1
        except ValueError:
            pass
        return f"{year}-{mon:02d}-{day:02d}"

    # YYYY년 M월 D일
    m = re.match(r'(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일', d)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    return d  # 파싱 실패 시 원본


def _sort_pairs_by_date(pairs: list[tuple[str, str]], label: str) -> list[tuple[str, str]]:
    """(text, date) 쌍을 날짜 내림차순(최신→과거)으로 정렬 후 반환.
    날짜 정보 없는 항목은 뒤로.
    """
    enriched = [(t, d, _normalize_date(d)) for t, d in pairs]

    norm_sample = [nd for _, _, nd in enriched if nd][:5]
    if norm_sample:
        print(f"      [sort-check:{label}] GQL 원본 첫 5건: {norm_sample}")
        already = all(norm_sample[i] >= norm_sample[i+1]
                      for i in range(len(norm_sample)-1))
        if already:
            print(f"      [sort-check:{label}] ✅ 이미 최신순")
        else:
            print(f"      [sort-check:{label}] 🔄 Python 날짜 정렬 적용")
    else:
        print(f"      [sort-check:{label}] 날짜 정보 없음 — 수집 순서 유지")
        return pairs

    enriched.sort(key=lambda x: x[2] if x[2] else '0000', reverse=True)
    return [(t, d) for t, d, _ in enriched]


async def _extract_texts_from_dom(page, target: int) -> list[str]:
    """DOM 셀렉터로 실제 리뷰 요소 텍스트 추출 (GraphQL 파싱 실패 시 fallback)"""
    try:
        return await page.evaluate(f"""() => {{
            const MIN = 10, MAX = {target};
            const seen = new Set(), out = [];
            function add(t) {{
                t = (t || '').trim();
                if (t.length >= MIN && !seen.has(t) && /[가-힣]{{2,}}/.test(t)) {{
                    seen.add(t); out.push(t);
                }}
            }}
            const selectors = [
                '[class*="Review_"] p', '[class*="review_"] p',
                '[class*="ReviewItem"] span[class*="text"]',
                '[class*="ReviewItem"] p',
                '[data-review-id] p', '[data-review-id] span',
                'li[class*="review"] span', 'li[class*="Review"] span',
                '.place_reviews p', '[class*="UnitReview"] p',
            ];
            for (const sel of selectors) {{
                if (out.length >= MAX) break;
                document.querySelectorAll(sel).forEach(el => {{
                    if (out.length < MAX) add(el.innerText);
                }});
                if (out.length >= 3) break;
            }}
            return out;
        }}""")
    except Exception:
        return []


async def _intercept_gql(page) -> list[str]:
    """page.route 로 GraphQL 응답 캡처 설정. 수집된 body 리스트 반환용 리스트 반환"""
    gql_bodies: list[str] = []

    async def handle(route):
        try:
            resp = await route.fetch()
            body_bytes = await resp.body()
            text = body_bytes.decode("utf-8", errors="replace")
            if len(text) > 50:
                gql_bodies.append(text)
            await route.fulfill(response=resp)
        except:
            try:
                await route.continue_()
            except:
                pass

    await page.route("**/graphql", handle)
    return gql_bodies


async def _click_latest_sort(page) -> bool:
    """최신순 정렬 버튼 클릭. 이미 최신순이면 그대로 반환."""
    # 1. 이미 최신순 활성 상태인지 확인
    try:
        already = await page.evaluate("""() => {
            const els = [...document.querySelectorAll('a, button, span')];
            const btn = els.find(e => e.textContent.trim() === '최신순');
            if (!btn) return false;
            return btn.classList.contains('active')
                || btn.getAttribute('aria-selected') === 'true'
                || btn.getAttribute('aria-current') === 'true'
                || getComputedStyle(btn).fontWeight >= 600;
        }""")
        if already:
            print("      [sort] 이미 최신순 활성 상태")
            return True
    except:
        pass

    # 2. 버튼 클릭 시도 (여러 셀렉터)
    for sel in [
        "a:has-text('최신순')", "button:has-text('최신순')",
        "span:has-text('최신순')",
        ".sort_area a:last-child", ".tab_sort button:last-child",
        "[class*='sort'] a:last-child", "[class*='Sort'] button:last-child",
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                await asyncio.sleep(2)
                print("      [sort] 최신순 버튼 클릭 성공")
                return True
        except:
            continue

    print("      [sort] 최신순 버튼 미발견 — 기본 정렬 유지")
    return False


async def _count_reviews_on_page(page) -> int:
    """실제 리뷰 컨테이너 요소 수를 DOM으로 카운트 (HTML grep 오차 방지)"""
    try:
        count = await page.evaluate("""() => {
            const selectors = [
                '[class*="Review_"]', '[class*="review_"]',
                '[data-review-id]', '[class*="ReviewItem"]',
                '.place_reviews li', '.review_list li',
                '[class*="UnitReview"]',
            ];
            for (const sel of selectors) {
                const els = document.querySelectorAll(sel);
                if (els.length > 0) return els.length;
            }
            return -1;
        }""")
        return count
    except:
        return -1


async def _load_more_reviews(page, target: int = 100, max_rounds: int = 20):
    """더보기 반복 클릭 + 스크롤로 target건 이상 로드.
    DOM 요소 수 기준으로만 판단 — HTML grep 없음.
    """
    for _ in range(max_rounds):
        dom_count = await _count_reviews_on_page(page)
        if dom_count >= target:
            break

        clicked = False
        for sel in ["a.fvwqf", ".more_btn", "a[class*='more']",
                    "button[class*='more']", "a:has-text('더보기')"]:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    clicked = True
                    await asyncio.sleep(1.2)
                    break
            except:
                continue

        await page.evaluate("window.scrollBy(0, 1000)")
        await asyncio.sleep(0.8)

        if not clicked:
            break


# ── 경량 활성도 체크 ──────────────────────────────────────────────────
async def check_recent_activity(pid: str, ctx, active_days: int = 90) -> bool:
    """방문자 리뷰 첫 페이지 GQL만 확인해 최근 N일 내 리뷰 여부 반환.
    스크롤/더보기 없음 → 약 5~8초 소요.
    날짜 정보를 아예 얻지 못하면 True 반환 (통과 처리).
    """
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=active_days)).strftime("%Y%m%d")

    page = await ctx.new_page()
    gql_bodies: list[str] = []

    async def handle(route):
        try:
            resp = await route.fetch()
            body = (await resp.body()).decode("utf-8", errors="replace")
            if len(body) > 100:
                gql_bodies.append(body)
            await route.fulfill(response=resp)
        except:
            try: await route.continue_()
            except: pass

    await page.route("**/graphql", handle)
    try:
        await page.goto(
            f"{BASE_PCMAP}/restaurant/{pid}/review/visitor",
            wait_until="load", timeout=15000,
        )
        await asyncio.sleep(3)
    except:
        pass
    await page.close()

    # GQL 응답에서 날짜 추출
    date_pattern = re.compile(
        r'"(?:createDate|visitDate|registDate|updatedDate)"\s*:\s*"(\d{8})"'
    )
    all_dates: list[str] = []
    for body in gql_bodies:
        all_dates.extend(date_pattern.findall(body))

    if not all_dates:
        return True  # 날짜 확인 불가 → 통과

    return max(all_dates) >= cutoff


# ── 영업 정보 추출 ────────────────────────────────────────────────────
async def _crawl_hours(pid: str, ctx) -> dict:
    """홈 페이지 GraphQL에서 영업시간·휴무일·브레이크타임 추출"""
    hours_info = {"business_hours": "", "break_time": "", "closed_days": ""}
    page = await ctx.new_page()

    try:
        gql_bodies = await _intercept_gql(page)

        await page.goto(
            f"{BASE_PCMAP}/restaurant/{pid}/home",
            wait_until="load", timeout=20000
        )
        await asyncio.sleep(3)

        html = await page.content()
        all_text = html + " ".join(gql_bodies)

        # 영업시간 패턴 탐색
        hours_m = re.search(
            r'((?:월|화|수|목|금|토|일|매일|평일|주말)[^\d]*\d{1,2}:\d{2}\s*[-~]\s*\d{1,2}:\d{2})',
            all_text
        )
        if hours_m:
            hours_info["business_hours"] = hours_m.group(1).strip()

        # 브레이크타임
        break_m = re.search(
            r'브레이크\s*타임[^\d]*(\d{1,2}:\d{2}\s*[-~]\s*\d{1,2}:\d{2})',
            all_text
        )
        if break_m:
            hours_info["break_time"] = break_m.group(1).strip()

        # 휴무일
        closed_m = re.search(
            r'((?:매주\s*)?(?:월|화|수|목|금|토|일)요일\s*휴무|연중무휴|정기\s*휴무[^\s]*)',
            all_text
        )
        if closed_m:
            hours_info["closed_days"] = closed_m.group(1).strip()

        # "contents" 필드에서도 시도 (GraphQL 응답)
        if not hours_info["business_hours"]:
            for body_text in gql_bodies:
                contents_m = re.search(
                    r'"contents"\s*:\s*"([^"]{20,300})"', body_text
                )
                if contents_m:
                    snippet = contents_m.group(1)
                    h = re.search(r'(\d{1,2}:\d{2}\s*[-~]\s*\d{1,2}:\d{2})', snippet)
                    if h:
                        hours_info["business_hours"] = h.group(1)
                        b = re.search(
                            r'브레이크\s*타임[^\d]*(\d{1,2}:\d{2}\s*[-~]\s*\d{1,2}:\d{2})',
                            snippet
                        )
                        if b:
                            hours_info["break_time"] = b.group(1)
                        break

    except Exception as e:
        pass
    finally:
        await page.close()

    return hours_info


# ── 리뷰 수집 (방문자 or 블로그) ─────────────────────────────────────
async def _crawl_reviews(pid: str, review_type: str, ctx,
                         target: int = 100) -> list[str]:
    """
    1. GraphQL 응답에서 리뷰 배열만 타겟 추출 (HTML 전체 grep 제거)
    2. 수집 실패 시 DOM 셀렉터 fallback
    3. 결과를 반드시 target 이하로 반환
    """
    page = await ctx.new_page()
    gql_bodies: list[str] = []
    gql_requests: list[str] = []  # 요청 URL 기록용

    async def handle(route):
        req_url = route.request.url
        req_body = route.request.post_data or ''
        # sort 관련 파라미터 추출해서 로그
        sort_hint = ''
        if 'sort' in req_body.lower():
            import re as _re
            m = _re.search(r'"sort"\s*:\s*"([^"]+)"', req_body)
            if m:
                sort_hint = f' [sort={m.group(1)}]'
        gql_requests.append(f"{req_url[:80]}{sort_hint}")
        try:
            resp = await route.fetch()
            body_bytes = await resp.body()
            text = body_bytes.decode("utf-8", errors="replace")
            if len(text) > 100:
                gql_bodies.append(text)
            await route.fulfill(response=resp)
        except:
            try:
                await route.continue_()
            except:
                pass

    await page.route("**/graphql", handle)

    if review_type == "visitor":
        urls_to_try = [
            f"{BASE_PCMAP}/restaurant/{pid}/review/visitor?sort=recent",
            f"{BASE_PCMAP}/restaurant/{pid}/review/visitor",
        ]
    else:
        urls_to_try = [
            f"{BASE_PCMAP}/restaurant/{pid}/review/ugc?sort=recent",
            f"{BASE_PCMAP}/restaurant/{pid}/review/ugc",
            f"{BASE_PCMAP}/restaurant/{pid}/review/blog?sort=recent",
            f"{BASE_PCMAP}/restaurant/{pid}/review/blog",
        ]

    for url in urls_to_try:
        try:
            await page.goto(url, wait_until="load", timeout=22000)
            break
        except:
            continue
    await asyncio.sleep(4)

    # 페이지 초기 로드 시 발생한 GQL 요청 로그
    print(f"      [GQL-초기:{review_type}] {len(gql_requests)}건 요청")
    for u in gql_requests[-3:]:  # 마지막 3개만
        print(f"        {u}")

    # 최신순 정렬 — 클릭 후 기존 GQL 응답(관련도순) 제거하고 재수집
    gql_requests_before = len(gql_requests)
    await _click_latest_sort(page)
    gql_bodies.clear()       # 버튼 클릭 전 응답 제거
    await asyncio.sleep(2.5) # 재정렬 후 새 GQL 응답 대기

    # 버튼 클릭 후 새 GQL 요청 발생했는지 확인
    new_requests = gql_requests[gql_requests_before:]
    if new_requests:
        print(f"      [GQL-재요청:{review_type}] 버튼 클릭 후 {len(new_requests)}건 새 요청 ✅ (서버 재요청)")
        for u in new_requests[:3]:
            print(f"        {u}")
    else:
        print(f"      [GQL-재요청:{review_type}] 새 요청 없음 ⚠️ (클라이언트 단 재정렬 — 최신순 보장 불가)")

    # target 건수까지 더보기 클릭
    await _load_more_reviews(page, target=target, max_rounds=20)

    # ── 1차: GraphQL 응답에서 리뷰 배열 파싱 ──
    pairs: list[tuple[str, str]] = []  # (text, date)
    seen: set[str] = set()
    for body_text in gql_bodies:
        for t, d in _parse_review_arrays_from_gql(body_text, target):
            if t not in seen and len(pairs) < target:
                seen.add(t)
                pairs.append((t, d))

    # 날짜 기준 내림차순 정렬 (네이버 UI 정렬 실패 시에도 최신순 보장)
    if pairs:
        pairs = _sort_pairs_by_date(pairs, review_type)

    texts = [t for t, _ in pairs]

    # ── 2차: GraphQL 파싱 수집 부족 시 DOM fallback ──
    if len(texts) < 5:
        dom_texts = await _extract_texts_from_dom(page, target)
        for t in dom_texts:
            if t not in seen and len(texts) < target:
                seen.add(t)
                texts.append(t)
        if dom_texts:
            print(f"      [sort-check:{review_type}] DOM fallback 사용 — 날짜 검증 불가")

    await page.close()
    return texts[:target]  # 반드시 target 이하


# ── 어뷰징 필터 ──────────────────────────────────────────────────────
def _is_mindless_review(text: str) -> bool:
    """무지성 봇 리뷰 판별: 의미 있는 내용이 전혀 없는 경우만 제거
    ㅋㅋㅋ·ㅎㅎㅎ 등 구어체 반복은 실제 찐 리뷰인 경우가 많아 허용.
    """
    if len(text) < 10:
        return True
    # 한글·영어 단어가 전혀 없음 (이모지·숫자·특수문자만으로 구성)
    if not re.search(r'[가-힣a-zA-Z]{2,}', text):
        return True
    return False


def analyze_sentiment(valid_texts: list[str]) -> dict:
    """유효 리뷰(광고·중복 제거 후)에 대한 감성 분석

    무지성 리뷰 제외 후:
      긍정 = 긍정키워드 수 > 부정키워드 수
      부정 = 부정키워드 수 > 긍정키워드 수
      중립 = 동점 or 키워드 없음
    긍정률 = 긍정 / (긍정+부정+중립) * 100
    """
    positive = negative = neutral = mindless = 0

    for text in valid_texts:
        if _is_mindless_review(text):
            mindless += 1
            continue
        pos = sum(1 for kw in POSITIVE_KEYWORDS if kw in text)
        neg = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text)
        if pos > neg:
            positive += 1
        elif neg > pos:
            negative += 1
        else:
            neutral += 1

    analyzed = positive + negative + neutral
    positive_rate = round(positive / analyzed * 100) if analyzed > 0 else 0

    return {
        "positive": positive,
        "negative": negative,
        "neutral":  neutral,
        "mindless_excluded": mindless,
        "sentiment_total":   analyzed,
        "positive_rate":     positive_rate,
    }


def filter_reviews(texts: list[str]) -> dict:
    """광고·협찬·이벤트 키워드 제거 + 중복 복붙 제거 + 감성 분석"""
    ad_count = 0
    valid1 = []
    for t in texts:
        if any(kw in t for kw in AD_KEYWORDS):
            ad_count += 1
        else:
            valid1.append(t)

    snippet_count: dict[str, int] = {}
    for t in valid1:
        for end in range(DUP_MIN_LEN, min(len(t), 50) + 1):
            s = t[:end]
            snippet_count[s] = snippet_count.get(s, 0) + 1

    dup_patterns = {s for s, c in snippet_count.items() if c >= DUP_THRESHOLD}

    dup_count = 0
    valid2 = []
    for t in valid1:
        if any(t.startswith(p) for p in dup_patterns):
            dup_count += 1
        else:
            valid2.append(t)

    sentiment = analyze_sentiment(valid2)

    return {
        "valid": valid2,
        "ad_count": ad_count,
        "duplicate_count": dup_count,
        **sentiment,
    }


# 프랜차이즈 브랜드 (스캔 단계에서 제외)
CHAIN_BRANDS = {
    '맥도날드', '버거킹', 'KFC', '롯데리아', '맘스터치', '노브랜드버거', '쉐이크쉑',
    '스타벅스', '이디야', '메가커피', '컴포즈', '빽다방', '투썸플레이스', '할리스',
    '파리바게뜨', '뚜레쥬르', '베이커리', '베스킨라빈스', '배스킨', '던킨',
    'GS25', 'CU편의점', '세븐일레븐', '미니스톱', 'GS수퍼',
    '피자헛', '도미노', '파파존스', '피자알볼로',
    '서브웨이', '퀴즈노스',
    '한솥', '본도시락', '한솥도시락',
    '놀부', '원할머니', '이촌동', '한우리',
    '교촌치킨', '굽네치킨', '페리카나', 'BBQ', 'BHC', '지코바', '네네치킨',
    '청년다방', '이삭토스트', '김밥천국',
}


def _is_chain(name: str) -> bool:
    """상호명에 프랜차이즈 브랜드명이 포함되면 True"""
    return any(brand in name for brand in CHAIN_BRANDS)


# ── 장소 검색 ──────────────────────────────────────────────────────────
async def search_places(query: str, limit: int = 500,
                        min_reviews: int = 100) -> list[dict]:
    """allSearch API 인터셉트로 장소 목록 수집.

    결과 패널을 끝까지 스크롤해 limit건 또는 더 이상 새 결과가 없을 때까지 수집.
    min_reviews: 방문자+블로그 합산 최소 리뷰 수 (스캔 단계 사전 필터)
    """
    print(f"[naver] '{query}' 검색 중 (목표 {limit}개, 최소리뷰 {min_reviews}건)...")
    places: list[dict] = []
    seen_ids: set[str] = set()
    total_seen = 0
    skip_chain = 0
    skip_reviews = 0

    async with _stealth.use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="ko-KR",
            extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9"}
        )
        page = await ctx.new_page()

        _api_fields_logged = False  # 첫 응답 필드 한 번만 로그

        async def on_response(resp):
            nonlocal _api_fields_logged
            if "allSearch" not in resp.url:
                return
            try:
                body = await resp.json()
                items = (body.get("result", {})
                             .get("place", {})
                             .get("list", []))
                # 첫 아이템 전체 키 출력 (어떤 날짜 필드가 있는지 확인)
                if not _api_fields_logged and items:
                    _api_fields_logged = True
                    first = items[0]
                    date_keys = {k: v for k, v in first.items()
                                 if any(w in k.lower() for w in
                                        ('date', 'time', 'review', 'visit', 'recent', 'last', 'update'))}
                    print(f"  [API 날짜 관련 필드] {date_keys}")
                for item in items:
                    pid = str(item.get("id", ""))
                    if not pid or pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    total_seen += 1

                    name = item.get("name", "")

                    # ── 스캔 단계 필터 ──
                    # 프랜차이즈 제외
                    if _is_chain(name):
                        skip_chain += 1
                        continue

                    cats = item.get("category", [])
                    bs   = item.get("businessStatus", {})
                    stat = bs.get("status", {})

                    visitor_count = int(item.get("placeReviewCount") or 0)
                    blog_count    = int(item.get("reviewCount") or 0)
                    total_count   = visitor_count + blog_count

                    # 최소 리뷰 수 미달 제외
                    if total_count < min_reviews:
                        skip_reviews += 1
                        continue

                    _GENERIC = {"음식점", "식당", "레스토랑", "먹거리", "푸드코트"}
                    cat = next((c for c in cats if c not in _GENERIC),
                               cats[0] if cats else "기타")

                    # 최근 리뷰 날짜 (API 제공 시) — YYYYMMDD 형식
                    last_review_date = str(item.get("recentReviewDate", "")
                                           or item.get("reviewDate", "")
                                           or item.get("lastReviewDate", ""))

                    places.append({
                        "id":                   pid,
                        "name":                 name,
                        "category":             cat,
                        "address":              item.get("roadAddress") or item.get("address", ""),
                        "x":                    item.get("x", "0"),
                        "y":                    item.get("y", "0"),
                        "review_count":         total_count,
                        "visitor_review_count": visitor_count,
                        "blog_review_count":    blog_count,
                        "business_status":      stat.get("text", ""),
                        "business_hours_today": bs.get("businessHours", ""),
                        "break_time_today":     bs.get("breakTime", ""),
                        "last_order":           bs.get("lastOrder", ""),
                        "last_review_date":     last_review_date,
                    })
            except:
                pass

        page.on("response", on_response)

        encoded = query.replace(" ", "+")
        try:
            await page.goto(
                f"https://map.naver.com/p/search/{encoded}",
                wait_until="load", timeout=25000
            )
        except:
            pass
        await asyncio.sleep(4)

        # ── 결과 패널 스크롤 (새 결과 없을 때까지) ──
        prev_count = 0
        stale = 0
        while len(places) < limit:
            # 네이버 지도 좌측 결과 패널 스크롤
            await page.evaluate("""() => {
                const sels = [
                    '#_pcmap_list_scroll_container',
                    '.search_list',
                    '[class*="list_scroll"]',
                    '[class*="SearchListView"]',
                    '[class*="PlaceList"]',
                ];
                for (const sel of sels) {
                    const el = document.querySelector(sel);
                    if (el && el.scrollHeight > el.clientHeight) {
                        el.scrollBy(0, 2000);
                        return;
                    }
                }
                window.scrollBy(0, 2000);
            }""")
            await asyncio.sleep(2)

            if len(places) == prev_count:
                stale += 1
                if stale >= 5:   # 5번 연속 새 결과 없으면 끝
                    print(f"  더 이상 결과 없음 ({len(places)}개 수집 완료)")
                    break
            else:
                stale = 0
            prev_count = len(places)

            if len(places) % 50 == 0 and len(places) > 0:
                print(f"  수집 중: {len(places)}개...")

        await ctx.close()

    result = places[:limit]
    print(f"[naver] 스캔 완료:")
    print(f"  전체 검색결과:   {total_seen}개")
    print(f"  프랜차이즈 제외: -{skip_chain}개")
    print(f"  리뷰부족 제외:   -{skip_reviews}개  (기준: {min_reviews}건 미만)")
    print(f"  최종 통과:       {len(result)}개")
    return result


# ── 개별 장소 크롤링 ──────────────────────────────────────────────────
async def crawl_place(place: dict, ctx) -> dict | None:
    """
    방문자 리뷰 100건 + 블로그 리뷰 50건 + 영업정보 수집
    """
    pid  = place["id"]
    name = place["name"]

    visitor_api_cnt = int(place.get("visitor_review_count", 0))
    blog_api_cnt    = int(place.get("blog_review_count",
                          max(0, int(place.get("review_count", 0)) - visitor_api_cnt)))
    total_reviews   = visitor_api_cnt + blog_api_cnt

    print(f"  -> {name} 수집 시작 (방문자 {visitor_api_cnt}건, 블로그 {blog_api_cnt}건)")

    # ── 영업 정보 ──
    hours_info = await _crawl_hours(pid, ctx)

    # API에서 받은 today 정보로 보완
    if not hours_info["business_hours"] and place.get("business_hours_today"):
        raw = place["business_hours_today"]
        # 형식: "202606031000~202606032000" → "10:00~20:00"
        m = re.match(r'\d{8}(\d{2})(\d{2})~\d{8}(\d{2})(\d{2})', raw)
        if m:
            hours_info["business_hours"] = f"{m.group(1)}:{m.group(2)}~{m.group(3)}:{m.group(4)}"

    if not hours_info["break_time"] and place.get("break_time_today"):
        raw = place["break_time_today"]
        m = re.match(r'\d{8}(\d{2})(\d{2})~\d{8}(\d{2})(\d{2})', raw)
        if m:
            hours_info["break_time"] = f"{m.group(1)}:{m.group(2)}~{m.group(3)}:{m.group(4)}"

    # ── 방문자 리뷰 (기본 목표 VISITOR_TARGET) ──
    visitor_texts = await _crawl_reviews(pid, "visitor", ctx, target=VISITOR_TARGET)
    visitor_filtered = filter_reviews(visitor_texts)

    # 방문자 부족분 계산 → 블로그 목표에 추가
    visitor_shortage = max(0, VISITOR_TARGET - len(visitor_texts))
    blog_target_adj  = BLOG_TARGET + visitor_shortage
    if visitor_shortage:
        print(f"    방문자 리뷰 {len(visitor_texts)}/{VISITOR_TARGET}건 "
              f"→ 블로그 목표 {blog_target_adj}건으로 증가")

    # ── 블로그 리뷰 (부족분 보완된 목표) ──
    blog_texts = await _crawl_reviews(pid, "blog", ctx, target=blog_target_adj)
    blog_filtered = filter_reviews(blog_texts)

    # 블로그 부족분 로깅
    blog_shortage = max(0, BLOG_TARGET - len(blog_texts))
    if blog_shortage:
        print(f"    블로그 리뷰 {len(blog_texts)}/{BLOG_TARGET}건 "
              f"(해당 업체 블로그 리뷰 부족)")

    # ── KoNLPy 리뷰 분석 ──
    review_analysis = analyze_reviews_konlpy(
        visitor_filtered["valid"],
        blog_filtered["valid"],
    )
    if review_analysis:
        print(f"    [분석] 메뉴:{review_analysis.get('top_menus')} "
              f"불만:{review_analysis.get('neg_topics')}")

    lat = _safe_float(place.get("y", 0))
    lng = _safe_float(place.get("x", 0))

    result = {
        "id":       pid,
        "name":     name,
        "category": place.get("category", "기타"),
        "address":  place.get("address", ""),
        "lat":      lat if 33 <= lat <= 39 else 0.0,
        "lng":      lng if 124 <= lng <= 132 else 0.0,

        # 영업 정보
        "business_hours": hours_info["business_hours"],
        "break_time":     hours_info["break_time"],
        "closed_days":    hours_info["closed_days"],
        "business_status": place.get("business_status", ""),

        # 방문자 리뷰
        "visitor_total":      visitor_api_cnt,
        "visitor_sampled":    len(visitor_texts),
        "visitor_valid":      len(visitor_filtered["valid"]),
        "visitor_ad_count":   visitor_filtered["ad_count"],
        "visitor_dup_count":  visitor_filtered["duplicate_count"],

        # 블로그 리뷰
        "blog_total":         blog_api_cnt,
        "blog_sampled":       len(blog_texts),
        "blog_valid":         len(blog_filtered["valid"]),
        "blog_ad_count":      blog_filtered["ad_count"],
        "blog_dup_count":     blog_filtered["duplicate_count"],

        # 합산 (등재 기준은 방문자 유효 리뷰)
        "total_reviews":    total_reviews,
        "filtered_reviews": len(visitor_filtered["valid"]),
    }

    # 감성 분석 — 방문자 + 블로그 합산 (dict 밖에서 계산)
    _sp = visitor_filtered.get("positive", 0) + blog_filtered.get("positive", 0)
    _sn = visitor_filtered.get("negative", 0) + blog_filtered.get("negative", 0)
    _su = visitor_filtered.get("neutral",  0) + blog_filtered.get("neutral",  0)
    _st = _sp + _sn + _su
    result.update({
        "positive_rate":      round(_sp / _st * 100) if _st > 0 else 0,
        "sentiment_positive": _sp,
        "sentiment_negative": _sn,
        "sentiment_neutral":  _su,
        "sentiment_total":    _st,
        "mindless_excluded":  (visitor_filtered.get("mindless_excluded", 0)
                               + blog_filtered.get("mindless_excluded", 0)),
        # KoNLPy 리뷰 분석 결과
        "review_analysis":   review_analysis,
        # 유효 리뷰 텍스트 저장 (최대 50건, 분석용)
        "visitor_reviews":   visitor_filtered["valid"][:50],
        "blog_reviews":      blog_filtered["valid"][:25],
        "crawled_at": datetime.now().isoformat(),
    })

    mark = "[OK]" if result["filtered_reviews"] >= MIN_REVIEWS else "[--]"
    print(
        f"  {mark} {name} - "
        f"방문자 {len(visitor_texts)}->{len(visitor_filtered['valid'])}건 "
        f"(광고 {visitor_filtered['ad_count']}건, 긍정률 {visitor_filtered.get('positive_rate',0)}%) | "
        f"블로그 {len(blog_texts)}->{len(blog_filtered['valid'])}건 "
        f"(광고 {blog_filtered['ad_count']}건)"
    )
    return result


# ── 메인 ──────────────────────────────────────────────────────────────
async def main(args):
    import os
    place_ids = list(args.ids or [])
    places: list[dict] = [
        {"id": pid, "name": "", "category": "", "address": "",
         "x": "0", "y": "0", "review_count": 0, "visitor_review_count": 0}
        for pid in place_ids
    ]

    query = args.query or os.environ.get("CRAWLER_QUERY")
    min_reviews = getattr(args, 'min_reviews', 100)
    if query:
        found = await search_places(query, args.limit, min_reviews)
        places.extend(found)

    if not places:
        print("장소 ID 또는 검색어를 입력하세요.")
        return

    print(f"\n[naver] {len(places)}개 장소 크롤링 시작\n")

    results = []
    async with _stealth.use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="ko-KR",
            extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9"}
        )
        for pl in places:
            r = await crawl_place(pl, ctx)
            if r:
                results.append(r)
            await asyncio.sleep(1)
        await ctx.close()

    passed = sum(1 for r in results if r["filtered_reviews"] >= MIN_REVIEWS)
    print(f"\n[naver] {len(results)}개 수집 / {passed}개 기준 통과 (방문자 유효 {MIN_REVIEWS}건+)")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUTPUT_DIR / f"naver_{ts}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"[naver] 저장: {out}")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="네이버 플레이스 크롤러")
    parser.add_argument("--query",       type=str, default=None)
    parser.add_argument("--limit",       type=int, default=500)
    parser.add_argument("--min-reviews", type=int, default=100, dest="min_reviews")
    parser.add_argument("--ids",         nargs="+")
    asyncio.run(main(parser.parse_args()))
