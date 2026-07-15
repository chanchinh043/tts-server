# tts_server/oauth_setup.py
#
# CHẠY FILE NÀY ĐÚNG 1 LẦN (thủ công, trực tiếp bằng `python -m
# tts_server.oauth_setup`, KHÔNG phải qua uvicorn) để lấy "refresh token"
# cho TÀI KHOẢN GMAIL CÁ NHÂN thật sự sở hữu folder Drive
# (TTS_DRIVE_ROOT_FOLDER_ID) — vì service account KHÔNG có quota lưu trữ
# riêng (Gmail cá nhân không có Shared Drive để né giới hạn này, xem lỗi
# 403 storageQuotaExceeded đã gặp).
#
# ── CÁCH DÙNG ─────────────────────────────────────────────────────────────
#   1. Tải file OAuth Client ID (Desktop app) từ Google Cloud Console →
#      đặt tên "oauth_client_secret.json", copy vào credentials/ (cạnh
#      service_account.json, cùng thư mục credentials/ đã có sẵn).
#   2. Chạy (từ project root, đã activate venv):
#        python -m tts_server.oauth_setup
#   3. Trình duyệt tự mở — ĐĂNG NHẬP ĐÚNG tài khoản Gmail sở hữu folder
#      Drive TTS_DRIVE_ROOT_FOLDER_ID, bấm "Allow"/"Cho phép".
#   4. Script tự lưu refresh token vào credentials/oauth_token.json —
#      drive_upload.py (bước sau) sẽ TỰ ĐỘNG dùng file này để upload, KHÔNG
#      cần đăng nhập lại, kể cả khi restart server (refresh token không hết
#      hạn trừ khi bạn tự thu hồi quyền truy cập ở Google Account settings).
#
# ⚠️ KHÔNG commit oauth_client_secret.json / oauth_token.json lên git —
# thêm "credentials/" vào .gitignore (cùng lý do service_account.json).
#
# ── CÀI ĐẶT CẦN THÊM ─────────────────────────────────────────────────────
#   pip install google-auth-oauthlib
from __future__ import annotations

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from tts_server.config import CONFIG

_SCOPES = ["https://www.googleapis.com/auth/drive"]


def main() -> None:
    client_secret_path = CONFIG.drive_oauth_client_secret_path
    token_path = CONFIG.drive_oauth_token_path

    if not client_secret_path.exists():
        print(
            f"LỖI: không tìm thấy {client_secret_path}\n"
            f"  → Tải file OAuth Client ID (Desktop app) từ Google Cloud Console, "
            f"đặt tên đúng 'oauth_client_secret.json', copy vào {client_secret_path.parent}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Đang dùng client secret: {client_secret_path}")
    print("Trình duyệt sẽ tự mở — ĐĂNG NHẬP ĐÚNG tài khoản Gmail sở hữu folder Drive rồi bấm Allow.\n")

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), _SCOPES)
    credentials = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")

    print(f"\n✓ Xong! Đã lưu refresh token vào: {token_path}")
    print("  Bây giờ server có thể upload lên Drive bằng chính tài khoản này, không cần đăng nhập lại.")


if __name__ == "__main__":
    main()
