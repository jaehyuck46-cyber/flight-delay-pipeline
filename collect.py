"""
collect.py ???곗씠???섏쭛 ?대떦 ?뚯씪

??븷: ?쒓뎅怨듯빆怨듭궗 ?ㅼ떆媛??꾩갑??API瑜??몄텧 ??源??GMP) ?몃쭔 怨⑤씪 ??DB ???
?ъ슜踰?
  py collect.py --inspect   # 源???몃쭔 ?붾㈃??異쒕젰 (???????
  py collect.py             # 源?????섏쭛 ??DB ???
"""

import os
import sys
import xml.etree.ElementTree as ET   # XML ?뚯떛??(?뚯씠??湲곕낯 ?댁옣)
from datetime import datetime, timezone, timedelta

import urllib.request, urllib.parse
from dotenv import load_dotenv

from db import get_connection, init_db

load_dotenv()

# ?? ?ㅼ젙 ??
BASE_URL = "https://apis.data.go.kr/B551178/flight-status/arrival"
SERVICE_KEY = (os.getenv("KAC_SERVICE_KEY") or "").strip()
KST = timezone(timedelta(hours=9))

# 源?ш났??肄붾뱶 (?꾩갑?몄씠??arrAirportCode 媛 GMP ??寃껊쭔)
TARGET_AIRPORT = "GMP"

# 吏???먮떒 湲곗?: ?덉젙蹂대떎 15遺?珥덇낵
DELAY_THRESHOLD_MIN = 15


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
def fetch_all_arrivals(max_pages=25):
    """API瑜??щ윭 ?섏씠吏 ?뚮ŉ ?꾧뎅 ?꾩갑?몄쓣 ??諛쏆븘 dict 由ъ뒪?몃줈 諛섑솚.
    ??API????踰덉뿉 理쒕? 100嫄대쭔 二쇰?濡?pageNo瑜??섍꺼媛硫?紐⑥???
    """
    import urllib.request

    if not SERVICE_KEY:
        raise RuntimeError("??.env ??KAC_SERVICE_KEY 媛 ?놁뼱!")

    flights = []
    total_count = None

    for page in range(1, max_pages + 1):
        # ?좑툘 numOfRows ??諛섎뱶??100 ?댄븯! (珥덇낵?섎㈃ 鍮??묐떟 ??
               # airport=源??濡?源???꾩갑?몃쭔 諛쏅뒗??(?꾧뎅 ??諛쏄퀬 嫄곕Ⅴ??寃껊낫???⑥뵮 媛踰쇱?)
        airport = urllib.parse.quote("源??)
        url = f"{BASE_URL}?serviceKey={SERVICE_KEY}&numOfRows=100&pageNo={page}&airport={airport}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8")

        root = ET.fromstring(text)
        result_code = root.findtext(".//resultCode")
        if result_code and result_code != "00":
            msg = root.findtext(".//resultMsg")
            raise RuntimeError(f"API ?먮윭: {result_code} / {msg}")

        if total_count is None:
            total_count = int(root.findtext(".//totalCount") or 0)

        page_items = root.findall(".//item")
        if not page_items:      # ???댁긽 ?곗씠???놁쑝硫?以묐떒
            break

        for item in page_items:
            flights.append({
                "fid":       item.findtext("fid"),
                "flightid":  item.findtext("flightid"),
                "airline":   item.findtext("airline"),
                "dep_code":  item.findtext("depAirportCode"),
                "arr_code":  item.findtext("arrAirportCode"),
                "scheduled": item.findtext("scheduledatetime"),
                "estimated": item.findtext("estimateddatetime"),
                "status":    item.findtext("rmkKor"),
                "io":        item.findtext("io"),
                "line":      item.findtext("line"),
            })

        # ??諛쏆븯?쇰㈃ 以묐떒
        if len(flights) >= total_count:
            break

    print(f"?벀 ?꾧뎅 ?꾩갑??{len(flights)}嫄??섏쭛 (totalCount={total_count})")
    return flights


def filter_gimpo(flights):
    """?꾧뎅 ?꾩갑??以?源??GMP) ?꾩갑留?怨⑤씪?몃떎."""
    return [f for f in flights if f["arr_code"] == TARGET_AIRPORT]


def inspect():
    """????놁씠 源???꾩갑?몃쭔 ?붾㈃??異쒕젰."""
    print("?뵇 API ?몄텧 ??源???꾩갑??異붾━??以?..\n")
    all_flights = fetch_all_arrivals()
    gmp = filter_gimpo(all_flights)

    print(f"?꾩껜 ?꾩갑?? {len(all_flights)}嫄?/ 洹몄쨷 源??GMP): {len(gmp)}嫄?n")
    print("?? 源???꾩갑??誘몃━蹂닿린 ??")
    for f in gmp[:15]:
        print(f"  {f['flightid']:8} {f['airline']:10} "
              f"{f['dep_code']}?묰MP  ?덉젙 {f['scheduled']}  "
              f"?덉긽 {f['estimated']}  [{f['status']}]")


def save(flights):
    """源???꾩갑?몄쓣 DB?????(UPSERT + ?곹깭蹂寃??대젰)."""
    conn = get_connection()
    cur = conn.cursor()
    now = now_kst()
    changed = 0

    for f in flights:
        # 怨좎쑀?? API??fid ?ъ슜 (?몃퀎 怨좎쑀)
        key = f["fid"]

        # 湲곗〈 ?곹깭 議고쉶
        cur.execute("SELECT status, estimated_dt FROM flights WHERE flight_key=?", (key,))
        row = cur.fetchone()

        # flights ?뚯씠釉?UPSERT
        cur.execute("""
            INSERT INTO flights
                (flight_key, flight_id, airline, airport, io_type,
                 scheduled_dt, estimated_dt, status, collected_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(flight_key) DO UPDATE SET
                estimated_dt=excluded.estimated_dt,
                status=excluded.status,
                updated_at=excluded.updated_at
        """, (key, f["flightid"], f["airline"], f["arr_code"], f["io"],
              f["scheduled"], f["estimated"], f["status"], now, now))

        # ?곹깭???덉긽?쒓컖??諛붾뚯뿀?쇰㈃ ?대젰 ?④린湲?
        if row is None or row["status"] != f["status"] or row["estimated_dt"] != f["estimated"]:
            cur.execute("""
                INSERT INTO flight_events (flight_key, status, estimated_dt, collected_at)
                VALUES (?,?,?,?)
            """, (key, f["status"], f["estimated"], now))
            changed += 1

    # ?섏쭛 濡쒓렇
    cur.execute("""
        INSERT INTO collection_log
            (collected_at, airport, rows_fetched, rows_changed, success, error_message)
        VALUES (?,?,?,?,?,?)
    """, (now, TARGET_AIRPORT, len(flights), changed, 1, None))

    conn.commit()
    conn.close()
    print(f"??????꾨즺: 源??{len(flights)}嫄?(?곹깭蹂寃?{changed}嫄? @ {now}")


def collect():
    """?ㅼ젣 ?섏쭛: API ??源???꾪꽣 ?????"""
    init_db()   # ?뚯씠釉??놁쑝硫?留뚮뱾湲?
    all_flights = fetch_all_arrivals()
    gmp = filter_gimpo(all_flights)
    save(gmp)


if __name__ == "__main__":
    if "--inspect" in sys.argv:
        inspect()
    else:
        collect()
