"""
naver_crawler.py — 네이버 플레이스 크롤러 (stealth + GraphQL 인터셉트)

수집 항목:
  - 방문자 리뷰: 최신순 100건 (실제 텍스트 분석, 작성일 함께 저장)
  - 블로그 리뷰: 최신순 기본 25건 — 미리보기 스니펫이 아니라 각 글 blog.naver.com
    본문 전문을 수집·분석 (방문자 부족 시 최대 50건까지 보충, 작성일 함께 저장)
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
VISITOR_TARGET  = 100  # 방문자 리뷰 기본 수집·분석·저장 목표
BLOG_TARGET     = 25   # 블로그 리뷰 기본 목표 (방문자가 100건 다 차면 이 값만 수집)
BLOG_CAP        = 50   # 블로그 상한 — 방문자 부족분을 보충해도 분석·저장은 여기까지만
VISITOR_LOW     = 30   # 이 미만이면 '리뷰 부족' 표시 (분석 신뢰 낮음)
BLOG_LOW        = 10   # 이 미만이면 '리뷰 부족' 표시
BLOG_MIN_LEN    = 25   # 블로그 유효 최소 길이 (네이버 블로그 스니펫 중앙값 ~59자 →
                       # 100자 컷은 정상 리뷰 70%를 버려 거짓 '부족'을 유발했음.
                       # 길이 대신 _is_clip_review 로 해시태그 클립을 정밀 제거한다.)

_stealth = Stealth()

# 광고·협찬·이벤트 키워드
AD_KEYWORDS = [
    "제공받았습니다", "제공 받았습니다", "협찬", "무료로 제공",
    "초대받아", "체험단", "이벤트 당첨", "서포터즈", "기자단",
    "방문권", "무료 체험", "댓가로", "대가로",
    "광고", "홍보", "리뷰 이벤트", "리뷰이벤트",
    "영수증 리뷰", "포토 리뷰", "sns 이벤트",
    # 블로그 전문에서 자주 노출되는 유료/협찬 고지
    "원고료", "소정의 고료", "업체로부터 제공", "제공받아 작성", "지원받아 작성",
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
    # 한식·면·밥류
    '탕', '찌개', '전골', '국밥', '곰탕', '설렁탕', '해장국', '칼국수', '수제비',
    '구이', '볶음', '조림', '무침', '찜', '면', '밥', '죽', '전', '회', '정식', '백반',
    '냉면', '국수', '쌀국수', '비빔밥', '덮밥', '갈비', '삼겹', '오겹', '수육',
    '항정', '곱창', '막창', '순대', '족발', '보쌈', '육회', '만두', '꼬치',
    '떡볶이', '순두부', '해물', '라면', '튀김', '돈까스', '카츠', '쌈밥',
    # 일식·중식·양식
    '초밥', '스시', '사시미', '우동', '소바', '텐동', '규동', '카레',
    '짜장', '짬뽕', '탕수육', '마라탕', '마라샹궈', '딤섬',
    '파스타', '피자', '버거', '스테이크', '샐러드', '리조또', '그라탕',
    '타코', '부리또', '핫도그', '샌드위치',
    # 빵·디저트·음료
    '빵', '케이크', '도넛', '쿠키', '토스트', '베이글', '크로플', '와플',
    '빙수', '젤라또', '아이스크림', '라떼', '에이드', '스무디', '커피',
)

# ── 리뷰 분석 항목 정의 (단일 출처) ──────────────────────────────────
# 프론트엔드는 이 결과를 그대로 렌더링만 한다. 키워드/아이콘은 여기서만 관리.
REVIEW_ASPECTS = [
    {"key": "taste",       "label": "음식의 맛", "icon": "😋",
     "keywords": ["맛있", "맛없", "맛나", "존맛", "꿀맛", "별로", "실망",
                  "맛이 좋", "맛이 없", "맛 좋", "맛 없", "기대이하", "최고의 맛", "진짜 맛"]},
    {"key": "amount",      "label": "음식의 양", "icon": "🍚",
     "keywords": ["양이 많", "양이 적", "양 많", "양 적", "푸짐", "가성비",
                  "적은 양", "많은 양", "양은", "넉넉"]},
    {"key": "service",     "label": "서비스",   "icon": "🤝",
     "keywords": ["친절", "불친절", "서비스", "직원", "사장님", "알바", "응대", "무뚝뚝", "상냥"]},
    {"key": "hygiene",     "label": "위생",     "icon": "🧹",
     "keywords": ["청결", "깨끗", "더럽", "불결", "위생", "끈적", "냄새", "청소"]},
    {"key": "convenience", "label": "편의",     "icon": "🚻",
     # '테이블' 제외 → 캐치테이블·테이블링 오매칭 방지
     "keywords": ["화장실", "주차", "물티슈", "좌석", "자리 간격", "좌석 간격",
                  "남녀분리", "1인석", "단체석", "비품"]},
    {"key": "waiting",     "label": "웨이팅",   "icon": "⏱",
     "keywords": ["웨이팅", "대기", "줄 서", "기다", "원격등록", "현장등록", "예약"]},
    {"key": "mood",        "label": "분위기",   "icon": "✨",
     "keywords": ["분위기", "데이트", "가족", "조용", "시끄럽", "어둡", "밝",
                  "인테리어", "테마", "아늑", "감성"]},
]

# 리뷰에서 감지할 온라인 예약 앱
RESERVATION_APPS = ["캐치테이블", "테이블링", "네이버예약"]

# 메뉴로 인정하되 너무 일반적이라 후순위로 미루는 단어 (구체적 메뉴명을 우선 노출)
_GENERIC_MENU = {
    '한식', '일식', '중식', '양식', '분식', '음식', '요리', '식사', '코스', '세트',
    '런치', '디너', '브런치', '고기', '피자', '파스타', '커피', '음료', '빙수',
}

# 메뉴가 아닌데 취식·긍정 문맥에 자주 끼는 단어 (하드 제외)
_NON_MENU = {
    '메뉴', '반찬', '안주', '사이드', '혼밥', '혼술', '점심', '저녁', '아침', '야식', '간식',
    '재료', '육수', '국물', '양념', '잡내', '냄새', '정도', '종류', '메뉴판', '맛집',
    '분위기', '친구', '가족', '연인', '데이트', '친절', '서비스', '가성비', '가격', '다양', '깔끔',
    '식전', '식후', '오전', '오후', '여왕', '본점', '지점', '매장', '입구', '내부',
    # '전'으로 끝나는 비(非)음식어 (파전·김치전 등 진짜 '전'류는 복합어라 영향 없음)
    '예전', '이전', '사전', '회전', '여전', '발전', '기회', '처음', '다음',
}


def _has_food_suffix(w: str) -> bool:
    return any(w.endswith(s) or w == s for s in _FOOD_SUFFIXES)


# ── 대표 메뉴 추출 (리뷰 문맥 기반 점수화) ────────────────────────────
# 접미사 매칭이 아니라, "OO를 먹었다 / 대표메뉴인 OO"처럼 메뉴가 등장하는
# 문법적 맥락을 보고 후보에 점수를 매긴다. 사전에 없는 메뉴명도 잡고,
# 우연히 접미사만 맞는 비(非)메뉴는 거른다.
_EAT_STEMS    = ('먹', '드시', '자시', '시키', '맛보', '즐기')  # 취식·주문 동사 어간(VV)
_ORDER_NOUNS  = ('주문',)                                      # 'OO 주문했다' (NNG + 하다)
_MENU_MARKERS = ('대표메뉴', '시그니처', '시그니쳐', '추천메뉴', '인기메뉴',
                 '메인메뉴', '베스트메뉴', '간판메뉴')           # 'OO' 앞에 오는 지칭어
_POS_NEAR     = ('맛있', '존맛', '최고', '추천', '강추', '유명', '일품', '꿀맛', '맛나')


def _noun_runs(toks):
    """연속된 명사(NNG/NNP) 토큰을 복합명사 구간 (텍스트, 시작idx, 끝idx)으로 묶는다."""
    runs = []
    i, n = 0, len(toks)
    while i < n:
        if toks[i].tag in ("NNG", "NNP"):
            j = i
            forms = []
            while j < n and toks[j].tag in ("NNG", "NNP"):
                forms.append(toks[j].form)
                j += 1
            runs.append((''.join(forms), i, j))
            i = j
        else:
            i += 1
    return runs


def _score_menu_in_review(text: str) -> dict[str, tuple[int, int]]:
    """리뷰 1건에서 메뉴 후보별로 (강한신호 횟수, 총점)을 매긴다.
    강한 신호 = 목적격+취식동사 / 메뉴 지칭어. 약한 신호(긍정어·접미사)는 점수만.
    """
    try:
        toks = _kiwi.tokenize(text)
    except Exception:
        return {}
    runs = _noun_runs(toks)
    out: dict[str, tuple[int, int]] = {}
    for idx, (cand, s, e) in enumerate(runs):
        if (len(cand) < 2 or cand in _STOP_NOUNS or cand in _NON_MENU
                or '메뉴' in cand or '재료' in cand):
            continue
        strong = 0
        score  = 0
        # (1) 목적격 조사 + 취식/주문 동사 → 강한 신호
        if e < len(toks) and toks[e].tag == "JKO":
            for w in range(e + 1, min(e + 5, len(toks))):
                f, tag = toks[w].form, toks[w].tag
                if tag.startswith("VV") and any(f.startswith(st) for st in _EAT_STEMS):
                    strong += 1
                    score  += 3
                    break
                if tag == "NNG" and f in _ORDER_NOUNS:
                    strong += 1
                    score  += 3
                    break
        # (2) 바로 앞에 '대표메뉴/시그니처' 등 지칭어 → 강한 신호
        if idx > 0 and any(mk in runs[idx - 1][0] for mk in _MENU_MARKERS):
            strong += 1
            score  += 3
        # (3) 뒤쪽 근처에 맛있/추천 등 긍정 표현 → 약한 신호 (순위 보정용)
        for w in range(e, min(e + 6, len(toks))):
            if any(toks[w].form.startswith(p) for p in _POS_NEAR):
                score += 1
                break
        # (4) 음식 접미사 → 약한 보조 신호
        if any(cand.endswith(sfx) or cand == sfx for sfx in _FOOD_SUFFIXES):
            score += 1
        if strong or score:
            ps, psc = out.get(cand, (0, 0))
            out[cand] = (ps + strong, psc + score)
    return out


def _extract_menus(sample: list[str], place_name: str) -> list[str]:
    """문맥 점수를 리뷰 전체에 누적해 대표 메뉴를 뽑는다.
    강한 신호(취식·지칭)가 1회 이상인 후보만 인정 → 비메뉴 노이즈를 거른다.
    일반 카테고리어(피자·커피 등)는 구체적 메뉴보다 뒤로 민다.
    """
    strong: dict[str, int] = {}
    score:  dict[str, int] = {}
    freq:   dict[str, int] = {}
    for text in sample:
        for cand, (st, sc) in _score_menu_in_review(text).items():
            if cand == place_name:
                continue
            strong[cand] = strong.get(cand, 0) + st
            score[cand]  = score.get(cand, 0) + sc
            freq[cand]   = freq.get(cand, 0) + 1

    # 후보 인정: 강한 문맥 신호가 있거나, 음식 접미사어가 2회 이상 언급된 경우
    #  → 문맥의 정밀도 + 접미사의 재현율을 결합 (한쪽만 쓰면 놓치거나 노이즈가 많음)
    cands = [c for c in strong
             if strong[c] >= 1 or (_has_food_suffix(c) and freq[c] >= 2)]
    if not cands:
        return []

    # 구체적 메뉴 > 일반 카테고리어, 그다음 강한신호·총점·빈도 순
    cands.sort(key=lambda c: (c not in _GENERIC_MENU, strong[c], score[c], freq[c]),
               reverse=True)

    menus: list[str] = []
    for cand in cands:
        dominated = False
        for existing in list(menus):
            if existing in cand:        # 기존이 현재의 부분 → 더 긴 것으로 교체
                menus.remove(existing)
            elif cand in existing:      # 현재가 기존의 부분 → 스킵
                dominated = True
                break
        if not dominated:
            menus.append(cand)
        if len(menus) >= 5:
            break
    return menus[:5]


def _aspect_quotes(reviews_typed: list[tuple[str, str]], keywords: list[str],
                   max_quotes: int = 2) -> tuple[list[dict], int]:
    """키워드가 언급된 리뷰 수(total)와 대표 인용문(앞뒤 30자 문맥)을 반환."""
    quotes: list[dict] = []
    total = 0
    for text, rtype in reviews_typed:
        low = text.lower()
        hit = next((kw for kw in keywords if kw.lower() in low), None)
        if not hit:
            continue
        total += 1
        if len(quotes) < max_quotes:
            idx = low.find(hit.lower())
            start = max(0, idx - 30)
            end   = min(len(text), idx + len(hit) + 30)
            ctx = ('…' if start > 0 else '') + text[start:end].strip() + ('…' if end < len(text) else '')
            quotes.append({"text": ctx, "type": rtype, "kw": hit})
    return quotes, total


def _norm_text(t: str) -> str:
    """공백 제거 정규화 (동일 리뷰 비교용)."""
    return ''.join(t.split())


_HASHTAG_RE = re.compile(r'#\S+')


def _is_clip_review(text: str) -> bool:
    """해시태그 위주의 '클립/챌린지' 글 판별 (실질 본문이 거의 없는 것).
    예: '#오늘클립챌린지 #국내여행 #천안여행 #빵집추천' → 본문 없음 → 제외.
    해시태그를 걷어낸 뒤 남는 한글 본문이 12자 미만이면 클립으로 본다.
    """
    core = re.sub(r'[^가-힣]', '', _HASHTAG_RE.sub('', text))
    return len(core) < 12


def _is_valid_blog(text: str) -> bool:
    """블로그 리뷰 유효성: 최소 길이 충족 + 해시태그 클립이 아님.
    (길이 컷은 완화하고, 쓰레기 클립은 _is_clip_review 로 정밀 제거.)"""
    return len(text) >= BLOG_MIN_LEN and not _is_clip_review(text)


def _dedup_blog_against_visitor(visitor: list[str], blog: list[str]) -> tuple[list[str], int]:
    """블로그 리뷰 중 방문자 리뷰와 사실상 동일한 것을 제거한다 (이중 집계 방지).
    블로그 수집기가 방문자 리뷰를 섞어오는 경우를 거르고, (정제된 블로그, 제거된 수)를 반환.
    """
    vnorm = {_norm_text(t) for t in visitor if isinstance(t, str)}
    out: list[str] = []
    seen: set[str] = set()
    removed = 0
    for t in blog:
        if not isinstance(t, str):
            continue
        n = _norm_text(t)
        if n in vnorm or n in seen:   # 방문자와 중복 or 블로그 내부 중복
            removed += 1
            continue
        seen.add(n)
        out.append(t)
    return out, removed


def build_review_analysis(visitor_valid: list[str], blog_valid: list[str],
                          place_name: str = "") -> dict:
    """유효 리뷰 → 프론트가 그대로 렌더링하는 분석 구조 생성.
    방문자와 중복되는 블로그 리뷰는 제거하고(이중 집계 방지), 실제 분석된 수만 보고한다.
    반환:
      menus            : list[str]        대표 메뉴
      reservation_apps : list[str]        리뷰에서 감지된 예약 앱
      aspects          : list[{key,label,icon,count,quotes:[{text,type,kw}]}]
      analyzed         : int              분석한 총 리뷰 수 (중복 제거 후)
      counts           : {visitor, blog}  타입별 분석 수
      shortage         : {visitor, blog}  리뷰가 신뢰 기준 미만이면 True
    """
    visitor = [t for t in (visitor_valid or []) if isinstance(t, str)]
    blog_raw, _removed = _dedup_blog_against_visitor(visitor, blog_valid or [])
    # 해시태그 클립·초단문만 제외 (정상 블로그 스니펫은 짧아도 인정)
    blog = [t for t in blog_raw if _is_valid_blog(t)]

    reviews_typed = ([(t, "visitor") for t in visitor] +
                     [(t, "blog")    for t in blog])
    if len(reviews_typed) < 3:
        return {}

    sample = [t for t, _ in reviews_typed][:150]
    menus = _extract_menus(sample, place_name) if NLP_AVAILABLE else []

    aspects = []
    for asp in REVIEW_ASPECTS:
        quotes, total = _aspect_quotes(reviews_typed, asp["keywords"])
        if total > 0:
            aspects.append({
                "key": asp["key"], "label": asp["label"], "icon": asp["icon"],
                "count": total, "quotes": quotes,
            })

    all_text = " ".join(t for t, _ in reviews_typed)
    reservation_apps = [a for a in RESERVATION_APPS if a in all_text]

    n_v, n_b = len(visitor), len(blog)
    return {
        "menus": menus,
        "reservation_apps": reservation_apps,
        "aspects": aspects,
        "analyzed": n_v + n_b,
        "counts": {"visitor": n_v, "blog": n_b},
        "shortage": {"visitor": n_v < VISITOR_LOW, "blog": n_b < BLOG_LOW},
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


def _parse_business_list_from_gql(gql_bodies: list[str]) -> list[dict]:
    """pcmap 응답(Apollo 캐시 또는 GraphQL)에서 음식점 항목 추출.

    pcmap restaurant/list는 Apollo SSR 상태를 'PlaceListBusinessesItem:{id}'
    키로 평탄화 저장하므로, __typename 기준으로 재귀 탐색해 수집한다.
    """
    results: list[dict] = []
    seen_ids: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            if node.get("__typename") == "PlaceListBusinessesItem":
                pid = str(node.get("id", ""))
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    results.append(node)
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    for body in gql_bodies:
        try:
            obj = json.loads(body)
        except Exception:
            continue
        walk(obj)

    return results


def _parse_ad_ids_from_gql(gql_bodies: list[str]) -> set[str]:
    """네이버가 '광고'로 노출하는 업체의 place id를 수집한다.

    광고는 organic 목록(PlaceListBusinessesItem)과 분리된
    'RestaurantAdSummary' 타입(또는 adId 필드를 가진 항목)으로 내려온다.
    동일 업체가 organic 목록에도 함께 뜰 수 있으므로, 이 id 집합으로
    스캔 단계에서 광고 업체를 제외한다.
    """
    ad_ids: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            is_ad = (node.get("__typename") == "RestaurantAdSummary"
                     or bool(node.get("adId")))
            if is_ad:
                pid = str(node.get("id", "")
                          or node.get("apolloCacheId", ""))
                if pid:
                    ad_ids.add(pid)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    for body in gql_bodies:
        try:
            obj = json.loads(body)
        except Exception:
            continue
        walk(obj)

    return ad_ids


def _to_int(v) -> int:
    """'2,424' / '100+' / 2424 → 정수. 실패 시 0."""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r'[\d,]+', str(v))
    if not m:
        return 0
    try:
        return int(m.group(0).replace(",", ""))
    except Exception:
        return 0


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


# ── 수상 인식 (미쉐린·빕구르망·블루리본) ─────────────────────────────
# 네이버 플레이스가 '실제로' 헤더/소개에 표시하는 공식 배지만 인식한다.
#  · 미쉐린: '미쉐린 가이드 서울/부산 2026'(실제 연도)이 렌더링된 경우만.
#    └ i18n 템플릿 '...{{value}}'는 모든 페이지에 존재하므로 \d{4} 로 배제(오탐 방지).
#  · 별 등급은 place_blind 의 'N스타', 빕구르망은 '빕구르망' 텍스트로 구분.
#  · 블루리본: 소개글의 '블루리본 서베이' 문구.
_AW_MICHELIN = re.compile(r'미쉐린\s*가이드\s*(?:서울|부산)\s*\d{4}')
_AW_STAR     = re.compile(r'place_blind[^>]*>\s*([1-3])\s*스타')
_AW_BIB      = re.compile(r'빕\s*구르?망')
_AW_BLUE     = re.compile(r'블루리본\s*서베이')


def _detect_awards(html: str) -> list[str]:
    """네이버 플레이스 페이지 원문에서 공식 수상 배지를 정밀 추출.
    반환 예: ['미쉐린 3스타'], ['빕구르망'], ['블루리본'], ['미쉐린 1스타','블루리본'].
    신호가 없으면 빈 리스트."""
    awards: list[str] = []
    if _AW_MICHELIN.search(html):
        if _AW_BIB.search(html):
            awards.append("빕구르망")
        else:
            m = _AW_STAR.search(html)
            awards.append(f"미쉐린 {m.group(1)}스타" if m else "미쉐린")
    if _AW_BLUE.search(html):
        awards.append("블루리본")
    return awards


# ── 영업 정보 추출 ────────────────────────────────────────────────────
async def _crawl_hours(pid: str, ctx) -> dict:
    """홈 페이지 GraphQL에서 영업시간·휴무일·브레이크타임 + 공식 수상 배지 추출"""
    hours_info = {"business_hours": "", "break_time": "", "closed_days": "", "awards": []}
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

        # 공식 수상 배지 (미쉐린·빕구르망·블루리본) — 렌더된 신호만
        hours_info["awards"] = _detect_awards(all_text)

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

    # ── 2차: GraphQL 파싱 수집 부족 시 DOM fallback ──
    # DOM 경로는 날짜를 얻지 못하므로 date="" 로 채운다 (정렬·검증 불가).
    if len(pairs) < 5:
        dom_texts = await _extract_texts_from_dom(page, target)
        for t in dom_texts:
            if t not in seen and len(pairs) < target:
                seen.add(t)
                pairs.append((t, ""))
        if dom_texts:
            print(f"      [sort-check:{review_type}] DOM fallback 사용 — 날짜 검증 불가")

    await page.close()
    return pairs[:target]  # (text, date) 쌍, 반드시 target 이하


# ── 블로그 전문 수집 (실제 글 본문 분석) ─────────────────────────────
# 플레이스 블로그 탭은 ~400자 미리보기 스니펫만 내려준다. 정확한 분석을 위해
# 각 글의 실제 blog.naver.com 본문 전체를 가져와 분석한다.
BLOG_BODY_CAP = 5000  # 글 1건 저장·분석 상한 (협찬 고지가 보통 하단이라 넉넉히)
BLOG_CONCURRENCY = 5  # 블로그 본문 동시 수집 수 (순차는 곳당 수 분 → 병렬로 단축)

# 글 본문 추출 셀렉터 (스마트에디터 ONE → 구버전 → 기타 순)
_BLOG_BODY_JS = """() => {
  const sels = ['.se-main-container', '#postViewArea', '.post_ct', '#viewTypeSelector'];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el && el.innerText && el.innerText.trim().length > 50) return el.innerText;
  }
  return '';
}"""

# 블로그 탭에서 각 글의 URL(+카드 텍스트) 수집 — 본문 글만(작성자 프로필 링크 제외)
_BLOG_URLS_JS = """() => {
  const seen = new Set(), out = [];
  for (const a of document.querySelectorAll('a[href*="blog.naver.com"]')) {
    const href = a.href;
    if (!/blog\\.naver\\.com\\/[^/?]+\\/\\d+/.test(href)) continue;  // /{id}/{logNo}
    if (seen.has(href)) continue;
    seen.add(href);
    let card = a;
    for (let i = 0; i < 6 && card; i++) { if (card.tagName === 'LI') break; card = card.parentElement; }
    out.push({ href, card: (card || a).innerText || '' });
  }
  return out;
}"""

_BLOG_DATE_RE = re.compile(
    r'\d{4}[.\-]\d{1,2}[.\-]\d{1,2}'      # 2026.06.04 / 2026-06-04
    r'|\d{1,2}\.\d{1,2}\.[월화수목금토일]'  # 6.1.월
    r'|\d{4}년\s*\d{1,2}월\s*\d{1,2}일'
)


def _clean_blog_body(text: str) -> str:
    """블로그 본문 정제: 과도한 빈 줄 정리 + 길이 상한."""
    text = re.sub(r'\n{3,}', '\n\n', (text or '').strip())
    return text[:BLOG_BODY_CAP]


async def _fetch_blog_fulltext(url: str, ctx) -> str:
    """blog.naver.com 글 1건의 본문 전문을 추출. 실패 시 빈 문자열.
    iframe 우회를 위해 PostView.naver 직접 URL로 접근한다."""
    m = re.search(r'blog\.naver\.com/([^/?]+)/(\d+)', url)
    if not m:
        return ""
    view = f"https://blog.naver.com/PostView.naver?blogId={m.group(1)}&logNo={m.group(2)}"
    page = await ctx.new_page()
    try:
        # 본문(se-main-container)은 초기 HTML에 서버 렌더되므로 domcontentloaded면 충분
        await page.goto(view, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(0.5)
        body = await page.evaluate(_BLOG_BODY_JS)
    except Exception:
        body = ""
    finally:
        await page.close()
    return _clean_blog_body(body)


async def _crawl_blog_fulltext(pid: str, ctx, target: int = 25) -> list[tuple[str, str]]:
    """블로그 탭에서 글 URL 목록을 최신순으로 모은 뒤, 각 글의 본문 전문을 수집.
    반환: [(본문, 작성일), ...] (최신순, target 이하). 본문 추출 실패 건은 제외.
    """
    page = await ctx.new_page()
    for url in (f"{BASE_PCMAP}/restaurant/{pid}/review/ugc?sort=recent",
                f"{BASE_PCMAP}/restaurant/{pid}/review/ugc"):
        try:
            await page.goto(url, wait_until="load", timeout=22000)
            break
        except Exception:
            continue
    await asyncio.sleep(3)
    await _click_latest_sort(page)
    await asyncio.sleep(1.5)

    # URL을 스크롤 라운드마다 '누적'한다 — 인라인 '더보기' 클릭이 블로그 앵커를
    # 지워버리는 경우가 있어, 마지막 DOM 상태만 긁으면 0건이 될 수 있다.
    # 바닥 '더보기' 버튼만 클릭(인라인 펼치기 a:has-text('더보기')는 제외).
    collected: dict[str, str] = {}  # href → 작성일 (삽입 순서 = 최신순 유지)
    prev = -1
    stall = 0
    for _ in range(20):
        try:
            items = await page.evaluate(_BLOG_URLS_JS)
        except Exception:
            items = []
        for it in items:
            href = it.get("href", "")
            if href and href not in collected:
                dm = _BLOG_DATE_RE.search(it.get("card", ""))
                collected[href] = _normalize_date(dm.group(0)) if dm else ""
        # 본문 추출 실패·방문자 중복 제거로 줄어들 것을 대비해 여유분(+12) 확보
        if len(collected) >= target + 12:
            break
        if len(collected) == prev:
            stall += 1
            if stall >= 3:
                break
        else:
            stall = 0
        prev = len(collected)

        for sel in ["a.fvwqf", ".more_btn", "a[class*='more']", "button[class*='more']"]:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(1.2)
                    break
            except Exception:
                continue
        await page.evaluate("window.scrollBy(0, 1200)")
        await asyncio.sleep(0.7)

    await page.close()
    url_dates: list[tuple[str, str]] = list(collected.items())[:target + 12]

    # 방문자 중복 제거(crawl_place 단계)로 더 줄어드므로 target보다 약간 더 확보한다.
    fetch_goal = target + 4
    print(f"      [blog-전문] 글 링크 {len(url_dates)}건 → 본문 병렬 수집 (목표 {fetch_goal}건)")

    # 블로그 글마다 페이지 1회라 순차는 느리다 → 동시 BLOG_CONCURRENCY개씩 병렬 수집.
    out: list[tuple[str, str]] = []
    idx = 0
    while idx < len(url_dates) and len(out) < fetch_goal:
        chunk = url_dates[idx: idx + BLOG_CONCURRENCY]
        idx += BLOG_CONCURRENCY
        bodies = await asyncio.gather(
            *(_fetch_blog_fulltext(href, ctx) for href, _ in chunk),
            return_exceptions=True)
        for (href, date), body in zip(chunk, bodies):   # 최신순 유지
            if isinstance(body, str) and len(body) >= BLOG_MIN_LEN:
                out.append((body, date))
    print(f"      [blog-전문] 본문 확보 {len(out)}건")
    return out[:fetch_goal]


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
    ad_ids: set[str] = set()   # 네이버가 '광고'로 노출하는 업체 id
    total_seen = 0
    skip_chain = 0
    skip_reviews = 0
    skip_ad = 0

    async with _stealth.use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={"width": 1600, "height": 900},
            locale="ko-KR",
            extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9"}
        )
        page = await ctx.new_page()

        async def on_response(resp):
            nonlocal total_seen, skip_chain, skip_reviews, skip_ad
            if "allSearch" not in resp.url:
                return
            try:
                body = await resp.json()
                place_obj = body.get("result", {}).get("place", {})
                items = place_obj.get("list", [])
                # 광고 업체 id 수집 (organic 항목에도 같이 떠도 제외하기 위함)
                ad_ids.update(_parse_ad_ids_from_gql([json.dumps(body, ensure_ascii=False)]))
                for item in items:
                    pid = str(item.get("id", ""))
                    if not pid or pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    total_seen += 1

                    name = item.get("name", "")

                    # ── 스캔 단계 필터 ──
                    # 네이버 '광고' 노출 업체 제외
                    if pid in ad_ids or item.get("adId"):
                        ad_ids.add(pid)
                        skip_ad += 1
                        continue

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

        # ── pcmap restaurant/list iframe GraphQL로 추가 결과 수집 ──
        # allSearch는 통합검색이라 첫 20개만 반환(페이지네이션 X).
        # 실제 목록은 pcmap.place.naver.com/restaurant/list iframe이 담당하며
        # GraphQL로 데이터를 받아오고 display 파라미터로 페이지당 건수 제어.
        pcmap_url = None
        for fr in page.frames:
            if "pcmap.place.naver.com/restaurant/list" in (fr.url or ""):
                pcmap_url = fr.url
                break

        if not pcmap_url:
            print("  [pcmap] restaurant/list iframe URL 미발견 — allSearch 결과만 사용")
        else:
            # pcmap restaurant/list 페이지를 직접 열고, 내부 스크롤 컨테이너를
            # 스크롤해 lazy-load를 유발한다. 추가 로드된 항목은 Apollo 캐시에
            # 누적되므로 매 라운드 캐시를 다시 읽어 중복 제거하며 모은다.
            list_page = await ctx.new_page()
            nav_url = re.sub(r'display=\d+', 'display=100', pcmap_url)
            if 'display=' not in nav_url:
                nav_url += '&display=100'
            nav_url = re.sub(r'[?&](?:page|start)=\d+', '', nav_url)

            # 스크롤로 새로 발생하는 GraphQL 응답도 보조 소스로 캡처
            gql_extra: list[str] = []

            async def cap_list_resp(resp):
                try:
                    if "json" not in (resp.headers or {}).get("content-type", ""):
                        return
                    txt = await resp.text()
                    if len(txt) > 200 and "PlaceListBusinessesItem" in txt:
                        gql_extra.append(txt)
                except Exception:
                    pass

            list_page.on("response", cap_list_resp)

            try:
                await list_page.goto(nav_url, wait_until="load", timeout=25000)
            except Exception:
                pass
            await asyncio.sleep(2.5)

            # 실제 스크롤되는 안쪽 컨테이너를 찾아 끝까지 스크롤 (창 스크롤 X)
            # ※ pcmap 단독 페이지는 한 쿼리당 최대 100개. 스크롤은 lazy-load된
            #   항목을 Apollo 캐시에 모두 반영시키기 위함 (100 초과 불가).
            scroll_js = """() => {
                let best = null, bestH = 0;
                for (const el of document.querySelectorAll('div, ul, section')) {
                    const s = getComputedStyle(el);
                    if ((s.overflowY === 'auto' || s.overflowY === 'scroll')
                        && el.scrollHeight > el.clientHeight + 100) {
                        if (el.scrollHeight > bestH) { best = el; bestH = el.scrollHeight; }
                    }
                }
                if (best) { best.scrollTop = best.scrollHeight; return best.scrollHeight; }
                window.scrollTo(0, document.body.scrollHeight);
                return -1;
            }"""

            def _ingest(items: list[dict]) -> int:
                """파싱된 음식점 항목을 places에 반영. 신규 통과 건수 반환."""
                added = 0
                for item in items:
                    if len(places) >= limit:
                        break
                    pid = str(item.get("id", ""))
                    if not pid or pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    total_seen_ref[0] += 1

                    # 네이버 '광고' 노출 업체 제외
                    if pid in ad_ids:
                        skip_ad_ref[0] += 1
                        continue

                    name = item.get("name", "")
                    if not name:
                        continue
                    if _is_chain(name):
                        skip_chain_ref[0] += 1
                        continue

                    visitor_count = _to_int(item.get("visitorReviewCount"))
                    blog_count    = _to_int(item.get("blogCafeReviewCount"))
                    total_count   = visitor_count + blog_count
                    if total_count < min_reviews:
                        skip_reviews_ref[0] += 1
                        continue

                    raw_cat = item.get("category")
                    cat = raw_cat if isinstance(raw_cat, str) and raw_cat else "기타"
                    nbh = item.get("newBusinessHours") or {}
                    biz_status = nbh.get("status", "") if isinstance(nbh, dict) else ""
                    biz_hours  = nbh.get("description", "") if isinstance(nbh, dict) else ""

                    places.append({
                        "id":                   pid,
                        "name":                 name,
                        "category":             cat,
                        "address":              item.get("roadAddress") or item.get("address") or item.get("commonAddress", ""),
                        "x":                    item.get("x", "0"),
                        "y":                    item.get("y", "0"),
                        "review_count":         total_count,
                        "visitor_review_count": visitor_count,
                        "blog_review_count":    blog_count,
                        "business_status":      biz_status,
                        "business_hours_today": biz_hours,
                        "break_time_today":     "",
                        "last_order":           "",
                        "last_review_date":     "",
                    })
                    added += 1
                return added

            total_seen_ref   = [total_seen]
            skip_chain_ref   = [skip_chain]
            skip_reviews_ref = [skip_reviews]
            skip_ad_ref      = [skip_ad]

            prev_parsed = -1
            stall = 0
            rnd = 0
            for rnd in range(20):  # 안전 상한 (보통 2~3라운드 내 100개 확보)
                # Apollo 캐시 + 스크롤로 캡처된 응답을 합쳐 파싱
                apollo_state = None
                try:
                    apollo_state = await list_page.evaluate(
                        "() => window.__APOLLO_STATE__ ? JSON.stringify(window.__APOLLO_STATE__) : null"
                    )
                except Exception:
                    pass
                sources = ([apollo_state] if apollo_state else []) + gql_extra
                # 광고 업체 id 먼저 수집 → organic 항목에서도 제외
                ad_ids.update(_parse_ad_ids_from_gql(sources))
                biz_items = _parse_business_list_from_gql(sources)

                _ingest(biz_items)
                total_seen   = total_seen_ref[0]
                skip_chain   = skip_chain_ref[0]
                skip_reviews = skip_reviews_ref[0]
                skip_ad      = skip_ad_ref[0]

                if len(places) >= limit:
                    break

                # 파싱된 전체 건수가 더 안 늘면 stall (100 도달 → 종료)
                if len(biz_items) == prev_parsed:
                    stall += 1
                    if stall >= 2:
                        break
                else:
                    stall = 0
                prev_parsed = len(biz_items)

                # 내부 컨테이너 스크롤 → 남은 항목을 Apollo 캐시에 반영
                try:
                    await list_page.evaluate(scroll_js)
                except Exception:
                    pass
                await asyncio.sleep(1.0)

            print(f"  [pcmap] 통과 {len(places)}개 / 스캔 {total_seen}개")
            await list_page.close()

        await ctx.close()

    result = places[:limit]
    print(f"[naver] 스캔 완료:")
    print(f"  전체 검색결과:   {total_seen}개")
    print(f"  광고 감지:       {len(ad_ids)}건  (organic 목록과 분리 노출)")
    print(f"  └ organic 중복 제외: -{skip_ad}개  (광고가 일반 목록에도 끼어든 경우)")
    print(f"  프랜차이즈 제외: -{skip_chain}개")
    print(f"  리뷰부족 제외:   -{skip_reviews}개  (기준: {min_reviews}건 미만)")
    print(f"  최종 통과:       {len(result)}개")
    return result


# ── 개별 장소 크롤링 ──────────────────────────────────────────────────
async def crawl_place(place: dict, ctx) -> dict | None:
    """
    방문자 리뷰 ≤100건 + 블로그 리뷰 기본 25건(방문자 부족 시 최대 50건)
    + 영업정보 수집. 리뷰는 (텍스트, 작성일) 쌍으로 최신순 저장.
    """
    pid  = place["id"]
    name = place["name"]

    visitor_api_cnt = int(place.get("visitor_review_count", 0))
    blog_api_cnt    = int(place.get("blog_review_count",
                          max(0, int(place.get("review_count", 0)) - visitor_api_cnt)))
    total_reviews   = visitor_api_cnt + blog_api_cnt

    print(f"  -> {name} 수집 시작 (방문자 {visitor_api_cnt}건, 블로그 {blog_api_cnt}건)")

    # ── 영업 정보 + 수상 배지 ──
    hours_info = await _crawl_hours(pid, ctx)
    if hours_info.get("awards"):
        print(f"    [수상] {', '.join(hours_info['awards'])}")

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
    # _crawl_reviews 는 (텍스트, 작성일) 쌍을 최신순으로 반환한다.
    visitor_pairs = await _crawl_reviews(pid, "visitor", ctx, target=VISITOR_TARGET)
    visitor_texts = [t for t, _ in visitor_pairs]
    visitor_date_of = {t: d for t, d in visitor_pairs}   # 텍스트→작성일 (저장용)
    visitor_filtered = filter_reviews(visitor_texts)

    # 방문자 부족분 계산 → 블로그 목표에 추가 (동적 보충)
    visitor_shortage = max(0, VISITOR_TARGET - len(visitor_texts))
    blog_target_adj  = BLOG_TARGET + visitor_shortage
    if visitor_shortage:
        print(f"    방문자 리뷰 {len(visitor_texts)}/{VISITOR_TARGET}건 "
              f"→ 블로그 목표 {blog_target_adj}건으로 증가")

    # ── 블로그 리뷰 (부족분 보완된 목표) — 실제 글 본문 전문 수집 ──
    # 플레이스 블로그 탭의 ~400자 미리보기가 아니라 blog.naver.com 글 본문 전체를
    # 가져와 분석한다. 전문 수집 실패 시(접근불가·구조변경) 스니펫으로 폴백.
    blog_pairs = await _crawl_blog_fulltext(pid, ctx, target=blog_target_adj)
    if not blog_pairs:
        print("    [blog-전문] 본문 0건 → 미리보기 스니펫으로 폴백")
        blog_pairs = await _crawl_reviews(pid, "blog", ctx, target=blog_target_adj)
    blog_texts = [t for t, _ in blog_pairs]
    blog_date_of = {t: d for t, d in blog_pairs}
    blog_filtered = filter_reviews(blog_texts)

    # 방문자와 중복 제거 + 해시태그 클립·초단문 제거 (_is_valid_blog)
    blog_valid_dedup, blog_cross_dup = _dedup_blog_against_visitor(
        visitor_filtered["valid"], blog_filtered["valid"])
    blog_valid_dedup = [t for t in blog_valid_dedup if _is_valid_blog(t)]
    if blog_cross_dup:
        print(f"    블로그-방문자 중복 {blog_cross_dup}건 제거")
    print(f"    블로그 유효({BLOG_MIN_LEN}자+): {len(blog_valid_dedup)}건")

    # ── 분석·저장에 쓸 최종 유효 리뷰 (상한 적용을 한 곳에서) ──
    #   방문자: 최대 VISITOR_TARGET(100), 블로그: 최대 BLOG_CAP(50).
    #   분석 입력과 저장본을 동일한 리스트로 통일한다 (crawl ↔ reanalyze 일치).
    visitor_valid = visitor_filtered["valid"][:VISITOR_TARGET]
    blog_valid    = blog_valid_dedup[:BLOG_CAP]

    # 블로그 부족분 로깅
    if len(blog_valid) < BLOG_TARGET:
        print(f"    블로그 유효 리뷰 {len(blog_valid)}/{BLOG_TARGET}건 "
              f"(해당 업체 블로그 리뷰 부족)")

    # ── 리뷰 분석 (메뉴·항목별 인용·예약앱) ──
    review_analysis = build_review_analysis(
        visitor_valid,
        blog_valid,
        name,
    )
    if review_analysis:
        print(f"    [분석] 메뉴:{review_analysis.get('menus')} "
              f"항목:{[a['key'] for a in review_analysis.get('aspects', [])]}")

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

        # 공식 수상 (네이버가 표시한 경우만)
        "awards": hours_info.get("awards", []),

        # 방문자 리뷰
        "visitor_total":      visitor_api_cnt,
        "visitor_sampled":    len(visitor_texts),
        "visitor_valid":      len(visitor_valid),
        "visitor_ad_count":   visitor_filtered["ad_count"],
        "visitor_dup_count":  visitor_filtered["duplicate_count"],

        # 블로그 리뷰 (방문자 중복 제거 + 상한 적용 후 기준)
        "blog_total":         blog_api_cnt,
        "blog_sampled":       len(blog_valid),
        "blog_valid":         len(blog_valid),
        "blog_ad_count":      blog_filtered["ad_count"],
        "blog_dup_count":     blog_filtered["duplicate_count"] + blog_cross_dup,

        # 합산 (등재 기준은 방문자 유효 리뷰)
        "total_reviews":    total_reviews,
        "filtered_reviews": len(visitor_valid),
    }

    # 감성 분석 — 방문자 + (중복 제거·상한 적용된) 블로그 합산으로 재계산
    sent = analyze_sentiment(visitor_valid + blog_valid)
    result.update({
        "positive_rate":      sent["positive_rate"],
        "sentiment_positive": sent["positive"],
        "sentiment_negative": sent["negative"],
        "sentiment_neutral":  sent["neutral"],
        "sentiment_total":    sent["sentiment_total"],
        "mindless_excluded":  sent["mindless_excluded"],
        # 리뷰 분석 결과
        "review_analysis":   review_analysis,
        # 유효 리뷰 텍스트 + 작성일 저장 (방문자 ≤100, 블로그 ≤50)
        # 작성일은 텍스트와 같은 순서(최신순)의 병렬 배열, YYYY-MM-DD 정규화.
        # 날짜를 못 얻은 경우(DOM fallback 등)는 "".
        "visitor_reviews":       visitor_valid,
        "visitor_reviews_dates": [_normalize_date(visitor_date_of.get(t, "")) for t in visitor_valid],
        "blog_reviews":          blog_valid,
        "blog_reviews_dates":    [_normalize_date(blog_date_of.get(t, "")) for t in blog_valid],
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
