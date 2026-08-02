from epub_scraper.entity_resolution import NUCandidate, Resolution, resolve, score_candidate


def test_score_candidate_matches_on_associated_name_not_display_title():
    # Display title is a generic re-translation; the real match lives in the
    # associated-names list, which is exactly the point of scoring against it.
    candidate = NUCandidate(title="Some Generic Retitling",
                             url="https://nu/series/x",
                             associated_names=["Reverend Insanity", "Xin Xi Lu"])
    score = score_candidate("Reverend Insanity", None, candidate)
    assert score >= 95


def test_score_candidate_uses_alt_title_when_main_title_diverges():
    candidate = NUCandidate(title="Reverend Insanity", url="https://nu/series/x",
                             associated_names=["脑抽的） Reverend Insanity"])
    score_main_only = score_candidate("Totally Different MTL Title", None, candidate)
    score_with_alt = score_candidate("Totally Different MTL Title", "Reverend Insanity", candidate)
    assert score_with_alt > score_main_only


def test_resolve_no_candidates_returns_no_candidates_decision():
    result = resolve("Some Title", None, [])
    assert result == Resolution("no_candidates", None, None, None)


def test_resolve_clear_winner_is_auto():
    good = NUCandidate("Reverend Insanity", "https://nu/1", ["Reverend Insanity"])
    bad = NUCandidate("Completely Unrelated Novel", "https://nu/2", ["Completely Unrelated Novel"])
    result = resolve("Reverend Insanity", None, [good, bad])
    assert result.decision == "auto"
    assert result.best == good


def test_resolve_close_scores_are_ambiguous():
    a = NUCandidate("Reverend Insanity Book One", "https://nu/1", ["Reverend Insanity Book One"])
    b = NUCandidate("Reverend Insanity Book Two", "https://nu/2", ["Reverend Insanity Book Two"])
    result = resolve("Reverend Insanity", None, [a, b])
    assert result.decision == "ambiguous"


def test_resolve_low_scores_are_no_candidates():
    candidate = NUCandidate("Totally Unrelated", "https://nu/1", ["Totally Unrelated"])
    result = resolve("Reverend Insanity", None, [candidate])
    assert result.decision == "no_candidates"
