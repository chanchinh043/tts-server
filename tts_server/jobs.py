# tts_server/jobs.py
#
# Quản lý bảng job DUY NHẤT của server — thay thế hoàn toàn việc trả cứng
# "pending" ở main.py bước 1. Dùng SQLite (đủ dùng cho quy mô 1 server, dễ
# backup — chỉ 1 file .db) — có thể đổi sang Postgres sau này nếu cần nhiều
# worker chạy song song, nhưng KHÔNG cần thiết ở giai đoạn này.
#
# ── SCHEMA ────────────────────────────────────────────────────────────────
# PRIMARY KEY (reading_id, sid, content_hash) — đúng khoá dedup mà Android
# (TtsMyReadingRequestClient.kt) đã giả định: 2 thiết bị/2 lần gọi trùng cả
# 3 giá trị này chỉ tạo ra ĐÚNG 1 job, an toàn khi gọi lặp lại.
#
# ── AN TOÀN ĐA LUỒNG ─────────────────────────────────────────────────────
# sqlite3 connection không an toàn dùng chung giữa nhiều thread mặc định —
# dùng check_same_thread=False + 1 threading.Lock bọc quanh mọi thao tác ghi
# (insert/update). Đơn giản, đủ dùng cho lưu lượng thấp ở giai đoạn hiện
# tại — nếu sau này cần xử lý đồng thời nhiều job nặng, cân nhắc đổi sang
# connection pool hoặc Postgres.
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "tts_myreading_jobs.db"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        _conn.commit()


def _require_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("jobs.init_db() chưa được gọi — kiểm tra lại startup event ở main.py")
    return _conn


# ── Gọi từ POST /tts/myreading/request — tạo job nếu chưa có, hoặc trả về
# status của job ĐÃ tồn tại nếu trùng (reading_id, sid, content_hash). AN
# TOÀN khi gọi lặp lại nhiều lần/nhiều thiết bị — INSERT OR IGNORE không ghi
# đè job đã có, dòng SELECT sau đó luôn đọc lại đúng trạng thái hiện tại
# (dù job đó vừa được TẠO MỚI ở chính lệnh INSERT này hay đã tồn tại từ
# trước). ────────────────────────────────────────────────────────────────
#
# items_json: JSON-encode sẵn của list [{"type","itemId","textEn"}, ...] do
# Android gửi kèm — CHÍNH LÀ toàn bộ nội dung cần tổng hợp giọng, KHÔNG có
# nguồn nào khác (server không tự đi hỏi Supabase). Chỉ được LƯU LẦN ĐẦU
# job được tạo — nếu job đã tồn tại (trùng contentHash = trùng nội dung),
# KHÔNG ghi đè items_json cũ, vì về mặt logic 2 lần gửi với cùng
# contentHash PHẢI có items giống hệt nhau (contentHash được tính từ chính
# text đó ở phía Android — xem TtsMyReadingContentHash.kt).
def create_or_get_job(reading_id: str, sid: int, content_hash: str, items_json: str) -> str:
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
        conn.commit()
        row = conn.execute(
            """
            SELECT status FROM tts_myreading_jobs
            WHERE reading_id = ? AND sid = ? AND content_hash = ?
            """,
            (reading_id, sid, content_hash),
        ).fetchone()
    return row[0] if row else "pending"


# ── Lấy lại items đã lưu của 1 job — dùng bởi job processor (bước 8, chưa
# làm) để có text cần generate, KHÔNG cần hỏi Supabase hay bất kỳ nguồn nào
# khác. Trả về [] nếu job không tồn tại (không throw). ────────────────────
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


# ── Gọi từ GET /tts/myreading/status — chỉ ĐỌC, không tạo job mới. Trả về
# None nếu chưa từng có job nào khớp đúng 3 giá trị này (chưa từng
# requestSynthesis(), hoặc contentHash đã đổi vì nội dung bài thay đổi) —
# caller (main.py) tự quyết định map None thành giá trị gì trả về Android. ──
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


# ── Cập nhật status — dùng bởi job processor (bước 8, chưa làm) khi
# generate/encode/upload xong (→ 'ready') hoặc lỗi (→ 'failed'). Đặt sẵn ở
# đây từ bước 3 dù chưa có nơi gọi, để không phải sửa lại schema/module này
# lần nữa ở bước 8. ──────────────────────────────────────────────────────
def update_job_status(reading_id: str, sid: int, content_hash: str, status: str) -> None:
    conn = _require_conn()
    with _lock:
        conn.execute(
            """
            UPDATE tts_myreading_jobs
            SET status = ?, updated_at = ?
            WHERE reading_id = ? AND sid = ? AND content_hash = ?
            """,
            (status, _now_iso(), reading_id, sid, content_hash),
        )
        conn.commit()


# ── Toàn bộ job đang pending/processing — dùng bởi job processor (bước 8)
# để biết cần xử lý những gì, KHÔNG cần server tự nhớ thêm ở đâu khác. ─────
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
