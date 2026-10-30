"""Build ``src/data/skills_vi.json`` from the VietJobs dataset (one-off).

Source: Hugging Face ``dinhieufam/VietJobs`` (MIT, ~48k Vietnamese postings with
``technical_skills`` / ``soft_skills`` / ``languages_required`` columns).  The raw
CSV (~100 MB) is cached under ``dist/vietjobs/`` and never committed.

The dictionary merges, by frequency:

* VietJobs technical skills (normalised: "Sử dụng thành thạo Excel" → "excel",
  "Tin học văn phòng (Word, Excel)" → "tin học văn phòng", "word", "excel");
* VietJobs soft skills that are written as a *skill* ("kỹ năng đàm phán"), not
  adjectives ("trung thực", "nhanh nhẹn");
* languages, ``src.etl.enrich.CORE_SKILLS`` and the legacy ``TECH_SKILLS`` list.

A held-out sample (``--eval-size`` rows, fixed seed) is excluded from the build
and used by ``--evaluate`` to measure skill coverage and the rule-based
extractors (industry, experience, benefits).

Usage::

    python scripts/build_skill_dictionary.py            # download + build
    python scripts/build_skill_dictionary.py --evaluate # build + report metrics
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common.text_utils import normalize_vn_text  # noqa: E402
from src.etl import enrich  # noqa: E402
from src.etl.transformer import TECH_SKILLS  # noqa: E402

DATASET_URL = "https://huggingface.co/datasets/dinhieufam/VietJobs/resolve/main/VietJobs.csv"
CACHE_PATH = ROOT / "dist" / "vietjobs" / "VietJobs.csv"
OUTPUT_PATH = ROOT / "src" / "data" / "skills_vi.json"
MAX_BYTES = 300 * 1024
EVAL_SEED = 20261030
MIN_LABEL_COUNT = 3  # a skill must be labelled in at least this many postings
MAX_CANDIDATES = 12000
PRECISION_SAMPLE = 8000  # postings used to measure label precision
MIN_TEXT_HITS = 3
MIN_PRECISION = 0.25

# Our industry codes → VietJobs ``category`` (coarser, 16 classes).
INDUSTRY_TO_VIETJOBS = {
    "ke_toan_tai_chinh": "tài_chính_kế_toán_ngân_hàng_bảo_hiểm",
    "ngan_hang_bao_hiem": "tài_chính_kế_toán_ngân_hàng_bảo_hiểm",
    "kinh_doanh_ban_hang": "kinh_doanh_bán_hàng_chăm_sóc_khách_hàng",
    "marketing": "marketing_truyền_thông_quảng_cáo_nội_dung",
    "it_phan_mem": "công_nghệ_thông_tin_kỹ_thuật_số",
    "it_ha_tang": "công_nghệ_thông_tin_kỹ_thuật_số",
    "nhan_su": "nhân_sự_hành_chính_pháp_chế_tư_vấn",
    "hanh_chinh": "nhân_sự_hành_chính_pháp_chế_tư_vấn",
    "luat": "nhân_sự_hành_chính_pháp_chế_tư_vấn",
    "logistics": "logistics_vận_tải_chuỗi_cung_ứng",
    "san_xuat": "sản_xuất_lao_động_phổ_thông_cơ_khí",
    "lao_dong_pho_thong": "sản_xuất_lao_động_phổ_thông_cơ_khí",
    "ky_thuat_dien": "kỹ_thuật_điện_điện_tử_viễn_thông",
    "xay_dung": "xây_dựng_kiến_trúc_bất_động_sản",
    "bat_dong_san": "xây_dựng_kiến_trúc_bất_động_sản",
    "y_te": "y_tế_dược_chăm_sóc_sức_khỏe_công_nghệ_sinh_học",
    "giao_duc": "giáo_dục_đào_tạo_nghiên_cứu",
    "fnb_khach_san": "du_lịch_nhà_hàng_khách_sạn_dịch_vụ",
    "thiet_ke": "thiết_kế_nghệ_thuật_giải_trí_truyền_hình_báo_chí",
    "khac": "nhóm_nghề_khác",
}
# VietJobs classes with no counterpart in our taxonomy (excluded from "mapped" accuracy).
UNMAPPED_VIETJOBS = {"ngôn_ngữ_dịch_thuật", "nông_nghiệp_năng_lượng_môi_trường",
                     "nhóm_nghề_khác"}

# ── Normalisation ────────────────────────────────────────────────

_PREFIXES = (
    "sử dụng thành thạo các ", "sử dụng thành thạo ", "sử dụng tốt ", "sử dụng được ",
    "biết sử dụng ", "có khả năng sử dụng ", "khả năng sử dụng ", "sử dụng ", "thành thạo ",
    "thông thạo ", "thạo ", "cơ bản ", "có kỹ năng ", "kỹ năng ", "kĩ năng ", "có khả năng ", "khả năng ",
    "kiến thức về ", "kiến thức ", "hiểu biết về ", "hiểu biết ", "am hiểu về ", "am hiểu ",
    "kinh nghiệm sử dụng ", "kinh nghiệm với ", "biết ", "có ", "ưu tiên ", "proficient in ",
    "knowledge of ", "experience with ", "các ", "công cụ ", "ứng dụng ",
)
_SUFFIXES = (
    " tốt", " cơ bản", " thành thạo", " nâng cao", " chuyên nghiệp", " hiệu quả",
    " là lợi thế", " là một lợi thế", " là điểm cộng", " (bắt buộc)", " skills", " skill",
    " căn bản", " trôi chảy", " lưu loát",
)
_LANGUAGE = re.compile(r"^(?:tiếng|ngoại ngữ) (anh|nhật|hàn|trung|hoa|pháp|đức|thái|nga)\b")
_JUNK = {
    "", "khác", "không", "không yêu cầu", "unk", "none", "n/a", "internet", "máy tính",
    "vi tính", "tin học", "office", "văn phòng", "phần mềm", "công nghệ", "kỹ thuật",
    "kinh doanh", "bán hàng", "marketing", "kế toán", "sales", "it", "data", "word excel",
    # adjectives and fragments that VietJobs lists as skills
    "chủ động", "sáng tạo", "tỉ mỉ", "tỉ mỉ trong công việc", "cẩn thận", "nhanh nhẹn",
    "trung thực", "nhiệt tình", "năng động", "chăm chỉ", "chịu khó", "linh hoạt", "cầu tiến",
    "kiên nhẫn", "theo nhóm", "e.g", "words", "tiếng việt", "máy vi tính", "smartphone",
    "chịu áp lực cao", "chịu được áp lực", "làm việc dưới áp lực", "quan sát", "analytical",
    "trách nhiệm", "có trách nhiệm", "tinh thần trách nhiệm", "ham học hỏi", "chịu áp lực",
    "phần mềm văn phòng", "phần mềm chuyên ngành", "thiết bị quay", "hệ điều hành",
}


def _parse_list(value: str) -> list[str]:
    value = (value or "").strip()
    if not value:
        return []
    try:
        items = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        items = value.split(",")
    if isinstance(items, str):
        items = [items]
    return [str(i) for i in items if str(i).strip()]


def _alias_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for canonical, aliases in enrich.CORE_SKILLS.items():
        for name in (canonical, *aliases):
            index.setdefault(normalize_vn_text(name), canonical)
    return index


_ALIASES = _alias_index()


def _strip_affixes(text: str) -> str:
    changed = True
    while changed:
        changed = False
        for prefix in _PREFIXES:
            if text.startswith(prefix) and len(text) > len(prefix) + 1:
                text, changed = text[len(prefix):], True
        for suffix in _SUFFIXES:
            if text.endswith(suffix) and len(text) > len(suffix) + 1:
                text, changed = text[: -len(suffix)], True
        for tool_prefix in ("phần mềm kế toán ", "phần mềm ", "hệ thống "):
            rest = text[len(tool_prefix):]
            if text.startswith(tool_prefix) and rest.isascii() and len(rest) > 1:
                text, changed = rest, True  # "phần mềm kế toán misa" → "misa"
        text = text.strip(" -:.,;*•+'\"")
    return text


def normalize_skill(raw: str) -> list[str]:
    """One labelled skill → canonical display names (several for "A (B, C)")."""
    text = normalize_vn_text(raw).replace("&amp;", "&")
    parts = [text]
    inner = re.findall(r"\(([^)]*)\)", text)
    if inner:
        parts = [re.sub(r"\([^)]*\)", " ", text)]
        for group in inner:
            parts += re.split(r"[,;/]| và | and ", group)
    names: list[str] = []
    for part in parts:
        for piece in re.split(r"[,;]| và | and |\s/\s", part):
            # "Excel/Word" is two tools, "UI/UX" or "CI/CD" is one.
            pieces = piece.split("/") if re.fullmatch(r"[^/]{4,}/[^/]{4,}", piece) else [piece]
            for name in pieces:
                name = normalize_vn_text(name).strip(" -:.,;*•+'\"")
                if name in _ALIASES:
                    names.append(_ALIASES[name])
                    continue
                name = _strip_affixes(name)
                language = _LANGUAGE.match(name)
                if language:
                    lang = "trung" if language.group(1) == "hoa" else language.group(1)
                    names.append(f"tiếng {lang}")
                    continue
                name = _ALIASES.get(name, name)
                if (name in _JUNK or len(name) < 2 or len(name) > 40 or len(name.split()) > 5
                        or not re.search(r"[^\W\d_]", name)):
                    continue
                names.append(name)
    return list(dict.fromkeys(names))


# ── Data ─────────────────────────────────────────────────────────


def download(path: Path = CACHE_PATH) -> Path:
    if path.exists() and path.stat().st_size > 1_000_000:
        return path
    import requests

    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {DATASET_URL} → {path}")
    with requests.get(DATASET_URL, stream=True, timeout=60) as response:
        response.raise_for_status()
        tmp = path.with_suffix(".part")
        with tmp.open("wb") as handle:
            for chunk in response.iter_content(1 << 20):
                handle.write(chunk)
        tmp.replace(path)
    return path


def load_rows(path: Path) -> list[dict]:
    csv.field_size_limit(1 << 30)
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def split(rows: list[dict], eval_size: int) -> tuple[list[dict], list[dict]]:
    indices = list(range(len(rows)))
    random.Random(EVAL_SEED).shuffle(indices)
    held = set(indices[:eval_size])
    return ([r for i, r in enumerate(rows) if i not in held],
            [r for i, r in enumerate(rows) if i in held])


# ── Build ────────────────────────────────────────────────────────


def build(rows: list[dict], top: int) -> dict[str, list[str]]:
    counts: Counter[str] = Counter()
    soft_counts: Counter[str] = Counter()
    soft_marked: Counter[str] = Counter()
    for row in rows:
        seen: set[str] = set()
        soft: set[str] = set()
        for raw in _parse_list(row.get("technical_skills", "")):
            seen.update(normalize_skill(raw))
        for raw in _parse_list(row.get("soft_skills", "")):
            names = normalize_skill(raw)
            if re.match(r"\s*(?:có )?(?:kỹ|kĩ) năng ", normalize_vn_text(raw)):
                soft_marked.update(names)
            soft.update(names)
        for raw in _parse_list(row.get("languages_required", "")):
            seen.update(n for n in normalize_skill(raw) if n.startswith("tiếng "))
        counts.update(seen)
        soft_counts.update(soft - seen)
    # Soft skills count only when written as "kỹ năng X" at least 20 times, which
    # keeps "đàm phán"/"thuyết trình" and drops adjectives ("trung thực").
    for name, count in soft_counts.items():
        if soft_marked[name] >= 20:
            counts[name] += count

    # Merge display variants that fold to the same key ("kĩ năng"/"kỹ năng", "hoà"/"hòa").
    by_key: dict[str, Counter[str]] = defaultdict(Counter)
    for name, count in counts.items():
        key = enrich.skill_key(name)
        if key and key not in enrich._BLOCKED_SKILL_KEYS:
            by_key[key][name] += count

    ranked = sorted(by_key.items(), key=lambda kv: -sum(kv[1].values()))
    dictionary: dict[str, list[str]] = {}
    for canonical, aliases in enrich.CORE_SKILLS.items():
        dictionary[canonical] = sorted(set(aliases))
    for skill in TECH_SKILLS:
        dictionary.setdefault(skill, [])
    known = {enrich.skill_key(n) for c, a in dictionary.items() for n in (c, *a)}
    candidates: dict[str, str] = {}  # canonical display name -> key
    for key, variants in ranked:
        if key in known or sum(variants.values()) < MIN_LABEL_COUNT:
            continue
        canonical = variants.most_common(1)[0][0]
        if len(key.split()) == 1 and enrich._has_vn_diacritics(canonical):
            continue  # single Vietnamese syllables are everyday words after folding
        candidates[canonical] = key
        if len(candidates) >= MAX_CANDIDATES:
            break

    precision = label_precision(rows, candidates)
    for canonical in candidates:
        if len(dictionary) >= top:
            break
        found, labelled = precision.get(canonical, (0, 0))
        # Keep names that postings label as a skill when they use them; generic
        # words ("kinh nghiệm", "công việc", "báo cáo") appear everywhere unlabelled.
        # Names too rare in running text to measure cannot flood the tags either.
        if found < MIN_TEXT_HITS or labelled / found >= MIN_PRECISION:
            dictionary[canonical] = []
    return dictionary


def label_precision(rows: list[dict], candidates: dict[str, str]) -> dict[str, tuple[int, int]]:
    """``{skill: (postings whose text mentions it, ... that also label it)}``."""
    matcher = enrich.SkillMatcher({name: [] for name in candidates})
    sample = random.Random(EVAL_SEED + 1).sample(rows, min(PRECISION_SAMPLE, len(rows)))
    found: Counter[str] = Counter()
    labelled: Counter[str] = Counter()
    for row in sample:
        text = "\n".join(filter(None, (row.get("job_title"), row.get("description"),
                                        row.get("requirements_text"))))
        hits = set(matcher.find(text, limit=1000))
        labels = {matcher.index.get(enrich.skill_key(n))
                  for col in ("technical_skills", "soft_skills", "languages_required")
                  for raw in _parse_list(row.get(col, "")) for n in normalize_skill(raw)}
        found.update(hits)
        labelled.update(hits & labels)
    return {name: (found[name], labelled[name]) for name in candidates}


def write(dictionary: dict[str, list[str]], path: Path, source_rows: int) -> int:
    payload = {
        "version": 1,
        "source": "dinhieufam/VietJobs (MIT) + src.etl.enrich.CORE_SKILLS + TECH_SKILLS",
        "built_from_rows": source_rows,
        "skills": {k: v for k, v in sorted(dictionary.items())},
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data = text.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise SystemExit(f"{path} would be {len(data)} bytes (> {MAX_BYTES}); lower --top")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data + b"\n")
    return len(data)


# ── Evaluation ───────────────────────────────────────────────────


def _years_label(value: str) -> int | None:
    value = normalize_vn_text(value)
    if not value:
        return None
    if value.startswith("không"):
        return 0
    match = re.match(r"(\d+)\s*(năm|tháng)", value)
    if not match:
        return None
    number = int(match.group(1))
    return number if match.group(2) == "năm" else number // 12


def evaluate(sample: list[dict], dictionary: dict[str, list[str]]) -> dict:
    matcher = enrich.SkillMatcher(dictionary)
    legacy = enrich.SkillMatcher({s: [] for s in TECH_SKILLS})
    stats: Counter[str] = Counter()
    confusion: Counter[tuple[str, str]] = Counter()
    started = time.perf_counter()
    for row in sample:
        title = row["job_title"]
        body = "\n".join(filter(None, [row.get("description", ""), row.get("requirements_text", "")]))
        stats["rows"] += 1

        # Industry
        predicted = enrich.classify_industry(title, body)
        expected = row["category"]
        mapped = INDUSTRY_TO_VIETJOBS[predicted]
        stats["industry_correct"] += mapped == expected
        confusion[(expected, mapped)] += 1
        if expected not in UNMAPPED_VIETJOBS:
            stats["industry_mapped_rows"] += 1
            stats["industry_mapped_correct"] += mapped == expected
        stats["industry_khac"] += predicted == "khac"

        # Skills
        found = set(matcher.find(f"{title}\n{body}", limit=100))
        stats["rows_with_skill"] += bool(found)
        stats["skills_found"] += len(found)
        stats["rows_with_legacy_skill"] += bool(legacy.find(f"{title}\n{body}"))
        labels = {n for raw in _parse_list(row.get("technical_skills", ""))
                  for n in normalize_skill(raw)}
        for label in labels:
            canonical = matcher.index.get(enrich.skill_key(label))
            stats["label_skills"] += 1
            if canonical is None:
                continue
            stats["label_in_dictionary"] += 1
            stats["label_found"] += canonical in found

        # Experience (label: the board's structured field; text may not say it)
        label_years = _years_label(row.get("experience_required", ""))
        low, _ = enrich.extract_experience(row.get("requirements_text", ""),
                                           row.get("description", ""), title)
        if label_years is not None:
            stats["exp_labelled"] += 1
            if low is not None:
                stats["exp_extracted"] += 1
                stats["exp_exact"] += low == label_years

        # Benefits: share of the board's benefit bullets we can map to a code
        for item in _parse_list(row.get("benefits", "")):
            stats["benefit_items"] += 1
            stats["benefit_items_mapped"] += bool(enrich.extract_benefits(item))

        # Level (no label in VietJobs: report the distribution)
        stats[f"level_{enrich.extract_level(title, body, low)}"] += 1
    stats["ms_per_row_x1000"] = int((time.perf_counter() - started) * 1e6 / max(1, len(sample)))

    def pct(a: str, b: str) -> str:
        return f"{100 * stats[a] / max(1, stats[b]):.1f}%"

    report = {
        "rows": stats["rows"],
        "industry_accuracy_all_16_classes": pct("industry_correct", "rows"),
        "industry_accuracy_mapped_13_classes": pct("industry_mapped_correct",
                                                   "industry_mapped_rows"),
        "industry_khac_share": pct("industry_khac", "rows"),
        "skill_rows_with_any_skill": pct("rows_with_skill", "rows"),
        "skill_rows_with_any_legacy_tech_skill": pct("rows_with_legacy_skill", "rows"),
        "skill_avg_per_row": round(stats["skills_found"] / max(1, stats["rows"]), 2),
        "skill_labels_in_dictionary": pct("label_in_dictionary", "label_skills"),
        "skill_label_recall_in_text": pct("label_found", "label_in_dictionary"),
        "experience_extracted_when_labelled": pct("exp_extracted", "exp_labelled"),
        "experience_min_exact_when_extracted": pct("exp_exact", "exp_extracted"),
        "benefit_items_mapped": pct("benefit_items_mapped", "benefit_items"),
        "levels": {k[6:]: v for k, v in sorted(stats.items()) if k.startswith("level_")},
        "ms_per_row": stats["ms_per_row_x1000"] / 1000,
        "top_confusions": [
            f"{e} → {p}: {n}" for (e, p), n in confusion.most_common(40) if e != p
        ][:12],
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--top", type=int, default=3000, help="number of canonical skills")
    parser.add_argument("--eval-size", type=int, default=2000, help="held-out rows")
    parser.add_argument("--evaluate", action="store_true", help="print metrics on held-out rows")
    parser.add_argument("--no-write", action="store_true", help="do not write the JSON")
    parser.add_argument("--csv", type=Path, default=CACHE_PATH)
    args = parser.parse_args()

    rows = load_rows(download(args.csv))
    train, held_out = split(rows, args.eval_size)
    dictionary = build(train, args.top)
    aliases = sum(len(v) for v in dictionary.values())
    print(f"rows={len(rows)} train={len(train)} held_out={len(held_out)} "
          f"skills={len(dictionary)} aliases={aliases}")
    if not args.no_write:
        size = write(dictionary, OUTPUT_PATH, len(train))
        print(f"wrote {OUTPUT_PATH.relative_to(ROOT)} ({size / 1024:.1f} KB)")
    if args.evaluate:
        print(json.dumps(evaluate(held_out, dictionary), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
