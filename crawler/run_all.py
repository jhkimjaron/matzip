"""
run_all.py — 네이버 크롤링 → 파이프라인 → places.json 생성

사용법:
  python crawler/run_all.py --query "천안시 음식점"           # 기본 500개, 최소리뷰 100건
  python crawler/run_all.py --query "서울 강남구 음식점" --limit 500
  python crawler/run_all.py --query "홍대 음식점" --limit 200 --min-reviews 50

필터 (스캔 단계):
  - 방문자+블로그 합산 --min-reviews 건 이상 (기본 100)
  - 프랜차이즈 자동 제외 (맥도날드, 스타벅스 등)
"""

import asyncio
import argparse
import subprocess
import sys
import os
from pathlib import Path
from datetime import datetime

CRAWLER_DIR = Path(__file__).parent
ROOT_DIR    = CRAWLER_DIR.parent


async def run_naver(query: str, limit: int, min_reviews: int = 100) -> bool:
    script = CRAWLER_DIR / "naver_crawler.py"
    cmd = [sys.executable, "-u", "-X", "utf8", str(script),
           "--limit", str(limit), "--min-reviews", str(min_reviews)]

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"]       = "1"
    env["CRAWLER_QUERY"]    = query   # 한글 쿼리는 env로 전달

    print(f"[naver] 크롤링 시작: {query}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(ROOT_DIR),
        env=env,
    )
    async for line in proc.stdout:
        text = line.decode('utf-8', errors='replace').rstrip()
        try:
            print(f"  {text}")
        except UnicodeEncodeError:
            print(f"  {text.encode('ascii', errors='replace').decode('ascii')}")
    await proc.wait()

    ok = proc.returncode == 0
    print(f"[naver] {'완료' if ok else f'실패 (코드 {proc.returncode})'}")
    return ok


def run_pipeline() -> bool:
    print("\n=== 파이프라인 시작 ===")
    result = subprocess.run(
        [sys.executable, str(CRAWLER_DIR / "merge_pipeline.py")],
        cwd=str(ROOT_DIR),
    )
    return result.returncode == 0


async def main(args):
    print(f"\n{'='*50}")
    print(f"검색어:   {args.query}")
    print(f"최대:     {args.limit}개")
    print(f"최소리뷰: {args.min_reviews}건 이상 (방문자+블로그)")
    print(f"{'='*50}\n")

    start = datetime.now()

    ok = await run_naver(args.query, args.limit, args.min_reviews)

    if not ok:
        print("크롤링 실패. 파이프라인을 건너뜁니다.")
        return

    run_pipeline()

    elapsed = (datetime.now() - start).seconds
    print(f"\n{'='*50}")
    print(f"완료 ({elapsed}초)")
    print(f"결과: data/places.json, data/places.js")
    print(f"지도 열기: python -m http.server 8000  →  http://localhost:8000")
    print(f"{'='*50}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="네이버 맛집 지도 크롤러")
    parser.add_argument("--query", required=True, type=str,
                        help="검색어 (예: 천안 맛집, 홍대, 성수동 카페)")
    parser.add_argument("--limit", type=int, default=500,
                        help="최대 장소 수 (기본값: 500)")
    parser.add_argument("--min-reviews", type=int, default=100,
                        help="최소 리뷰 수 방문자+블로그 합산 (기본값: 100)")
    asyncio.run(main(parser.parse_args()))
