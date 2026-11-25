"""Unit tests for Wikidata P106 candidate triage and QuickStatements export."""

from __future__ import annotations

import csv
from pathlib import Path

from etl.wikidata_p106_triage import (
    classify_p106_candidate,
    export_approved_csv,
    extract_bio_activities,
    format_quickstatements_line,
    write_triage_outputs,
)


def _bio_wikitext(activities: str = "Attrice|Attività2=Doppiatrice", dub_section: bool = True) -> str:
    section = "\n== Doppiaggio ==\n* Film X\n" if dub_section else "\n== Biografia ==\n* Nota\n"
    return f"{{{{Bio|Nome=Ada|Cognome=Test|Attività={activities}|Nazionalità=italiana}}}}{section}"


def test_extract_bio_activities_handles_multiple_slots() -> None:
    wikitext = _bio_wikitext("Attrice|Attività2=Doppiatrice|Attività3=Conduttrice televisiva|AttivitàAltre=e direttrice del doppiaggio")
    activities = extract_bio_activities(wikitext)
    assert activities == (
        "attrice",
        "doppiatrice",
        "conduttrice televisiva",
        "e direttrice del doppiaggio",
    )


def test_classify_p106_candidate_all_buckets() -> None:
    base_kwargs = {
        "wikidata_qid": "Q100",
        "display_name": "Ada Test",
        "birth_year": 1970,
        "revision_id": 123456,
        "wikipedia_url": "https://it.wikipedia.org/wiki/Ada_Test",
    }

    # Tier 1: clean, P31=Q5, P27=Q38, explicit doppiatrice in Bio + == Doppiaggio ==
    tier1 = classify_p106_candidate(
        **base_kwargs,
        wikitext=_bio_wikitext(),
        has_unsourced_flag=False,
        compliance_flags=("third_party_catalog_ref",),
        p31_qids=("Q5",),
        p106_qids=("Q33999",),
        p27_qids=("Q38",),
    )
    assert tier1.tier == "TIER1_AUTO_READY"
    assert tier1.review_reason == "high_confidence_explicit_bio"

    # Unsourced template -> Tier 2 manual review
    unsourced = classify_p106_candidate(
        **base_kwargs,
        wikitext=_bio_wikitext(),
        has_unsourced_flag=True,
        compliance_flags=("unsourced_template",),
        p31_qids=("Q5",),
        p106_qids=("Q33999",),
        p27_qids=("Q38",),
    )
    assert unsourced.tier == "TIER2_MANUAL_REVIEW"
    assert unsourced.review_reason == "unsourced_itwiki"

    # Missing explicit doppiatore/doppiatrice in Bio -> Tier 2 manual review
    not_explicit = classify_p106_candidate(
        **base_kwargs,
        wikitext=_bio_wikitext("Attrice"),
        has_unsourced_flag=False,
        compliance_flags=(),
        p31_qids=("Q5",),
        p106_qids=("Q33999",),
        p27_qids=("Q38",),
    )
    assert not_explicit.tier == "TIER2_MANUAL_REVIEW"
    assert not_explicit.review_reason == "bio_not_explicit_or_no_dub_section"

    # Missing both P106 and P27 -> Tier 2
    no_p27 = classify_p106_candidate(
        **base_kwargs,
        wikitext=_bio_wikitext(),
        has_unsourced_flag=False,
        compliance_flags=(),
        p31_qids=("Q5",),
        p106_qids=(),
        p27_qids=(),
    )
    assert no_p27.tier == "TIER2_MANUAL_REVIEW"
    assert no_p27.review_reason == "missing_both_p106_and_p27"

    # Foreign / Kingdom of Italy citizenship missing P106 -> Tier 2, never overwrite P27
    hist_missing = classify_p106_candidate(
        **base_kwargs,
        wikitext=_bio_wikitext(),
        has_unsourced_flag=False,
        compliance_flags=(),
        p31_qids=("Q5",),
        p106_qids=("Q33999",),
        p27_qids=("Q172579",),
    )
    assert hist_missing.tier == "TIER2_MANUAL_REVIEW"
    assert hist_missing.review_reason == "foreign_or_historical_p27_missing_p106"

    # Historical citizenship already voice -> NO_ACTION
    hist_ok = classify_p106_candidate(
        **base_kwargs,
        wikitext=_bio_wikitext(),
        has_unsourced_flag=False,
        compliance_flags=(),
        p31_qids=("Q5",),
        p106_qids=("Q2405480",),
        p27_qids=("Q172579",),
    )
    assert hist_ok.tier == "NO_ACTION"
    assert hist_ok.review_reason == "historical_or_foreign_p27_already_voice"

    # Already matching -> NO_ACTION
    matched = classify_p106_candidate(
        **base_kwargs,
        wikitext=_bio_wikitext(),
        has_unsourced_flag=False,
        compliance_flags=(),
        p31_qids=("Q5",),
        p106_qids=("Q2405480",),
        p27_qids=("Q38",),
    )
    assert matched.tier == "NO_ACTION"
    assert matched.review_reason == "already_matching_wikidata"

    # Missing P31=Q5 -> Tier 2
    non_human = classify_p106_candidate(
        **base_kwargs,
        wikitext=_bio_wikitext(),
        has_unsourced_flag=False,
        compliance_flags=(),
        p31_qids=("Q15",),
        p106_qids=(),
        p27_qids=(),
    )
    assert non_human.tier == "TIER2_MANUAL_REVIEW"
    assert non_human.review_reason == "missing_p31_human"


def test_quickstatements_formatting_and_manual_export(tmp_path: Path) -> None:
    line = format_quickstatements_line("Q374855", 139000123)
    assert line == 'Q374855\tP106\tQ11481802\tS143\tQ11920\tS4656\t"https://it.wikipedia.org/w/index.php?oldid=139000123"'

    c_tier1 = classify_p106_candidate(
        wikidata_qid="Q101",
        display_name="Mario Rossi",
        birth_year=1975,
        revision_id=10001,
        wikipedia_url="https://it.wikipedia.org/wiki/Mario_Rossi",
        wikitext=_bio_wikitext("Doppiatore"),
        has_unsourced_flag=False,
        compliance_flags=(),
        p31_qids=("Q5",),
        p106_qids=("Q33999",),
        p27_qids=("Q38",),
    )
    c_tier2_yes = classify_p106_candidate(
        wikidata_qid="Q102",
        display_name="Giulia Bianchi",
        birth_year=1982,
        revision_id=10002,
        wikipedia_url="https://it.wikipedia.org/wiki/Giulia_Bianchi",
        wikitext=_bio_wikitext("Attrice"),
        has_unsourced_flag=False,
        compliance_flags=(),
        p31_qids=("Q5",),
        p106_qids=("Q33999",),
        p27_qids=("Q38",),
    )
    c_no_action = classify_p106_candidate(
        wikidata_qid="Q103",
        display_name="Carlo Verdi",
        birth_year=1920,
        revision_id=10003,
        wikipedia_url="https://it.wikipedia.org/wiki/Carlo_Verdi",
        wikitext=_bio_wikitext("Doppiatore"),
        has_unsourced_flag=False,
        compliance_flags=(),
        p31_qids=("Q5",),
        p106_qids=("Q2405480",),
        p27_qids=("Q172579",),
    )

    summary = write_triage_outputs([c_tier1, c_tier2_yes, c_no_action], tmp_path)
    assert summary["tier1_count"] == 1
    assert summary["tier2_count"] == 1
    assert summary["no_action_count"] == 1
    assert Path(summary["files"]["pilot_qs"]).read_text(encoding="utf-8").strip().startswith("Q101\tP106\tQ11481802")

    # Simulate user filling SI on Q102 and accidentally writing SI on NO_ACTION Q103
    manual_csv = Path(summary["files"]["manual_csv"])
    with manual_csv.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fieldnames = list(rows[0].keys())
    for row in rows:
        if row["wikidata_qid"] in {"Q102", "Q103"}:
            row["approvato"] = "SÌ" if row["wikidata_qid"] == "Q102" else "SI"
    with manual_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    approved_qs = tmp_path / "manual_approved.qs"
    res = export_approved_csv(manual_csv, approved_qs)
    assert res == {"exported": 1, "skipped": 1}
    content = approved_qs.read_text(encoding="utf-8").strip().splitlines()
    assert len(content) == 1
    assert content[0].startswith("Q102\tP106\tQ11481802\tS143\tQ11920\tS4656\t\"https://it.wikipedia.org/w/index.php?oldid=10002\"")
