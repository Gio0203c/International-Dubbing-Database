"""Triage it.wikipedia voice-actor QIDs missing upstream Wikidata claims into QuickStatements batches."""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from etl.db import connect
from etl.wikidata.extract import get_user_agent
from etl.wikipedia.extract import _mediawiki_get
from etl.wikipedia.transform import (
    BIO_PARAM_RE,
    _clean_bio_value,
    _extract_bio_block,
    detect_compliance_flags,
    has_dubbing_section,
)

WIKIDATA_API_ENDPOINT = "https://www.wikidata.org/w/api.php"
DUB_ACTOR_QID = "Q11481802"
VOICE_ACTOR_QIDS = frozenset({"Q2405480", DUB_ACTOR_QID})
ITALIAN_WIKIPEDIA_ITEM_QID = "Q11920"
QID_RE = re.compile(r"^Q[1-9][0-9]*$")
DUB_ACTIVITY_RE = re.compile(r"\bdoppiat(?:or[ei]|ric[ei])\b", re.IGNORECASE)
APPROVED_TOKENS = frozenset({"SI", "SÌ", "YES", "Y"})


@dataclass(frozen=True)
class TriageCandidate:
    """Classified Wikidata enrichment candidate paired with review guidance."""

    tier: str
    review_reason: str
    wikidata_qid: str
    display_name: str
    birth_year: int | None
    bio_activities: tuple[str, ...]
    current_p106: tuple[str, ...]
    current_p27: tuple[str, ...]
    has_dubbing_section: bool
    has_unsourced_flag: bool
    has_third_party_ref: bool
    revision_id: int
    wikipedia_url: str
    wikidata_url: str
    proposed_action: str
    cosa_fare: str


def extract_bio_activities(wikitext: str) -> tuple[str, ...]:
    """Extract normalized lowercase activity labels from Italian {{Bio}} parameters."""
    block = _extract_bio_block(wikitext)
    if block is None:
        return ()

    raw_params: dict[str, str] = {}
    for match in BIO_PARAM_RE.finditer(block):
        key = match.group(1).strip().lower()
        if key:
            raw_params[key] = match.group(2)

    activities: list[str] = []
    seen: set[str] = set()
    for key in ("attività", "attività2", "attività3", "attivitàaltre"):
        cleaned = _clean_bio_value(raw_params.get(key))
        if cleaned:
            lowered = cleaned.lower()
            if lowered not in seen:
                seen.add(lowered)
                activities.append(lowered)
    return tuple(activities)


def is_explicit_dub_actor_bio(activities: tuple[str, ...]) -> bool:
    """Return True if parsed {{Bio}} activities explicitly mention doppiatore/doppiatrice."""
    return any(DUB_ACTIVITY_RE.search(item) for item in activities)


def format_quickstatements_line(qid: str, revision_id: int) -> str:
    """Build a QuickStatements V1 line adding P106=Q11481802 sourced from it.wikipedia rev."""
    if not QID_RE.fullmatch(qid):
        raise ValueError(f"Invalid Wikidata QID: {qid!r}")
    if revision_id <= 0:
        raise ValueError(f"Revision ID must be positive, got {revision_id!r}")
    rev_url = f"https://it.wikipedia.org/w/index.php?oldid={revision_id}"
    return f'{qid}\tP106\t{DUB_ACTOR_QID}\tS143\t{ITALIAN_WIKIPEDIA_ITEM_QID}\tS4656\t"{rev_url}"'


def _claim_entity_qids(entity: dict[str, Any], prop: str) -> tuple[str, ...]:
    qids: set[str] = set()
    for stmt in entity.get("claims", {}).get(prop, []):
        dv = stmt.get("mainsnak", {}).get("datavalue", {})
        if dv.get("type") == "wikibase-entityid":
            target_id = dv.get("value", {}).get("id")
            if isinstance(target_id, str) and QID_RE.fullmatch(target_id):
                qids.add(target_id)
    return tuple(sorted(qids, key=lambda q: int(q[1:])))


def classify_p106_candidate(
    *,
    wikidata_qid: str,
    display_name: str,
    birth_year: int | None,
    revision_id: int,
    wikipedia_url: str,
    wikitext: str,
    has_unsourced_flag: bool,
    compliance_flags: tuple[str, ...],
    p31_qids: tuple[str, ...],
    p106_qids: tuple[str, ...],
    p27_qids: tuple[str, ...],
) -> TriageCandidate:
    """Classify one itwiki-discovered QID into Tier 1 auto-ready, Tier 2 manual review, or no-action."""
    activities = extract_bio_activities(wikitext)
    dub_section = has_dubbing_section(wikitext)
    has_third_party = "third_party_catalog_ref" in compliance_flags
    wikidata_url = f"https://www.wikidata.org/wiki/{wikidata_qid}"
    has_voice = bool(set(p106_qids) & VOICE_ACTOR_QIDS)
    has_italy = "Q38" in p27_qids

    if "Q5" not in p31_qids:
        tier = "TIER2_MANUAL_REVIEW"
        reason = "missing_p31_human"
        action = ""
        guidance = "Apri Wikidata e verifica se l'elemento e' davvero una persona (P31=Q5) prima di qualsiasi modifica."
    elif has_voice and has_italy:
        tier = "NO_ACTION"
        reason = "already_matching_wikidata"
        action = ""
        guidance = "Nessuna modifica su Wikidata: gia' allineato, rientrera' al prossimo sync Wikidata."
    elif has_voice and not has_italy:
        if p27_qids:
            tier = "NO_ACTION"
            reason = "historical_or_foreign_p27_already_voice"
            action = ""
            guidance = (
                "Nessuna modifica su Wikidata: ha gia' P106=doppiatore e una cittadinanza "
                "storica/estera valida (es. Q172579 Regno d'Italia)."
            )
        else:
            tier = "TIER2_MANUAL_REVIEW"
            reason = "missing_p27_only"
            action = ""
            guidance = (
                "Ha gia' P106=doppiatore ma manca P27: verifica cittadinanza dalla voce Wikipedia "
                "(Q38 Italia o Q172579 se morto prima del 1946) e aggiungila manualmente su Wikidata."
            )
    elif not has_italy:
        action = "ADD_P106_Q11481802"
        if p27_qids:
            tier = "TIER2_MANUAL_REVIEW"
            reason = "foreign_or_historical_p27_missing_p106"
            guidance = (
                "NON aggiungere P27=Q38 (cittadinanza estera o storica Q172579). Apri la voce it.wikipedia: "
                "se e' davvero doppiatore professionista, scrivi SI per aggiungere solo P106=Q11481802."
            )
        else:
            tier = "TIER2_MANUAL_REVIEW"
            reason = "missing_both_p106_and_p27"
            guidance = (
                "Mancano sia P106 sia P27: verifica dalla voce it.wikipedia se e' doppiatore "
                "(scrivi SI per generare P106=Q11481802) e poi aggiungi manualmente il P27 corretto su Wikidata "
                "(Q38 o Q172579 se morto prima del 1946)."
            )
    elif has_unsourced_flag:
        tier = "TIER2_MANUAL_REVIEW"
        reason = "unsourced_itwiki"
        action = "ADD_P106_Q11481802"
        guidance = (
            "La voce it.wikipedia ha l'avviso Senza fonti ({{F}}/{{NN}}): verifica l'attivita' di "
            "doppiatore da una fonte affidabile o primaria (mai copiare da siti proprietari protetti), "
            "poi scrivi SI se confermata."
        )
    elif not is_explicit_dub_actor_bio(activities) or not dub_section:
        tier = "TIER2_MANUAL_REVIEW"
        reason = "bio_not_explicit_or_no_dub_section"
        action = "ADD_P106_Q11481802"
        guidance = (
            "Controlla la voce it.wikipedia: nel template Bio non compare 'doppiatore/doppiatrice' o manca "
            "la sezione Doppiaggio. Verifica che non sia un cameo isolato da talent/attore; se e' davvero "
            "doppiatore, scrivi SI."
        )
    else:
        tier = "TIER1_AUTO_READY"
        reason = "high_confidence_explicit_bio"
        action = "ADD_P106_Q11481802"
        guidance = "Pronto per QuickStatements (voce pulita con Bio esplicito 'doppiatore/doppiatrice' e sezione Doppiaggio)."

    return TriageCandidate(
        tier=tier,
        review_reason=reason,
        wikidata_qid=wikidata_qid,
        display_name=display_name,
        birth_year=birth_year,
        bio_activities=activities,
        current_p106=p106_qids,
        current_p27=p27_qids,
        has_dubbing_section=dub_section,
        has_unsourced_flag=has_unsourced_flag,
        has_third_party_ref=has_third_party,
        revision_id=revision_id,
        wikipedia_url=wikipedia_url,
        wikidata_url=wikidata_url,
        proposed_action=action,
        cosa_fare=guidance,
    )


def fetch_wikidata_claims(
    qids: list[str],
    *,
    user_agent: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch current Wikidata claims for up to 50 QIDs per polite API request."""
    ua = user_agent or get_user_agent()
    client = session or requests.Session()
    owns_session = session is None
    entities: dict[str, dict[str, Any]] = {}

    try:
        for idx in range(0, len(qids), 50):
            chunk = qids[idx : idx + 50]
            if not chunk:
                continue
            payload = _mediawiki_get(
                WIKIDATA_API_ENDPOINT,
                {
                    "action": "wbgetentities",
                    "ids": "|".join(chunk),
                    "props": "claims",
                },
                user_agent=ua,
                session=client,
            )
            entities.update(payload.get("entities", {}))
    finally:
        if owns_session:
            client.close()
    return entities


def _candidate_to_csv_row(item: TriageCandidate, *, include_approval: bool) -> dict[str, str]:
    row = {
        "tier": item.tier,
        "review_reason": item.review_reason,
        "wikidata_qid": item.wikidata_qid,
        "display_name": item.display_name,
        "birth_year": str(item.birth_year) if item.birth_year is not None else "",
        "bio_activities": "; ".join(item.bio_activities),
        "current_p106": "|".join(item.current_p106),
        "current_p27": "|".join(item.current_p27),
        "has_dubbing_section": "yes" if item.has_dubbing_section else "no",
        "has_unsourced_flag": "yes" if item.has_unsourced_flag else "no",
        "has_third_party_ref": "yes" if item.has_third_party_ref else "no",
        "revision_id": str(item.revision_id),
        "wikipedia_url": item.wikipedia_url,
        "wikidata_url": item.wikidata_url,
        "proposed_action": item.proposed_action,
        "cosa_fare": item.cosa_fare,
    }
    if include_approval:
        row["approvato"] = ""
    return row


def write_triage_outputs(candidates: list[TriageCandidate], out_dir: Path) -> dict[str, Any]:
    """Write QuickStatements files and manual review spreadsheets into out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tier1 = sorted(
        (c for c in candidates if c.tier == "TIER1_AUTO_READY"),
        key=lambda c: (c.display_name.lower(), c.wikidata_qid),
    )
    tier2_and_no_action = sorted(
        (c for c in candidates if c.tier != "TIER1_AUTO_READY"),
        key=lambda c: (c.tier, c.review_reason, c.display_name.lower(), c.wikidata_qid),
    )

    pilot_qs = out_dir / "wikidata_p106_tier1_pilot10.qs"
    all_qs = out_dir / "wikidata_p106_tier1_all.qs"
    tier1_csv = out_dir / "wikidata_p106_tier1_summary.csv"
    manual_csv = out_dir / "wikidata_p106_manual_review.csv"

    pilot_lines = [format_quickstatements_line(c.wikidata_qid, c.revision_id) for c in tier1[:10]]
    all_lines = [format_quickstatements_line(c.wikidata_qid, c.revision_id) for c in tier1]
    pilot_qs.write_text("\n".join(pilot_lines) + ("\n" if pilot_lines else ""), encoding="utf-8")
    all_qs.write_text("\n".join(all_lines) + ("\n" if all_lines else ""), encoding="utf-8")

    summary_fields = list(
        _candidate_to_csv_row(
            tier1[0] if tier1 else tier2_and_no_action[0],
            include_approval=False,
        ).keys()
    ) if (tier1 or tier2_and_no_action) else []

    if summary_fields:
        with tier1_csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=summary_fields, lineterminator="\n")
            writer.writeheader()
            for item in tier1:
                writer.writerow(_candidate_to_csv_row(item, include_approval=False))

        manual_fields = [*summary_fields, "approvato"]
        with manual_csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=manual_fields, lineterminator="\n")
            writer.writeheader()
            for item in tier2_and_no_action:
                writer.writerow(_candidate_to_csv_row(item, include_approval=True))

    reason_counts: dict[str, int] = {}
    for item in candidates:
        reason_counts[item.review_reason] = reason_counts.get(item.review_reason, 0) + 1

    return {
        "total": len(candidates),
        "tier1_count": len(tier1),
        "pilot_count": len(pilot_lines),
        "tier2_count": sum(1 for c in candidates if c.tier == "TIER2_MANUAL_REVIEW"),
        "no_action_count": sum(1 for c in candidates if c.tier == "NO_ACTION"),
        "reason_counts": reason_counts,
        "files": {
            "pilot_qs": str(pilot_qs),
            "all_qs": str(all_qs),
            "tier1_csv": str(tier1_csv),
            "manual_csv": str(manual_csv),
        },
    }


def export_approved_csv(input_csv: Path, output_qs: Path) -> dict[str, int]:
    """Convert rows manually marked SI/YES in manual review CSV into QuickStatements V1."""
    lines: list[str] = []
    skipped = 0

    with input_csv.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            verdict = (row.get("approvato") or "").strip().upper()
            if verdict not in APPROVED_TOKENS:
                continue
            if row.get("tier") == "NO_ACTION" or row.get("proposed_action") != "ADD_P106_Q11481802":
                skipped += 1
                continue
            qid = (row.get("wikidata_qid") or "").strip()
            rev_raw = (row.get("revision_id") or "").strip()
            try:
                rev_id = int(rev_raw)
                lines.append(format_quickstatements_line(qid, rev_id))
            except ValueError:
                skipped += 1

    output_qs.parent.mkdir(parents=True, exist_ok=True)
    output_qs.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return {"exported": len(lines), "skipped": skipped}


def generate_triage(out_dir: Path) -> dict[str, Any]:
    """Query itwiki candidates from PostgreSQL, check live Wikidata claims, and write artifacts."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (s.wikidata_qid)
                    s.wikidata_qid,
                    s.display_name,
                    s.birth_year,
                    w.revision_id,
                    w.page_url,
                    w.wikitext,
                    w.has_copyviol_or_unsourced_flag,
                    w.compliance_flags
                FROM public.speakers s
                JOIN staging.wikipedia_raw w
                  ON w.wiki_lang = 'it' AND w.wikidata_qid = s.wikidata_qid
                WHERE s.primary_source = 'itwiki'
                  AND s.wikidata_qid IS NOT NULL
                ORDER BY s.wikidata_qid, w.revision_id DESC
                """
            )
            rows = cur.fetchall()

    qids = [row["wikidata_qid"] for row in rows]
    entities = fetch_wikidata_claims(qids)

    candidates: list[TriageCandidate] = []
    for row in rows:
        qid = row["wikidata_qid"]
        ent = entities.get(qid, {})
        flags = tuple(row["compliance_flags"] or ())
        if not flags:
            flags = detect_compliance_flags(row["wikitext"], "it")
        if "copyviol_template" in flags:
            continue
        has_unsourced = bool(row["has_copyviol_or_unsourced_flag"]) or ("unsourced_template" in flags)
        candidates.append(
            classify_p106_candidate(
                wikidata_qid=qid,
                display_name=row["display_name"],
                birth_year=row["birth_year"],
                revision_id=int(row["revision_id"]),
                wikipedia_url=row["page_url"],
                wikitext=row["wikitext"],
                has_unsourced_flag=has_unsourced,
                compliance_flags=flags,
                p31_qids=_claim_entity_qids(ent, "P31"),
                p106_qids=_claim_entity_qids(ent, "P106"),
                p27_qids=_claim_entity_qids(ent, "P27"),
            )
        )

    return write_triage_outputs(candidates, out_dir)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Triage itwiki Wikidata P106 candidates and export QuickStatements.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen_parser = subparsers.add_parser("generate", help="Generate Tier 1 QuickStatements and Tier 2 review CSV.")
    gen_parser.add_argument("--out-dir", type=Path, default=Path("exports"), help="Output directory (gitignored).")

    exp_parser = subparsers.add_parser("export-approved", help="Convert reviewed SI rows into QuickStatements V1.")
    exp_parser.add_argument(
        "--input",
        type=Path,
        default=Path("exports/wikidata_p106_manual_review.csv"),
        help="Path to reviewed CSV.",
    )
    exp_parser.add_argument(
        "--output",
        type=Path,
        default=Path("exports/wikidata_p106_manual_approved.qs"),
        help="Destination QuickStatements file.",
    )

    args = parser.parse_args()
    if args.command == "generate":
        result = generate_triage(args.out_dir)
        print(f"TRIAGE SUMMARY: {result}")
    else:
        result = export_approved_csv(args.input, args.output)
        print(f"EXPORT APPROVED SUMMARY: {result}")


if __name__ == "__main__":
    main()
