# tts_server/config.py
#
# Bước 8/9 (phần 1): nơi DUY NHẤT đọc cấu hình server từ biến môi trường —
# job_processor.py (viết ngay sau file này) và main.py (khi wiring lại
# startup) chỉ import CONFIG từ đây, KHÔNG tự đọc os.environ ở nơi khác,
# tránh rải rác việc đọc config ra nhiều file.
#
# ── QUY ƯỚC ĐƯỜNG DẪN MẶC ĐỊNH — "cạnh tts_server/" ─────────────────────
# Giống đúng quy ước đã dùng ở jobs.py (DB_PATH = parent.parent / "..."):
# file này nằm tại tts_server/config.py, nên PROJECT_ROOT = parent.parent
# là thư mục CHA của tts_server/ — mọi output dir mặc định (wav tạm, ogg,
# zip, credentials) đều đặt ở PROJECT_ROOT, NGANG HÀNG với tts_server/, vd:
#
#   project_root/
#     tts_server/
#       config.py            ← file này
#       jobs.py
#       audio_encode.py
#       drive_upload.py
#       engines/
#       pipeline/
#     tts_output/             ← OUTPUT_DIR mặc định (chứa .ogg theo cấu
#                                trúc {reading_id}/{sid}/*.ogg — DÙNG CHUNG
#                                làm input-dir cho package_job_to_zip())
#     tts_zips/                ← ZIP_OUTPUT_DIR mặc định
#     tts_wav_tmp/              ← WAV_TMP_DIR mặc định
#     credentials/
#       service_account.json   ← CREDENTIALS_PATH mặc định (bạn tự copy vào)
#     tts_myreading_jobs.db     ← đã có sẵn từ jobs.py (không đổi)
#
# Dùng Path (không tự f-string nối chuỗi) — an toàn trên Windows, tự xử lý
# đúng dấu phân cách "\" mà không cần bạn tự escape gì trong biến môi trường.
#
# ── CÁCH GHI ĐÈ ────────────────────────────────────────────────────────
# CÁCH 1 (khuyên dùng — chỉ điền 1 LẦN, không cần gõ lại mỗi lần mở CMD):
# tạo file ".env" NẰM CẠNH main.py (tức ở PROJECT_ROOT, NGOÀI tts_server/),
# mỗi dòng 1 biến dạng KEY=VALUE, vd:
#   TTS_KOKORO_MODEL_DIR=D:\models\kokoro-sherpa-full\kokoro
#   TTS_DRIVE_ROOT_FOLDER_ID=19TaPg0Kpuv1pbLOy8-hNWWVaTRwnGLOT
# File này được _load_dotenv() bên dưới TỰ ĐỘNG đọc lúc import module —
# KHÔNG cần cài thêm thư viện (python-dotenv), tự parse đơn giản, chỉ hỗ
# trợ đúng dạng KEY=VALUE (không hỗ trợ giá trị có dấu ngoặc kép/comment
# cuối dòng — nếu cần phức tạp hơn, dùng CÁCH 2 hoặc 3 bên dưới).
# ⚠️ KHÔNG commit file .env lên git (chứa đường dẫn/ID riêng của máy bạn) —
# thêm ".env" vào .gitignore.
#
# CÁCH 2: set biến môi trường TRƯỚC khi chạy uvicorn (chỉ tồn tại trong
# phiên CMD/PowerShell đang mở):
#   set TTS_KOKORO_MODEL_DIR=D:\models\kokoro-sherpa-full\kokoro
#   uvicorn main:app --reload
#
# CÁCH 3: setx (Windows) — ghi vĩnh viễn vào biến môi trường user, cần mở
# cửa sổ CMD MỚI mới có hiệu lực:
#   setx TTS_KOKORO_MODEL_DIR "D:\models\kokoro-sherpa-full\kokoro"
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("tts_server.config")

# tts_server/ (thư mục chứa chính file config.py này)
_TTS_SERVER_DIR = Path(__file__).resolve().parent
# project_root/ (cha của tts_server/) — mọi mặc định đặt ở đây, NGANG HÀNG
# với tts_server/, không nằm lồng bên trong nó.
_PROJECT_ROOT = _TTS_SERVER_DIR.parent


def _load_dotenv(env_path: Path) -> None:
    """Đọc file .env đơn giản (KEY=VALUE mỗi dòng) và set vào os.environ —
    CHỈ set nếu biến đó CHƯA có sẵn trong môi trường (os.environ.setdefault),
    để biến đã set thủ công qua `set`/`setx` (CÁCH 2/3) LUÔN được ưu tiên
    hơn giá trị trong .env — đúng thứ tự ưu tiên thông thường của các công
    cụ đọc .env khác. Bỏ qua dòng rỗng và dòng bắt đầu bằng "#" (comment).
    KHÔNG throw nếu file .env không tồn tại — đây là tính năng TUỲ CHỌN.
    """
    if not env_path.exists():
        return

    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)
        logger.info("config: đã nạp cấu hình từ %s", env_path)
    except Exception:
        logger.exception("config: lỗi đọc file .env tại %s — bỏ qua, dùng biến môi trường hệ thống", env_path)


# ── Nạp .env NGAY LÚC IMPORT module này, TRƯỚC khi load_config() đọc bất
# kỳ biến nào — để mọi os.environ.get() bên dưới thấy được giá trị từ .env
# (nếu có) như thể chúng được set bằng `set` thủ công. ─────────────────────
_load_dotenv(_PROJECT_ROOT / ".env")



def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw) if raw else default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("config: biến %s=%r không phải số nguyên hợp lệ, dùng mặc định %d", name, raw, default)
        return default


@dataclass(frozen=True)
class ServerConfig:
    # ── Kokoro engine (bước 5) ────────────────────────────────────────────
    # KHÔNG có mặc định hợp lý cho model_dir (đường dẫn model tuỳ máy bạn
    # tải về) — để trống nếu chưa cấu hình, job_processor.py sẽ tự báo lỗi
    # RÕ RÀNG lúc khởi động thay vì lỗi mập mờ khi build_tts() thất bại vì
    # đường dẫn rỗng.
    kokoro_model_dir: str
    kokoro_num_threads: int

    # ── Thư mục làm việc (bước 5-7) ───────────────────────────────────────
    wav_tmp_dir: Path       # .wav tạm trước khi encode (bước 5→6)
    output_dir: Path        # gốc chứa .ogg, cấu trúc {reading_id}/{sid}/*.ogg
                             # (bước 6) — CHÍNH LÀ input-dir cho zip (bước 7)
    zip_output_dir: Path    # nơi ghi .zip trước khi upload (bước 7)

    # ── Encode (bước 6) ───────────────────────────────────────────────────
    opus_bitrate: str
    ffmpeg_path: str

    # ── Google Drive (bước 7) ─────────────────────────────────────────────
    # ⚠️ SERVICE ACCOUNT (drive_credentials_path) KHÔNG dùng để UPLOAD nữa —
    # gặp lỗi 403 storageQuotaExceeded vì service account không có quota
    # lưu trữ riêng, và Gmail cá nhân (không phải Workspace) không có
    # Shared Drive để né giới hạn này. Giữ lại field này CHỈ để tương thích
    # ngược / dùng cho việc ĐỌC nếu cần sau này — upload_or_replace_zip()
    # (drive_upload.py) đã đổi sang dùng OAuth (2 field dưới) thay thế.
    drive_credentials_path: Path
    drive_folder_id: str

    # ── OAuth cho UPLOAD (thay service account) — tài khoản Gmail THẬT sự
    # hữu folder Drive, có quota lưu trữ riêng. Lấy 1 LẦN qua
    # oauth_setup.py (chạy thủ công), sau đó drive_upload.py tự dùng lại
    # refresh token đã lưu, KHÔNG cần đăng nhập lại. ────────────────────────
    drive_oauth_client_secret_path: Path   # tải từ Cloud Console (Desktop app)
    drive_oauth_token_path: Path           # tự ghi bởi oauth_setup.py

    def validate(self) -> list[str]:
        """Trả về danh sách cảnh báo cấu hình thiếu/không hợp lệ — GỌI 1 LẦN
        lúc startup (main.py), chỉ log chứ KHÔNG chặn server khởi động (đúng
        tinh thần toàn bộ server này: thiếu cấu hình → job cứ tạo, nhưng xử
        lý sẽ tự 'failed' có lý do rõ ràng trong log, không phải crash server).
        """
        problems = []
        if not self.kokoro_model_dir:
            problems.append(
                "TTS_KOKORO_MODEL_DIR chưa được cấu hình — job_processor sẽ KHÔNG "
                "thể load KokoroVoiceEngine, mọi job sẽ 'failed' ngay bước generate."
            )
        if not self.drive_folder_id:
            problems.append(
                "TTS_DRIVE_ROOT_FOLDER_ID chưa được cấu hình — upload Drive sẽ "
                "thất bại (không biết upload vào folder nào)."
            )
        if not self.drive_oauth_token_path.exists():
            problems.append(
                f"drive_oauth_token_path không tồn tại: {self.drive_oauth_token_path} — "
                f"chạy 'python -m tts_server.oauth_setup' (1 lần) để lấy refresh token "
                f"cho tài khoản Gmail sở hữu folder Drive, service account KHÔNG dùng "
                f"được để upload (lỗi storageQuotaExceeded)."
            )
        return problems


def load_config() -> ServerConfig:
    cfg = ServerConfig(
        kokoro_model_dir=_env_str("TTS_KOKORO_MODEL_DIR", ""),
        kokoro_num_threads=_env_int("TTS_KOKORO_NUM_THREADS", 2),
        wav_tmp_dir=_env_path("TTS_WAV_TMP_DIR", _PROJECT_ROOT / "tts_wav_tmp"),
        output_dir=_env_path("TTS_OUTPUT_DIR", _PROJECT_ROOT / "tts_output"),
        zip_output_dir=_env_path("TTS_ZIP_OUTPUT_DIR", _PROJECT_ROOT / "tts_zips"),
        opus_bitrate=_env_str("TTS_OPUS_BITRATE", "32k"),
        ffmpeg_path=_env_str("TTS_FFMPEG_PATH", "ffmpeg"),
        drive_credentials_path=_env_path(
            "TTS_DRIVE_CREDENTIALS_PATH", _PROJECT_ROOT / "credentials" / "service_account.json"
        ),
        drive_folder_id=_env_str("TTS_DRIVE_ROOT_FOLDER_ID", ""),
        drive_oauth_client_secret_path=_env_path(
            "TTS_DRIVE_OAUTH_CLIENT_SECRET_PATH", _PROJECT_ROOT / "credentials" / "oauth_client_secret.json"
        ),
        drive_oauth_token_path=_env_path(
            "TTS_DRIVE_OAUTH_TOKEN_PATH", _PROJECT_ROOT / "credentials" / "oauth_token.json"
        ),
    )

    # Tạo sẵn các thư mục làm việc nếu chưa có — tránh job đầu tiên lỗi vô
    # nghĩa chỉ vì thư mục chưa tồn tại (encode/wav write sẽ tự fail nếu
    # thiếu thư mục cha).
    cfg.wav_tmp_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    cfg.zip_output_dir.mkdir(parents=True, exist_ok=True)

    for problem in cfg.validate():
        logger.warning("config: %s", problem)

    return cfg


# ── Singleton — nạp 1 lần khi module này được import lần đầu (import ở
# main.py lúc startup, và ở job_processor.py) — KHÔNG gọi load_config()
# nhiều nơi khác nhau để tránh đọc env var không nhất quán giữa 2 lần gọi
# trong cùng 1 lần chạy server. ─────────────────────────────────────────
CONFIG = load_config()