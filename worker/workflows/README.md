# Workflows

One ComfyUI graph per creative format, in **API format** — the shape you get from
ComfyUI's *Save (API Format)*, not the editor's save. `default.json` is the
fallback, so an unknown format degrades to a generic render instead of failing
the job.

Adding a format is dropping in `<format-id>.json`. No code change, the same way
adding an agent is dropping in a spec.

## What these two assume you have installed

| Requirement | Where it comes from |
|---|---|
| ComfyUI | Pinokio installs it |
| **ComfyUI-VideoHelperSuite** | needed for `VHS_VideoCombine` → mp4 |
| An SDXL checkpoint | `sd_xl_base_1.0.safetensors` |
| An SVD checkpoint | `svd_xt.safetensors` |

`SVD_img2vid_Conditioning`, `VideoLinearCFGGuidance` and
`ImageOnlyCheckpointLoader` are core ComfyUI — no custom nodes needed for those.

**The two `ckpt_name` values must match the filenames actually in
`ComfyUI/models/checkpoints/`.** That is the single most likely reason a first
render fails, and ComfyUI's error says so plainly when it does.

The mp4 format matters too: the storage bucket only accepts `video/mp4` and
`video/webm`, so `VHS_VideoCombine` must stay on `video/h264-mp4`. Core's
`SaveAnimatedWEBP` emits `image/webp` and would be rejected on upload.

## The shape of the graph

Text is all we have — the brief is words, not a picture — so this is two stages:

```
SDXL txt2img  →  one keyframe  →  SVD img2vid  →  25 frames  →  mp4
```

`b-roll-montage.json` is the default with more camera movement
(`motion_bucket_id` 150 vs 110) and slower playback, because b-roll is
atmosphere rather than a subject.

## Placeholders

Substituted before the graph is sent. A placeholder works in two positions:

| Written as | Becomes |
|---|---|
| `"__WIDTH__"` (whole value) | a JSON number, e.g. `576` |
| `"...text __PROMPT__ more"` (embedded) | escaped and spliced into the string |

That distinction is load-bearing. Briefs are model-written prose and routinely
contain double quotes — a real one read *Founder says "we don't do brochure
weddings"*. Spliced in raw that ends the JSON string early and the graph will
not parse, so embedded values are escaped. There is a test for it.

| Token | Value |
|---|---|
| `__PROMPT__` | shot brief + format guidance |
| `__HOOK__` | the asset's hook line |
| `__WIDTH__` / `__HEIGHT__` | from aspect ratio — 9:16 → 576×1024, 1:1 → 768×768, 16:9 → 1024×576 |
| `__SEED__` | fresh per render |

## Replacing these with your own

Build a graph in ComfyUI until it renders something you actually like, then
*Save (API Format)* and swap the tuned values for placeholders. Keep the node
that writes the video producing mp4, and keep its `save_output` true — the
worker reads the finished file out of the history response's `gifs`/`videos`
outputs, which is where VideoHelperSuite puts it.

## Status

**These two have never rendered a frame.** They were written from the node
contracts, not from a working install, so treat them as a starting point rather
than a known-good config. The substitution layer around them *is* tested. The
first real run will tell us the rest — most likely the checkpoint filenames.
