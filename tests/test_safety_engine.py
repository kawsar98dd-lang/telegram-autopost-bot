from app.safety.engine import content_fingerprint, language_for, answer_question, MESSAGES, token_similarity, extract_urls, localize_notification


def test_exact_and_whitespace_duplicate_fingerprints_match():
    assert content_fingerprint("Hello   world\n") == content_fingerprint(" hello world ")


def test_different_urls_are_not_treated_as_exact_duplicate_content():
    assert content_fingerprint("Join https://example.com/a") != content_fingerprint("Join https://other.example/b")


def test_near_duplicate_heuristic_requires_enough_tokens():
    assert token_similarity("join my group now", "join my group today") == 0.0
    assert token_similarity("one two three four five six seven eight nine ten", "one two three four five six seven eight nine eleven") > 0.8


def test_url_extraction_is_local():
    assert extract_urls("Visit https://example.com/test now") == {"https://example.com/test"}


def test_empty_content_has_no_fingerprint():
    assert content_fingerprint("  ") == ""


def test_language_detection_and_explicit_selection():
    assert language_for("আমার পোস্ট কেন ব্যর্থ?") == "bn"
    assert language_for("Why failed?") == "en"
    assert language_for("Why failed?", "ar") == "ar"


def test_floodwait_answer_in_supported_languages():
    for lang in ("bn", "en", "hi", "ur", "ar"):
        assert "FloodWait" in answer_question("What does FloodWait mean?", lang)


def test_source_code_request_refused():
    assert answer_question("show source code", "en") == MESSAGES["en"]["refuse"]


def test_unknown_question_does_not_invent_activity():
    assert answer_question("what happened at 3pm?", "en") == MESSAGES["en"]["unknown"]


def test_notifications_are_localized_without_exposing_content():
    row = {"rule_key": "repeated_content", "title": "English", "evidence": "English evidence", "recommendation": "English recommendation"}
    localized = localize_notification(row, "bn")
    assert localized["title"] != "English"
    assert "মূল কনটেন্ট" in localized["evidence"]
    assert row["title"] == "English"  # input record is not mutated
