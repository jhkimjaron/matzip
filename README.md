# 맛집지도

네이버 플레이스 리뷰를 수집·분석해 지역별 맛집을 지도에 보여주는 웹 서비스입니다.
방문자/블로그 리뷰를 최신순으로 크롤링하고, 광고·중복·무지성 리뷰를 걸러낸 뒤
키워드·감성 분석 결과와 함께 등재합니다.

## 구조

```
matzip/
├── index.html              ← 프론트엔드 (지도 + 리스트 UI)
├── manage.py               ← 통합 관리 CLI (scan/crawl/export/push/status/update)
├── config.json             ← 스캔 지역 목록 및 기준값 설정
├── crawler/
│   ├── naver_crawler.py    ← 네이버 플레이스 크롤러 (검색·리뷰·영업정보)
│   └── db.py               ← SQLite 저장소 (data/places.db)
└── data/
    ├── places.db           ← 원천 데이터 (스캔 + 크롤링 결과)
    ├── places.json         ← 프론트엔드가 읽는 export
    └── places.js           ← window.PLACES_DATA (index.html 로드용)
```

## 설치

```bash
pip install playwright playwright-stealth kiwipiepy
playwright install chromium
```

## 사용법

모든 작업은 `manage.py`로 합니다.

```bash
# 1) 스캔 — 검색어로 음식점 목록 수집 (프랜차이즈·리뷰부족 제외) → DB 저장
python manage.py scan --area "천안시 동남구 음식점" --limit 100

# 2) 크롤링 — 스캔된 곳의 리뷰·영업정보 수집·분석 → DB 저장
python manage.py crawl

# 3) export — DB → data/places.json / places.js 생성
python manage.py export

# 4) push — GitHub Pages 배포
python manage.py push

# 전체 일괄 (scan + crawl + export + push)
python manage.py update --area "천안시 동남구 음식점"

# 현황 확인
python manage.py status
```

`--area` 없이 `scan`을 실행하면 `config.json`의 `areas` 목록 전체를 스캔합니다.

### 프론트엔드 실행

```bash
python -m http.server 8000
# http://localhost:8000
```

## 동작 방식

1. **스캔** — `map.naver.com` 검색 → 결과 목록(`pcmap` iframe)의 Apollo 캐시에서
   음식점을 추출. 한 검색어당 네이버가 최대 ~100개를 내려주므로, 더 넓은 범위는
   구·동 단위로 검색어를 나눠 수집합니다.
2. **필터(스캔 단계)** — 프랜차이즈 제외, 방문자+블로그 리뷰 합산이 `min_reviews`
   미만이면 제외.
3. **크롤링** — 방문자 리뷰 최신순 100건 + 블로그 리뷰 25건 + 영업시간/휴무를 수집.
4. **필터(리뷰 단계)** — 광고·협찬·체험단 키워드 리뷰, 복붙 중복, 무지성 리뷰 제거.
5. **분석** — kiwipiepy 형태소 분석으로 대표 메뉴·키워드·불만 토픽 추출, 감성(긍정률) 계산.

## 설정 (config.json)

| 키 | 의미 |
|---|---|
| `areas` | 스캔할 검색어 목록 |
| `limit_per_area` | 지역당 최대 수집 수 |
| `min_reviews` | 스캔 통과 최소 리뷰 수 (방문자+블로그 합산) |
| `min_valid_reviews` | 등재 최소 유효 리뷰 수 |
| `active_days` | 최근 N일 내 리뷰가 있어야 활성으로 간주 |
| `update_interval_days` | 재크롤링 주기 |

## 주의사항

개인 학습 목적의 프로토타입입니다. 네이버 이용약관에 따라 무단 크롤링은
법적 문제가 될 수 있습니다.
