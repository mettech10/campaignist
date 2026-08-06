# Fitting the north-star sequence to the frontend that exists

Goal: adopt the agent sequence from [CLAUDE.md](../CLAUDE.md) **without
rebuilding the frontend**. This document is the audit §6 of that file requires —
what exists, what changes, and what genuinely does not fit.

Frontend as built (`mettech10/professional-portfolio`): landing, sign-in/sign-up,
onboarding wizard, dashboard, new-campaign form, generation view, campaign view
(Strategy / Assets / Calendar / Results tabs), settings.

---

## The one thing that makes this cheap

`components/new-campaign-form.tsx` renders the generation view from
`pipelineSteps` in `lib/ui-constants.ts`, and derives progress by **counting
keys the API has written into `campaign.pipeline_outputs`**:

```ts
const state = i < completedSteps ? 'done' : i === completedSteps ? 'active' : 'pending'
```

It does not know or care what the steps *are*. Renaming five stages to seven
agents is an **edit to one array** plus the backend writing matching keys. No
component changes, no layout work, no new routes.

So the sequence fits the frontend by construction, as long as the orchestrator
persists each agent's output under an incrementing key as it completes — which
`runner.py` already does.

---

## Agent → screen mapping

| Agent | Where its output already has a home | Frontend work |
|---|---|---|
| Research | Generation view step 1 | None — new array entry |
| Strategy | Campaign view → **Strategy** tab (segments, positioning, funnel, budget split all render today) | None |
| Copy & Social | Campaign view → **Assets** tab (renders any `content_assets` row with `type`/`channel`/`format`) | None |
| Email | **Assets** tab — `type: "email"` already supported | None |
| SEO | *No home* | New tab |
| GEO | *No home* | New tab |
| Video/UGC | Partly — scripts render as assets under the existing `ugc-*` formats | Player/preview later |
| Analytics & Learning | Campaign view → **Results** tab (log-results form exists); dashboard shows a memory count | Push notifications have no surface |

Five of eight need **zero** frontend change. The Assets tab is the reason: it
renders by `type`/`channel`/`format` rather than by hardcoded categories, so new
agents that emit `content_assets` rows appear automatically.

---

## What does not fit, honestly

Three real mismatches. None are fatal, none should be papered over.

**1. Onboarding collects a business profile; the north star pastes a URL.**
`components/onboarding-wizard.tsx` is a 3-step manual form (basics → offer →
brand-voice quiz) writing a `businesses` row. The north star's entry point is a
single URL that the Research Agent scrapes into that same shape.

The rows are compatible — the Research Agent's output maps onto
`businesses.name / industry / offer_description / price_point / brand_voice`. So
the *data model* needs nothing. But the **first screen a user sees is wrong for
the new product**, and that is the headline of the whole pitch ("paste your
website"). This is the one piece of frontend work the pivot genuinely requires.

Smallest honest version: add a URL field as step 0 of the existing wizard, run
Research, and pre-fill the remaining steps for the user to confirm. That keeps
the wizard (and its validation, and the brand-voice quiz) and adds the scrape in
front of it, rather than replacing the flow.

**2. Agent chat has no surface at all.** One thread per
`campaign_id + agent_type`, routed by the orchestrator, with tool access to edit
the underlying `content_assets`. Nothing in the current UI resembles this, and
it is one of the three "on track" criteria in §8. It needs a new component and a
new API surface — this is real work, not a mapping exercise.

**3. Proactive suggestions have nowhere to appear.** §4 is explicit that a
passive dashboard is not the feature. The dashboard has a "Things your AI has
learned" stat but no notification surface.

---

## Backend changes the sequence implies

Not frontend, but worth stating in the same audit:

- `pipeline/steps.py` is five hardcoded functions. The north star wants **one
  reusable agent runtime** (`{system_prompt, tool_set, memory_scope, output_contract}`).
  The prompt specs in `pipeline/prompts/*.json` are already 80% of that shape —
  they carry `id`, `model` role, `system`, `user_template`, `schema`. What is
  missing is `tool_set` and `memory_scope`.
- `runner.py` runs steps sequentially 1..5. Strategy must precede the channel
  agents, but **Copy/Social, Email, SEO and GEO are independent of each other**
  and should fan out concurrently — the same `ThreadPoolExecutor` pattern step 4
  already uses. That matters more now: it is the difference between one slow
  sequence and four agents in parallel.
- Research needs a scraper. Per §4, reuse the Bright Data setup from
  `~/metusa-deal-analyzer` rather than building one.

---

## Recommended order

1. **Agent runtime refactor** — collapse the five step functions into one runtime
   driven by the existing prompt specs. No user-visible change; makes everything
   after it cheap. This is §4's "single most important engineering decision".
2. **Re-cut the sequence** — Research → Strategy → (Copy/Social ∥ Email ∥ SEO ∥
   GEO) → Assembly, with the four channel agents concurrent. Update
   `pipelineSteps` to match: one array edit.
3. **URL-first onboarding** — add the scrape as step 0 of the existing wizard.
4. **SEO and GEO tabs** — two new tabs on the campaign view.
5. **Agent chat** — new surface, backend and frontend.
6. **Video** — after the text loop works, per §4.

Steps 1 and 2 deliver the new architecture with **one line of frontend change**.
Everything user-visible about the pivot lands in step 3 onward.
