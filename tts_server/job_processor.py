# tts_server/job_processor.py
#
# ── CẬP NHẬT: AUTO-RETRY ──────────────────────────────────────────────────
# Trước đây mọi lỗi (generate/encode/zip/upload) đánh 'failed' ngay lập
# tức, không có đường quay lại trừ khi Android tự gửi request MỚI (contentHash
# khác). Giờ thêm _fail_or_retry(): lỗi được coi là TẠM THỜI theo mặc định
# — tự enqueue lại sau 1 khoảng delay tăng dần (backoff), tối đa
# MAX_RETRIES lần. Chỉ khi vượt quá MAX_RETRIES mới đánh 'failed' thật sự
# (lúc đó cần retry THỦ CÔNG — xem jobs.create_or_get_job(), tự reset khi
# Android gọi lại đúng request).
from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Optional

from tts_server import jobs
from tts_server.config import CONFIG
from tts_server.audio_encode import encode_item_to_cache
from tts_server.drive_upload import package_job_to_zip, upload_or_replace_zip
from tts_server.engines.kokoro_engine import KokoroVoiceEngine

logger = logging.getLogger("tts_server.job_processor")

CLEANUP_INTERVAL_SECONDS = 60 * 60

# ── Cấu hình auto-retry ────────────────────────────────────────────────
# MAX_RETRIES=3: job được TỰ ĐỘNG thử lại tối đa 3 lần trước khi đánh
# 'failed' hẳn (tổng cộng 4 lần chạy: 1 lần đầu + 3 lần retry).
# RETRY_DELAYS_SECONDS: delay TRƯỚC lần retry thứ N (backoff tăng dần —
# lỗi mạng/Drive tạm thời thường tự hết sau vài chục giây tới vài phút).
# Nếu retry_count vượt quá độ dài danh sách này, dùng giá trị CUỐI CÙNG lặp
# lại (tránh IndexError nếu sau này tăng MAX_RETRIES mà quên thêm delay).
MAX_RETRIES = 3
RETRY_DELAYS_SECONDS = [30, 120, 300]  # 30s, 2 phút, 5 phút

_job_queue: "queue.Queue[tuple[str, int, str]]" = queue.Queue()

_worker_thread: Optional[threading.Thread] = None
_cleanup_thread: Optional[threading.Thread] = None
_engine: Optional[KokoroVoiceEngine] = None
_engine_lock = threading.Lock()


def _get_engine() -> Optional[KokoroVoiceEngine]:
    global _engine
    if _engine is not None:
        return _engine

    with _engine_lock:
        if _engine is not None:
            return _engine

        if not CONFIG.kokoro_model_dir:
            logger.error(
                "_get_engine: TTS_KOKORO_MODEL_DIR chưa cấu hình — KHÔNG thể load "
                "KokoroVoiceEngine, mọi job sẽ bị đánh 'failed'."
            )
            return None

        try:
            _engine = KokoroVoiceEngine(
                model_dir=CONFIG.kokoro_model_dir,
                num_threads=CONFIG.kokoro_num_threads,
                wav_tmp_dir=str(CONFIG.wav_tmp_dir),
            )
        except Exception:
            logger.exception("_get_engine: lỗi load KokoroVoiceEngine — model_dir=%s", CONFIG.kokoro_model_dir)
            _engine = None

        return _engine


def _fail_or_retry(reading_id: str, sid: int, content_hash: str, error: str) -> None:
    """Gọi thay cho jobs.update_job_status(..., 'failed') ở MỌI điểm lỗi
    trong _process_one_job(). Quyết định: tự enqueue lại (còn lượt retry)
    hay đánh 'failed' thật sự (hết lượt retry).

    Trong lúc CHỜ retry, status được set về 'pending' (không phải giữ
    'processing') — để nếu server bị tắt/restart đúng lúc đang chờ,
    start_worker() ở lần khởi động sau vẫn thấy job này qua
    get_unfinished_jobs() và tự enqueue lại bình thường, không bị timer cũ
    (đã mất theo tiến trình cũ) làm kẹt job mãi mãi.
    """
    label = f"reading_id={reading_id} sid={sid} content_hash={content_hash}"
    new_retry_count = jobs.increment_retry(reading_id, sid, content_hash)

    if new_retry_count <= MAX_RETRIES:
        delay_index = min(new_retry_count - 1, len(RETRY_DELAYS_SECONDS) - 1)
        delay = RETRY_DELAYS_SECONDS[delay_index]
        logger.warning(
            "_fail_or_retry: %s lỗi (lần thử %d/%d): %s — sẽ TỰ THỬ LẠI sau %d giây",
            label, new_retry_count, MAX_RETRIES, error, delay,
        )
        jobs.update_job_status(reading_id, sid, content_hash, "pending", error=error)
        timer = threading.Timer(delay, enqueue_job, args=(reading_id, sid, content_hash))
        timer.daemon = True
        timer.start()
    else:
        logger.error(
            "_fail_or_retry: %s đã hết %d lượt tự động thử lại, đánh 'failed' HẲN. Lỗi cuối: %s",
            label, MAX_RETRIES, error,
        )
        jobs.update_job_status(reading_id, sid, content_hash, "failed", error=error)


def _process_one_job(reading_id: str, sid: int, content_hash: str) -> None:
    label = f"reading_id={reading_id} sid={sid} content_hash={content_hash}"
    logger.info("_process_one_job: bắt đầu %s", label)
    jobs.update_job_status(reading_id, sid, content_hash, "processing")

    items = jobs.get_job_items(reading_id, sid, content_hash)
    if not items:
        # Không có items nghĩa là lỗi dữ liệu (Android gửi thiếu) — KHÔNG
        # phải lỗi tạm thời, retry cũng vô ích. Đánh failed thẳng, không
        # qua _fail_or_retry (tránh tốn 3 lượt retry vô nghĩa).
        logger.warning("_process_one_job: %s không có items nào đã lưu → 'failed'", label)
        jobs.update_job_status(reading_id, sid, content_hash, "failed", error="job không có items nào")
        return

    engine = _get_engine()
    if engine is None:
        # Model chưa load được (cấu hình sai) — CŨNG không phải lỗi tạm
        # thời tự hết, nhưng vẫn cho auto-retry vì có thể server đang khởi
        # động dở/model đang load ở thread khác. Nếu retry hết vẫn lỗi thì
        # người dùng sẽ thấy 'failed' + last_error rõ ràng để tự sửa cấu hình.
        _fail_or_retry(reading_id, sid, content_hash, "KokoroVoiceEngine chưa sẵn sàng (kiểm tra TTS_KOKORO_MODEL_DIR)")
        return

    ogg_paths: list[Path] = []
    failed_count = 0

    for item in items:
        item_type = item.get("type")
        item_id = item.get("itemId")
        text_en = item.get("textEn")

        if not item_type or not item_id or not text_en:
            logger.warning("_process_one_job: %s item thiếu field (type/itemId/textEn): %r → bỏ qua", label, item)
            failed_count += 1
            continue

        gen_result = engine.generate(text=text_en, sid=sid, item_type=item_type)
        if not gen_result.success or not gen_result.wav_path:
            logger.warning(
                "_process_one_job: %s generate lỗi cho item_id=%s type=%s: %s",
                label, item_id, item_type, gen_result.error,
            )
            failed_count += 1
            continue

        enc_result = encode_item_to_cache(
            wav_path=Path(gen_result.wav_path),
            output_dir=CONFIG.output_dir,
            reading_id=reading_id,
            sid=sid,
            item_type=item_type,
            item_id=item_id,
            text_en=text_en,
            opus_bitrate=CONFIG.opus_bitrate,
            ffmpeg_path=CONFIG.ffmpeg_path,
        )
        if not enc_result.success or not enc_result.ogg_path:
            logger.warning(
                "_process_one_job: %s encode lỗi cho item_id=%s type=%s: %s",
                label, item_id, item_type, enc_result.error,
            )
            failed_count += 1
            continue

        ogg_paths.append(enc_result.ogg_path)

    if not ogg_paths:
        # KHÔNG item nào ra được audio — có thể tạm thời (vd ffmpeg bận,
        # engine lỗi thoáng qua) → cho auto-retry thay vì failed ngay.
        _fail_or_retry(
            reading_id, sid, content_hash,
            f"KHÔNG có item nào encode thành công ({failed_count} lỗi / {len(items)} item)",
        )
        return

    if failed_count > 0:
        logger.warning(
            "_process_one_job: %s có %d/%d item lỗi, vẫn tiếp tục đóng gói %d item thành công",
            label, failed_count, len(items), len(ogg_paths),
        )

    try:
        zip_path = package_job_to_zip(
            ogg_paths=ogg_paths,
            zip_output_dir=CONFIG.zip_output_dir,
            reading_id=reading_id,
            sid=sid,
        )
    except Exception as e:
        logger.exception("_process_one_job: %s lỗi đóng gói zip", label)
        _fail_or_retry(reading_id, sid, content_hash, f"lỗi đóng gói zip: {e}")
        return

    upload_result = upload_or_replace_zip(
        zip_path=zip_path,
        filename=f"{reading_id}_{sid}.zip",
        folder_id=CONFIG.drive_folder_id,
        token_path=str(CONFIG.drive_oauth_token_path),
    )

    if not upload_result.success:
        # ⚠️ Đây chính là case OAuth token hết hạn (invalid_grant) đã gặp —
        # với case này, auto-retry 3 lần x (30s/120s/300s) sẽ KHÔNG cứu
        # được (token chết hẳn, không tự hồi phục theo thời gian). Nhưng
        # vẫn cho qua _fail_or_retry bình thường (không phân biệt loại lỗi
        # ở đây — giữ code đơn giản), vì sau khi hết retry và đánh
        # 'failed', cơ chế MANUAL RETRY (jobs.create_or_get_job) vẫn hoạt
        # động: bạn chạy lại oauth_setup.py lấy token mới, rồi lần
        # TtsMyReadingSyncTrigger tiếp theo (hoặc user mở lại bài) tự động
        # kích hoạt lại job này từ đầu, không cần bạn tự tay xoá DB.
        logger.error("_process_one_job: %s upload Drive lỗi: %s", label, upload_result.error)
        _fail_or_retry(reading_id, sid, content_hash, f"lỗi upload Drive: {upload_result.error}")
        return

    jobs.update_job_status(reading_id, sid, content_hash, "ready")
    logger.info(
        "_process_one_job: %s HOÀN TẤT → 'ready' (file_id=%s, %d/%d item thành công)",
        label, upload_result.file_id, len(ogg_paths), len(items),
    )


def _worker_loop() -> None:
    logger.info("_worker_loop: worker thread đã khởi động")
    while True:
        reading_id, sid, content_hash = _job_queue.get()
        try:
            _process_one_job(reading_id, sid, content_hash)
        except Exception as e:
            logger.exception(
                "_worker_loop: lỗi KHÔNG LƯỜNG TRƯỚC khi xử lý reading_id=%s sid=%d content_hash=%s",
                reading_id, sid, content_hash,
            )
            try:
                _fail_or_retry(reading_id, sid, content_hash, f"lỗi không lường trước: {e}")
            except Exception:
                logger.exception("_worker_loop: lỗi cả khi cố ghi status/retry")
        finally:
            _job_queue.task_done()


def _cleanup_loop() -> None:
    logger.info(
        "_cleanup_loop: cleanup thread đã khởi động (mỗi %d giây, TTL=%d giờ)",
        CLEANUP_INTERVAL_SECONDS, jobs.FINISHED_JOB_TTL_HOURS,
    )
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            deleted = jobs.cleanup_finished_jobs()
            if deleted > 0:
                logger.info("_cleanup_loop: đã dọn %d job ready/failed quá hạn TTL", deleted)
        except Exception:
            logger.exception("_cleanup_loop: lỗi khi dọn job quá hạn — thử lại chu kỳ sau")


def enqueue_job(reading_id: str, sid: int, content_hash: str) -> None:
    _job_queue.put((reading_id, sid, content_hash))
    logger.info("enqueue_job: đã thêm reading_id=%s sid=%d content_hash=%s vào hàng đợi", reading_id, sid, content_hash)


def start_worker() -> None:
    global _worker_thread, _cleanup_thread
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_worker_loop, name="tts-job-worker", daemon=True)
        _worker_thread.start()

        unfinished = jobs.get_unfinished_jobs()
        if unfinished:
            logger.info("start_worker: khôi phục %d job dở dang từ lần chạy trước", len(unfinished))
            for reading_id, sid, content_hash in unfinished:
                enqueue_job(reading_id, sid, content_hash)

    if _cleanup_thread is None or not _cleanup_thread.is_alive():
        _cleanup_thread = threading.Thread(target=_cleanup_loop, name="tts-job-cleanup", daemon=True)
        _cleanup_thread.start()