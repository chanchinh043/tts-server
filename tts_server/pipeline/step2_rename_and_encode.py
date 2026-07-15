#!/usr/bin/env python3
# step2_rename_and_encode.py
#
# Bước 2/9 của pipeline "remote TTS pack": đổi tên file WAV sinh ra ở bước 1
# (đang đặt tên tạm "{type}_{itemId}.wav") sang ĐÚNG quy ước cache của
# TtsAudioCache.kt: "{type}_{itemId}_{contentHash}.ogg" — rồi encode sang
# OGG/Opus (nhỏ hơn WAV 10-15 lần, Android decode native qua MediaPlayer,
# không cần thư viện gì thêm bên Kotlin).
#
# ⚠️ contentHash PHẢI khớp CHÍNH XÁC công thức bên Kotlin
# (TtsAudioCache.contentHash): 8 ký tự đầu (hex, chữ thường) của
# SHA-256(text UTF-8) — SAI công thức này = app không bao giờ nhận ra file
# đã tải về khớp đúng item nào, coi như cache vô dụng. Copy y hệt logic đó
# ở content_hash() bên dưới, KHÔNG tự đổi cách tính.
#
# ── VÌ SAO ĐỌC LẠI DB THAY VÌ DÙNG TÊN FILE Ở BƯỚC 1 ────────────────────
# Bước 1 lưu theo "{type}_{itemId}.wav" (chưa có hash, vì lúc đó chưa xác
# nhận công thức) — để tính đúng contentHash cần có lại ĐÚNG text_en đã
# dùng để generate, nên script này đọc lại readings.db/myreading.db (cùng
# 2 hàm truy vấn như bước 1) để lấy text tương ứng với từng item_id, thay vì
# đoán mò hoặc parse ngược từ đâu đó.
#
# ── CÀI ĐẶT CẦN THIẾT ────────────────────────────────────────────────────
#   - ffmpeg phải có sẵn trong PATH (kiểm tra: ffmpeg -version)
#   - Không cần thư viện Python thêm ngoài chuẩn (sqlite3, hashlib, subprocess)
#
# ── CẤU TRÚC OUTPUT ──────────────────────────────────────────────────────
#   {output_dir}/{reading_id}/{sid}/{type}_{itemId}_{contentHash}.ogg
#   — ĐÚNG cấu trúc thư mục TtsAudioCache.voiceDir() mong đợi, sẵn sàng để
#   bước 4 (đóng gói zip) dùng thẳng.
#
# ── VÍ DỤ CHẠY (nếu vẫn muốn dùng dòng lệnh) ─────────────────────────────
#   python step2_rename_and_encode.py \
#       --readings-db ./readings.db \
#       --myreading-db ./myreading.db \
#       --input-dir ./generated_audio \
#       --output-dir ./packaged_audio \
#       --sids 2 3 9 11 16 26 \
#       --opus-bitrate 32k

# ══════════════════════════════════════════════════════════════════════
# CẤU HÌNH — SỬA TRỰC TIẾP CÁC DÒNG DƯỚI ĐÂY.
# Không cần truyền tham số dòng lệnh, không cần cấu hình launch.json.
# Bấm Run/F5 (kể cả qua debugpy) là chạy được ngay với giá trị bên dưới.
# Các giá trị này chỉ là MẶC ĐỊNH — nếu bạn vẫn truyền cờ dòng lệnh
# (--input-dir, --sids, ...) thì cờ dòng lệnh sẽ được ưu tiên hơn.
# ══════════════════════════════════════════════════════════════════════

# Đường dẫn 2 file DB. Để chuỗi rỗng "" nếu không có / không dùng loại đó.
# Để trống thì script tự tìm ở thư mục "db" cạnh file .py này — CÙNG chỗ
# bạn đã đặt readings.db/myreading.db khi chạy bước 1.
READINGS_DB = r""    # vd: r"D:\Website\App Androi\TTS\...\readings.db"
MYREADING_DB = r""   # vd: r"D:\Website\App Androi\TTS\...\myreading.db"

# Thư mục chứa WAV do bước 1 sinh ra. Để chuỗi rỗng "" để dùng thư mục
# "generated_audio" cạnh file .py này — ĐÚNG mặc định output của bước 1.
INPUT_DIR = r""

# Thư mục ghi OGG output cuối cùng. Để chuỗi rỗng "" để dùng thư mục
# "packaged_audio" cạnh file .py này.
OUTPUT_DIR = r""

# Danh sách sid cần xử lý — nên khớp với SIDS đã dùng ở bước 1.
SIDS = [2, 3, 9, 11, 16, 26]

# Chỉ xử lý các reading_id này. Để [] = TOÀN BỘ bài trong DB.
READING_IDS: list = []

# Bitrate Opus khi encode (32k đủ rõ cho giọng đọc, nhẹ dung lượng).
OPUS_BITRATE = "32k"

# Đường dẫn tới file ffmpeg.exe (Windows) hoặc ffmpeg (Linux/macOS).
# Để chuỗi rỗng "" nếu ffmpeg đã có sẵn trong PATH hệ thống.
# Nếu "pip install ffmpeg" không đủ (đó chỉ là package Python trùng tên,
# KHÔNG phải chương trình ffmpeg thật), tải bản ffmpeg thật cho Windows ở
# https://www.gyan.dev/ffmpeg/builds/ (ffmpeg-release-essentials.zip),
# giải nén, rồi dán đường dẫn đầy đủ tới ffmpeg.exe vào đây, ví dụ:
#   r"D:\ffmpeg\ffmpeg-8.0-essentials_build\bin\ffmpeg.exe"
FFMPEG_PATH = r""
# ══════════════════════════════════════════════════════════════════════

import argparse
import hashlib
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# ── Thư mục gốc = nơi đặt file .py này (không phụ thuộc đang chạy từ đâu).
# Dùng làm fallback cho INPUT_DIR/OUTPUT_DIR/DB khi để trống ở trên.
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_DIR = SCRIPT_DIR / "db"
DEFAULT_READINGS_DB = Path(READINGS_DB) if READINGS_DB else DEFAULT_DB_DIR / "readings.db"
DEFAULT_MYREADING_DB = Path(MYREADING_DB) if MYREADING_DB else DEFAULT_DB_DIR / "myreading.db"
DEFAULT_INPUT_DIR = Path(INPUT_DIR) if INPUT_DIR else SCRIPT_DIR / "generated_audio"
DEFAULT_OUTPUT_DIR = Path(OUTPUT_DIR) if OUTPUT_DIR else SCRIPT_DIR / "packaged_audio"
DEFAULT_SIDS = SIDS
DEFAULT_READING_IDS = READING_IDS or None
DEFAULT_OPUS_BITRATE = OPUS_BITRATE
DEFAULT_FFMPEG_PATH = FFMPEG_PATH if FFMPEG_PATH else "ffmpeg"


@dataclass
class Item:
    item_type: str
    item_id: str
    text_en: str


def content_hash(text: str) -> str:
    # ── PHẢI khớp TtsAudioCache.contentHash() bên Kotlin từng byte một:
    # SHA-256 trên UTF-8 bytes, in ra hex chữ THƯỜNG, lấy 8 ký tự đầu. ────
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest[:8]


def open_readonly(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def get_reading_ids(db_path: Path, has_deleted_at: bool) -> List[str]:
    with open_readonly(db_path) as conn:
        query = "SELECT reading_id FROM readings"
        if has_deleted_at:
            query += " WHERE deleted_at IS NULL"
        return [row[0] for row in conn.execute(query)]


def get_items_for_reading(db_path: Path, reading_id: str) -> List[Item]:
    # ── Y HỆT logic bước 1 (get_items_for_reading) — tách riêng vì đây là
    # script độc lập chạy sau, chưa gộp thành module dùng chung (có thể làm
    # sau nếu pipeline ổn định, không cần thiết ở giai đoạn đang xác nhận
    # từng bước này). ────────────────────────────────────────────────────
    items: List[Item] = []
    with open_readonly(db_path) as conn:
        cur = conn.execute(
            "SELECT sentence_id, text_en FROM reading_sentences WHERE reading_id = ?",
            (reading_id,),
        )
        for sentence_id, text_en in cur:
            if text_en and text_en.strip():
                items.append(Item("sentence", sentence_id, text_en))

        cur = conn.execute(
            """
            SELECT w.word_id, w.text_en
            FROM sentence_words w
            INNER JOIN reading_sentences s ON s.sentence_id = w.sentence_id
            WHERE s.reading_id = ?
            """,
            (reading_id,),
        )
        for word_id, text_en in cur:
            if text_en and text_en.strip():
                items.append(Item("word", word_id, text_en))

        cur = conn.execute(
            """
            SELECT p.phrase_id, p.text_en
            FROM sentence_phrases p
            INNER JOIN reading_sentences s ON s.sentence_id = p.sentence_id
            WHERE s.reading_id = ?
            """,
            (reading_id,),
        )
        for phrase_id, text_en in cur:
            if text_en and text_en.strip():
                items.append(Item("phrase", phrase_id, text_en))

    return items


def check_ffmpeg_available(ffmpeg_cmd: str) -> None:
    # ffmpeg_cmd có thể là path tuyệt đối tới ffmpeg.exe (nếu FFMPEG_PATH
    # được set), hoặc chỉ là "ffmpeg" để tìm trong PATH hệ thống.
    cmd_path = Path(ffmpeg_cmd)
    is_direct_path = cmd_path.is_absolute() or ("/" in ffmpeg_cmd or "\\" in ffmpeg_cmd)

    if is_direct_path:
        if not cmd_path.exists():
            print(
                f"LỖI: không tìm thấy ffmpeg tại đường dẫn đã cấu hình: {cmd_path}\n"
                f"  → Kiểm tra lại biến FFMPEG_PATH đầu file (hoặc --ffmpeg-path).",
                file=sys.stderr,
            )
            sys.exit(1)
        return

    if shutil.which(ffmpeg_cmd) is None:
        print(
            f"LỖI: không tìm thấy '{ffmpeg_cmd}' trong PATH.\n"
            f"  Lưu ý: 'pip install ffmpeg' KHÔNG cài chương trình ffmpeg thật "
            f"(đó là package Python trùng tên, vô dụng cho việc này).\n"
            f"  → Cách 1: chạy 'winget install ffmpeg' rồi mở lại terminal/VS Code.\n"
            f"  → Cách 2: tải https://www.gyan.dev/ffmpeg/builds/ (bản essentials), "
            f"giải nén, rồi dán đường dẫn đầy đủ tới ffmpeg.exe vào biến FFMPEG_PATH "
            f"đầu file này.",
            file=sys.stderr,
        )
        sys.exit(1)


def encode_to_opus(src_wav: Path, dst_ogg: Path, bitrate: str, ffmpeg_cmd: str) -> bool:
    dst_ogg.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ffmpeg_cmd,
            "-y",  # ghi đè nếu output đã tồn tại
            "-loglevel", "error",
            "-i", str(src_wav),
            "-c:a", "libopus",
            "-b:a", bitrate,
            "-ac", "1",  # mono — khớp WAV gốc, không cần stereo cho TTS
            str(dst_ogg),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"    LỖI encode '{src_wav.name}': {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def run(args: argparse.Namespace) -> None:
    check_ffmpeg_available(args.ffmpeg_path)

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)

    reading_sources = []

    # ── Tự dò DB trong thư mục db/ cạnh file .py nếu người dùng không
    # truyền --readings-db / --myreading-db (giống hệt cơ chế bước 1). ───
    readings_db_path = Path(args.readings_db) if args.readings_db else DEFAULT_READINGS_DB
    myreading_db_path = Path(args.myreading_db) if args.myreading_db else DEFAULT_MYREADING_DB

    if readings_db_path.exists():
        reading_sources.append((readings_db_path, False))
    elif args.readings_db:
        print(f"LỖI: không tìm thấy --readings-db: {readings_db_path}", file=sys.stderr)
        sys.exit(1)

    if myreading_db_path.exists():
        reading_sources.append((myreading_db_path, True))
    elif args.myreading_db:
        print(f"LỖI: không tìm thấy --myreading-db: {myreading_db_path}", file=sys.stderr)
        sys.exit(1)

    if not reading_sources:
        print(
            f"Không tìm thấy DB nào. Đã kiểm tra mặc định:\n"
            f"  {DEFAULT_READINGS_DB}\n"
            f"  {DEFAULT_MYREADING_DB}\n"
            f"Hãy đặt file DB vào thư mục '{DEFAULT_DB_DIR}' (cạnh file .py), "
            f"hoặc sửa biến READINGS_DB/MYREADING_DB đầu file, "
            f"hoặc truyền --readings-db / --myreading-db thủ công.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Nguồn DB sẽ dùng:")
    for db_path, _ in reading_sources:
        print(f"  - {db_path}")

    all_reading_ids: List[tuple] = []
    for db_path, has_deleted_at in reading_sources:
        ids = get_reading_ids(db_path, has_deleted_at)
        all_reading_ids.extend((rid, db_path) for rid in ids)

    if args.reading_ids:
        wanted = set(args.reading_ids)
        all_reading_ids = [(rid, db) for rid, db in all_reading_ids if rid in wanted]

    print(f"Tổng số bài sẽ xử lý: {len(all_reading_ids)}")

    total_encoded = 0
    total_missing_input = 0
    total_encode_failed = 0

    for reading_id, db_path in all_reading_ids:
        items = get_items_for_reading(db_path, reading_id)
        print(f"\n[{reading_id}] {len(items)} item")

        for sid in args.sids:
            for item in items:
                src_wav = input_dir / reading_id / str(sid) / f"{item.item_type}_{item.item_id}.wav"
                if not src_wav.exists():
                    # Bước 1 có thể đã bỏ qua item này (câm hoàn toàn, hết
                    # chuỗi giọng) — không phải lỗi, chỉ là chưa có gì để
                    # đóng gói cho item này, bỏ qua an toàn.
                    total_missing_input += 1
                    continue

                hash_ = content_hash(item.text_en)
                dst_ogg = output_dir / reading_id / str(sid) / f"{item.item_type}_{item.item_id}_{hash_}.ogg"

                if dst_ogg.exists():
                    continue  # đã encode từ lượt chạy trước — resume tự nhiên

                ok = encode_to_opus(src_wav, dst_ogg, args.opus_bitrate, args.ffmpeg_path)
                if ok:
                    total_encoded += 1
                else:
                    total_encode_failed += 1

    print(
        f"\nXong. Đã encode {total_encoded} file, "
        f"{total_missing_input} item không có WAV nguồn (bỏ qua), "
        f"{total_encode_failed} file encode lỗi."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Đổi tên đúng chuẩn cache + encode WAV sang OGG/Opus (bước 2/9).",
    )
    parser.add_argument(
        "--readings-db",
        default=None,
        help=(
            "Đường dẫn readings.db. "
            f"Mặc định (sửa ở biến READINGS_DB đầu file): '{DEFAULT_READINGS_DB}'."
        ),
    )
    parser.add_argument(
        "--myreading-db",
        default=None,
        help=(
            "Đường dẫn myreading.db. "
            f"Mặc định (sửa ở biến MYREADING_DB đầu file): '{DEFAULT_MYREADING_DB}'."
        ),
    )
    parser.add_argument(
        "--input-dir",
        default=str(DEFAULT_INPUT_DIR),
        help=(
            "Thư mục WAV output từ bước 1. "
            f"Mặc định (sửa ở biến INPUT_DIR đầu file): '{DEFAULT_INPUT_DIR}'."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=(
            "Thư mục OGG output cuối cùng. "
            f"Mặc định (sửa ở biến OUTPUT_DIR đầu file): '{DEFAULT_OUTPUT_DIR}'."
        ),
    )
    parser.add_argument(
        "--sids",
        type=int,
        nargs="+",
        default=DEFAULT_SIDS,
        help=f"Danh sách sid, vd: 2 3 9. Mặc định (biến SIDS đầu file): {DEFAULT_SIDS}",
    )
    parser.add_argument(
        "--reading-ids",
        nargs="*",
        default=DEFAULT_READING_IDS,
        help=(
            "Chỉ xử lý các bài này (bỏ trống = tất cả). "
            f"Mặc định (biến READING_IDS đầu file): {DEFAULT_READING_IDS}"
        ),
    )
    parser.add_argument(
        "--opus-bitrate",
        default=DEFAULT_OPUS_BITRATE,
        help=f"Bitrate Opus. Mặc định (biến OPUS_BITRATE đầu file): {DEFAULT_OPUS_BITRATE}",
    )
    parser.add_argument(
        "--ffmpeg-path",
        default=DEFAULT_FFMPEG_PATH,
        help=(
            "Đường dẫn tới ffmpeg.exe, hoặc để mặc định 'ffmpeg' nếu đã có "
            f"trong PATH. Mặc định (biến FFMPEG_PATH đầu file): '{DEFAULT_FFMPEG_PATH}'."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())