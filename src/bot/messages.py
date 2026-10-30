"""Bounded HTML job cards: escape text and URLs before sending to Telegram."""

from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlsplit

from src.matcher.search import published_at, timestamp

VN_TZ = timezone(timedelta(hours=7))


def _relative_day(moment: datetime, now: datetime) -> str:
    days = (now.astimezone(VN_TZ).date() - moment.astimezone(VN_TZ).date()).days
    if days <= 0:
        return "hôm nay"
    if days == 1:
        return "hôm qua"
    if days < 30:
        return f"{days} ngày trước"
    return moment.astimezone(VN_TZ).strftime("%d/%m/%Y")


def date_line(job: dict, now: datetime | None = None) -> str:
    """'🕒 Đăng 2 ngày trước · Hạn nộp 15/11/2026' from whatever dates the board gave.

    Boards that print no posting date fall back to when we first saw the job,
    labelled as such so the reader knows it is only a lower bound.
    """
    now = now or datetime.now(timezone.utc)
    parts = []
    posted = timestamp(job.get("posted_at"))
    if posted:
        parts.append(f"Đăng {_relative_day(posted, now)}")
    else:
        seen = published_at(job, now)
        if seen:
            parts.append(f"Thấy {_relative_day(seen, now)}")
    deadline = timestamp(job.get("expires_at"))
    if deadline:
        left = (deadline.astimezone(VN_TZ).date() - now.astimezone(VN_TZ).date()).days
        label = deadline.astimezone(VN_TZ).strftime("%d/%m/%Y")
        if left < 0:
            parts.append(f"Hạn nộp {label} (đã hết hạn)")
        elif left <= 3:
            parts.append(f"Hạn nộp {label} (còn {left} ngày)")
        else:
            parts.append(f"Hạn nộp {label}")
    return "🕒 " + " · ".join(parts) + "\n" if parts else ""


def clip(value, length: int) -> str:
    value = str(value or "")
    return value if len(value) <= length else value[: length - 1] + "…"


def escaped(value, budget: int) -> str:
    """Bound encoded text without splitting HTML entities or surrogate pairs."""
    result = ""
    for char in str(value or ""):
        part = escape(char)
        if len((result + part).encode("utf-16-le")) // 2 > budget - 1:
            return result + "…"
        result += part
    return result


def job_card(job: dict, number: int) -> str:
    text = (
        f"<b>{number}. {escaped(clip(job.get('title'), 160), 450)}</b>\n"
        f"🏢 {escaped(clip(job.get('company') or 'Chưa rõ công ty', 80), 220)}\n"
        f"📍 {escaped(clip(job.get('location') or 'Chưa rõ địa điểm', 80), 220)}\n"
        f"💰 {escaped(clip(job.get('salary_raw') or 'Thỏa thuận', 70), 180)}\n"
    )
    text += escaped(date_line(job), 120)
    url = str(job.get("source_url") or "")
    try:
        valid_url = urlsplit(url).scheme in {"https", "http"} and bool(urlsplit(url).netloc)
    except ValueError:
        valid_url = False
    source = escaped(clip(job.get("source") or "Nguồn tuyển dụng", 30), 100)
    if valid_url and len(escape(url, quote=True).encode("utf-16-le")) // 2 <= 1000:
        text += f'🔗 <a href="{escape(url, quote=True)}">{source}</a>\n'
    return text


def job_pages(jobs: list[dict], heading: str, page_size: int = 5):
    """Yield (message, jobs actually included); no silent truncation on alerts."""
    header = f"<b>{escaped(clip(heading, 160), 400)}</b>\n\n"
    text, included = header, []
    for number, job in enumerate(jobs, 1):
        card = job_card(job, number) + "\n"
        if included and (
            len(included) >= page_size or len((text + card).encode("utf-16-le")) // 2 > 3500
        ):
            yield text, included
            text, included = header, []
        text += card
        included.append(job)
    if included:
        yield text, included
