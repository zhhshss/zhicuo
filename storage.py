# -*- coding: utf-8 -*-
"""错题本的 SQLite 数据层。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
MEDIA_DIR = DATA_DIR / "media"
DB_FILE = DATA_DIR / "mistakes.db"

JSON_FIELDS = {"tags", "knowledge_points", "image_urls"}
EDITABLE_FIELDS = {
    "title", "question", "answer", "analysis", "my_answer", "subject", "grade",
    "source", "question_type", "difficulty", "mastery", "error_type", "tags",
    "knowledge_points", "image_urls", "is_starred", "note", "notebook", "status",
    "next_review_at",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def init_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS mistakes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '',
                question TEXT NOT NULL DEFAULT '',
                answer TEXT NOT NULL DEFAULT '',
                analysis TEXT NOT NULL DEFAULT '',
                my_answer TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '未分类',
                grade TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                question_type TEXT NOT NULL DEFAULT '',
                difficulty INTEGER NOT NULL DEFAULT 3,
                mastery INTEGER NOT NULL DEFAULT 0,
                error_type TEXT NOT NULL DEFAULT '知识盲区',
                tags TEXT NOT NULL DEFAULT '[]',
                knowledge_points TEXT NOT NULL DEFAULT '[]',
                image_urls TEXT NOT NULL DEFAULT '[]',
                is_starred INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                notebook TEXT NOT NULL DEFAULT '默认错题本',
                status TEXT NOT NULL DEFAULT 'active',
                review_count INTEGER NOT NULL DEFAULT 0,
                correct_streak INTEGER NOT NULL DEFAULT 0,
                next_review_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS review_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mistake_id INTEGER NOT NULL,
                rating INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT NOT NULL,
                FOREIGN KEY(mistake_id) REFERENCES mistakes(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_mistakes_subject ON mistakes(subject);
            CREATE INDEX IF NOT EXISTS idx_mistakes_next_review ON mistakes(next_review_at);
            CREATE INDEX IF NOT EXISTS idx_mistakes_updated ON mistakes(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_reviews_mistake ON review_logs(mistake_id);
            """
        )


def materialize_data_urls(urls: list[str]) -> list[str]:
    """将前端逐题裁剪产生的 data URL 落盘，避免大段 Base64 写入数据库。"""
    import base64
    import re
    import uuid
    clean: list[str] = []
    pattern = re.compile(r"^data:image/(jpeg|jpg|png|webp);base64,(.+)$", re.I | re.S)
    for url in urls:
        match = pattern.match(str(url))
        if not match:
            clean.append(str(url))
            continue
        try:
            data = base64.b64decode(match.group(2), validate=True)
        except (ValueError, base64.binascii.Error):
            continue
        if not data or len(data) > 10 * 1024 * 1024:
            continue
        suffix = ".jpg" if match.group(1).lower() in {"jpeg", "jpg"} else f".{match.group(1).lower()}"
        relative = Path(f"{datetime.now():%Y%m}/{uuid.uuid4().hex}{suffix}")
        target = MEDIA_DIR / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        clean.append(f"/media/{relative.as_posix()}")
    return clean


@contextmanager
def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _decode(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    for field in JSON_FIELDS:
        try:
            item[field] = json.loads(item.get(field) or "[]")
        except (TypeError, json.JSONDecodeError):
            item[field] = []
    item["is_starred"] = bool(item.get("is_starred"))
    return item


def _encode(data: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in data.items():
        if key not in EDITABLE_FIELDS:
            continue
        if key in JSON_FIELDS:
            if isinstance(value, str):
                value = [part.strip() for part in value.split(",") if part.strip()]
            if key == "image_urls":
                value = materialize_data_urls(value or [])
            clean[key] = json.dumps(value or [], ensure_ascii=False)
        elif key == "is_starred":
            clean[key] = int(bool(value))
        elif key in {"difficulty", "mastery"}:
            clean[key] = max(0, min(5 if key == "difficulty" else 100, int(value or 0)))
        elif key == "next_review_at":
            clean[key] = value or None
        else:
            clean[key] = value if value is not None else ""
    return clean


def create_mistake(data: dict[str, Any]) -> dict[str, Any]:
    values = _encode(data)
    stamp = now_iso()
    values.setdefault("next_review_at", stamp)
    values.update(created_at=stamp, updated_at=stamp)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    with connect() as db:
        cur = db.execute(
            f"INSERT INTO mistakes ({columns}) VALUES ({placeholders})", tuple(values.values())
        )
        row = db.execute("SELECT * FROM mistakes WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _decode(row)


def get_mistake(item_id: int) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM mistakes WHERE id = ?", (item_id,)).fetchone()
    return _decode(row) if row else None


def update_mistake(item_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    values = _encode(data)
    if not values:
        return get_mistake(item_id)
    values["updated_at"] = now_iso()
    assignments = ", ".join(f"{key} = ?" for key in values)
    with connect() as db:
        db.execute(
            f"UPDATE mistakes SET {assignments} WHERE id = ?", (*values.values(), item_id)
        )
    return get_mistake(item_id)


def delete_mistake(item_id: int) -> bool:
    with connect() as db:
        cur = db.execute("DELETE FROM mistakes WHERE id = ?", (item_id,))
    return cur.rowcount > 0


def list_mistakes(
    *, query: str = "", subject: str = "", notebook: str = "", error_type: str = "",
    starred: bool | None = None, status: str = "active", due: bool = False,
    sort: str = "updated_desc", limit: int = 50, offset: int = 0,
) -> dict[str, Any]:
    clauses, args = [], []
    if status and status != "all":
        clauses.append("status = ?")
        args.append(status)
    if query:
        clauses.append("(title LIKE ? OR question LIKE ? OR answer LIKE ? OR analysis LIKE ? OR tags LIKE ? OR knowledge_points LIKE ?)")
        like = f"%{query}%"
        args.extend([like] * 6)
    for field, value in (("subject", subject), ("notebook", notebook), ("error_type", error_type)):
        if value:
            clauses.append(f"{field} = ?")
            args.append(value)
    if starred is not None:
        clauses.append("is_starred = ?")
        args.append(int(starred))
    if due:
        clauses.append("(next_review_at IS NULL OR next_review_at <= ?)")
        args.append(now_iso())
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    orders = {
        "updated_desc": "updated_at DESC", "created_desc": "created_at DESC",
        "difficulty_desc": "difficulty DESC, updated_at DESC",
        "mastery_asc": "mastery ASC, updated_at DESC", "review_asc": "next_review_at ASC",
    }
    order = orders.get(sort, orders["updated_desc"])
    limit = max(1, min(500, limit))
    offset = max(0, offset)
    with connect() as db:
        total = db.execute(f"SELECT COUNT(*) FROM mistakes{where}", args).fetchone()[0]
        rows = db.execute(
            f"SELECT * FROM mistakes{where} ORDER BY {order} LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
    return {"items": [_decode(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def batch_update(ids: Iterable[int], action: str, value: Any = None) -> int:
    clean_ids = sorted({int(item_id) for item_id in ids if int(item_id) > 0})
    if not clean_ids:
        return 0
    placeholders = ",".join("?" for _ in clean_ids)
    with connect() as db:
        if action == "delete":
            cur = db.execute(f"DELETE FROM mistakes WHERE id IN ({placeholders})", clean_ids)
        elif action in {"archive", "restore"}:
            cur = db.execute(
                f"UPDATE mistakes SET status = ?, updated_at = ? WHERE id IN ({placeholders})",
                ("archived" if action == "archive" else "active", now_iso(), *clean_ids),
            )
        elif action == "star":
            cur = db.execute(
                f"UPDATE mistakes SET is_starred = ?, updated_at = ? WHERE id IN ({placeholders})",
                (int(bool(value)), now_iso(), *clean_ids),
            )
        elif action in {"subject", "notebook"}:
            cur = db.execute(
                f"UPDATE mistakes SET {action} = ?, updated_at = ? WHERE id IN ({placeholders})",
                (str(value or ""), now_iso(), *clean_ids),
            )
        else:
            raise ValueError("不支持的批量操作")
    return cur.rowcount


def record_review(item_id: int, rating: int, note: str = "") -> dict[str, Any] | None:
    item = get_mistake(item_id)
    if not item:
        return None
    rating = max(0, min(3, int(rating)))
    intervals = {0: 1, 1: 2, 2: 4, 3: 7}
    streak = item["correct_streak"] + 1 if rating >= 2 else 0
    if streak > 1 and rating >= 2:
        intervals[rating] = min(90, intervals[rating] * (2 ** min(streak - 1, 4)))
    mastery_delta = {-1: -20, 0: -15, 1: 2, 2: 10, 3: 18}.get(rating, 0)
    mastery = max(0, min(100, item["mastery"] + mastery_delta))
    next_review = (datetime.now().astimezone() + timedelta(days=intervals[rating])).isoformat(timespec="seconds")
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO review_logs (mistake_id, rating, note, reviewed_at) VALUES (?, ?, ?, ?)",
            (item_id, rating, note, stamp),
        )
        db.execute(
            """UPDATE mistakes SET review_count = review_count + 1, correct_streak = ?,
               mastery = ?, next_review_at = ?, updated_at = ? WHERE id = ?""",
            (streak, mastery, next_review, stamp, item_id),
        )
    return get_mistake(item_id)


def dashboard() -> dict[str, Any]:
    now = now_iso()
    seven_days = (datetime.now().astimezone() - timedelta(days=6)).date().isoformat()
    with connect() as db:
        stats = dict(db.execute(
            """SELECT COUNT(*) AS total,
               SUM(CASE WHEN status='active' AND (next_review_at IS NULL OR next_review_at <= ?) THEN 1 ELSE 0 END) AS due,
               SUM(CASE WHEN is_starred=1 AND status='active' THEN 1 ELSE 0 END) AS starred,
               COALESCE(ROUND(AVG(CASE WHEN status='active' THEN mastery END)), 0) AS mastery
               FROM mistakes""", (now,)
        ).fetchone())
        subjects = [dict(row) for row in db.execute(
            "SELECT subject AS name, COUNT(*) AS value FROM mistakes WHERE status='active' GROUP BY subject ORDER BY value DESC LIMIT 8"
        )]
        error_types = [dict(row) for row in db.execute(
            "SELECT error_type AS name, COUNT(*) AS value FROM mistakes WHERE status='active' GROUP BY error_type ORDER BY value DESC LIMIT 8"
        )]
        trend_rows = db.execute(
            """SELECT substr(created_at,1,10) AS day, COUNT(*) AS value FROM mistakes
               WHERE created_at >= ? GROUP BY day ORDER BY day""", (seven_days,)
        ).fetchall()
        activity = [dict(row) for row in db.execute(
            """SELECT r.rating, r.reviewed_at, m.title, m.subject FROM review_logs r
               JOIN mistakes m ON m.id=r.mistake_id ORDER BY r.reviewed_at DESC LIMIT 8"""
        )]
    trend_map = {row["day"]: row["value"] for row in trend_rows}
    days = [(datetime.now().date() - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    stats = {key: int(value or 0) for key, value in stats.items()}
    rating_labels = {0: "忘记了", 1: "有点难", 2: "掌握了", 3: "很轻松"}
    for row in activity:
        row["rating_label"] = rating_labels.get(row["rating"], "已复习")
    return {
        "stats": stats, "subjects": subjects, "error_types": error_types,
        "trend": [{"day": day, "value": trend_map.get(day, 0)} for day in days],
        "activity": activity,
    }


def distinct_options() -> dict[str, list[str]]:
    fields = ("subject", "grade", "notebook", "error_type", "source", "question_type")
    result: dict[str, list[str]] = {}
    with connect() as db:
        for field in fields:
            result[field] = [row[0] for row in db.execute(
                f"SELECT DISTINCT {field} FROM mistakes WHERE {field} != '' ORDER BY {field}"
            )]
    return result


def review_history(item_id: int) -> list[dict[str, Any]]:
    with connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT rating, note, reviewed_at FROM review_logs WHERE mistake_id=? ORDER BY reviewed_at DESC",
            (item_id,),
        )]


init_storage()
