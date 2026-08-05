"""Creative format taxonomy — backend mirror of web/lib/content-formats.ts.

Step 3 chooses which formats a funnel stage needs; Step 4 writes copy and an
art-direction brief in the chosen format's idiom. Keep the ids in sync with the
frontend file: the frontend renders the label and aspect ratio from the id the
backend stores on content_assets.format.

Campaignist writes the brief; it does not render video or images.
"""

FORMATS: dict[str, dict] = {
    # ── video ───────────────────────────────────────────────────────────────
    "ugc-testimonial": {
        "label": "UGC Testimonial",
        "category": "video",
        "aspect_ratio": "9:16",
        "brief_guidance": (
            "Write a shot-by-shot beat sheet with timecodes. Specify handheld phone "
            "framing, available light, and where the cut lands. The speaker must sound "
            "like a customer, never like ad copy. Do not invent a named person or a "
            "quote attributed to a real customer — describe the person generically."
        ),
    },
    "ugc-demo": {
        "label": "UGC Demo / Unboxing",
        "category": "video",
        "aspect_ratio": "9:16",
        "brief_guidance": (
            "Hands-only or over-the-shoulder. Beat sheet with a cut on each action. "
            "Under 20 seconds. End on the product doing the thing that was promised."
        ),
    },
    "founder-piece": {
        "label": "Founder Piece to Camera",
        "category": "video",
        "aspect_ratio": "9:16 or 1:1",
        "brief_guidance": (
            "A cue card the owner can read. Open on the claim, not a greeting. One "
            "take, 30-60 seconds, no jump cuts."
        ),
    },
    "b-roll-montage": {
        "label": "B-Roll Montage",
        "category": "video",
        "aspect_ratio": "9:16",
        "brief_guidance": (
            "Six to eight shots at ~2s each, listed in order, plus grade and music "
            "direction. Name the beat the hero shot cuts to."
        ),
    },
    # ── image ───────────────────────────────────────────────────────────────
    "product-shot": {
        "label": "Product Shot",
        "category": "image",
        "aspect_ratio": "1:1 or 4:5",
        "brief_guidance": (
            "Specify subject placement, backdrop, lighting direction and quality, "
            "camera height, and which region of frame stays empty for overlay text."
        ),
    },
    "lifestyle-shot": {
        "label": "Lifestyle Shot",
        "category": "image",
        "aspect_ratio": "4:5",
        "brief_guidance": (
            "Product in real use. Describe the moment, not a pose. Name the light, "
            "the background treatment, and where the headline sits."
        ),
    },
    "anime-illustrated": {
        "label": "Anime / Illustrated",
        "category": "image",
        "aspect_ratio": "1:1 or 9:16",
        "brief_guidance": (
            "Name the illustration style, shading, and palette explicitly. Describe "
            "the character's action and mood. Do not reference a living artist, a "
            "studio, or a named franchise by name — describe the visual qualities."
        ),
    },
    "flat-lay": {
        "label": "Flat Lay",
        "category": "image",
        "aspect_ratio": "1:1",
        "brief_guidance": (
            "Straight-down. List the objects and their arrangement, the surface, the "
            "light direction, and where negative space is reserved."
        ),
    },
    "before-after": {
        "label": "Before & After",
        "category": "image",
        "aspect_ratio": "1:1",
        "brief_guidance": (
            "Split frame with matched framing on both sides. Only claim a difference "
            "the business can actually evidence."
        ),
    },
    "quote-card": {
        "label": "Quote Card",
        "category": "image",
        "aspect_ratio": "1:1",
        "brief_guidance": (
            "Typography only. Specify background, typeface feel, and setting. Leave "
            "the quote itself as a placeholder for the user to paste a real review "
            "into — never write an invented customer quote."
        ),
    },
    "infographic": {
        "label": "Infographic / Carousel",
        "category": "image",
        "aspect_ratio": "4:5",
        "brief_guidance": (
            "Slide-by-slide: the copy for each slide plus one line of layout "
            "direction. Keep the grid identical across slides."
        ),
    },
    "meme-overlay": {
        "label": "Meme / Text Overlay",
        "category": "image",
        "aspect_ratio": "1:1",
        "brief_guidance": (
            "Panel structure and caption per panel. Only select this format when "
            "brand_voice humour is 'Playful'."
        ),
    },
    # ── text ────────────────────────────────────────────────────────────────
    "long-form-email": {
        "label": "Long-Form Email",
        "category": "text",
        "aspect_ratio": "—",
        "brief_guidance": (
            "Subject line under 42 characters. Open on a specific moment. One idea "
            "per paragraph. Single CTA. Only {{first_name}} as a merge tag."
        ),
    },
    "landing-block": {
        "label": "Landing Page Block",
        "category": "text",
        "aspect_ratio": "—",
        "brief_guidance": (
            "H1 under 9 words carrying the outcome. Subhead names the audience and "
            "the price floor. Objections written as the customer's own questions."
        ),
    },
}

VIDEO = [k for k, v in FORMATS.items() if v["category"] == "video"]
IMAGE = [k for k, v in FORMATS.items() if v["category"] == "image"]
TEXT = [k for k, v in FORMATS.items() if v["category"] == "text"]


def catalogue_for_prompt() -> str:
    """Compact catalogue injected into the Step 3 and Step 4 prompts."""
    lines = []
    for fid, f in FORMATS.items():
        lines.append(f"- {fid} ({f['category']}, {f['aspect_ratio']}): {f['label']}")
    return "\n".join(lines)


def guidance(format_id: str) -> str:
    entry = FORMATS.get(format_id)
    return entry["brief_guidance"] if entry else ""
