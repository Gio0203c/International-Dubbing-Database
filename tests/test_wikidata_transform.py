"""Unit tests for etl.wikidata.transform edge cases and sample fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from etl.wikidata.transform import (
    SpeakerRecord,
    build_sort_name,
    parse_country_code,
    parse_gender,
    parse_imdb_nconst,
    parse_legal_name,
    parse_native_lang,
    parse_qid,
    parse_tmdb_person_id,
    parse_year,
    transform_binding,
    transform_bindings,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "wikidata_sample_bindings.json"
)


def test_parse_qid_accepts_uris_and_rejects_invalid_values() -> None:
    assert parse_qid("http://www.wikidata.org/entity/Q3870135") == "Q3870135"
    assert parse_qid("Q117085918") == "Q117085918"
    assert parse_qid("http://www.wikidata.org/entity/Q0") is None
    assert parse_qid("http://www.wikidata.org/.well-known/genid/12345") is None
    assert parse_qid("_:b0") is None
    assert parse_qid("P345") is None
    assert parse_qid("") is None
    assert parse_qid(None) is None


def test_parse_year_handles_iso_precision_ranges_and_blank_nodes() -> None:
    assert parse_year("1975-08-13T00:00:00Z") == 1975
    assert parse_year("+2004-09-20T00:00:00Z") == 2004
    assert parse_year("1967") == 1967
    assert (
        parse_year(
            "http://www.wikidata.org/.well-known/genid/x|1975-05-01T00:00:00Z|1974-01-01T00:00:00Z"
        )
        == 1974
    )
    assert parse_year("-0044-03-15T00:00:00Z") is None
    assert parse_year("1492-01-01T00:00:00Z") is None
    assert parse_year("2200-01-01T00:00:00Z") is None
    assert parse_year("_:t123") is None
    assert parse_year("") is None


def test_parse_gender_maps_qids_independently_of_language() -> None:
    assert parse_gender("http://www.wikidata.org/entity/Q6581072") == "female"
    assert parse_gender("http://www.wikidata.org/entity/Q6581097") == "male"
    assert parse_gender("http://www.wikidata.org/entity/Q48270") == "non_binary"
    assert parse_gender("http://www.wikidata.org/entity/Q999999") == "other"
    assert parse_gender("http://www.wikidata.org/.well-known/genid/1") is None
    assert parse_gender(None) is None


def test_parse_iso_codes_are_deterministic_regardless_of_order() -> None:
    assert parse_country_code("US|IT") == parse_country_code("IT|US") == "IT"
    assert parse_country_code("it|invalid|USA") == "IT"
    assert parse_country_code("USA|12") is None

    assert parse_native_lang("fr|EN|ITA") == parse_native_lang("EN|fr") == "en"
    assert parse_native_lang("it") == "it"
    assert parse_native_lang("ita|123") is None


def test_parse_bridge_ids_validate_and_resolve_multi_values() -> None:
    assert parse_imdb_nconst("nm1129594") == "nm1129594"
    assert parse_imdb_nconst("tt0111161|nm9999999|nm0000123|bad") == "nm0000123"
    assert parse_imdb_nconst("tt0111161|nmXYZ|_:b0") is None

    assert parse_tmdb_person_id("1137540") == 1137540
    assert parse_tmdb_person_id("-5|0|abc|999999999999|4500|320") == 320
    assert parse_tmdb_person_id("0|-1|abc|3000000000") is None


def test_build_sort_name_handles_surnames_compound_names_and_fallbacks() -> None:
    assert build_sort_name("Nanni Baldini", "Baldini") == "Baldini, Nanni"
    assert build_sort_name("Domitilla D'Amico", "D'Amico") == "D'Amico, Domitilla"
    assert build_sort_name("Perla Liberatori", "") == "Perla Liberatori"
    assert build_sort_name("Perla Liberatori", None) == "Perla Liberatori"
    # Stage name whose P734 does not match the end of display_name stays untouched
    assert build_sort_name("Nanni Stage", "Baldini") == "Nanni Stage"
    # Longer compound surname matches ahead of a shorter suffix fragment
    assert build_sort_name("Marco De Luca", "Luca|De Luca") == "De Luca, Marco"


def test_parse_legal_name_omits_duplicates_of_display_name() -> None:
    assert parse_legal_name("Giovanni Baldini", "Nanni Baldini") == "Giovanni Baldini"
    assert parse_legal_name("Domitilla D'Amico", "Domitilla D'Amico") is None
    assert parse_legal_name("domitilla d'amico", "Domitilla D'Amico") is None
    assert parse_legal_name("http://www.wikidata.org/.well-known/genid/1", "A") is None


def test_transform_binding_ignores_bnode_type_cells() -> None:
    binding = {
        "person": {"type": "bnode", "value": "t12345"},
        "displayName": {"type": "literal", "value": "Anonymous"},
    }
    assert transform_binding(binding) is None


def test_transform_bindings_on_sample_fixture() -> None:
    raw_bindings = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    result = transform_bindings(raw_bindings)

    assert result.discarded_count == 2
    assert result.duplicate_qid_count == 1
    assert len(result.records) == 6

    by_qid = {rec.wikidata_qid: rec for rec in result.records}

    assert by_qid["Q3870135"] == SpeakerRecord(
        wikidata_qid="Q3870135",
        display_name="Nanni Baldini",
        sort_name="Baldini, Nanni",
        legal_name="Giovanni Baldini",
        birth_year=1975,
        death_year=None,
        gender="male",
        native_lang=None,
        country_code="IT",
        imdb_nconst="nm1129594",
        tmdb_person_id=None,
    )

    assert by_qid["Q3713381"].legal_name is None
    assert by_qid["Q3713381"].sort_name == "D'Amico, Domitilla"
    assert by_qid["Q3713381"].tmdb_person_id == 1137540

    assert by_qid["Q3297658"].sort_name == "Perla Liberatori"
    assert by_qid["Q117085918"].native_lang == "it"

    dirty = by_qid["Q3764098"]
    assert dirty.display_name == "Dirty Multi Actor"
    assert dirty.sort_name == "Multi Actor, Dirty"
    assert dirty.legal_name is None
    assert dirty.birth_year == 1974
    assert dirty.death_year is None
    assert dirty.gender == "non_binary"
    assert dirty.native_lang == "en"
    assert dirty.country_code == "IT"
    assert dirty.imdb_nconst == "nm0000123"
    assert dirty.tmdb_person_id == 320

    bad_lifespan = by_qid["Q9990001"]
    assert bad_lifespan.birth_year is None
    assert bad_lifespan.death_year is None
