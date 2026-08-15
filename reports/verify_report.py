"""생성된 리포트의 <q>...</q> 인용문이 실제 DB 원문 리뷰에 존재하는지 전수 검증."""
import html as html_mod
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "places.db"


def norm(s):
    return "".join((s or "").split())


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    total_quotes = 0
    bad_quotes = 0
    total_hours_shown = 0

    patterns = sys.argv[1:] or ["부산_*_분석리포트.html"]
    html_paths = sorted(set(
        p for pat in patterns for p in (ROOT / "reports").glob(pat)
    ))
    for html_path in html_paths:
        html = html_path.read_text(encoding="utf-8")
        # 가게별 섹션 분리
        sections = re.split(r'<section class="rest-sec"', html)[1:]
        print(f"\n=== {html_path.name} ({len(sections)}개 가게 섹션) ===")
        for sec in sections:
            m = re.search(r'id="([^"]+)"', sec)
            m2 = re.search(r'class="rest-name">([^<]+)', sec)
            name_disp = html_mod.unescape(m2.group(1).strip()) if m2 else "?"

            row = conn.execute(
                "SELECT visitor_reviews_json, blog_reviews_json, name FROM places WHERE name=?",
                (name_disp,),
            ).fetchone()
            if not row:
                print(f"  [FAIL] '{name_disp}' DB에서 못 찾음")
                bad_quotes += 1
                continue
            import json
            pool = [norm(t) for t in json.loads(row["visitor_reviews_json"] or "[]")]
            pool += [norm(t) for t in json.loads(row["blog_reviews_json"] or "[]")]

            quotes = re.findall(r'<q>(.*?)</q>', sec, re.S)
            for q in quotes:
                total_quotes += 1
                core = norm(html_mod.unescape(q).strip("…"))
                if not any(core in p for p in pool):
                    bad_quotes += 1
                    print(f"  [FAIL] {name_disp}: 인용문이 원문에 없음 -> {q[:50]!r}")

            # 영업시간 표기 검증: "정보 미확인"이 아닌 경우 시간패턴 형식인지
            hm = re.search(r'bi-hours">.*?<b[^>]*>영업시간</b>\s*(.*?)</div>', sec, re.S)
            if hm:
                htxt = re.sub(r'<[^>]+>', '', hm.group(1)).strip()
                if htxt and "미확인" not in htxt:
                    total_hours_shown += 1
                    if not re.search(r'\d{1,2}:\d{2}', htxt) or len(htxt) > 40:
                        print(f"  [FAIL] {name_disp}: 영업시간 표기 의심 -> {htxt[:60]!r}")

    print(f"\n{'='*50}")
    print(f"총 인용문: {total_quotes}건 / 원문 불일치: {bad_quotes}건")
    print(f"영업시간 표기(미확인 제외): {total_hours_shown}건")
    print("PASS" if bad_quotes == 0 else "FAIL")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
