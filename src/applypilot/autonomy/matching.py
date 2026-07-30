"""Deterministic role-family matching and human-readable fit ranking.

The matching layer intentionally runs before any model-authored materials.  It
uses explicit role families, early-career signals, location preferences, and
confirmed applicant skills.  Generic words such as ``analyst`` or ``associate``
are never sufficient admission signals on their own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from applypilot.autonomy.models import CandidateProfile, FreshnessEvidence, RoleCandidate


ROLE_FAMILY_MARKERS: dict[str, tuple[str, ...]] = {
    "ai_product": (
        "ai product",
        "artificial intelligence product",
        "machine learning product",
        "product management",
        "product manager",
        "product development",
        "product operations",
        "product owner",
        "product analyst",
        "product strategy",
    ),
    "data_analytics": (
        "data analyst",
        "data analytics",
        "business analytics",
        "digital analytics",
        "analytics intern",
        "analytics internship",
        "business intelligence",
        "data science",
        "insights analyst",
    ),
    "growth_analytics": (
        "growth analytics",
        "growth analyst",
        "marketing analytics",
        "marketing analyst",
        "marketing leadership development",
        "revenue analytics",
        "lifecycle analytics",
        "go to market analytics",
    ),
    "technical_business": (
        "business analyst",
        "business analysts",
        "technology analyst",
        "technical business",
        "commercial strategy",
        "commercial growth",
        "turnaround restructuring",
        "restructuring advisory",
        "strategy intern",
        "strategy internship",
        "strategy and operations",
        "strategy operations",
        "business operations",
        "operations finance",
        "operations analyst",
        "operations coordination",
        "program analyst",
        "corporate analyst development",
        "agile intern",
    ),
    "venture": (
        "venture capital",
        "venture analyst",
        "venture intern",
        "portfolio operations",
        "startup analyst",
        "investor relations",
        "investment banking analyst internship",
    ),
    "seo_analytics": (
        "seo analytics",
        "seo analyst",
        "search engine optimization",
        "organic search",
        "search analytics",
    ),
}

TARGET_FAMILY_HINTS: dict[str, tuple[str, ...]] = {
    "ai_product": ("ai product", "product", "product management"),
    "data_analytics": ("data", "analytics", "business analytics", "data science"),
    "growth_analytics": ("growth", "marketing analytics", "revenue analytics"),
    "technical_business": (
        "technical business",
        "business analyst",
        "strategy",
        "operations",
        "program",
    ),
    "venture": ("venture", "startup", "portfolio"),
    "seo_analytics": ("seo", "search engine optimization", "organic search"),
}

MAJOR_US_MARKET_MARKERS = (
    "atlanta",
    "austin",
    "boston",
    "chicago",
    "dallas",
    "denver",
    "houston",
    "jersey city",
    "los angeles",
    "miami",
    "new york",
    "philadelphia",
    "san francisco",
    "seattle",
    "washington dc",
)

EARLY_CAREER_MARKERS = (
    "intern",
    "internship",
    "co op",
    "apprentice",
    "apprenticeship",
    "new grad",
    "new graduate",
    "early career",
    "summer analyst",
    "summer associate",
    "development program",
    "university program",
    "student program",
)

SENIOR_TITLE_MARKERS = (
    "senior",
    "staff",
    "principal",
    "manager",
    "director",
    "vice president",
    "vp",
    "head of",
    "lead",
)

OUT_OF_SCOPE_TITLE_MARKERS = (
    "accounting",
    "audit",
    "banking",
    "commercial",
    "corporate credit",
    "equity research",
    "finance",
    "investment banking",
    "lending",
    "real estate investment trust",
    "risk technology",
    "sales",
    "sreit",
    "tax",
    "underwriting",
    "wealth management",
)

DEGREE_FAMILY_MARKERS: dict[str, tuple[str, ...]] = {
    "technical": (
        "computer science",
        "computer engineering",
        "information systems",
        "information technology",
        "software engineering",
        "data science",
        "electrical engineering",
    ),
    "business": (
        "business administration",
        "business management",
        "school of business",
        "economics",
        "finance",
        "marketing",
    ),
    "quantitative": (
        "mathematics",
        "statistics",
        "operations research",
        "physics",
    ),
}

NO_SPONSORSHIP_PATTERNS = (
    r"\b(?:unable|not able) to (?:consider|hire|employ|sponsor) candidates? (?:who|that) (?:will )?require visa sponsorship\b",
    r"\b(?:visa|employment) sponsorship (?:is|will be) (?:not available|unavailable|not provided)\b",
    r"\b(?:do|does) not (?:offer|provide) (?:visa|employment) sponsorship\b",
    r"\bno (?:visa|employment) sponsorship\b",
    r"\bmust not require (?:visa|employment) sponsorship\b",
)
WORK_AUTHORIZATION_PATTERNS = (
    r"\bmust be (?:legally )?authorized to work (?:in|within) (?:the )?united states\b",
    r"\b(?:legally )?authorized to work in (?:the )?u\.?s\.?(?: without restriction)?\b",
    r"\bproof of (?:legal )?authorization to work\b",
)
AGE_18_PATTERNS = (
    r"\bmust (?:be|at least be) (?:at least )?18 years? (?:old|of age)\b",
    r"\bat least 18 years? (?:old|of age)\b",
    r"\bage requirement\s*:?\s*must (?:at least )?be 18\b",
)


@dataclass(frozen=True)
class FitAssessment:
    """Bounded deterministic score plus displayable decision evidence."""

    score: int
    matched_families: tuple[str, ...]
    matched_skills: tuple[str, ...]
    inclusion_reasons: tuple[str, ...]
    exclusion_reasons: tuple[str, ...]

    @property
    def qualifies(self) -> bool:
        return not self.exclusion_reasons and self.score >= 70


def infer_target_families(text: str) -> tuple[str, ...]:
    """Map an applicant objective to explicit supported role families."""
    normalized = normalize(text)
    families = [
        family
        for family, markers in TARGET_FAMILY_HINTS.items()
        if any(phrase_present(normalized, marker) for marker in markers)
    ]
    return tuple(families)


def classify_role(candidate: RoleCandidate) -> tuple[str, ...]:
    """Return only families supported by specific title/description phrases."""
    text = normalize(f"{candidate.title} {candidate.description}")
    return tuple(
        family
        for family, markers in ROLE_FAMILY_MARKERS.items()
        if any(phrase_present(text, marker) for marker in markers)
    )


def early_career_signal(candidate: RoleCandidate) -> bool:
    text = normalize(f"{candidate.title} {candidate.description}")
    return any(phrase_present(text, marker) for marker in EARLY_CAREER_MARKERS)


def senior_title_signal(candidate: RoleCandidate) -> str:
    title = normalize(candidate.title)
    for marker in SENIOR_TITLE_MARKERS:
        if not phrase_present(title, marker):
            continue
        if (
            marker == "manager"
            and early_career_signal(candidate)
            and (
                phrase_present(title, "product manager")
                or phrase_present(title, "product management")
            )
        ):
            continue
        return marker
    return ""


def out_of_scope_title_signal(candidate: RoleCandidate) -> str:
    title = normalize(candidate.title)
    # The exceptions below are narrow, approved internship routes.  Generic
    # finance and banking analyst titles remain out of scope.
    if phrase_present(title, "operations finance") or phrase_present(
        title, "investment banking analyst internship"
    ):
        return ""
    return next(
        (marker for marker in OUT_OF_SCOPE_TITLE_MARKERS if phrase_present(title, marker)),
        "",
    )


def location_preference_match(
    location: str,
    preferred_locations: Iterable[str],
) -> tuple[bool | None, str]:
    """Return true/false/unknown and the matching normalized preference."""
    normalized_location = normalize_location(location)
    if not normalized_location:
        return None, ""
    normalized_preferences = tuple(
        normalized
        for preferred in preferred_locations
        if (normalized := normalize_location(preferred))
    )
    if not normalized_preferences:
        return None, ""
    for normalized_preferred in normalized_preferences:
        if phrase_present(normalized_location, normalized_preferred):
            return True, normalized_preferred
        if (
            phrase_present(normalized_preferred, "elsewhere in texas")
            and phrase_present(normalized_location, "texas")
        ):
            return True, normalized_preferred
        if (
            (
                phrase_present(normalized_preferred, "major united states markets")
                or phrase_present(normalized_preferred, "major u s markets")
            )
            and any(
                phrase_present(normalized_location, market)
                for market in MAJOR_US_MARKET_MARKERS
            )
        ):
            return True, normalized_preferred
    return False, ""


def education_requirement_match(
    candidate: RoleCandidate,
    profile: CandidateProfile,
) -> tuple[bool | None, tuple[str, ...]]:
    """Match only explicit required-major families; unknown stays reviewable."""
    text = normalize(candidate.description)
    required: set[str] = set()
    for family, markers in DEGREE_FAMILY_MARKERS.items():
        for marker in markers:
            start = 0
            while True:
                index = text.find(normalize(marker), start)
                if index < 0:
                    break
                window = text[max(0, index - 180) : index + len(marker) + 180]
                if re.search(
                    r"\b(?:degree|major|course of study|pursuing|pursue|required|qualification)\b",
                    window,
                ):
                    required.add(family)
                    break
                start = index + len(marker)
    if not required:
        return True, ()

    education = normalize(" ".join(profile.education_evidence))
    candidate_families = {
        family
        for family, markers in DEGREE_FAMILY_MARKERS.items()
        if any(phrase_present(education, marker) for marker in markers)
    }
    if re.search(r"\bbba\b", education):
        candidate_families.add("business")
    if not profile.education_evidence:
        return None, tuple(sorted(required))
    return bool(required & candidate_families), tuple(sorted(required))


def explicit_applicant_requirement_decision(
    candidate: RoleCandidate,
    profile: CandidateProfile,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return reject reasons, review reasons, and posting evidence for explicit facts."""
    text = normalize(f"{candidate.title} {candidate.description}")
    rejects: list[str] = []
    reviews: list[str] = []
    evidence: list[str] = []

    no_sponsorship = _matches_any(text, NO_SPONSORSHIP_PATTERNS)
    if no_sponsorship:
        evidence.append("posting explicitly disallows sponsorship")
        if profile.require_sponsorship is True:
            rejects.append("sponsorship_requirement_incompatible")
        elif profile.require_sponsorship is None:
            reviews.append("sponsorship_status_unconfirmed")

    requires_authorization = _matches_any(text, WORK_AUTHORIZATION_PATTERNS)
    if requires_authorization:
        evidence.append("posting explicitly requires work authorization")
        if profile.legally_authorized_to_work is False:
            rejects.append("work_authorization_incompatible")
        elif profile.legally_authorized_to_work is None:
            reviews.append("work_authorization_unconfirmed")

    requires_adult = _matches_any(text, AGE_18_PATTERNS)
    if requires_adult:
        evidence.append("posting explicitly requires age 18 or older")
        if profile.is_at_least_18 is False:
            rejects.append("age_requirement_incompatible")
        elif profile.is_at_least_18 is None:
            reviews.append("age_18_status_unconfirmed")

    return tuple(rejects), tuple(reviews), tuple(evidence)


def assess_fit(
    candidate: RoleCandidate,
    evidence: FreshnessEvidence,
    profile: CandidateProfile,
) -> FitAssessment:
    """Score a verified candidate on transparent, non-model components."""
    role_families = classify_role(candidate)
    matched_families = tuple(
        family for family in role_families if family in profile.target_families
    )
    inclusion: list[str] = []
    exclusion: list[str] = []
    score = 0

    if matched_families:
        score += 40
        inclusion.append("target role family: " + ", ".join(matched_families))
    else:
        exclusion.append("no supported target role family")

    if early_career_signal(candidate):
        score += 20
        inclusion.append("explicit internship or early-career signal")
    else:
        exclusion.append("no internship or early-career signal")

    location_match, matched_location = location_preference_match(
        candidate.location,
        profile.preferred_locations,
    )
    if location_match is True:
        score += 15
        inclusion.append(f"preferred location: {matched_location}")
    elif location_match is False:
        exclusion.append(f"location outside preferences: {candidate.location}")
    else:
        exclusion.append("location not verified")

    job_tokens = token_set(f"{candidate.title} {evidence.description or candidate.description}")
    matched_skills = tuple(
        sorted(
            skill
            for skill in profile.skills
            if skill_tokens(skill) and skill_tokens(skill).issubset(job_tokens)
        )
    )[:8]
    if matched_skills:
        score += min(15, 5 + len(matched_skills) * 2)
        inclusion.append("confirmed skill overlap: " + ", ".join(matched_skills))

    if evidence.first_party and evidence.resolved and evidence.open_state is True:
        score += 10
        inclusion.append("first-party posting rendered open")
    else:
        exclusion.append("first-party open state not verified")

    senior = senior_title_signal(candidate)
    if senior:
        exclusion.append(f"senior title marker: {senior}")
    out_of_scope = out_of_scope_title_signal(candidate)
    if out_of_scope:
        exclusion.append(f"out-of-scope function: {out_of_scope}")

    return FitAssessment(
        score=min(score, 100),
        matched_families=matched_families,
        matched_skills=matched_skills,
        inclusion_reasons=tuple(inclusion),
        exclusion_reasons=tuple(dict.fromkeys(exclusion)),
    )


def preliminary_fit_score(candidate: RoleCandidate, profile: CandidateProfile) -> int:
    """Rank discovery candidates before spending first-party verification calls."""
    families = classify_role(candidate)
    score = 40 if set(families) & set(profile.target_families) else 0
    if early_career_signal(candidate):
        score += 25
    location_match, _ = location_preference_match(candidate.location, profile.preferred_locations)
    if location_match is True:
        score += 20
    if candidate.posted_date is not None:
        score += 5
    if candidate.description:
        score += 10
    return score


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def normalize_location(value: str) -> str:
    normalized = normalize(value)
    aliases = {
        "ca": "california",
        "il": "illinois",
        "nyc": "new york",
        "ny": "new york",
        "sf": "san francisco",
        "tx": "texas",
        "us": "united states",
        "usa": "united states",
    }
    return " ".join(aliases.get(token, token) for token in normalized.split())


def phrase_present(haystack: str, needle: str) -> bool:
    normalized = normalize(needle)
    return bool(normalized and re.search(rf"\b{re.escape(normalized)}\b", haystack))


def token_set(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9][a-z0-9+#.-]*", normalize(value)))


def skill_tokens(value: str) -> set[str]:
    return token_set(value)


def _matches_any(normalized_text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, normalized_text) for pattern in patterns)
