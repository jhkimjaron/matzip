"""
지역×카테고리 풀에서 상위 10곳을 선정한다.

점수 = blend(우리 키워드 감성분석 positive_rate, 네이버 자체 평점 naver_rating)
       + 부산 로컬 가점(향토 음식/디저트 키워드 매칭 시)

두 지표를 섞는 이유: positive_rate는 음식 어휘 기반 키워드 매칭이라 명소·일부
카페(뷰 위주)에서 리뷰가 '중립'으로 새는 경우가 있고, naver_rating은 네이버가
집계한 별점이라 이런 편향이 없다. 서로 다른 오차 특성을 가진 두 지표를 섞어
한쪽 편향에 전체 랭킹이 휘둘리지 않게 한다.

가점 사유는 반드시 보고서에 근거(어떤 키워드가 매칭됐는지)와 함께 표기한다 —
근거 없는 '로컬 보정'은 신뢰할 수 없는 임의 조정이 되기 때문.
"""
import importlib.util
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "places.db"

# gen_report.py의 메뉴 오탐 필터(clean_menus)를 재사용 — 두 스크립트가 같은
# review_analysis.menus를 읽으므로 필터도 동일하게 적용해야 랭킹·보고서가 일치한다.
_spec = importlib.util.spec_from_file_location("gen_report", ROOT / "reports" / "gen_report.py")
_gen_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gen_report)
clean_menus = _gen_report.clean_menus

# 부산 향토 음식/디저트로 널리 알려진 키워드만 엄선 (전국구 음식은 제외).
# category, name, review_analysis.menus 중 하나라도 매칭되면 가점.
BUSAN_LOCAL_KEYWORDS = {
    "밀면": "부산 대표 향토음식(밀면)",
    "돼지국밥": "부산 대표 향토음식(돼지국밥)",
    "씨앗호떡": "부산 명물 간식(씨앗호떡)",
    "곰장어": "부산 명물(꼼장어구이)",
    "낙곱새": "부산식 낙지·곱창·새우 조합(낙곱새)",
    "냉채족발": "부산식 냉채족발",
    "완당": "부산식 만두국(완당)",
    "비빔당면": "부산 명물(비빔당면)",
    "동래파전": "부산 향토음식(동래파전)",
    "재첩국": "부산 향토음식(재첩국)",
    "삼진어묵": "부산 어묵 명물(삼진어묵)",
    "부산어묵": "부산 명물(부산어묵)",
    "유부주머니": "부산식 유부주머니",
}
BUSAN_BONUS = 4  # positive_rate/naver_rating 100점 만점 스케일 기준 가점
# 지역×카테고리당 가점을 인정하는 최대 곳수. 로컬 키워드 매칭이 흔한 편이라
# (돼지국밥·밀면·곰장어 등) 가점 없이 상한도 없으면 TOP10이 특정 향토음식
# 위주로 쏠려 다양성이 사라진다 — base_score(가점 전 순수 점수) 상위 N곳에만
# 가점을 실제로 적용해 "그래도 잘하는 로컬맛집"만 우대하고 나머지는 가점 없이
# 순수 실력으로 경쟁시킨다.
MAX_BUSAN_BONUS_PER_GROUP = 4


def busan_bonus_reason(name: str, category: str, menus: list[str]) -> str | None:
    haystack = f"{name} {category} {' '.join(menus)}"
    for kw, reason in BUSAN_LOCAL_KEYWORDS.items():
        if kw in haystack:
            return reason
    return None


def blended_score(positive_rate: float, naver_rating: float) -> float:
    """positive_rate(0~100)와 naver_rating(0~5→0~100 환산)을 50:50 블렌드.
    naver_rating이 없으면(구버전 크롤 데이터 등) positive_rate만 사용."""
    if naver_rating and naver_rating > 0:
        return positive_rate * 0.5 + (naver_rating / 5 * 100) * 0.5
    return float(positive_rate)


def select_top(region: str, category: str, n: int = 10, min_valid: int = 30) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    area = f"부산 {region} {category}"
    rows = conn.execute(
        "SELECT * FROM places WHERE area=? AND scan_only=0", (area,)
    ).fetchall()
    conn.close()

    candidates = []
    for r in rows:
        d = dict(r)
        total_valid = d["visitor_valid"] + d["blog_valid"]
        if total_valid < min_valid:
            continue
        ra = json.loads(d["review_analysis"] or "{}")
        menus = clean_menus(ra.get("menus", []), d["name"])
        reason = busan_bonus_reason(d["name"], d["category"], menus) if category != "명소" else None
        base = blended_score(d["positive_rate"], d["naver_rating"])
        d["_base_score"] = round(base, 1)
        d["_local_reason"] = reason  # 가점 대상 후보 여부(아직 미확정)
        candidates.append(d)

    # 로컬 키워드 매칭 후보 중 base_score 상위 MAX_BUSAN_BONUS_PER_GROUP 곳에만
    # 실제로 가점을 준다 — "매칭됐다고 다 가점"이 아니라 "그중에서도 잘하는 곳만 우대".
    local_ranked = sorted(
        (c for c in candidates if c["_local_reason"]),
        key=lambda x: -x["_base_score"],
    )
    bonus_ids = {c["id"] for c in local_ranked[:MAX_BUSAN_BONUS_PER_GROUP]}

    for d in candidates:
        got_bonus = d["id"] in bonus_ids
        d["_busan_reason"] = d["_local_reason"] if got_bonus else None
        d["_score"] = round(d["_base_score"] + (BUSAN_BONUS if got_bonus else 0), 1)
        del d["_local_reason"]

    candidates.sort(key=lambda x: -x["_score"])
    return candidates[:n]


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for region in ["서면", "해운대", "광안리", "기장"]:
        for category in ["맛집", "카페", "명소"]:
            top = select_top(region, category)
            print(f"\n=== {region} {category} TOP{len(top)} ===")
            for d in top:
                tag = f" [+{BUSAN_BONUS} {d['_busan_reason']}]" if d["_busan_reason"] else ""
                print(f"  {d['_score']:>5.1f}  {d['name']:<24} "
                      f"(감성{d['positive_rate']}% / 평점{d['naver_rating']}){tag}")
