"""
Module: Processor Step 7 - Scoring Engine
Purpose: Applies the "5 Pillars" business logic to calculate a weighted
         performance score (0-100) for a sales meeting, then maps that score
         to a letter grade.

Algorithm
---------
Each of the 5 pillars receives a raw score (0-100) from upstream pipeline
steps (acoustic analysis + AI insights).  The engine multiplies every raw
score by its pillar weight, sums the weighted values to produce a final score,
and resolves that score to a letter grade using the defined grade boundaries.

Pillar weights (must sum to 1.0):
  - Discovery          0.30  (30 %)
  - Objection Handling 0.25  (25 %)
  - Talk Ratio         0.15  (15 %)
  - Next Steps         0.15  (15 %)
  - Closing            0.15  (15 %)

Grade boundaries:
  A+   95 – 100
  A    90 – 94.99
  B    80 – 89.99
  C    70 – 79.99
  D     0 – 69.99
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Final

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Pillar weights — must sum to exactly 1.0.
_WEIGHTS: Final[dict[str, float]] = {
    "discovery":          0.30,
    "objection_handling": 0.25,
    "talk_ratio":         0.15,
    "next_steps":         0.15,
    "closing":            0.15,
}

# Grade boundaries — evaluated top-down; first match wins.
_GRADE_BOUNDARIES: Final[list[tuple[float, str]]] = [
    (95.0, "A+"),
    (90.0, "A"),
    (80.0, "B"),
    (70.0, "C"),
    (0.0,  "D"),
]

# Valid raw-score range.
_SCORE_MIN: Final[float] = 0.0
_SCORE_MAX: Final[float] = 100.0


# ---------------------------------------------------------------------------
# Public data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PillarScores:
    """
    Validated raw scores (0-100) for each of the 5 pillars.

    All fields are immutable after construction to prevent accidental mutation
    downstream.
    """

    discovery:          float
    objection_handling: float
    talk_ratio:         float
    next_steps:         float
    closing:            float


@dataclass(frozen=True)
class ScoringResult:
    """
    Complete output of the scoring engine for a single sales meeting.

    Attributes:
        pillar_scores:       The validated raw (unweighted) input scores.
        weighted_scores:     Each pillar's contribution to the final total
                             (raw_score × weight), keyed by pillar name.
        final_score:         The weighted sum rounded to 2 decimal places.
        grade:               Letter grade derived from ``final_score``.
    """

    pillar_scores:   PillarScores
    weighted_scores: dict[str, float]
    final_score:     float
    grade:           str

    def to_dict(self) -> dict:
        """Serialise to a plain dictionary for storage or API responses."""
        return {
            "pillar_scores": {
                "discovery":          self.pillar_scores.discovery,
                "objection_handling": self.pillar_scores.objection_handling,
                "talk_ratio":         self.pillar_scores.talk_ratio,
                "next_steps":         self.pillar_scores.next_steps,
                "closing":            self.pillar_scores.closing,
            },
            "weighted_scores": self.weighted_scores,
            "final_score":     self.final_score,
            "grade":           self.grade,
        }


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class ScoringValidationError(ValueError):
    """Raised when a raw pillar score falls outside the 0-100 range."""


def _validate_score(pillar: str, value: float) -> float:
    """
    Ensure a raw score is a finite number within [0, 100].

    Args:
        pillar: Pillar name — used only to produce a meaningful error message.
        value:  The raw score to validate.

    Returns:
        The original ``value`` if valid.

    Raises:
        ScoringValidationError: If the value is out of range or not a real number.
    """
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ScoringValidationError(
            f"Pillar '{pillar}': expected a numeric score, got {value!r}."
        ) from exc

    if not (_SCORE_MIN <= value <= _SCORE_MAX):
        raise ScoringValidationError(
            f"Pillar '{pillar}': score {value} is out of bounds "
            f"[{_SCORE_MIN}, {_SCORE_MAX}]."
        )
    return value


# ---------------------------------------------------------------------------
# Core scoring logic
# ---------------------------------------------------------------------------

def _resolve_grade(final_score: float) -> str:
    """
    Map a final weighted score to its letter grade.

    Iterates the boundary list top-down so the highest threshold wins.

    Args:
        final_score: Weighted total score in [0, 100].

    Returns:
        Letter grade string (``"A+"``, ``"A"``, ``"B"``, ``"C"``, or ``"D"``).
    """
    for threshold, grade in _GRADE_BOUNDARIES:
        if final_score >= threshold:
            return grade
    return "D"  # Unreachable given _SCORE_MIN == 0, but satisfies the type checker.


def score(
    *,
    discovery:          float,
    objection_handling: float,
    talk_ratio:         float,
    next_steps:         float,
    closing:            float,
) -> ScoringResult:
    """
    Calculate the weighted performance score for a sales meeting.

    All arguments are keyword-only to prevent accidental positional mismatches
    between pillars.

    Args:
        discovery:          Raw discovery score (0-100).
        objection_handling: Raw objection-handling score (0-100).
        talk_ratio:         Raw talk-ratio score (0-100).
        next_steps:         Raw next-steps score (0-100).
        closing:            Raw closing score (0-100).

    Returns:
        A ``ScoringResult`` containing validated raw scores, individual weighted
        contributions, the final score, and the letter grade.

    Raises:
        ScoringValidationError: If any score is not a finite number in [0, 100].

    Example::

        result = score(
            discovery=80,
            objection_handling=70,
            talk_ratio=90,
            next_steps=60,
            closing=75,
        )
        print(result.final_score)  # → 75.25
        print(result.grade)        # → "C"
    """
    # --- Validate all inputs before touching any arithmetic --------------------
    raw: dict[str, float] = {
        "discovery":          _validate_score("discovery",          discovery),
        "objection_handling": _validate_score("objection_handling", objection_handling),
        "talk_ratio":         _validate_score("talk_ratio",         talk_ratio),
        "next_steps":         _validate_score("next_steps",         next_steps),
        "closing":            _validate_score("closing",            closing),
    }

    logger.debug("scoring_engine: raw scores  %s", raw)

    # --- Apply weights --------------------------------------------------------
    weighted: dict[str, float] = {
        pillar: round(raw[pillar] * weight, 4)
        for pillar, weight in _WEIGHTS.items()
    }

    # --- Aggregate final score ------------------------------------------------
    final_score = round(sum(weighted.values()), 2)

    # Guard against floating-point drift pushing the total just outside [0, 100].
    final_score = max(_SCORE_MIN, min(_SCORE_MAX, final_score))

    # --- Resolve grade --------------------------------------------------------
    grade = _resolve_grade(final_score)

    logger.info(
        "scoring_engine: final_score=%.2f  grade=%s  weighted=%s",
        final_score, grade, weighted,
    )

    return ScoringResult(
        pillar_scores=PillarScores(
            discovery=raw["discovery"],
            objection_handling=raw["objection_handling"],
            talk_ratio=raw["talk_ratio"],
            next_steps=raw["next_steps"],
            closing=raw["closing"],
        ),
        weighted_scores=weighted,
        final_score=final_score,
        grade=grade,
    )