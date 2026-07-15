# tts_server/audio_encode.py
#
# Bước 6/9: nối liền NGAY SAU KokoroVoiceEngine.generate() (bước 5) —
# nhận file .wav TẠM (chưa đúng tên/định dạng cache), gọi thẳng
# content_hash() + encode_to_opus() từ step2_rename_and_encode.py (KHÔNG
# copy logic sang), ghi ra đúng vị trí + tên file mà step3_package_zip.py
# (bước 7) mong đợi, rồi XOÁ file .wav tạm (không cần giữ lại — OGG đã có
# đủ thông tin để phát, giữ WAV chỉ tốn dung lượng).
#
# ⚠️ CẤU TRÚC OUTPUT — PHẢI khớp CHÍNH XÁC những gì step3_package_zip.py
# đang quét (xem run() ở step3: input_dir.iterdir() → reading_dir →
# sid_dir → *.ogg):
#   {output_dir}/{reading_id}/{sid}/{type}_{itemId}_{contentHash}.ogg
#
# ⚠️ contentHash Ở ĐÂY dùng ĐÚNG content_hash() từ step2 — SHA-256(text
# UTF-8), 8 ký tự hex đầu, PHẢI khớp TtsAudioCache.contentHash() bên Kotlin
# từng byte một (xem comment gốc ở step2). KHÔNG được tự tính lại bằng cách
# khác ở đây, kể cả khi trông có vẻ tương đương.
#
# ⚠️ VỊ TRÍ ĐẶT FILE — cùng cấp với engines/ và pipeline/ (xem kokoro_engine.py):
#   tts_server/
#     audio_encode.py     ← file này
#     engines/
#       kokoro_engine.py
#     pipeline/
#       step1_generate_kokoro_audio.py
#       step2_rename_and_encode.py
#       step3_package_zip.py
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tts_server.audio_encode")

# ── Chèn thư mục pipeline/ vào sys.path (giống hệt kokoro_engine.py) — an
# toàn khi gọi lặp lại, đã có guard "not in sys.path". Import content_hash()
# / encode_to_opus() trực tiếp từ step2_rename_and_encode.py, không sửa gì
# file gốc đó. ────────────────────────────────────────────────────────────
_PIPELINE_DIR = Path(__file__).resolve().parent / "pipeline"
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

try:
    from step2_rename_and_encode import content_hash, encode_to_opus  # type: ignore
except ImportError as e:
    raise ImportError(
        f"Không import được step2_rename_and_encode.py từ {_PIPELINE_DIR}. "
        f"Kiểm tra lại file đã được copy vào đúng tts_server/pipeline/ chưa. "
        f"Lỗi gốc: {e}"
    ) from e


@dataclass
class EncodeResult:
    """Kết quả encode 1 item — dùng bởi process_job() (bước 8) để biết item
    này có sẵn sàng đưa vào zip (bước 7) hay không.

    success:     True nếu encode thành công, wav tạm đã được xoá.
    ogg_path:    đường dẫn .ogg cuối cùng (đã đúng tên chuẩn cache), None
                 nếu success=False.
    content_hash: hash 8 ký tự đã dùng để đặt tên — caller có thể cần giá
                 trị này để log/debug, dù đã nằm sẵn trong ogg_path.
    error:       thông điệp lỗi ngắn gọn nếu success=False.
    """
    success: bool
    ogg_path: Optional[Path] = None
    content_hash: Optional[str] = None
    error: Optional[str] = None


def encode_item_to_cache(
    wav_path: Path,
    output_dir: Path,
    reading_id: str,
    sid: int,
    item_type: str,
    item_id: str,
    text_en: str,
    opus_bitrate: str = "32k",
    ffmpeg_path: str = "ffmpeg",
    delete_wav_after: bool = True,
) -> EncodeResult:
    """Encode 1 file .wav tạm (từ KokoroVoiceEngine.generate()) sang .ogg
    đúng tên/vị trí chuẩn cache, rồi xoá .wav tạm.

    wav_path:    file .wav tạm (từ VoiceGenerationResult.wav_path, bước 5).
    output_dir:  thư mục gốc output — TRUYỀN THẲNG làm --input-dir cho
                 step3_package_zip.py ở bước 7, PHẢI là cùng 1 thư mục.
    reading_id, sid, item_type, item_id: dùng để build đúng đường dẫn +
                 tên file — item_type PHẢI là "word"/"sentence"/"phrase"
                 (khớp TtsMyReadingItemType bên Android/MyReadingItem bên
                 main.py — không tự ý dùng giá trị khác).
    text_en:     PHẢI là ĐÚNG NGUYÊN VĂN text đã dùng để generate audio ở
                 bước 5 — content_hash() tính từ chính text này, sai lệch dù
                 chỉ 1 ký tự (thừa khoảng trắng, khác encoding...) sẽ ra hash
                 khác, làm Android không bao giờ khớp được file tải về với
                 đúng item cần phát.
    """
    if not wav_path.exists():
        return EncodeResult(success=False, error=f"wav_path không tồn tại: {wav_path}")

    hash_ = content_hash(text_en)
    dst_ogg = output_dir / reading_id / str(sid) / f"{item_type}_{item_id}_{hash_}.ogg"

    ok = encode_to_opus(wav_path, dst_ogg, opus_bitrate, ffmpeg_path)

    if delete_wav_after:
        try:
            wav_path.unlink(missing_ok=True)
        except Exception as e:
            # Không xoá được wav tạm KHÔNG phải lỗi nghiêm trọng (không ảnh
            # hưởng kết quả .ogg) — chỉ log, không đổi kết quả trả về.
            logger.warning("encode_item_to_cache: không xoá được wav tạm %s: %s", wav_path, e)

    if not ok:
        return EncodeResult(
            success=False,
            content_hash=hash_,
            error=f"ffmpeg encode thất bại cho {wav_path.name} → {dst_ogg.name}",
        )

    return EncodeResult(success=True, ogg_path=dst_ogg, content_hash=hash_)
