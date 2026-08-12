from app.number_words import amount_in_words, number_to_words


def test_matches_sample_pp_doc_amount_exactly():
    # Real string from the sample PP.doc for the Awan Tech firm total.
    assert (
        amount_in_words(249138.12)
        == "Pak Rupees Two Hundred Forty Nine Thousand One Hundred Thirty Eight and Paisa Twelve Only"
    )


def test_zero_paisa_omits_paisa_clause():
    assert amount_in_words(1000.0) == "Pak Rupees One Thousand Only"


def test_rounding_carries_into_rupees():
    assert amount_in_words(999.999) == "Pak Rupees One Thousand Only"


def test_number_to_words_basic_cases():
    assert number_to_words(0) == "Zero"
    assert number_to_words(19) == "Nineteen"
    assert number_to_words(100) == "One Hundred"
    assert number_to_words(2026) == "Two Thousand Twenty Six"
    assert number_to_words(1_000_000) == "One Million"
