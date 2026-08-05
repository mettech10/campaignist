# Campaignist

Turns one business goal into a complete, executable campaign — strategy,
audience, funnel, content, and 30-day calendar — in under five minutes, and
remembers what worked so the next campaign is better.

Full spec: [docs/BLUEPRINT.md](docs/BLUEPRINT.md).

## Layout

```
docs/       Blueprint v2.0 (markdown + original PDF)
web/        Next.js frontend — separate git repo (mettech10/professional-portfolio, v0-managed)
api/        Flask backend — deploys to Render
supabase/   SQL migrations + a local verification harness
```

`web/` is its own git checkout so v0 can keep pushing to it. The rest of this
tree is not a git repo yet.

## Running it

```bash
cd api && cp .env.example .env && ./.venv/bin/python wsgi.py
```

```bash
cd web && cp .env.example .env.local && pnpm dev
```

Tests:

```bash
cd api && ./.venv/bin/python -m pytest -q
```

The API needs Python 3.10+ (it uses `X | None` syntax). `api/.venv` is built
against Homebrew Python 3.12; macOS system Python is 3.8.

## Models

The pipeline asks for a **role**; config decides the model, and the model
decides the provider. The two roles can sit on **different providers**, which is
what makes a bake-off an env change rather than a refactor.

| Role | Default | Price /MTok | Used by |
|---|---|---|---|
| `MODEL_STRATEGY` | `kimi-k3` | $3.00 / $15.00 | Steps 1, 2, 3, 5, and written formats in step 4 |
| `MODEL_CONTENT` | `kimi-k2.5` | $0.60 / $3.00 | The step 4 content fan-out |

Two adapters cover the field (`api/app/pipeline/providers.py`):

* **`openai_compatible`** — Kimi/Moonshot, OpenAI, OpenRouter, and anything else
  speaking the chat-completions shape. Differs only by base URL and key.
* **`anthropic`** — Claude's Messages API, which uses `output_config.format`
  instead of `response_format`. The SDK is an optional import, so you only need
  it installed if a role points at a `claude-*` model.

`GET /api/health` reports what each role resolves to and whether its key is
present.

### Structured outputs replace the JSON-repair layer

The blueprint budgets Week 2 for "retry + JSON-repair handling". That isn't
needed: Kimi supports `response_format: {"type": "json_schema", …, "strict": true}`,
so the model cannot return malformed or off-schema JSON. `llm.py` keeps one
retry for transport errors and overloads, and has no repair path.

**This is a load-bearing assumption.** Older `moonshot-v1-*` models predate
strict schema support and would silently return unvalidated JSON, so `llm.py`
refuses any model not in its `MODELS` table, and a test asserts both configured
roles resolve to something on it.

It also constrains the prompts: every object in a prompt schema must set
`additionalProperties: false` and list `required`. A test enforces that across
all five specs, so a schema that would fail at runtime fails in CI instead.

## Database

Live on Supabase project `campaignist` (London / eu-west-2). Schema applied and
verified: 7 tables with RLS, `full_name`-only column grant on `profiles`,
`spend_credit` not executable by browser roles, signup trigger and pgvector
present.

Before applying any new migration:

```bash
cd /Users/preciousuchechukwumetu/ai-marketing-platform && ./supabase/verify_migration.sh
```

Spins up a throwaway Postgres 17, stubs what Supabase provides (`auth.users`,
`auth.uid()`, the `anon`/`authenticated` roles, default grants), applies every
migration, and asserts the security properties RLS alone does not give you.
Needs `brew install postgresql@17 pgvector`.

### Two bugs that harness caught

Both were in `0001_init.sql`, both in code whose comment claimed the opposite,
and neither would have failed at apply time — they would have shipped.

**1. Users could grant themselves the Pro plan and unlimited credits.** The
`profiles_update` policy restricts *which row* a user may update; an RLS policy
cannot restrict *which columns*. Any user could `PATCH /profiles?id=eq.<self>`
through PostgREST with `{"plan":"pro","credits":9999}`. Fixed with column-level
privileges: `revoke update on profiles`, then `grant update (full_name)`.

**2. Any user could drain another user's credits.** `spend_credit` is
`SECURITY DEFINER` and takes a `user_id`, so execute permission on it is
permission to spend anyone's credits. Postgres grants `EXECUTE` to `PUBLIC` by
default and roles inherit it, so revoking from `anon, authenticated` alone left
it callable. Fixed by revoking from `PUBLIC` too.

## Creative formats

`web/lib/content-formats.ts` and `api/app/pipeline/formats.py` define one
taxonomy of 14 formats — UGC testimonial and demo, founder piece, b-roll,
product and lifestyle shots, anime/illustrated, flat lay, before/after, quote
card, carousel, meme, long-form email, landing block. Step 3 picks formats per
funnel stage; step 4 writes copy and an art-direction brief in the chosen
format's idiom.

The two files must stay in sync — the frontend renders labels and aspect ratios
from whatever id the backend stores. `test_format_catalogue_matches_the_frontend_taxonomy`
fails if they drift.

**Scope note:** Campaignist writes the copy and the shot brief. It does not
render video or images — the brief is what you shoot from, hand to a creator, or
feed to an image generator. The blueprint defers image generation to Phase 2,
and every user-facing surface says so.

## Status

| Week | Scope | State |
|---|---|---|
| 1 | Schema + RLS, auth port, Stripe port, skeleton UI | Done |
| 2 | Prompt specs 1–3, pipeline runner, per-step persistence | Done |
| 3 | Step 4 fan-out, step 5 assembly, per-asset regenerate, cost logging | Done |
| 4 | Frontend wired to the API, auth screens | Done |
| 5 | Export bundle, retrospective, memory write | Not started |
| 6 | Landing polish, pricing gates, beta users | Not started |

Step 4 fans out one call per (funnel stage × chosen format) across a 6-worker
pool, routing written formats to the strategy model and everything else to the
cheaper content model. A failed leg is logged and skipped rather than losing the
whole run; the step only fails if every leg fails. Step 5 schedules only assets
that actually exist — a calendar entry naming an unknown `asset_id` is dropped
rather than rendering as a dead link.

The generation screen polls `GET /api/campaigns/<id>` and counts the steps the
API has persisted into `pipeline_outputs`, so a slow step sits visibly at
"working" rather than advancing on a timer.

**Nothing has run against a live model API yet.** That needs `KIMI_API_KEY` in
`api/.env` and is the first thing to do.

## Deploying

**Render** hosts the Flask API. `render.yaml` sits at the repo root (Render only
reads Blueprints from there) with `rootDir: api`.

| Setting | Value |
|---|---|
| Root directory | `api` |
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn wsgi:app --workers 2 --threads 8 --timeout 120` |
| Health check | `/api/health` |

`--threads 8` is load-bearing, not tuning: it switches gunicorn from the `sync`
worker to `gthread`. Generation runs in a background thread and step 4 fans out
over a thread pool, so a single-threaded worker would serialise the whole
pipeline. `--timeout 120` covers the slowest single model call; the generation
request itself returns 202 immediately and the frontend polls.

Two workers is safe because no pipeline state lives in process memory — a poll
that lands on the other worker reads the same row from Supabase.

Environment variables, split by where they are safe:

* **Render** (all secret): `KIMI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`,
  `MODEL_STRATEGY`, `MODEL_CONTENT`, `STRIPE_SECRET_KEY`,
  `STRIPE_WEBHOOK_SECRET`, `CORS_ORIGINS` (your Vercel domain).
* **Vercel** (all publishable): `NEXT_PUBLIC_SUPABASE_URL`,
  `NEXT_PUBLIC_SUPABASE_ANON_KEY`, `NEXT_PUBLIC_API_URL` (the Render URL).

The service-role and Kimi keys must never go in Vercel — anything prefixed
`NEXT_PUBLIC_` is compiled into the browser bundle.

`GET /api/health` reports what each pipeline role resolved to and whether its
key is present, so check that first after a deploy.

The API runs on Render's **starter** plan ($7/mo). Free was rejected because
free instances spin down after ~15 minutes idle, and a generation runs in a
background thread that returns 202 immediately — a spin-down mid-run would
strand the campaign in `generating` with the credit already spent.

That failure mode still exists on starter, just far more rarely: a deploy during
a generation does the same thing. There is no reaper for it yet — see Open
decisions.

**pnpm note for `web/`:** Vercel runs pnpm 10, pinned via `packageManager` in
package.json. Install with `corepack pnpm@10.18.0 install`, never bare `pnpm` —
a newer local pnpm silently drops the lockfile's `overrides` block and breaks
the deploy.

## Choosing a model — the golden set

`api/evals/` is the blueprint's §11 mitigation ("golden-set of 10 test
businesses scored weekly") and the instrument for deciding which provider to
use. It runs the real prompts against the real API but touches no database.

```bash
cd api && ./.venv/bin/python evals/run_eval.py --label kimi
```

For a bake-off, point the roles at a different pairing and run it again:

```bash
cd api && MODEL_STRATEGY=claude-sonnet-5 MODEL_CONTENT=claude-haiku-4-5 ./.venv/bin/python evals/run_eval.py --label claude
```

Output lands in `evals/runs/<label>/` — one JSON per business plus a summary of
mean cost, mean and max latency, and rule hits, so two runs can be read side by
side. `--only <id>` and `--steps 3` give a fast loop while editing a prompt.

The ten businesses deliberately include the awkward cases: a £0 budget, a
near-empty input, a 14-day timeframe, a health-adjacent brief where outcome
claims are risky, and exactly one playful brand voice (the only brief where
`meme-overlay` should be eligible).

The automated checks catch what is objectively wrong, not what is good:
fabricated social proof and ratings, banned marketing clichés, US spellings,
budget splits that do not sum to 100, format ids outside the taxonomy, calendar
entries beyond the timeframe or pointing at assets that do not exist, and
`meme-overlay` chosen for a non-playful brand. **Judging whether the copy is any
good is still yours** — the checks exist so you spend that attention on craft
rather than on hunting broken references.

## Open decisions

1. **Embedding provider.** The blueprint specifies `vector(1536)` but names no
   provider. Kimi does not expose an embeddings endpoint, so the memory loop
   needs a separate one — OpenAI `text-embedding-3-small` is 1536 and drops in;
   Voyage `voyage-3` is 1024 and would need a migration. Must be settled before
   Week 5, because changing the dimension after data exists means re-embedding
   everything.
2. **Repo name.** `mettech10/professional-portfolio` and `mettech10/campaignist`
   hold the same v0 scaffold. The product is called Campaignist everywhere, so
   the former is the misnomer — but it is the one v0 pushes to and the one
   `web/` tracks.
3. **Stuck generations.** A campaign whose worker died mid-run stays
   `generating` with no timeout and no refund. Cheapest fix is a stale-campaign
   check on the polling GET: if `status = generating` and `created_at` is older
   than a few minutes with no new `pipeline_outputs` key, mark it `error` and
   return the credit. Rare on starter (deploys only), but needed before beta
   users since it currently costs them a credit with no recovery.
4. **Backend repo.** `api/` is untracked. `render.yaml` sets `rootDir: api`, so
   either a monorepo or its own repo works.
