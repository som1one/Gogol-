"""Работа с PostgreSQL: подключение, схема, чтение и запись контента сайта."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg import conninfo as pg_conninfo
from psycopg import sql as pg_sql
from psycopg.rows import dict_row
from psycopg.types.json import Json

BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "schema.sql"
SEED_PATH = BASE_DIR / "content.json"

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/gogol"

# Слоты фотографий: ключ в разметке -> подпись в админке. Порядок = порядок в админке.
PHOTO_SLOTS: list[tuple[str, str]] = [
    ("logo", "Логотип"),
    ("hero1", "Главная сетка — большое левое"),
    ("hero2", "Главная сетка — верх по центру"),
    ("hero3", "Главная сетка — низ по центру"),
    ("hero4", "Главная сетка — большое правое"),
    ("hero5", "Главная сетка — широкое снизу"),
    ("services", "Блок «Услуги»"),
    ("atmosphere", "Блок «Атмосфера»"),
    ("about", "Блок «О нас»"),
]

SETTING_KEYS = ("booking_url", "show_cosmetics", "combo_kicker", "combo_title", "combo_price")


def dsn() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_DSN


@contextmanager
def connect(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Соединение с рабочей базой. Транзакция коммитится при выходе без исключения."""
    conn = psycopg.connect(dsn(), row_factory=dict_row, autocommit=autocommit)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def ensure_database() -> bool:
    """Создаёт базу из DATABASE_URL, если её ещё нет. True — если создали сейчас."""
    info = pg_conninfo.conninfo_to_dict(dsn())
    dbname = info.pop("dbname", "gogol")
    admin_dsn = pg_conninfo.make_conninfo(**info, dbname="postgres")

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if exists:
            return False
        # Имя базы нельзя передать плейсхолдером — экранируем как идентификатор.
        conn.execute(
            pg_sql.SQL("CREATE DATABASE {}").format(pg_sql.Identifier(dbname))
        )
    return True


def init_schema() -> None:
    with connect() as conn:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


def is_empty() -> bool:
    with connect() as conn:
        row = conn.execute("SELECT count(*) AS n FROM photos").fetchone()
    return row["n"] == 0


def seed_from_file() -> None:
    """Первичное наполнение базы из content.json."""
    doc = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    save_content(doc, revision=False)


# --------------------------------------------------------------------------- чтение


def load_content() -> dict[str, Any]:
    """Собирает документ контента из таблиц — в том виде, в каком его ждёт фронтенд."""
    with connect() as conn:
        settings = {
            r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")
        }

        photos_rows = conn.execute(
            "SELECT slot, label, url FROM photos ORDER BY position, slot"
        ).fetchall()

        services = conn.execute(
            "SELECT num, title, price, descr, tags FROM services ORDER BY position, id"
        ).fetchall()

        groups = conn.execute(
            "SELECT id, title, visible FROM price_groups ORDER BY position, id"
        ).fetchall()
        rows_by_group: dict[int, list[dict[str, str]]] = {g["id"]: [] for g in groups}
        for row in conn.execute(
            "SELECT group_id, name, price FROM price_rows ORDER BY group_id, position, id"
        ):
            rows_by_group.setdefault(row["group_id"], []).append(
                {"name": row["name"], "price": row["price"]}
            )

        brands = conn.execute(
            "SELECT id, name, tagline FROM brands ORDER BY position, id"
        ).fetchall()
        items_by_brand: dict[int, list[dict[str, str]]] = {b["id"]: [] for b in brands}
        for item in conn.execute(
            "SELECT brand_id, name, note FROM brand_items ORDER BY brand_id, position, id"
        ):
            items_by_brand.setdefault(item["brand_id"], []).append(
                {"name": item["name"], "note": item["note"]}
            )

    return {
        "booking": {"url": settings.get("booking_url", "tel:+375292137271")},
        "sections": {"showCosmetics": settings.get("show_cosmetics", "true") == "true"},
        "photos": {r["slot"]: r["url"] for r in photos_rows},
        "photoLabels": {r["slot"]: r["label"] for r in photos_rows},
        "services": [
            {
                "num": s["num"],
                "title": s["title"],
                "price": s["price"],
                "desc": s["descr"],
                "tags": s["tags"],
            }
            for s in services
        ],
        "combo": {
            "kicker": settings.get("combo_kicker", ""),
            "title": settings.get("combo_title", ""),
            "price": settings.get("combo_price", ""),
        },
        "priceGroups": [
            {
                "title": g["title"],
                "visible": g["visible"],
                "rows": rows_by_group.get(g["id"], []),
            }
            for g in groups
        ],
        "brands": [
            {
                "name": b["name"],
                "tagline": b["tagline"],
                "items": items_by_brand.get(b["id"], []),
            }
            for b in brands
        ],
    }


def public_content() -> dict[str, Any]:
    """То, что уходит на сайт: без служебных полей админки."""
    doc = load_content()
    doc.pop("photoLabels", None)
    return doc


# --------------------------------------------------------------------------- запись


def _s(value: Any, limit: int = 2000) -> str:
    """Приводит значение к строке разумной длины — защита от мусора в теле запроса."""
    if value is None:
        return ""
    return str(value)[:limit]


def save_content(doc: dict[str, Any], revision: bool = True) -> None:
    """Полностью перезаписывает контент одной транзакцией."""
    booking = doc.get("booking") or {}
    sections = doc.get("sections") or {}
    combo = doc.get("combo") or {}
    photos = doc.get("photos") or {}
    labels = {slot: label for slot, label in PHOTO_SLOTS}

    with connect() as conn:
        values = {
            "booking_url": _s(booking.get("url"), 500),
            "show_cosmetics": "true" if sections.get("showCosmetics", True) else "false",
            "combo_kicker": _s(combo.get("kicker"), 200),
            "combo_title": _s(combo.get("title"), 300),
            "combo_price": _s(combo.get("price"), 100),
        }
        for key, value in values.items():
            conn.execute(
                """INSERT INTO settings (key, value) VALUES (%s, %s)
                   ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value, updated_at = now()""",
                (key, value),
            )

        for position, (slot, label) in enumerate(PHOTO_SLOTS):
            conn.execute(
                """INSERT INTO photos (slot, label, url, position) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (slot) DO UPDATE
                   SET url = EXCLUDED.url, label = EXCLUDED.label,
                       position = EXCLUDED.position, updated_at = now()""",
                (slot, labels[slot], _s(photos.get(slot), 1000), position),
            )

        conn.execute("DELETE FROM services")
        for position, svc in enumerate(doc.get("services") or []):
            conn.execute(
                """INSERT INTO services (position, num, title, price, descr, tags)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (
                    position,
                    _s(svc.get("num"), 40),
                    _s(svc.get("title"), 200),
                    _s(svc.get("price"), 100),
                    _s(svc.get("desc")),
                    _s(svc.get("tags"), 300),
                ),
            )

        conn.execute("DELETE FROM price_groups")  # price_rows уйдут по ON DELETE CASCADE
        for position, group in enumerate(doc.get("priceGroups") or []):
            group_id = conn.execute(
                "INSERT INTO price_groups (position, title, visible) VALUES (%s, %s, %s) RETURNING id",
                (position, _s(group.get("title"), 200), bool(group.get("visible", True))),
            ).fetchone()["id"]
            for row_pos, row in enumerate(group.get("rows") or []):
                conn.execute(
                    "INSERT INTO price_rows (group_id, position, name, price) VALUES (%s, %s, %s, %s)",
                    (group_id, row_pos, _s(row.get("name"), 300), _s(row.get("price"), 100)),
                )

        conn.execute("DELETE FROM brands")  # brand_items уйдут по ON DELETE CASCADE
        for position, brand in enumerate(doc.get("brands") or []):
            brand_id = conn.execute(
                "INSERT INTO brands (position, name, tagline) VALUES (%s, %s, %s) RETURNING id",
                (position, _s(brand.get("name"), 200), _s(brand.get("tagline"), 200)),
            ).fetchone()["id"]
            for item_pos, item in enumerate(brand.get("items") or []):
                conn.execute(
                    "INSERT INTO brand_items (brand_id, position, name, note) VALUES (%s, %s, %s, %s)",
                    (brand_id, item_pos, _s(item.get("name"), 300), _s(item.get("note"))),
                )

        if revision:
            conn.execute(
                "INSERT INTO content_revisions (document) VALUES (%s)", (Json(doc),)
            )
            # Держим последние 50 снимков, остальное чистим.
            conn.execute(
                """DELETE FROM content_revisions WHERE id NOT IN (
                       SELECT id FROM content_revisions ORDER BY id DESC LIMIT 50
                   )"""
            )


# --------------------------------------------------------------------------- медиа


def register_media(filename: str, original_name: str, size_bytes: int) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO media (filename, original_name, size_bytes) VALUES (%s, %s, %s)
               ON CONFLICT (filename) DO NOTHING""",
            (filename, original_name[:255], size_bytes),
        )


def list_media() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT filename, original_name, size_bytes, uploaded_at FROM media ORDER BY uploaded_at DESC"
        ).fetchall()
    return [
        {
            "filename": r["filename"],
            "originalName": r["original_name"],
            "size": r["size_bytes"],
            "uploadedAt": r["uploaded_at"].isoformat(),
        }
        for r in rows
    ]


def forget_media(filename: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM media WHERE filename = %s", (filename,))


# --------------------------------------------------------------------------- доступ


def get_setting(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO settings (key, value) VALUES (%s, %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
            (key, value),
        )
