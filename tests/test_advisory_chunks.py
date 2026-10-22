"""Tests for the webhook advisory chunk builder."""

import copy
import hashlib

import pytest

from pipeline.advisory_chunks import build_chunks, clean_value

HEADER = (
    "Sorghum- Harvesting Management\n"
    "Crop: Sorghum | Problem type: Harvesting Management | Season: Rabi"
)
OVERVIEW = (
    "Timely harvesting of AKJ-1 helps preserve grain appearance, processing quality and "
    "suitability for flaking. Proper drying and handling after harvest are essential to "
    "minimize grain damage and deterioration."
)
SYMPTOMS = (
    "Panicles become mature and dry, grains become hard and attain characteristic colour, "
    "and most leaves begin to dry."
)
CONDITIONS = (
    "Crop has reached physiological maturity and panicles are fully developed; grains are "
    "firm and suitable for flaking quality."
)
ADVISORY = (
    "Harvest AKJ-1 at physiological maturity when the grains are fully developed and firm. "
    "Timely harvesting is important for maintaining grain quality required for flaking. "
    "Avoid delayed harvesting, which may increase grain weathering, shattering and mould "
    "risk. After harvesting, dry the panicles properly, thresh carefully, clean the grain "
    "and reduce moisture to a safe level before storage or processing."
)

SORGHUM_PAYLOAD = {
    "contentID": 987,
    "title": "Sorghum- Harvesting Management",
    "commodityName": "Sorghum",
    "creatorName": "jak",
    "domainName": "Agriculture",
    "subdomainName": "Cereals",
    "instituteName": "ICAR–Directorate of Knowledge Management in Agriculture (DKMA)",
    "isPublished": 1,
    "status": "Published",
    "popList": [
        {
            "popID": 819,
            "season": "Rabi",
            "problemType": '"Harvesting Management"',
            "problemDetails": '"Method Of Harvesting"',
            "startPeriod": "105",
            "endPeriod": "120",
            "status": "Published",
            "description": f'"{OVERVIEW}"',
            "applicableSymptoms": [SYMPTOMS],
            "applicableCondition": [CONDITIONS],
            "advisory": f'"{ADVISORY}"',
        }
    ],
}


def make_row(payload=None, **overrides):
    payload = copy.deepcopy(SORGHUM_PAYLOAD if payload is None else payload)
    row = {
        "id": "row-1",
        "external_id": "987",
        "title": payload.get("title", ""),
        "slug": "987/sorghum--harvesting-management",
        "status": payload.get("status"),
        "is_published": payload.get("isPublished"),
        "payload": payload,
        "payload_hash": "hash-1",
        "updated_at": "2026-10-16T09:15:10.000Z",
    }
    row.update(overrides)
    return row


def long_when_combined(text: str) -> int:
    """Over the limit only when the entry carries more than one question-side field."""
    return 600 if "Overview: " in text and "Symptoms: " in text else 300


def pop_of(row):
    return row["payload"]["popList"][0]


class TestCleanValue:
    def test_strips_wrapping_quotes(self):
        assert clean_value('"Aphids"') == "Aphids"
        assert clean_value('""') == ""

    def test_handles_none_numbers_and_whitespace(self):
        assert clean_value(None) == ""
        assert clean_value(819) == "819"
        assert clean_value("  x ") == "x"


class TestShortEntry:
    def test_short_entry_is_one_full_chunk(self):
        chunks = build_chunks(make_row(), lambda t: 250)
        assert len(chunks) == 1
        assert chunks[0]["section"] == "full"
        assert chunks[0]["text"] == "\n".join(
            [
                HEADER,
                f"Overview: {OVERVIEW}",
                f"Symptoms: {SYMPTOMS}",
                f"Conditions: {CONDITIONS}",
                f"Advisory: {ADVISORY}",
            ]
        )
        assert chunks[0]["text_for_embedding"] == chunks[0]["text"]


class TestLongEntry:
    def test_long_entry_splits_into_three_chunks_each_with_advice(self):
        chunks = build_chunks(make_row(), long_when_combined)
        assert [c["section"] for c in chunks] == ["overview", "symptoms", "conditions"]
        for chunk in chunks:
            assert chunk["text"].startswith(HEADER)
            assert chunk["text"].endswith(f"Advisory: {ADVISORY}")
        assert "Symptoms:" not in chunks[0]["text"]
        assert f"Symptoms: {SYMPTOMS}" in chunks[1]["text"]
        assert [c["chunk_num"] for c in chunks] == [0, 1, 2]

    def test_empty_conditions_gives_two_chunks(self):
        row = make_row()
        pop_of(row)["applicableCondition"] = []
        chunks = build_chunks(row, long_when_combined)
        assert [c["section"] for c in chunks] == ["overview", "symptoms"]

    def test_no_advisory_gives_one_chunk_per_field(self):
        row = make_row()
        pop_of(row)["advisory"] = None
        chunks = build_chunks(row, lambda t: 600)
        assert [c["section"] for c in chunks] == ["overview", "symptoms", "conditions"]
        for chunk in chunks:
            assert "Advisory:" not in chunk["text"]

    def test_only_advisory_is_one_advisory_chunk(self):
        row = make_row()
        pop = pop_of(row)
        pop["description"] = None
        pop["applicableSymptoms"] = []
        pop["applicableCondition"] = []
        chunks = build_chunks(row, lambda t: 600)
        assert [c["section"] for c in chunks] == ["advisory"]
        assert chunks[0]["text"] == f"{HEADER}\nAdvisory: {ADVISORY}"


class TestPublishingAndShape:
    @pytest.mark.parametrize(
        "overrides",
        [{"is_published": 0}, {"status": "Draft"}],
    )
    def test_unpublished_record_returns_empty(self, overrides):
        assert build_chunks(make_row(**overrides), lambda t: 250) == []

    def test_unpublished_entry_is_skipped(self):
        row = make_row()
        row["payload"]["popList"].append(
            {**copy.deepcopy(pop_of(row)), "popID": 820, "status": "Draft"}
        )
        chunks = build_chunks(row, lambda t: 250)
        assert [c["pop_id"] for c in chunks] == [819]

    @pytest.mark.parametrize("pop_list", [None, [], "nope"])
    def test_missing_or_empty_poplist_returns_empty(self, pop_list):
        row = make_row()
        row["payload"]["popList"] = pop_list
        assert build_chunks(row, lambda t: 250) == []

    def test_ids_are_stable_and_unique(self):
        first = build_chunks(make_row(), long_when_combined)
        second = build_chunks(make_row(), long_when_combined)
        assert [c["_id"] for c in first] == [c["_id"] for c in second]
        assert len({c["_id"] for c in first}) == 3
        full = build_chunks(make_row(), lambda t: 250)[0]
        expected = hashlib.md5(b"row-1:819:full").hexdigest()
        assert full["_id"] == expected
        assert full["chunk_id"] == expected

    def test_metadata_fields(self):
        chunk = build_chunks(make_row(), lambda t: 250)[0]
        assert chunk["document_kind"] == "advisory"
        assert chunk["type"] == "document"
        assert chunk["source"] == "webhook"
        assert chunk["workflow_id"] == ""
        assert chunk["doc_id"] == "row-1"
        assert chunk["external_id"] == "987"
        assert chunk["pop_id"] == 819
        assert chunk["title_en"] == "Sorghum- Harvesting Management"
        assert chunk["slug"] == "987/sorghum--harvesting-management"
        assert chunk["crop"] == "Sorghum"
        assert chunk["domain"] == "Agriculture"
        assert chunk["subdomain"] == "Cereals"
        assert chunk["institute"].startswith("ICAR")
        assert chunk["creator"] == "jak"
        assert chunk["problem_type"] == "Harvesting Management"
        assert chunk["problem"] == "Method Of Harvesting"
        assert chunk["season"] == "Rabi"
        assert chunk["start_period"] == "105"
        assert chunk["end_period"] == "120"
        assert chunk["updated_at"] == "2026-10-16T09:15:10.000Z"

    def test_null_values_never_appear_as_text(self):
        row = make_row()
        pop = pop_of(row)
        pop["description"] = None
        pop["season"] = None
        pop["applicableSymptoms"] = [None, "x"]
        chunks = build_chunks(row, lambda t: 250)
        assert len(chunks) == 1
        assert "None" not in chunks[0]["text"]
        assert "Season:" not in chunks[0]["text"]
        assert "Symptoms: x" in chunks[0]["text"]
