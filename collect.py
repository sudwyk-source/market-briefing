#!/usr/bin/env python3
"""
정기 시장 브리핑 — 수집 스크립트
텔레그램 7개 + 유튜브 9개를 구간 필터링해 번들 파일 하나로 만든다.
하루 2회차(05:30 미장 마감 / 22:00 미장 개장 전). 외부 금융 사이트는 쓰지 않는다.

사용법:
    python collect.py --session 0530        # 미장 마감 회차 (어제 22:00 ~ 지금)
    python collect.py --session 22          # 미장 개장 전 회차 (오늘 05:30 ~ 지금)
    python collect.py --session 22 --no-transcript   # 자막 생략(빠름)

필요 패키지:
    pip install requests beautifulsoup4 lxml youtube-transcript-api
"""

import argparse
import json
import os
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

PHOTO_RE = re.compile(r"background-image\s*:\s*url\(\s*['\"]?(https://[^'\")]+)['\"]?\s*\)", re.I)


def _photos(box):
    """게시물 박스에서 사진 URL을 뽑는다. 섹터 맵은 사진으로 올라온다."""
    urls = []
    for el in box.select("a.tgme_widget_message_photo_wrap, i.tgme_widget_message_photo,"
                         " .tgme_widget_message_photo_wrap, [style*='background-image']"):
        m = PHOTO_RE.search(el.get("style") or "")
        if m and m.group(1) not in urls:
            urls.append(m.group(1))
    for img in box.select("img.tgme_widget_message_photo, picture img, img[src^='https://']"):
        src = img.get("src")
        cls = " ".join(img.get("class") or [])
        if not src or "emoji" in src or "user_photo" in cls or "author_photo" in cls:
            continue          # 채널 아바타는 사진이 아니다
        if src not in urls:
            urls.append(src)
    # 링크 프리뷰 썸네일은 섹터 맵이 아니므로 제외
    prev = box.select_one("a.tgme_widget_message_link_preview")
    if prev:
        drop = set()
        for el in prev.select("[style*='background-image'], img[src^='https://']"):
            m = PHOTO_RE.search(el.get("style") or "")
            if m:
                drop.add(m.group(1))
            if el.get("src"):
                drop.add(el["src"])
        urls = [u for u in urls if u not in drop]
    return urls


def parse_telegram(html: str):
    """t.me/s/<ch> HTML → [{id, dt, text, photos}] (오래된 순)"""
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
        photos = _photos(box)
        # 사진만 있고 본문이 없는 게시물도 버리지 않는다 — 섹터 맵이 그런 형태다
        if text or photos:
            out.append({"id": mid, "dt": dt, "text": text, "photos": photos})
    out.sort(key=lambda p: p["dt"])
    return out


MIN_IMAGE_BYTES = 30000   # 아바타·작은 썸네일을 거른다. 섹터 맵은 훨씬 크다.


def download_images(posts, outdir: Path, channel: str, limit: int = 10):
    """게시물 사진을 최신순으로 내려받아 저장. [(파일명, 게시물시각, 캡션)] 반환.

    섹터 맵은 장마감 직후에 올라오므로 최신 쪽부터 받는다.
    URL이 같은 사진(채널 아바타 등)은 한 번만 받는다.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    saved, seen = [], set()
    for p in sorted(posts, key=lambda q: q["dt"], reverse=True):
        for i, url in enumerate(p.get("photos") or []):
            if len(saved) >= limit:
                return saved
            if url in seen:
                continue
            seen.add(url)
            name = f"{channel}_{p['dt']:%Y%m%d_%H%M}_{p['id'] or 0}_{i}.jpg"
            try:
                r = get(url, timeout=40)
                if r.status_code != 200 or len(r.content) < MIN_IMAGE_BYTES:
                    continue
                (outdir / name).write_bytes(r.content)
                saved.append((name, p["dt"], (p.get("text") or "")[:120]))
            except Exception:
                continue
    saved.sort(key=lambda t: t[1], reverse=True)
    return saved


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

# 11개 SPDR 섹터 ETF — finviz 맵이 보여주는 섹터 흐름을 숫자로 대체한다
SECTORS = [
    ("XLK",  "기술"),        ("XLC",  "커뮤니케이션"), ("XLY",  "경기소비재"),
    ("XLP",  "필수소비재"),  ("XLE",  "에너지"),       ("XLF",  "금융"),
    ("XLV",  "헬스케어"),    ("XLI",  "산업재"),       ("XLB",  "소재"),
    ("XLRE", "부동산"),      ("XLU",  "유틸리티"),
    ("SPY",  "S&P500"),      ("QQQ",  "나스닥100"),    ("SMH",  "반도체"),
]

# 관심종목. 저장소에 남기고 싶지 않으면 Actions Secret WATCHLIST에
# "NVDA,AVGO,..." 형태로 넣으면 그 값이 우선한다.
DEFAULT_WATCHLIST = ["NVDA", "AVGO", "ARM", "MRVL", "TSM", "MU", "SNDK",
                     "GOOG", "AMZN", "ORCL", "TSLA", "LITE", "AAOI",
                     "ETN", "BE", "GEV", "SOXX", "SPYM", "QQQM", "AIPO"]


def watchlist():
    env = os.environ.get("WATCHLIST", "").strip()
    return [t.strip().upper() for t in env.split(",") if t.strip()] or DEFAULT_WATCHLIST


def quote_yahoo(sym):
    """(종가, 등락%) — 야후 차트 API. 실패하면 None."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{sym}?range=5d&interval=1d")
    r = get(url, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    meta = r.json()["chart"]["result"][0]["meta"]
    last = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if last is None or not prev:
        raise RuntimeError("no price in meta")
    return round(float(last), 4), round((float(last) - float(prev)) / float(prev) * 100, 2)


def quote_stooq(sym):
    """야후가 막혔을 때의 대체 경로. 미국 주식은 <티커>.us."""
    s = sym.lower().replace("^", "").replace("-", ".")
    r = get(f"https://stooq.com/q/d/l/?s={s}.us&i=d", timeout=20)
    rows = [l for l in r.text.strip().split("\n") if l and l[0].isdigit()]
    if len(rows) < 2:
        raise RuntimeError("stooq empty")
    close = lambda row: float(row.split(",")[4])
    last, prev = close(rows[-1]), close(rows[-2])
    return round(last, 4), round((last - prev) / prev * 100, 2)


def fetch_quotes(symbols):
    """{sym: (종가, 등락%)} — 야후 먼저, 실패하면 stooq."""
    out, fails = {}, []
    for sym in symbols:
        for fn in (quote_yahoo, quote_stooq):
            try:
                out[sym] = fn(sym)
                break
            except Exception:
                continue
        else:
            fails.append(sym)
        time.sleep(0.15)
    return out, fails


def fetch_finviz_map():
    """finviz 맵의 원본 데이터(되면). 그림이 아니라 종목별 등락률 JSON."""
    r = get("https://finviz.com/api/map_perf.ashx?t=sec_all", timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    d = r.json()
    if not isinstance(d, dict) or not d:
        raise RuntimeError("unexpected shape")
    return d


# ─────────────────────────── 번들 ───────────────────────────

def build(session, out_path, want_transcript=True, img_dir=None):
    base, ext, now = window(session)
    checks, lines = [], []
    img_dir = Path(img_dir) if img_dir else Path(out_path).parent / "images"
    images = []

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
            # 섹터 맵은 insidertracking에 사진으로 올라온다 → 파일로 내려받아 둔다
            if ch == "insidertracking":
                images = download_images(inw + inext, img_dir, ch)
                checks.append({"source": "images/insidertracking", "status": "ok" if images else "NO_IMAGE",
                               "saved": len(images)})
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

    # 섹터 맵 이미지
    lines.append("\n\n## S&P500 섹터 맵 이미지\n")
    if images:
        lines.append(f"{img_dir.name}/ 에 {len(images)}장 저장. 가장 최근 것이 섹터 맵일 가능성이 높다. "
                     "이미지를 직접 열어 보고, 거기 보이는 것만으로 섹터 흐름을 서술하라.\n")
        for name, dt, cap in images:
            lines.append(f"- `{img_dir.name}/{name}` — {dt:%m-%d %H:%M} KST"
                         + (f" / 캡션: {cap}" if cap else " / 캡션 없음"))
    else:
        lines.append("맵 미게시 — 구간 내 insidertracking 사진 없음. 다른 경로로 대체하지 마라.\n")

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

    # 섹터·관심종목 시세 — finviz 맵을 숫자로 대체한다
    lines.append("\n\n## 섹터 흐름 (ETF 등락률)\n")
    try:
        sec_q, sec_fail = fetch_quotes([s for s, _ in SECTORS])
        ranked = sorted(((n, s, sec_q[s]) for s, n in SECTORS if s in sec_q),
                        key=lambda r: r[2][1], reverse=True)
        checks.append({"source": "quotes/sectors", "status": "ok" if ranked else "EMPTY",
                       "got": len(ranked), "failed": len(sec_fail)})
        if ranked:
            lines.append("| 섹터 | 티커 | 종가 | 등락 |")
            lines.append("|---|---|---|---|")
            for name, sym, (c, pct) in ranked:
                lines.append(f"| {name} | {sym} | {c:,} | {pct:+.2f}% |")
            top = ranked[0]
            bot = ranked[-1]
            lines.append(f"\n가장 강한 섹터: {top[0]} {top[2][1]:+.2f}% / "
                         f"가장 약한 섹터: {bot[0]} {bot[2][1]:+.2f}%")
        if sec_fail:
            lines.append(f"\n조회 실패: {', '.join(sec_fail)}")
    except Exception as e:
        checks.append({"source": "quotes/sectors", "status": "ERROR", "detail": str(e)[:120]})
        lines.append(f"섹터 시세 수집 실패: {type(e).__name__} {str(e)[:120]}\n")

    lines.append("\n\n## 관심종목 등락률\n")
    try:
        wl = watchlist()
        w_q, w_fail = fetch_quotes(wl)
        checks.append({"source": "quotes/watchlist", "status": "ok" if w_q else "EMPTY",
                       "got": len(w_q), "failed": len(w_fail)})
        if w_q:
            lines.append("| 종목 | 종가 | 등락 |")
            lines.append("|---|---|---|")
            for sym in wl:
                if sym in w_q:
                    c, pct = w_q[sym]
                    lines.append(f"| {sym} | {c:,} | {pct:+.2f}% |")
        if w_fail:
            lines.append(f"\n조회 실패(등락 미확인으로 처리할 것): {', '.join(w_fail)}")
    except Exception as e:
        checks.append({"source": "quotes/watchlist", "status": "ERROR", "detail": str(e)[:120]})
        lines.append(f"관심종목 시세 수집 실패: {type(e).__name__} {str(e)[:120]}\n")

    # finviz 맵 원본 데이터 — 되면 얹고, 막히면 그렇게 적는다
    lines.append("\n\n## finviz 맵 데이터\n")
    try:
        fv = fetch_finviz_map()
        checks.append({"source": "finviz/map", "status": "ok", "entries": len(fv)})
        items = [(k, v) for k, v in fv.items() if isinstance(v, (int, float))]
        items.sort(key=lambda kv: kv[1], reverse=True)
        lines.append(f"종목 {len(items)}개의 등락률을 받았다(그림이 아니라 수치).\n")
        lines.append("상승 상위 15: " + ", ".join(f"{k} {v:+.2f}%" for k, v in items[:15]))
        lines.append("\n하락 상위 15: " + ", ".join(f"{k} {v:+.2f}%" for k, v in items[-15:]))
    except Exception as e:
        checks.append({"source": "finviz/map", "status": "BLOCKED", "detail": str(e)[:80]})
        lines.append(f"finviz 접근 불가 ({type(e).__name__}). "
                     "위의 섹터 ETF 표로 섹터 흐름을 읽어라. 맵을 봤다고 쓰지 마라.\n")

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
    ap.add_argument("--images", default=None,
                    help="섹터 맵 이미지를 저장할 디렉터리(기본: 번들 파일 옆 images/)")
    a = ap.parse_args()
    out = a.out or f"bundle_{datetime.now(KST):%Y%m%d}_{a.session}.md"
    sys.exit(build(a.session, out, want_transcript=not a.no_transcript,
                   img_dir=a.images))
