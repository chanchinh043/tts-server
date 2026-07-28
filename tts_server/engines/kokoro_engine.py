# tts_server/engines/kokoro_engine.py
#
# Implement VoiceEngine (base.py) — LỚP BỌC MỎNG quanh
# step1_generate_kokoro_audio.py đã có sẵn. KHÔNG copy logic sang đây, chỉ
# import và gọi thẳng build_tts()/generate_with_fallback()/save_wav() — đúng
# thoả thuận đã chốt: "3 file gốc giữ nguyên 100%, không sửa gì cả, server
# chỉ import đúng những hàm xử lý audio thuần (không đụng DB)".
#
# ⚠️ VỊ TRÍ ĐẶT 3 FILE STEP1/STEP2/STEP3 — BẮT BUỘC theo cấu trúc:
#   tts_server/
#     engines/
#       base.py
#       kokoro_engine.py     ← file này
#     pipeline/
#       step1_generate_kokoro_audio.py   ← copy y nguyên, KHÔNG sửa
#       step2_rename_and_encode.py       ← copy y nguyên (dùng ở bước 6)
#       step3_package_zip.py             ← copy y nguyên (dùng ở bước 7)
#
# KHÔNG cần tts_server/pipeline/__init__.py — import ở đây dùng cách chèn
# thẳng thư mục pipeline/ vào sys.path rồi import theo tên file gốc (không
# qua dotted-package), để 3 file step1/2/3 KHÔNG cần sửa bất kỳ dòng nào
# (chúng vốn được viết như script độc lập, không có `from . import ...`).
#
# ⚠️ KHÔNG dùng MODEL_DIR/READINGS_DB/MYREADING_DB hardcode trong
# step1_generate_kokoro_audio.py — những biến đó chỉ là giá trị MẶC ĐỊNH cho
# argparse khi chạy step1 độc lập bằng dòng lệnh, KHÔNG được build_tts()/
# generate_with_fallback() dùng tới. model_dir ở đây LUÔN do server tự
# truyền vào qua constructor KokoroVoiceEngine(model_dir=...), đọc từ cấu
# hình server (biến môi trường/config riêng), không đụng gì tới file step1.
from __future__ import annotations

import logging
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from .base import VoiceEngine, VoiceGenerationResult

logger = logging.getLogger("tts_server.kokoro_engine")

# ── Chèn thư mục pipeline/ (cạnh engines/, cùng cấp trong tts_server/) vào
# sys.path — để `import step1_generate_kokoro_audio` hoạt động như khi chạy
# trực tiếp file đó bằng dòng lệnh, không cần biến nó thành package con. ────
_PIPELINE_DIR = Path(__file__).resolve().parent.parent / "pipeline"
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

try:
    from step1_generate_kokoro_audio import (  # type: ignore
        build_tts,
        generate_with_fallback,
        save_wav,
    )
except ImportError as e:
    raise ImportError(
        f"Không import được step1_generate_kokoro_audio.py từ {_PIPELINE_DIR}. "
        f"Kiểm tra lại: (1) file đã được copy vào đúng tts_server/pipeline/ chưa, "
        f"(2) đã 'pip install sherpa-onnx numpy' chưa (step1 tự sys.exit nếu thiếu "
        f"sherpa-onnx). Lỗi gốc: {e}"
    ) from e


class KokoroVoiceEngine(VoiceEngine):
    """Engine sinh giọng đọc bằng Kokoro (sherpa-onnx) — implement đúng
    VoiceEngine ở base.py. Load model 1 LẦN DUY NHẤT lúc khởi tạo (build_tts
    khá tốn thời gian), tái sử dụng cho mọi lần generate() sau đó — đúng lý
    do step1_generate_kokoro_audio.py gốc cũng chỉ gọi build_tts() 1 lần rồi
    generate cho hàng ngàn item trong vòng lặp.

    Muốn đổi sang engine khác sau này (model mới/dịch vụ TTS ngoài): viết 1
    class mới cùng thư mục engines/, implement VoiceEngine, KHÔNG đụng gì
    tới class này hay 3 file step1/2/3.
    """

    def __init__(
        self,
        model_dir: str,
        num_threads: int = 2,
        wav_tmp_dir: Optional[str] = None,
    ):
        """
        model_dir:   thư mục chứa model.onnx/voices.bin/tokens.txt/... — do
                     SERVER tự cấu hình (biến môi trường/config riêng của
                     server, KHÔNG phải MODEL_DIR hardcode trong step1).
        num_threads: số luồng CPU cho model — nên lấy từ config server, có
                     thể khác giá trị NUM_THREADS mặc định trong step1.
        wav_tmp_dir: thư mục ghi file .wav TẠM (trước khi bước 6 encode sang
                     OGG rồi xoá) — mặc định dùng thư mục hệ thống
                     (tempfile.gettempdir()) / "tts_myreading_wav", server có
                     thể tự dọn định kỳ nếu muốn.
        """
        logger.info("KokoroVoiceEngine: đang load model từ %s (num_threads=%d)", model_dir, num_threads)
        self._tts = build_tts(Path(model_dir), num_threads)
        logger.info("KokoroVoiceEngine: model sẵn sàng")

        self._wav_tmp_dir = Path(wav_tmp_dir) if wav_tmp_dir else Path(tempfile.gettempdir()) / "tts_myreading_wav"
        self._wav_tmp_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, text: str, sid: int, item_type: str) -> VoiceGenerationResult:
        if not text or not text.strip():
            return VoiceGenerationResult(success=False, error="text rỗng, không có gì để generate")

        try:
            # generate_with_fallback() tự thử giọng gốc (sid) trước, hết
            # MAX_ATTEMPTS_PER_VOICE lần vẫn câm thì tự chuyển sang giọng
            # thay thế cùng nhóm (VOICE_GROUPS) — xem step1, KHÔNG cần logic
            # fallback riêng ở đây, gọi thẳng là đủ. Tốc độ đọc cũng do
            # step1 tự tra theo item_type (get_speed_for_type()) — KHÔNG
            # còn truyền speed từ tầng này nữa, xem base.py.
            result = generate_with_fallback(self._tts, text, sid, item_type)
        except Exception as e:
            logger.exception("generate: lỗi khi gọi generate_with_fallback (sid=%d)", sid)
            return VoiceGenerationResult(success=False, error=f"lỗi generate: {e}")

        if result is None:
            # HẾT TOÀN BỘ chuỗi giọng thay thế mà vẫn câm — xem
            # SILENCE_AMPLITUDE_THRESHOLD/VOICE_GROUPS trong step1.
            return VoiceGenerationResult(
                success=False,
                error=f"hết chuỗi giọng thay thế, vẫn câm cho sid={sid}",
            )

        samples, sample_rate, used_sid = result
        if used_sid != sid:
            logger.warning(
                "generate: sid=%d câm, đã tự chuyển sang giọng thay thế sid=%d cho text=%r",
                sid, used_sid, text[:60],
            )

        # Ghi WAV tạm — tên file random (uuid4) vì ở tầng này KHÔNG biết
        # itemId/itemType (interface VoiceEngine cố tình trung lập, không
        # nhận itemId — xem base.py). Caller (process_job(), bước 8) chịu
        # trách nhiệm đặt lại đúng tên file chuẩn cache khi encode (bước 6,
        # dùng content_hash() từ step2_rename_and_encode.py) rồi XOÁ file
        # tạm này sau khi encode xong.
        wav_path = self._wav_tmp_dir / f"{uuid.uuid4().hex}.wav"
        try:
            save_wav(wav_path, samples, sample_rate)
        except Exception as e:
            logger.exception("generate: lỗi ghi WAV tạm %s", wav_path)
            return VoiceGenerationResult(success=False, error=f"lỗi ghi WAV: {e}")

        return VoiceGenerationResult(success=True, wav_path=str(wav_path))

    def close(self) -> None:
        # sherpa_onnx.OfflineTts không có API close() tường minh — thả
        # reference để Python tự giải phóng (GC) khi không còn nơi nào giữ.
        # Đặt sẵn hàm này để khớp interface VoiceEngine và để chỗ cho engine
        # khác sau này CÓ thể cần giải phóng tài nguyên thật (vd đóng kết nối
        # HTTP tới dịch vụ TTS ngoài).
        self._tts = None
        logger.info("KokoroVoiceEngine: đã đóng")