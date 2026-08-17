"""Переносит фотографии сайта с чужого хостинга к себе в media/.

Зачем: адреса снимков в базе вели на старый сайт клиента через сторонний
прокси wsrv.nl. Любая заминка на том хостинге или у прокси — и посетитель
видит пустой хиро. Скрипт скачивает каждую такую картинку один раз, кладёт
рядом с логотипом в media/ и переписывает адрес в базе на локальный.

Запуск на сервере (из каталога приложения, с его окружением):

    set -a && . /etc/gogol.env && set +a
    .venv/bin/python tools/localize_photos.py

Повторный запуск безопасен: локальные адреса он пропускает.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import db  # noqa: E402  (после правки sys.path)

MEDIA_DIR = BASE_DIR / "media"
TIMEOUT = 60


def _extension(data: bytes) -> str:
    """Тип определяем по сигнатуре: прокси отдаёт webp, а исходники — jpeg."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return ".bin"


def main() -> int:
    MEDIA_DIR.mkdir(exist_ok=True)
    with db.connect() as conn:
        rows = conn.execute("SELECT slot, url FROM photos ORDER BY slot").fetchall()

    moved = skipped = failed = 0
    for row in rows:
        slot, url = row["slot"], row["url"]
        if not url.lower().startswith(("http://", "https://")):
            print(f"пропуск  {slot:<11} уже локально: {url}")
            skipped += 1
            continue

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "gogol-site/1.0"})
            data = urllib.request.urlopen(req, timeout=TIMEOUT).read()
        except Exception as exc:  # сеть, 404, таймаут — не роняем остальные слоты
            print(f"ОШИБКА   {slot:<11} {exc}")
            failed += 1
            continue

        name = f"{slot}-{hashlib.sha1(data).hexdigest()[:8]}{_extension(data)}"
        (MEDIA_DIR / name).write_bytes(data)

        with db.connect() as conn:
            conn.execute(
                """INSERT INTO media (filename, original_name, size_bytes)
                   VALUES (%s, %s, %s) ON CONFLICT (filename) DO NOTHING""",
                (name, url[:200], len(data)),
            )
            conn.execute("UPDATE photos SET url = %s WHERE slot = %s", (f"/media/{name}", slot))
        print(f"скачано  {slot:<11} {len(data):>7} байт -> /media/{name}")
        moved += 1

    print(f"\nитого: перенесено {moved}, уже локальных {skipped}, ошибок {failed}")
    if failed:
        print("слоты с ошибкой остались на старых адресах — запустите скрипт ещё раз")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
