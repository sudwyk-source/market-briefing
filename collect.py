#!/usr/bin/env python3
"""
정기 시장 브리핑 — 수집 스크립트
텔레그램 7개 + 유튜브 9개를 구간 필터링해 번들 파일 하나로 만든다.
하루 2회차(05:30 미장 마감 / 22:00 미장 개장 전). 외부 금융 사이트는 쓰지 않는다.

사용법:
    python collect.py --session 0530        # 미장 마감 회차 (어제 22:00 ~ 지금)
    python collect.py --session 22          # 미장 개장 전 회차 (오늘 05:30 ~ 지금)
    python collect.py --session 22 --no-transcript   # 자막 생략(빠름)
    python collect.py --session 0530 --quotes        # 시세까지(기본 off)

필요 패키지:
    pip install requests beautifulsoup4 lxml yfinance youtube-transcript-api
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"}

# ─────────────────────────── 설정 ───────────────────────────

TELEGRAM = [
    ("bornlupin",        "해외 리서치·증권사 리포트 (미국 중심, 최우선)"),
    ("insidertracking",  "속보·지정학·정책, 프리마켓 브리핑 (미국 중심, 최우선)"),
    ("TNBfolio",         "뉴스 번역 (원 출처 명시형)"),
    ("umbrellaresearch", "국내외 혼합, AWAKE 시장 한눈에 보기"),
    ("autoteamkorea",    "한투 자동차 섹터"),
    ("minionsstock",     "국내 중소형 테마"),
    ("HJS_YSK",          "유안타 유틸리티·전력기기·음식료"),
]

YOUTUBE = [
    ("UC_JJ_NhRqPKcIOj5Ko3W_3w", "오선의 미국 증시 라이브 (미국 마감 요약, 최우선)"),
    ("UCWskYkV4c4S9D__rsfOl2JA", "한경 글로벌마켓 (뉴욕 특파원 매크로, 최우선)"),
    ("UCC3yfxS5qC6PCwDzetUuEWg", "소수몽키 (미국 테마·수혜주)"),
    ("UCH2sxkxg_vdJSK4KYRXNE0Q", "T3chfeed (테슬라·팔란티어·스페이스X)"),
    ("UCiDmfbYvuMEVbRxPmFP4sng", "알상무 (금리·매크로)"),
    ("UCGCGxsbmG_9nincyI7xypow", "한경 코리아마켓"),
    ("UCsJ6RuBiTVWRX156FVbeaGg", "슈카월드"),
    ("UCJo6G1u0e_-wS-JQn3T-zEw", "머니코믹스"),
    ("UCVKdDIkp_AiioJiy9NoELgQ", "한희재의 투자교실"),
]

# 관심종목 20개 + 지수·금리·환율·원자재
WATCHLIST = ["NVDA", "AVGO", "ARM", "MRVL", "TSM", "MU", "SNDK",
             "GOOG", "AMZN", "ORCL", "TSLA", "LITE", "AAOI",
             "ETN", "BE", "GEV", "SOXX", "SPYM", "QQQM", "AIPO"]

MACRO = {
    "^GSPC": "S&P500", "^DJI": "다우", "^IXIC": "나스닥", "^NDX": "나스닥100",
    "^SOX": "필라델피아반도체", "^VIX": "VIX",
    "^IRX": "미국채 13주", "^FVX": "미국채 5년", "^TNX": "미국채 10년", "^TYX": "미국채 30년",
    "DX-Y.NYB": "달러인덱스", "KRW=X": "달러/원", "JPY=X": "달러/엔",
    "CL=F": "WTI", "BZ=F": "브렌트", "GC=F": "금", "SI=F": "은",
    "HG=F": "구리", "NG=F": "천연가스", "BTC-USD": "비트코인",
    "^KS11": "코스피", "^KQ11": "코스닥", "^N225": "니케이", "000001.SS": "상해종합",
}

# 하루 2회차. (어느 날, 시, 분) — prev = 어제
SESSION_WINDOW = {
    "0530": ("prev", 22, 0),   # 미장 마감 회차: 어제 22:00 ~ 지금
    "22":   ("same",  5, 30),  # 미장 개장 전 회차: 오늘 05:30 ~ 지금
}
LOOKBACK_HOURS = 18  # 직전 회차가 빠졌을 수 있으므로 이만큼 더 거슬러 올라간다(직전 구간 전체를 덮는 길이)


# ─────────────────────────── 유틸 ───────────────────────────

def window(session: str, now: datetime = None):
    """(기본구간 시작, 확장구간 시작, 지금) — 전부 KST aware datetime"""
    now = now or datetime.now(KST)
    day, h, m = SESSION_WINDOW[session]
    if day == "prev":
        start = (now - timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
    else:
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if start > now:
            start -= timedelta(days=1)
    return start, start - timedelta(hours=LOOKBACK_HOURS), now


def clean(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get(url, **kw):
    kw.setdefault("timeout", 25)
    kw.setdefault("headers", UA)
    return requests.get(url, **kw)


# ─────────────────────────── 텔레그램 ───────────────────────────

def parse_telegram(html: str):
    """t.me/s/<ch> HTML → [{id, dt, text}] (오래된 순)"""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for box in soup.select("div.tgme_widget_message"):
        t = box.select_one("time[datetime]")
        if not t:
            continue
        try:
            dt = datetime.fromisoformat(t["datetime"].replace("Z", "+00:00")).astimezone(KST)
        except ValueError:
            continue
        body = box.select_one("div.tgme_widget_message_text")
        text = body.get_text("\n") if body else ""
        # 링크 프리뷰 제목/설명도 본문에 가치가 있어 포함
        prev = box.select_one("a.tgme_widget_message_link_preview")
        if prev:
            bits = [e.get_text(" ") for e in prev.select(
                ".link_preview_site_name, .link_preview_title, .link_preview_description")]
            if bits:
                text += "\n[링크] " + " / ".join(b.strip() for b in bits if b.strip())
        raw_id = box.get("data-post", "")
        mid = int(raw_id.split("/")[-1]) if "/" in raw_id and raw_id.split("/")[-1].isdigit() else None
        text = clean(text)
        if text:
            out.append({"id": mid, "dt": dt, "text": text})
    out.sort(key=lambda p: p["dt"])
    return out


def fetch_telegram(channel: str, since: datetime, max_pages: int = 6):
    """since 이후 게시물을 모을 때까지 ?before= 로 거슬러 올라간다."""
    collected, seen, before, pages, parsed_any = [], set(), None, 0, 0
    while pages < max_pages:
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        r = get(url)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        posts = parse_telegram(r.text)
        parsed_any += len(posts)
        if not posts:
            break
        for p in posts:
            key = p["id"] or p["dt"].isoformat()
            if key not in seen:
                seen.add(key)
                collected.append(p)
        pages += 1
        oldest = min(p["dt"] for p in posts)
        if oldest <= since:
            break                      # 충분히 거슬러 올라감
        ids = [p["id"] for p in posts if p["id"]]
        if not ids:
            break
        before = min(ids)
        time.sleep(0.4)
    collected.sort(key=lambda p: p["dt"])
    return collected, parsed_any


# ─────────────────────────── 유튜브 ───────────────────────────

def fetch_youtube_list(channel_id: str):
    r = get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    soup = BeautifulSoup(r.text, "xml")
    vids = []
    for e in soup.find_all("entry"):
        pub = e.find("published")
        vid = e.find("videoId")
        ttl = e.find("title")
        if not (pub and vid and ttl):
            continue
        vids.append({
            "id": vid.text,
            "title": ttl.text,
            "dt": datetime.fromisoformat(pub.text.replace("Z", "+00:00")).astimezone(KST),
        })
    return vids


def fetch_transcript(video_id: str):
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    tr = api.fetch(video_id, languages=["ko", "en"])
    snips = getattr(tr, "snippets", tr)
    return clean(" ".join(s.text for s in snips))


# ─────────────────────────── 시세 ───────────────────────────

def fetch_quotes(symbols):
    import yfinance as yf
    rows = {}
    data = yf.download(list(symbols), period="5d", interval="1d",
                       progress=False, auto_adjust=False, group_by="ticker")
    for sym in symbols:
        try:
            df = data[sym].dropna() if len(symbols) > 1 else data.dropna()
            if len(df) < 2:
                continue
            last, prev = df["Close"].iloc[-1], df["Close"].iloc[-2]
            rows[sym] = {"close": round(float(last), 4),
                         "chg_pct": round(float((last - prev) / prev * 100), 2)}
        except Exception:
            continue
    return rows


# ─────────────────────────── 번들 ───────────────────────────

def build(session, out_path, want_transcript=True, want_quotes=False):
    base, ext, now = window(session)
    checks, lines = [], []

    lines.append(f"# 브리핑 수집 번들 — {session}시 회차")
    lines.append(f"생성: {now:%Y-%m-%d %H:%M} KST")
    lines.append(f"기본 구간: {base:%m-%d %H:%M} ~ {now:%m-%d %H:%M}")
    lines.append(f"확장 구간: {ext:%m-%d %H:%M} ~ {base:%m-%d %H:%M} (직전 회차 보충용)\n")

    # 텔레그램
    lines.append("\n## 텔레그램\n")
    for ch, desc in TELEGRAM:
        try:
            posts, parsed = fetch_telegram(ch, ext)
            inw = [p for p in posts if p["dt"] >= base]
            inext = [p for p in posts if ext <= p["dt"] < base]
            status = "ok" if parsed else "PARSE_FAIL"
            checks.append({"source": f"tg/{ch}", "status": status,
                           "in_window": len(inw), "in_lookback": len(inext), "parsed": parsed})
            lines.append(f"\n### {ch} — {desc}")
            lines.append(f"구간 내 {len(inw)}건 / 확장 구간 {len(inext)}건 "
                         f"(페이지에서 파싱한 총 게시물 {parsed}건)\n")
            for p in inw:
                lines.append(f"**[{p['dt']:%m-%d %H:%M}]**\n{p['text']}\n")
            if inext:
                lines.append(f"\n<확장 구간 — 직전 회차에서 빠졌을 수 있음>\n")
                for p in inext:
                    lines.append(f"**[{p['dt']:%m-%d %H:%M}]**\n{p['text']}\n")
        except Exception as e:
            checks.append({"source": f"tg/{ch}", "status": "ERROR", "detail": str(e)[:120]})
            lines.append(f"\n### {ch} — 수집 실패: {type(e).__name__} {str(e)[:120]}\n")

    # 유튜브
    lines.append("\n\n## 유튜브\n")
    for cid, desc in YOUTUBE:
        try:
            vids = fetch_youtube_list(cid)
            inw = [v for v in vids if v["dt"] >= ext]
            checks.append({"source": f"yt/{desc[:20]}", "status": "ok" if vids else "EMPTY_FEED",
                           "in_window": len([v for v in vids if v["dt"] >= base]),
                           "in_lookback": len([v for v in vids if ext <= v["dt"] < base])})
            lines.append(f"\n### {desc}")
            if not inw:
                lines.append("구간 내 신규 없음\n")
                continue
            for v in inw:
                tag = "" if v["dt"] >= base else "  [확장 구간]"
                lines.append(f"\n**{v['title']}** — {v['dt']:%m-%d %H:%M}{tag}")
                lines.append(f"https://www.youtube.com/watch?v={v['id']}")
                if want_transcript:
                    try:
                        txt = fetch_transcript(v["id"])
                        lines.append(f"\n자막 ({len(txt):,}자):\n{txt}\n")
                    except Exception as e:
                        lines.append(f"\n자막 확인 불가 — {type(e).__name__}. "
                                     f"제목/설명만. 내용 해설을 쓰지 말 것.\n")
        except Exception as e:
            checks.append({"source": f"yt/{desc[:20]}", "status": "ERROR", "detail": str(e)[:120]})
            lines.append(f"\n### {desc} — 수집 실패: {type(e).__name__}\n")

    # 시세 — 기본 OFF. 브리핑은 텔레그램·유튜브에 실제로 적힌 숫자만 쓴다.
    lines.append("\n\n## 시세\n")
    if not want_quotes:
        lines.append("시세 수집은 꺼져 있다(--quotes 로 켤 수 있음). "
                     "지표·등락률은 텔레그램·유튜브 본문에 적힌 값만 쓰고, "
                     "없는 항목은 '소스 미제공'으로 남겨라. 추정 금지.\n")
    else:
        try:
            macro = fetch_quotes(list(MACRO.keys()))
            watch = fetch_quotes(WATCHLIST)
            checks.append({"source": "quotes", "status": "ok" if macro else "EMPTY",
                           "macro": len(macro), "watchlist": len(watch)})
            lines.append("\n### 지수·금리·환율·원자재\n")
            lines.append("| 지표 | 종가 | 변동 |")
            lines.append("|---|---|---|")
            for sym, name in MACRO.items():
                q = macro.get(sym)
                if q:
                    lines.append(f"| {name} | {q['close']:,} | {q['chg_pct']:+.2f}% |")
            lines.append("\n### 관심종목\n")
            lines.append("| 종목 | 종가 | 변동 |")
            lines.append("|---|---|---|")
            for sym in WATCHLIST:
                q = watch.get(sym)
                if q:
                    lines.append(f"| {sym} | {q['close']:,} | {q['chg_pct']:+.2f}% |")
            lines.append("\n※ 삼성전자·SK하이닉스는 005930.KS / 000660.KS 로 조회 가능하나 "
                         "국내 장 마감 기준이라 미국 흐름과 시점이 어긋난다. 별도 확인 필요.\n")
        except Exception as e:
            checks.append({"source": "quotes", "status": "ERROR", "detail": str(e)[:120]})
            lines.append(f"\n시세 수집 실패: {type(e).__name__} {str(e)[:120]}\n")

    # 수집 점검
    lines.append("\n\n## 수집 점검\n")
    lines.append("```json")
    lines.append(json.dumps(checks, ensure_ascii=False, indent=1))
    lines.append("```")

    broken = [c for c in checks if c["status"] not in ("ok",)]
    if broken:
        lines.append("\n⚠️ 아래 소스가 정상 수집되지 않았다. 브리핑에 반드시 명시할 것:")
        for c in broken:
            lines.append(f"- {c['source']}: {c['status']} {c.get('detail','')}")

    text = "\n".join(lines)
    Path(out_path).write_text(text, encoding="utf-8")

    print(f"번들 저장: {out_path}  ({len(text):,}자)", file=sys.stderr)
    for c in checks:
        print(f"  {c['source']:28} {c['status']:12} {c.get('in_window','')}", file=sys.stderr)
    # 전 채널 파싱 실패는 구조 변경 신호 → 종료코드 1
    tg = [c for c in checks if c["source"].startswith("tg/")]
    if tg and all(c["status"] != "ok" for c in tg):
        print("!! 텔레그램 전 채널 수집 실패 — HTML 구조가 바뀌었을 수 있음", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True, choices=["0530", "22"])
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-transcript", action="store_true")
    ap.add_argument("--quotes", action="store_true",
                    help="yfinance 시세도 함께 수집(기본 off — 브리핑은 소스에 적힌 숫자만 쓴다)")
    a = ap.parse_args()
    out = a.out or f"bundle_{datetime.now(KST):%Y%m%d}_{a.session}.md"
    sys.exit(build(a.session, out, want_transcript=not a.no_transcript,
                   want_quotes=a.quotes))
