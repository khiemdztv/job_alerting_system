"""Configure and verify ViecLamBot's Telegram slash-command menu."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


COMMANDS = [
    {"command": "start", "description": "Mở ViecLamBot"},
    {"command": "search", "description": "Tìm việc theo nghề và khu vực"},
    {"command": "myjobs", "description": "Xem việc phù hợp với đăng ký"},
    {"command": "subscribe", "description": "Tạo thông báo việc làm"},
    {"command": "saved", "description": "Tin đã lưu"},
    {"command": "settings", "description": "Tần suất, giờ im lặng, số tin mỗi lần"},
    {"command": "filter", "description": "Bộ lọc: địa điểm, lương, cấp bậc, hình thức"},
    {"command": "company", "description": "Tin đang tuyển của một công ty"},
    {"command": "trending", "description": "Nghề, kỹ năng, công ty nổi bật tuần này"},
    {"command": "history", "description": "Tin đã gửi 7 ngày qua"},
    {"command": "exclude", "description": "Loại trừ từ khóa hoặc công ty"},
    {"command": "follow", "description": "Theo dõi công ty"},
    {"command": "web", "description": "Tìm thêm việc mới trên Internet"},
    {"command": "list", "description": "Xem các thông báo đã đăng ký"},
    {"command": "unsubscribe", "description": "Hủy một thông báo việc làm"},
    {"command": "more", "description": "Xem trang kết quả tiếp theo"},
    {"command": "help", "description": "Xem hướng dẫn sử dụng"},
    {"command": "cancel", "description": "Hủy thao tác đang nhập"},
]


def configure() -> list[dict[str, str]]:
    from src.config import get_settings

    token = get_settings().telegram_bot_token
    if not token:
        raise RuntimeError("VIECLAMBOT_TELEGRAM_BOT_TOKEN is not configured")

    api_url = f"https://api.telegram.org/bot{token}"
    response = requests.post(
        f"{api_url}/setMyCommands",
        json={"commands": COMMANDS},
        timeout=15,
    )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram rejected setMyCommands")

    verify = requests.get(f"{api_url}/getMyCommands", timeout=15)
    verify.raise_for_status()
    received = verify.json().get("result", [])
    if received != COMMANDS:
        raise RuntimeError("Telegram command menu did not preserve the UTF-8 descriptions")
    return received


if __name__ == "__main__":
    commands = configure()
    print(
        json.dumps(
            {"ok": True, "commands": [item["command"] for item in commands]},
            ensure_ascii=False,
        )
    )
