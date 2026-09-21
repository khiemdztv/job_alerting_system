"""Bounded HTML job cards: escape text and URLs before sending to Telegram."""

from html import escape
from urllib.parse import urlsplit


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
