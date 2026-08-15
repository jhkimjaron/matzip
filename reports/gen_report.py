"""
gen_report.py — 부산 4개 지역(서면/해운대/광안리/기장) 맛집+카페 분석 리포트 생성.

data/places.db 에 저장된 실제 크롤링 결과(리뷰 원문 + review_analysis)만 사용해
정량적으로 리포트를 만든다. 인용문은 전부 저장된 리뷰 원문에서 그대로 발췌한다
(verify.py 로 최종 검증).

사용법: python reports/gen_report.py
"""
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "crawler"))
from naver_crawler import _aspect_quotes, analyze_sentiment, REVIEW_ASPECTS  # noqa: E402

DB_PATH = ROOT / "data" / "places.db"
OUT_DIR = ROOT / "reports"

AREA_GROUPS = [
    ("서면",  "부산 서면 맛집",  "부산 서면 카페"),
    ("해운대", "부산 해운대 맛집", "부산 해운대 카페"),
    ("광안리", "부산 광안리 맛집", "부산 광안리 카페"),
    ("기장",  "부산 기장 맛집",  "부산 기장 카페"),
]

CSS = (ROOT / "reports" / "_style.css").read_text(encoding="utf-8")


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slug(name: str) -> str:
    return re.sub(r"[^\w가-힣]", "", name)


# ── 영업시간 원문 정제 ──────────────────────────────────────────────
# business_hours 컬럼에 리뷰/소개글 텍스트가 섞이는 기존 버그가 있어(별도 이슈로
# 분리) 리포트에서는 "요일접두사 + 시간범위" 패턴을 찾되, 그 앞에 붙은 문맥이
# 짧거나 '영업시간' 라벨 근처일 때만 신뢰하고, 그 외(리뷰 문장 한복판)는
# "확인 필요"로 표시한다.
_DAY = r'(?:매일|평일|주말|공휴일|연중무휴|[월화수목금토일](?:\s*[~\-,]\s*[월화수목금토일])*\s*(?:요일)?)'
_TIME = r'\d{1,2}:\d{2}\s*[~\-]\s*\d{1,2}:\d{2}'
_HOURS_SEGMENT = re.compile(rf'{_DAY}?\s*\)?\s*{_TIME}')
_TRUST_PREFIX_LEN = 14  # 이 길이 이하 접두문맥이면 라벨 없이도 신뢰


def clean_hours(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    m = _HOURS_SEGMENT.search(raw)
    if not m:
        return None
    prefix = raw[: m.start()]
    trusted = len(prefix) <= _TRUST_PREFIX_LEN or re.search(r'영업\s*시간', prefix[-20:])
    if not trusted:
        return None
    return re.sub(r'[()]', '', m.group(0)).strip()


def clean_break(raw: str) -> str | None:
    raw = (raw or "").strip()
    if raw and len(raw) <= 20 and re.fullmatch(r'\d{1,2}:\d{2}\s*[~\-]\s*\d{1,2}:\d{2}', raw):
        return raw
    return None


# 메뉴 추출 NLP가 드물게 감성어를 명사로 오인하는 경우가 있어(예: '실망'을
# 메뉴로 인식) 리포트 표시 단계에서만 걸러낸다. 크롤러 핵심 로직(naver_crawler.py
# _extract_menus)은 건드리지 않는다 — 전체 places.json에 영향을 주는 공용 코드라
# 별도 검증 없이 바꾸는 건 범위 밖.
_NOT_MENU_WORDS = {
    "실망", "후회", "만족", "기대", "아쉬움", "아쉬운점", "최고", "대박",
    "완벽", "행복", "감동", "걱정", "불만", "다행", "재방문", "추천",
}


# 검증 과정에서 확인된, 특정 업체 한정 오탐 메뉴 (본문과 무관한 도입부 문장이
# 취식동사 근접으로 잘못 인식된 경우). 일반화된 규칙이 아니라 실사 확인된 건만 등재.
#   오븐의온도(베이커리): "부산 가면 밀면을 꼭 먹는다"는 무관한 도입부 문장에서
#   '밀면'이 취식동사 근접으로 오탐됨 — 원문 확인 완료.
_PLACE_MENU_OVERRIDE = {
    "오븐의온도": {"밀면"},
}


def clean_menus(menus: list[str], place_name: str = "") -> list[str]:
    exclude = _NOT_MENU_WORDS | _PLACE_MENU_OVERRIDE.get(place_name, set())
    return [m for m in menus if m not in exclude]


# ── 메뉴별 인용문 추출 (aspect quote와 동일 로직, 키워드=메뉴명) ────────
def menu_quote(reviews_typed: list[tuple[str, str]], menu: str) -> dict | None:
    quotes, total = _aspect_quotes(reviews_typed, [menu], max_quotes=1)
    if not quotes:
        return None
    q = quotes[0]
    q["total"] = total
    return q


def load_places(area: str) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM places WHERE area=? AND scan_only=0 ORDER BY positive_rate DESC",
        (area,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def sentiment_bar(label: str, texts: list[str]) -> str:
    if not texts:
        return ""
    s = analyze_sentiment(texts)
    n = s["sentiment_total"]
    if n == 0:
        return ""
    pos = s["positive"] / n * 100
    neu = s["neutral"] / n * 100
    neg = s["negative"] / n * 100
    return f"""
        <div class="sent-bar-wrap">
          <span class="sent-label">{label} <span class="n-cnt">({len(texts)}건)</span></span>
          <div class="bar-track">
            <div class="bar-pos" style="width:{pos:.1f}%"></div>
            <div class="bar-neu" style="width:{neu:.1f}%"></div>
            <div class="bar-neg" style="width:{neg:.1f}%"></div>
          </div>
          <span class="sent-nums">긍정 {pos:.1f}% / 중립 {neu:.1f}% / 부정 {neg:.1f}%</span>
        </div>"""


def award_badges(awards: list[str]) -> str:
    if not awards:
        return ""
    return "".join(f'<span class="pill p-go" style="margin-left:4px">{esc(a)}</span>' for a in awards)


def place_card(row: dict, kind: str = "restaurant", rank_info: dict | None = None) -> str:
    """kind: 'restaurant'|'cafe'|'attraction'. 'attraction'은 '메뉴' 개념이 없어
    대표메뉴 섹션을 생략한다. rank_info={'score','base_score','busan_reason'}가
    있으면 선정 사유 배지를 카드 상단에 추가한다."""
    name = row["name"]
    sid = slug(name)
    ra = json.loads(row["review_analysis"] or "{}")
    visitor = json.loads(row["visitor_reviews_json"] or "[]")
    blog = json.loads(row["blog_reviews_json"] or "[]")
    reviews_typed = [(t, "visitor") for t in visitor] + [(t, "blog") for t in blog]

    hours = clean_hours(row["business_hours"])
    brk = clean_break(row["break_time"])
    # 원본 캡처가 콜론 뒤 요일 없이 잘리는 경우가 있어(예: '정기휴무:') 방어적으로 정리
    closed = re.sub(r'[:：]\s*$', '', (row["closed_days"] or "").strip())
    awards = json.loads(row["awards"] or "[]")

    hours_html = f'<span class="yes">{esc(hours)}</span>' if hours else '<span class="maybe">정보 미확인(매장 문의 권장)</span>'
    etc_bits = []
    if brk:
        etc_bits.append(f'<b class="bi-lab">브레이크타임</b> {esc(brk)}')
    if closed:
        etc_bits.append(f'<b class="bi-lab">휴무</b> {esc(closed)}')
    etc_html = f'<div class="bi-etc">{" <span class=\'bi-sep\'>·</span> ".join(etc_bits)}</div>' if etc_bits else ""

    rank_html = ""
    if rank_info:
        bits = [f'종합점수 <b>{rank_info["score"]:.0f}</b>']
        if rank_info.get("busan_reason"):
            bits.append(f'<span class="pill p-go">🏮 부산 로컬 +{rank_info.get("bonus", 8)}점: {esc(rank_info["busan_reason"])}</span>')
        rank_html = f'<div class="verdict-box">{" · ".join(bits)}</div>'

    # 메뉴 추천 (실제 리뷰 인용문 포함) — 명소는 '메뉴' 개념이 없어 생략
    menu_html = ""
    menus = clean_menus(ra.get("menus", []), name) if kind != "attraction" else []
    if menus:
        boxes = []
        for i, m in enumerate(menus):
            q = menu_quote(reviews_typed, m)
            tag = "시그니처" if i == 0 else "언급 메뉴"
            cls = "mq-box-sig" if i == 0 else "mq-box-rec"
            tagcls = "mq-sig" if i == 0 else "mq-rec"
            if q:
                boxes.append(
                    f'<div class="mq-box {cls}"><span class="mq-tag {tagcls}">{tag}</span>'
                    f'<b>{esc(m)}</b> — <q>{esc(q["text"])}</q></div>'
                )
            else:
                boxes.append(
                    f'<div class="mq-box {cls}"><span class="mq-tag {tagcls}">{tag}</span>'
                    f'<b>{esc(m)}</b> — 리뷰 언급 빈도 상위</div>'
                )
        menu_html = f'''
        <div class="rs-sec">
          <div class="rs-title">대표 메뉴 (리뷰 언급 기준)</div>
          <div class="menu-q">{"".join(boxes)}</div>
        </div>'''

    # 항목별 리뷰 (맛/양/서비스/위생/편의/웨이팅/분위기)
    aspects_html = ""
    aspects = ra.get("aspects", [])
    if aspects:
        blocks = []
        for asp in aspects:
            quotes = "".join(
                f'<div class="neg-q">"{esc(q["text"])}"</div>' for q in asp.get("quotes", [])
            )
            blocks.append(f'''
            <div class="neg-pattern" style="background:#f7f3ec">
              <div class="neg-ph" style="color:var(--accent)">{asp["icon"]} {esc(asp["label"])}
                <span class="neg-dr">언급 {asp["count"]}건</span></div>
              {quotes}
            </div>''')
        aspects_html = f'''
        <div class="rs-sec">
          <div class="rs-title">항목별 리뷰 언급</div>
          {"".join(blocks)}
        </div>'''

    resv = ra.get("reservation_apps", [])
    resv_html = (
        f'<p class="resv-p">📱 리뷰에서 언급된 예약/웨이팅 앱: '
        + ", ".join(f'<span class="waiting-tag">{esc(a)}</span>' for a in resv) + "</p>"
    ) if resv else ""

    shortage = ra.get("shortage", {})
    warn = ""
    if shortage.get("visitor") or shortage.get("blog"):
        warn = '<p class="warn-tag">⚠ 표본이 적어 분석 신뢰도가 낮을 수 있음</p>'

    n_v, n_b = row["visitor_valid"], row["blog_valid"]
    nrate = row.get("naver_rating") or 0
    nrate_html = f' · 네이버평점 <b>{nrate:.2f}</b>/5' if nrate else ""

    return f"""
      <section class="rest-sec" id="{sid}">
        <div class="rest-hd">
          <div class="rest-hd-l">
            <h3 class="rest-name">{esc(name)} <span class="rest-cat">{esc(row['category'])}</span></h3>
            <span class="rev-cnt">방문자 {n_v} · 블로그 {n_b} · 종합 긍정률 {row['positive_rate']}%{nrate_html}{award_badges(awards)}</span>
          </div>
          <div class="hd-maps">
            <a class="m-naver" href="https://map.naver.com/p/entry/place/{row['id']}" target="_blank">네이버지도</a>
          </div>
        </div>
        {rank_html}
        <div class="rs-sec">
          <div class="rs-title">영업 정보</div>
          <div class="biz-info">
            <div class="bi-hours"><b class="bi-lab">영업시간</b> {hours_html}</div>
            {etc_html}
          </div>
          {resv_html}
        </div>
        <div class="rs-sec">
          <div class="rs-title">전반적 평가</div>
          {sentiment_bar("네이버 방문자", visitor)}
          {sentiment_bar("네이버 블로그", blog)}
          {warn}
        </div>
        {menu_html}
        {aspects_html}
        <a class="totop" href="#top">↑ 목록으로</a>
      </section>"""


def summary_row(row: dict, group_no: int) -> str:
    ra = json.loads(row["review_analysis"] or "{}")
    menus = ", ".join(clean_menus(ra.get("menus", []), row["name"])[:3]) or "—"
    hours = clean_hours(row["business_hours"]) or "확인필요"
    pr = row["positive_rate"]
    pcls = "p-go" if pr >= 80 else ("p-warn" if pr >= 60 else "p-no")
    return f"""
          <tr>
            <td class="l"><a href="#{slug(row['name'])}"><b>{esc(row['name'])}</b></a></td>
            <td>{esc(row['category'])}</td>
            <td class="l">{esc(hours)}</td>
            <td>{row['visitor_valid']+row['blog_valid']}건</td>
            <td><span class="pill {pcls}">{pr}%</span></td>
            <td class="l">{esc(menus)}</td>
          </tr>"""


def build_report(title_area: str, food_area: str, cafe_area: str) -> str:
    food = load_places(food_area)
    cafe = load_places(cafe_area)
    all_rows = food + cafe
    today = datetime.now().strftime("%Y-%m-%d")

    # TOC
    toc_food = "".join(f'<a href="#{slug(r["name"])}">{esc(r["name"])}</a>' for r in food)
    toc_cafe = "".join(f'<a href="#{slug(r["name"])}">{esc(r["name"])}</a>' for r in cafe)

    # 요약 표
    rows_food = "".join(summary_row(r, 1) for r in food)
    rows_cafe = "".join(summary_row(r, 2) for r in cafe)

    # 지역 총평 (데이터 기반)
    def stats(rows):
        n = len(rows)
        avg = sum(r["positive_rate"] for r in rows) / n if n else 0
        top = max(rows, key=lambda r: r["positive_rate"]) if rows else None
        return n, avg, top

    nf, avgf, topf = stats(food)
    nc, avgc, topc = stats(cafe)
    award_holders = [r for r in all_rows if json.loads(r["awards"] or "[]")]
    award_html = ""
    if award_holders:
        items = "".join(
            f'<li><b>{esc(r["name"])}</b> — {", ".join(json.loads(r["awards"]))}</li>'
            for r in award_holders
        )
        award_html = f'<div class="tip"><h4>🏅 공식 수상 배지 보유</h4><ul class="tight">{items}</ul></div>'

    top5_food = sorted(food, key=lambda r: -r["positive_rate"])[:5]
    top5_cafe = sorted(cafe, key=lambda r: -r["positive_rate"])[:5]
    top5_html = lambda rows: "".join(
        f'<li><b>{esc(r["name"])}</b> <span class="nrev">({r["positive_rate"]}%, 유효리뷰 {r["visitor_valid"]+r["blog_valid"]}건)</span></li>'
        for r in rows
    )

    body_food = "".join(place_card(r) for r in food)
    body_cafe = "".join(place_card(r) for r in cafe)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>부산 {title_area} 맛집·카페 {len(all_rows)}곳 리뷰 분석</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap" id="top">
  <header class="top">
    <h1>🌊 부산 {title_area} 맛집·카페 {len(all_rows)}곳 리뷰 분석</h1>
    <p>식사 {nf}곳 · 카페·디저트 {nc}곳 · 분석 기준일 {today}</p>
    <p>네이버 방문자·블로그 리뷰 기반 (광고·협찬·중복·무지성 리뷰 제외, 여행 맛집 탐색용)</p>
  </header>

  <div class="toc">
    <div class="toc-row"><span class="toc-gl">🍽 식사</span>{toc_food}</div>
    <div class="toc-row"><span class="toc-gl">☕ 카페·디저트</span>{toc_cafe}</div>
  </div>

  <h2 class="sec" id="summary">① {title_area} 한눈 요약</h2>
  <p class="note">
    긍정률은 방문자+블로그 유효 리뷰(광고·중복 제거) 기준 감성 분석 결과입니다.
    영업시간은 원문에서 명확히 확인된 경우만 표기하며, "확인필요"는 방문 전 매장 문의를 권장합니다.
  </p>
  <div class="tscroll">
    <table>
      <tr><th>이름</th><th>종류</th><th>영업시간</th><th>유효리뷰</th><th>긍정률</th><th>대표 메뉴</th></tr>
      <tr class="grp"><td colspan="6">🍽 식사 ({nf}곳)</td></tr>
      {rows_food}
      <tr class="grp"><td colspan="6">☕ 카페·디저트 ({nc}곳)</td></tr>
      {rows_cafe}
    </table>
  </div>

  <h2 class="sec" id="pick">② {title_area} 추천 픽 (긍정률 상위)</h2>
  <div class="tips3col">
    <div class="t3 t3-good">
      <b>🍽 식사 TOP5</b>
      <ul>{top5_html(top5_food)}</ul>
    </div>
    <div class="t3 t3-good">
      <b>☕ 카페·디저트 TOP5</b>
      <ul>{top5_html(top5_cafe)}</ul>
    </div>
    <div class="t3 t3-tip">
      <b>📊 지역 평균 긍정률</b>
      <ul><li>식사 평균 {avgf:.1f}%</li><li>카페·디저트 평균 {avgc:.1f}%</li></ul>
    </div>
  </div>
  {award_html}

  <h2 class="sec" id="detail">③ 가게별 세부 분석</h2>
  <h3 class="subh">식사 ({nf}곳)</h3>
  {body_food}
  <h3 class="subh">카페·디저트 ({nc}곳)</h3>
  {body_cafe}

  <div class="foot">
    데이터 출처: 네이버 플레이스 방문자·블로그 리뷰 (naver_crawler.py 자동 수집·분석) ·
    분석 기준일 {today} · 광고/협찬/체험단 키워드 및 복붙 중복 리뷰는 사전 제거됨.
  </div>
</div>
</body>
</html>"""


if __name__ == "__main__":
    OUT_DIR.mkdir(exist_ok=True)
    for title, food_area, cafe_area in AREA_GROUPS:
        html = build_report(title, food_area, cafe_area)
        out = OUT_DIR / f"부산_{title}_맛집카페_분석리포트.html"
        out.write_text(html, encoding="utf-8")
        print(f"[생성] {out} ({len(html):,} bytes)")
