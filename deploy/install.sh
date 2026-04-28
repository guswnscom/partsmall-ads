#!/usr/bin/env bash
# One-shot installer for the PARTS-MALL ad system on a fresh Ubuntu 22.04/24.04 VPS.
#
# What it does:
#   1. Creates non-root 'partsmall' user
#   2. Clones (or pulls) the repo into /opt/partsmall
#   3. Builds a Python venv + installs requirements
#   4. Copies systemd unit files and starts services
#   5. Installs Caddy config (auto HTTPS) and reloads Caddy
#   6. Sets up daily SQLite + uploads backup cron
#   7. Configures ufw firewall (22, 80, 443)
#
# Run as root on the server:
#   curl -fsSL https://raw.githubusercontent.com/<YOUR_GH>/<REPO>/main/deploy/install.sh | bash -s -- <YOUR_GH>/<REPO>
#
# Or after manually cloning:
#   cd /opt/partsmall && bash deploy/install.sh

set -euo pipefail

REPO_SLUG="${1:-}"  # e.g. "junlee/partsmall-ads" — only needed on first install
APP_USER="partsmall"
APP_DIR="/opt/partsmall"

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "This script must be run as root." >&2
        exit 1
    fi
}

ensure_user() {
    if ! id -u "${APP_USER}" >/dev/null 2>&1; then
        echo "==> Creating user ${APP_USER}"
        useradd --system --create-home --shell /bin/bash "${APP_USER}"
    fi
}

clone_or_pull() {
    if [[ -d "${APP_DIR}/.git" ]]; then
        echo "==> Pulling latest code"
        chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
        sudo -u "${APP_USER}" git -C "${APP_DIR}" pull --ff-only
    else
        if [[ -z "${REPO_SLUG}" ]]; then
            echo "ERROR: First-time install requires REPO_SLUG argument (e.g. junlee/partsmall-ads)" >&2
            exit 1
        fi
        echo "==> Cloning https://github.com/${REPO_SLUG}.git"
        # Ensure target dir exists, is empty, and owned by partsmall before git clone
        mkdir -p "${APP_DIR}"
        # If a previous failed run left files, clean them
        if [[ -n "$(ls -A "${APP_DIR}" 2>/dev/null)" ]]; then
            echo "  (cleaning previous partial install in ${APP_DIR})"
            rm -rf "${APP_DIR}"
            mkdir -p "${APP_DIR}"
        fi
        chown "${APP_USER}:${APP_USER}" "${APP_DIR}"
        sudo -u "${APP_USER}" git clone "https://github.com/${REPO_SLUG}.git" "${APP_DIR}"
    fi
    chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
}

setup_venv() {
    echo "==> Setting up Python venv"
    sudo -u "${APP_USER}" bash -c "
        cd '${APP_DIR}' && \
        python3 -m venv .venv && \
        .venv/bin/pip install --upgrade pip setuptools wheel && \
        .venv/bin/pip install -r requirements.txt
    "
}

seed_db() {
    echo "==> Seeding branches + staff (idempotent)"
    sudo -u "${APP_USER}" bash -c "
        cd '${APP_DIR}' && \
        DB_PATH='${APP_DIR}/data/partsmall.db' .venv/bin/python -m core.seed
    " || echo "  (seed step warned — check above; continuing)"
}

ensure_data_dirs() {
    echo "==> Ensuring data + uploads directories"
    sudo -u "${APP_USER}" mkdir -p \
        "${APP_DIR}/data" \
        "${APP_DIR}/uploads/customer" \
        "${APP_DIR}/generated" \
        "${APP_DIR}/backups"
}

ensure_env() {
    if [[ ! -f "${APP_DIR}/.env" ]]; then
        echo "==> Creating template .env (FILL IN VALUES BEFORE STARTING SERVICES)"
        cp "${APP_DIR}/production.env.example" "${APP_DIR}/.env"
        chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
        chmod 600 "${APP_DIR}/.env"
        echo ""
        echo "  >>> Edit ${APP_DIR}/.env now. Required: ANTHROPIC_API_KEY, ADMIN_PASSWORD."
        echo ""
    fi
}

install_systemd() {
    echo "==> Installing systemd unit files"
    cp "${APP_DIR}/deploy/partsmall-landing.service" /etc/systemd/system/
    cp "${APP_DIR}/deploy/partsmall-admin.service" /etc/systemd/system/
    cp "${APP_DIR}/deploy/director-daily.service" /etc/systemd/system/
    cp "${APP_DIR}/deploy/director-daily.timer" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable partsmall-landing partsmall-admin director-daily.timer
    systemctl start director-daily.timer
}

install_caddy() {
    echo "==> Installing Caddyfile"
    # Don't overwrite an already-customized Caddyfile (e.g. one with a real admin hash).
    # Only deploy the template if /etc/caddy/Caddyfile is missing OR still has the placeholder.
    if [[ ! -f /etc/caddy/Caddyfile ]] || grep -q "REPLACE_ME_WITH_REAL_HASH" /etc/caddy/Caddyfile; then
        cp "${APP_DIR}/deploy/Caddyfile" /etc/caddy/Caddyfile
        echo "  (deployed fresh Caddyfile from template)"
    else
        echo "  (kept existing Caddyfile — it has a real admin hash)"
    fi

    if grep -q "REPLACE_ME_WITH_REAL_HASH" /etc/caddy/Caddyfile; then
        echo ""
        echo "  >>> Caddyfile still has placeholder admin password hash."
        echo "      Set it with the one-liner below:"
        echo "        read -s -p 'Admin pw: ' PW; echo"
        echo "        export HASH=\$(caddy hash-password --plaintext \"\$PW\"); unset PW"
        echo "        perl -pi -e 's|\\\$2a\\\$14\\\$REPLACE_ME_WITH_REAL_HASH|\$ENV{HASH}|g' /etc/caddy/Caddyfile"
        echo "        unset HASH && systemctl reload caddy"
        echo ""
    fi
}

setup_firewall() {
    echo "==> Configuring ufw firewall"
    ufw allow OpenSSH || true
    ufw allow 80/tcp || true
    ufw allow 443/tcp || true
    yes | ufw enable || true
}

setup_backups() {
    echo "==> Installing daily backup cron"
    cp "${APP_DIR}/deploy/backup.sh" /etc/cron.daily/partsmall-backup
    chmod 755 /etc/cron.daily/partsmall-backup
    apt-get install -y sqlite3
}

restart_services() {
    echo "==> Starting/restarting services"
    systemctl restart partsmall-landing
    systemctl restart partsmall-admin
    systemctl reload caddy || systemctl restart caddy
}

main() {
    require_root
    ensure_user
    clone_or_pull
    ensure_data_dirs
    setup_venv
    ensure_env
    seed_db
    install_systemd
    install_caddy
    setup_firewall
    setup_backups

    if [[ -f "${APP_DIR}/.env" ]] && grep -q "ANTHROPIC_API_KEY=$" "${APP_DIR}/.env"; then
        echo ""
        echo "  ⚠  .env still has empty ANTHROPIC_API_KEY. Edit it, then run:"
        echo "        systemctl restart partsmall-landing partsmall-admin"
    else
        restart_services
    fi

    echo ""
    echo "============================================================"
    echo "Install complete."
    echo ""
    echo "Check service status:"
    echo "  systemctl status partsmall-landing"
    echo "  systemctl status partsmall-admin"
    echo "  systemctl status caddy"
    echo ""
    echo "Tail logs:"
    echo "  journalctl -u partsmall-landing -f"
    echo "  journalctl -u partsmall-admin -f"
    echo "  tail -f /var/log/caddy/landing.log"
    echo ""
    echo "Visit: https://psms-pmaad.co.za"
    echo "============================================================"
}

main "$@"
