from typing import NamedTuple, Optional

from rapidfuzz import fuzz

# Tuned to the cascade agreed in the Classification Data Spine story: a clear
# winner auto-accepts, a close field needs a tiebreak (synopsis-embedding
# similarity or manual/LLM adjudication -- not built yet, this module only
# produces the decision, not the tiebreak itself), and nothing worth calling
# a match gets flagged as such rather than forced into a low-confidence pick.
AUTO_ACCEPT_MIN_SCORE = 90.0
AUTO_ACCEPT_MARGIN = 15.0
AMBIGUOUS_MIN_SCORE = 60.0


class NUCandidate(NamedTuple):
    """Minimal shape of one Novel Updates search result, as needed for entity
    resolution -- not the full NU series-page scrape (that scraper doesn't
    exist yet; this module is built ahead of it so it's ready to wire in)."""
    title: str
    url: str
    associated_names: list


class Resolution(NamedTuple):
    decision: str  # "auto" | "ambiguous" | "no_candidates"
    best: Optional[NUCandidate]
    best_score: Optional[float]
    runner_up_score: Optional[float]


def score_candidate(fanmtl_title, fanmtl_alt_title, nu_candidate):
    """Best RapidFuzz token_set_ratio between either FanMTL title (main or
    alternate/original-script) and any of the NU candidate's associated
    names -- the associated-names list is where romanization/subtitle
    variants live, not the display title alone."""
    fanmtl_titles = [t for t in (fanmtl_title, fanmtl_alt_title) if t]
    names = nu_candidate.associated_names or [nu_candidate.title]
    return max(
        fuzz.token_set_ratio(ft, name)
        for ft in fanmtl_titles
        for name in names
    )


def resolve(fanmtl_title, fanmtl_alt_title, nu_candidates):
    """Score every NU candidate against the FanMTL novel and decide: "auto"
    (clear best match), "ambiguous" (multiple close scores -- needs a
    tiebreak or manual/LLM adjudication), or "no_candidates" (nothing scored
    high enough to be a real match, or the search returned nothing)."""
    if not nu_candidates:
        return Resolution("no_candidates", None, None, None)

    scored = sorted(
        ((score_candidate(fanmtl_title, fanmtl_alt_title, c), c) for c in nu_candidates),
        key=lambda pair: pair[0], reverse=True,
    )
    best_score, best = scored[0]
    runner_up_score = scored[1][0] if len(scored) > 1 else None

    if best_score < AMBIGUOUS_MIN_SCORE:
        return Resolution("no_candidates", None, best_score, runner_up_score)

    clear_margin = runner_up_score is None or (best_score - runner_up_score) >= AUTO_ACCEPT_MARGIN
    if best_score >= AUTO_ACCEPT_MIN_SCORE and clear_margin:
        return Resolution("auto", best, best_score, runner_up_score)

    return Resolution("ambiguous", best, best_score, runner_up_score)
