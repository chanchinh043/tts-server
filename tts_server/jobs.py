# tts_server/jobs.py
#
# Quản lý bảng job DUY NHẤT của server.
#
# ── CẬP NHẬT: RETRY ──────────────────────────────────────────────────────
# Thêm 2 cột: retry_count (số lần đã tự động thử lại) và last_error (lỗi
# gần nhất, để debug/hiển thị). Có 2 cơ chế retry:
#   1. AUTO-RETRY (job_processor.py): lỗi tạm thời (mạng, Drive timeout...)
#      → tự enqueue lại tối đa MAX_RETRIES lần, có backoff — xem
#      job_processor._fail_or_retry().
#   2. MANUAL RETRY (create_or_get_job dưới đây): nếu Android gọi lại
#      request_synthesis() cho đúng job đã 'failed' (hết auto-retry, hoặc
#      lỗi cần người sửa như OAuth token) → job được RESET về 'pending' và
#      main.py sẽ tự enqueue lại (vì logic ở main.py chỉ enqueue khi status
#      vừa đọc lại là 'pending').
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "tts_myreading_jobs.db"

FINISHED_JOB_TTL_HOURS = 48

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def init_db(db_path: Path = DB_PATH) -> None:
    """Gọi 1 lần lúc server khởi động (xem main.py, sự kiện startup)."""
    global _conn
    _conn = sqlite3.connect(str(db_path), check_same_thread=False)
    with _lock:
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tts_myreading_jobs (
                reading_id   TEXT NOT NULL,
                sid          INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                items_json   TEXT NOT NULL DEFAULT '[]',
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                PRIMARY KEY (reading_id, sid, content_hash)
            )
            """
        )
        # ── Migration an toàn cho DB đã tồn tại từ trước (chưa có 2 cột
        # mới) — ALTER TABLE ADD COLUMN chỉ chạy nếu cột CHƯA có, tránh lỗi
        # "duplicate column" mỗi lần server khởi động lại. ─────────────────
        if not _column_exists(_conn, "tts_myreading_jobs", "retry_count"):
            _conn.execute(
                "ALTER TABLE tts_myreading_jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
            )
        if not _column_exists(_conn, "tts_myreading_jobs", "last_error"):
            _conn.execute(
                "ALTER TABLE tts_myreading_jobs ADD COLUMN last_error TEXT"
            )
        _conn.commit()


def _require_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("jobs.init_db() chưa được gọi — kiểm tra lại startup event ở main.py")
    return _conn


def create_or_get_job(reading_id: str, sid: int, content_hash: str, items_json: str) -> str:
    """Tạo job mới nếu chưa có, hoặc trả về status của job đã tồn tại.

    ⚠️ CẬP NHẬT RETRY: nếu job đã tồn tại và đang ở status 'failed', COI
    LẦN GỌI NÀY LÀ YÊU CẦU THỬ LẠI — reset về 'pending', retry_count=0,
    xoá last_error, rồi trả về 'pending' (main.py sẽ tự enqueue lại vì nó
    chỉ enqueue khi status đọc được là 'pending'). Đây là cách retry thủ
    công tự nhiên nhất: Android không cần biết gì về khái niệm "retry" —
    chỉ cần gọi lại đúng request cũ (TtsMyReadingSyncTrigger vốn đã làm
    việc này định kỳ) là job cũ được hồi sinh.

    Job đang 'pending'/'processing'/'ready' thì giữ nguyên, KHÔNG đụng gì
    (tránh xử lý trùng vô ích hoặc phá vỡ dedup theo content_hash).
    """
    conn = _require_conn()
    now = _now_iso()
    with _lock:
        conn.execute(
            """
            INSERT OR IGNORE INTO tts_myreading_jobs
                (reading_id, sid, content_hash, status, items_json, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, ?, ?)
            """,
            (reading_id, sid, content_hash, items_json, now, now),
        )

        row = conn.execute(
            """
            SELECT status FROM tts_myreading_jobs
            WHERE reading_id = ? AND sid = ? AND content_hash = ?
            """,
            (reading_id, sid, content_hash),
        ).fetchone()

        status = row[0] if row else "pending"

        if status == "failed":
            conn.execute(
                """
                UPDATE tts_myreading_jobs
                SET status = 'pending', retry_count = 0, last_error = NULL, updated_at = ?
                WHERE reading_id = ? AND sid = ? AND content_hash = ?
                """,
                (now, reading_id, sid, content_hash),
            )
            status = "pending"

        conn.commit()
    return status


def get_job_items(reading_id: str, sid: int, content_hash: str) -> list[dict]:
    import json

    conn = _require_conn()
    row = conn.execute(
        """
        SELECT items_json FROM tts_myreading_jobs
        WHERE reading_id = ? AND sid = ? AND content_hash = ?
        """,
        (reading_id, sid, content_hash),
    ).fetchone()
    if row is None:
        return []
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return []


def get_job_status(reading_id: str, sid: int, content_hash: str) -> Optional[str]:
    conn = _require_conn()
    row = conn.execute(
        """
        SELECT status FROM tts_myreading_jobs
        WHERE reading_id = ? AND sid = ? AND content_hash = ?
        """,
        (reading_id, sid, content_hash),
    ).fetchone()
    return row[0] if row else None


def update_job_status(
    reading_id: str,
    sid: int,
    content_hash: str,
    status: str,
    error: Optional[str] = None,
) -> None:
    """Cập nhật status — như cũ, nhưng nhận thêm `error` (tuỳ chọn) để lưu
    vào last_error khi status='failed'. Khi status khác 'failed' (vd
    'processing', 'ready'), error luôn bị bỏ qua/xoá — last_error chỉ có ý
    nghĩa khi job đang ở trạng thái failed.
    """
    conn = _require_conn()
    with _lock:
        conn.execute(
            """
            UPDATE tts_myreading_jobs
            SET status = ?, updated_at = ?, last_error = ?
            WHERE reading_id = ? AND sid = ? AND content_hash = ?
            """,
            (status, _now_iso(), error if status == "failed" else None, reading_id, sid, content_hash),
        )
        conn.commit()


def increment_retry(reading_id: str, sid: int, content_hash: str) -> int:
    """Tăng retry_count lên 1, trả về giá trị MỚI sau khi tăng — dùng bởi
    job_processor._fail_or_retry() để quyết định còn được tự động thử lại
    nữa hay không (so với MAX_RETRIES).
    """
    conn = _require_conn()
    with _lock:
        conn.execute(
            """
            UPDATE tts_myreading_jobs
            SET retry_count = retry_count + 1, updated_at = ?
            WHERE reading_id = ? AND sid = ? AND content_hash = ?
            """,
            (_now_iso(), reading_id, sid, content_hash),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT retry_count FROM tts_myreading_jobs
            WHERE reading_id = ? AND sid = ? AND content_hash = ?
            """,
            (reading_id, sid, content_hash),
        ).fetchone()
    return row[0] if row else 0


def get_unfinished_jobs() -> list[tuple[str, int, str]]:
    conn = _require_conn()
    rows = conn.execute(
        """
        SELECT reading_id, sid, content_hash FROM tts_myreading_jobs
        WHERE status IN ('pending', 'processing')
        ORDER BY created_at ASC
        """
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def cleanup_finished_jobs(ttl_hours: int = FINISHED_JOB_TTL_HOURS) -> int:
    conn = _require_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock:
        cursor = conn.execute(
            """
            DELETE FROM tts_myreading_jobs
            WHERE status IN ('ready', 'failed') AND updated_at < ?
            """,
            (cutoff,),
        )
        conn.commit()
        return cursor.rowcount