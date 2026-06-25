"""Comparable scoring.

`score_comparable(base, candidate)` decides whether a market listing is a fair
comparison for the user's item. It returns a score (0-100), the reasons points
were awarded, and hard rejection reasons. Hard rejections force the candidate
out regardless of score - this is what stops a 5m upgraded pet being compared
to a 40k junk pet.

Calibration: a candidate that genuinely matches on the value-defining traits
clears the default threshold (75); anything that differs on a value-defining
trait is hard-rejected so it can never set the price.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from .config import settings
from .features import RARITY_ORDER

# Awarded once when the candidate shares the base's tag and category (pet/gear).
BASE_MATCH = 25


@dataclass
class ScoreResult:
    score: int
    reasons: List[str] = field(default_factory=list)
    rejections: List[str] = field(default_factory=list)
    accepted: bool = False


def _rarity_rank(rarity) -> int:
    if not rarity:
        return -1
    try:
        return RARITY_ORDER.index(str(rarity).upper())
    except ValueError:
        return -1


def _overlap(base, cand) -> float:
    """Jaccard overlap of two iterables of names (0-1)."""
    bset, cset = set(base or []), set(cand or [])
    if not bset and not cset:
        return 1.0
    if not bset or not cset:
        return 0.0
    return len(bset & cset) / len(bset | cset)


def _attr_similarity(base: Dict[str, int], cand: Dict[str, int]) -> float:
    """How well candidate attributes match base (0-1). Candidate should carry
    the base's attributes at similar-or-higher level to score well."""
    if not base:
        return 1.0
    if not cand:
        return 0.0
    matched = 0.0
    for name, blevel in base.items():
        clevel = cand.get(name)
        if clevel is None:
            continue
        if clevel >= blevel:
            matched += 1.0
        elif clevel >= blevel - 1:
            matched += 0.6
        else:
            matched += 0.3
    return matched / len(base)


def _finish(score: int, reasons, rejections) -> ScoreResult:
    score = max(0, min(100, score))
    accepted = score >= settings.relist_min_comparable_score and not rejections
    if not accepted and not rejections:
        rejections.append(f"Score {score} below threshold {settings.relist_min_comparable_score}")
    return ScoreResult(score=score, reasons=reasons, rejections=rejections, accepted=accepted)


def score_comparable(base_features: Dict[str, Any], candidate_features: Dict[str, Any]) -> ScoreResult:
    """Score a candidate listing against the base item."""
    reasons: List[str] = []
    rejections: List[str] = []
    score = 0

    base = base_features or {}
    cand = candidate_features or {}

    # --- Hard requirement: same item tag ---
    if not base.get("item_tag") or base.get("item_tag") != cand.get("item_tag"):
        return ScoreResult(0, reasons, ["Different item tag"], False)

    # Don't compare an item to itself.
    if base.get("uuid") and base.get("uuid") == cand.get("uuid"):
        return ScoreResult(0, reasons, ["Same auction (self)"], False)

    # =====================================================================
    # PETS
    # =====================================================================
    if base.get("is_pet"):
        if not cand.get("is_pet"):
            return ScoreResult(0, reasons, ["Candidate is not a pet"], False)

        bp = base.get("pet") or {}
        cp = cand.get("pet") or {}
        score += BASE_MATCH
        reasons.append("Same pet type")

        # Hard: pet tier/rarity must match (Legendary != Epic).
        btier = bp.get("tier") or base.get("rarity")
        ctier = cp.get("tier") or cand.get("rarity")
        if btier and ctier and btier != ctier:
            return ScoreResult(0, reasons, [f"Different pet tier ({btier} vs {ctier})"], False)
        if btier and ctier:
            score += 25
            reasons.append(f"Same pet tier ({btier})")

        # Hard: pet level within tolerance.
        blevel, clevel = bp.get("level"), cp.get("level")
        tol = settings.relist_pet_level_tolerance
        if blevel is not None and clevel is not None:
            if abs(blevel - clevel) > tol:
                return ScoreResult(0, reasons, [f"Pet level too different ({blevel} vs {clevel})"], False)
            score += 25
            reasons.append(f"Pet level close ({blevel} vs {clevel})")
        else:
            reasons.append("Pet level unknown (reduced confidence)")

        # Held item / skin shift value.
        if bp.get("held_item") or cp.get("held_item"):
            if bp.get("held_item") == cp.get("held_item"):
                score += 10
                reasons.append("Same held item")
            else:
                score -= 8
                reasons.append("Different held item")
        if bp.get("skin") or cp.get("skin"):
            if bp.get("skin") == cp.get("skin"):
                score += 4
                reasons.append("Same pet skin")
            else:
                return ScoreResult(0, reasons, ["Different pet skin (changes value)"], False)

    # =====================================================================
    # GEAR (armour / weapons / tools / accessories)
    # =====================================================================
    else:
        if cand.get("is_pet"):
            return ScoreResult(0, reasons, ["Candidate is a pet, base is not"], False)

        score += BASE_MATCH
        reasons.append("Same item base")

        # Rarity / tier.
        if base.get("rarity") and cand.get("rarity"):
            if base["rarity"] == cand["rarity"]:
                score += 25
                reasons.append(f"Same rarity ({base['rarity']})")
            elif abs(_rarity_rank(base["rarity"]) - _rarity_rank(cand["rarity"])) <= 1:
                score += 8
                reasons.append("Rarity within one step")
            else:
                return ScoreResult(0, reasons, [f"Rarity too different ({base['rarity']} vs {cand['rarity']})"], False)

        # Stars (dungeon/upgrade) - hard within tolerance.
        bstars, cstars = int(base.get("stars") or 0), int(cand.get("stars") or 0)
        if bstars or cstars:
            if abs(bstars - cstars) > settings.relist_star_tolerance:
                return ScoreResult(0, reasons, [f"Star level differs ({bstars}* vs {cstars}*)"], False)
            score += 20
            reasons.append(f"Same stars ({bstars})")
        else:
            score += 15
            reasons.append("Both clean (no stars)")

        # Attributes - very important for attribute gear.
        battrs = base.get("attributes") or {}
        cattrs = cand.get("attributes") or {}
        if battrs:
            sim = _attr_similarity(battrs, cattrs)
            if sim < 0.5:
                return ScoreResult(0, reasons, ["Missing/weak matching attributes"], False)
            score += int(round(25 * sim))
            reasons.append(f"Attributes match ~{int(sim * 100)}%")
        elif cattrs:
            return ScoreResult(0, reasons, ["Candidate has attributes base lacks (higher quality)"], False)

        # Gemstones - never compare gem'd vs clean.
        bgems, cgems = base.get("gemstones") or {}, cand.get("gemstones") or {}
        bhas, chas = bool(bgems.get("has_gems")), bool(cgems.get("has_gems"))
        if bhas != chas:
            return ScoreResult(0, reasons, ["Gemstone mismatch (one gem'd, one clean)"], False)
        if bhas and chas:
            ov = _overlap(bgems.get("qualities"), cgems.get("qualities"))
            if ov >= settings.relist_gemstone_tolerance:
                score += 20
                reasons.append("Gemstones similar")
            else:
                score += 8
                reasons.append("Gemstones roughly similar")

    # =====================================================================
    # SHARED TRAITS (pets + gear)
    # =====================================================================

    # Recombobulator.
    if bool(base.get("recombobulated")) == bool(cand.get("recombobulated")):
        score += 15
        reasons.append("Recomb status matches")
    elif not base.get("is_pet"):
        return ScoreResult(0, reasons, ["Recomb mismatch"], False)
    else:
        score -= 8
        reasons.append("Recomb mismatch (pet)")

    # Reforge.
    if base.get("reforge") and cand.get("reforge") and base["reforge"] == cand["reforge"]:
        score += 8
        reasons.append("Same reforge")

    # Important enchants - key value driver for gear.
    benc = base.get("important_enchants") or {}
    cenc = cand.get("important_enchants") or {}
    if benc:
        ov = _overlap(benc.keys(), cenc.keys())
        if ov < 0.5:
            return ScoreResult(0, reasons, ["Missing key enchants present on base"], False)
        score += int(round(15 * ov))
        reasons.append("Key enchants match" if ov >= 0.75 else "Most key enchants match")
    elif cenc:
        return ScoreResult(0, reasons, ["Candidate has extra key enchants (higher quality)"], False)

    # Hot potato books (gear only, minor).
    if not base.get("is_pet"):
        bhp = (base.get("hot_potato") or {}).get("total", 0)
        chp = (cand.get("hot_potato") or {}).get("total", 0)
        if abs(bhp - chp) <= 2:
            score += 3
            if bhp or chp:
                reasons.append("Similar potato books")

    return _finish(score, reasons, rejections)
