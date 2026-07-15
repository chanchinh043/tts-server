#!/usr/bin/env python3
# step3_package_zip.py
#
# Bước 3/9 của pipeline "remote TTS pack": gom các file .ogg đã encode ở
# bước 2 (theo từng (readingId, sid)) thành 1 file .zip PHẲNG (không thư
# mục con bên trong) — vì TtsRemotePackDownloader.extractZip() bên Kotlin
# ghi thẳng File(destDir, entry.name), nếu entry.name có đường dẫn con thì
# sẽ tạo nhầm cấu trúc thư mục lồng bên trong voiceDir(), không khớp với
# nơi TtsAudioCache.getCachedFile() tìm file phẳng.
#
# Cũng tính sha256 của zip (để khớp TtsRemotePackRef.sha256 — dùng để
# TtsRemotePackDownloader.verifyChecksum() xác thực trước khi giải nén) và
# quản lý "version" tăng dần CHỈ KHI nội dung thực sự đổi — tránh phải
# re-upload lại toàn bộ zip không đổi mỗi lần chạy lại pipeline.
#
# ── QUẢN LÝ VERSION/STATE ────────────────────────────────────────────────
# Lưu 1 file JSON trạng thái (mặc định version_state.json) dạng:
#   {
#     "reading_001:2": {"sha256": "...", "version": 3, "download_url": "https://..."},
#     ...
#   }
# Logic mỗi lần chạy cho 1 (readingId, sid):
#   - Chưa có trong state           → version = 1, download_url = null (CẦN upload)
#   - Có trong state, sha256 GIỐNG  → giữ nguyên version + download_url cũ
#     (zip không đổi nội dung, KHÔNG cần re-upload — bước 5 sẽ tự bỏ qua)
#   - Có trong state, sha256 KHÁC   → version += 1, download_url = null (CẦN
#     upload lại — nội dung đã đổi, ví dụ bài đọc được sửa lại text)
#
# download_url luôn null ở BƯỚC NÀY vì chưa upload — bước 5 (upload lên
# Drive) sẽ cập nhật ngược lại đúng field này trong cùng state file, rồi
# script riêng (bước cuối) mới build manifest.json HOÀN CHỈNH (có đủ URL)
# từ state file để publish. Bước 3 chỉ xuất thêm "pending_upload.json" —
# danh sách các zip đang CẦN upload (download_url còn null) để bước 5 biết
# chính xác việc gì cần làm, không phải quét lại toàn bộ mỗi lần.
#
# ── VÍ DỤ CHẠY (nếu vẫn muốn dùng dòng lệnh) ─────────────────────────────
#   python step3_package_zip.py \
#       --input-dir ./packaged_audio \
#       --zip-output-dir ./zips \
#       --state-file ./version_state.json \
#       --pending-upload-out ./pending_upload.json

# ══════════════════════════════════════════════════════════════════════
# CẤU HÌNH — SỬA TRỰC TIẾP CÁC DÒNG DƯỚI ĐÂY.
# Không cần truyền tham số dòng lệnh, không cần cấu hình launch.json.
# Bấm Run/F5 (kể cả qua debugpy) là chạy được ngay với giá trị bên dưới.
# Các giá trị này chỉ là MẶC ĐỊNH — nếu bạn vẫn truyền cờ dòng lệnh
# (--input-dir, --zip-output-dir, ...) thì cờ dòng lệnh sẽ được ưu tiên hơn.
# ══════════════════════════════════════════════════════════════════════

# Thư mục chứa OGG do bước 2 sinh ra. Để chuỗi rỗng "" để dùng thư mục
# "packaged_audio" cạnh file .py này — ĐÚNG mặc định output của bước 2.
INPUT_DIR = r""

# Thư mục ghi các file .zip. Để chuỗi rỗng "" để dùng thư mục "zips" cạnh
# file .py này.
ZIP_OUTPUT_DIR = r""

# File JSON lưu version/sha256/download_url theo từng (readingId, sid).
# Để chuỗi rỗng "" để dùng "version_state.json" cạnh file .py này.
# ⚠️ File này PHẢI được giữ lại xuyên suốt giữa các lần chạy (đừng xoá),
# vì nó là nơi duy nhất nhớ version hiện tại + download_url đã upload —
# xoá nhầm sẽ khiến toàn bộ gói bị coi là "mới" và tăng version sai.
STATE_FILE = r""

# File JSON danh sách zip đang CẦN upload (bước 5 sẽ đọc file này). Để
# chuỗi rỗng "" để dùng "pending_upload.json" cạnh file .py này.
PENDING_UPLOAD_OUT = r""
# ══════════════════════════════════════════════════════════════════════

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Dict, Optional

# ── Thư mục gốc = nơi đặt file .py này (không phụ thuộc đang chạy từ đâu).
# Dùng làm fallback cho các đường dẫn khi để trống ở trên.
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = Path(INPUT_DIR) if INPUT_DIR else SCRIPT_DIR / "packaged_audio"
DEFAULT_ZIP_OUTPUT_DIR = Path(ZIP_OUTPUT_DIR) if ZIP_OUTPUT_DIR else SCRIPT_DIR / "zips"
DEFAULT_STATE_FILE = Path(STATE_FILE) if STATE_FILE else SCRIPT_DIR / "version_state.json"
DEFAULT_PENDING_UPLOAD_OUT = Path(PENDING_UPLOAD_OUT) if PENDING_UPLOAD_OUT else SCRIPT_DIR / "pending_upload.json"


def compute_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_state(state_file: Path) -> Dict:
    if not state_file.exists():
        return {}
    with state_file.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state_file: Path, state: Dict) -> None:
    with state_file.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def build_zip(ogg_files: list, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    # ZIP_DEFLATED — nén thêm chút ít (opus đã tự nén rồi nên lợi ích không
    # nhiều, nhưng không tốn gì để bật, và tên file trong zip CHỈ LÀ TÊN
    # FILE (arcname=ogg.name), không kèm đường dẫn cha — bắt buộc để khớp
    # cách extractZip() bên Kotlin ghi entry.name thẳng vào destDir.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for ogg_file in ogg_files:
            zf.write(ogg_file, arcname=ogg_file.name)


def run(args: argparse.Namespace) -> None:
    input_dir = Path(args.input_dir)
    zip_output_dir = Path(args.zip_output_dir)
    state_file = Path(args.state_file)
    pending_upload_out = Path(args.pending_upload_out)

    if not input_dir.exists():
        print(
            f"LỖI: input-dir không tồn tại: {input_dir}\n"
            f"  → Kiểm tra đã chạy xong bước 2 (step2_rename_and_encode.py) chưa, "
            f"hoặc sửa lại biến INPUT_DIR đầu file này / truyền --input-dir.",
            file=sys.stderr,
        )
        sys.exit(1)

    state = load_state(state_file)
    pending_upload = []  # danh sách {"key", "zip_path", "sha256", "version"} cần upload

    total_packed = 0
    total_unchanged = 0

    # ── Duyệt cấu trúc {input_dir}/{reading_id}/{sid}/*.ogg — mỗi thư mục
    # con cấp 2 (sid) ứng với đúng 1 zip cần đóng gói. ─────────────────────
    for reading_dir in sorted(p for p in input_dir.iterdir() if p.is_dir()):
        reading_id = reading_dir.name

        for sid_dir in sorted(p for p in reading_dir.iterdir() if p.is_dir()):
            sid = sid_dir.name
            ogg_files = sorted(sid_dir.glob("*.ogg"))
            if not ogg_files:
                continue

            key = f"{reading_id}:{sid}"
            zip_path = zip_output_dir / f"{reading_id}_{sid}.zip"

            build_zip(ogg_files, zip_path)
            new_sha256 = compute_sha256(zip_path)

            existing: Optional[Dict] = state.get(key)
            if existing is not None and existing.get("sha256") == new_sha256:
                # Nội dung không đổi — giữ nguyên version + download_url cũ,
                # KHÔNG cần thêm vào danh sách cần upload.
                total_unchanged += 1
                print(f"  [{key}] không đổi (version={existing['version']}), bỏ qua upload")
                continue

            new_version = (existing.get("version", 0) + 1) if existing else 1
            state[key] = {
                "sha256": new_sha256,
                "version": new_version,
                "download_url": None,  # bước 5 sẽ điền sau khi upload
                "reading_id": reading_id,
                "sid": int(sid),
                "zip_filename": zip_path.name,
            }
            pending_upload.append(state[key])
            total_packed += 1
            print(f"  [{key}] đóng gói xong, version={new_version}, sha256={new_sha256[:12]}...")

    save_state(state_file, state)

    pending_upload_out.parent.mkdir(parents=True, exist_ok=True)
    with pending_upload_out.open("w", encoding="utf-8") as f:
        json.dump(pending_upload, f, ensure_ascii=False, indent=2)

    print(
        f"\nXong. {total_packed} gói mới/đã đổi cần upload, "
        f"{total_unchanged} gói không đổi (bỏ qua). "
        f"Danh sách cần upload đã ghi vào: {pending_upload_out}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Đóng gói OGG theo (readingId, sid) thành zip + quản lý version (bước 3/9).",
    )
    parser.add_argument(
        "--input-dir",
        default=str(DEFAULT_INPUT_DIR),
        help=(
            "Thư mục OGG output từ bước 2. "
            f"Mặc định (sửa ở biến INPUT_DIR đầu file): '{DEFAULT_INPUT_DIR}'."
        ),
    )
    parser.add_argument(
        "--zip-output-dir",
        default=str(DEFAULT_ZIP_OUTPUT_DIR),
        help=(
            "Thư mục lưu file .zip. "
            f"Mặc định (sửa ở biến ZIP_OUTPUT_DIR đầu file): '{DEFAULT_ZIP_OUTPUT_DIR}'."
        ),
    )
    parser.add_argument(
        "--state-file",
        default=str(DEFAULT_STATE_FILE),
        help=(
            "File JSON lưu version/sha256/URL. "
            f"Mặc định (sửa ở biến STATE_FILE đầu file): '{DEFAULT_STATE_FILE}'."
        ),
    )
    parser.add_argument(
        "--pending-upload-out",
        default=str(DEFAULT_PENDING_UPLOAD_OUT),
        help=(
            "File JSON danh sách zip cần upload. "
            f"Mặc định (sửa ở biến PENDING_UPLOAD_OUT đầu file): '{DEFAULT_PENDING_UPLOAD_OUT}'."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())