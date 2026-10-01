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
        if src and "emoji" not in src and src not in urls:
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


def download_images(posts, outdir: Path, channel: str, limit: int = 6):
    """구간 내 게시물의 사진을 내려받아 파일로 저장. [(파일명, 게시물시각, 캡션)] 반환."""
    outdir.mkdir(parents=True, exist_ok=True)
    saved, n = [], 0
    for p in posts:
        for i, url in enumerate(p.get("photos") or []):
            if n >= limit:
                return saved
            name = f"{channel}_{p['dt']:%Y%m%d_%H%M}_{p['id'] or 0}_{i}.jpg"
            try:
                r = get(url, timeout=40)
                if r.status_code != 200 or len(r.content) < 5000:
                    continue
                (outdir / name).write_bytes(r.content)
                saved.append((name, p["dt"], (p.get("text") or "")[:120]))
                n += 1
            except Exception:
                continue
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

    # 시세는 수집하지 않는다. 지표는 텔레그램·유튜브 본문에 적힌 값만 쓴다.
    lines.append("\n\n## 시세\n")
    lines.append("시세는 수집하지 않는다. 지표·등락률은 텔레그램·유튜브 본문에 적힌 값만 쓰고, "
                 "없는 항목은 '소스 미제공'으로 남겨라. 추정 금지.\n")

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
