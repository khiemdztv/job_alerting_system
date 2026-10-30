from datetime import datetime, timedelta, timezone

from src.bot.messages import date_line, job_card

NOW = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def test_date_line_uses_posted_date_and_deadline():
    job = {"posted_at": iso(2), "expires_at": (NOW + timedelta(days=16)).isoformat()}
    assert date_line(job, NOW) == "🕒 Đăng 2 ngày trước · Hạn nộp 15/11/2026\n"


def test_date_line_marks_first_seen_when_board_gives_no_date():
    job = {"first_seen_at": iso(0.2), "scraped_at": iso(0.1)}
    assert date_line(job, NOW) == "🕒 Thấy hôm nay\n"


def test_date_line_warns_about_near_and_past_deadlines():
    soon = {"posted_at": iso(1), "expires_at": (NOW + timedelta(days=2)).isoformat()}
    assert "còn 2 ngày" in date_line(soon, NOW) and "hôm qua" in date_line(soon, NOW)
    past = {"posted_at": iso(40), "expires_at": (NOW - timedelta(days=1)).isoformat()}
    line = date_line(past, NOW)
    assert "đã hết hạn" in line and "20/09/2026" in line


def test_date_line_is_empty_without_any_date():
    assert date_line({}, NOW) == ""


def test_job_card_includes_the_date_line():
    card = job_card({"title": "Kế toán", "posted_at": iso(3), "source_url": "https://x.vn/1",
                     "source": "CareerViet"}, 1)
    assert "🕒 Đăng 3 ngày trước" in card
    assert card.index("💰") < card.index("🕒") < card.index("🔗")
