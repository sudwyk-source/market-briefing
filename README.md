# market-briefing

미국 시장 중심 하루 2회 브리핑의 **수집 단계**. 텔레그램 7개 + 유튜브 9개를 구간 필터링해
`bundles/latest.md` 하나로 만들고, insidertracking의 S&P500 섹터 맵 사진을 `bundles/images/`에
내려받는다. 브리핑 세션은 이 두 곳만 읽으면 되므로 PC 브라우저가 필요 없다.

## 설치 (한 번만)

1. GitHub에서 **private 저장소**를 만든다 (이름: `market-briefing`, 초기화 파일 없이)
2. 이 폴더의 파일 두 개를 올린다 — 웹 UI라면 **Add file → Upload files**
   - `collect.py` (루트)
   - `.github/workflows/briefing-collect.yml` (경로를 직접 타이핑해야 폴더가 생긴다)
3. **Settings → Actions → General → Workflow permissions** 에서
   **Read and write permissions** 선택 후 Save. (봇이 번들을 커밋해야 한다)
4. **Actions** 탭 → `briefing-collect` → **Run workflow** → session `22` → 실행
5. 끝나면 `bundles/latest.md` 와 `bundles/images/` 가 생겼는지 확인

## 확인할 것 — 첫 실행 로그에서

번들 맨 아래 `## 수집 점검` JSON 과 ⚠️ 목록을 본다.

| 보이는 것 | 뜻 |
|---|---|
| `tg/*` 가 전부 `ok` | 텔레그램 정상 |
| `tg/*` 가 전부 `PARSE_FAIL` | t.me HTML 구조가 바뀐 것. 종료코드 1로 워크플로가 붉게 뜬다 |
| `images/insidertracking: NO_IMAGE` | 구간 내 사진 없음. 맵 미게시이면 정상 |
| `map/미장마감글: ok` | 마감 글에 사진이 붙어 왔고 URL까지 잡혔다 — 맵 정상 |
| `map/미장마감글: NO_PHOTO` | 글은 왔는데 사진 URL을 못 뽑았다. 같은 줄의 `hint`(그 글의 class 목록)로 선택자를 고친다 |
| `map/미장마감글: NOT_FOUND` | 구간 안에 '미장 마감' 글이 아예 없다 — 수집 시각이 배치보다 이른 것 |
| 유튜브에 `자막 확인 불가` 가 많음 | GitHub IP가 유튜브 자막에서 차단된 것 (아래 참조) |

## 멤버십 영상 (한경 글로벌마켓) — `members.py`

멤버십 영상은 **돈 낸 계정으로 로그인된 세션**에서만 열린다. 클라우드에는 그 세션이 없으므로
이 한 조각만 사용자의 윈도우에서 돈다. 결과는 깃허브에 올라가고 브리핑이 읽는다.

집PC·사무실 노트북 **양쪽에 똑같이** 깔아 둔다. 그날 켜져 있던 쪽이 처리하고, 둘 다 돌아도
중복되지 않는다 (`bundles/members_seen.json` 이 이미 처리한 영상 id를 기억한다).

1. `pip install yt-dlp`
2. 쿠키 내보내기 — **이 순서를 지켜야 한다.** 유튜브는 열린 탭의 쿠키를 자주 갈아치운다
   - 크롬 **시크릿 창**에서 유튜브 로그인
   - 같은 탭에서 `https://www.youtube.com/robots.txt` 로 이동
   - 확장으로 youtube.com 쿠키를 `cookies.txt` 로 저장 (저장소 폴더에)
   - **시크릿 창을 닫는다**
3. `run_members.bat` 을 작업 스케줄러에 건다 (하루 한 번이면 충분)

**핵심 — 구간이 아니라 워터마크.** 시간 구간으로 끊으면 두 기계가 다 꺼져 있던 날이 영영
사라진다. 그래서 채널별 "마지막까지 처리한 영상 id"를 남기고 그 뒤로 올라온 것을 전부
따라잡는다. 사흘 만에 켜도 사흘치가 들어온다. **늦을 수는 있어도 빠지지는 않는다.**

자막을 못 받은 영상도 제목·링크는 번들에 남는다. 조용히 비는 일이 없어야 한다.

보충이 36시간보다 오래되면 번들 맨 위에 경고가 붙는다. 브리핑은 그걸 "없음"이 아니라
"미수집"으로 적는다.

> 유튜브는 계정 쿠키를 쓰는 자동화를 경계한다(yt-dlp 위키도 계정 정지 가능성을 경고한다).
> 멤버십 때문에 throwaway 계정을 쓸 수 없으므로, 대신 **요청량을 낮게** 유지한다 —
> 채널 1개, 실행당 8편, 요청 사이 4초. 이 숫자를 올리지 마라.

## 유튜브 자막이 막히면

YouTube는 **데이터센터 IP**의 자막 요청을 막는다. GitHub Actions 러너가 그 대역이다.
로그인해도 소용없다 — IP를 보고 자른다. 코드로 우회할 수 있는 종류의 벽이 아니다.

그래서 공개 영상은 **Gemini**로 돈다. 유튜브 주소를 Gemini에 넘기면 구글이 **자기 서버에서**
영상을 읽으므로, 유튜브에 붙는 쪽이 우리 IP가 아니게 된다. 벽을 뚫는 게 아니라 벽이 없는
쪽으로 도는 것이다.

- 키 발급: [aistudio.google.com](https://aistudio.google.com) → Get API key (카드 등록 없음)
- 등록: 저장소 Settings → Secrets and variables → Actions → `GEMINI_API_KEY`
- 무료 등급은 **하루 유튜브 8시간**. 회차당 12편으로 상한을 걸어 뒀다 (`GEMINI_MAX_VIDEOS`)
- 키가 없으면 Gemini 경로를 **아예 타지 않는다.** 제목·설명란만으로 간다

**Gemini 결과는 자막이 아니다.** 모델이 영상을 보고 쓴 글이라 AI가 한 겹 더 낀다. 그래서
`⚠️ Gemini 영상 분석 — 자막 아님` 꼬리표가 크게 붙고, 실제 자막이 되는 영상은 Gemini를
**호출조차 하지 않는다**. 말 그대로가 항상 우선이다. 점검 JSON에 `실제자막 N / Gemini분석 N`이
따로 찍힌다.

꼬리표를 함수 이름에서 끌어오게 짰다가 테스트에서 걸렸다 — 함수명만 바꿔도 모델이 쓴 글이
"자막"으로 조용히 둔갑하는 구조였다. 지금은 `GEMINI_LABEL` 상수로 못박혀 있다.

자막을 아예 끄려면 `python collect.py` 줄 끝에 `--no-transcript` 를 붙인다.

## 수동 실행

```bash
pip install requests beautifulsoup4 lxml youtube-transcript-api yt-dlp
python collect.py --session 0530 --out bundles/latest.md --images bundles/images
python collect.py --session 22    --out bundles/latest.md --images bundles/images
python collect.py --session 22 --no-transcript    # 자막 생략(빠름)
python collect.py --session 22 --quotes           # yfinance 시세 추가(기본 off)
```

## 스케줄

| 회차 | 수집 (UTC cron) | 수집 (KST) | 브리핑 (KST) |
|---|---|---|---|
| 0530 | `40 20` / `40 21` | 05:40 / 06:40 | 06:10 (겨울 07:10) |
| 22 | `20 12` / `20 13` | 21:20 / 22:20 | 21:49 (겨울 22:49) |

서머타임 양쪽 시각을 모두 걸어뒀다. 철이 바뀌어도 한쪽은 브리핑 직전에 돌고,
다른 한쪽은 그냥 한 번 더 도는 것이라 손댈 필요가 없다.

**05:40인 이유.** 인사이더는 마감 직후 05:00~05:30에 하루치를 몰아 올린다
(05:12 증시 브리핑, 05:17 섹터별 소식, 05:21 서학개미, 05:23 섹터 맵 이미지).
처음엔 05:00에 수집했는데, 그건 글이 올라오기 **전**이었다. 그래서 10-01 번들에는
05:00 '미장 마감' 글만 들어오고 05:23 맵은 아예 구간 밖이었다. 배치가 끝난 뒤로 옮겼다.

## 설계 메모 — 지키는 이유

**t.me/s/ 는 최신 글이 맨 아래에 있다.** 페이지 앞부분만 읽으면 "신규 없음"이라는 거짓 결론이
나온다. 실제로 그 오류로 하루 48건을 통째로 놓친 적이 있다. 그래서 `parse_telegram`은 DOM의
모든 메시지를 뽑아 **시각으로 정렬**하고, 구간 시작점보다 과거에 닿을 때까지 `?before=`로
거슬러 올라간다.

`parsed_any`는 "구간 내 0건(정상)"과 "한 건도 파싱 못 함(파서 깨짐)"을 구분한다. 후자만
`PARSE_FAIL`로 찍고, 7채널 전부 실패하면 종료코드 1을 내 워크플로가 실패로 표시된다.
조용히 비는 일이 없게 하는 장치다.

**사진만 있고 본문이 없는 게시물도 버리지 않는다.** 섹터 맵이 정확히 그런 형태로 올라온다.
