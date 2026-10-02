#!/usr/bin/env python3
"""멤버십 전용 영상 보충 — 집PC / 사무실 노트북 중 켜져 있는 쪽에서 돈다.

왜 따로 있나
------------
멤버십 영상은 "돈 낸 계정으로 로그인된 세션"에서만 열린다. 클라우드(GitHub
Actions)는 로그인 세션이 없고, 유튜브는 데이터센터 IP의 자막 요청을 막는다.
그래서 이 한 조각만 사용자의 윈도우에서 돌린다. 결과는 깃허브에 올리고,
클라우드 브리핑이 그걸 읽는다. 어느 기계에서 돌았는지는 상관없다.

핵심 설계 — 시간 구간이 아니라 '마지막으로 본 번호'
---------------------------------------------------
구간으로 끊으면 두 기계가 다 꺼져 있던 날의 영상이 영영 사라진다.
그래서 채널별로 마지막까지 처리한 영상 id를 bundles/members_seen.json 에
남기고, 다음 실행 때 그 뒤로 올라온 것을 전부 따라잡는다. 사흘 만에 켜도
사흘치가 들어온다. 늦을 수는 있어도 빠지지는 않는다.

쓰는 법 (양쪽 기계에 똑같이)
---------------------------
  1) pip install yt-dlp
  2) 쿠키 내보내기 — 반드시 아래 순서로 (유튜브가 쿠키를 자주 갈아치운다)
     · 크롬 시크릿 창에서 유튜브 로그인
     · 같은 탭에서 https://www.youtube.com/robots.txt 로 이동
     · 확장으로 youtube.com 쿠키를 cookies.txt 로 저장
     · 시크릿 창을 닫는다  ← 이걸 해야 세션이 안 갈린다
  3) 이 저장소를 clone 한 폴더에 cookies.txt 를 두고
     python members.py
  4) 작업 스케줄러에 run_members.bat 을 건다 (하루 한 번이면 충분)

주의: 유튜브는 계정 쿠키를 쓰는 자동화를 경계한다. 그래서 이 스크립트는
채널 하나·한 번에 몇 편만 건드리고 사이에 쉰다. 요청을 늘리지 마라.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))

# 멤버십이 걸린 채널만 넣는다. 공개 영상은 클라우드가 이미 가져온다.
MEMBER_CHANNELS = [
    ("UCWskYkV4c4S9D__rsfOl2JA", "한경 글로벌마켓"),
]

ROOT = Path(__file__).resolve().parent
COOKIES = Path(os.environ.get("YT_COOKIES", ROOT / "cookies.txt"))
SEEN_PATH = ROOT / "bundles" / "members_seen.json"
OUT_PATH = ROOT / "bundles" / "members_latest.md"

LIST_LIMIT = int(os.environ.get("MEMBERS_LIST_LIMIT", "30"))   # 목록에서 훑을 편수
FETCH_LIMIT = int(os.environ.get("MEMBERS_FETCH_LIMIT", "8"))  # 한 번에 자막 받을 편수
KEEP_DAYS = int(os.environ.get("MEMBERS_KEEP_DAYS", "7"))      # 번들에 남겨둘 기간
SLEEP_SEC = 4                                                  # 요청 사이 간격

# availability 가 이것 중 하나면 '로그인 필요'로 본다.
RESTRICTED = {"subscriber_only", "premium_only", "needs_auth", "unlisted", "private"}


def log(msg):
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


def ytdlp(args, timeout=180):
    cmd = ["yt-dlp"]
    if COOKIES.exists():
        cmd += ["--cookies", str(COOKIES)]
    cmd += args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    return r


def check_ready():
    problems = []
    try:
        r = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            problems.append("yt-dlp 실행 실패 — pip install -U yt-dlp")
        else:
            log(f"yt-dlp {r.stdout.strip()}")
    except FileNotFoundError:
        problems.append("yt-dlp 없음 — pip install -U yt-dlp")
    except Exception as e:
        problems.append(f"yt-dlp 확인 불가: {e}")
    if not COOKIES.exists():
        problems.append(f"쿠키 파일 없음: {COOKIES} — 멤버십 영상은 못 연다")
    return problems


def load_seen():
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_seen(seen):
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")


def list_videos(channel_id):
    """로그인 상태로 채널 목록을 읽는다. 멤버십 영상도 여기 나온다."""
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    r = ytdlp(["--flat-playlist", "-J", "--playlist-end", str(LIST_LIMIT), url], timeout=180)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError((r.stderr or "목록 비었음").strip().split("\n")[-1][:200])
    data = json.loads(r.stdout)
    out = []
    for e in (data.get("entries") or []):
        if not e or not e.get("id"):
            continue
        out.append({
            "id": e["id"],
            "title": e.get("title") or "(제목 없음)",
            "availability": e.get("availability"),
            "ts": e.get("timestamp"),
        })
    return out


def vtt_to_text(raw):
    out, seen = [], set()
    for ln in raw.split("\n"):
        ln = ln.strip()
        if not ln or "-->" in ln or ln.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        ln = re.sub(r"<[^>]+>", "", ln).strip()
        if ln and ln not in seen:
            seen.add(ln)
            out.append(ln)
    return re.sub(r"[ \t]+", " ", " ".join(out)).strip()


def fetch_subs(video_id, workdir: Path):
    workdir.mkdir(parents=True, exist_ok=True)
    for f in workdir.glob("*.vtt"):
        try:
            f.unlink()
        except OSError:
            pass
    r = ytdlp(["--skip-download", "--write-auto-subs", "--write-subs",
               "--sub-langs", "ko,en", "--sub-format", "vtt",
               "-o", str(workdir / "%(id)s.%(ext)s"),
               f"https://www.youtube.com/watch?v={video_id}"], timeout=240)
    files = sorted(workdir.glob("*.vtt"))
    if not files:
        raise RuntimeError((r.stderr or "자막 파일 없음").strip().split("\n")[-1][:200])
    txt = vtt_to_text(files[0].read_text(encoding="utf-8", errors="replace"))
    if len(txt) < 100:
        raise RuntimeError(f"자막이 {len(txt)}자뿐 — 받은 걸로 치지 않는다")
    return txt


def existing_blocks():
    """이전 실행이 써 둔 영상 블록을 날짜로 걸러 재사용한다."""
    if not OUT_PATH.exists():
        return []
    txt = OUT_PATH.read_text(encoding="utf-8")
    blocks = [b for b in txt.split("\n<!--VID-->\n") if "<!--DT:" in b]
    cutoff = datetime.now(KST) - timedelta(days=KEEP_DAYS)
    keep = []
    for b in blocks:
        m = re.search(r"<!--DT:([0-9T:+\-]+)-->", b)
        if not m:
            continue
        try:
            if datetime.fromisoformat(m.group(1)) >= cutoff:
                keep.append(b)
        except ValueError:
            continue
    return keep


def main():
    problems = check_ready()
    for p in problems:
        log("문제: " + p)
    if any("yt-dlp" in p for p in problems):
        log("중단 — yt-dlp 없이는 아무것도 못 한다")
        return 1

    seen = load_seen()
    blocks = existing_blocks()
    report = []
    workdir = ROOT / ".members_tmp"

    for cid, name in MEMBER_CHANNELS:
        st = seen.setdefault(cid, {"done": [], "last_run": None})
        done = set(st.get("done") or [])
        try:
            vids = list_videos(cid)
        except Exception as e:
            log(f"{name}: 목록 실패 — {e}")
            report.append({"channel": name, "status": "LIST_FAIL", "why": str(e)[:160]})
            continue

        # 아직 처리 안 한 것만. 시간이 아니라 id 기준이라 며칠 밀려도 따라잡는다.
        fresh = [v for v in vids if v["id"] not in done]
        restricted = [v for v in fresh
                      if (v["availability"] in RESTRICTED) or (v["availability"] is None)]
        log(f"{name}: 목록 {len(vids)}편 / 새 글 {len(fresh)}편 / 로그인필요·불명 {len(restricted)}편")

        got = fail = 0
        for v in restricted[:FETCH_LIMIT]:
            try:
                txt = fetch_subs(v["id"], workdir)
                got += 1
                avail = v["availability"] or "불명"
                blocks.append(
                    f"<!--DT:{datetime.now(KST).isoformat(timespec='seconds')}-->\n"
                    f"### [{name}] {v['title']}\n"
                    f"https://www.youtube.com/watch?v={v['id']}  (공개범위: {avail})\n\n"
                    f"자막 ({len(txt):,}자):\n{txt}\n")
                done.add(v["id"])
                log(f"  + {v['title'][:40]} — 자막 {len(txt):,}자")
            except Exception as e:
                fail += 1
                log(f"  ! {v['title'][:40]} — {e}")
                # 실패한 건 done 에 넣지 않는다. 다음 실행에서 다시 시도한다.
            time.sleep(SLEEP_SEC)

        # 자막을 못 받았어도 '있었다'는 사실은 남긴다 — 조용히 비는 일이 없게.
        skipped = restricted[FETCH_LIMIT:]
        for v in skipped:
            blocks.append(
                f"<!--DT:{datetime.now(KST).isoformat(timespec='seconds')}-->\n"
                f"### [{name}] {v['title']}  — 이번 실행 한도 초과, 다음 실행에서 받는다\n"
                f"https://www.youtube.com/watch?v={v['id']}\n")

        st["done"] = sorted(done)[-400:]
        st["last_run"] = datetime.now(KST).isoformat(timespec="seconds")
        report.append({"channel": name, "status": "ok", "목록": len(vids),
                       "새글": len(fresh), "자막받음": got, "실패": fail,
                       "미처리": len(skipped)})

    save_seen(seen)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    head = (f"# 멤버십 영상 보충\n"
            f"수집: {datetime.now(KST):%Y-%m-%d %H:%M} KST  (기계: {os.environ.get('COMPUTERNAME', '?')})\n"
            f"최근 {KEEP_DAYS}일치를 남긴다. 구간이 아니라 '마지막 처리 영상' 기준이라, "
            f"며칠 꺼져 있었어도 그사이 것이 따라온다.\n\n"
            f"```json\n{json.dumps(report, ensure_ascii=False, indent=1)}\n```\n")
    OUT_PATH.write_text(head + "\n<!--VID-->\n".join(blocks) + "\n", encoding="utf-8")
    log(f"기록: {OUT_PATH}  (영상 블록 {len(blocks)}개)")

    try:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
