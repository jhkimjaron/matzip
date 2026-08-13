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

# 1-1) list — 지역 검색 없이 직접 지정한 음식점만 크롤링 → DB 저장
python manage.py list --ids 1529048751 1856358676          # place 고유번호 (정확)
python manage.py list --ids "https://map.naver.com/p/entry/place/1529048751"
python manage.py list --names "몽탄 청담점" "산낙지마을 강남점"   # 상호명 검색
python manage.py list --file places.txt                    # 목록 파일 (ID·이름 혼용 가능)

# 2) 크롤링 — 스캔된 곳의 리뷰·영업정보 수집·분석 → DB 저장
python manage.py crawl

# 2-1) reanalyze — 저장된 원문 리뷰로 분석만 재생성 (분석 로직 변경 시, 재크롤링 불필요)
python manage.py reanalyze

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

#### list — 임의 지정 크롤링

`list`는 프랜차이즈 제외·최소 리뷰수 같은 스캔 단계 필터를 적용하지 않고, 지정한
업체를 그대로 크롤링합니다. 지정 방법은 두 가지입니다.

| 방법 | 정확도 | 비고 |
|---|---|---|
| `--ids` (권장) | 정확 | 검색 단계가 없어 **동명이업체 오매칭이 원천적으로 불가능** |
| `--names` | 보통 | 검색 후 관련도 1위를 사용 — 오매칭 가능성 있음 |

**place 고유번호 찾는 법** — 네이버 지도에서 가게를 열면 주소창에 숫자가 보입니다.

```
https://map.naver.com/p/entry/place/1529048751
                                    └─ 이 숫자가 place ID
```

URL을 통째로 붙여넣어도 ID만 자동으로 추출합니다. `pcmap.place.naver.com/restaurant/{id}/home`
형태나 검색 결과 URL(`/p/search/천안맛집/place/1856358676`)도 인식합니다.
단축 URL(`naver.me/...`)은 지원하지 않으니 펼쳐진 주소를 쓰세요.

**목록 파일** — 한 줄에 하나씩, ID와 이름을 섞어 써도 됩니다. 숫자로만 된 줄과
네이버 지도 URL은 ID로, 나머지는 상호명으로 자동 판별합니다.

```text
# places.txt
1529048751
https://map.naver.com/p/entry/place/1856358676
몽탄 청담점
```

이름으로 지정할 때는 동명이업체 오매칭을 줄이기 위해 동네를 함께 적어주세요
(예: `몽탄 청담점`). 실행 로그의 `[매칭]` 줄에서 어떤 업체가 선택됐는지 확인할 수 있고,
의도한 곳이 아니면 해당 업체의 ID로 다시 돌리면 됩니다.

기타 옵션 — `--area 라벨`로 DB에 저장할 area 값을 지정할 수 있고(기본값 `직접등록`),
`--force`를 주면 유효 리뷰 수가 `min_valid_reviews` 미만이어도 저장합니다.

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
3. **크롤링** — 방문자 리뷰 최신순 100건 + 블로그 리뷰 25건(각 글 본문 전문) +
   영업시간/휴무 + 공식 수상 배지(미쉐린·빕구르망·블루리본)를 수집.
4. **필터(리뷰 단계)** — 광고·협찬·체험단 키워드 리뷰, 복붙 중복, 무지성 리뷰 제거.
5. **분석(서버 단일 처리)** — kiwipiepy 형태소 분석으로 대표 메뉴를 추출하고,
   항목별(맛·양·서비스·위생·편의·웨이팅·분위기) 인용문과 예약앱 감지, 감성(긍정률)을 계산해
   `review_analysis` 한 구조로 저장한다. 프론트엔드는 이 결과를 **렌더링만** 한다
   (원문 리뷰는 브라우저로 보내지 않아 `places.js`가 가볍다). 분석 키워드·아이콘은
   `crawler/naver_crawler.py`의 `REVIEW_ASPECTS` 한 곳에서만 관리한다.

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
