# 맛집지도 프로토타입

카카오맵 + 네이버 플레이스 + 뽈레 리뷰를 수집하고,
어뷰징을 필터링한 뒤 OR 방식으로 맛집을 판별하는 웹 서비스입니다.

## 구조

```
matzip/
├── index.html              ← 프론트엔드 (지도 + 리스트 UI)
├── data/
│   ├── places.json         ← 프론트엔드가 읽는 최종 데이터
│   └── raw/
│       ├── kakao/          ← 카카오 크롤링 결과
│       ├── naver/          ← 네이버 크롤링 결과
│       └── polle/          ← 뽈레 크롤링 결과
└── crawler/
    ├── kakao_crawler.py    ← 카카오맵 크롤러
    ├── naver_crawler.py    ← 네이버 플레이스 크롤러
    ├── polle_crawler.py    ← 뽈레 크롤러
    └── merge_pipeline.py   ← 병합 + 판별 파이프라인
```

## 설치

```bash
pip install playwright
playwright install chromium
```

## 사용법

### 1. 크롤링

```bash
# 카카오맵
python crawler/kakao_crawler.py --query "마포구 맛집" --limit 50

# 네이버 플레이스
python crawler/naver_crawler.py --query "마포구 맛집" --limit 50

# 뽈레
python crawler/polle_crawler.py --query "마포구" --limit 30
```

### 2. 병합 + 판별

```bash
# 최신 크롤링 결과를 자동으로 병합
python crawler/merge_pipeline.py

# 파일 직접 지정
python crawler/merge_pipeline.py \
  --kakao data/raw/kakao/kakao_20250101_120000.json \
  --naver data/raw/naver/naver_20250101_120000.json \
  --polle data/raw/polle/polle_20250101_120000.json
```

### 3. 프론트엔드 실행

```bash
# Python 내장 서버
python -m http.server 8000
# 브라우저에서 http://localhost:8000 열기
```

## 판별 기준

### OR 방식 — 하나라도 통과하면 등재

| 소스 | 통과 기준 |
|------|----------|
| 카카오 | Gold 리뷰어 긍정률 75%↑ + Gold 5명↑ + 거품 없음 |
| 네이버 | 광고 제거 후 긍정률 75%↑ + 유효리뷰 30건↑ |
| 뽈레 | 긍정률 70%↑ + 유효리뷰 10건↑ (기준 완화) |
| 교차 | 카카오+네이버 둘 다 긍정률 60%↑ |

### 어뷰징 필터

**카카오맵**
- 칭찬봇 제거: 리뷰 3건 이하 원타임 리뷰어의 5점 리뷰
- Gold 리뷰어 선별: 리뷰 50건↑ + 평균 별점 2.5~4.2

**네이버 플레이스**
- 광고/체험단 키워드 포함 리뷰 제거
- 복붙 리뷰 묶음 감지 및 제거

**뽈레**
- 광고 키워드 제거
- 신규/일회성 계정 리뷰 제거

### 판별 등급

| 등급 | 기준 |
|------|------|
| 숨은맛집 | 카카오 평점 3.7↓ + Gold 긍정률 70%↑ + Gold 8명↑ |
| 맛집 | OR 통과 + 평균 긍정률 75%↑ |
| 괜찮음 | OR 통과 + 평균 긍정률 75% 미만 |
| 보통 | 평균 긍정률 30~75% |
| 주의 | 평균 긍정률 30% 미만 |

## 주의사항

이 프로젝트는 개인 학습 목적의 프로토타입입니다.
카카오맵, 네이버, 뽈레의 이용약관에 따라 무단 크롤링은
법적 문제가 될 수 있습니다.
