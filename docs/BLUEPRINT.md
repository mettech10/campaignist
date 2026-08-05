# AI Marketing Platform — Buildable Blueprint v2.0

**Working name:** (TBD) · **Author:** Precious / Metusa Property Ltd · **Date:** August 2026
**Status:** Pre-build blueprint — replaces "Project Plan & MVP Design v1"
**Source:** `ai-marketing-platform-blueprint.pdf` (same folder)

---

## 1. Product Overview

### 1.1 One-liner
An AI marketing operating system for small businesses and solo founders that turns a single business goal into a complete, executable campaign — strategy, audience, funnel, content, and channel plan — in under 5 minutes.

### 1.2 The problem
Small businesses don't fail at marketing because tools are missing. They fail because:

1. **Strategy is the bottleneck, not content.** ChatGPT can write a post; it can't tell you *which* campaign to run, in what order, for what budget.
2. **Tools are fragmented.** Strategy lives in someone's head, content in Canva, scheduling in Buffer, analytics in five dashboards. Nothing closes the loop.
3. **No memory or compounding.** Every campaign starts from zero. Nothing learns what worked last time.

### 1.3 The wedge (what the MVP actually is)
The MVP is **not** an autonomous agent swarm. It is a **Campaign Generator with a feedback memory**:

> Goal in → structured campaign plan + ready-to-publish content out → user reports results → system remembers and improves the next campaign.

Autonomy (auto-publishing, self-optimising agents) is Phase 3+, once the generator proves people will pay for the output.

### 1.4 Target user
- **Primary:** UK solo founders & micro-businesses (1–10 staff) doing their own marketing. No marketing hire, £0–500/mo ad budget.
- **Secondary (later):** freelance marketers/agencies running campaigns for multiple clients (unlocks the Pro tier).

### 1.5 Positioning vs. competitors

| Competitor | What they do | Gap you exploit |
|---|---|---|
| Jasper / Copy.ai | Content generation | No strategy layer, no campaign structure, no memory |
| HubSpot / Mailchimp AI | Channel execution | Heavy, expensive, assumes you already have a strategy |
| Ocoya / Predis | Social scheduling + AI captions | Post-level, not campaign-level thinking |
| Agencies | Full service | £1,500+/mo minimum — you're the £49 alternative |

**Positioning statement:** "The marketing director you can't afford, for £49/month."

---

## 2. Scope Decisions (what v1 got wrong)

| v1 said | v2 decision | Why |
|---|---|---|
| 5 agents at MVP | 1 orchestrated pipeline, agent-shaped later | Agents multiply failure modes; a pipeline is debuggable and 80% of the value |
| FastAPI backend | **Flask** (or FastAPI if starting clean) | You already run Flask in production on Metalyzi — reuse auth, Stripe, credit-system patterns |
| Pinecone vector DB | **Supabase pgvector** | Already planned for BA Copilot; one DB, no extra vendor, free tier |
| OpenAI API | **Claude API** | Deep prompt-engineering experience with Claude; structured JSON output patterns already proven in Metalyzi |
| Redis cache at MVP | Skip until needed | Premature; Supabase + HTTP caching covers MVP load |
| LangGraph | Skip at MVP | A sequenced set of Claude calls with JSON contracts is simpler and fully controllable; adopt LangGraph only if Phase 3 agents genuinely need graph state |
| "Distribution Layer" at MVP | Export + copy-to-clipboard + .zip download | OAuth to Meta/LinkedIn/X is weeks of work and app-review pain; defer to Phase 2 |

**Guiding principle:** ship the smallest thing someone will pay £49/month for, on the stack you already know.

---

## 3. System Architecture (MVP)

```
┌──────────────────────────────────────────────────────────┐
│  Next.js 14 (App Router) + Tailwind — Vercel             │
│  Auth UI · Onboarding · Campaign Wizard · Dashboard      │
└──────────────────────────────────────────────────────────┘
                      │ REST (JSON)
                      ▼
┌──────────────────────────────────────────────────────────┐
│  Flask API — Render                                      │
│  /auth  /business  /campaigns  /content  /results        │
│  Campaign Pipeline (5 sequenced Claude calls)            │
│  Stripe webhooks · Credit system (port Metalyzi's)       │
└──────────────────────────────────────────────────────────┘
         │                          │
         ▼                          ▼
┌──────────────────┐   ┌────────────────────────────────────┐
│  Claude API      │   │  Supabase (Postgres + pgvector)    │
│  Sonnet for      │   │  Users · Businesses · Campaigns    │
│  strategy;       │   │  ContentAssets · Results ·         │
│  Haiku for       │   │  MemoryEmbeddings · Credits        │
│  short copy      │   │  Storage: exported asset bundles   │
└──────────────────┘   └────────────────────────────────────┘
```

**Reuse from Metalyzi (direct ports, ~2 weeks saved):**
- Auth + email verification + welcome flow
- Stripe subscription + credit system + post-payment redirect
- Admin dashboard skeleton
- "AI result card" rendering patterns and JSON-contract prompt style
- Legal/compliance page templates

---

## 4. The Campaign Pipeline (replaces "Agent System" at MVP)

One user request triggers a **sequenced pipeline of 5 Claude calls**, each with a strict JSON output contract. Each step's output feeds the next. Store every intermediate output (for debugging, regeneration of a single step, and future fine-tuning of prompts).

### Step 1 — Business Understanding
- **Input:** onboarding data (industry, offer, price point, location, brand voice, past campaign memory if any)
- **Output contract:** `{ business_summary, customer_pain_points[], differentiators[], constraints[] }`
- **Model:** Sonnet

### Step 2 — Audience & Positioning
- **Input:** Step 1 output + user goal
- **Output contract:** `{ segments[]: { name, demographics, psychographics, watering_holes[], objections[] }, primary_segment, positioning_statement }`
- **Model:** Sonnet

### Step 3 — Funnel & Channel Strategy
- **Input:** Steps 1–2 + budget + timeframe
- **Output contract:** `{ funnel_stages[]: { stage, objective, channel, content_types[], kpi, weekly_cadence }, budget_split{}, expected_outcomes }`
- **Model:** Sonnet

### Step 4 — Content Generation (fan-out)
- **Input:** Step 3's content requirements, brand voice
- **Output contract per asset:** `{ type, channel, hook, body, cta, hashtags[], image_brief, variant_count }`
- Generate 2–3 variants per asset. **Model:** Haiku for short copy (cost control), Sonnet for long-form (email sequences, landing page copy).
- MVP asset types: social posts (IG/FB/LinkedIn/X), ad copy (Meta + Google), 3-email nurture sequence, 1 landing-page copy block.

### Step 5 — Campaign Assembly & Calendar
- **Input:** everything above
- **Output contract:** `{ campaign_name, summary, 30_day_calendar[]: { date, channel, asset_id, action }, launch_checklist[], success_metrics[] }`
- **Model:** Sonnet

### Memory loop (the moat)
After a campaign runs, the user logs results (manual at MVP: reach, clicks, leads, sales — a 60-second form). Then:

1. A Claude call produces a **campaign retrospective**: `{ what_worked[], what_failed[], hypotheses[], next_campaign_adjustments[] }`
2. Retrospective + campaign summary is embedded (pgvector) into the business's **memory store**.
3. Step 1 of every future campaign retrieves the top-k relevant memories → **campaigns visibly improve over time**. This is the retention mechanic and the demo moment for investors.

**Cost estimate per full campaign generation:** ~£0.15–0.40 in Claude tokens (Sonnet strategy + Haiku fan-out). At £49/mo with ~10 campaigns/mo, gross margin stays >90%.

---

## 5. Database Schema (Supabase)

```sql
-- users handled by Supabase Auth; profile extension:
profiles        (id uuid PK → auth.users, full_name, plan text, credits int,
                 stripe_customer_id, created_at)

businesses      (id uuid PK, user_id FK, name, industry, offer_description,
                 price_point, location, brand_voice jsonb, website, created_at)

campaigns       (id uuid PK, business_id FK, goal text, budget numeric,
                 timeframe_days int, status text
                   CHECK (status IN ('draft','generating','ready','live','completed')),
                 pipeline_outputs jsonb,   -- all 5 step outputs, keyed by step
                 calendar jsonb, created_at, completed_at)

content_assets  (id uuid PK, campaign_id FK, type text, channel text,
                 variant int, content jsonb, image_brief text,
                 status text CHECK (status IN ('generated','edited','approved','exported')),
                 edited_content jsonb, created_at)

campaign_results(id uuid PK, campaign_id FK, reported_at, reach int,
                 clicks int, leads int, sales int, revenue numeric,
                 user_notes text)

memories        (id uuid PK, business_id FK, source_campaign_id FK,
                 kind text CHECK (kind IN ('retrospective','preference','fact')),
                 content text, embedding vector(1536), created_at)

credit_ledger   (id uuid PK, user_id FK, delta int, reason text,
                 campaign_id FK NULL, created_at)  -- port from Metalyzi
```

**Row-Level Security:** every table scoped to `user_id` via business ownership — same RLS pattern as Metalyzi.

---

## 6. API Endpoints (Flask)

```
POST  /api/auth/*                      (port from Metalyzi)
POST  /api/business                    create/update business profile
GET   /api/business/:id

POST  /api/campaigns                   start generation → returns campaign_id, status
GET   /api/campaigns/:id               poll status + fetch outputs
POST  /api/campaigns/:id/regenerate    body: { step: 1-5 }  regenerate one step onward
POST  /api/campaigns/:id/approve
GET   /api/campaigns?business_id=

GET   /api/campaigns/:id/assets
PATCH /api/assets/:id                  save user edits
POST  /api/assets/:id/regenerate       one-asset regen (cheap Haiku call)
GET   /api/campaigns/:id/export        .zip of assets + calendar CSV + PDF summary

POST  /api/campaigns/:id/results       log results → triggers retrospective + memory write
GET   /api/business/:id/memories       "What the AI has learned about your business"

POST  /api/stripe/webhook              (port from Metalyzi)
```

**Generation is async:** enqueue on POST, frontend polls `GET /campaigns/:id` (Metalyzi pattern). Steps stream into `pipeline_outputs` as they finish so the UI can reveal progress step-by-step — great perceived speed.

---

## 7. MVP Screens (8 total)

1. **Onboarding wizard** (3 steps): business basics → offer & pricing → brand voice quiz (5 questions, generates `brand_voice` jsonb)
2. **Dashboard:** campaign list, credits remaining, "New Campaign" CTA, memory count ("Your AI has learned 12 things about your business")
3. **New Campaign:** single form — goal (free text), budget, timeframe. One button.
4. **Generation view:** 5-step progress reveal (Step 1 done ✓ → Step 2 generating…) — makes the AI's thinking visible, builds trust
5. **Campaign view:** strategy summary, audience card, funnel diagram, 30-day calendar
6. **Asset editor:** asset list with variants, inline editing, per-asset regenerate, approve toggle
7. **Export:** copy buttons per asset + download bundle (.zip: txt files, calendar .csv, campaign summary .pdf)
8. **Results & retrospective:** log-results form → AI retrospective card → "applied to future campaigns" confirmation

---

## 8. Build Plan (6 weeks, solo, Claude Code)

Follows the staged pattern: audit → implement → log → verify → summarise per section.

### Week 1 — Foundation
- Repo setup, Supabase schema + RLS, port Metalyzi auth + Stripe + credits
- Ticket size: schema (1d), auth port (2d), Stripe port (1d), skeleton UI (1d)

### Week 2 — Pipeline core
- Prompt templates for Steps 1–3 as JSON files (BA Copilot pattern), pipeline runner with per-step persistence, retry + JSON-repair handling
- Verification: 5 test businesses through Steps 1–3, manually score outputs

### Week 3 — Content engine
- Step 4 fan-out (Haiku), Step 5 assembly, variant generation, cost logging per campaign
- Verification: full pipeline < 90s, cost per run < £0.40

### Week 4 — Frontend
- Screens 1–6, generation progress view, asset editor
- Verification: full user journey clickable end-to-end

### Week 5 — Export, results, memory
- Export bundle, results form, retrospective call, pgvector memory write + retrieval into Step 1
- Verification: second campaign for same business demonstrably references first campaign's retrospective

### Week 6 — Polish & launch
- Landing page, pricing gates, email flows (port), legal pages (port), 10 beta users from property/founder network
- Launch channels: existing social audience, Indie Hackers, UK founder communities

---

## 9. Monetisation

| Tier | Price | Limits | Target user |
|---|---|---|---|
| Starter | £49/mo | 1 business, 5 campaigns/mo, 50 assets | Solo founder |
| Growth | £149/mo | 3 businesses, 20 campaigns/mo, unlimited assets, memory insights | Small business / power user |
| Pro | £299/mo | 10 businesses (client workspaces), white-label exports, priority generation | Freelancers & micro-agencies |

- **Free trial:** 1 full campaign, no card (the generation view sells itself)
- **Credit overage:** buy extra campaign credits (reuse Metalyzi credit system)
- **Anchor metric for pricing page:** "vs. £1,500/mo for a freelance marketer"

**Break-even maths:** ~£300/mo infra (Render + Supabase + Vercel + tokens) → 7 Starter customers. 50 customers ≈ £3,500 MRR.

---

## 10. Roadmap Beyond MVP

- **Phase 2 (weeks 7–12) — Distribution:** Meta + LinkedIn OAuth publishing, Buffer/Zapier integration as the shortcut, scheduled posting from the calendar, image generation for the `image_brief` fields.
- **Phase 3 (months 4–6) — Analytics & agents:** pull real performance data via channel APIs (replaces manual results form), convert pipeline steps into monitored agents with an optimisation loop, A/B test tracking on variants.
- **Phase 4 (months 6+) — Compounding moat:** cross-customer anonymised benchmarks ("businesses like yours see 2.1% CTR" — the Metalyzi Benchmark Database playbook applied to marketing), agency multi-seat, API access.

---

## 11. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Output quality inconsistent | JSON contracts + per-step regeneration + golden-set of 10 test businesses scored weekly |
| "ChatGPT can do this" objection | Memory loop + structured campaign (not chat) + calendar/export UX — sell the *system*, not the text |
| Token costs blow up | Haiku for fan-out, per-campaign cost logging from day 1, hard credit caps |
| Channel API rejection (Phase 2) | Buffer/Zapier integration as fallback distribution |
| Solo-founder bandwidth vs. Metalyzi + BA Copilot | 6-week timebox; if no paying beta user by week 8, park it — this plan is designed to be cheap to test |
| Ad-content compliance (financial/health verticals) | Vertical disclaimer library in prompts; exclude regulated verticals at MVP |

---

## 12. Definition of Done (MVP)

- A new user goes from signup → complete campaign with ≥15 assets in <10 minutes
- Full pipeline run costs <£0.40 and completes in <90 seconds
- Second campaign for a business visibly incorporates learnings from the first
- Export bundle opens cleanly and is usable without the app
- Stripe subscription + credit gating live
- 10 beta users onboarded, ≥3 have generated 2+ campaigns
