@echo off
REM ───────────────────────────────────────────────────────────────
REM  멤버십 영상 보충 — 작업 스케줄러에 이 파일을 건다.
REM  집PC / 사무실 노트북 양쪽에 똑같이 깔아 두면, 그날 켜져 있던
REM  쪽이 알아서 따라잡는다. 둘 다 돌아도 중복은 안 생긴다
REM  (bundles/members_seen.json 이 이미 처리한 영상을 기억한다).
REM ───────────────────────────────────────────────────────────────
setlocal
cd /d "%~dp0"

echo [1/5] yt-dlp 최신화
python -m pip install -q -U yt-dlp
if errorlevel 1 echo    (업데이트 실패 — 기존 버전으로 진행)

echo [2/5] 저장소 최신 상태로
git pull --rebase --autostash
if errorlevel 1 (
  echo    git pull 실패. 충돌이 있는지 확인이 필요하다. 중단한다.
  exit /b 1
)

echo [3/5] 멤버십 영상 수집
python members.py
if errorlevel 1 (
  echo    수집 실패. 위 메시지를 확인할 것. 올리지 않고 끝낸다.
  exit /b 1
)

echo [4/5] 커밋
git add bundles/members_latest.md bundles/members_seen.json
git diff --staged --quiet
if errorlevel 1 (
  git commit -m "members: %COMPUTERNAME% %DATE% %TIME%"
) else (
  echo    바뀐 것 없음 — 커밋 생략
  goto done
)

echo [5/5] 올리기
git push
if errorlevel 1 (
  echo    push 실패. 다음 실행 때 다시 시도된다.
  exit /b 1
)

:done
echo 끝. 브리핑이 이 내용을 읽어 간다.
endlocal
