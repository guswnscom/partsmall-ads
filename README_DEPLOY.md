# Production deployment — psms-pmaad.co.za

남아공 Hetzner VPS (Ubuntu 24.04) 에 배포. 한 번 셋업하면 24시간 무인 가동.

## 0. 사전 준비 (이미 완료)

- ✅ Hetzner CPX22 서버 (`178.104.82.36`)
- ✅ 도메인 `psms-pmaad.co.za` 보유 (domains.co.za)
- ✅ DNS A 레코드 `@` + `www` → 서버 IP
- ⏳ DNS A 레코드 `admin` → 서버 IP (admin UI 용, 추가 필요)

## 1. GitHub repo 만들기

```bash
# 로컬 (Windows CMD or Git Bash)
cd "C:\Users\Jun Lee\Desktop\Parts-Mall Boksburg Ad development"
git init
git add .
git commit -m "Initial commit: PARTS-MALL ad system MVP"

# GitHub 에서 새 repo 생성 (예: junlee/partsmall-ads, Public 또는 Private)
git remote add origin https://github.com/<YOUR_USERNAME>/<REPO>.git
git branch -M main
git push -u origin main
```

⚠️ Private repo 로 만들면 서버에서 clone 할 때 deploy key 필요. 처음엔 Public 으로 시작 (`.env` 는 절대 commit 안 됨, `.gitignore` 가 막음).

## 2. 서버에서 install.sh 실행

SSH 로 서버 접속 후 (root):

```bash
# 첫 설치
bash <(curl -fsSL https://raw.githubusercontent.com/<YOUR_USERNAME>/<REPO>/main/deploy/install.sh) <YOUR_USERNAME>/<REPO>
```

스크립트가 자동으로:
- `partsmall` 유저 생성
- `/opt/partsmall` 에 코드 clone
- Python venv + requirements 설치
- systemd 서비스 등록 + 시작
- Caddy 설정 (자동 HTTPS)
- 방화벽 (ufw) 셋업 — 22, 80, 443 만 오픈
- 매일 백업 cron 등록

## 3. .env 채우기 (중요)

설치 후 `/opt/partsmall/.env` 가 템플릿으로 생성돼 있어. 편집:

```bash
nano /opt/partsmall/.env
```

채울 값:
```
ANTHROPIC_API_KEY=sk-ant-...
ADMIN_PASSWORD=<강한 패스워드 16자+>
```

저장 후:
```bash
systemctl restart partsmall-landing partsmall-admin
```

## 4. Admin Basic Auth 해시 만들기

`https://admin.psms-pmaad.co.za` 의 Basic Auth 비번 설정:

```bash
# 해시 생성
caddy hash-password
# Enter password: <매니저용 비번>
# Confirm password: <같은 거>
# 출력: $2a$14$abc123xyz...

# Caddyfile 의 REPLACE_ME_WITH_REAL_HASH 라인을 위 해시로 교체
nano /etc/caddy/Caddyfile

# 적용
systemctl reload caddy
```

## 5. 동작 확인

```bash
# 서비스 상태
systemctl status partsmall-landing partsmall-admin caddy

# 실시간 로그
journalctl -u partsmall-landing -f

# Caddy 로그
tail -f /var/log/caddy/landing.log
```

브라우저에서:
- https://psms-pmaad.co.za → 지점 선택 페이지
- https://psms-pmaad.co.za/boksburg → Boksburg 랜딩
- https://psms-pmaad.co.za/healthz → `{"ok":true}`
- https://admin.psms-pmaad.co.za → Basic Auth → 어드민 (manager / <위 패스워드>)

## 6. 코드 업데이트 (이후 배포)

로컬에서 변경 → push:
```bash
git add . && git commit -m "Fix XYZ" && git push
```

서버에서:
```bash
cd /opt/partsmall && bash deploy/install.sh
```

또는 deploy_update 단축:
```bash
sudo -u partsmall git -C /opt/partsmall pull \
  && /opt/partsmall/.venv/bin/pip install -r /opt/partsmall/requirements.txt \
  && systemctl restart partsmall-landing partsmall-admin
```

## 7. 백업 확인

매일 새벽에 자동 실행. 수동 확인:
```bash
ls -lah /opt/partsmall/backups/
# partsmall-20260428-030000.tar.gz 같은 파일들
```

복원:
```bash
cd /tmp && tar -xzf /opt/partsmall/backups/partsmall-XXXXX.tar.gz
# db/partsmall.db 와 uploads/, generated/ 가 추출됨
```

## 트러블슈팅

| 증상 | 확인 |
|---|---|
| HTTPS 안 열림 (cert 에러) | `journalctl -u caddy -n 50` — DNS 가 IP 안 가리키면 Let's Encrypt 발급 실패 |
| 502 Bad Gateway | landing/admin 서비스 다운 — `systemctl status partsmall-landing` |
| 서비스 자꾸 죽음 | `journalctl -u partsmall-landing -n 100` 에서 에러 확인 (대부분 .env 누락) |
| 사진 업로드 안 됨 | `/opt/partsmall/uploads/customer` 폴더 권한 — `chown -R partsmall:partsmall /opt/partsmall/uploads` |
| OCR 항상 fail | `.env` 의 `ANTHROPIC_API_KEY` 확인, `journalctl -u partsmall-landing` 에 "Vision API call failed" 메시지 |
