"""Derive a business profile from its own website.

The onboarding wizard asks nine questions. Most of the answers are already
written on the business's home page, and the ones that are not — brand voice —
are better inferred from how they actually write than from a five-question quiz
nobody enjoys.

This produces a *draft*. It is never saved without the owner seeing it: the
model is reading marketing copy, which overstates, and a business whose site
says "award-winning" is not necessarily award-winning. Confirming a filled form
is a much shorter job than filling an empty one, which is the whole point.
"""
from __future__ import annotations

import logging

from ..pipeline.llm import call_json
from . import site

log = logging.getLogger(__name__)

# Matches the columns on `businesses`, plus the same brand_voice keys the
# onboarding quiz uses — so a drafted profile and a hand-filled one are the
# same shape and everything downstream is unchanged.
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "industry", "location", "offer_description",
                 "price_point", "brand_voice", "confidence"],
    "properties": {
        "name": {"type": "string"},
        "industry": {"type": "string"},
        "location": {"type": "string", "description": "City and country, or empty if the site never says"},
        "offer_description": {"type": "string"},
        "price_point": {"type": "string", "description": "Only if prices appear on the site; otherwise empty"},
        "brand_voice": {
            "type": "object",
            "additionalProperties": False,
            "required": ["tone", "energy", "humor", "authority", "detail"],
            "properties": {
                "tone": {"type": "string", "enum": ["Formal", "Conversational", "Casual"]},
                "energy": {"type": "string", "enum": ["Calm", "Warm", "Bold"]},
                "humor": {"type": "string", "enum": ["Serious", "Light touches", "Playful"]},
                "authority": {"type": "string", "enum": ["Expert", "Peer", "Guide"]},
                "detail": {"type": "string", "enum": ["Straight to the point", "Balanced", "Storyteller"]},
            },
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "low when the site was thin or mostly images",
        },
        "notes": {
            "type": "string",
            "description": "What could not be found, in one line, for the owner to fill in",
        },
    },
}

SYSTEM = """You are reading a small business's own website to fill in their profile.

Report only what the site actually says. If the site does not state a location, leave location empty — do not infer one from a phone code, a domain suffix, or a stock photo. If no prices appear, leave price_point empty. Guessing produces a profile the owner has to correct, which is worse than a gap they can fill.

Marketing copy overstates. Describe what they sell plainly, in the words a customer would use, not the site's own superlatives. Never carry across a claim of an award, a rating, a review or a customer count — those become campaign copy later, and a claim invented here becomes a claim published in their name.

brand_voice describes how this business already writes, judged from their copy. Pick the closest option in each case.

Set confidence to low when there was little text to go on, and use notes to say what you could not find.

Write in British English."""

USER = """## Website
{url}

## Page title
{title}

## Meta description
{description}

## Page text
{text}

Produce the business profile."""


def draft_from_url(raw_url: str) -> dict:
    """Read a website and return a profile draft plus what it was read from.

    Raises site.SiteError with a message meant for the owner when the site
    cannot be read — that path ends at the manual form, not at an error page.
    """
    page = site.read(raw_url)

    profile, cost, resolved = call_json(
        role="strategy",
        system=SYSTEM,
        user=USER.format(
            url=page["url"],
            title=page["title"] or "(none)",
            description=page["description"] or "(none)",
            text=page["text"],
        ),
        schema=SCHEMA,
        schema_name="business_profile",
    )

    log.info("[research] drafted %r from %s on %s — £%.4f",
             profile.get("name"), page["url"], resolved.model, cost)

    profile["website"] = page["url"]
    return {
        "profile": profile,
        "source": {"url": page["url"], "pages": page["pages"],
                   "characters": len(page["text"])},
        "cost_gbp": round(cost, 4),
    }
