#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Изолированная установка «Гоголь барбершоп» рядом с другим сайтом.
#
# Запускается на сервере от root:   bash /opt/gogol/deploy/bootstrap.sh
# Полное удаление (ничего чужого не трогает):
#                                   bash /opt/gogol/deploy/bootstrap.sh --remove
#
# Что делает и чего НЕ делает:
#   • ставит приложение в свой каталог, свой venv, свою базу и свой сервис;
#   • слушает ТОЛЬКО 127.0.0.1 — наружу порт не открывается, второй сайт,
#     его nginx и его база не затрагиваются;
#   • НЕ ставит и НЕ настраивает Postgres и nginx — если Postgres не найден,
#     скрипт честно остановится, а не начнёт перекраивать систему.
# ---------------------------------------------------------------------------
set -euo pipefail

# ----- настройки (можно переопределить через окружение при запуске) --------
PORT="${PORT:-8790}"                       # порт приложения; ДОЛЖЕН быть свободен
BIND="${BIND:-127.0.0.1}"                  # 127.0.0.1 = приватно (по умолчанию)
SERVICE="${SERVICE:-gogol}"                # имя systemd-сервиса
APP_USER="${APP_USER:-gogol}"              # системный пользователь сервиса
DB_NAME="${DB_NAME:-gogol_staging}"        # отдельная база, не общая с сайтом
DB_USER="${DB_USER:-gogol}"                # отдельная роль Postgres
ENV_FILE="/etc/${SERVICE}.env"             # секреты сервиса, root:600
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"  # каталог с app.py (родитель deploy/)

say()  { printf '\033[1m[gogol]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[gogol] ОШИБКА:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "нужен root."

# --------------------------------------------------------------- удаление ---
if [ "${1:-}" = "--remove" ]; then
  say "останавливаю и удаляю сервис ${SERVICE}…"
  systemctl disable --now "${SERVICE}" 2>/dev/null || true
  rm -f "/etc/systemd/system/${SERVICE}.service"
  systemctl daemon-reload
  if command -v psql >/dev/null 2>&1; then
    sudo -u postgres psql -tAc "DROP DATABASE IF EXISTS ${DB_NAME};" || true
    sudo -u postgres psql -tAc "DROP ROLE IF EXISTS ${DB_USER};" || true
  fi
  rm -f "${ENV_FILE}"
  say "готово. Каталог ${APP_DIR} оставлен на месте — удали вручную, если нужно:"
  say "  rm -rf ${APP_DIR}   &&   userdel ${APP_USER}"
  exit 0
fi

# --------------------------------------------------------------- проверки ---
[ -f "${APP_DIR}/app.py" ] || die "в ${APP_DIR} нет app.py — сначала скопируй файлы (см. deploy/README.md)."

command -v python3 >/dev/null 2>&1 || die "нет python3."
command -v ss      >/dev/null 2>&1 || die "нет утилиты ss (iproute2)."
command -v psql    >/dev/null 2>&1 || die "Postgres не найден. Скрипт его не ставит намеренно — установи и настрой отдельно, чтобы не задеть второй сайт."
sudo -u postgres pg_isready -q      || die "Postgres установлен, но не отвечает (pg_isready). Проверь службу."

# порт должен быть свободен — иначе не рискуем
if ss -ltnH "sport = :${PORT}" | grep -q .; then
  say "порт ${PORT} уже занят. Свободные смотри так:  ss -tlnp"
  die "выбери другой:  PORT=NNNN bash $0"
fi
say "порт ${PORT} свободен, продолжаю."

# --------------------------------------------------------------- пользователь
if ! id "${APP_USER}" >/dev/null 2>&1; then
  say "создаю системного пользователя ${APP_USER}…"
  useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
fi

# --------------------------------------------------------------- секреты -----
# Пишем один раз. При повторном запуске переиспользуем — пароли не «плывут».
if [ ! -f "${ENV_FILE}" ]; then
  DB_PASS="$(openssl rand -hex 16 2>/dev/null || head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  ADMIN_PASS="${GOGOL_ADMIN_PASSWORD:-$(openssl rand -hex 6)}"
  umask 077
  cat > "${ENV_FILE}" <<EOF
DATABASE_URL=postgresql://${DB_USER}:${DB_PASS}@127.0.0.1:5432/${DB_NAME}
GOGOL_ADMIN_PASSWORD=${ADMIN_PASS}
EOF
  chmod 600 "${ENV_FILE}"
  say "создан ${ENV_FILE} (root:600). Пароль админки: ${ADMIN_PASS}"
fi
# shellcheck disable=SC1090
set -a; . "${ENV_FILE}"; set +a
DB_PASS="$(printf '%s' "${DATABASE_URL}" | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')"

# --------------------------------------------------------------- Postgres ----
# Своя роль и своя база. Существующие базы второго сайта не трогаем.
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1; then
  say "создаю роль Postgres ${DB_USER}…"
  sudo -u postgres psql -qc "CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';"
fi
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  say "создаю базу ${DB_NAME} (владелец ${DB_USER})…"
  sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"
fi

# --------------------------------------------------------------- venv --------
if [ ! -x "${APP_DIR}/.venv/bin/python" ]; then
  say "создаю venv…"
  python3 -m venv "${APP_DIR}/.venv" \
    || die "не удалось создать venv. Обычно помогает:  apt-get install -y python3-venv"
fi
say "ставлю зависимости…"
"${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt" gunicorn

chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

# --------------------------------------------------------------- systemd -----
say "пишу сервис ${SERVICE} (слушает ${BIND}:${PORT})…"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Gogol barbershop (preview, isolated)
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
# --preload обязателен: без него каждый воркер выполняет bootstrap() и
# init_schema() параллельно, и на чистой базе они дерутся за CREATE TABLE
# (CREATE TABLE IF NOT EXISTS не защищает от гонки — падает UniqueViolation
# на pg_type_typname_nsp_index). С --preload приложение поднимается один раз
# в мастере, воркеры форкаются уже готовыми.
ExecStart=${APP_DIR}/.venv/bin/gunicorn --preload -w 2 -b ${BIND}:${PORT} app:app
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "${SERVICE}"

# --------------------------------------------------------------- проверка ----
sleep 2
if curl -fsS "http://${BIND}:${PORT}/" -o /dev/null; then
  say "сервис поднялся ✔"
else
  say "сервис не ответил сразу. Логи:  journalctl -u ${SERVICE} -n 40 --no-pager"
fi

cat <<EOF

  ─────────────────────────────────────────────────────────────
  Готово. Приложение живёт приватно на ${BIND}:${PORT}
  Второй сайт, его nginx и его база не затронуты.

  Открыть превью со своей машины (туннель, наружу порт закрыт):
     ssh -N -L 8899:127.0.0.1:${PORT} root@186.246.10.51
     → затем браузер: http://localhost:8899/    (админка: /admin)

  Логи:      journalctl -u ${SERVICE} -f
  Рестарт:   systemctl restart ${SERVICE}
  Снести:    bash ${APP_DIR}/deploy/bootstrap.sh --remove
  ─────────────────────────────────────────────────────────────
EOF
