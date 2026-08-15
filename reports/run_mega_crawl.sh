#!/usr/bin/env bash
# 지역×카테고리별 targets_*.txt(신규 크롤링 대상 id 목록)를 순회하며
# manage.py list --ids 로 크롤링. manage.py list 자체에 25곳마다 브라우저를
# 재시작하는 배치 로직이 있으므로, 여기서는 set -e 없이 지역 단위 실패가
# 다음 지역 진행을 막지 않도록만 한다(그레이스풀 디그레이드).
cd "C:\Users\jh960\Desktop\matzip"

run_batch() {
  local region="$1" cat="$2"
  local file="reports/targets_${region}_${cat}.txt"
  local area="부산 ${region} ${cat}"
  if [ ! -s "$file" ]; then
    echo "##### [$area] 대상 없음 — 스킵"
    return
  fi
  local ids
  ids=$(tr '\n' ' ' < "$file")
  local n
  n=$(wc -l < "$file")
  echo "##### [$area] 크롤링 시작 $(date '+%Y-%m-%d %H:%M:%S') (대상 ${n}곳)"
  python manage.py list --ids $ids --area "$area"
  echo "##### [$area] 완료 $(date '+%Y-%m-%d %H:%M:%S') (exit=$?)"
}

for region in 서면 해운대 광안리 기장; do
  for cat in 맛집; do
    run_batch "$region" "$cat"
  done
done

echo "===== 전체 크롤링 완료 $(date '+%Y-%m-%d %H:%M:%S') ====="
