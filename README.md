# PARTS-MALL Ad Automation MVP

24/7 광고 자동화 시스템 — 남아공 PARTS-MALL Boksburg + Edenvale.
모든 광고 산출물 / 랜딩 / WhatsApp 메시지는 **영어**. 개발 노트만 한국어.

## Quick start (Windows)

```cmd
REM 1. Customer landing (FastAPI, port 8000)
run_landing.bat

REM 2. Manager admin UI (Streamlit, port 8501) — 다른 터미널에서
run_admin.bat
```

처음 실행 시 `.venv` 자동 생성 + `requirements.txt` 설치 + DB 초기화 + 직원/지점 시드.

## URLs

| 용도 | URL |
|---|---|
| Region picker | `http://localhost:8000/` |
| Boksburg landing | `http://localhost:8000/boksburg` |
| Edenvale landing | `http://localhost:8000/edenvale` |
| Admin UI | `http://localhost:8501/` |
| Health check | `http://localhost:8000/healthz` |

광고 트래킹용 UTM 예시:
`http://localhost:8000/boksburg?utm_source=meta&utm_campaign=hyundai_brakes&cid=1`

## 프로젝트 구조

```
.
├── admin/admin_app.py    # 매니저용 Streamlit UI (업로드, 승인, 통계)
├── landing/              # 고객용 FastAPI (mobile-first, English only)
│   ├── main.py
│   ├── templates/
│   └── static/styles.css
├── core/
│   ├── db.py             # SQLite 스키마
│   ├── seed.py           # 지점 + 직원 6명 시드
│   └── routing.py        # 라운드로빈 + 영업시간 + wa.me URL 빌더
├── assets/logo.png       # PARTS-MALL 로고
├── uploads/              # 매니저가 올린 포스터 원본
├── generated/            # 워터마크 합성된 광고용 에셋 (Director Agent 출력)
├── data/partsmall.db     # SQLite (자동 생성)
├── requirements.txt
└── .env.example          # 복사해서 .env 로 사용
```

## 데이터 모델

- `branches` : Boksburg, Edenvale (좌표 + 광고 반경)
- `staff` : 6명 (Boksburg 4 + Edenvale 2), `last_assigned_at` 으로 라운드로빈
- `campaigns` : 매니저 업로드한 포스터 + 메타데이터
- `ad_creatives` : Director Agent 가 생성한 플랫폼별 카피 (Week 3+)
- `click_logs` : `/go/<branch>` 클릭 추적 (UTM 포함)

## 라우팅 동작

1. 광고 클릭 → `/<branch>` 랜딩 (mobile-first)
2. 차종 선택 (현대/기아/쉐보레/스즈키/쌍용 우선 표시) + 부품 입력
3. `POST /go/<branch>` → DB 에 click_log 기록 → `pick_staff()` 라운드로빈 → `wa.me` 리다이렉트
4. WhatsApp prefill (영어): "Hi PARTS-MALL. I'm looking for ..."
5. 영업시간 외에는 prefill 에 "다음 영업일 응답" 안내 추가

## 다음 단계 (Week 3+)

- [ ] Director Agent (Anthropic SDK) — 매일 아침 cron, 어제 클릭 데이터로 카피 생성
- [ ] Asset Generator — Pillow 로 포스터에 로고 + 지역 WhatsApp QR 합성
- [ ] Meta Graph API 연동 — 승인된 캠페인 자동 게시
- [ ] 배포: psms-pmaad.co.za (HTTPS), Caddy + systemd 또는 Render/Railway
- [ ] WhatsApp Business API (Phase 2, 3개월 후)
