"""
gen_final_report.py — 부산 4개 지역(서면/해운대/광안리/기장) 최종 큐레이션 리포트.

지역별로 크롤링된 음식점(≤80)·카페(≤60)·명소(≤15) 풀에서 rank_and_select.py의
점수(감성분석+네이버평점 블렌드 + 부산로컬가점)로 상위 10곳씩만 선정해 상세
리포트를 만든다. gen_report.py의 place_card/CSS를 재사용한다.

사용법: python reports/gen_final_report.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "reports"))
sys.path.insert(0, str(ROOT / "crawler"))

import gen_report as G  # noqa: E402
import rank_and_select as R  # noqa: E402

OUT_DIR = ROOT / "reports"
REGIONS = ["서면", "해운대", "광안리", "기장"]
CATS = [("맛집", "restaurant", "🍽 음식점"), ("카페", "cafe", "☕ 카페·디저트"), ("명소", "attraction", "🎡 명소·놀거리")]


def summary_row(d: dict, cat_label: str) -> str:
    ra = json.loads(d["review_analysis"] or "{}")
    menus = ", ".join(G.clean_menus(ra.get("menus", []), d["name"])[:3]) or "—"
    hours = G.clean_hours(d["business_hours"]) or "확인필요"
    reason = f' 🏮{d["_busan_reason"]}' if d.get("_busan_reason") else ""
    nrate = f'{d["naver_rating"]:.2f}' if d.get("naver_rating") else "—"
    return f"""
          <tr>
            <td class="l"><a href="#{G.slug(d['name'])}"><b>{G.esc(d['name'])}</b></a></td>
            <td class="l">{G.esc(hours)}</td>
            <td>{d['positive_rate']}%</td>
            <td>{nrate}</td>
            <td><b>{d['_score']:.0f}</b></td>
            <td class="l">{G.esc(menus)}{reason}</td>
          </tr>"""


def build_region_report(region: str, cats: list[tuple] = CATS) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    picks = {}
    for cat, kind, label in cats:
        picks[cat] = (kind, label, R.select_top(region, cat, n=10, min_valid=30))

    toc = "".join(
        f'<div class="toc-row"><span class="toc-gl">{label}</span>'
        + "".join(f'<a href="#{G.slug(d["name"])}">{G.esc(d["name"])}</a>' for d in rows)
        + "</div>"
        for cat, (kind, label, rows) in picks.items()
    )

    summary_tables = ""
    for cat, (kind, label, rows) in picks.items():
        rows_html = "".join(summary_row(d, label) for d in rows)
        summary_tables += f"""
      <tr class="grp"><td colspan="6">{label} TOP{len(rows)}</td></tr>
      {rows_html}"""

    cards = ""
    for cat, (kind, label, rows) in picks.items():
        card_html = ""
        for d in rows:
            rank_info = {"score": d["_score"], "base_score": d["_base_score"],
                        "busan_reason": d.get("_busan_reason"), "bonus": R.BUSAN_BONUS}
            card_html += G.place_card(d, kind=kind, rank_info=rank_info)
        cards += f'<h3 class="subh">{label} TOP{len(rows)}</h3>{card_html}'

    n_total = sum(len(rows) for _, _, rows in picks.values())
    cat_labels_line = " · ".join(f"{label} TOP{len(rows)}" for _, (_, label, rows) in picks.items())
    title_cats = "·".join(cat for cat, _, _ in cats)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>부산 {region} 여행 큐레이션 — {title_cats} TOP{n_total}</title>
<style>{G.CSS}</style>
</head>
<body>
<div class="wrap" id="top">
  <header class="top">
    <h1>🌊 부산 {region} 여행 큐레이션 TOP{n_total}</h1>
    <p>{cat_labels_line} · 분석 기준일 {today}</p>
    <p>네이버 방문자·블로그 리뷰 감성분석 + 네이버 자체 평점 블렌드 점수로 선정 (하단 방법론 참고)</p>
  </header>

  <div class="toc">{toc}</div>

  <h2 class="sec" id="method">① 선정 방법론</h2>
  <div class="tip">
    <h4>📐 점수 = 감성분석 긍정률(50%) + 네이버 자체 평점(50%) + 부산 로컬 가점</h4>
    <ul class="tight">
      <li><b>감성분석 긍정률</b>: 방문자+블로그 유효 리뷰(광고·중복 제거)를 자체 키워드 사전으로 분석한 값.</li>
      <li><b>네이버 자체 평점</b>: 네이버가 집계한 방문자 평균 별점(5점 만점→100점 환산). 명소·일부 카페처럼
        음식 어휘가 적어 키워드 분석이 성기게 걸리는 곳의 편향을 보정하는 교차검증 지표.</li>
      <li><b>부산 로컬 가점(+{R.BUSAN_BONUS}점, 지역당 최대 {R.MAX_BUSAN_BONUS_PER_GROUP}곳)</b>: 상호명·카테고리·대표메뉴에
        부산 향토 음식/명물 키워드(밀면·돼지국밥·씨앗호떡·곰장어·낙곱새·냉채족발·완당·비빔당면·동래파전·재첩국·부산어묵 등)가
        매칭된 곳 중, 가점 이전 순수 점수(base_score)가 높은 순으로 최대 {R.MAX_BUSAN_BONUS_PER_GROUP}곳에만 부여했다
        (매칭되는 곳이 많아 상한 없이 부여하면 특정 향토음식 위주로 쏠려 다양성이 사라지는 문제가 있었다).
        근거 키워드는 아래 요약표·카드에 그대로 표기했다.</li>
      <li>유효 리뷰 30건 미만인 곳은 표본 부족으로 후보에서 제외했다.</li>
    </ul>
  </div>

  <h2 class="sec" id="summary">② {region} 한눈 요약</h2>
  <p class="note">긍정률·평점·종합점수 순으로 정렬. 🏮 표시는 부산 로컬 가점 적용 근거.</p>
  <div class="tscroll">
    <table>
      <tr><th>이름</th><th>영업시간</th><th>감성긍정률</th><th>네이버평점</th><th>종합점수</th><th>대표 메뉴 / 가점사유</th></tr>
      {summary_tables}
    </table>
  </div>

  <h2 class="sec" id="detail">③ 선정 이유 상세</h2>
  {cards}

  <div class="foot">
    데이터 출처: 네이버 플레이스 방문자·블로그 리뷰 + 네이버 자체 평점 (naver_crawler.py 자동 수집·분석) ·
    분석 기준일 {today} · 광고/협찬/체험단 키워드 및 복붙 중복 리뷰는 사전 제거됨 ·
    부산 로컬 가점 방법론은 상단 ① 참고.
  </div>
</div>
</body>
</html>"""


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    OUT_DIR.mkdir(exist_ok=True)

    # 카페(60)·명소(15)는 아직 크롤링 전이라 이번엔 음식점만 별도 리포트로 생성.
    # 카페·명소 크롤링 완료 후 gen_final_report.py를 인자 없이 실행하면 3개 카테고리
    # 통합본(부산_{지역}_여행큐레이션_TOP30.html)으로 다시 생성된다.
    only = sys.argv[1] if len(sys.argv) > 1 else "맛집"
    cats = [c for c in CATS if c[0] == only] if only != "all" else CATS

    for region in REGIONS:
        html = build_region_report(region, cats=cats)
        suffix = "음식점_TOP10" if cats == [c for c in CATS if c[0] == "맛집"] else "여행큐레이션_TOP30"
        out = OUT_DIR / f"부산_{region}_{suffix}.html"
        out.write_text(html, encoding="utf-8")
        print(f"[생성] {out} ({len(html):,} bytes)")
