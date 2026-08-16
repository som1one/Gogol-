"""Сайт «Гоголь барбершоп» + админка. Контент хранится в PostgreSQL."""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import uuid
from html import escape
from pathlib import Path

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import db

BASE_DIR = Path(__file__).resolve().parent
SITE_FILE = BASE_DIR / "Gogol Barbershop.dc.html"
ADMIN_DIR = BASE_DIR / "admin"
MEDIA_DIR = BASE_DIR / "media"

ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
DEFAULT_PASSWORD = "gogol76"


# --------------------------------------------------------------------------- запуск


def bootstrap() -> None:
    """Готовит базу: создаёт её при необходимости, накатывает схему, наполняет данными."""
    created = db.ensure_database()
    db.init_schema()
    if db.is_empty():
        db.seed_from_file()
        print("[db] контент загружен из content.json")
    elif created:
        print("[db] база создана")

    if not db.get_setting("admin_password_hash"):
        password = os.environ.get("GOGOL_ADMIN_PASSWORD") or DEFAULT_PASSWORD
        db.set_setting("admin_password_hash", generate_password_hash(password))
        print(f"[admin] пароль: {password}  (сменить: GOGOL_ADMIN_PASSWORD)")

    if not db.get_setting("flask_secret"):
        db.set_setting("flask_secret", secrets.token_hex(32))


def create_app() -> Flask:
    MEDIA_DIR.mkdir(exist_ok=True)
    bootstrap()

    app = Flask(__name__, static_folder=None)
    app.config.update(
        SECRET_KEY=db.get_setting("flask_secret"),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
    )
    app.json.ensure_ascii = False
    register_routes(app)
    return app


# --------------------------------------------------------------------------- доступ


def logged_in() -> bool:
    return bool(session.get("admin"))


def require_login() -> None:
    if not logged_in():
        abort(401)


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def check_csrf() -> None:
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf")
    if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
        abort(403, "CSRF token mismatch")


# --------------------------------------------------------------------------- сайт


PHOTO_PLACEHOLDER_RE = re.compile(r"\{\{\s*photos\.(\w+)\s*\}\}")


def render_site() -> str:
    """Отдаёт разметку сайта с подставленным контентом из базы."""
    html = SITE_FILE.read_text(encoding="utf-8")
    content = db.public_content()

    # Адреса картинок подставляем прямо в разметку: иначе браузер успевает
    # отправить запрос по литеральному "{{ photos.* }}" ещё до рендера рантайма.
    photos = content.get("photos", {})
    html = PHOTO_PLACEHOLDER_RE.sub(
        lambda m: escape(photos.get(m.group(1), ""), quote=True), html
    )

    payload = json.dumps(content, ensure_ascii=False)
    # Не даём данным «выйти» из тега <script>.
    payload = payload.replace("</", "<\\/").replace("<!--", "<\\!--")
    inject = (
        f"<script>window.__GOGOL_CONTENT__ = {payload};</script>\n"
        '<script src="/support.js"></script>'
    )
    return html.replace('<script src="./support.js"></script>', inject, 1)


# --------------------------------------------------------------------------- маршруты


def register_routes(app: Flask) -> None:
    @app.get("/")
    def site():
        return render_site(), 200, {"Content-Type": "text/html; charset=utf-8"}

    @app.get("/support.js")
    def support_js():
        return send_from_directory(BASE_DIR, "support.js", mimetype="text/javascript")

    @app.get("/media/<path:filename>")
    def media(filename: str):
        return send_from_directory(MEDIA_DIR, filename)

    # ---------------------------------------------------------------- админка

    @app.get("/admin")
    def admin_page():
        if not logged_in():
            return redirect(url_for("admin_login_page"))
        csrf_token()
        return send_from_directory(ADMIN_DIR, "index.html")

    @app.get("/admin/login")
    def admin_login_page():
        if logged_in():
            return redirect(url_for("admin_page"))
        return send_from_directory(ADMIN_DIR, "login.html")

    @app.post("/admin/login")
    def admin_login():
        password = (request.form.get("password") or "").strip()
        stored = db.get_setting("admin_password_hash") or ""
        if not stored or not check_password_hash(stored, password):
            return redirect(url_for("admin_login_page", e=1))
        session.clear()
        session["admin"] = True
        session.permanent = False
        csrf_token()
        return redirect(url_for("admin_page"))

    @app.post("/admin/logout")
    def admin_logout():
        session.clear()
        return redirect(url_for("admin_login_page"))

    # ---------------------------------------------------------------- API

    @app.get("/api/content")
    def api_content_get():
        require_login()
        return jsonify(
            {
                "content": db.load_content(),
                "photoSlots": [
                    {"slot": slot, "label": label} for slot, label in db.PHOTO_SLOTS
                ],
                "csrf": csrf_token(),
            }
        )

    @app.put("/api/content")
    def api_content_put():
        require_login()
        check_csrf()
        doc = request.get_json(silent=True)
        if not isinstance(doc, dict):
            abort(400, "ожидался JSON-объект")
        db.save_content(doc)
        return jsonify({"ok": True, "content": db.load_content()})

    @app.get("/api/media")
    def api_media_list():
        require_login()
        return jsonify({"files": db.list_media()})

    @app.post("/api/media")
    def api_media_upload():
        require_login()
        check_csrf()
        file = request.files.get("file")
        if not file or not file.filename:
            abort(400, "файл не передан")

        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            abort(400, f"недопустимый формат {ext or '?'}; можно: " + ", ".join(sorted(ALLOWED_EXT)))

        # Имя собираем сами из белого списка символов, поэтому «..» и слеши исключены.
        # Кириллица целиком отсеивается — тогда просто берём нейтральное «photo».
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(file.filename).stem).strip("-")
        filename = f"{stem[:40] or 'photo'}-{uuid.uuid4().hex[:8]}{ext}"
        path = MEDIA_DIR / filename
        file.save(path)

        size = path.stat().st_size
        if size == 0:
            path.unlink(missing_ok=True)
            abort(400, "пустой файл")

        db.register_media(filename, file.filename, size)
        return jsonify({"ok": True, "url": f"/media/{filename}", "filename": filename})

    @app.delete("/api/media/<path:filename>")
    def api_media_delete(filename: str):
        require_login()
        check_csrf()
        safe = secure_filename(filename)
        if safe != filename:
            abort(400, "некорректное имя файла")
        (MEDIA_DIR / safe).unlink(missing_ok=True)
        db.forget_media(safe)
        return jsonify({"ok": True})

    # ---------------------------------------------------------------- ошибки

    @app.errorhandler(401)
    def unauthorized(_):
        return jsonify({"error": "нужна авторизация"}), 401

    @app.errorhandler(403)
    def forbidden(err):
        return jsonify({"error": getattr(err, "description", "запрещено")}), 403

    @app.errorhandler(400)
    def bad_request(err):
        return jsonify({"error": getattr(err, "description", "некорректный запрос")}), 400

    @app.errorhandler(413)
    def too_large(_):
        return jsonify({"error": f"файл больше {MAX_UPLOAD_BYTES // 1024 // 1024} МБ"}), 413


# --------------------------------------------------------------------------- main

try:
    app = create_app()
except Exception as exc:  # noqa: BLE001 — на старте важно объяснить, что не так
    print(f"\n[ошибка] не удалось подключиться к PostgreSQL: {exc}", file=sys.stderr)
    print(f"[ошибка] DATABASE_URL = {db.dsn()}", file=sys.stderr)
    print("[ошибка] проверь, что сервер запущен и доступен.\n", file=sys.stderr)
    raise

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8123"))
    print(f"[сайт]    http://127.0.0.1:{port}/")
    print(f"[админка] http://127.0.0.1:{port}/admin")
    app.run(host="127.0.0.1", port=port, debug=False)
