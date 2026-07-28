# tts_server/engines/base.py
#
# Đặt tại: tts_server/engines/base.py
#
# Interface DUY NHẤT mà mọi phần khác của server (DB job, packaging, Drive
# upload, process_job()) được phép gọi để "xin audio cho 1 đoạn text". Đây
# là phần khung quan trọng nhất cho mục tiêu "sau này nâng cấp/đổi giọng
# đọc dễ dàng" — mọi engine cụ thể (Kokoro, hay dịch vụ TTS khác sau này)
# chỉ cần implement đúng class này, KHÔNG đụng gì tới DB job, Supabase/
# items, encode, zip, hay Drive uploader.
#
# ⚠️ CHƯA CÓ IMPLEMENTATION THẬT ở file này — đây chỉ là "hợp đồng" (giống
# interface/abstract class). Bước 5 sẽ viết KokoroVoiceEngine implement
# đúng class VoiceEngine bên dưới, gọi thẳng vào 3 file step1/step2/step3
# đã có sẵn (không copy logic sang).
#
# ── CÁCH DÙNG DỰ KIẾN (bước 5 trở đi) ──────────────────────────────────
#   engine = KokoroVoiceEngine(model_dir=..., num_threads=...)
#   result = engine.generate(text="Hello world.", sid=2, speed=1.0)
#   # result.wav_path  -> đường dẫn file .wav vừa sinh ra
#   # result.success   -> True/False
#   engine.close()
#
# Sau này muốn đổi sang engine khác (model mới, dịch vụ TTS ngoài...):
# chỉ cần viết 1 class mới implement đúng VoiceEngine, không sửa gì ở
# process_job()/jobs.py/packaging/Drive upload.
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class VoiceGenerationResult:
    """Kết quả sinh audio cho 1 đoạn text — engine nào cũng phải trả về
    đúng dạng này, bất kể bên trong dùng Kokoro hay engine khác.

    success:    True nếu sinh audio thành công, False nếu lỗi (model lỗi,
                text rỗng, timeout...). Khi False, wav_path PHẢI là None —
                caller (process_job(), bước 8) dựa vào đúng cờ này để quyết
                định đánh dấu job 'failed', không dựa vào việc thử mở file.
    wav_path:   đường dẫn tuyệt đối tới file .wav vừa sinh ra (chưa encode
                OGG/Opus — việc đó là của bước 6, step2_rename_and_encode.py).
                None nếu success=False.
    error:      thông điệp lỗi ngắn gọn (để log/debug) — None nếu thành
                công. KHÔNG bắt buộc set khi success=False (có thể None nếu
                engine không có gì cụ thể để báo), nhưng nên set khi có.
    """
    success: bool
    wav_path: Optional[str] = None
    error: Optional[str] = None


class VoiceEngine(ABC):
    """Interface mọi engine sinh giọng đọc phải implement.

    KHÔNG được giả định gì về Kokoro cụ thể ở đây (không tham số nào tên
    "kokoro", không import gì từ step1/step2/step3) — interface này phải
    trung lập, để sau này 1 engine hoàn toàn khác (vd gọi API TTS ngoài)
    cũng implement được mà không cần sửa lại chữ ký hàm.
    """

    @abstractmethod
    def generate(self, text: str, sid: int, item_type: str) -> VoiceGenerationResult:
        """Sinh audio cho ĐÚNG 1 đoạn text (1 item: sentence/word/phrase).

        text:      nội dung cần đọc (textEn của 1 item — xem MyReadingItem ở
                   main.py). KHÔNG rỗng — caller nên tự lọc item rỗng trước
                   khi gọi tới đây.
        sid:       speaker id / chỉ số giọng — khớp đúng ý nghĩa `sid` đã
                   dùng xuyên suốt pipeline (TtsMyReadingRequestClient.kt,
                   jobs.py, step1_generate_kokoro_audio.py).
        item_type: "word" | "sentence" | "phrase" — engine tự tra tốc độ
                   đọc tương ứng (xem get_speed_for_type() trong
                   step1_generate_kokoro_audio.py, DUY NHẤT nơi quyết định
                   tốc độ theo loại item). Caller KHÔNG tự tính/truyền speed
                   nữa — mọi engine implement interface này đều nhận
                   item_type để tự quyết định tốc độ nhất quán giữa server
                   MyReading và batch script bài hệ thống.

        Trả về VoiceGenerationResult — KHÔNG throw exception ra ngoài trong
        trường hợp lỗi sinh audio thông thường (model không handle được
        text, timeout...) — chỉ throw nếu lỗi cấu hình nghiêm trọng (model
        chưa load được lúc khởi tạo). Đây là lựa chọn có chủ đích: caller
        (process_job(), bước 8) xử lý lỗi từng item bằng cách đọc
        result.success, không cần try/except quanh MỌI lời gọi generate().
        """
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """Giải phóng tài nguyên (model đã load, thread pool...).

        Gọi 1 lần khi process_job() xử lý xong 1 job (hoặc khi server tắt,
        tuỳ thiết kế bước 8) — KHÔNG bắt buộc gọi sau MỖI lần generate(),
        vì việc load lại model cho mỗi item sẽ rất chậm (đúng lý do
        build_tts() trong step1_generate_kokoro_audio.py chỉ gọi 1 lần rồi
        tái dùng cho nhiều item, xem generate_with_fallback()).
        """
        raise NotImplementedError