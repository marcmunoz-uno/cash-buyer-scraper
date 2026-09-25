"""Buyer-name normalization must collapse every rendering of the same LLC to
one key, or one buyer splits into several entities (inflated buyer list +
diluted velocity)."""

from __future__ import annotations

from cash_buyer_intel.db import normalize_buyer_name


def test_spaced_llc_collapses_to_same_key_as_solid_and_dotted():
    solid = normalize_buyer_name("SMITH HOLDINGS LLC")
    dotted = normalize_buyer_name("Smith Holdings, L.L.C.")
    spaced = normalize_buyer_name("SMITH HOLDINGS L L C")
    assert solid == "SMITH HOLDINGS"
    assert dotted == solid
    # Regression: the spaced single-letter rendering common in deed records
    # used to survive as "SMITH HOLDINGS L L C" and split the entity.
    assert spaced == solid


def test_spaced_llc_on_individual_name():
    assert normalize_buyer_name("JOHN SMITH L L C") == normalize_buyer_name("JOHN SMITH LLC")


def test_does_not_over_strip_a_middle_initial_before_a_c_surname():
    # "BILL L CARTER" must NOT be read as a spaced LLC.
    assert normalize_buyer_name("BILL L CARTER") == "BILL L CARTER"
