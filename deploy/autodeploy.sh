#!/usr/bin/env bash
# Автодеплой «Гоголя»: тянет изменения из GitHub и раскатывает их.
#
# Запускается таймером systemd (gogol-deploy.timer). Если новых коммитов нет —
# молча выходит. Если после выката приложение не поднялось — откатывается на
# предыдущий коммит, поэтому сломанный push не роняет сайт.
#
# Ручной запуск:  systemctl start gogol-deploy
# Логи:           journalctl -u gogol-deploy -n 50

set -euo pipefail

REPO="git@github.com:som1one/Gogol-.git"
BRANCH="main"
# Репозиторий приватный, поэтому клон идёт по SSH и ему нужен ключ. Ключ свой,
# отдельный от общих root'овых: на сервере живёт второй сайт, и его настройки
# трогать нельзя. Публичную половину один раз добавляют в GitHub → репозиторий →
# Settings → Deploy keys (доступ на чтение, write access не нужен).
# accept-new — у systemd нет терминала, и неизвестный ключ github.com иначе
# завалил бы выкат вопросом, на который некому ответить.
DEPLOY_KEY="/root/.ssh/gogol_deploy"
SRC="/opt/gogol-src"          # рабочая копия репозитория
APP="/opt/gogol"              # то, что реально крутится
PORT="8790"
USER_APP="gogol"

log() { echo "[deploy] $*"; }

if [[ -f "${DEPLOY_KEY}" ]]; then
    export GIT_SSH_COMMAND="ssh -i ${DEPLOY_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
fi

# Первый запуск — клонируем.
if [[ ! -d "${SRC}/.git" ]]; then
    log "первый запуск, клонирую ${REPO}"
    git clone --branch "${BRANCH}" "${REPO}" "${SRC}"
fi

cd "${SRC}"
git remote set-url origin "${REPO}"

before="$(git rev-parse HEAD 2>/dev/null || echo '')"
git fetch --quiet origin "${BRANCH}"
after="$(git rev-parse "origin/${BRANCH}")"

if [[ "${before}" == "${after}" ]]; then
    exit 0                     # нечего делать, не шумим в журнале
fi

log "новый коммит: ${before:0:7}${before:+ }→ ${after:0:7}"
git reset --quiet --hard "origin/${BRANCH}"

# Раскладываем код. Не трогаем то, что живёт только на сервере:
#   media/     — фотографии, загруженные через админку
#   .venv/     — окружение
#   .gunicorn/ — управляющий сокет
sync_code() {
    rsync -a --delete \
        --exclude='.git/' --exclude='.venv/' --exclude='media/' \
        --exclude='.gunicorn/' --exclude='__pycache__/' \
        "${SRC}/" "${APP}/"
    "${APP}/.venv/bin/pip" install -q -r "${APP}/requirements.txt"
    chown -R "${USER_APP}:${USER_APP}" "${APP}"
    systemctl restart gogol
}

sync_code

# Проверяем, что приложение действительно ответило.
healthy=""
for _ in $(seq 1 15); do
    sleep 1
    if [[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/")" == "200" ]]; then
        healthy="yes"
        break
    fi
done

if [[ -n "${healthy}" ]]; then
    log "выкачен ${after:0:7}, приложение отвечает 200"
    exit 0
fi

# Не поднялось — откатываемся, чтобы сайт остался живым.
log "ПРИЛОЖЕНИЕ НЕ ОТВЕТИЛО после выката ${after:0:7} — откат"
if [[ -n "${before}" ]]; then
    git reset --quiet --hard "${before}"
    sync_code
    log "откатились на ${before:0:7}"
else
    log "откатываться некуда: это был первый выкат"
fi
exit 1
