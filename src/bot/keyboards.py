"""Inline keyboards and callback_data codes for bot v2.

Telegram limits callback_data to 64 bytes, so jobs are addressed by the 32-hex
identity hash and every other choice by a short code.  Layout of the data:

- ``j:<action>:<card no>:<identity>``  job buttons (s=save h=hide u=👍 d=👎 r=unsave)
- ``m``                                 next page of the current list
- ``fo:<company>``                      follow a company
- ``st:<field>[:<value>]``              /settings
- ``f:<category>[:<value>]``            /filter wizard
- ``o<step>:<value>``                   onboarding on /start
- ``go:<command>``                      shortcuts to commands
"""

from __future__ import annotations

import re

from src.bot.filters import (
    CITY_CHOICES,
    JOB_TYPE_CHOICES,
    LEVEL_CHOICES,
    SALARY_CHOICES,
    job_key,
)

MAX_CALLBACK_BYTES = 64

FREQUENCY_CHOICES = {
    "instant": "Ngay khi có",
    "twice": "2 lần/ngày",
    "daily": "1 lần/ngày",
    "weekly": "Hằng tuần",
}
MAX_JOBS_CHOICES = (5, 10, 15)

ONBOARD_OCCUPATIONS = {
    "ketoan": "kế toán",
    "it": "lập trình viên",
    "sales": "nhân viên kinh doanh",
    "mkt": "marketing",
    "data": "data analyst",
    "hr": "nhân sự",
    "design": "thiết kế đồ họa",
    "cskh": "chăm sóc khách hàng",
}


def button(text: str, data: str) -> dict:
    encoded = data.encode("utf-8")
    if len(encoded) > MAX_CALLBACK_BYTES:
        data = encoded[:MAX_CALLBACK_BYTES].decode("utf-8", errors="ignore")
    return {"text": text, "callback_data": data}


def inline(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": [row for row in rows if row]}


def job_buttons(jobs: list[dict], *, start: int = 1, saved_list: bool = False,
                more: bool = False, extra_rows: list[list[dict]] | None = None) -> dict:
    """One row of buttons per card on the page, numbered like the cards."""
    rows = []
    for number, job in enumerate(jobs, start):
        key = job_key(job)
        if saved_list:
            rows.append([
                button(f"🗑 Bỏ lưu {number}", f"j:r:{number}:{key}"),
                button(f"🙈 {number}", f"j:h:{number}:{key}"),
            ])
        else:
            rows.append([
                button(f"💾 {number}", f"j:s:{number}:{key}"),
                button(f"🙈 {number}", f"j:h:{number}:{key}"),
                button(f"👍 {number}", f"j:u:{number}:{key}"),
                button(f"👎 {number}", f"j:d:{number}:{key}"),
            ])
    rows.extend(extra_rows or [])
    if more:
        rows.append([button("➡️ Xem thêm", "m")])
    return inline(rows)


def follow_button(company_normalized: str) -> list[dict]:
    return [button("🔔 Theo dõi công ty", f"fo:{company_normalized}")]


def _mark(selected: bool) -> str:
    return "✅ " if selected else ""


def settings_keyboard(prefs: dict, default_max: int) -> dict:
    frequency = prefs.get("frequency") or "instant"
    max_jobs = int(prefs.get("max_jobs") or 0) or default_max
    quiet = bool(prefs.get("quiet_hours"))
    freq = [button(_mark(code == frequency) + label, f"st:fq:{code}")
            for code, label in FREQUENCY_CHOICES.items()]
    return inline([
        freq[:2],
        freq[2:],
        [button(("🌙 Giờ im lặng: BẬT" if quiet else "🔔 Giờ im lặng: TẮT"), "st:qh")],
        [button(_mark(n == max_jobs) + f"{n} tin/lần", f"st:mx:{n}") for n in MAX_JOBS_CHOICES],
        [button("🇻🇳 Tiếng Việt", "st:lg")],
        [button("🎛 Bộ lọc", "go:filter"), button("🚫 Loại trừ", "go:exclude")],
        [button("📋 Đăng ký", "go:list"), button("🕘 Lịch sử", "go:history")],
        [button("🌐 Tìm trên web", "go:web"), button("🏢 Công ty theo dõi", "go:follow")],
    ])


def filter_home_keyboard() -> dict:
    return inline([
        [button("📍 Địa điểm", "f:loc"), button("💰 Lương", "f:sal")],
        [button("🎓 Cấp bậc", "f:lvl"), button("🕘 Hình thức", "f:typ")],
        [button("♻️ Xóa bộ lọc", "f:reset"), button("✅ Xong", "f:done")],
    ])


FILTER_FIELDS = {
    "loc": ("filter_location", CITY_CHOICES),
    "sal": ("filter_salary_min", {code: ("Không giới hạn" if not value
                                         else f"Từ {value // 1_000_000} triệu")
                                  for code, value in SALARY_CHOICES.items()}),
    "lvl": ("filter_level", LEVEL_CHOICES),
    "typ": ("filter_job_type", JOB_TYPE_CHOICES),
}


def _pairs(buttons: list[dict], width: int = 2) -> list[list[dict]]:
    return [buttons[i:i + width] for i in range(0, len(buttons), width)]


def filter_options_keyboard(category: str, prefs: dict) -> dict:
    field, choices = FILTER_FIELDS[category]
    current = str(prefs.get(field) if prefs.get(field) is not None else "")
    if category == "sal":
        current = str(int(prefs.get(field) or 0) // 1_000_000)
    buttons = [button(_mark(code == current) + label, f"f:{category}:{code}")
               for code, label in choices.items()]
    return inline([*_pairs(buttons), [button("⬅️ Quay lại", "f:home")]])


def onboarding_keyboard(step: int) -> dict:
    if step == 1:
        buttons = [button(label.capitalize(), f"o1:{code}")
                   for code, label in ONBOARD_OCCUPATIONS.items()]
        return inline([*_pairs(buttons), [button("✍️ Tự nhập nghề khác", "o1:custom")]])
    if step == 2:
        buttons = [button(label, f"o2:{code}") for code, label in CITY_CHOICES.items()]
        return inline(_pairs(buttons, 3))
    buttons = [button(label, f"o3:{code}") for code, label in LEVEL_CHOICES.items()]
    return inline(_pairs(buttons, 3))


# ── Reading a card back from the message the button belongs to ──────

_CARD_HEAD = re.compile(r"^(\d+)\. (.+)$")


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def parse_card(message: dict, number: int) -> dict | None:
    """Recover title/company/location/salary/url of card ``number`` from a message.

    Callback queries carry the original message (plain text + entities), so a
    button press can be stored without reading the jobs table.
    """
    text = str((message or {}).get("text") or "")
    if not text:
        return None
    lines = text.split("\n")
    offset = 0
    card: dict | None = None
    for line in lines:
        head = _CARD_HEAD.match(line)
        if card is None:
            if head and int(head.group(1)) == number:
                card = {"title": head.group(2).strip()}
        elif not line.strip() or head:
            break
        elif line.startswith("🏢 "):
            card["company"] = line[2:].strip()
        elif line.startswith("📍 "):
            card["location"] = line[2:].strip()
        elif line.startswith("💰 "):
            card["salary_raw"] = line[2:].strip()
        elif line.startswith("🔗 "):
            card["source"] = line[2:].strip()
            start, end = offset, offset + _utf16_len(line)
            for entity in message.get("entities") or []:
                if (entity.get("type") == "text_link"
                        and start <= int(entity.get("offset", -1)) < end):
                    card["source_url"] = entity.get("url", "")
                    break
        offset += _utf16_len(line) + 1
    if card is None:
        return None
    if card.get("company") == "Chưa rõ công ty":
        card["company"] = ""
    return card
