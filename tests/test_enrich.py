"""Rule-based enrichment (src/etl/enrich.py) and cross-board grouping."""

import json

import pytest

from src.common.models import Job, JobLevel, JobSource, RawJob
from src.etl import enrich
from src.etl.deduplicator import (
    canonical_group_id,
    group_duplicates,
    normalize_company,
    normalize_title_for_group,
)
from src.etl.transformer import Transformer


@pytest.mark.parametrize("title, expected", [
    ("Thực tập sinh Marketing", "intern"),
    ("Marketing Intern", "intern"),
    ("Fresher QA Engineer", "fresher"),
    ("Junior/Middle Java Developer", "junior"),
    ("Middle PHP Developer", "mid"),
    ("Senior Data Engineer", "senior"),
    ("Senior Developer / Team Lead", "senior"),
    ("Trưởng nhóm kinh doanh", "lead"),
    ("Tổ trưởng sản xuất", "lead"),
    ("Trưởng phòng Kế toán", "manager"),
    ("Giám đốc kinh doanh (Sales Director)", "manager"),
    ("Kế toán trưởng", "manager"),
    ("Quản lý cửa hàng", "manager"),
    ("Nhân viên quản lý kho", "unknown"),
    ("Nhân viên kinh doanh", "unknown"),
])
def test_level_from_title(title, expected):
    assert enrich.extract_level(title) == expected


def test_level_falls_back_to_experience_then_fresher_cues():
    assert enrich.extract_level("Kế toán viên", "", 5) == "senior"
    assert enrich.extract_level("Kế toán viên", "", 3) == "mid"
    assert enrich.extract_level("Kế toán viên", "", 1) == "junior"
    assert enrich.extract_level("Kế toán viên", "Chấp nhận sinh viên mới ra trường", 0) \
        == "fresher"


@pytest.mark.parametrize("text, expected", [
    ("Có 2-3 năm kinh nghiệm ở vị trí tương đương", (2, 3)),
    ("Có kinh nghiệm từ 1 năm trở lên", (1, None)),
    ("Tối thiểu 03 năm kinh nghiệm kế toán", (3, None)),
    ("Không yêu cầu kinh nghiệm, được đào tạo", (0, None)),
    ("Kinh nghiệm: Không yêu cầu", (0, None)),
    ("3+ years of experience with Python", (3, None)),
    ("At least 5 years experience in sales", (5, None)),
    ("Kinh nghiệm: 6 tháng", (0, None)),
    ("Kinh nghiệm dưới 1 năm", (0, 1)),
    ("Độ tuổi 22-35 tuổi. Thử việc 2 tháng", (None, None)),
    ("Nghỉ phép 12 ngày/năm", (None, None)),
])
def test_experience(text, expected):
    assert enrich.extract_experience(text) == expected


def test_experience_prefers_requirements_over_description():
    assert enrich.extract_experience("Ít nhất 4 năm kinh nghiệm", "2 năm kinh nghiệm") == (4, None)


def test_benefits():
    text = ("Phúc lợi: BHXH đầy đủ, bảo hiểm sức khỏe PVI, lương tháng 13, thưởng KPI, "
            "du lịch hằng năm, cấp laptop, làm việc hybrid, nghỉ thứ 7, 12 ngày phép năm")
    assert set(enrich.extract_benefits(text)) >= {
        "insurance", "health_insurance", "13th_month", "bonus", "travel", "laptop", "hybrid",
        "weekend_off", "annual_leave",
    }
    assert enrich.extract_benefits("Mô tả công việc: lập trình backend") == []
    assert set(enrich.BENEFIT_LABELS) >= set(enrich._BENEFIT_CUES)


@pytest.mark.parametrize("title, text, expected", [
    ("Kế toán tổng hợp", "", "ke_toan_tai_chinh"),
    ("Chuyên viên quan hệ khách hàng cá nhân - Ngân hàng", "", "ngan_hang_bao_hiem"),
    ("Nhân viên kinh doanh", "Tìm kiếm khách hàng, đạt doanh số", "kinh_doanh_ban_hang"),
    ("Nhân viên kinh doanh bất động sản", "", "bat_dong_san"),
    ("Digital Marketing Executive", "", "marketing"),
    ("Senior Java Developer", "Spring Boot, microservices", "it_phan_mem"),
    ("IT Helpdesk", "Hỗ trợ người dùng, quản trị mạng nội bộ", "it_ha_tang"),
    ("Chuyên viên tuyển dụng", "", "nhan_su"),
    ("Lễ tân văn phòng", "", "hanh_chinh"),
    ("Chuyên viên pháp chế", "", "luat"),
    ("Nhân viên xuất nhập khẩu", "Làm chứng từ, khai hải quan", "logistics"),
    ("Kỹ sư cơ khí", "Thiết kế khuôn, vận hành máy CNC", "san_xuat"),
    ("Kỹ sư điện tự động hóa", "Lập trình PLC", "ky_thuat_dien"),
    ("Kỹ sư kết cấu", "Thiết kế kết cấu công trình", "xay_dung"),
    ("Điều dưỡng viên", "", "y_te"),
    ("Giáo viên tiếng Anh", "", "giao_duc"),
    ("Nhân viên pha chế", "", "fnb_khach_san"),
    ("Graphic Designer", "Photoshop, Illustrator", "thiet_ke"),
    ("Công nhân may", "", "lao_dong_pho_thong"),
    ("Vận hành sàn thương mại điện tử", "", "kinh_doanh_ban_hang"),
    ("Xyz", "", "khac"),
])
def test_industry(title, text, expected):
    assert enrich.classify_industry(title, text) == expected
    assert expected in enrich.INDUSTRY_LABELS


@pytest.mark.parametrize("location, text, city, expected", [
    ("Quận 7, TP.HCM", "", "Ho Chi Minh", "Quận 7"),
    ("Q.1, Hồ Chí Minh", "", "Ho Chi Minh", "Quận 1"),
    ("Cầu Giấy, Hà Nội", "", "Ha Noi", "Cầu Giấy"),
    ("Hà Nội", "Địa chỉ: Tầng 5, 18 Tam Trinh, Hoàng Mai, Hà Nội", "Ha Noi", "Hoàng Mai"),
    ("Đà Nẵng", "Địa điểm làm việc: Hải Châu", "Da Nang", "Hải Châu"),
    ("Hà Nội", "", "Ha Noi", ""),
    ("Quận 3, Hà Nội", "", "Ha Noi", ""),  # numbered districts are HCM only
])
def test_district(location, text, city, expected):
    assert enrich.extract_district(location, text, city) == expected


@pytest.mark.parametrize("raw, expected", [
    ("15 - 20 triệu", "month"),
    ("15-20 triệu/tháng", "month"),
    ("$30,000/year", "year"),
    ("25.000đ/giờ", "hour"),
    ("300k/ngày", "day"),
    ("Thỏa thuận", ""),
    ("", ""),
])
def test_salary_period(raw, expected):
    assert enrich.salary_period(raw) == expected


def test_skills_are_multi_industry_and_token_bounded():
    skills = enrich.extract_skills(
        "Kế toán", "Sử dụng thành thạo MISA, Excel, Tiếng Anh giao tiếp. AutoCAD là lợi thế. "
        "Sắp tới công ty mở rộng. JavaScript, ReactJS, Node.js"
    )
    assert {"misa", "excel", "tiếng anh", "autocad", "javascript", "react"} <= set(skills)
    assert "sap" not in skills  # "Sắp" (soon) is not SAP
    assert "java" not in skills  # token boundaries: JavaScript is not Java


def test_skill_dictionary_file_is_small_and_valid():
    data = json.loads(enrich.SKILL_DICTIONARY_PATH.read_text(encoding="utf-8"))
    assert enrich.SKILL_DICTIONARY_PATH.stat().st_size <= 300 * 1024
    assert 1000 <= len(data["skills"]) <= 3500
    assert all(isinstance(v, list) for v in data["skills"].values())


def test_skill_matcher_handles_aliases_and_case_sensitive_words():
    matcher = enrich.SkillMatcher({"meta ads": ["facebook ads"], "ai": [], "word": []})
    assert matcher.find("Chạy Facebook Ads và AI") == ["meta ads", "ai"]
    assert matcher.find("ai cũng làm được, word of mouth") == []


def test_transform_fills_v2_fields_and_tags_equal_skills():
    raw = RawJob(
        title="Senior Kế Toán Tổng Hợp",
        company="Công ty TNHH ABC",
        location="Quận 7, TP.HCM",
        salary_raw="15 - 20 triệu/tháng",
        description="Thành thạo MISA, Excel. Có ít nhất 3 năm kinh nghiệm. Phúc lợi: BHXH, "
                    "lương tháng 13, du lịch hằng năm." * 3,
        requirements="Tốt nghiệp chuyên ngành kế toán",
        posted_at_raw="2 ngày trước",
        source=JobSource.TOPCV,
        source_url="https://www.topcv.vn/viec-lam/ke-toan/1.html",
    )
    job = Transformer().transform(raw)
    assert job.level == JobLevel.SENIOR
    assert (job.experience_min_years, job.experience_max_years) == (3, None)
    assert job.industry == "ke_toan_tai_chinh"
    assert {"misa", "excel"} <= set(job.skills) and job.tags == job.skills
    assert {"insurance", "13th_month", "travel"} <= set(job.benefits)
    assert job.salary_period == "month" and job.district == "Quận 7"
    assert job.canonical_group_id == canonical_group_id(job)
    assert 60 <= job.quality_score <= 100
    item = job.to_dynamo_item()
    assert item["level"] == "senior" and item["experience_min_years"] == 3
    assert item["industry"] == "ke_toan_tai_chinh" and item["district"] == "Quận 7"
    assert isinstance(item["quality_score"], int) and "experience_max_years" not in item


def test_quality_score_rewards_completeness():
    bare = Job(title="Kế toán", source=JobSource.CAREERLINK)
    assert enrich.quality_score(bare) == 10
    full = Transformer().transform(RawJob(
        title="Junior Kế toán", company="ACME", location="Hà Nội", salary_raw="10-12 triệu",
        description="x" * 250, requirements="1 năm kinh nghiệm, Excel",
        posted_at_raw="2026-10-01", valid_through_raw="2026-11-30", source=JobSource.CAREERLINK,
    ))
    assert full.quality_score == 100


# ── Cross-board grouping ────────────────────────────────────────


@pytest.mark.parametrize("a, b", [
    ("Công ty TNHH ABC Việt Nam", "ABC Co., Ltd"),
    ("CÔNG TY CỔ PHẦN GIẢI PHÁP THIÊN HOÀNG", "Công ty CP Giải Pháp Thiên Hoàng"),
    ("Công ty TNHH MTV Thương mại Dịch vụ Minh Phát", "Minh Phát"),
    ("FPT Software Company Limited", "FPT SOFTWARE"),
])
def test_company_normalisation(a, b):
    assert normalize_company(a) == normalize_company(b) != ""


@pytest.mark.parametrize("a, b", [
    ("Senior Java Developer (Urgent)", "Java Developer"),
    ("Java Developer - Lương 20-30 triệu", "[HN] Java Developer"),
    ("Nhân viên kế toán tổng hợp tại Hà Nội", "Kế toán tổng hợp - Hà Nội"),
])
def test_title_normalisation_drops_noise(a, b):
    assert normalize_title_for_group(a) == normalize_title_for_group(b)


def test_title_normalisation_keeps_the_occupation():
    assert normalize_title_for_group("Tài xế xe tải") != normalize_title_for_group("Xe tải")
    assert normalize_title_for_group("Kế toán") != normalize_title_for_group("Kiểm toán")


def _job(source, url, title, company, location):
    return Transformer().transform(RawJob(
        title=title, company=company, location=location, source=source, source_url=url,
    ))


def test_canonical_group_id_merges_boards_but_keeps_job_id():
    jobsgo = _job(JobSource.JOBSGO, "https://jobsgo.vn/1", "Java Developer",
                  "Công ty Cổ phần Giải Pháp Thiên Hoàng", "Hà Nội")
    indeed = _job(JobSource.INDEED, "https://vn.indeed.com/2", "Senior Java Developer",
                  "CÔNG TY CP GIẢI PHÁP THIÊN HOÀNG", "Hà Nội, HN")
    other_city = _job(JobSource.TOPCV, "https://topcv.vn/3", "Java Developer",
                      "Thiên Hoàng JSC", "Đà Nẵng")
    assert jobsgo.canonical_group_id == indeed.canonical_group_id
    assert jobsgo.job_id != indeed.job_id
    assert other_city.canonical_group_id != jobsgo.canonical_group_id
    groups = group_duplicates([jobsgo, other_city, indeed])
    assert list(groups.values())[0] == [jobsgo, indeed]
    assert len(groups) == 2


def test_jobs_without_company_are_never_grouped():
    first = _job(JobSource.CHOTOT, "https://chotot.com/1", "Nhân viên kinh doanh", "", "Hà Nội")
    second = _job(JobSource.CHOTOT, "https://chotot.com/2", "Nhân viên kinh doanh", "", "Hà Nội")
    assert first.canonical_group_id != second.canonical_group_id


def test_group_duplicates_accepts_dynamodb_items():
    items = [
        {"job_id": "a", "title": "Kế toán", "company": "ACME JSC", "location": "Hà Nội"},
        {"job_id": "b", "title": "Kế toán (Gấp)", "company": "Công ty CP ACME",
         "location": "Hà Nội"},
        {"job_id": "c", "title": "Kế toán", "company": "ACME", "location": "TP.HCM",
         "canonical_group_id": "stored"},
    ]
    groups = group_duplicates(items)
    assert [[i["job_id"] for i in g] for g in groups.values()] == [["a", "b"], ["c"]]
    assert "stored" in groups
