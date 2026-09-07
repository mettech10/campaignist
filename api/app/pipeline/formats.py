"""Creative format taxonomy — backend mirror of web/lib/content-formats.ts.

Step 3 chooses which formats a funnel stage needs; Step 4 writes copy and an
art-direction brief in the chosen format's idiom. Keep the ids in sync with the
frontend file: the frontend renders the label and aspect ratio from the id the
backend stores on content_assets.format.

Video production modes (Metalyzi cost model):
  * adapt — default for market-pattern work. Pull a TikTok *pattern* (oEmbed
    metadata), then ffmpeg-edit an owned/uploaded source to match. Cheap.
  * generate — fal text-to-video. Reserved for product-specific UGC and
    motion-design. Do not use as the default for every vertical clip.
"""

# Video guidance describes ONE continuous clip of roughly six seconds, because
# that is now what happens to it: these formats are rendered by a text-to-video
# model, not handed to a crew. The guidance used to ask for beat sheets,
# timecodes, cuts and 30-60 second takes, which is direction for a person with
# an editor — and the first real render showed the cost, a still-life read of a
# brief that said "lifestyle photography style, soft focus background" and named
# no motion at all.
#
# It is also better direction for a small business owner filming on a phone. An
# eight-shot montage they will never cut is worth less than one shot they can
# actually get.
FORMATS: dict[str, dict] = {
    # ── video ───────────────────────────────────────────────────────────────
    "ugc-testimonial": {
        "label": "UGC Testimonial",
        "category": "video",
        "production": "generate",
        "aspect_ratio": "9:16",
        "brief_guidance": (
            "One continuous shot of about six seconds, described as a single "
            "moment. Handheld phone framing, available light, one person "
            "talking straight to camera. Say what moves in frame and what the "
            "camera does — holds still, drifts a little. No cuts, no "
            "timecodes, no beat sheet: this is rendered as one clip and there "
            "is no editor. The speaker must sound like a customer, never like "
            "ad copy. Do not invent a named person or a quote attributed to a "
            "real customer — describe the person generically."
        ),
    },
    "ugc-demo": {
        "label": "UGC Demo / Unboxing",
        "category": "video",
        "production": "generate",
        "aspect_ratio": "9:16",
        "brief_guidance": (
            "One continuous shot of about six seconds. Hands-only or "
            "over-the-shoulder, with a single action starting and finishing on "
            "camera. Say what the hands do, what the product does, and what "
            "the camera does. No cuts — one clip, one action, ending on the "
            "product doing the thing that was promised."
        ),
    },
    "founder-piece": {
        "label": "Founder Piece to Camera",
        "category": "video",
        # Prefer adapting the owner's own phone footage to a market pattern.
        "production": "adapt",
        "aspect_ratio": "9:16 or 1:1",
        "brief_guidance": (
            "One continuous shot of about six seconds: the owner, static "
            "camera or a slow push in. Say where they are, what is behind "
            "them, what they do with their hands, and whether they look at the "
            "lens. Open on the claim, not a greeting. Six seconds is one "
            "sentence, so pick the sentence."
        ),
    },
    "b-roll-montage": {
        "label": "B-Roll Montage",
        "category": "video",
        "production": "adapt",
        "aspect_ratio": "9:16",
        "brief_guidance": (
            "Six to eight shots at ~2s each, numbered and in order, plus grade "
            "and music direction. Each numbered shot is rendered on its own, "
            "so each must stand alone as one continuous moment with its own "
            "subject, motion and camera direction — no shot may rely on the "
            "one before it. Name the beat the hero shot cuts to."
        ),
    },
    "slideshow": {
        "label": "Slideshow / Photo cards",
        "category": "video",
        "production": "remix",
        "aspect_ratio": "9:16",
        "brief_guidance": (
            "A sequence of 4–7 full-screen text cards (TikTok Photo Mode energy). "
            "Each card is one short line: hook, points, then CTA. No live action. "
            "This is the cheapest volume format — rebuilt from a market pattern, "
            "not generated frame-by-frame by a video model."
        ),
    },
    "hook-demo": {
        "label": "Hook + Demo",
        "category": "video",
        "production": "remix",
        "aspect_ratio": "9:16",
        "brief_guidance": (
            "Open on a bold text hook for ~2 seconds, then cut to one continuous "
            "owned product/demo clip (~8–12s), ending on a clear CTA. The demo "
            "footage must be the business's own (phone, screen recording, or "
            "licensed stock) — never another creator's TikTok file."
        ),
    },
    "meme-video": {
        "label": "Meme Video",
        "category": "video",
        "production": "remix",
        "aspect_ratio": "9:16",
        "brief_guidance": (
            "A short POV / Nobody: style meme. Top and bottom captions only, "
            "optional owned background still. Rebuild from a market pattern with "
            "the business product name — never another creator's video file."
        ),
    },
    "motion-design": {


        "label": "Motion Design",
        "category": "video",
        "production": "generate",
        "aspect_ratio": "9:16",
        "brief_guidance": (
            "One continuous motion-graphics clip of about six to eight seconds. "
            "Describe kinetic typography, product UI, or abstract brand shapes "
            "moving on a clean background — not a live-action person. Name the "
            "palette, the type treatment, what enters and exits frame, and the "
            "final end-card with the product name. No cuts: one rendered take."
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
# fal text-to-video is only for these. Everything else adapts a real source.
GENERATE = [k for k, v in FORMATS.items()
            if v["category"] == "video" and v.get("production") == "generate"]
ADAPT = [k for k, v in FORMATS.items()
         if v["category"] == "video" and v.get("production") == "adapt"]
REMIX = [k for k, v in FORMATS.items()
         if v["category"] == "video" and v.get("production") == "remix"]


def catalogue_for_prompt() -> str:
    """Compact catalogue injected into the Step 3 and Step 4 prompts."""
    lines = []
    for fid, f in FORMATS.items():
        prod = f.get("production")
        prod_bit = f", {prod}" if prod else ""
        lines.append(
            f"- {fid} ({f['category']}{prod_bit}, {f['aspect_ratio']}): {f['label']}"
        )
    return "\n".join(lines)


def guidance(format_id: str) -> str:
    entry = FORMATS.get(format_id)
    return entry["brief_guidance"] if entry else ""
