# main.py
# Server MyReading TTS — BƯỚC 4a/9: server KHÔNG BAO GIỜ nói chuyện với
# Supabase — toàn bộ text cần tổng hợp giọng (items: sentence/word/phrase)
# được Android gửi kèm THẲNG trong request, cho CẢ guest lẫn user đã đăng
# nhập. Server chỉ lưu lại đúng những gì nhận được, không tự đi hỏi nguồn
# nào khác. Vẫn CHƯA có: generate audio thật, encode, đóng gói zip, upload
# Drive, job processor chạy nền (status vẫn "pending" mãi cho tới bước 8).
#
# ── HỢP ĐỒNG (khớp đúng comment đầu TtsMyReadingRequestClient.kt) ─────────
#   POST /tts/myreading/request
#     body: {"readingId": "...", "sid": <int>, "contentHash": "..."}
#     resp: {"status": "pending" | "processing" | "ready" | "failed"}
#
#   GET /tts/myreading/status?readingId=...&sid=...&contentHash=...
#     resp: {"status": "pending" | "processing" | "ready" | "failed"}
#
# ── CÀI ĐẶT ─────────────────────────────────────────────────────────────
#   pip install fastapi "uvicorn[standard]"
#
# ── CHẠY THỬ (local) ────────────────────────────────────────────────────
#   uvicorn main:app --host 0.0.0.0 --port 8000 --reload
#
# ── TEST NHANH BẰNG curl ────────────────────────────────────────────────
#   curl -X POST http://localhost:8000/tts/myreading/request \
#        -H "Content-Type: application/json" \
#        -d '{"readingId":"r_test_001","sid":2,"contentHash":"abc123"}'
#
#   curl "http://localhost:8000/tts/myreading/status?readingId=r_test_001&sid=2&contentHash=abc123"
#
# ── NỐI VỚI ANDROID ─────────────────────────────────────────────────────
# Sau khi server chạy và có URL public (vd qua Cloudflare Tunnel / ngrok /
# deploy lên VPS có domain), điền URL đó vào local.properties:
#   TTS_MYREADING_API_BASE_URL=https://<url-server-của-bạn>
# (KHÔNG có dấu "/" ở cuối — xem TtsMyReadingConfig.kt)
#
# Các bước sau (2-6, xem phần tổng kết đã gửi) sẽ thay dần phần "trả cứng
# pending" ở đây bằng: DB job thật + dedup, lấy text Supabase, generate
# audio Kokoro, đóng gói zip, upload Drive, cập nhật status thật.

import json
import logging
from enum import Enum

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from tts_server import jobs, job_processor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tts_myreading_server")

app = FastAPI(title="eLeap MyReading TTS Server", version="0.4.0-step8")


@app.on_event("startup")
def _on_startup() -> None:
    jobs.init_db()
    logger.info("startup: đã khởi tạo DB job tại %s", jobs.DB_PATH)

    # ── Bước 8: khởi động worker thread xử lý nền (generate→encode→zip→
    # upload) + tự khôi phục job dở dang từ lần chạy trước. Sau dòng này,
    # status KHÔNG còn mãi mãi "pending" nữa — job sẽ tự chuyển
    # processing→ready/failed theo tiến độ xử lý thật. ─────────────────────
    job_processor.start_worker()
    logger.info("startup: đã khởi động job worker nền")


# ── Enum status — khớp 1:1 với TtsMyReadingJobStatus.kt bên Android (chữ
# thường, đúng 4 giá trị: pending/processing/ready/failed). ────────────────
class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


# ── Request/response models — Pydantic tự validate JSON, tự trả lỗi 422
# rõ ràng nếu Android gửi thiếu field, không cần tự viết code kiểm tra. ─────
class MyReadingItem(BaseModel):
    # ── Khớp 1:1 dataclass Item trong step1_generate_kokoro_audio.py /
    # step2_rename_and_encode.py (item_type, item_id, text_en) — CỐ Ý đặt
    # tên field khác kiểu (camelCase cho JSON, snake_case cho Python nội
    # bộ) để không phải đổi style code Python đã có, việc map field sẽ làm
    # ở bước 5 (job processor) khi chuyển MyReadingItem -> Item. ────────────
    type: str = Field(..., pattern="^(sentence|word|phrase)$")
    itemId: str = Field(..., min_length=1)
    textEn: str


class SynthesisRequest(BaseModel):
    readingId: str = Field(..., min_length=1)
    sid: int
    contentHash: str = Field(..., min_length=1)
    # ── TOÀN BỘ nội dung cần tổng hợp giọng — server KHÔNG có nguồn nào
    # khác để lấy text (không Supabase, không SQLite local). Cho phép rỗng
    # ở tầng validate (Android tự đảm bảo không gửi rỗng — xem
    # TtsMyReadingDownloadGate.kt: guard sentences.isEmpty() đã có sẵn phía
    # Android), server chỉ log cảnh báo nếu nhận được rỗng, không chặn cứng.
    items: list[MyReadingItem] = Field(default_factory=list)


class StatusResponse(BaseModel):
    status: JobStatus


@app.post("/tts/myreading/request", response_model=StatusResponse)
async def request_synthesis(body: SynthesisRequest) -> StatusResponse:
    if not body.items:
        # Không chặn cứng (422) — chỉ cảnh báo. Job vẫn được tạo, nhưng job
        # processor (bước 8) sẽ không có gì để generate, tự đánh "failed".
        # Giữ hành vi "không throw" nhất quán với toàn bộ server này.
        logger.warning(
            "request_synthesis: readingId=%s sid=%d nhận items RỖNG — kiểm tra lại "
            "phía Android (TtsMyReadingSyncTrigger) có build đúng items chưa",
            body.readingId, body.sid,
        )

    items_json = json.dumps([item.model_dump() for item in body.items], ensure_ascii=False)

    # ── Tạo job nếu chưa có (INSERT OR IGNORE), hoặc trả về status của job
    # ĐÃ TỒN TẠI nếu (readingId, sid, contentHash) này đã được xin trước đó
    # — an toàn khi Android gọi lặp lại (retry mạng, mở bài nhiều lần...).
    # Job vừa tạo LUÔN có status='pending' — CHƯA có gì xử lý nó cho tới
    # bước 8 (job processor). ────────────────────────────────────────────
    #
    # ⚠️ BỌC RÕ RÀNG: đây là bước "lưu request xuống DB" — Android chỉ nên
    # coi request là ĐÃ ĐƯỢC SERVER NHẬN khi nhận về HTTP 200 kèm status hợp
    # lệ (xem TtsMyReadingSentRequestStore.kt phía Android, chỉ ghi nhớ "đã
    # gửi" SAU KHI có response thành công). Nếu ghi DB lỗi (đĩa đầy, DB
    # khoá, lỗi I/O...), PHẢI trả lỗi HTTP thật (503) để Android hiểu "chưa
    # lưu được, cần thử lại" — TUYỆT ĐỐI không được nuốt lỗi rồi trả về 1
    # status giả (vd mặc định 'pending') vì Android sẽ tưởng nhầm là server
    # đã nhận và ghi nhớ "đã gửi", trong khi thực ra job không hề tồn tại
    # trong DB — bài đó sẽ mãi mãi không bao giờ có audio.
    try:
        status = jobs.create_or_get_job(body.readingId, body.sid, body.contentHash, items_json)
    except Exception:
        logger.exception(
            "request_synthesis: LỖI GHI DB cho readingId=%s sid=%d contentHash=%s — "
            "trả 503, Android cần thử lại sau",
            body.readingId, body.sid, body.contentHash,
        )
        raise HTTPException(
            status_code=503,
            detail="Server không lưu được request lúc này, vui lòng thử lại sau.",
        )

    logger.info(
        "request_synthesis: readingId=%s sid=%d contentHash=%s items=%d → status=%s",
        body.readingId, body.sid, body.contentHash, len(body.items), status,
    )

    # ── CHỈ enqueue khi job THẬT SỰ vừa được tạo mới (status vừa đọc lại là
    # 'pending') — job đã tồn tại từ trước (đang 'processing'/'ready'/'failed'
    # do trùng request) KHÔNG cần enqueue lại, tránh xử lý trùng vô ích. Job
    # 'pending' từ TRƯỚC đó (vd do server restart giữa chừng trước khi có
    # bước 8) đã tự được enqueue lại ở start_worker() lúc khởi động — an
    # toàn nếu lỡ enqueue trùng ở đây (worker xử lý lại từ đầu, không hỏng
    # gì, chỉ tốn thêm 1 lượt xử lý). ────────────────────────────────────
    if status == JobStatus.PENDING.value:
        job_processor.enqueue_job(body.readingId, body.sid, body.contentHash)

    return StatusResponse(status=JobStatus(status))


@app.get("/tts/myreading/status", response_model=StatusResponse)
async def check_status(readingId: str, sid: int, contentHash: str) -> StatusResponse:
    status = jobs.get_job_status(readingId, sid, contentHash)

    if status is None:
        # Không tìm thấy job nào khớp đúng 3 giá trị này — nghĩa là CHƯA
        # TỪNG có ai gọi requestSynthesis() với đúng bộ này (hoặc nội dung
        # bài đã đổi, contentHash mới không khớp job cũ). Trả "failed" —
        # Android (TtsMyReadingRequestClient) tự hiểu đây là "chưa sẵn
        # sàng", KHÔNG throw/crash gì cả (xem TtsMyReadingDownloadGate: chỉ
        # có READY mới cho tiến hành, mọi giá trị khác đều "bỏ qua lượt
        # này, thử lại lần sau").
        logger.warning(
            "check_status: KHÔNG tìm thấy job readingId=%s sid=%d contentHash=%s → trả 'failed'",
            readingId, sid, contentHash,
        )
        return StatusResponse(status=JobStatus.FAILED)

    logger.info(
        "check_status: readingId=%s sid=%d contentHash=%s → status=%s",
        readingId, sid, contentHash, status,
    )
    return StatusResponse(status=JobStatus(status))


@app.get("/health")
async def health() -> dict:
    # Tiện ích nhỏ — dùng để kiểm tra server còn sống khi deploy (uptime
    # monitor, hoặc tự tay curl để debug nhanh không cần nhớ path đầy đủ).
    return {"ok": True}


@app.get("/debug/jobs")
async def debug_list_jobs() -> dict:
    # ── CHỈ để bạn tự kiểm tra trong lúc dev (xem dedup có hoạt động đúng
    # không, job có bị tạo trùng không) — KHÔNG có trong hợp đồng Android,
    # không cần bảo mật gì thêm ở giai đoạn local/ngrok hiện tại. Cân nhắc
    # xoá hoặc thêm auth trước khi deploy thật lên server public lâu dài.
    return {"unfinished_jobs": jobs.get_unfinished_jobs()}


@app.get("/debug/job-items")
async def debug_job_items(readingId: str, sid: int, contentHash: str) -> dict:
    # ── Xem lại ĐÚNG items Android đã gửi cho 1 job cụ thể — dùng để xác
    # nhận Android build đúng danh sách sentence/word/phrase (bước 4b, phía
    # Android) trước khi tin tưởng chuyển sang bước 5 (generate audio thật).
    items = jobs.get_job_items(readingId, sid, contentHash)
    return {"count": len(items), "items": items}