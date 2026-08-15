"""지역×카테고리별로 review_count(방문자+블로그 API 합산) 상위 N개의 place id를 뽑는다.
이미 크롤링된(scan_only=0) 곳은 그대로 두고, 스캔만 된(scan_only=1) 곳 중 상위권에
드는 id만 반환 — manage.py list --ids 로 넘겨 크롤링한다.

사용법: python reports/select_targets.py
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "places.db"

# 1차: 음식점 100곳 × 4지역 검증 실행. 카페(60)·명소(15)는 이후 단계에서 추가.
TARGETS = [
    ("서면",  "맛집", 100),
    ("해운대", "맛집", 100),
    ("광안리", "맛집", 100),
    ("기장",  "맛집", 100),
]


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    for region, cat, n in TARGETS:
        area = f"부산 {region} {cat}"
        rows = conn.execute(
            "SELECT id, scan_only, visitor_total + blog_total AS rc "
            "FROM places WHERE area=? ORDER BY rc DESC LIMIT ?",
            (area, n),
        ).fetchall()
        total = len(rows)
        pending = [r["id"] for r in rows if r["scan_only"] == 1]
        done = total - len(pending)
        print(f"{area:<16} 풀={total:>3} 이미크롤링={done:>3} 신규대상={len(pending):>3}")
        out = ROOT / "reports" / f"targets_{region}_{cat}.txt"
        out.write_text("\n".join(pending), encoding="utf-8")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
