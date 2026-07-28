#!/usr/bin/env python3
# pipeline_main.py
#
# Main RIÊNG cho việc generate audio bài đọc HỆ THỐNG (đọc từ readings.db),
# chạy TAY 1 lần khi cần (không phải qua server), rồi bạn tự upload zip lên
# Drive thủ công. KHÔNG liên quan gì tới main.py (server FastAPI xử lý
# MyReading) — 2 file main này ĐỘC LẬP hoàn toàn:
#
#   main.py           -> chạy bằng `uvicorn main:app`, server sống liên tục,
#                         xử lý job MyReading tự động qua job_processor.py.
#   pipeline_main.py   -> chạy bằng `python pipeline_main.py`, chạy 1 lần rồi
#                         thoát, KHÔNG khởi động server/FastAPI, KHÔNG đụng
#                         tới tts_myreading_jobs.db hay jobs.py/job_processor.py.
#
# Nối liền 3 bước đã có sẵn, KHÔNG copy logic — chỉ gọi thẳng run() của mỗi
# bước với input/output trỏ vào ĐÚNG 1 thư mục gốc riêng (PIPELINE_OUTPUT_DIR
# bên dưới), để không lẫn với tts_output/tts_zips (thư mục server MyReading
# đang dùng, xem tts_server/config.py):
#
#   Bước 1 (step1_generate_kokoro_audio.py) -> pipeline_output/wav_raw/
#   Bước 2 (step2_rename_and_encode.py)     -> pipeline_output/ogg/
#   Bước 3 (step3_package_zip.py)           -> pipeline_output/zip/
#                                               + version_state.json
#                                               + pending_upload.json
#
# Tốc độ đọc (word/sentence/phrase) đã tự động theo item_type NGAY TRONG
# step1_generate_kokoro_audio.py (get_speed_for_type()) — file này KHÔNG
# cần biết/truyền speed gì cả, cứ gọi step1.run() là tự đúng tốc độ.
#
# ── CÁCH DÙNG ─────────────────────────────────────────────────────────────
# Sửa trực tiếp khối CẤU HÌNH ngay dưới đây (MODEL_DIR, READINGS_DB, SIDS,
# READING_IDS...), rồi chạy:
#   python pipeline_main.py
# Muốn chỉ chạy 1/vài bước (vd đã generate WAV rồi, chỉ cần encode lại):
#   python pipeline_main.py --steps 2,3
#   python pipeline_main.py --steps 1
from __future__ import annotations

# ══════════════════════════════════════════════════════════════════════
# CẤU HÌNH — SỬA TRỰC TIẾP CÁC DÒNG DƯỚI ĐÂY, giống style step1/2/3.
# ══════════════════════════════════════════════════════════════════════

# Thư mục chứa model Kokoro — PHẢI trỏ vào thư mục con "kokoro" (giống hệt
# MODEL_DIR trong step1_generate_kokoro_audio.py).
MODEL_DIR = r"D:\Website\App Androi\TTS\Download model\kokoro-sherpa-full\kokoro"

# Đường dẫn readings.db (bài đọc HỆ THỐNG) — file main này CỐ Ý không đụng
# tới myreading.db (đó là việc của server/job_processor.py, luồng khác).
READINGS_DB = r""   # để trống "" = tự tìm ở tts_server/pipeline/db/readings.db

# Danh sách sid cần generate — nên khớp SIDS đang dùng ở step1/step2 gốc.
SIDS = [2, 3, 6, 11, 16, 26]

# Chỉ xử lý các reading_id này. Để [] = TOÀN BỘ bài trong DB.
READING_IDS: list = []

NUM_THREADS = 2
OPUS_BITRATE = "32k"

# Để trống "" nếu ffmpeg đã có sẵn trong PATH hệ thống, hoặc dán đường dẫn
# đầy đủ tới ffmpeg.exe (xem ghi chú chi tiết trong step2_rename_and_encode.py).
FFMPEG_PATH = r""
# ══════════════════════════════════════════════════════════════════════

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

SCRIPT_DIR = Path(__file__).resolve().parent

# ── Thư mục gốc RIÊNG cho toàn bộ output của pipeline này — TÁCH HẲN khỏi
# tts_output/tts_zips (server MyReading đang dùng, xem tts_server/config.py)
# để 2 luồng generate (batch bài hệ thống vs job tự động MyReading) không
# bao giờ vô tình đè lên nhau, dù chạy cùng lúc trên cùng máy. ─────────────
PIPELINE_OUTPUT_DIR = SCRIPT_DIR / "pipeline_output"
WAV_RAW_DIR = PIPELINE_OUTPUT_DIR / "wav_raw"
OGG_DIR = PIPELINE_OUTPUT_DIR / "ogg"
ZIP_DIR = PIPELINE_OUTPUT_DIR / "zip"
STATE_FILE = PIPELINE_OUTPUT_DIR / "version_state.json"
PENDING_UPLOAD_FILE = PIPELINE_OUTPUT_DIR / "pending_upload.json"

_PIPELINE_STEPS_DIR = SCRIPT_DIR / "tts_server" / "pipeline"


def _load_step_module(filename: str) -> ModuleType:
    """Nạp 1 file step*.py bằng importlib theo ĐÚNG ĐƯỜNG DẪN THẬT — an
    toàn hơn cách 'sys.path.insert() rồi import tên module' khi phải nạp
    NHIỀU file cùng lúc trong 1 process (kokoro_engine.py/audio_encode.py/
    drive_upload.py chỉ cần nạp ĐÚNG 1 file mỗi nơi nên dùng cách chèn
    sys.path là đủ; ở đây cần nạp CẢ 3 file cùng lúc, dùng importlib theo
    path rõ ràng để chắc chắn nạp đúng file, đúng thứ tự, không phụ thuộc
    thứ tự sys.path).
    """
    path = _PIPELINE_STEPS_DIR / filename
    if not path.exists():
        print(
            f"LỖI: không tìm thấy {path}\n"
            f"  → Kiểm tra lại 3 file step1/2/3 đã được copy vào đúng "
            f"tts_server/pipeline/ chưa (xem ghi chú ở kokoro_engine.py).",
            file=sys.stderr,
        )
        sys.exit(1)

    module_name = path.stem  # vd "step1_generate_kokoro_audio"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # để step tự import lẫn nhau nếu cần
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def run_step1(step1: ModuleType, args: argparse.Namespace) -> None:
    print("\n" + "═" * 70)
    print("BƯỚC 1/3 — Generate WAV thô từ readings.db (Kokoro)")
    print("═" * 70)
    ns = argparse.Namespace(
        readings_db=args.readings_db,
        myreading_db="",  # CỐ Ý rỗng — pipeline này CHỈ xử lý bài hệ thống
        model_dir=str(MODEL_DIR),
        output_dir=str(WAV_RAW_DIR),
        sids=SIDS,
        reading_ids=READING_IDS or None,
        num_threads=NUM_THREADS,
    )
    step1.run(ns)


def run_step2(step2: ModuleType, args: argparse.Namespace) -> None:
    print("\n" + "═" * 70)
    print("BƯỚC 2/3 — Đổi tên đúng chuẩn cache + encode OGG/Opus")
    print("═" * 70)
    ns = argparse.Namespace(
        readings_db=args.readings_db,
        myreading_db="",
        input_dir=str(WAV_RAW_DIR),
        output_dir=str(OGG_DIR),
        sids=SIDS,
        reading_ids=READING_IDS or None,
        opus_bitrate=OPUS_BITRATE,
        ffmpeg_path=FFMPEG_PATH if FFMPEG_PATH else "ffmpeg",
    )
    step2.run(ns)


def run_step3(step3: ModuleType) -> None:
    print("\n" + "═" * 70)
    print("BƯỚC 3/3 — Đóng gói OGG thành zip (sẵn sàng upload Drive thủ công)")
    print("═" * 70)
    ns = argparse.Namespace(
        input_dir=str(OGG_DIR),
        zip_output_dir=str(ZIP_DIR),
        state_file=str(STATE_FILE),
        pending_upload_out=str(PENDING_UPLOAD_FILE),
    )
    step3.run(ns)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Chạy trọn pipeline (bước 1→2→3) generate audio bài đọc hệ thống từ readings.db.",
    )
    parser.add_argument(
        "--readings-db",
        default=READINGS_DB,
        help=f"Đường dẫn readings.db. Mặc định (biến READINGS_DB đầu file): '{READINGS_DB or '(tự tìm ở tts_server/pipeline/db/)'}'.",
    )
    parser.add_argument(
        "--steps",
        default="1,2,3",
        help="Chỉ chạy các bước này, vd '1,2,3' (mặc định, chạy đủ) hoặc '2,3' (bỏ qua generate WAV nếu đã có).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    wanted_steps = {int(s) for s in args.steps.split(",") if s.strip()}

    for d in (WAV_RAW_DIR, OGG_DIR, ZIP_DIR):
        d.mkdir(parents=True, exist_ok=True)

    print(f"Thư mục output pipeline (riêng, tách khỏi server MyReading): {PIPELINE_OUTPUT_DIR}")
    print(f"Các bước sẽ chạy: {sorted(wanted_steps)}")

    step1 = step2 = step3 = None
    if 1 in wanted_steps:
        step1 = _load_step_module("step1_generate_kokoro_audio.py")
    if 2 in wanted_steps:
        step2 = _load_step_module("step2_rename_and_encode.py")
    if 3 in wanted_steps:
        step3 = _load_step_module("step3_package_zip.py")

    if 1 in wanted_steps:
        run_step1(step1, args)
    if 2 in wanted_steps:
        run_step2(step2, args)
    if 3 in wanted_steps:
        run_step3(step3)

    print("\n" + "═" * 70)
    print(f"XONG TOÀN BỘ PIPELINE. Zip sẵn sàng upload thủ công tại: {ZIP_DIR}")
    print("═" * 70)


if __name__ == "__main__":
    main()