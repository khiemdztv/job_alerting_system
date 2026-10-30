"""Rule-based enrichment of processed jobs (ETL v2, docs/BIG_UPDATE_PLAN.md §6).

Everything here is deterministic, dependency-free and fast enough to run on
every posting inside the ETL Lambda:

* ``extract_level``       intern / fresher / junior / mid / senior / lead / manager
* ``extract_experience``  "2-3 năm kinh nghiệm", "từ 1 năm", "3+ years", ...
* ``extract_benefits``    benefit codes (``BENEFIT_LABELS`` has the Vietnamese text)
* ``classify_industry``   one of ``INDUSTRY_LABELS`` from title + text cues
* ``extract_district``    quận/huyện of Ho Chi Minh City, Ha Noi and Da Nang
* ``extract_skills``      multi-industry dictionary in ``src/data/skills_vi.json``
* ``salary_period``       month / year / day / hour as printed on the card
* ``quality_score``       0-100 completeness of a Job

All matching runs on accent-folded text (``src.common.text_utils``) with token
boundaries, so "Kế toán", "ke toan" and "KẾ TOÁN" are the same cue and "java"
never fires inside "javascript".
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

from src.common.logger import get_logger
from src.common.models import JobLevel
from src.common.text_utils import clean_vn_text, fold_vn_text, normalize_vn_text

if TYPE_CHECKING:  # pragma: no cover
    from src.common.models import Job, RawJob

logger = get_logger(__name__)

SKILL_DICTIONARY_PATH = Path(__file__).resolve().parent.parent / "data" / "skills_vi.json"
MAX_SKILLS = 30
MAX_BENEFITS = 12

# ── Labels ───────────────────────────────────────────────────────────────────

LEVELS = ("intern", "fresher", "junior", "mid", "senior", "lead", "manager", "unknown")
_LEVEL_RANK = {level: rank for rank, level in enumerate(LEVELS)}

INDUSTRY_LABELS: dict[str, str] = {
    "ke_toan_tai_chinh": "Kế toán / Tài chính",
    "ngan_hang_bao_hiem": "Ngân hàng / Bảo hiểm",
    "kinh_doanh_ban_hang": "Kinh doanh / Bán hàng",
    "marketing": "Marketing / Truyền thông",
    "it_phan_mem": "IT phần mềm",
    "it_ha_tang": "IT hạ tầng / Hệ thống",
    "nhan_su": "Nhân sự",
    "hanh_chinh": "Hành chính / Văn phòng",
    "luat": "Luật / Pháp chế",
    "logistics": "Logistics / Kho vận / Mua hàng",
    "san_xuat": "Sản xuất / Cơ khí / QA-QC",
    "ky_thuat_dien": "Điện / Điện tử / Viễn thông",
    "xay_dung": "Xây dựng / Kiến trúc",
    "bat_dong_san": "Bất động sản",
    "y_te": "Y tế / Dược",
    "giao_duc": "Giáo dục / Đào tạo",
    "fnb_khach_san": "F&B / Nhà hàng / Khách sạn / Du lịch",
    "thiet_ke": "Thiết kế / Media",
    "lao_dong_pho_thong": "Lao động phổ thông",
    "khac": "Khác",
}

BENEFIT_LABELS: dict[str, str] = {
    "insurance": "Bảo hiểm đầy đủ",
    "health_insurance": "Bảo hiểm sức khỏe",
    "13th_month": "Lương tháng 13",
    "bonus": "Thưởng (KPI, dự án, lễ tết)",
    "commission": "Hoa hồng",
    "salary_review": "Xét tăng lương",
    "travel": "Du lịch",
    "teambuilding": "Team building",
    "laptop": "Cấp laptop/thiết bị",
    "remote": "Làm việc từ xa",
    "hybrid": "Hybrid",
    "flexible_hours": "Giờ làm linh hoạt",
    "weekend_off": "Nghỉ thứ 7, Chủ nhật",
    "training": "Đào tạo",
    "promotion": "Lộ trình thăng tiến",
    "health_check": "Khám sức khỏe",
    "meal": "Ăn trưa / phụ cấp ăn",
    "transport": "Xăng xe / đưa đón",
    "phone": "Phụ cấp điện thoại",
    "housing": "Hỗ trợ nhà ở",
    "allowance": "Phụ cấp",
    "annual_leave": "Nghỉ phép năm",
    "stock": "Cổ phần / ESOP",
    "overtime_pay": "Lương tăng ca",
    "uniform": "Đồng phục",
}

# Curated multi-industry skills: canonical → aliases.  Always part of the
# dictionary (``scripts/build_skill_dictionary.py`` merges them with VietJobs) and
# the whole runtime vocabulary when ``src/data/skills_vi.json`` is missing.
CORE_SKILLS: dict[str, list[str]] = {
    # Office / accounting / ERP
    "tin học văn phòng": ["vi tính văn phòng", "microsoft office", "ms office", "office 365",
                          "microsoft 365", "tin học"],
    "excel": ["microsoft excel", "ms excel", "excel nâng cao"],
    "word": ["microsoft word", "ms word"],
    "powerpoint": ["power point", "microsoft powerpoint", "ms powerpoint", "ppt"],
    "google sheets": ["google sheet", "gg sheet", "google spreadsheet"],
    "misa": ["phần mềm misa", "kế toán misa", "misa sme"],
    "fast accounting": ["phần mềm fast", "fast"],
    "phần mềm kế toán": ["accounting software"],
    "sap": ["sap erp", "sap b1", "sap business one", "sap s4 hana", "sap s/4hana"],
    "oracle": ["oracle erp", "oracle db"],
    "erp": [],
    "crm": ["salesforce crm", "hubspot crm", "hệ thống crm"],
    "kế toán tổng hợp": [],
    "kế toán thuế": ["quyết toán thuế", "báo cáo thuế", "khai báo thuế"],
    "báo cáo tài chính": ["lập báo cáo tài chính"],
    "ifrs": [],
    "vas": [],
    "acca": [],
    "cpa": [],
    "cfa": [],
    "power bi": ["powerbi"],
    "tableau": [],
    "phân tích dữ liệu": ["data analysis", "phân tích số liệu"],
    # Marketing
    "meta ads": ["facebook ads", "fb ads", "facebook ads manager", "quảng cáo facebook"],
    "google ads": ["adwords", "google adwords", "quảng cáo google"],
    "tiktok ads": ["quảng cáo tiktok"],
    "seo": ["search engine optimization"],
    "sem": [],
    "google analytics": ["ga4"],
    "content marketing": [],
    "digital marketing": [],
    "email marketing": [],
    "social media": ["social media marketing", "mạng xã hội"],
    "livestream": ["live stream"],
    "copywriting": ["copywriter", "viết bài quảng cáo"],
    "canva": [],
    "capcut": ["cap cut"],
    # Design / media / engineering drawing
    "photoshop": ["adobe photoshop", "ps"],
    "illustrator": ["adobe illustrator", "ai illustrator"],
    "premiere": ["adobe premiere", "premiere pro", "adobe premiere pro"],
    "final cut pro": ["final cut"],
    "after effects": ["after effect", "adobe after effects", "ae"],
    "indesign": ["adobe indesign"],
    "lightroom": ["adobe lightroom"],
    "figma": [],
    "sketchup": ["sketch up"],
    "3ds max": ["3d max", "3dsmax", "3dmax"],
    "autocad": ["auto cad", "cad"],
    "revit": ["autodesk revit"],
    "solidworks": ["solid works", "solidwork"],
    "lumion": [],
    "bim": [],
    "etabs": [],
    "sap2000": ["sap 2000"],
    "inventor": ["autodesk inventor"],
    "catia": [],
    "plc": [],
    "ms project": ["microsoft project"],
    "đọc bản vẽ kỹ thuật": ["đọc hiểu bản vẽ kỹ thuật", "đọc và hiểu bản vẽ kỹ thuật",
                            "đọc bản vẽ", "đọc hiểu bản vẽ"],
    "dự toán": ["bóc tách khối lượng", "lập dự toán"],
    # Languages and certificates
    "tiếng anh": ["english", "anh văn", "tiếng anh giao tiếp"],
    "tiếng nhật": ["japanese", "nhật ngữ"],
    "tiếng hàn": ["korean", "hàn ngữ"],
    "tiếng trung": ["chinese", "mandarin", "tiếng hoa", "trung văn", "tiếng quan thoại"],
    "tiếng pháp": ["french"],
    "tiếng đức": ["german"],
    "tiếng thái": ["thai"],
    "ielts": [],
    "toeic": [],
    "jlpt": [],
    "hsk": [],
    "topik": [],
    # Business
    "đàm phán": ["kỹ năng đàm phán", "negotiation", "thương lượng"],
    "giao tiếp": ["kỹ năng giao tiếp", "communication skills"],
    "thuyết trình": ["kỹ năng thuyết trình", "presentation skills"],
    "làm việc nhóm": ["teamwork", "kỹ năng làm việc nhóm", "làm việc theo nhóm",
                      "phối hợp nhóm"],
    "quản lý thời gian": ["time management"],
    "giải quyết vấn đề": ["problem solving", "problem-solving"],
    "lãnh đạo": ["leadership", "kỹ năng lãnh đạo"],
    "quản lý dự án": ["project management"],
    "chăm sóc khách hàng": ["customer service", "cskh"],
    "telesales": ["telesale", "tele sale"],
    "b2b": [],
    "b2c": [],
    # Tech spellings (same canonical names as the legacy TECH_SKILLS tags)
    "react": ["reactjs", "react.js", "react js"],
    "vue": ["vuejs", "vue.js"],
    "angular": ["angularjs", "angular.js"],
    "node.js": ["nodejs", "node js"],
    "golang": [],
    "c#": ["c sharp"],
    "c++": ["cpp"],
    "bằng lái xe": ["bằng lái b2", "bằng lái c", "giấy phép lái xe"],
    "xuất nhập khẩu": ["import export", "xnk"],
    "khai báo hải quan": ["thủ tục hải quan", "customs clearance"],
}

# Short or everyday words that only count as skills when written exactly so.
_CASE_SENSITIVE: dict[str, re.Pattern] = {
    "ai": re.compile(r"AI"),
    "go": re.compile(r"Go|GO"),
    "word": re.compile(r"Word|WORD"),
    "access": re.compile(r"Access"),
    "sketch": re.compile(r"Sketch"),
    "swift": re.compile(r"Swift"),
    "spring": re.compile(r"Spring|SPRING"),
    "unity": re.compile(r"Unity|UNITY"),
    "dart": re.compile(r"Dart"),
    "notion": re.compile(r"Notion"),
    "fast": re.compile(r"FAST|Fast"),
    "oracle": re.compile(r"Oracle|ORACLE"),
    "sap": re.compile(r"SAP"),
    "bim": re.compile(r"BIM"),
    "cad": re.compile(r"CAD"),
    "ps": re.compile(r"PS"),
    "ae": re.compile(r"AE"),
    "vas": re.compile(r"VAS"),
    "sem": re.compile(r"SEM"),
    "thai": re.compile(r"Thai"),
    "french": re.compile(r"French"),
    "german": re.compile(r"German"),
}
# Never skills on their own (too ambiguous after folding / in English prose).
_BLOCKED_SKILL_KEYS = frozenset({
    "r", "c", "it", "go to", "office", "project", "teams", "ads", "data", "internet", "web",
    "sales", "sale", "marketing", "ke toan", "tin hoc", "may tinh", "su dung may tinh",
})


# ── Tokenising ───────────────────────────────────────────────────────────────

_TOKEN = re.compile(r"[^\W_]+[+#]*")
_ETH = str.maketrans({"Ð": "Đ", "ð": "đ"})


def _fold(text: str) -> str:
    return clean_vn_text(text)


def tokens(text: str) -> list[str]:
    """Folded lowercase tokens; '+'/'#' stay attached (c++, c#)."""
    return _TOKEN.findall(_fold(text))


def skill_key(text: str) -> str:
    """Dictionary key of a skill name: its folded tokens joined by one space."""
    return " ".join(tokens(text))


def _has_vn_diacritics(token: str) -> bool:
    normalized = normalize_vn_text(token)
    return fold_vn_text(normalized) != normalized


# ── Skills ───────────────────────────────────────────────────────────────────


class SkillMatcher:
    """Greedy longest-match lookup of skill n-grams on token boundaries."""

    def __init__(self, dictionary: dict[str, Iterable[str]]):
        self.index: dict[str, str] = {}
        self.ascii_keys: set[str] = set()
        for canonical, aliases in dictionary.items():
            for name in (canonical, *aliases):
                key = skill_key(name)
                if not key or key in _BLOCKED_SKILL_KEYS:
                    continue
                if len(key.split()) == 1 and _has_vn_diacritics(name):
                    continue  # a single Vietnamese syllable is a common word after folding
                self.index.setdefault(key, canonical)
                if not _has_vn_diacritics(name):
                    self.ascii_keys.add(key)
        self.max_n = max((k.count(" ") + 1 for k in self.index), default=1)
        self.prefixes = {k.split(" ", 1)[0] for k in self.index if " " in k}

    def __len__(self) -> int:
        return len(set(self.index.values()))

    def find(self, text: str, limit: int = MAX_SKILLS) -> list[str]:
        """Canonical skills in order of first appearance."""
        if not text:
            return []
        original = _TOKEN.findall(unicodedata.normalize("NFC", str(text).translate(_ETH)))
        folded = [fold_vn_text(t.lower()) for t in original]
        found: list[str] = []
        seen: set[str] = set()
        i, n_tokens = 0, len(folded)
        while i < n_tokens and len(found) < limit:
            matched = 0
            top = min(self.max_n, n_tokens - i) if folded[i] in self.prefixes else 1
            for n in range(top, 0, -1):
                key = folded[i] if n == 1 else " ".join(folded[i:i + n])
                canonical = self.index.get(key)
                if canonical is None or not self._accept(key, original[i:i + n]):
                    continue
                if canonical not in seen:
                    seen.add(canonical)
                    found.append(canonical)
                matched = n
                break
            i += matched or 1
        return found

    def _accept(self, key: str, spelled: list[str]) -> bool:
        # An English/tool name typed with Vietnamese tones is a different word:
        # "sắp" is not SAP and "bỉm" is not BIM.
        if key in self.ascii_keys and any(_has_vn_diacritics(t) for t in spelled):
            return False
        pattern = _CASE_SENSITIVE.get(key)
        return pattern is None or bool(pattern.fullmatch(spelled[0]))


@lru_cache(maxsize=1)
def load_skill_dictionary() -> dict[str, list[str]]:
    """``skills_vi.json`` merged with ``CORE_SKILLS`` and the legacy tech list."""
    from src.etl.transformer import TECH_SKILLS

    merged: dict[str, list[str]] = {}
    try:
        data = json.loads(SKILL_DICTIONARY_PATH.read_text(encoding="utf-8"))
        merged.update({k: list(v) for k, v in data.get("skills", {}).items()})
    except FileNotFoundError:
        logger.warning(f"Skill dictionary missing at {SKILL_DICTIONARY_PATH}; using core list")
    except (OSError, ValueError) as exc:
        logger.warning(f"Skill dictionary unreadable ({exc}); using core list")
    for canonical, aliases in CORE_SKILLS.items():
        merged[canonical] = sorted(set(merged.get(canonical, [])) | set(aliases))
    for skill in TECH_SKILLS:
        merged.setdefault(skill, [])
    return merged


@lru_cache(maxsize=1)
def skill_matcher() -> SkillMatcher:
    return SkillMatcher(load_skill_dictionary())


def extract_skills(*texts: str, limit: int = MAX_SKILLS) -> list[str]:
    return skill_matcher().find("\n".join(t for t in texts if t), limit=limit)


# ── Cue helpers ──────────────────────────────────────────────────────────────


def _cue_regex(cues: Iterable[str]) -> re.Pattern:
    keys = sorted({_fold(c) for c in cues}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(map(re.escape, keys)) + r")(?![a-z0-9])")


# ── Level ────────────────────────────────────────────────────────────────────

_LEVEL_TITLE_CUES: dict[str, tuple[str, ...]] = {
    "intern": ("thực tập sinh", "thực tập", "intern", "internship", "trainee", "học việc",
               "tts"),
    "fresher": ("fresher", "fresh graduate", "mới tốt nghiệp", "mới ra trường", "graduate",
                "entry level", "entry-level", "sinh viên mới ra trường"),
    "junior": ("junior", "jr"),
    "mid": ("middle", "mid level", "mid-level", "midlevel", "intermediate"),
    "senior": ("senior", "sr", "chuyên viên cao cấp", "chuyên gia", "expert", "principal",
               "staff engineer"),
    "lead": ("trưởng nhóm", "team lead", "team leader", "tech lead", "technical lead",
             "leader", "lead", "tổ trưởng", "trưởng ca", "ca trưởng", "nhóm trưởng",
             "giám sát", "supervisor", "phó nhóm", "trưởng nhóm kinh doanh"),
    "manager": ("trưởng phòng", "phó phòng", "trưởng bộ phận", "trưởng ban", "trưởng chi nhánh",
                "giám đốc", "phó giám đốc", "director", "manager", "head of", "head",
                "kế toán trưởng", "bếp trưởng", "cửa hàng trưởng", "quản đốc", "chief",
                "ceo", "cfo", "cto", "coo", "cmo", "vp", "vice president",
                "trưởng khoa", "hiệu trưởng", "quản lý cửa hàng", "quản lý nhà hàng",
                "quản lý chi nhánh", "quản lý khu vực", "quản lý vùng", "area manager",
                "general manager"),
}
_LEVEL_TITLE_RE = {level: _cue_regex(cues) for level, cues in _LEVEL_TITLE_CUES.items()}
# A title starting with "Quản lý ..." is a manager; "Nhân viên quản lý kho" is not.
_TITLE_STARTS_MANAGER = re.compile(r"^\W*quan ly(?! (?:kho|chat luong|don hang|hoc vien|du lieu"
                                   r"|ho so|tai lieu|thong tin|ban hang online))")
_FRESHER_TEXT = _cue_regex((
    "mới tốt nghiệp", "mới ra trường", "sinh viên mới tốt nghiệp", "fresh graduate",
    "chấp nhận sinh viên", "nhận sinh viên", "sinh viên năm cuối", "chưa có kinh nghiệm",
    "không yêu cầu kinh nghiệm", "no experience required", "fresher",
))


def extract_level(title: str, text: str = "", experience_min: Optional[int] = None) -> str:
    """Seniority from the title, else explicit text cues, else years of experience."""
    folded_title = _fold(title)
    found = [level for level, regex in _LEVEL_TITLE_RE.items() if regex.search(folded_title)]
    if not found and _TITLE_STARTS_MANAGER.search(folded_title):
        found = ["manager"]
    if found:
        individual = [lv for lv in found if lv not in {"lead", "manager"}]
        # "Junior/Middle Developer" → junior; "Senior Developer / Team Lead" → senior;
        # "Trưởng phòng" alone → manager.
        pool = individual or found
        return min(pool, key=_LEVEL_RANK.__getitem__)
    if experience_min is not None:
        if experience_min >= 5:
            return "senior"
        if experience_min >= 3:
            return "mid"
        if experience_min >= 1:
            return "junior"
    if text and _FRESHER_TEXT.search(_fold(text)):
        return "fresher"
    return "unknown"


# ── Experience ───────────────────────────────────────────────────────────────

_NUM = r"(\d{1,2}(?:[.,]5)?)"
_UNIT = r"(nam|years?|yrs?|thang|months?)"
_EXP_SPAN = re.compile(
    rf"(?<![\d/.,])0?{_NUM}\s*(?:\+\s*)?(?:(?:-|–|~|den|toi|to)\s*0?{_NUM}\s*)?(?:\+\s*)?"
    rf"{_UNIT}(?![a-z])"
)
_EXP_WORD = re.compile(r"kinh nghiem|experience|exp(?![a-z])|kn(?![a-z])|tham nien")
_EXP_NONE = re.compile(
    r"(?:khong (?:yeu cau|can|doi hoi|bat buoc) (?:co )?kinh nghiem"
    r"|chua (?:co|can) kinh nghiem|kinh nghiem\W{0,3}(?:khong yeu cau|khong can|khong bat buoc)"
    r"|no (?:prior )?experience (?:required|needed|necessary)|khong yc kinh nghiem"
    r"|khong yeu cau kn|0 nam kinh nghiem)"
)
_EXP_UPPER = re.compile(r"(?:duoi|toi da|khong qua|up to|less than|under|<)\W{0,3}$")
# Numbers of years that are not experience: age, warranty, contract, probation.
_EXP_NOT = re.compile(r"(?:do tuoi|tuoi|age|bao hanh|hop dong|thu viec|probation)\W*$")


def _years(value: str, unit: str, *, upper: bool = False) -> int:
    number = float(value.replace(",", "."))
    if unit.startswith(("thang", "month")):
        number = number / 12
    if upper:
        return int(-(-number // 1))  # ceil: "6-18 tháng" → max 2 years
    return int(number)


def _experience_in_line(line: str) -> Optional[tuple[Optional[int], Optional[int]]]:
    for match in _EXP_SPAN.finditer(line):
        low, high, unit = match.groups()
        if not unit.startswith(("thang", "month")) and float(low.replace(",", ".")) > 40:
            continue
        start, end = match.span()
        before, after = line[max(0, start - 60):start], line[end:end + 40]
        if not (_EXP_WORD.search(before) or _EXP_WORD.search(after)) or _EXP_NOT.search(before):
            continue
        if high is not None:
            return _years(low, unit), _years(high, unit, upper=True)
        if _EXP_UPPER.search(before):
            return 0, _years(low, unit, upper=True)
        return _years(low, unit), None  # "từ 2 năm", "2+ years", "2 năm trở lên"
    return None


def extract_experience(*texts: str) -> tuple[Optional[int], Optional[int]]:
    """``(min_years, max_years)``; ``(0, None)`` for "không yêu cầu kinh nghiệm".

    Texts are tried in order (requirements before description); the first line
    that pairs a number of years/months with an experience word wins.
    """
    for text in texts:
        if not text:
            continue
        lines = [_fold(line) for line in re.split(r"[\n\r•;]+", text)]
        for line in lines:
            found = _experience_in_line(line)
            if found is not None:
                return found
        if any(_EXP_NONE.search(line) for line in lines):
            return 0, None
    return None, None


# ── Benefits ─────────────────────────────────────────────────────────────────

_BENEFIT_CUES: dict[str, tuple[str, ...]] = {
    "insurance": ("bảo hiểm xã hội", "bhxh", "bhyt", "bảo hiểm y tế", "bảo hiểm thất nghiệp",
                  "bảo hiểm đầy đủ", "bảo hiểm theo quy định", "bảo hiểm theo luật",
                  "đóng bảo hiểm", "full insurance", "social insurance", "bảo hiểm tai nạn",
                  "bh xh", "bảo hiểm bắt buộc", "bhtn"),
    "health_insurance": ("bảo hiểm sức khỏe", "bảo hiểm sức khoẻ", "health insurance",
                         "healthcare", "premium healthcare", "bảo hiểm sức khỏe cao cấp",
                         "bảo hiểm pvi", "bảo việt care", "bảo hiểm sức khỏe toàn diện"),
    "13th_month": ("lương tháng 13", "thưởng tháng 13", "tháng lương thứ 13",
                   "lương tháng thứ 13", "13th month", "13th-month", "thưởng lương tháng 13",
                   "tháng 13"),
    "bonus": ("thưởng hiệu quả", "thưởng kpi", "thưởng doanh số", "thưởng quý",
              "thưởng cuối năm", "thưởng dự án", "thưởng lễ", "thưởng tết", "thưởng năm",
              "performance bonus", "bonus", "thưởng nóng", "thưởng theo", "thưởng thâm niên",
              "thưởng sinh nhật", "lương thưởng hấp dẫn"),
    "commission": ("hoa hồng", "commission"),
    "salary_review": ("xét tăng lương", "tăng lương", "salary review", "review lương",
                      "đánh giá lương"),
    "travel": ("du lịch", "company trip", "nghỉ mát", "annual trip"),
    "teambuilding": ("team building", "teambuilding", "team-building"),
    "laptop": ("laptop", "macbook", "cấp máy tính", "trang bị máy tính", "cấp thiết bị",
               "trang bị đầy đủ thiết bị", "được cấp máy"),
    "remote": ("làm việc từ xa", "remote", "work from home", "wfh", "làm tại nhà",
               "làm việc tại nhà", "online tại nhà"),
    "hybrid": ("hybrid",),
    "flexible_hours": ("giờ làm linh hoạt", "thời gian linh hoạt", "giờ giấc linh hoạt",
                       "flexible working", "flexible hours", "flexible time",
                       "làm việc linh hoạt", "thời gian làm việc linh hoạt"),
    "weekend_off": ("nghỉ thứ 7", "nghỉ t7", "nghỉ thứ bảy", "từ thứ 2 đến thứ 6",
                    "thứ 2 - thứ 6", "thứ 2 đến thứ 6", "monday to friday", "mon-fri",
                    "mon - fri", "t2-t6", "t2 - t6", "5 ngày/tuần", "5 days/week"),
    "training": ("được đào tạo", "đào tạo bài bản", "đào tạo chuyên sâu", "đào tạo nội bộ",
                 "đào tạo miễn phí", "chương trình đào tạo", "training", "khóa học",
                 "khoá học", "hỗ trợ học phí", "đào tạo nghiệp vụ", "cầm tay chỉ việc",
                 "đào tạo từ đầu"),
    "promotion": ("thăng tiến", "career path", "lộ trình phát triển", "career development",
                  "cơ hội phát triển"),
    "health_check": ("khám sức khỏe", "khám sức khoẻ", "health check", "medical check"),
    "meal": ("ăn trưa", "cơm trưa", "bữa trưa", "phụ cấp ăn", "tiền ăn", "bao ăn", "lunch",
             "meal", "suất ăn", "ăn ca", "hỗ trợ ăn", "bao cơm"),
    "transport": ("xăng xe", "đi lại", "đưa đón", "xe đưa đón", "gửi xe", "parking",
                  "transportation", "công tác phí"),
    "phone": ("phụ cấp điện thoại", "cước điện thoại", "hỗ trợ điện thoại", "phone allowance"),
    "housing": ("nhà ở", "ký túc xá", "chỗ ở", "hỗ trợ nhà", "housing", "accommodation",
                "bao ở", "phụ cấp nhà"),
    "allowance": ("phụ cấp", "trợ cấp", "allowance"),
    "annual_leave": ("nghỉ phép", "phép năm", "annual leave", "paid leave", "ngày phép",
                     "nghỉ lễ"),
    "stock": ("esop", "cổ phiếu", "cổ phần ưu đãi", "stock option", "share option"),
    "overtime_pay": ("lương tăng ca", "phụ cấp tăng ca", "overtime", "tăng ca", "làm thêm giờ"),
    "uniform": ("đồng phục", "uniform"),
}
_BENEFIT_RE = {code: _cue_regex(cues) for code, cues in _BENEFIT_CUES.items()}


def extract_benefits(*texts: str) -> list[str]:
    """Benefit codes (see ``BENEFIT_LABELS``) in a fixed order."""
    folded = _fold("\n".join(t for t in texts if t))
    if not folded:
        return []
    found = [code for code, regex in _BENEFIT_RE.items() if regex.search(folded)]
    return found[:MAX_BENEFITS]


# ── Industry ─────────────────────────────────────────────────────────────────

# (cue, weight).  Weight 3 = names the field unambiguously; 2 = typical role in the
# field; 1 = weak hint.  Title hits count 3×, description hits once (distinct cues).
_INDUSTRY_CUES: dict[str, tuple[tuple[str, int], ...]] = {
    "ke_toan_tai_chinh": (
        ("kế toán", 3), ("accountant", 3), ("accounting", 3), ("kiểm toán", 3), ("audit", 3),
        ("auditor", 3), ("tài chính", 2), ("finance", 2), ("financial", 2), ("thuế", 2),
        ("tax", 2), ("công nợ", 2), ("thủ quỹ", 3), ("cfo", 3), ("controller", 2),
        ("treasury", 2), ("báo cáo tài chính", 1), ("misa", 1), ("định mức", 1),
        ("giá thành", 2), ("payroll", 1),
    ),
    "ngan_hang_bao_hiem": (
        ("ngân hàng", 3), ("bank", 3), ("banking", 3), ("tín dụng", 3), ("credit", 2),
        ("giao dịch viên", 3), ("teller", 3), ("bảo hiểm nhân thọ", 4), ("bảo hiểm", 1),
        ("insurance", 2), ("thẩm định", 2), ("chứng khoán", 3), ("securities", 3),
        ("môi giới chứng khoán", 4), ("thu hồi nợ", 3), ("collection", 1),
        ("khách hàng cá nhân", 2), ("khách hàng doanh nghiệp", 2), ("quan hệ khách hàng", 2),
        ("relationship manager", 3), ("tư vấn tài chính", 3), ("đầu tư", 1), ("quỹ", 1),
        ("vay", 1), ("fintech", 2), ("hoạch định tài chính", 3),
    ),
    "kinh_doanh_ban_hang": (
        ("kinh doanh", 2), ("sales", 2), ("sale", 2), ("bán hàng", 3), ("telesales", 3),
        ("telesale", 3), ("tư vấn bán hàng", 3), ("chăm sóc khách hàng", 2), ("cskh", 2),
        ("customer service", 2), ("customer success", 2), ("tư vấn viên", 2), ("tư vấn", 1),
        ("business development", 2), ("phát triển thị trường", 3), ("phát triển kinh doanh", 2),
        ("account manager", 2), ("key account", 2), ("đại lý", 1), ("cửa hàng", 1),
        ("siêu thị", 1), ("thu ngân", 2), ("cashier", 2), ("ctv", 1), ("cộng tác viên", 1),
        ("bán lẻ", 2), ("retail", 2), ("fmcg", 1), ("kênh gt", 2), ("kênh mt", 2),
        ("thị trường", 1), ("doanh số", 1), ("call center", 2), ("tổng đài", 2),
        ("thương mại điện tử", 1), ("shopee", 1),
        ("ecommerce", 2), ("e-commerce", 2), ("presales", 1), ("pre-sales", 1),
        ("retail electronics", 2), ("telemarketing", 3), ("sales engineer", 3),
        ("sale engineer", 3), ("kỹ sư bán hàng", 3), ("kỹ sư kinh doanh", 3),
    ),
    "marketing": (
        ("marketing", 3), ("truyền thông", 3), ("quảng cáo", 2), ("content", 2),
        ("seo", 2), ("digital", 2), ("social media", 3), ("pr", 2), ("brand", 2),
        ("thương hiệu", 3), ("livestream", 3), ("live stream", 3), ("kol", 2), ("koc", 2),
        ("tiktok", 2), ("copywriter", 3), ("sáng tạo nội dung", 3), ("nội dung", 1),
        ("tiếp thị", 3), ("event", 2), ("sự kiện", 2), ("fanpage", 2), ("ads", 2),
        ("performance marketing", 3), ("growth", 1), ("trade marketing", 3), ("media", 1),
        ("youtube", 1), ("communication", 2), ("communications", 2), ("crm", 1),
    ),
    "it_phan_mem": (
        ("developer", 3), ("lập trình", 3), ("lập trình viên", 3), ("software", 3),
        ("phần mềm", 2), ("frontend", 3), ("front-end", 3), ("front end", 3), ("backend", 3),
        ("back-end", 3), ("back end", 3), ("fullstack", 3), ("full stack", 3),
        ("full-stack", 3), ("mobile app", 2), ("ios", 2), ("android", 2), ("tester", 3),
        ("qa engineer", 3), ("qc engineer", 2), ("automation test", 3), ("kiểm thử", 3),
        ("data engineer", 3), ("data scientist", 3), ("data analyst", 2), ("machine learning", 3),
        ("ai engineer", 3), ("business analyst", 2), ("product owner", 3), ("product manager", 2),
        ("scrum master", 3), ("game developer", 3), ("unity", 2), ("php", 3), ("java", 3),
        ("javascript", 3), (".net", 3), ("net", 1), ("python", 3), ("reactjs", 3), ("react", 2),
        ("nodejs", 3), ("node.js", 3), ("golang", 3), ("flutter", 3), ("kỹ sư phần mềm", 3),
        ("software engineer", 3), ("web developer", 3), ("lập trình web", 3), ("erp", 1),
        ("odoo", 2), ("salesforce", 1), ("it comtor", 2), ("brse", 3), ("bridge se", 3),
        ("coder", 3), ("programmer", 3), ("sql", 1), ("api", 1), ("ui developer", 3),
        ("blockchain", 2), ("embedded", 2), ("firmware", 2), ("game", 1), ("sap", 1),
        ("công nghệ thông tin", 2), ("it", 1), ("bi", 1), ("data", 1),
    ),
    "it_ha_tang": (
        ("system admin", 3), ("system administrator", 3), ("sysadmin", 3),
        ("quản trị hệ thống", 3), ("quản trị mạng", 3), ("network", 2), ("mạng", 1),
        ("it helpdesk", 3), ("helpdesk", 3), ("it support", 3), ("hỗ trợ kỹ thuật", 1),
        ("devops", 3), ("cloud", 2), ("aws", 2), ("azure", 2), ("security", 2),
        ("an ninh mạng", 3), ("an toàn thông tin", 3), ("bảo mật", 2), ("phần cứng", 2),
        ("datacenter", 3), ("data center", 3), ("server", 2), ("sre", 3), ("linux", 2),
        ("noc", 2), ("soc", 2), ("cisco", 3), ("vmware", 3), ("ccna", 3), ("kỹ thuật viên it", 3),
        ("nhân viên it", 3), ("it staff", 3), ("hạ tầng", 2), ("infrastructure", 3),
        ("cyber", 2), ("dba", 3), ("database administrator", 3), ("vận hành hệ thống", 2),
    ),
    "nhan_su": (
        ("nhân sự", 3), ("hr", 3), ("human resource", 3), ("human resources", 3),
        ("tuyển dụng", 3), ("recruiter", 3), ("recruitment", 3), ("talent acquisition", 3),
        ("c&b", 3), ("c b", 2), ("hrbp", 3), ("headhunt", 3), ("headhunter", 3),
        ("tiền lương", 2), ("lương thưởng", 1), ("chấm công", 2), ("hành chính nhân sự", 3),
        ("đào tạo nội bộ", 2), ("l&d", 3), ("chính sách nhân sự", 2),
    ),
    "hanh_chinh": (
        ("hành chính", 3), ("admin", 2), ("administrative", 3), ("văn phòng", 2),
        ("lễ tân", 3), ("receptionist", 3), ("tiếp tân", 3), ("thư ký", 3), ("secretary", 3),
        ("trợ lý", 2), ("assistant", 2), ("nhập liệu", 3), ("data entry", 3), ("văn thư", 3),
        ("back office", 2), ("điều phối", 1), ("phiên dịch", 2), ("biên dịch", 2),
        ("sale admin", 4), ("sales admin", 4), ("saleadmin", 4),
        ("thông dịch", 2), ("interpreter", 2), ("translator", 2), ("dịch thuật", 2),
        ("hồ sơ", 1), ("soạn thảo văn bản", 2),
    ),
    "luat": (
        ("luật", 3), ("pháp chế", 3), ("pháp lý", 3), ("legal", 3), ("lawyer", 3),
        ("luật sư", 4), ("compliance", 2), ("tuân thủ", 2), ("công chứng", 3),
        ("sở hữu trí tuệ", 3), ("hợp đồng", 1), ("paralegal", 3), ("thừa phát lại", 3),
    ),
    "logistics": (
        ("logistics", 3), ("xuất nhập khẩu", 3), ("xnk", 3), ("import export", 3),
        ("thủ kho", 3), ("kho", 2), ("warehouse", 3), ("vận chuyển", 2), ("vận tải", 3),
        ("giao hàng", 2), ("giao nhận", 3), ("shipper", 3), ("tài xế", 2), ("lái xe", 2),
        ("driver", 2), ("chuỗi cung ứng", 3), ("supply chain", 3), ("scm", 3), ("mua hàng", 3),
        ("thu mua", 3), ("purchasing", 3), ("procurement", 3), ("buyer", 2), ("chứng từ", 2),
        ("freight", 3), ("forwarder", 3), ("forwarding", 3), ("hải quan", 3), ("customs", 3),
        ("điều vận", 3), ("bưu cục", 3), ("bưu chính", 2), ("xe nâng", 2), ("merchandiser", 2),
        ("đơn hàng", 1), ("kiểm kê", 1), ("sea", 1), ("air", 1), ("cảng", 2),
        ("order", 1), ("planning", 1), ("demand planning", 3), ("inventory", 2),
    ),
    "san_xuat": (
        ("sản xuất", 3), ("production", 3), ("nhà máy", 2), ("factory", 2), ("xưởng", 2),
        ("cơ khí", 3), ("mechanical", 3), ("vận hành máy", 3), ("cnc", 3), ("hàn", 1),
        ("thợ hàn", 3), ("bảo trì", 2), ("maintenance", 2), ("bảo dưỡng", 2), ("qc", 2),
        ("qa", 1), ("kcs", 3), ("quality", 2), ("chất lượng", 2), ("kiểm soát chất lượng", 3),
        ("ép nhựa", 3), ("khuôn", 3), ("may mặc", 3), ("dệt may", 3), ("thợ may", 3),
        ("cắt rập", 3), ("hóa chất", 2), ("kỹ sư công nghệ", 1), ("process engineer", 3),
        ("industrial engineer", 3), ("lean", 2), ("kaizen", 2), ("quản đốc", 3), ("ie", 1),
        ("tiện", 1), ("phay", 2), ("dập", 2), ("lắp ráp", 2), ("ô tô", 2), ("sửa chữa", 1),
        ("thực phẩm", 1), ("hse", 2), ("an toàn lao động", 2), ("ehs", 2), ("môi trường", 1),
        ("xử lý nước", 2), ("nông nghiệp", 2), ("chăn nuôi", 2), ("thú y", 2), ("đóng gói", 2),
        ("kỹ thuật viên", 1), ("autocad", 1), ("solidworks", 2), ("gỗ", 1), ("in ấn", 2),
    ),
    "ky_thuat_dien": (
        ("điện", 2), ("điện tử", 3), ("điện lạnh", 3), ("điện công nghiệp", 3),
        ("electrical", 3), ("electronics", 3), ("electronic", 3), ("viễn thông", 3),
        ("telecom", 3), ("kỹ sư điện", 3), ("thợ điện", 3), ("kỹ thuật điện", 3),
        ("plc", 3), ("tự động hóa", 3), ("automation", 2), ("điều hòa", 2), ("hvac", 3),
        ("cơ điện", 2), ("cơ điện tử", 3), ("pcb", 3), ("mạch", 2), ("năng lượng mặt trời", 3),
        ("solar", 3), ("camera", 2), ("thang máy", 3), ("robot", 2), ("bán dẫn", 3),
        ("semiconductor", 3), ("vi mạch", 3), ("điện nước", 1), ("m&e", 1),
        ("bảo trì điện", 4), ("kỹ thuật viên điện", 4), ("bảo hành", 1),
    ),
    "xay_dung": (
        ("xây dựng", 3), ("construction", 3), ("kiến trúc", 3), ("architect", 3),
        ("kiến trúc sư", 4), ("kết cấu", 3), ("công trình", 2), ("thi công", 3),
        ("giám sát thi công", 3), ("dự toán", 3), ("qs", 3), ("quantity surveyor", 3),
        ("mep", 3), ("m&e", 2), ("site engineer", 3), ("civil", 3), ("cầu đường", 3),
        ("hạ tầng kỹ thuật", 2), ("nội thất", 2), ("interior", 2), ("revit", 2), ("bim", 3),
        ("đấu thầu", 2), ("hồ sơ thầu", 3), ("trắc địa", 3), ("đo đạc", 2), ("hiện trường", 2),
        ("shop drawing", 3), ("cơ điện", 1), ("điện nước", 1), ("cảnh quan", 2),
        ("diễn họa", 2), ("phòng cháy", 1), ("pccc", 2), ("thầu", 1),
    ),
    "bat_dong_san": (
        ("bất động sản", 4), ("bđs", 4), ("real estate", 4), ("property", 2), ("dự án", 1),
        ("căn hộ", 2), ("nhà đất", 3), ("chung cư", 2), ("môi giới", 2), ("đất nền", 3),
        ("leasing", 2), ("cho thuê", 1), ("tòa nhà", 1), ("building management", 3),
        ("quản lý tòa nhà", 3), ("vận hành tòa nhà", 3),
    ),
    "y_te": (
        ("y tế", 3), ("bác sĩ", 4), ("doctor", 3), ("điều dưỡng", 4), ("nurse", 3),
        ("y tá", 4), ("dược", 3), ("dược sĩ", 4), ("pharmacist", 4), ("pharma", 3),
        ("phòng khám", 3), ("bệnh viện", 3), ("hospital", 3), ("clinic", 3), ("nha khoa", 4),
        ("nha sĩ", 4), ("dental", 3), ("xét nghiệm", 3), ("trình dược viên", 4),
        ("vật lý trị liệu", 4), ("chăm sóc sức khỏe", 2), ("thiết bị y tế", 3),
        ("hộ lý", 4), ("kỹ thuật viên y", 4), ("medical", 2), ("sinh học", 2),
        ("lâm sàng", 3), ("clinical", 3), ("thú y", 2), ("otc", 2), ("etc", 1),
        ("dinh dưỡng", 2), ("spa", 1), ("thẩm mỹ", 2), ("massage", 1),
    ),
    "giao_duc": (
        ("giáo viên", 4), ("teacher", 4), ("giảng viên", 4), ("lecturer", 3), ("gia sư", 4),
        ("tutor", 3), ("trợ giảng", 4), ("teaching assistant", 4), ("giáo dục", 3),
        ("education", 3), ("mầm non", 3), ("tuyển sinh", 3), ("học thuật", 3), ("academic", 3),
        ("du học", 3), ("trung tâm anh ngữ", 3), ("e-learning", 2), ("elearning", 2),
        ("đào tạo", 1), ("trainer", 2), ("bảo mẫu", 3), ("giáo vụ", 4), ("học vụ", 4),
        ("nghiên cứu", 1), ("researcher", 2), ("dạy", 2), ("ielts", 1), ("toeic", 1),
        ("trường", 1), ("học viên", 1), ("ta", 1),
    ),
    "fnb_khach_san": (
        ("nhà hàng", 3), ("khách sạn", 3), ("hotel", 3), ("restaurant", 3), ("f&b", 3),
        ("f b", 2), ("phục vụ", 3), ("waiter", 3), ("waitress", 3), ("bartender", 4),
        ("pha chế", 4), ("barista", 4), ("bếp", 3), ("đầu bếp", 4), ("chef", 4), ("phụ bếp", 4),
        ("buồng phòng", 4), ("housekeeping", 3), ("du lịch", 3), ("tour", 3), ("travel", 2),
        ("hướng dẫn viên", 4), ("cafe", 2), ("cà phê", 2), ("coffee", 2), ("quán", 1),
        ("resort", 3), ("lễ tân khách sạn", 4), ("front office", 2), ("bell", 1),
        ("concierge", 3), ("banquet", 3), ("tiệc", 2), ("trà sữa", 3), ("bánh", 1),
        ("vé máy bay", 3), ("booking", 1), ("visa", 2), ("điều hành tour", 4),
    ),
    "thiet_ke": (
        ("thiết kế", 3), ("designer", 3), ("design", 2), ("đồ họa", 3), ("graphic", 3),
        ("ui/ux", 3), ("ui ux", 3), ("ux", 2), ("video editor", 4), ("editor", 2),
        ("dựng phim", 4), ("dựng video", 4), ("quay phim", 4), ("quay dựng", 4),
        ("nhiếp ảnh", 4), ("photographer", 4), ("chụp ảnh", 3), ("3d", 2), ("2d", 2),
        ("motion", 2), ("animation", 3), ("animator", 3), ("artist", 3), ("họa sĩ", 3),
        ("biên tập", 3), ("phóng viên", 4), ("nhà báo", 4), ("mc", 3), ("diễn viên", 3),
        ("người mẫu", 3), ("model", 1), ("media", 2), ("vfx", 4), ("concept art", 4),
        ("photoshop", 1), ("illustrator", 1), ("in ấn", 1), ("thời trang", 1), ("stylist", 3),
        ("makeup", 2), ("trang điểm", 2), ("biên kịch", 4), ("đạo diễn", 4), ("vj", 3),
    ),
    "lao_dong_pho_thong": (
        ("lao động phổ thông", 5), ("công nhân", 4), ("tạp vụ", 5), ("bảo vệ", 5),
        ("giúp việc", 5), ("bốc xếp", 5), ("bốc vác", 5), ("vệ sinh", 2), ("phụ kho", 3),
        ("phụ việc", 4), ("lđpt", 5), ("worker", 2), ("security guard", 5), ("cleaner", 4),
        ("tạp hóa", 1), ("phát tờ rơi", 4), ("đóng gói", 2), ("trông xe", 4), ("giữ xe", 4),
        ("chăm sóc người già", 3), ("chăm sóc người cao tuổi", 3), ("rửa bát", 4),
        ("thời vụ", 2), ("part time", 1),
    ),
}
# cue n-gram → [(industry, weight)]; cues are matched as whole token sequences.
_INDUSTRY_INDEX: dict[str, list[tuple[str, int]]] = {}
for _code, _cues in _INDUSTRY_CUES.items():
    for _cue, _weight in _cues:
        _INDUSTRY_INDEX.setdefault(skill_key(_cue), []).append((_code, _weight))
_INDUSTRY_MAX_N = max(k.count(" ") + 1 for k in _INDUSTRY_INDEX)
# Lower index wins ties: specific domains before generic functions.
_INDUSTRY_PRIORITY = tuple(_INDUSTRY_CUES)
_TITLE_WEIGHT = 4
_TEXT_CHARS = 1500
_TEXT_CAP = 6

# Compounds whose parts are misleading cues ("thương mại điện tử" is not electronics,
# "điện thoại" is a phone).  Replaced before matching.
_INDUSTRY_REWRITES = (
    (re.compile(r"(?:san )?thuong mai dien tu|san thuong mai|tmdt"), "ecommerce"),
    (re.compile(r"(?:qua |goi )?dien thoai"), "phone"),
    (re.compile(r"dien may"), "retail electronics"),
)


def _industry_hits(text: str) -> dict[str, int]:
    """``{industry: sum of weights of the distinct cues present}``."""
    folded = _fold(text)
    for pattern, replacement in _INDUSTRY_REWRITES:
        folded = pattern.sub(replacement, folded)
    words = _TOKEN.findall(folded)
    grams = {
        " ".join(words[i:i + n])
        for n in range(1, _INDUSTRY_MAX_N + 1)
        for i in range(len(words) - n + 1)
    }
    hits: dict[str, int] = {}
    for gram in grams & _INDUSTRY_INDEX.keys():
        for code, weight in _INDUSTRY_INDEX[gram]:
            hits[code] = hits.get(code, 0) + weight
    return hits


def industry_scores(title: str, text: str = "") -> dict[str, int]:
    scores = {code: _TITLE_WEIGHT * w for code, w in _industry_hits(title).items()}
    if text:
        for code, weight in _industry_hits(text[:_TEXT_CHARS]).items():
            scores[code] = scores.get(code, 0) + min(_TEXT_CAP, weight)
    return scores


def classify_industry(title: str, text: str = "") -> str:
    """Best industry code for a posting, ``"khac"`` when no cue fires."""
    scores = industry_scores(title, text)
    if not scores:
        return "khac"
    return max(scores, key=lambda c: (scores[c], -_INDUSTRY_PRIORITY.index(c)))


# ── District ─────────────────────────────────────────────────────────────────

_DISTRICTS: dict[str, tuple[str, ...]] = {
    "Ho Chi Minh": (
        "Bình Thạnh", "Gò Vấp", "Tân Bình", "Tân Phú", "Phú Nhuận", "Bình Tân", "Thủ Đức",
        "Hóc Môn", "Củ Chi", "Nhà Bè", "Bình Chánh", "Cần Giờ",
    ),
    "Ha Noi": (
        "Ba Đình", "Hoàn Kiếm", "Hai Bà Trưng", "Đống Đa", "Tây Hồ", "Cầu Giấy", "Thanh Xuân",
        "Hoàng Mai", "Long Biên", "Bắc Từ Liêm", "Nam Từ Liêm", "Từ Liêm", "Hà Đông",
        "Sơn Tây", "Ba Vì", "Chương Mỹ", "Đan Phượng", "Đông Anh", "Gia Lâm", "Hoài Đức",
        "Mê Linh", "Mỹ Đức", "Phú Xuyên", "Phúc Thọ", "Quốc Oai", "Sóc Sơn", "Thạch Thất",
        "Thanh Oai", "Thanh Trì", "Thường Tín", "Ứng Hòa",
    ),
    "Da Nang": (
        "Hải Châu", "Thanh Khê", "Sơn Trà", "Ngũ Hành Sơn", "Liên Chiểu", "Cẩm Lệ", "Hòa Vang",
    ),
}
_DISTRICT_LOOKUP: dict[str, tuple[str, str]] = {
    _fold(name): (name, city) for city, names in _DISTRICTS.items() for name in names
}
_DISTRICT_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(map(re.escape, sorted(_DISTRICT_LOOKUP, key=len, reverse=True)))
    + r")(?![a-z0-9])"
)
_NUMBERED_DISTRICT_RE = re.compile(r"(?<![a-z0-9])(?:quan|q|district|dist)\.?\s*0?(\d{1,2})(?!\d)")
_ADDRESS_LINE = re.compile(
    r"(?:dia chi|dia diem lam viec|noi lam viec|address|work location)\W{0,3}([^\n]{0,160})"
)


def extract_district(location: str, text: str = "", city: str = "") -> str:
    """Display name of the quận/huyện ("Quận 1", "Cầu Giấy"), else ``""``."""
    candidates = [_fold(location)]
    if text:
        candidates += [m.group(1) for m in _ADDRESS_LINE.finditer(_fold(text[:6000]))]
    for chunk in candidates:
        if not chunk:
            continue
        match = _DISTRICT_RE.search(chunk)
        if match:
            name, district_city = _DISTRICT_LOOKUP[match.group(1)]
            if not city or city == district_city:
                return name
        match = _NUMBERED_DISTRICT_RE.search(chunk)
        if match and 1 <= int(match.group(1)) <= 12 and city in {"", "Ho Chi Minh"}:
            return f"Quận {int(match.group(1))}"
    return ""


# ── Salary period ────────────────────────────────────────────────────────────

_PERIOD_HOUR = re.compile(r"/\s*(?:gio|h|hour|hr)(?![a-z])|per\s+hour|theo\s+gio|hourly")
_PERIOD_DAY = re.compile(r"/\s*(?:ngay|day|ca|buoi)(?![a-z])|per\s+day|theo\s+ngay|daily")
_PERIOD_YEAR = re.compile(r"/\s*(?:nam|year|yr)(?![a-z])|per\s+(?:year|annum)|annual|yearly")
_PERIOD_MONTH = re.compile(r"/\s*(?:thang|month|mo)(?![a-z])|per\s+month|monthly|hang thang")


def salary_period(salary_raw: str) -> str:
    """"month" / "year" / "day" / "hour" for a salary that has a figure, else ``""``."""
    folded = _fold(salary_raw)
    if not folded or not re.search(r"\d", folded):
        return ""
    for period, regex in (("hour", _PERIOD_HOUR), ("day", _PERIOD_DAY), ("year", _PERIOD_YEAR),
                          ("month", _PERIOD_MONTH)):
        if regex.search(folded):
            return period
    return "month"


# ── Quality ──────────────────────────────────────────────────────────────────


def quality_score(job: "Job") -> int:
    """0-100: how complete the stored posting is (not how good the job is)."""
    description = len(job.description or "")
    points = (
        (10, bool(job.title)),
        (10, bool(job.company)),
        (10, bool(job.location)),
        (15, job.salary_min is not None or job.salary_max is not None),
        (15, description >= 200),
        (7, 50 <= description < 200),
        (5, bool(job.requirements)),
        (10, job.posted_at is not None),
        (5, job.expires_at is not None),
        (5, bool(job.skills)),
        (5, job.level not in {"", "unknown"}),
        (5, job.experience_min_years is not None),
        (5, job.industry not in {"", "khac"}),
    )
    return min(100, sum(weight for weight, ok in points if ok))


# ── Orchestration ────────────────────────────────────────────────────────────


def enrich_job(job: "Job", raw: "RawJob", legacy_tags: Iterable[str] = ()) -> "Job":
    """Fill the v2 fields of ``job`` in place (and return it)."""
    benefits_raw = getattr(raw, "benefits_raw", "") or ""
    body = "\n".join(t for t in (raw.description, raw.requirements) if t)

    skills = extract_skills(raw.title, body)
    for tag in legacy_tags:
        if tag not in skills:
            skills.append(tag)
    job.skills = sorted(skills[:MAX_SKILLS])
    job.tags = list(job.skills)

    low, high = extract_experience(raw.requirements, raw.description, raw.title)
    job.experience_min_years, job.experience_max_years = low, high
    job.level = JobLevel(extract_level(raw.title, body, low))
    job.benefits = extract_benefits(benefits_raw, raw.description)
    job.industry = classify_industry(raw.title, body)
    job.salary_period = salary_period(raw.salary_raw)
    job.district = extract_district(raw.location, body, job.location_normalized)
    job.quality_score = quality_score(job)
    return job
