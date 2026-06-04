"""
db.py — SQLite 기반 맛집 데이터 저장소

테이블: places
  - 스캔 결과(scan_only=1)와 크롤링 결과(scan_only=0) 모두 저장
  - 기존 데이터 유지, 재크롤링 시 업데이트
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("data/places.db")


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS places (
            id   TEXT PRIMARY KEY,
            name TEXT,
            category TEXT DEFAULT '',
            address  TEXT DEFAULT '',
            lat  REAL DEFAULT 0,
            lng  REAL DEFAULT 0,

            -- 리뷰 집계 (API)
            visitor_total  INT DEFAULT 0,
            blog_total     INT DEFAULT 0,

            -- 리뷰 분석 (크롤링)
            visitor_sampled  INT DEFAULT 0,
            blog_sampled     INT DEFAULT 0,
            visitor_valid    INT DEFAULT 0,
            blog_valid       INT DEFAULT 0,
            visitor_ad_count INT DEFAULT 0,
            blog_ad_count    INT DEFAULT 0,
            visitor_dup_count INT DEFAULT 0,
            blog_dup_count   INT DEFAULT 0,
            filtered_reviews INT DEFAULT 0,
            total_reviews    INT DEFAULT 0,

            -- 감성 분석
            positive_rate      INT DEFAULT 0,
            sentiment_positive INT DEFAULT 0,
            sentiment_negative INT DEFAULT 0,
            sentiment_neutral  INT DEFAULT 0,
            sentiment_total    INT DEFAULT 0,
            mindless_excluded  INT DEFAULT 0,

            -- KoNLPy 분석
            review_analysis TEXT DEFAULT '{}',

            -- 영업 정보
            business_hours  TEXT DEFAULT '',
            break_time      TEXT DEFAULT '',
            closed_days     TEXT DEFAULT '',
            business_status TEXT DEFAULT '',

            -- 리뷰 원문 (키워드 분석용)
            visitor_reviews_json TEXT DEFAULT '[]',
            blog_reviews_json    TEXT DEFAULT '[]',

            -- 관리 메타
            area             TEXT DEFAULT '',
            scan_only        INT  DEFAULT 1,
            last_review_date TEXT DEFAULT '',
            first_seen       TEXT,
            last_crawled     TEXT,
            verdict          TEXT DEFAULT '맛집'
        )
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_area      ON places(area)
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_crawled   ON places(last_crawled)
        """)
        # 기존 DB에 컬럼 없을 경우 마이그레이션
        for col, definition in [
            ("visitor_reviews_json", "TEXT DEFAULT '[]'"),
            ("blog_reviews_json",    "TEXT DEFAULT '[]'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE places ADD COLUMN {col} {definition}")
                conn.commit()
            except Exception:
                pass  # 이미 존재하면 무시
        conn.commit()


# ── 저장 ──────────────────────────────────────────────────────────────

def upsert_scan(place: dict, area: str):
    """스캔 결과 저장. 이미 크롤링된 곳은 area만 갱신."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT scan_only FROM places WHERE id=?", (place["id"],)
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE places SET area=?, visitor_total=?, blog_total=?, last_review_date=? WHERE id=?",
                (area,
                 place.get("visitor_review_count", 0),
                 place.get("blog_review_count", 0),
                 place.get("last_review_date", ""),
                 place["id"])
            )
        else:
            conn.execute("""
            INSERT INTO places
              (id, name, category, address, lat, lng,
               visitor_total, blog_total, last_review_date,
               area, first_seen, scan_only)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,1)
            """, (
                place["id"], place["name"],
                place.get("category", "기타"),
                place.get("address", ""),
                float(place.get("y", 0) or 0),
                float(place.get("x", 0) or 0),
                place.get("visitor_review_count", 0),
                place.get("blog_review_count", 0),
                place.get("last_review_date", ""),
                area,
                datetime.now().isoformat(),
            ))
        conn.commit()


def upsert_crawl(result: dict):
    """크롤링 결과 저장 (전체 갱신)."""
    ra = result.get("review_analysis", {})
    with get_conn() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO places (
            id, name, category, address, lat, lng,
            visitor_total, blog_total,
            visitor_sampled, blog_sampled,
            visitor_valid, blog_valid,
            visitor_ad_count, blog_ad_count,
            visitor_dup_count, blog_dup_count,
            filtered_reviews, total_reviews,
            positive_rate, sentiment_positive, sentiment_negative,
            sentiment_neutral, sentiment_total, mindless_excluded,
            review_analysis,
            visitor_reviews_json, blog_reviews_json,
            business_hours, break_time, closed_days, business_status,
            area, scan_only, last_crawled, first_seen, verdict
        ) VALUES (
            :id, :name, :category, :address, :lat, :lng,
            :visitor_total, :blog_total,
            :visitor_sampled, :blog_sampled,
            :visitor_valid, :blog_valid,
            :visitor_ad_count, :blog_ad_count,
            :visitor_dup_count, :blog_dup_count,
            :filtered_reviews, :total_reviews,
            :positive_rate, :sentiment_positive, :sentiment_negative,
            :sentiment_neutral, :sentiment_total, :mindless_excluded,
            :review_analysis,
            :visitor_reviews_json, :blog_reviews_json,
            :business_hours, :break_time, :closed_days, :business_status,
            :area, 0, :last_crawled,
            COALESCE((SELECT first_seen FROM places WHERE id=:id), :last_crawled),
            '맛집'
        )
        """, {
            **result,
            "lat": result.get("lat", 0),
            "lng": result.get("lng", 0),
            "review_analysis": json.dumps(ra, ensure_ascii=False),
            "visitor_reviews_json": json.dumps(result.get("visitor_reviews", []), ensure_ascii=False),
            "blog_reviews_json":    json.dumps(result.get("blog_reviews", []), ensure_ascii=False),
            "last_crawled": datetime.now().isoformat(),
            "area": result.get("area", ""),
        })
        conn.commit()


# ── 조회 ──────────────────────────────────────────────────────────────

def get_pending_crawl(older_than_days: int = 30) -> list[dict]:
    """스캔만 됐거나 N일 이상 지난 장소 (리뷰 많은 순)."""
    cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute("""
        SELECT * FROM places
        WHERE scan_only = 1
           OR (last_crawled IS NOT NULL AND last_crawled < ?)
        ORDER BY visitor_total DESC
        """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def _fix_hours(s: str) -> str | None:
    """영업시간 필드에 리뷰 텍스트가 섞인 경우 시간 패턴만 추출."""
    if not s:
        return None
    if len(s) <= 60 and re.search(r"\d{1,2}:\d{2}", s):
        return s
    src = s[s.rfind("영업시간"):] if "영업시간" in s else s
    m = re.search(
        r"(?:[월화수목금토일]{1,3}[~\-]?[월화수목금토일]{0,3}\s+)?\d{1,2}:\d{2}\s*[~\-]\s*\d{1,2}:\d{2}",
        src,
    )
    return m.group(0).strip() if m else None


def export_places(min_valid: int = 50) -> list[dict]:
    """웹용 export — 크롤링 완료 + 유효리뷰 기준 통과한 것만."""
    with get_conn() as conn:
        rows = conn.execute("""
        SELECT * FROM places
        WHERE scan_only = 0
          AND filtered_reviews >= ?
          AND lat != 0 AND lng != 0
        ORDER BY filtered_reviews DESC
        """, (min_valid,)).fetchall()
    places = []
    for r in rows:
        p = dict(r)
        p["review_analysis"] = json.loads(p.get("review_analysis") or "{}")
        p["business_hours"]  = _fix_hours(p.get("business_hours") or "")
        # 원문 리뷰는 브라우저로 보내지 않는다 (분석에 인용문이 이미 포함됨).
        # DB에는 그대로 보관 → reanalyze로 재분석 가능.
        p.pop("visitor_reviews_json", None)
        p.pop("blog_reviews_json", None)
        places.append(p)
    return places


def get_crawled_with_reviews() -> list[dict]:
    """크롤링 완료 장소의 저장된 원문 리뷰 반환 (재분석용)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, visitor_reviews_json, blog_reviews_json "
            "FROM places WHERE scan_only = 0"
        ).fetchall()
    return [dict(r) for r in rows]


def update_analysis(pid: str, analysis: dict):
    """review_analysis 컬럼만 갱신 (재분석)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE places SET review_analysis=? WHERE id=?",
            (json.dumps(analysis, ensure_ascii=False), pid),
        )
        conn.commit()


def status() -> dict:
    with get_conn() as conn:
        total     = conn.execute("SELECT COUNT(*) FROM places").fetchone()[0]
        scan_only = conn.execute("SELECT COUNT(*) FROM places WHERE scan_only=1").fetchone()[0]
        crawled   = conn.execute("SELECT COUNT(*) FROM places WHERE scan_only=0").fetchone()[0]
        qualified = conn.execute(
            "SELECT COUNT(*) FROM places WHERE scan_only=0 AND filtered_reviews>=50"
        ).fetchone()[0]
        areas = conn.execute(
            "SELECT area, COUNT(*) as cnt FROM places GROUP BY area ORDER BY cnt DESC"
        ).fetchall()
    return {
        "total": total,
        "scan_only": scan_only,
        "crawled": crawled,
        "qualified": qualified,
        "areas": [(a["area"], a["cnt"]) for a in areas],
    }
