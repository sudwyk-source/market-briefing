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
    """게시물의 사진·동영상 썸네일 URL. 섹터 맵은 사진이나 앨범으로 올라온다."""
    urls = []
    sel = ("a.tgme_widget_message_photo_wrap, i.tgme_widget_message_photo,"
           " .tgme_widget_message_photo_wrap, a.grouped_media_wrap,"
           " .tgme_widget_message_video_thumb, i.tgme_widget_message_video_thumb,"
           " [style*='background-image']")
    for el in box.select(sel):
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
    prev = box.select_one("a.tgme_widget_message_link_preview")
    if prev:                                  # 링크 프리뷰 썸네일은 제외
        drop = set()
        for el in prev.select("[style*='background-image'], img[src^='https://']"):
            m = PHOTO_RE.search(el.get("style") or "")
            if m:
                drop.add(m.group(1))
            if el.get("src"):
                drop.add(el["src"])
        urls = [u for u in urls if u not in drop]
    return urls


def _box_hint(box, limit=14):
    """사진 URL이 안 잡혔을 때, 그 글에 어떤 class가 있었는지 짧게 남긴다.
    '미장 마감' 글의 맵 이미지를 놓친 적이 있어 원인을 추적하기 위한 단서다."""
    seen = []
    for el in box.find_all(True):
        for c in (el.get("class") or []):
            if c.startswith("tgme_widget_message_") and c not in seen:
                seen.append(c)
            if len(seen) >= limit:
                return seen
    return seen


def _media_kinds(box):
    """사진 URL이 안 잡히는 첨부(문서·동영상·음성)의 종류를 적어 둔다."""
    kinds = []
    for css, name in (("div.tgme_widget_message_document", "문서"),
                      ("div.tgme_widget_message_video_player", "동영상"),
                      ("a.tgme_widget_message_video_player", "동영상"),
                      ("audio.tgme_widget_message_voice", "음성"),
                      ("div.tgme_widget_message_roundvideo_player", "원형영상"),
                      ("div.tgme_widget_message_poll", "투표"),
                      ("div.tgme_widget_message_sticker_wrap", "스티커")):
        if box.select_one(css):
            kinds.append(name)
    return kinds


def parse_telegram(html: str, channel: str = ""):
    """t.me/s/<ch> HTML → [{id, dt, text, photos, media}] (오래된 순)"""
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
        # data-post 는 "<채널>/<글번호>" 이므로 호출자가 채널명을 안 넘겨도 여기서 복원된다.
        ch = channel or (raw_id.split("/")[0] if "/" in raw_id else "")
        text = clean(text)
        photos = _photos(box)
        kinds = _media_kinds(box)
        # 본문도 사진도 없는 글이라도 버리지 않는다. 섹터 맵이 그렇게 들어왔다가
        # 통째로 사라진 적이 있다. 무엇이 붙어 있었는지와 글 주소를 남긴다.
        if not text:
            tag = "/".join(kinds) if kinds else ("사진" if photos else "첨부 불명")
            text = f"[본문 없는 게시물 — {tag}]"
            if mid and ch:
                text += f" https://t.me/{ch}/{mid}"
        rec = {"id": mid, "dt": dt, "text": text, "photos": photos, "media": kinds}
        if not photos:
            rec["hint"] = _box_hint(box)
        out.append(rec)
    out.sort(key=lambda p: p["dt"])
    return out


MIN_IMAGE_BYTES = 30000   # 아바타·작은 썸네일을 거른다. 섹터 맵은 훨씬 크다.


def _image_priority(posts, close_band=(4, 8)):
    """장마감 직후(KST 04~08시)에 올라온 글을 먼저, 나머지는 최신순.

    섹터 맵은 마감 직후에 올라온다. 수집이 늦게 돌아도 그 밴드를 놓치지
    않도록 최신순보다 앞세운다(실제로 최신순만 썼다가 05:23 맵을 놓쳤다).
    """
    lo, hi = close_band
    band = [p for p in posts if lo <= p["dt"].hour < hi]
    rest = [p for p in posts if not (lo <= p["dt"].hour < hi)]
    band.sort(key=lambda q: q["dt"], reverse=True)
    rest.sort(key=lambda q: q["dt"], reverse=True)
    return band + rest


def download_images(posts, outdir: Path, channel: str, limit: int = 12):
    """게시물 사진을 내려받아 저장. [(파일명, 게시물시각, 캡션)] 반환.

    순서는 _image_priority — 장마감 직후 밴드 먼저, 그다음 최신순.
    URL이 같은 사진(채널 아바타 등)은 한 번만 받는다.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    saved, seen = [], set()
    for p in _image_priority(posts):
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
        posts = parse_telegram(r.text, channel)
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

def _yt_feed_candidates(cid: str):
    """RSS 피드 주소 후보들. 한 가지만 믿지 않는다.

    10-02 실행에서 9개 채널이 전부 HTTP 404로 떨어졌다. 그 전날에는 같은 주소가
    되던 것이라, 주소가 바뀐 것인지 깃허브 IP가 막힌 것인지 알 수 없었다.
    그래서 후보를 돌려보고 '무엇이 무엇을 돌려줬는지'를 번들에 남긴다.
    """
    return [
        ("기본", f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", None),
        ("지역지정", f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}&hl=ko&gl=KR", None),
        ("www없음", f"https://youtube.com/feeds/videos.xml?channel_id={cid}", None),
        ("UA없음", f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
         {"User-Agent": "", "Accept": "application/atom+xml,application/xml;q=0.9"}),
        ("피드전용UA", f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
         {"User-Agent": "feedparser/6.0", "Accept": "*/*"}),
    ]


YT_PROBE = []   # 어느 후보가 무엇을 돌려줬는지 (첫 채널에서만 기록)


def fetch_youtube_list(channel_id: str):
    xml, why = None, []
    for name, url, hdr in _yt_feed_candidates(channel_id):
        try:
            r = get(url, headers=hdr) if hdr is not None else get(url)
            if r.status_code == 200 and "<entry" in r.text:
                if not YT_PROBE:
                    YT_PROBE.append({"성공한후보": name, "url": url.split("?")[0]})
                xml = r.text
                break
            why.append(f"{name}: HTTP {r.status_code}"
                       + ("" if r.status_code != 200 else f", entry 없음 ({len(r.text)}자)"))
        except Exception as e:
            why.append(f"{name}: {type(e).__name__} {str(e)[:40]}")
    if xml is None:
        if not YT_PROBE:
            YT_PROBE.append({"성공한후보": None, "시도": why})
        raise RuntimeError(" / ".join(why))

    soup = BeautifulSoup(xml, "xml")
    vids = []
    for e in soup.find_all("entry"):
        pub = e.find("published")
        vid = e.find("videoId")
        ttl = e.find("title")
        if not (pub and vid and ttl):
            continue
        # RSS 피드에는 설명란 전문이 들어 있다. 자막이 IP 차단으로 막혀도
        # 이건 막히지 않는다 — 공짜로 받을 수 있는 가장 긴 본문이다.
        desc = e.find("description")           # <media:description>
        vids.append({
            "id": vid.text,
            "title": ttl.text,
            "desc": clean(desc.text) if desc and desc.text else "",
            "dt": datetime.fromisoformat(pub.text.replace("Z", "+00:00")).astimezone(KST),
        })
    return vids


def _transcript_api(video_id):
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    tr = api.fetch(video_id, languages=["ko", "en"])
    snips = getattr(tr, "snippets", tr)
    return clean(" ".join(s.text for s in snips))


def _transcript_ytdlp(video_id):
    """youtube-transcript-api가 IP 차단으로 막힐 때의 대체 경로.
    yt-dlp는 다른 클라이언트로 접근해서 뚫릴 때가 있다."""
    import subprocess, tempfile, glob, os as _os
    with tempfile.TemporaryDirectory() as td:
        cmd = ["yt-dlp", "--skip-download", "--write-auto-subs", "--write-subs",
               "--sub-langs", "ko,en", "--sub-format", "vtt",
               "-o", _os.path.join(td, "%(id)s.%(ext)s"),
               f"https://www.youtube.com/watch?v={video_id}"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        files = glob.glob(_os.path.join(td, "*.vtt"))
        if not files:
            raise RuntimeError((r.stderr or "no vtt").strip().split("\n")[-1][:120])
        raw = open(files[0], encoding="utf-8", errors="replace").read()
    out, seen = [], set()
    for ln in raw.split("\n"):
        ln = ln.strip()
        if not ln or "-->" in ln or ln.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        ln = re.sub(r"<[^>]+>", "", ln)
        if ln and ln not in seen:
            seen.add(ln)
            out.append(ln)
    return clean(" ".join(out))


# ── Gemini 경유 영상 분석 ────────────────────────────────────────────
# 자막 엔드포인트는 데이터센터 IP를 막지만, Gemini에 유튜브 주소를 넘기면
# 구글이 자기 서버에서 영상을 읽는다. 차단되는 IP가 우리 쪽이 아니게 된다.
# 무료 등급은 하루 유튜브 8시간, 공개 영상만(멤버십·비공개 불가).

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip()
GEMINI_MAX = int(os.environ.get("GEMINI_MAX_VIDEOS", "12"))   # 무료 8시간/일 보호
GEMINI_BASE = "https://generativelanguage.googleapis.com"
GEMINI_LABEL = "Gemini영상분석"   # 자막과 절대 섞이면 안 되는 꼬리표

GEMINI_PROMPT = (
    "이 영상에서 투자 판단에 쓸 내용을 뽑아라. 한국어로.\n"
    "1) 다룬 종목·티커를 전부 나열\n"
    "2) 말한 숫자(목표주가, 실적, 전망치, 비중, 날짜)를 **들린 그대로** 적어라. 반올림·환산 금지\n"
    "3) 핵심 주장과 그 근거를 화자의 논리 순서대로\n"
    "4) 화자가 조심스럽게 말한 부분은 조심스럽다고 표시\n"
    "규칙: 영상에 없는 내용을 채우지 마라. 안 들리거나 불확실하면 '불명확'이라고 적어라. "
    "일반론으로 분량을 늘리지 마라."
)


def _gemini_models():
    """쓸 수 있는 모델 이름을 서버에 직접 물어본다. 모델명이 바뀌어도 따라간다."""
    r = get(f"{GEMINI_BASE}/v1beta/models?key={GEMINI_KEY}", timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"models HTTP {r.status_code}")
    names = []
    for m in (r.json().get("models") or []):
        n = (m.get("name") or "").split("/")[-1]
        if "generateContent" in (m.get("supportedGenerationMethods") or []):
            names.append(n)
    # flash 계열을 선호(무료 등급에서 가장 넉넉하다), 그다음 아무거나
    return sorted([n for n in names if "flash" in n], reverse=True) + \
           sorted([n for n in names if "flash" not in n], reverse=True)


def _gemini_call(model: str, url: str):
    """두 가지 요청 형식을 모두 시도한다. API가 바뀌는 중이라 한쪽만 믿지 않는다."""
    import json as _json
    attempts = [
        (f"{GEMINI_BASE}/v1beta/models/{model}:generateContent?key={GEMINI_KEY}",
         {"contents": [{"parts": [{"text": GEMINI_PROMPT},
                                  {"file_data": {"file_uri": url}}]}]}),
        (f"{GEMINI_BASE}/v1beta/interactions?key={GEMINI_KEY}",
         {"model": model,
          "input": [{"type": "text", "text": GEMINI_PROMPT},
                    {"type": "video", "uri": url}]}),
    ]
    why = []
    for ep, body in attempts:
        try:
            r = requests.post(ep, json=body, timeout=300,
                              headers={"Content-Type": "application/json"})
            if r.status_code != 200:
                why.append(f"{ep.split('/')[-1].split('?')[0]}: HTTP {r.status_code} "
                           f"{r.text[:100]}")
                continue
            txt = _extract_gemini_text(r.json())
            if txt and len(txt) >= 200:
                return txt
            why.append(f"{ep.split('/')[-1].split('?')[0]}: 응답 {len(txt or '')}자 — 너무 짧음")
        except Exception as e:
            why.append(f"{type(e).__name__} {str(e)[:70]}")
    raise RuntimeError(" ||| ".join(why))


def _extract_gemini_text(d):
    """응답 형식이 여러 가지라 글자가 들어 있는 곳을 전부 훑는다."""
    if not isinstance(d, dict):
        return ""
    bits = []
    for c in (d.get("candidates") or []):
        for p in ((c.get("content") or {}).get("parts") or []):
            if isinstance(p.get("text"), str):
                bits.append(p["text"])
    for o in (d.get("output") or []):
        if isinstance(o, dict) and isinstance(o.get("text"), str):
            bits.append(o["text"])
    if isinstance(d.get("text"), str):
        bits.append(d["text"])
    return clean("\n".join(bits))


def _transcript_gemini(video_id):
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY 미설정")
    url = f"https://www.youtube.com/watch?v={video_id}"
    tried = []
    cands = [GEMINI_MODEL] if GEMINI_MODEL else []
    try:
        cands += [m for m in _gemini_models() if m not in cands][:3]
    except Exception as e:
        tried.append(f"모델목록 실패: {str(e)[:60]}")
    if not cands:
        raise RuntimeError(" / ".join(tried) or "쓸 수 있는 모델 없음")
    for m in cands:
        try:
            return _gemini_call(m, url)
        except Exception as e:
            tried.append(f"{m}: {str(e)[:110]}")
    raise RuntimeError(" / ".join(tried))


def fetch_transcript(video_id: str, allow_gemini: bool = True):
    """실제 자막 → yt-dlp → Gemini 순. 전부 실패하면 사유를 합쳐서 올린다.

    앞의 둘은 화자의 말 그대로이고, Gemini는 모델이 영상을 보고 정리한 것이다.
    성질이 다르므로 어느 경로였는지를 반드시 함께 돌려주고 번들에도 표시한다.
    """
    # 라벨을 함수 이름에서 끌어오지 않고 여기 못박는다. 함수명을 바꿨을 때
    # 'Gemini가 만든 글'이 '자막'으로 조용히 둔갑하는 일을 막기 위해서다.
    fns = [("자막", _transcript_api), ("자막-ytdlp", _transcript_ytdlp)]
    if allow_gemini and GEMINI_KEY:
        fns.append((GEMINI_LABEL, _transcript_gemini))
    why = []
    for label, fn in fns:
        try:
            txt = fn(video_id)
            if txt:
                return txt, label
        except Exception as e:
            why.append(f"{label}: {type(e).__name__} {str(e)[:60]}")
    raise RuntimeError(" / ".join(why))


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


FINVIZ_CANDIDATES = [
    "https://finviz.com/api/map_perf.ashx?t=sec_all",
    "https://finviz.com/api/map_perf.ashx?t=sec",
    "https://finviz.com/api/map_perf.ashx?t=sec_all&st=d1",
    "https://finviz.com/api/map.ashx?t=sec_all",
    "https://finviz.com/map.ashx?t=sec_all",
]


def _finviz_rows(d):
    """응답에서 {티커: 등락률} 모양만 추려 낸다. 중첩 구조도 한 겹 파고든다."""
    def rows_of(obj):
        if not isinstance(obj, dict):
            return {}
        return {k: v for k, v in obj.items()
                if isinstance(k, str) and 1 <= len(k) <= 6 and k.isupper()
                and isinstance(v, (int, float)) and not isinstance(v, bool)}
    best = rows_of(d)
    if isinstance(d, dict):
        for v in d.values():
            if isinstance(v, dict):
                cand = rows_of(v)
                if len(cand) > len(best):
                    best = cand
            elif isinstance(v, list):
                for it in v:
                    cand = rows_of(it)
                    if len(cand) > len(best):
                        best = cand
    return best


def fetch_finviz_map():
    """finviz 맵의 원본 데이터. 후보 주소를 차례로 시도하고,
    전부 실패하면 각 주소가 무엇을 돌려줬는지를 예외 메시지에 담는다.
    (추측으로 고르지 말고, 다음 수정 때 이 기록을 보고 주소를 확정한다.)"""
    diag = []
    for url in FINVIZ_CANDIDATES:
        try:
            r = get(url, timeout=25)
        except Exception as e:
            diag.append(f"{url} → {type(e).__name__}")
            continue
        head = (r.text or "")[:120].replace("\n", " ")
        try:
            rows = _finviz_rows(r.json())
        except Exception:
            diag.append(f"{url} → HTTP {r.status_code}, JSON 아님: {head!r}")
            continue
        if len(rows) >= 50:
            return rows, url
        diag.append(f"{url} → HTTP {r.status_code}, 티커형 {len(rows)}개, 본문: {head!r}")
    raise RuntimeError("맵 데이터 없음 ||| " + " ||| ".join(diag))


# ─────────────────────────── 번들 ───────────────────────────

def build(session, out_path, want_transcript=True, img_dir=None):
    base, ext, now = window(session)
    checks, lines = [], []
    img_dir = Path(img_dir) if img_dir else Path(out_path).parent / "images"
    images = []
    tr_ok, tr_fail = [], []
    desc_got = desc_empty = 0      # 설명란이 실제로 얼마나 들어오는지 — 자막 대체재의 실측치
    gem_used = 0                   # Gemini로 돌린 편수 (무료 한도 보호)

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
                # 섹터 맵은 '미장 마감' 글에 이미지로 붙어 온다. 그 글을 콕 집어 점검한다.
                closers = [q for q in inw + inext if "미장 마감" in (q.get("text") or "")]
                if closers:
                    c0 = max(closers, key=lambda q: q["dt"])
                    checks.append({"source": "map/미장마감글",
                                   "status": "ok" if c0.get("photos") else "NO_PHOTO",
                                   "at": f"{c0['dt']:%m-%d %H:%M}",
                                   "photos": len(c0.get("photos") or []),
                                   "media": c0.get("media") or [],
                                   "hint": (c0.get("hint") or [])[:10]})
                else:
                    checks.append({"source": "map/미장마감글", "status": "NOT_FOUND"})
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
                # 설명란은 RSS로 공짜로 들어온다. 자막이 막혀도 이건 남는다.
                vd = (v.get("desc") or "").strip()
                if vd:
                    lines.append(f"\n설명란 ({len(vd):,}자):\n{vd[:4000]}\n")
                    desc_got += 1
                else:
                    desc_empty += 1
                if want_transcript:
                    # 무료 등급은 하루 유튜브 8시간이다. 편수를 막아 한도를 보호한다.
                    use_gem = gem_used < GEMINI_MAX
                    try:
                        txt, via = fetch_transcript(v["id"], allow_gemini=use_gem)
                        tr_ok.append(via)
                        if via == GEMINI_LABEL:
                            gem_used += 1
                            # 화자의 말 그대로가 아니다. 섞이지 않게 꼬리표를 크게 단다.
                            lines.append(
                                f"\n⚠️ Gemini 영상 분석 ({len(txt):,}자) — **자막 아님.** "
                                f"모델이 영상을 보고 정리한 것이라 표현은 모델의 것이다. "
                                f"수치는 원문 확인 전까지 '영상 언급치'로만 쓸 것:\n{txt}\n")
                        else:
                            lines.append(f"\n자막 ({len(txt):,}자, {via}):\n{txt}\n")
                    except Exception as e:
                        tr_fail.append(str(e)[:140])
                        lines.append("\n자막 확인 불가. 제목/설명만. 내용 해설을 쓰지 말 것.\n")
        except Exception as e:
            checks.append({"source": f"yt/{desc[:20]}", "status": "ERROR", "detail": str(e)[:120]})
            lines.append(f"\n### {desc} — 수집 실패: {type(e).__name__}\n")

    checks.append({"source": "youtube/피드주소", "status": "ok" if (YT_PROBE and YT_PROBE[0].get("성공한후보")) else "ALL_FAILED",
                   **(YT_PROBE[0] if YT_PROBE else {"note": "채널을 하나도 시도하지 않음"})})

    checks.append({"source": "youtube/설명란",
                   "status": "ok" if desc_got else ("NO_DESC" if desc_empty else "NO_VIDEO"),
                   "설명란있음": desc_got, "설명란빔": desc_empty})

    if want_transcript:
        real = [v for v in tr_ok if v != GEMINI_LABEL]
        checks.append({"source": "youtube/transcript",
                       "status": "ok" if tr_ok else "BLOCKED",
                       "실제자막": len(real), "Gemini분석": gem_used,
                       "failed": len(tr_fail),
                       "via": sorted(set(tr_ok)) or None,
                       "why": sorted(set(tr_fail))[:2]})
        checks.append({"source": "youtube/gemini",
                       "status": ("ok" if gem_used else
                                  ("NO_KEY" if not GEMINI_KEY else "FAILED")),
                       "돌린편수": gem_used, "한도": GEMINI_MAX,
                       "한도도달": gem_used >= GEMINI_MAX})

    # 멤버십 보충 — 사용자 PC가 올려둔 파일을 그대로 끼워 넣는다.
    # 클라우드는 멤버십 영상을 못 연다. 켜져 있던 기계가 대신 받아 둔 것이다.
    lines.append("\n\n## 멤버십 영상 보충 (사용자 PC 수집)\n")
    mp = Path("bundles/members_latest.md")
    try:
        if mp.exists():
            raw = mp.read_text(encoding="utf-8")
            m = re.search(r"수집:\s*([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2})", raw)
            age_h = None
            if m:
                got_at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=KST)
                age_h = round((now - got_at).total_seconds() / 3600, 1)
            if age_h is not None and age_h > 36:
                lines.append(f"⚠️ 이 보충은 **{age_h}시간 전** 것이다(집·사무실 PC가 그동안 꺼져 "
                             f"있었다는 뜻). 그 사이 올라온 멤버십 영상은 아직 안 들어와 있다. "
                             f"없다고 쓰지 말고 '미수집'으로 적어라.\n")
            lines.append(raw)
            checks.append({"source": "members/PC보충", "status": "ok",
                           "수집시각": m.group(1) if m else "불명",
                           "경과시간": age_h, "오래됨": bool(age_h and age_h > 36),
                           "글자수": len(raw)})
        else:
            lines.append("보충 파일 없음 — 멤버십 채널(한경 글로벌마켓)은 아직 한 번도 "
                         "수집되지 않았다. 해당 채널은 제목·링크만 있는 상태로 보고하라.\n")
            checks.append({"source": "members/PC보충", "status": "NEVER_RUN"})
    except Exception as e:
        lines.append(f"보충 읽기 실패: {type(e).__name__}\n")
        checks.append({"source": "members/PC보충", "status": "ERROR", "detail": str(e)[:120]})

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
        fv, fv_url = fetch_finviz_map()
        checks.append({"source": "finviz/map", "status": "ok",
                       "entries": len(fv), "url": fv_url})
        items = sorted(fv.items(), key=lambda kv: kv[1], reverse=True)
        lines.append(f"종목 {len(items)}개의 등락률을 받았다(그림이 아니라 수치). 주소: {fv_url}\n")
        lines.append("상승 상위 15: " + ", ".join(f"{k} {v:+.2f}%" for k, v in items[:15]))
        lines.append("\n하락 상위 15: " + ", ".join(f"{k} {v:+.2f}%" for k, v in items[-15:]))
    except Exception as e:
        checks.append({"source": "finviz/map", "status": "BLOCKED", "detail": str(e)[:400]})
        lines.append("finviz 접근 불가. 위의 섹터 ETF 표로 섹터 흐름을 읽어라. "
                     "맵을 봤다고 쓰지 마라.\n")
        lines.append("\n<진단 — 다음 수정 때 쓸 기록>\n")
        for part in str(e).split("|||"):
            lines.append(f"- {part.strip()}")

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
