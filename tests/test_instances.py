"""Tests for instance code → state name resolution."""

import pytest


class TestInstanceDisplayName:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "code,expected",
        [
            ("bv", "Bharat Vistaar"),
            ("BV", "Bharat Vistaar"),
            ("mh", "Maharashtra"),
            ("ka", "Karnataka"),
            ("up", "Uttar Pradesh"),
            ("gj", "Gujarat"),
            ("tn", "Tamil Nadu"),
            ("wb", "West Bengal"),
            ("dl", "Delhi"),
            ("jk", "Jammu and Kashmir"),
            ("default", "Default"),
        ],
    )
    def test_known_codes(self, code, expected):
        from pipeline.instances import instance_display_name

        assert instance_display_name(code) == expected

    @pytest.mark.unit
    @pytest.mark.parametrize("code,expected", [("or", "Odisha"), ("od", "Odisha"), ("ts", "Telangana"), ("tg", "Telangana")])
    def test_legacy_code_aliases(self, code, expected):
        from pipeline.instances import instance_display_name

        assert instance_display_name(code) == expected

    @pytest.mark.unit
    def test_blank_falls_back_to_default(self):
        from pipeline.instances import instance_display_name

        assert instance_display_name("") == "Default"
        assert instance_display_name(None) == "Default"

    @pytest.mark.unit
    def test_unknown_code_is_made_presentable(self):
        """A new state must degrade to a readable label, never break ingestion."""
        from pipeline.instances import instance_display_name

        assert instance_display_name("new-state") == "New State"
