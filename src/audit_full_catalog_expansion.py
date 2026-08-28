"""Audit full UBC Calendar expansion without changing production catalog data.

This tool fetches the Calendar master page and a deliberately bounded,
representative new-subject sample. It writes only a planning report; it never
writes any candidate catalog or production dataset.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from scrape_courses import (
    MASTER_SUBJECT_URL,
    ExtractionMetrics,
    extract_courses_from_subject_html,
    fetch_html,
    find_subject_page_links,
    make_http_session,
    polite_delay,
)
from tag_interest_categories import ALLOWED_INTEREST_CATEGORIES, load_subject_codes


REPRESENTATIVE_NEW_SUBJECTS = (
    "AI", "ARCH", "BA", "CEEN", "DENT", "EECE", "EXGR", "LAW", "MEDD", "DHYG",
    "PHTH", "SOWK", "VANT", "DES", "WRIT",
)

# These are planning-only proposals for the existing fixed taxonomy. The
# fallback remains explicit in the report and is always marked for review.
CATEGORY_CODE_GROUPS = {
    "Technology & Computing": "AI BAIT COGS COLX INFO".split(),
    "Mathematics & Statistics": "BABS".split(),
    "Business & Management": (
        "BA BAAC BAEN BAFI BAHR BALA BAMA BAPA BASC BASM BAUL BUSI COHR COMR MGMT"
    ).split(),
    "Economics & Finance": "AGEC COEC FRE".split(),
    "Psychology & Behaviour": "CNPS ECPS".split(),
    "Biology & Life Sciences": (
        "AANB ANAT AQUA BIOC BIOF BIOT BOTA CELL FISH FOOD GSAT MEDG MICB NEUR NRSC PLNT ZOOL"
    ).split(),
    "Health & Medicine": (
        "AUDI CAPS DENT DHYG FMPR GLBH HUNU MEDD MEDI MIDW NURS OBMS OBST OHS ONCO OSOT "
        "PATH PCTH PHAR PHRM PHTH PHYL PSYT RADI RADS RHSC SPHA SPPH SURG UROL VRHC WACH"
    ).split(),
    "Physical Sciences": "ASTR NSCI SCIE".split(),
    "Environment & Earth": (
        "CONS ENST ENSO ENVR FCOR FOPR FRST GEM GRS LFS LWS NRES RES SOIL UFOR WOOD"
    ).split(),
    "Engineering & Applied Science": (
        "APPP CEEN CHBE EECE ELEC ENPP ENVE HPB IGEN IWME MANU MINE MRNE MTRL NAME SPE SGES URSY"
    ).split(),
    "Society & Culture": (
        "ACAM AFST CCFI CDST CENS CSIS CSPW EXCH FMST HGSE IAR IEST INDS INTR LAST MES SOWK STS"
    ).split(),
    "Politics & Law": "LAW LASO PPGA".split(),
    "History & Civilization": "AMNE ARCL ASIA CLST MDVL NEST".split(),
    "Philosophy & Religion": "JWST RGST".split(),
    "Languages & Linguistics": (
        "ARBC ARBM ASL ASLA CNTO CTLN DANI FNEL GERN GMST GREK HEBR HINU INDO ITAL KORN LATN "
        "NEPL NORD PERS POLS PORT PUNJ RMST RUSS SANS SEAL SLAV SOAL SWAH SWED TIBT UKRN YDSH"
    ).split(),
    "Literature & Writing": "ASTU CHIL WRDS WRIT".split(),
    "Arts & Design": "ARCH ARST ARTC ARTS CCST CINE CMST DES DMED FIPR MDIA THTR THFL UDES VISA".split(),
    "Education & Teaching": "ECED EDCP EDST EDUC EPSE ETEC LIBE LLED VANT".split(),
}

AMBIGUOUS_SUBJECTS = {
    "AI", "APPP", "ARCH", "ARTS", "ASIC", "ASIX", "ASTU", "CAP", "CCFI", "COGS", "CSPW",
    "DENT", "DMED", "ETEC", "EXCH", "EXGR", "HGSE", "IAR", "IEST", "INDS", "INTR", "LAW",
    "LASO", "LFS", "MDIA", "MEDD", "MEDI", "MINE", "MRNE", "NSCI", "OSOT", "PPGA", "RES",
    "SGES", "SPHA", "SPPH", "STS", "UDES", "URSY", "VANT", "VRHC", "WACH", "WRIT",
}


def subject_inventory(master_html: str, master_url: str) -> dict[str, dict[str, str]]:
    """Read code, displayed name, and resolved URL from supplied master HTML."""
    soup = BeautifulSoup(master_html, "html.parser")
    links = find_subject_page_links(master_html, master_url)
    inventory: dict[str, dict[str, str]] = {}
    for link in soup.select('a[href*="/course-descriptions/subject/"]'):
        label = link.get_text(" ", strip=True)
        match = re.match(r"^(?P<code>[A-Z0-9]+)_V\b\s*(?P<name>.*)$", label)
        if not match:
            continue
        code = match.group("code")
        if code not in links:
            continue
        inventory[code] = {
            "display_name": match.group("name").lstrip("- ").strip(),
            "subject_url": links[code],
        }
    return dict(sorted(inventory.items()))


def explicit_categories() -> dict[str, str]:
    """Invert the compact category groups and fail if a proposal conflicts."""
    categories: dict[str, str] = {}
    for category, codes in CATEGORY_CODE_GROUPS.items():
        for code in codes:
            if code in categories and categories[code] != category:
                raise ValueError(f"Conflicting proposed categories for {code}")
            categories[code] = category
    return categories


def category_from_name(display_name: str) -> str:
    """Give every otherwise-unlisted subject a conservative taxonomy proposal."""
    lowered = display_name.lower()
    keyword_categories = (
        (("engineering", "manufacturing", "buildings", "grid", "process"), "Engineering & Applied Science"),
        (("business", "management", "entrepreneur", "marketing", "accounting", "finance"), "Business & Management"),
        (("economics",), "Economics & Finance"),
        (("health", "medicine", "medical", "dental", "nursing", "therapy", "surgery", "pharmacy"), "Health & Medicine"),
        (("biology", "animal", "botany", "plant", "genome", "microbio", "neuro"), "Biology & Life Sciences"),
        (("environment", "forestry", "resource", "soil", "water", "fisher"), "Environment & Earth"),
        (("language", "arabic", "cantonese", "danish", "german", "greek", "hebrew", "hindi", "italian", "japanese", "korean", "latin", "nepali", "persian", "polish", "portuguese", "punjabi", "russian", "sanskrit", "swahili", "swedish", "tibetan", "ukrainian", "yiddish"), "Languages & Linguistics"),
        (("theatre", "cinema", "film", "design", "visual", "media", "art"), "Arts & Design"),
        (("education", "teaching", "curriculum", "pedagogy", "librarian"), "Education & Teaching"),
        (("history", "archae", "medieval", "ancient"), "History & Civilization"),
        (("law", "policy", "political"), "Politics & Law"),
        (("writing", "literature"), "Literature & Writing"),
        (("psychology", "counselling"), "Psychology & Behaviour"),
        (("science", "astronomy"), "Physical Sciences"),
    )
    for keywords, category in keyword_categories:
        if any(keyword in lowered for keyword in keywords):
            return category
    return "Society & Culture"


def proposed_mapping(code: str, display_name: str, categories: dict[str, str]) -> dict[str, Any]:
    """Return one current-taxonomy candidate and whether it needs review."""
    category = categories.get(code, category_from_name(display_name))
    fallback = code not in categories
    review = code in AMBIGUOUS_SUBJECTS or fallback
    reason = None
    if fallback:
        reason = "No subject-code proposal rule; category was inferred from the displayed Calendar name."
    elif code in AMBIGUOUS_SUBJECTS:
        reason = "Interdisciplinary, professional, program, or cross-faculty scope makes this single-tag choice debatable."
    return {
        "candidate_category": category,
        "requires_human_review": review,
        "review_reason": reason,
    }


def sample_subjects(
    session: requests.Session, inventory: dict[str, dict[str, str]]
) -> dict[str, dict[str, Any]]:
    """Fetch only the fixed 15-subject sample sequentially and politely."""
    results: dict[str, dict[str, Any]] = {}
    made_request = False
    for code in REPRESENTATIVE_NEW_SUBJECTS:
        source_url = inventory[code]["subject_url"]
        if made_request:
            polite_delay()
        made_request = True
        metrics = ExtractionMetrics()
        try:
            html, final_url = fetch_html(session, source_url)
            records = extract_courses_from_subject_html(html, final_url, metrics)
            results[code] = {
                "status": "fetched",
                "requested_url": source_url,
                "final_url": final_url,
                "redirected": final_url != source_url,
                "undergraduate_records": len(records),
                "courses_excluded_outside_100_to_499": metrics.excluded_outside_undergraduate_range,
                "malformed_course_headings": metrics.malformed_course_headings,
                "malformed_heading_examples": metrics.malformed_heading_examples,
                "unusual_variable_credit_formats": metrics.unusual_credit_formats,
                "duplicate_course_codes": metrics.duplicate_course_codes,
                "missing_titles": metrics.missing_titles,
                "missing_descriptions": metrics.missing_descriptions,
                "other_parsing_errors": metrics.other_parsing_errors,
            }
        except requests.RequestException as error:
            results[code] = {
                "status": "fetch_failed",
                "requested_url": source_url,
                "fetch_error": str(error),
            }
    return results


def build_report(inventory: dict[str, dict[str, str]], current_subjects: list[str], sample: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a JSON-ready planning artifact without constructing a catalog."""
    current_set = set(current_subjects)
    categories = explicit_categories()
    new_codes = [code for code in inventory if code not in current_set]
    proposals = {
        code: {"display_name": inventory[code]["display_name"], **proposed_mapping(code, inventory[code]["display_name"], categories)}
        for code in new_codes
    }
    if any(value["candidate_category"] not in ALLOWED_INTEREST_CATEGORIES for value in proposals.values()):
        raise ValueError("A proposed category is outside the current fixed taxonomy")

    fetched = [result for result in sample.values() if result["status"] == "fetched"]
    sample_courses = sum(result["undergraduate_records"] for result in fetched)
    sample_zero = sum(result["undergraduate_records"] == 0 for result in fetched)
    sample_without_law = [result for code, result in sample.items() if code != "LAW" and result["status"] == "fetched"]
    no_law_average = (
        sum(result["undergraduate_records"] for result in sample_without_law) / len(sample_without_law)
    )
    sample_average = sample_courses / len(fetched) if fetched else 0
    conservative_total = round(3491 + len(new_codes) * no_law_average)
    sample_rate_total = round(3491 + len(new_codes) * sample_average)
    estimated_subjects_with_undergraduate_courses = round(
        len(current_subjects) + len(new_codes) * (len(fetched) - sample_zero) / len(fetched)
    ) if fetched else len(current_subjects)

    grade_report_path = Path("data/full_grade_enrichment_report.json")
    grade_scaling: dict[str, Any] = {"available": False}
    if grade_report_path.exists():
        grade_report = json.loads(grade_report_path.read_text(encoding="utf-8"))
        summary = grade_report.get("summary", {})
        prior_batches = int(summary.get("unique_subject_session_batches", 0))
        session_count = len(grade_report.get("available_sessions_newest_first", []))
        batches_per_subject = prior_batches / len(current_subjects)
        grade_scaling = {
            "available": True,
            "historical_v1_subject_session_batches": prior_batches,
            "historical_v1_sessions_considered": session_count,
            "historical_batches_per_subject": batches_per_subject,
            "estimated_additional_batches_at_same_average": round(len(new_codes) * batches_per_subject),
            "estimated_full_inventory_batches_at_same_average": round(len(inventory) * batches_per_subject),
            "theoretical_maximum_batches_at_current_session_count": len(inventory) * session_count,
        }

    inventory_rows = []
    for code, item in inventory.items():
        standard_url = item["subject_url"].endswith(f"/subject/{code.lower()}v")
        row: dict[str, Any] = {
            "code": code,
            "display_name": item["display_name"],
            "subject_url": item["subject_url"],
            "already_in_v1": code in current_set,
            "standard_code_v_url": standard_url,
        }
        if code not in current_set:
            row.update(proposals[code])
        inventory_rows.append(row)

    return {
        "scope": "Phase 7.5A planning audit only; no production or candidate catalog was written.",
        "master_subject_url": MASTER_SUBJECT_URL,
        "current_v1_subject_count": len(current_subjects),
        "calendar_subject_count": len(inventory),
        "new_subject_count": len(new_codes),
        "current_v1_subject_codes": current_subjects,
        "new_subject_codes": new_codes,
        "subject_inventory": inventory_rows,
        "representative_sample": sample,
        "representative_sample_summary": {
            "subjects_requested": len(REPRESENTATIVE_NEW_SUBJECTS),
            "subjects_fetched": len(fetched),
            "subjects_with_no_undergraduate_records": [
                code for code, result in sample.items()
                if result["status"] == "fetched" and result["undergraduate_records"] == 0
            ],
            "total_undergraduate_records": sample_courses,
            "total_500_plus_exclusions": sum(result["courses_excluded_outside_100_to_499"] for result in fetched),
            "total_malformed_headings": sum(result["malformed_course_headings"] for result in fetched),
            "total_variable_credit_formats": sum(result["unusual_variable_credit_formats"] for result in fetched),
            "total_missing_descriptions": sum(result["missing_descriptions"] for result in fetched),
            "total_duplicate_course_codes": sum(result["duplicate_course_codes"] for result in fetched),
            "total_parser_errors": sum(result["other_parsing_errors"] for result in fetched),
            "redirects": [code for code, result in sample.items() if result.get("redirected")],
        },
        "estimated_expansion": {
            "method": "Uses the new-subject sample only; LAW is shown separately because its 188 undergraduate records make the small sample highly skewed.",
            "sample_average_new_courses_per_subject": sample_average,
            "sample_average_excluding_law": no_law_average,
            "illustrative_total_courses_excluding_law_outlier": conservative_total,
            "illustrative_total_courses_using_full_sample_average": sample_rate_total,
            "recommended_planning_range_total_courses": [conservative_total, sample_rate_total],
            "estimated_subjects_with_at_least_one_undergraduate_course": estimated_subjects_with_undergraduate_courses,
            "estimated_final_json_size_mb": [round(conservative_total * 3.8 / 3491, 1), round(sample_rate_total * 3.8 / 3491, 1)],
        },
        "proposed_interest_mapping_for_new_subjects": proposals,
        "ambiguous_subjects_requiring_human_review": [
            code for code, proposal in proposals.items() if proposal["requires_human_review"]
        ],
        "grade_enrichment_scaling": grade_scaling,
        "safe_candidate_artifacts": [
            "data/ubc_courses_full_base_candidate.json",
            "data/ubc_courses_full_base_candidate_report.json",
            "data/ubc_courses_full_with_grades_candidate.json",
            "data/ubc_courses_full_grade_report_candidate.json",
            "data/ubc_courses_full_final_candidate.json",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit a possible full UBC Calendar expansion.")
    parser.add_argument(
        "--report-path", type=Path, default=Path("data/full_catalog_expansion_audit.json")
    )
    args = parser.parse_args()
    logging.getLogger().setLevel(logging.ERROR)
    try:
        session = make_http_session()
        master_html, master_url = fetch_html(session, MASTER_SUBJECT_URL)
        inventory = subject_inventory(master_html, master_url)
        current_subjects = load_subject_codes(Path("config/subjects_v1.txt"))
        missing_sample_codes = set(REPRESENTATIVE_NEW_SUBJECTS) - set(inventory) - set(current_subjects)
        if missing_sample_codes:
            raise ValueError(f"Sample codes no longer available: {sorted(missing_sample_codes)}")
        if set(REPRESENTATIVE_NEW_SUBJECTS) & set(current_subjects):
            raise ValueError("Representative sample must contain new subjects only")
        sample = sample_subjects(session, inventory)
        report = build_report(inventory, current_subjects, sample)
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError, requests.RequestException) as error:
        logging.error("Expansion audit failed: %s", error)
        return 1
    print(f"Audited {report['calendar_subject_count']} Calendar subjects; report: {args.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
