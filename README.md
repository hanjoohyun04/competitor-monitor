# 다이얼독 경쟁사 주간 스냅샷 스크립트

매주 한 번 돌리면 App Store · Google Play · 홈페이지를 긁어 **직전 주와 비교한 변동만** Markdown 리포트로 만듭니다.
리포트(`reports/YYYY-MM-DD_diff.md`)를 그대로 채팅에 붙여넣거나 올려주시면 제가 모니터링 문서 3종을 갱신합니다.

## 구성
- `competitor_monitor.py` — 수집·비교·리포트 (Python 3.9+, 표준 라이브러리만으로 iOS·홈페이지 수집)
- `apps.json` — 추적 대상. 앱 16개(가나다순), 홈페이지 12개. 신규 경쟁사 편입 시 여기에 한 줄 추가
- `snapshots/` — 매주 원본 스냅샷 JSON (소개문 전문·버전 기록·홈페이지 본문 포함 → 나중에 "언제 바뀌었나" 추적 가능)
- `reports/` — 주간 diff 리포트

## 설치·첫 실행 (5분)
```bash
pip install google-play-scraper        # Google Play용. 없으면 iOS·홈페이지만 수집됩니다
python competitor_monitor.py --init    # 기준 스냅샷 저장 + 전체 인벤토리 출력
```
`--init`은 처음 한 번만. 이후 매주:
```bash
python competitor_monitor.py           # 수집 → snapshots/ 저장 → 직전 스냅샷과 diff → reports/ 저장 + 화면 출력
```
설정 파일을 수정했으면 `--config apps.json`, 데이터를 다른 폴더에 두려면 `--dir D:\dialodog\competitors` 처럼.

## 리포트 읽는 법
- 🔴 = 버전·소개문·앱명 변경(중요) / 표시 없음 = 평점·설치·크기 등 수치 변동 / 🟠 = 홈페이지 본문 변경(바뀐 줄만 표시)
- "변동 없음"에 든 앱은 그 주 체크 완료. 조회 실패는 따로 표시됩니다.
- iOS **버전 기록**(최근 25개 안팎)은 스토어 페이지에서 best-effort로 파싱합니다. Apple이 페이지 구조를 바꾸면 이 항목만 비고 Lookup API(현재 버전·배포일·노트·평점·소개문)는 그대로 돕니다.

## 자동 실행
- **Windows 작업 스케줄러**: 매주 일요일 08:00 → `python C:\...\competitor_monitor.py --config C:\...\apps.json --dir C:\...\data`
- **macOS/Linux cron**: `0 8 * * 0 cd ~/competitor-monitor && python3 competitor_monitor.py > /dev/null`
- **Slack 알림**: 환경변수 `SLACK_WEBHOOK`에 Incoming Webhook URL을 넣으면 "변동 요약" 부분만 채널로 보냅니다.

## AWS Lambda로 돌리려면 (선택)
1. `pip install google-play-scraper -t package/` → `package/`에 `competitor_monitor.py`, `apps.json` 복사 → zip
2. Lambda(Python 3.12, 메모리 512MB, 타임아웃 3분) 생성, 핸들러 `competitor_monitor.lambda_handler`
3. 환경변수 `S3_BUCKET`(스냅샷·리포트 보관), `CONFIG_PATH=/var/task/apps.json`, 선택 `SLACK_WEBHOOK`
4. 실행 역할에 해당 버킷 `s3:GetObject/PutObject/ListBucket`
5. EventBridge 규칙 `cron(0 23 ? * SAT *)` (UTC 토 23:00 = **일요일 08:00 KST**). 첫 회는 테스트 이벤트 `{"init": true}`로 실행
리포트는 `s3://버킷/reports/YYYY-MM-DD_diff.md` — 매주 이 파일을 올려주시면 됩니다.

## 알아둘 것
- **Google Play 패키지명 미확인**: 그루머노트, 멍투데이(2종), 반비서, 펫차트, 다이얼독(비즈/회원용). 폰의 Play 스토어에서 앱 페이지 → 공유 → 링크의 `id=` 뒤 값을 `apps.json`의 `"gp"`에 넣어주세요. 비어 있으면 iOS만 추적합니다.
- 우피(teepee.oopy.io)·노션처럼 자바스크립트로만 그려지는 페이지는 본문이 거의 안 잡힙니다. 변동 감지는 되지만 내용은 브라우저에서 확인해야 합니다.
- Lookup API는 스토어 반영보다 몇 시간 늦을 수 있고, 평점·리뷰 수는 한국 스토어 기준입니다.
- 스냅샷에는 소개문 전문과 홈페이지 본문이 들어가므로 한 주에 1~3MB 정도 쌓입니다. 1년 치도 문제없는 크기입니다.
- 주간 결과를 저에게 줄 때는 `reports/…_diff.md` 하나면 충분합니다. 특정 앱의 과거 추적이 필요하면 해당 주 `snapshots/…json`을 같이 주세요.
