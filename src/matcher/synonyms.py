"""
Synonym mapping for multi-industry job matching.

Groups Vietnamese ↔ English equivalent terms so that searching
"kế toán" also matches "accountant", "nhân sự" matches "HR", etc.

Usage:
    from src.matcher.synonyms import expand_token
    tokens = expand_token("ke toan")  # returns ["ke toan", "accountant", "accounting", ...]
"""

from __future__ import annotations

from src.common.text_utils import clean_vn_text

# ── Synonym groups ───────────────────────────────────────────────
# Each group is a list of equivalent terms (mixed VN/EN).
# Token matching uses OR within a group, AND between search tokens.

SYNONYM_GROUPS: list[list[str]] = [
    # Kế toán / Tài chính
    ["kế toán", "accountant", "accounting", "kiểm toán", "auditor",
     "tài chính kế toán", "finance", "tài chính"],

    # Nhân sự
    ["nhân sự", "hr", "human resources", "tuyển dụng", "recruiter",
     "talent acquisition", "c&b", "hành chính nhân sự"],

    # Marketing
    ["marketing", "digital marketing", "truyền thông", "quảng cáo",
     "content", "seo", "brand", "content marketing", "performance marketing"],

    # Y tế
    ["y tế", "điều dưỡng", "nurse", "bác sĩ", "doctor", "dược sĩ",
     "pharmacist", "y khoa", "sức khỏe", "healthcare"],

    # Giáo dục
    ["giáo dục", "giáo viên", "teacher", "gia sư", "tutor",
     "đào tạo", "trainer", "giảng viên", "lecturer"],

    # Xây dựng
    ["xây dựng", "civil engineer", "kỹ sư xây dựng", "construction",
     "giám sát công trình", "kiến trúc", "architect"],

    # Logistics
    ["logistics", "xuất nhập khẩu", "import export", "chuỗi cung ứng",
     "supply chain", "kho vận", "warehouse", "vận tải", "shipping"],

    # Bán hàng / Kinh doanh
    ["bán hàng", "sales", "kinh doanh", "sale", "telesales",
     "business development", "nhân viên kinh doanh", "account manager"],

    # IT / Công nghệ
    ["it", "công nghệ thông tin", "information technology", "phần mềm",
     "software", "developer", "lập trình", "lập trình viên", "programmer"],

    # Thực tập
    ["thực tập", "intern", "internship", "tts", "thực tập sinh"],

    # Fresher / Entry level
    ["fresher", "mới tốt nghiệp", "entry level", "junior"],

    # Thiết kế
    ["thiết kế", "design", "designer", "graphic design", "ui/ux",
     "ui ux", "đồ họa"],

    # Hành chính / Văn phòng
    ["hành chính", "admin", "administration", "văn phòng", "office",
     "thư ký", "secretary", "lễ tân", "receptionist"],

    # Khách sạn / Nhà hàng / Du lịch
    ["khách sạn", "hotel", "nhà hàng", "restaurant", "du lịch",
     "tourism", "hospitality", "f&b", "food and beverage", "đầu bếp", "chef"],

    # Ngân hàng / Bảo hiểm
    ["ngân hàng", "bank", "banking", "bảo hiểm", "insurance",
     "tín dụng", "credit"],

    # Business Analyst / BA
    ["business analyst", "phân tích nghiệp vụ", "phân tích kinh doanh",
     "business analysis", "system analyst"],

    # Data / Dữ liệu
    # NOTE: "data" alone is too generic (appears in most job descriptions).
    # Only compound terms are included to ensure precise matching.
    ["data analyst", "data engineer", "data scientist", "phân tích dữ liệu",
     "big data", "data analysis", "dữ liệu"],

    # QA / QC / Tester
    ["qa", "qc", "tester", "quality assurance", "quality control",
     "kiểm thử", "test", "testing", "automation test"],

    # Chăm sóc khách hàng
    ["chăm sóc khách hàng", "customer service", "cskh", "tư vấn",
     "consultant", "tư vấn viên", "support", "hỗ trợ khách hàng"],

    # Cơ khí / Điện / Kỹ thuật
    ["cơ khí", "mechanical", "điện", "electrical", "kỹ thuật",
     "technician", "engineer", "kỹ sư", "bảo trì", "maintenance"],
]

# ── Build inverted index ────────────────────────────────────────

_SYN_INDEX: dict[str, list[str]] = {}

for _group in SYNONYM_GROUPS:
    _cleaned_group = [clean_vn_text(term) for term in _group]
    # Remove empty strings and duplicates while preserving order
    _cleaned_group = list(dict.fromkeys(t for t in _cleaned_group if t))
    for _term in _cleaned_group:
        _SYN_INDEX[_term] = _cleaned_group


# Industry membership is useful for categorization but is not equivalence.
# Keep exact occupations separate (a nurse is not a doctor, DE is not DA).
_MATCH_GROUPS = [
    ["kế toán", "accountant", "accounting"],
    ["kiểm toán", "auditor", "audit"],
    ["tài chính", "finance", "financial"],
    ["nhân sự", "hr", "human resources"],
    ["tuyển dụng", "recruiter", "recruitment", "talent acquisition"],
    ["marketing", "tiếp thị"],
    ["truyền thông", "communications"],
    ["quảng cáo", "advertising"],
    ["y tế", "healthcare", "y khoa"],
    ["điều dưỡng", "nurse", "nursing"],
    ["bác sĩ", "doctor", "physician"],
    ["dược sĩ", "pharmacist"],
    ["giáo viên", "teacher"], ["gia sư", "tutor"],
    ["giảng viên", "lecturer"], ["đào tạo", "trainer", "training"],
    ["xây dựng", "construction"], ["kỹ sư xây dựng", "civil engineer"],
    ["kiến trúc sư", "architect"],
    ["logistics", "kho vận"], ["xuất nhập khẩu", "import export"],
    ["chuỗi cung ứng", "supply chain"], ["kho", "warehouse"],
    ["kinh doanh", "bán hàng", "sales", "sale", "business development"],
    ["it", "công nghệ thông tin", "information technology"],
    ["lập trình", "lập trình viên", "developer", "programmer"],
    ["phần mềm", "software"],
    ["thực tập", "thực tập sinh", "intern", "internship", "tts"],
    ["fresher", "mới tốt nghiệp", "entry level"], ["junior", "junior level"],
    ["thiết kế", "designer", "design"], ["đồ họa", "graphic design"],
    ["hành chính", "admin", "administration"],
    ["lễ tân", "receptionist"], ["thư ký", "secretary"],
    ["khách sạn", "hotel", "hospitality"], ["nhà hàng", "restaurant", "f&b"],
    ["đầu bếp", "chef", "cook"], ["du lịch", "tourism"],
    ["ngân hàng", "bank", "banking"], ["bảo hiểm", "insurance"],
    ["business analyst", "ba", "phân tích nghiệp vụ", "phân tích kinh doanh"],
    ["data engineer", "kỹ sư dữ liệu", "ki su du lieu"],
    ["data analyst", "phân tích dữ liệu", "data analysis"],
    ["data scientist", "khoa học dữ liệu"],
    ["qa", "quality assurance"], ["qc", "quality control"],
    ["tester", "kiểm thử", "testing"],
    ["chăm sóc khách hàng", "customer service", "cskh", "customer support"],
    ["tư vấn", "consultant", "tư vấn viên"],
    ["cơ khí", "mechanical"], ["điện", "electrical"],
    ["kỹ sư", "ki su", "engineer"], ["bảo trì", "maintenance"],
    ["remote", "từ xa", "work from home"],
]
_SYN_INDEX.clear()
for _group in _MATCH_GROUPS:
    _terms = list(dict.fromkeys(clean_vn_text(t) for t in _group))
    for _term in _terms:
        _SYN_INDEX[_term] = _terms


def expand_token(token: str) -> list[str]:
    """Return all synonyms for a token (already cleaned).

    If the token is not in any synonym group, returns [token] itself.

    Args:
        token: A cleaned (via clean_vn_text) search token.

    Returns:
        List of equivalent tokens including the original.

    Examples:
        >>> expand_token("ke toan")
        ['ke toan', 'accountant', 'accounting', 'kiem toan', 'auditor', ...]
        >>> expand_token("python")
        ['python']
    """
    return _SYN_INDEX.get(token, [token])


def tokenize_query(query: str) -> list[str]:
    """Tokenize a search query, preserving known multi-word synonyms.

    Instead of blindly splitting by space (which breaks 'business analyst' into
    ['business', 'analyst']), this extracts known phrases from the synonym index first.
    """
    import re

    query = clean_vn_text(query)

    # Sort all known terms by length descending to match longest phrases first
    known_terms = sorted(_SYN_INDEX.keys(), key=lambda x: len(x), reverse=True)

    tokens = []
    # Extract multi-word terms first
    for term in known_terms:
        pattern = r"(?<!\w)" + re.escape(term) + r"(?!\w)"
        if " " in term and re.search(pattern, query):
            tokens.append(term)
            query = re.sub(pattern, " ", query)

    # Split remaining by space
    remaining = re.split(r"[,\s\-/|]+", query)
    tokens.extend([t.strip() for t in remaining if re.search(r"\w", t)])

    return list(dict.fromkeys(tokens))  # remove duplicates


def get_category_for_text(text: str) -> str | None:
    """Determine job category from text using synonym groups.

    Args:
        text: Cleaned text (search_blob or title).

    Returns:
        Category slug (e.g. 'ke_toan', 'marketing') or None.
    """
    # Category names derived from first term in each group
    _CATEGORY_NAMES = [
        "ke_toan", "nhan_su", "marketing", "y_te", "giao_duc",
        "xay_dung", "logistics", "ban_hang", "it", "thuc_tap",
        "fresher", "thiet_ke", "hanh_chinh", "khach_san", "ngan_hang",
        "business_analyst", "data", "qa_qc", "cham_soc_kh", "co_khi",
    ]

    best_category = None
    best_count = 0

    for i, group in enumerate(SYNONYM_GROUPS):
        cleaned_terms = [clean_vn_text(t) for t in group]
        count = sum(1 for term in cleaned_terms if term and term in text)
        if count > best_count:
            best_count = count
            best_category = _CATEGORY_NAMES[i] if i < len(_CATEGORY_NAMES) else None

    return best_category
