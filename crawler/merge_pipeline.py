"""
merge_pipeline.py — 네이버 크롤링 결과 → places.json / places.js 생성

판별 기준 (별점 없음):
  인기    filtered_reviews >= 200
  검증됨  filtered_reviews >= 50
  리뷰부족 filtered_reviews < 50

사용법:
  python merge_pipeline.py
  python merge_pipeline.py --naver data/raw/naver/naver_xxx.json
"""

import json
import argparse
from pathlib import Path
from datetime import datetime

OUTPUT_JSON = Path("data/places.json")
OUTPUT_JS   = Path("data/places.js")


MIN_REVIEWS = 50   # 등재 최소 기준 (merge_pipeline 단계)


def process(naver_list: list) -> list[dict]:
    places = []
    for p in naver_list:
        fr = p.get("filtered_reviews", 0)

        # 기준 미달 업소는 완전 제외 (지도에 표시 안 함)
        if fr < MIN_REVIEWS:
            continue

        places.append({
            "id":       str(p.get("id", "")),
            "name":     p.get("name", ""),
            "category": p.get("category", "기타"),
            "address":  p.get("address", ""),
            "lat":      float(p.get("lat", 0)),
            "lng":      float(p.get("lng", 0)),
            "verdict":  "맛집",

            # 영업 정보
            "business_hours":  p.get("business_hours", ""),
            "break_time":      p.get("break_time", ""),
            "closed_days":     p.get("closed_days", ""),
            "business_status": p.get("business_status", ""),

            # 방문자 리뷰
            "visitor_total":    p.get("visitor_total", p.get("visitor_reviews", 0)),
            "visitor_sampled":  p.get("visitor_sampled", 0),
            "visitor_valid":    p.get("visitor_valid", fr),
            "visitor_ad_count": p.get("visitor_ad_count", p.get("ad_count", 0)),
            "visitor_dup_count":p.get("visitor_dup_count", 0),

            # 블로그 리뷰
            "blog_total":    p.get("blog_total", 0),
            "blog_sampled":  p.get("blog_sampled", 0),
            "blog_valid":    p.get("blog_valid", 0),
            "blog_ad_count": p.get("blog_ad_count", 0),
            "blog_dup_count":p.get("blog_dup_count", 0),

            "total_reviews":    p.get("total_reviews", 0),
            "filtered_reviews": fr,

            # KoNLPy 분석
            "review_analysis":    p.get("review_analysis", {}),

            # 감성 분석
            "positive_rate":      p.get("positive_rate", 0),
            "sentiment_positive": p.get("sentiment_positive", 0),
            "sentiment_negative": p.get("sentiment_negative", 0),
            "sentiment_neutral":  p.get("sentiment_neutral", 0),
            "sentiment_total":    p.get("sentiment_total", 0),
            "mindless_excluded":  p.get("mindless_excluded", 0),

            "tags":      p.get("tags", []),
            "merged_at": datetime.now().isoformat(),
        })
    # 유효 리뷰 많은 순 정렬
    places.sort(key=lambda x: x["filtered_reviews"], reverse=True)
    return places


def load_latest(directory: str) -> list[dict]:
    p = Path(directory)
    if not p.exists():
        return []
    files = sorted(p.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return []
    with open(files[0], encoding="utf-8") as f:
        data = json.load(f)
    print(f"  로드: {files[0]} ({len(data)}개)")
    return data if isinstance(data, list) else [data]


def save_outputs(places: list[dict]):
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(places, f, ensure_ascii=False, indent=2)

    js = "window.PLACES_DATA = " + json.dumps(places, ensure_ascii=False, indent=2) + ";\n"
    with open(OUTPUT_JS, "w", encoding="utf-8") as f:
        f.write(js)

    print(f"[OK] {len(places)}개 저장: {OUTPUT_JSON}")
    print(f"[OK] JS 저장: {OUTPUT_JS}")


def main(args):
    print("=== 파이프라인 시작 ===\n")

    if args.naver:
        with open(args.naver, encoding="utf-8") as f:
            naver_list = json.load(f)
        print(f"[naver] 파일 로드: {args.naver} ({len(naver_list)}개)")
    else:
        print("[naver] 최신 파일 자동 로드")
        naver_list = load_latest("data/raw/naver")

    print(f"\n네이버 {len(naver_list)}개")

    places = process(naver_list)

    print(f"\n맛집 등재: {len(places)}개 (유효 리뷰 {MIN_REVIEWS}건 이상)")

    coord_ok = sum(1 for p in places if p["lat"] and p["lng"])
    print(f"좌표 확보: {coord_ok}/{len(places)}개\n")

    save_outputs(places)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--naver", type=str)
    main(parser.parse_args())
