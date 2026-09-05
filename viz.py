"""
viz.py — 저장된 데이터로 정적 차트(HTML) 하나를 만든다.

역할: flights.db를 읽어 '항공사별/시간대별 15분 지연율'을 계산하고,
      그 숫자를 박아넣은 docs/index.html 파일을 만든다.
      (analyze.py가 '터미널에 출력'했다면, viz.py는 '웹페이지로 저장')
사용법: py viz.py  →  docs/index.html 생성 → 브라우저로 열어 확인
"""

import json
from pathlib import Path
from collections import defaultdict
from datetime import datetime

from db import get_connection

OUT_DIR = Path(__file__).parent / "docs"
OUT_PATH = OUT_DIR / "index.html"


def _parse_dt(text):
    """'YYYYMMDDHHMM' 문자열을 datetime으로. 이상하면 None. (analyze.py와 동일)"""
    try:
        return datetime.strptime(text, "%Y%m%d%H%M")
    except (ValueError, TypeError):
        return None


def _load_arrivals():
    """도착 완료 + 계획·예상시각이 둘 다 있는 편만 읽어온다."""
    conn = get_connection()
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT airline, scheduled_dt, estimated_dt
        FROM flights
        WHERE status = '도착'
          AND scheduled_dt IS NOT NULL AND scheduled_dt != ''
          AND estimated_dt IS NOT NULL AND estimated_dt != ''
    """).fetchall()
    conn.close()
    return rows


def delay_by_airline(rows, min_flights=20):
    """항공사별 15분 지연율(%). 20편 이상만, 지연율 높은 순."""
    stats = defaultdict(lambda: [0, 0])   # 항공사 -> [총편수, 15분지연 건수]
    for r in rows:
        sched = _parse_dt(r["scheduled_dt"])
        est = _parse_dt(r["estimated_dt"])
        if sched is None or est is None:
            continue
        delay = (est - sched).total_seconds() / 60
        s = stats[r["airline"] or "(없음)"]
        s[0] += 1
        if delay >= 15:
            s[1] += 1

    out = [(name, cnt, d15) for name, (cnt, d15) in stats.items() if cnt >= min_flights]
    out.sort(key=lambda x: x[2] / x[1], reverse=True)
    labels = [name for name, cnt, d15 in out]
    values = [round(d15 / cnt * 100, 1) for name, cnt, d15 in out]
    return labels, values


def delay_by_hour(rows):
    """계획시각의 '시' 기준 15분 지연율(%)."""
    stats = defaultdict(lambda: [0, 0])
    for r in rows:
        sched = _parse_dt(r["scheduled_dt"])
        est = _parse_dt(r["estimated_dt"])
        if sched is None or est is None:
            continue
        delay = (est - sched).total_seconds() / 60
        s = stats[sched.hour]
        s[0] += 1
        if delay >= 15:
            s[1] += 1

    hours = sorted(stats)
    labels = [f"{h:02d}시" for h in hours]
    values = [round(stats[h][1] / stats[h][0] * 100, 1) for h in hours]
    return labels, values


# HTML 뼈대. __XXX__ 부분을 나중에 실제 값으로 갈아끼운다.
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>김포공항 도착편 지연 분석</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  body { font-family: system-ui, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 16px; color:#222; }
  h1 { font-size: 1.5rem; }
  h2 { font-size: 1.1rem; margin-top: 0; }
  .meta { color: #666; font-size: 0.9rem; margin-bottom: 32px; }
  .card { margin-bottom: 48px; }
</style>
</head>
<body>
  <h1>김포공항(GMP) 도착편 지연 분석</h1>
  <p class="meta">대상: 도착 완료편 __TOTAL__건 · 생성 __GENERATED__</p>

  <div class="card">
    <h2>항공사별 15분 지연율 (20편 이상)</h2>
    <canvas id="airlineChart"></canvas>
  </div>

  <div class="card">
    <h2>시간대별 15분 지연율</h2>
    <canvas id="hourChart"></canvas>
  </div>

<script>
  const airlineLabels = __AIRLINE_LABELS__;
  const airlineValues = __AIRLINE_VALUES__;
  const hourLabels = __HOUR_LABELS__;
  const hourValues = __HOUR_VALUES__;

  new Chart(document.getElementById('airlineChart'), {
    type: 'bar',
    data: { labels: airlineLabels,
            datasets: [{ label: '15분 지연율(%)', data: airlineValues }] },
    options: { scales: { y: { beginAtZero: true } } }
  });

  new Chart(document.getElementById('hourChart'), {
    type: 'bar',
    data: { labels: hourLabels,
            datasets: [{ label: '15분 지연율(%)', data: hourValues }] },
    options: { scales: { y: { beginAtZero: true } } }
  });
</script>
</body>
</html>
"""


def main():
    rows = _load_arrivals()
    total = len(rows)
    a_labels, a_values = delay_by_airline(rows)
    h_labels, h_values = delay_by_hour(rows)

    # __XXX__ 자리에 실제 값을 넣는다.
    # json.dumps: 파이썬 리스트를 자바스크립트가 읽을 수 있는 글자로 바꿔준다.
    html = (HTML_TEMPLATE
        .replace("__TOTAL__", str(total))
        .replace("__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M"))
        .replace("__AIRLINE_LABELS__", json.dumps(a_labels, ensure_ascii=False))
        .replace("__AIRLINE_VALUES__", json.dumps(a_values))
        .replace("__HOUR_LABELS__", json.dumps(h_labels, ensure_ascii=False))
        .replace("__HOUR_VALUES__", json.dumps(h_values))
    )

    OUT_DIR.mkdir(exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"✅ 차트 생성 완료 → {OUT_PATH}")
    print("   브라우저로 이 파일을 열어 확인하세요.")


if __name__ == "__main__":
    main()
    