# tts_server/job_processor.py
#
# Bước 8/9 (phần 2): nối Bước 5 (KokoroVoiceEngine.generate) → Bước 6
# (audio_encode.encode_item_to_cache) → Bước 7 (drive_upload.package_job_to_zip
# + upload_or_replace_zip) thành 1 pipeline xử lý TRỌN VẸN 1 job, chạy NỀN
# bằng 1 THREAD WORKER DUY NHẤT xử lý TUẦN TỰ (KHÔNG song song) — vì
# sherpa_onnx.OfflineTts (bên trong KokoroVoiceEngine) KHÔNG đảm bảo an toàn
# khi nhiều luồng gọi generate() đồng thời trên CÙNG 1 model instance (xem
# ghi chú gốc ở kokoro_engine.py/step1_generate_kokoro_audio.py).
#
# ── VÌ SAO DÙNG threading.Thread + queue.Queue (KHÔNG dùng asyncio.Queue) ──
# generate() là hàm ĐỒNG BỘ, CHẶN (blocking CPU-bound) — chạy trực tiếp nó
# trong event loop của FastAPI (async def) sẽ ĐÓNG BĂNG toàn bộ server (mọi
# request khác, kể cả /health, phải đợi). Dùng 1 thread nền riêng, tách hẳn
# khỏi event loop — request POST /tts/myreading/request chỉ enqueue (rất
# nhanh, không chặn) rồi trả response ngay, xử lý thật diễn ra ở thread này.
#
# ── MODEL LOAD 1 LẦN DUY NHẤT ────────────────────────────────────────────
# KokoroVoiceEngine load model khá tốn thời gian (build_tts()) — tạo đúng 1
# instance khi worker thread khởi động, TÁI SỬ DỤNG cho mọi job xử lý sau đó
# trong suốt vòng đời server (không load lại mỗi job).
#
# ── AN TOÀN LỖI TỪNG ITEM ─────────────────────────────────────────────────
# 1 item generate/encode lỗi KHÔNG làm hỏng cả job — bỏ qua item đó, log rõ,
# tiếp tục các item còn lại. Job chỉ 'failed' toàn bộ nếu KHÔNG CÒN item nào
# encode thành công (không có gì để đóng gói zip) hoặc bước zip/upload lỗi.
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

# ── Chu kỳ chạy dọn job đã XONG (ready/failed) quá hạn TTL — xem
# jobs.cleanup_finished_jobs()/FINISHED_JOB_TTL_HOURS. Chạy 1 thread nền
# RIÊNG với worker thread xử lý job (KHÔNG dùng chung _worker_loop) — dọn
# dẹp không liên quan gì tới việc generate/encode/upload, tách riêng để lỗi
# ở bên này (nếu có) không ảnh hưởng tới việc xử lý job thật, và ngược lại.
# 1 giờ là đủ dày để DB không phình to giữa 2 lần dọn, nhưng đủ thưa để
# không tốn tài nguyên vô ích (so với TTL 48h, chạy mỗi giờ vẫn rất dư dả).
CLEANUP_INTERVAL_SECONDS = 60 * 60

# ── Hàng đợi job — mỗi phần tử là 1 tuple (reading_id, sid, content_hash).
# unbounded (maxsize=0) — số job đang chờ trong thực tế (1 người dùng cá
# nhân) rất nhỏ, không cần giới hạn kích thước hàng đợi ở giai đoạn này. ────
_job_queue: "queue.Queue[tuple[str, int, str]]" = queue.Queue()

_worker_thread: Optional[threading.Thread] = None
_cleanup_thread: Optional[threading.Thread] = None
_engine: Optional[KokoroVoiceEngine] = None
_engine_lock = threading.Lock()


def _get_engine() -> Optional[KokoroVoiceEngine]:
    """Load KokoroVoiceEngine LƯỜI (lazy) + AN TOÀN đa luồng — chỉ load 1
    lần dù enqueue_job() và worker thread có race nhau gọi tới đây. Trả về
    None nếu model_dir chưa cấu hình hoặc load lỗi — caller (worker loop) tự
    hiểu là "không thể xử lý job nào cả", đánh failed toàn bộ thay vì crash
    worker thread (crash worker thread sẽ làm MỌI job sau đó mãi mãi pending).
    """
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


def _process_one_job(reading_id: str, sid: int, content_hash: str) -> None:
    label = f"reading_id={reading_id} sid={sid} content_hash={content_hash}"
    logger.info("_process_one_job: bắt đầu %s", label)
    jobs.update_job_status(reading_id, sid, content_hash, "processing")

    items = jobs.get_job_items(reading_id, sid, content_hash)
    if not items:
        logger.warning("_process_one_job: %s không có items nào đã lưu → 'failed'", label)
        jobs.update_job_status(reading_id, sid, content_hash, "failed")
        return

    engine = _get_engine()
    if engine is None:
        jobs.update_job_status(reading_id, sid, content_hash, "failed")
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

        gen_result = engine.generate(text=text_en, sid=sid)
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
        logger.error("_process_one_job: %s KHÔNG có item nào encode thành công (%d lỗi) → 'failed'", label, failed_count)
        jobs.update_job_status(reading_id, sid, content_hash, "failed")
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
    except Exception:
        logger.exception("_process_one_job: %s lỗi đóng gói zip → 'failed'", label)
        jobs.update_job_status(reading_id, sid, content_hash, "failed")
        return

    upload_result = upload_or_replace_zip(
        zip_path=zip_path,
        filename=f"{reading_id}_{sid}.zip",
        folder_id=CONFIG.drive_folder_id,
        token_path=str(CONFIG.drive_oauth_token_path),
    )

    if not upload_result.success:
        logger.error("_process_one_job: %s upload Drive lỗi: %s → 'failed'", label, upload_result.error)
        jobs.update_job_status(reading_id, sid, content_hash, "failed")
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
        except Exception:
            # Lỗi KHÔNG LƯỜNG TRƯỚC ở tầng ngoài cùng — KHÔNG được để worker
            # thread chết (nếu chết, MỌI job enqueue sau đó mãi mãi nằm
            # trong queue, không bao giờ được xử lý, mà server không tự biết
            # để báo lỗi ở đâu). Log đầy đủ, đánh job này failed, rồi vòng
            # lặp tiếp tục nhận job kế tiếp bình thường.
            logger.exception(
                "_worker_loop: lỗi KHÔNG LƯỜNG TRƯỚC khi xử lý reading_id=%s sid=%d content_hash=%s",
                reading_id, sid, content_hash,
            )
            try:
                jobs.update_job_status(reading_id, sid, content_hash, "failed")
            except Exception:
                logger.exception("_worker_loop: lỗi cả khi cố ghi status 'failed'")
        finally:
            _job_queue.task_done()


def _cleanup_loop() -> None:
    """Vòng lặp nền RIÊNG, chạy song song _worker_loop — định kỳ mỗi
    CLEANUP_INTERVAL_SECONDS gọi jobs.cleanup_finished_jobs() để dọn job
    ready/failed đã quá TTL. Ngủ TRƯỚC khi dọn lần đầu (không cần dọn ngay
    lúc server vừa khởi động, DB vừa mới còn sạch) — vòng lặp vô hạn, lỗi ở
    1 lần dọn KHÔNG được làm chết thread này (tương tự nguyên tắc bảo vệ
    _worker_loop), chỉ log rồi chờ chu kỳ sau thử lại.
    """
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
    """Đưa 1 job vào hàng đợi xử lý nền — gọi từ main.py NGAY SAU
    jobs.create_or_get_job() khi job vừa được TẠO MỚI (status vừa trả về là
    'pending' LẦN ĐẦU). AN TOÀN gọi lặp lại cho cùng 1 job (vd request trùng
    tới trước khi job đầu xử lý xong) — worker sẽ chỉ tốn công xử lý lại từ
    đầu (KHÔNG hỏng gì), nhưng main.py NÊN chỉ gọi khi job thực sự mới để
    tránh lãng phí — xem wiring ở main.py.
    """
    _job_queue.put((reading_id, sid, content_hash))
    logger.info("enqueue_job: đã thêm reading_id=%s sid=%d content_hash=%s vào hàng đợi", reading_id, sid, content_hash)


def start_worker() -> None:
    """Gọi 1 LẦN lúc server khởi động (main.py, sự kiện startup) — khởi tạo
    thread worker nền. AN TOÀN gọi lặp lại (vd --reload của uvicorn có thể
    trigger startup event nhiều lần) — no-op nếu thread đã chạy.
    """
    global _worker_thread, _cleanup_thread
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_worker_loop, name="tts-job-worker", daemon=True)
        _worker_thread.start()

        # ── Khôi phục job dở dang từ lần chạy trước (server bị tắt/restart
        # giữa chừng khi đang có job 'pending'/'processing') — KHÔNG để
        # chúng nằm im mãi mãi trong DB, tự enqueue lại để worker xử lý
        # tiếp. Job đang 'processing' cũ (dở dang thật sự, không phải chỉ
        # mới ghi status) sẽ được xử lý LẠI TỪ ĐẦU (generate lại toàn bộ) —
        # chấp nhận được vì generate() không có tác dụng phụ gì ngoài tốn
        # thời gian CPU. ────────────────────────────────────────────────
        unfinished = jobs.get_unfinished_jobs()
        if unfinished:
            logger.info("start_worker: khôi phục %d job dở dang từ lần chạy trước", len(unfinished))
            for reading_id, sid, content_hash in unfinished:
                enqueue_job(reading_id, sid, content_hash)

    # ── Guard RIÊNG cho cleanup thread — tách khỏi guard của _worker_thread
    # ở trên để 2 thread độc lập nhau: dù 1 trong 2 vì lý do gì đó đã chết
    # (không nên xảy ra vì cả 2 đều tự bắt Exception trong vòng lặp, nhưng
    # phòng hờ), gọi lại start_worker() vẫn tự khởi động lại đúng thread bị
    # chết mà không đụng tới thread còn lại đang chạy tốt. ─────────────────
    if _cleanup_thread is None or not _cleanup_thread.is_alive():
        _cleanup_thread = threading.Thread(target=_cleanup_loop, name="tts-job-cleanup", daemon=True)
        _cleanup_thread.start()