# Reference materials for Director Agent

이 폴더에 떨궈놓는 모든 자료가 광고 카피 생성에 자동으로 반영됨.
파일 추가/삭제 후 `git push` 만 하면 서버에 반영.

## 폴더 구조

```
reference/
├── README.md           ← 이 파일
├── brand_voice.md      ← 핵심: 우리 브랜드 톤/문구 모음 (꼭 채울 것)
├── company_info.md     ← 회사 소개, 강점, 차별점
├── notes.md            ← 자유 메모 (있으면 좋음)
└── images/             ← 과거 광고/포스터 이미지 (Claude Vision 으로 학습)
    ├── facebook_ad_1.png
    ├── facebook_ad_2.png
    └── poster_2025_brake.jpg
```

## 어떤 형식이든 OK

- `.md`, `.txt` — 텍스트로 통째로 프롬프트에 들어감 (Director Agent system context)
- `.png`, `.jpg`, `.jpeg`, `.webp` — 최대 3장까지 Claude Vision 으로 분석 (브랜드 톤/색상/시각 스타일 학습)
- `.pdf` — 향후 지원 예정 (지금은 텍스트로 변환해서 .md 로 저장 권장)

## 채워야 할 최소 분량

1. **`brand_voice.md`** — 과거 페이스북 글 5~10개 복사 붙여넣기
   - https://www.facebook.com/PartsMallSA/ 에서 잘 됐던 포스트 복사
   - 직원이 손님한테 보낸 잘 써진 메시지 예시
2. **`company_info.md`** — 우리 강점/차별점
   - 보유 재고, 한국어 기반 전문성, 가격대, 배송, 결제 방식
3. **`images/`** — 페이스북 광고 스크린샷 3~5장
   - 잘 나간 광고 위주

## Director Agent 가 사용하는 방식

매니저가 어드민에서 "Generate Ad Copy" 클릭하면:

1. `reference/` 의 모든 텍스트 + 이미지 로드
2. 캠페인 정보 (차종, 지점, 매니저 메모) 결합
3. 최근 7일 클릭 데이터 분석 (어떤 차종/부품이 잘 먹히는지)
4. 최근 30일 이미 만든 광고 카피 (반복 방지)
5. Claude Sonnet 한테 5개 영어 카피 variant 요청:
   - Workshop B2B (account/stock 중심)
   - Driver trust (genuine parts)
   - Driver urgency (in stock now)
   - Workshop value (fair pricing)
   - Universal (local + WhatsApp)
6. 매니저가 어드민에서 [Approve / Reject / Edit] → 승인된 것만 광고 사용

## 매일 아침 cron (06:00 SAST)

서버에서 자동 실행:
- 활성 캠페인 중 approved 카피가 7일 이상 된 것 → 새로 생성
- 어제 클릭 데이터 반영 → 잘 먹힌 angle 위주로 다시 만들어
- 매니저는 어드민에서 새 carrier 들 검토만 하면 됨

## 비용

- 카피 생성 1번: 약 $0.01 (Sonnet)
- 매일 자동 5개 variant × 2 캠페인 = $0.10/일 = $3/월 ≈ R55/월
- OCR (license disc): 따로, 클릭당 약 $0.001
