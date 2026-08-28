"""Prepare and validate a full-catalog interest-mapping candidate without tagging courses."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from tag_interest_categories import ALLOWED_INTEREST_CATEGORIES, load_mapping, write_json


# Decisions below are intentionally limited to the 30 active human-review
# subjects from Phase 7.5B.  They use the current fixed, broad taxonomy.
HUMAN_REVIEW_DECISIONS: dict[str, dict[str, Any]] = {
    "ADHE": {"category": "Education & Teaching", "rationale": "Adult-learning theory, instructional practice, and educator development dominate.", "alternatives": ["Society & Culture"], "confidence": "high"},
    "AI": {"category": "Technology & Computing", "rationale": "Machine learning, reasoning, deep learning, and intelligent-system design dominate.", "alternatives": ["Mathematics & Statistics"], "confidence": "high"},
    "ASIC": {"category": "Society & Culture", "rationale": "The active courses frame global issues and sustainability through interdisciplinary social, humanities, and science perspectives.", "alternatives": ["Environment & Earth", "Arts & Design"], "confidence": "medium"},
    "ASIX": {"category": "Society & Culture", "rationale": "Courses centre Asian and diaspora cultures, migration, communities, and cultural analysis.", "alternatives": ["History & Civilization"], "confidence": "high"},
    "ASTU": {"category": "Literature & Writing", "rationale": "Writing, reading, research, and scholarly communication are the recurring undergraduate core.", "alternatives": ["Society & Culture"], "confidence": "high"},
    "BEST": {"category": "Environment & Earth", "rationale": "Bioeconomy, biomass, ecology, renewable energy, and managed ecosystems are the dominant applied context.", "alternatives": ["Biology & Life Sciences", "Physical Sciences"], "confidence": "medium"},
    "CAP": {"category": "Literature & Writing", "rationale": "Its active courses are writing, reading, research, and literary/cultural/media analysis seminars.", "alternatives": ["Society & Culture"], "confidence": "high"},
    "COGS": {"category": "Psychology & Behaviour", "rationale": "Human mental processes, cognition, brain function, and cognitive research methods are the common focus.", "alternatives": ["Technology & Computing", "Biology & Life Sciences"], "confidence": "medium"},
    "CSPW": {"category": "Physical Sciences", "rationale": "The sole active course is a coordinated science workshop built around scientific topics and hands-on work.", "alternatives": ["Education & Teaching"], "confidence": "medium"},
    "DENT": {"category": "Health & Medicine", "rationale": "Clinical dentistry, oral health, biomedical science, and patient/community care dominate.", "alternatives": [], "confidence": "high"},
    "EXCH": {"category": "Society & Culture", "rationale": "These are cross-faculty undergraduate exchange and study-abroad course shells rather than one discipline.", "alternatives": ["Education & Teaching"], "confidence": "low"},
    "FSCT": {"category": "Physical Sciences", "rationale": "Forensic-science methods, chemistry, evidence, and laboratory analysis are the main subject identity.", "alternatives": ["Biology & Life Sciences"], "confidence": "medium"},
    "HGSE": {"category": "Society & Culture", "rationale": "Indigenous communities, reconciliation, governance, and cross-cultural social-ecological study dominate.", "alternatives": ["Environment & Earth", "Politics & Law"], "confidence": "high"},
    "ILS": {"category": "Environment & Earth", "rationale": "Land, ecosystems, stewardship, and natural-resource management are the repeated course focus.", "alternatives": ["Society & Culture"], "confidence": "medium"},
    "INLB": {"category": "Society & Culture", "rationale": "Indigenous self-determination, community partnerships, and cultural/political land-based study dominate.", "alternatives": ["Environment & Earth"], "confidence": "medium"},
    "INTR": {"category": "Politics & Law", "rationale": "Approved human decision: international politics, policy, governance, and political theory make Politics & Law the more useful discovery category.", "alternatives": ["Society & Culture"], "confidence": "high"},
    "JRNL": {"category": "Literature & Writing", "rationale": "Journalistic writing and communication are the most useful elective-discovery signal.", "alternatives": ["Society & Culture", "Arts & Design"], "confidence": "high"},
    "LASO": {"category": "Politics & Law", "rationale": "The subject explicitly studies law, justice, rights, and legal institutions in society.", "alternatives": ["Society & Culture"], "confidence": "high"},
    "LAW": {"category": "Politics & Law", "rationale": "Contracts, criminal law, public law, property, and advocacy are direct law content.", "alternatives": [], "confidence": "high"},
    "LFS": {"category": "Environment & Earth", "rationale": "Land, food systems, sustainability, managed landscapes, and environmental systems dominate.", "alternatives": ["Biology & Life Sciences", "Health & Medicine"], "confidence": "high"},
    "MDIA": {"category": "Arts & Design", "rationale": "Media creation, production, media interfaces, and public media projects dominate.", "alternatives": ["Technology & Computing", "Literature & Writing"], "confidence": "high"},
    "MEDD": {"category": "Health & Medicine", "rationale": "Medical practice, clinical reasoning, clerkship, and senior clinical electives dominate.", "alternatives": [], "confidence": "high"},
    "MINE": {"category": "Engineering & Applied Science", "rationale": "Mining-system design, operations, processing, and engineering constraints dominate.", "alternatives": ["Environment & Earth"], "confidence": "high"},
    "MRNE": {"category": "Biology & Life Sciences", "rationale": "The active offerings are principally marine biology, invertebrates, fishes, algae, and animal physiology.", "alternatives": ["Environment & Earth"], "confidence": "high"},
    "NSCI": {"category": "Biology & Life Sciences", "rationale": "Cellular, molecular, behavioural, and computational neuroscience are primarily life-science study of the nervous system.", "alternatives": ["Psychology & Behaviour", "Physical Sciences"], "confidence": "medium"},
    "OSOT": {"category": "Health & Medicine", "rationale": "Occupational science and occupational-therapy practice are health disciplines.", "alternatives": ["Psychology & Behaviour"], "confidence": "high"},
    "PLAN": {"category": "Society & Culture", "rationale": "City-making, community engagement, urban justice, and urban studies dominate.", "alternatives": ["Environment & Earth", "Arts & Design"], "confidence": "high"},
    "PPGA": {"category": "Politics & Law", "rationale": "Public policy, global affairs, and policy debates are the direct focus.", "alternatives": ["Society & Culture"], "confidence": "high"},
    "SPPH": {"category": "Health & Medicine", "rationale": "Population health, epidemiology, healthcare, and health policy are central.", "alternatives": ["Society & Culture"], "confidence": "high"},
    "VANT": {"category": "Education & Teaching", "rationale": "Language enrichment, academic support, and multidisciplinary student learning are its primary program role.", "alternatives": ["Engineering & Applied Science"], "confidence": "medium"},
}


def load_json_records(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(record, dict) for record in value):
        raise ValueError(f"Expected a JSON list of records in {path}")
    return value


def load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def validate_full_candidate_mapping(
    mapping: dict[str, str], records: list[dict[str, Any]], original_v1_mapping: dict[str, str]
) -> dict[str, Any]:
    """Validate exact coverage without reusing Version 1's intentional 60-subject guard."""
    active_subjects = {str(record.get("subject")) for record in records}
    mapping_subjects = set(mapping)
    unmapped = sorted(active_subjects - mapping_subjects)
    zero_course_subjects_mapped = sorted(mapping_subjects - active_subjects)
    invalid_categories = sorted(
        f"{subject}: {category}" for subject, category in mapping.items()
        if category not in ALLOWED_INTEREST_CATEGORIES
    )
    changed_v1 = sorted(
        subject for subject, category in original_v1_mapping.items() if mapping.get(subject) != category
    )
    return {
        "total_subjects_with_undergraduate_courses": len(active_subjects),
        "total_mapped_subjects": len(mapping),
        "unmapped_subjects": unmapped,
        "mappings_to_invalid_categories": invalid_categories,
        "zero_course_subjects_unnecessarily_mapped": zero_course_subjects_mapped,
        "original_v1_mappings_changed": changed_v1,
        "new_contributing_subjects_without_mappings": sorted(
            (active_subjects - set(original_v1_mapping)) - mapping_subjects
        ),
        "is_valid": not (unmapped or invalid_categories or zero_course_subjects_mapped or changed_v1),
    }


def build_mapping_and_report(
    candidate_path: Path,
    candidate_build_report_path: Path,
    audit_path: Path,
    v1_mapping_path: Path,
    output_mapping_path: Path,
    output_report_path: Path,
) -> dict[str, Any]:
    """Write only mapping/review artifacts; never add tags to candidate courses."""
    records = load_json_records(candidate_path)
    build_report = load_json_object(candidate_build_report_path)
    audit = load_json_object(audit_path)
    v1_mapping = load_mapping(v1_mapping_path)
    active_subjects = {str(record["subject"]) for record in records}
    new_active = set(build_report["summary"]["new_subjects_with_at_least_one_undergraduate_course"])
    expected_active = set(v1_mapping) | new_active
    if active_subjects != expected_active:
        raise ValueError("Candidate subjects do not equal original V1 subjects plus active new subjects")
    proposals = audit["proposed_interest_mapping_for_new_subjects"]
    inventory = {row["code"]: row for row in audit["subject_inventory"]}
    expected_review = set(build_report["summary"]["ambiguous_subjects_with_at_least_one_undergraduate_course"])
    if set(HUMAN_REVIEW_DECISIONS) != expected_review:
        raise ValueError("Human-review decisions must cover exactly the active ambiguous subjects")

    mapping = dict(v1_mapping)
    for subject in sorted(new_active):
        mapping[subject] = HUMAN_REVIEW_DECISIONS.get(subject, proposals[subject])["category"] if subject in HUMAN_REVIEW_DECISIONS else proposals[subject]["candidate_category"]

    validation = validate_full_candidate_mapping(mapping, records, v1_mapping)
    if not validation["is_valid"]:
        raise ValueError(f"Candidate mapping validation failed: {validation}")
    courses_by_subject = Counter(str(record["subject"]) for record in records)
    human_review_rows = []
    for subject in sorted(expected_review):
        decision = HUMAN_REVIEW_DECISIONS[subject]
        examples = [
            {"course_code": record["course_code"], "title": record["title"]}
            for record in records if record["subject"] == subject
        ][:3]
        human_review_rows.append({
            "subject": subject,
            "display_name": inventory[subject]["display_name"],
            "undergraduate_course_count": courses_by_subject[subject],
            "proposed_category": decision["category"],
            "rationale": decision["rationale"],
            "plausible_alternatives": decision["alternatives"],
            "confidence": decision["confidence"],
            "course_examples": examples,
        })
    report = {
        "scope": "Phase 7.5C mapping review only; no candidate courses were tagged or enriched.",
        "candidate_dataset": str(candidate_path),
        "candidate_mapping": str(output_mapping_path),
        "validation": validation,
        "new_contributing_subjects_mapped": len(new_active),
        "human_review": human_review_rows,
        "additional_subjects_recommended_for_human_review": [],
        "mapped_subjects_per_category": dict(sorted(Counter(mapping.values()).items())),
        "candidate_courses_per_category": dict(sorted(
            Counter(mapping[str(record["subject"])] for record in records).items()
        )),
        "missing_description_spot_check_candidates": [
            {"course_code": record["course_code"], "subject": record["subject"], "title": record["title"], "source_url": record["source_url"]}
            for record in records if not str(record.get("description", "")).strip()
        ][:10],
    }
    write_json(dict(sorted(mapping.items())), output_mapping_path)
    write_json(report, output_report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a full-catalog interest mapping for human review.")
    parser.add_argument("--candidate-path", type=Path, default=Path("data/ubc_courses_full_base_candidate.json"))
    parser.add_argument("--candidate-build-report-path", type=Path, default=Path("data/ubc_courses_full_base_candidate_report.json"))
    parser.add_argument("--audit-path", type=Path, default=Path("data/full_catalog_expansion_audit.json"))
    parser.add_argument("--v1-mapping-path", type=Path, default=Path("config/subject_interest_map.json"))
    parser.add_argument("--output-mapping-path", type=Path, default=Path("config/subject_interest_map_full_candidate.json"))
    parser.add_argument("--output-report-path", type=Path, default=Path("data/full_candidate_interest_mapping_review.json"))
    args = parser.parse_args()
    try:
        report = build_mapping_and_report(
            args.candidate_path, args.candidate_build_report_path, args.audit_path,
            args.v1_mapping_path, args.output_mapping_path, args.output_report_path,
        )
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(f"Prepared {report['validation']['total_mapped_subjects']} subject mappings for review; no courses tagged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
