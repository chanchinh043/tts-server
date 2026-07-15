# tts_server/drive_upload.py
#
# Bước 7/9: đóng gói zip (gọi thẳng build_zip() từ step3_package_zip.py,
# KHÔNG copy logic) rồi upload lên ĐÚNG folder Drive hiện tại
# (TTS_DRIVE_ROOT_FOLDER_ID) — dùng LẠI đúng service account JSON mà
# TtsServiceAccountAuth.kt bên Android đang dùng để ĐỌC, xem quyết định đã
# chốt: "kết quả server tổng hợp xong vẫn upload vào ĐÚNG folder Drive
# Kokoro đang dùng — TtsGoogleDriveSource/TtsKokoroPackDownloader không cần
# biết gì về config này".
#
# ⚠️ KHÔNG dùng version_state.json/pending_upload.json của step3 ở đây —
# 2 file state đó phục vụ pipeline BATCH THỦ CÔNG (chạy 1 lần cho hàng loạt
# bài, cần resume/versioning qua nhiều lần chạy). Ở đây là job ĐƠN LẺ theo
# yêu cầu Android (1 job = 1 (readingId, sid, contentHash)) — không cần
# theo dõi version phức tạp, chỉ cần: build đúng 1 zip, upload/GHI ĐÈ đúng
# 1 file trên Drive theo tên "{readingId}_{sid}.zip", xong là xong. Nếu
# contentHash đổi ở lần request sau (bài bị sửa nội dung), job MỚI sẽ tự
# build lại zip mới và GHI ĐÈ đúng file đó trên Drive — TtsKokoroPackDownloader
# bên Android vốn đã tự phát hiện gói mới qua checksum lúc tải, không cần gì
# thêm ở phía server để báo "có bản mới".
#
# ⚠️ QUY ƯỚC TÊN FILE TRÊN DRIVE — PHẢI đúng "{reading_id}_{sid}.zip" (xem
# ghi chú gốc ở đầu cuộc trò chuyện, TtsGoogleDriveSource đọc theo đúng quy
# ước này) — KHÔNG thêm hash/version vào tên file trên Drive, Android tìm
# file theo đúng tên này, không phải theo hash.
#
# ── CÀI ĐẶT CẦN THIẾT ────────────────────────────────────────────────────
#   pip install google-auth google-api-python-client --break-system-packages
#
# ── SCOPE BẮT BUỘC ────────────────────────────────────────────────────────
# Phải xin scope ĐẦY ĐỦ (đọc + ghi), KHÔNG dùng scope readonly dù Android
# đang dùng scope hẹp hơn cho mục đích của nó — đây là việc xin quyền ở
# TẦNG CLIENT (server tự yêu cầu khi tạo credentials), hoàn toàn độc lập với
# cách Android cấu hình scope cho chính nó, KHÔNG đụng/sửa gì tới
# TtsServiceAccountAuth.kt hay file JSON.
# ⚠️ CẬP NHẬT SAU LỖI 403 storageQuotaExceeded — Service Account KHÔNG có
# quota lưu trữ riêng, KHÔNG thể tạo file mới trong 1 folder Drive cá nhân
# (My Drive) thông thường (chỉ hoạt động nếu folder nằm trong Shared Drive —
# tính năng chỉ có ở Google Workspace, KHÔNG có ở Gmail cá nhân). Vì folder
# TTS_DRIVE_ROOT_FOLDER_ID nằm trên Gmail cá nhân, đã đổi sang dùng OAuth
# với CHÍNH tài khoản Gmail sở hữu folder đó (có quota thật) — xem
# oauth_setup.py để lấy refresh token 1 lần. Service account JSON cũ
# (credentials_path) KHÔNG còn dùng để upload nữa, chỉ giữ lại cho mục đích
# đọc nếu cần sau này.
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger("tts_server.drive_upload")

_SCOPES = ["https://www.googleapis.com/auth/drive"]

# ── Chèn pipeline/ vào sys.path (giống kokoro_engine.py/audio_encode.py) —
# import build_zip() trực tiếp từ step3_package_zip.py, không copy logic. ──
_PIPELINE_DIR = Path(__file__).resolve().parent / "pipeline"
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

try:
    from step3_package_zip import build_zip  # type: ignore
except ImportError as e:
    raise ImportError(
        f"Không import được step3_package_zip.py từ {_PIPELINE_DIR}. "
        f"Kiểm tra lại file đã được copy vào đúng tts_server/pipeline/ chưa. "
        f"Lỗi gốc: {e}"
    ) from e


@dataclass
class DriveUploadResult:
    """Kết quả đóng gói + upload — dùng bởi process_job() (bước 8) để quyết
    định job này 'ready' (success=True) hay 'failed' (success=False).

    success:  True nếu build zip + upload/ghi đè Drive đều thành công.
    file_id:  Drive file ID của zip vừa upload — None nếu success=False.
    error:    thông điệp lỗi ngắn gọn nếu success=False.
    """
    success: bool
    file_id: Optional[str] = None
    error: Optional[str] = None


def _get_drive_service(token_path: str):
    """Tạo Drive API service bằng OAuth credentials (KHÔNG phải service
    account) — đọc refresh token đã lưu sẵn bởi oauth_setup.py (chạy 1 lần
    thủ công). Tự động REFRESH access token nếu đã hết hạn (access token
    chỉ sống ~1 giờ, refresh token thì gần như vĩnh viễn cho tới khi bị thu
    hồi thủ công ở Google Account settings) — VÀ tự ghi lại file token_path
    sau khi refresh, để lần gọi SAU không phải refresh lại từ đầu.

    token_path: đường dẫn file JSON do oauth_setup.py tạo ra (chứa
                refresh_token) — KHÔNG phải service_account.json.
    """
    path = Path(token_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Không tìm thấy OAuth token tại {path} — chạy "
            f"'python -m tts_server.oauth_setup' (1 lần) để tạo file này trước."
        )

    credentials = Credentials.from_authorized_user_file(str(path), _SCOPES)

    if not credentials.valid:
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            # ── Ghi lại access token mới vừa refresh — access token cũ đã
            # hết hạn, KHÔNG ghi lại thì lần gọi kế tiếp lại phải refresh
            # từ đầu (vẫn hoạt động đúng nhưng tốn 1 lượt gọi mạng vô ích
            # mỗi lần start service). ─────────────────────────────────────
            path.write_text(credentials.to_json(), encoding="utf-8")
        else:
            raise RuntimeError(
                f"OAuth token tại {path} không hợp lệ và không tự refresh được — "
                f"chạy lại 'python -m tts_server.oauth_setup' để lấy token mới."
            )

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _find_existing_file_id(service, folder_id: str, filename: str) -> Optional[str]:
    # ── Tìm file CÙNG TÊN đã có sẵn trong folder (nếu có) — để UPDATE
    # (ghi đè nội dung, giữ nguyên file_id) thay vì tạo file MỚI trùng tên,
    # tránh Drive có 2 file cùng tên "{readingId}_{sid}.zip" gây nhầm lẫn
    # khi Android liệt kê/tìm file theo tên. escape dấu ' trong tên file
    # (hiếm khi xảy ra vì tên chỉ gồm UUID + số, nhưng phòng hờ). ──────────
    safe_name = filename.replace("'", "\\'")
    query = (
        f"'{folder_id}' in parents and name = '{safe_name}' "
        f"and trashed = false"
    )
    response = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
        pageSize=1,
    ).execute()
    files = response.get("files", [])
    return files[0]["id"] if files else None


def upload_or_replace_zip(
    zip_path: Path,
    filename: str,
    folder_id: str,
    token_path: str,
) -> DriveUploadResult:
    """Upload 1 file zip lên Drive — GHI ĐÈ nếu đã có file cùng tên trong
    folder, tạo mới nếu chưa có.

    zip_path:  đường dẫn file .zip cục bộ đã build sẵn (xem
               package_job_to_zip() bên dưới).
    filename:  tên file trên Drive — PHẢI đúng "{reading_id}_{sid}.zip".
    folder_id: TTS_DRIVE_ROOT_FOLDER_ID — folder Drive hiện tại đang dùng
               chung cho cả bài hệ thống lẫn MyReading, do server tự cấu
               hình (biến môi trường/config riêng, KHÔNG hardcode ở đây).
    token_path: đường dẫn file OAuth token (tạo bởi oauth_setup.py, xem
               ghi chú đầu file) — KHÔNG phải service_account.json, vì
               service account không có quota lưu trữ để upload.
    """
    if not zip_path.exists():
        return DriveUploadResult(success=False, error=f"zip_path không tồn tại: {zip_path}")

    try:
        service = _get_drive_service(token_path)
    except Exception as e:
        logger.exception("upload_or_replace_zip: lỗi tạo Drive service")
        return DriveUploadResult(success=False, error=f"lỗi xác thực OAuth: {e}")

    try:
        media = MediaFileUpload(str(zip_path), mimetype="application/zip", resumable=False)
        existing_id = _find_existing_file_id(service, folder_id, filename)

        if existing_id:
            # ── Ghi đè NỘI DUNG, giữ nguyên file_id — Android nếu có lưu
            # cache theo file_id ở đâu đó (hiện tại thì không, tìm theo tên)
            # vẫn không bị ảnh hưởng, và không tạo rác nhiều bản trùng tên
            # trên Drive qua nhiều lần job chạy lại (bài bị sửa nội dung
            # nhiều lần). ────────────────────────────────────────────────
            service.files().update(fileId=existing_id, media_body=media).execute()
            logger.info("upload_or_replace_zip: đã GHI ĐÈ file_id=%s (%s)", existing_id, filename)
            return DriveUploadResult(success=True, file_id=existing_id)
        else:
            file_metadata = {"name": filename, "parents": [folder_id]}
            created = service.files().create(
                body=file_metadata, media_body=media, fields="id"
            ).execute()
            new_id = created["id"]
            logger.info("upload_or_replace_zip: đã TẠO MỚI file_id=%s (%s)", new_id, filename)
            return DriveUploadResult(success=True, file_id=new_id)
    except Exception as e:
        logger.exception("upload_or_replace_zip: lỗi upload %s", filename)
        return DriveUploadResult(success=False, error=f"lỗi upload Drive: {e}")


def package_job_to_zip(ogg_paths: list[Path], zip_output_dir: Path, reading_id: str, sid: int) -> Path:
    """Đóng gói toàn bộ .ogg của 1 job (đã encode ở bước 6) thành 1 zip —
    gọi THẲNG build_zip() từ step3_package_zip.py, giữ đúng quy ước ZIP
    PHẲNG (arcname=chỉ tên file, không kèm thư mục cha) mà build_zip() đã
    tự đảm bảo — xem comment gốc ở step3 về extractZip() bên Kotlin.

    Trả về đường dẫn zip vừa tạo — KHÔNG tự upload, gọi riêng
    upload_or_replace_zip() sau khi có đường dẫn này (tách 2 việc để
    process_job() (bước 8) dễ retry riêng từng phần nếu 1 trong 2 bước lỗi).
    """
    zip_path = zip_output_dir / f"{reading_id}_{sid}.zip"
    build_zip(ogg_paths, zip_path)
    return zip_path