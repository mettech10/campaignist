-- ============================================================================
-- AI Marketing Platform — initial schema
-- Blueprint v2.0 §5. Postgres 17 / Supabase.
--
-- Ownership model: auth.users → profiles → businesses → campaigns → everything.
-- Every RLS policy resolves back to auth.uid() through that chain (Metalyzi
-- pattern). The Flask API uses the service-role key and bypasses RLS; RLS is
-- the guard for the browser (anon key) hitting PostgREST directly.
-- ============================================================================

create extension if not exists "vector";
create extension if not exists "pgcrypto";

-- ── profiles ────────────────────────────────────────────────────────────────
create table if not exists public.profiles (
  id                 uuid primary key references auth.users (id) on delete cascade,
  full_name          text,
  plan               text not null default 'trial'
                       check (plan in ('trial','starter','growth','pro')),
  credits            int  not null default 1,      -- free trial = 1 full campaign
  stripe_customer_id text unique,
  created_at         timestamptz not null default now()
);

-- Auto-create a profile row on signup so the app never has to handle a missing
-- profile. SECURITY DEFINER because the trigger runs as the auth system.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, full_name)
  values (new.id, coalesce(new.raw_user_meta_data ->> 'full_name', ''))
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- ── businesses ──────────────────────────────────────────────────────────────
create table if not exists public.businesses (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null references public.profiles (id) on delete cascade,
  name              text not null,
  industry          text,
  offer_description text,
  price_point       text,
  location          text,
  brand_voice       jsonb not null default '{}'::jsonb,  -- from onboarding quiz
  website           text,
  created_at        timestamptz not null default now()
);
create index if not exists businesses_user_id_idx on public.businesses (user_id);

-- ── campaigns ───────────────────────────────────────────────────────────────
create table if not exists public.campaigns (
  id               uuid primary key default gen_random_uuid(),
  business_id      uuid not null references public.businesses (id) on delete cascade,
  goal             text not null,
  budget           numeric(10,2),
  timeframe_days   int,
  -- 'error' and 'archived' are not in the blueprint's list; they match the
  -- status union the v0 frontend already ships in web/lib/mock-data.ts.
  status           text not null default 'draft'
                     check (status in ('draft','generating','ready','live','completed','archived','error')),
  -- pipeline_outputs is keyed by step: {"1": {...}, "2": {...}, ...}
  -- Steps are written as they complete so the UI can poll and reveal progress.
  pipeline_outputs jsonb not null default '{}'::jsonb,
  calendar         jsonb,
  error            text,          -- populated when status = 'failed'
  cost_gbp         numeric(8,4),  -- token cost logged per run, from day 1
  created_at       timestamptz not null default now(),
  completed_at     timestamptz
);
create index if not exists campaigns_business_id_idx on public.campaigns (business_id);
create index if not exists campaigns_status_idx     on public.campaigns (status);

-- ── content_assets ──────────────────────────────────────────────────────────
create table if not exists public.content_assets (
  id             uuid primary key default gen_random_uuid(),
  campaign_id    uuid not null references public.campaigns (id) on delete cascade,
  type           text not null,   -- social_post | ad_copy | email | landing_copy
  channel        text,            -- instagram | facebook | linkedin | x | google | email
  -- Creative format: ugc-testimonial | anime-illustrated | product-shot | ...
  -- The full taxonomy lives in web/lib/content-formats.ts and is mirrored in
  -- api/app/pipeline/formats.py. Kept as free text, not a CHECK constraint, so
  -- new formats ship without a migration.
  format         text,
  variant        int  not null default 1,
  content        jsonb not null,  -- { hook, body, cta, hashtags[] }
  image_brief    text,            -- art direction for the shot, in the format's idiom
  status         text not null default 'generated'
                   check (status in ('generated','edited','approved','exported')),
  edited_content jsonb,           -- user edits; null means "use content"
  created_at     timestamptz not null default now()
);
create index if not exists content_assets_campaign_id_idx on public.content_assets (campaign_id);

-- ── campaign_results ────────────────────────────────────────────────────────
create table if not exists public.campaign_results (
  id          uuid primary key default gen_random_uuid(),
  campaign_id uuid not null references public.campaigns (id) on delete cascade,
  reported_at timestamptz not null default now(),
  reach       int,
  clicks      int,
  leads       int,
  sales       int,
  revenue     numeric(10,2),
  user_notes  text
);
create index if not exists campaign_results_campaign_id_idx on public.campaign_results (campaign_id);

-- ── memories (the moat) ─────────────────────────────────────────────────────
-- NOTE: vector(1536) matches the blueprint. Confirm against whichever embedding
-- model is chosen before loading real data — changing the dimension later means
-- re-embedding everything. See docs/BLUEPRINT.md §4 "Memory loop".
create table if not exists public.memories (
  id                 uuid primary key default gen_random_uuid(),
  business_id        uuid not null references public.businesses (id) on delete cascade,
  source_campaign_id uuid references public.campaigns (id) on delete set null,
  kind               text not null check (kind in ('retrospective','preference','fact')),
  content            text not null,
  embedding          vector(1536),
  created_at         timestamptz not null default now()
);
create index if not exists memories_business_id_idx on public.memories (business_id);

-- IVFFlat needs data before it helps and requires ANALYZE; at MVP scale a
-- sequential scan per business is fine. Create the index once a business has
-- thousands of memories:
--   create index memories_embedding_idx on public.memories
--     using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- Top-k retrieval for Step 1 of the pipeline. SECURITY INVOKER so RLS still
-- applies when called with a user's JWT.
create or replace function public.match_memories(
  p_business_id uuid,
  p_embedding   vector(1536),
  p_match_count int default 5
)
returns table (id uuid, kind text, content text, similarity float)
language sql
stable
as $$
  select m.id,
         m.kind,
         m.content,
         1 - (m.embedding <=> p_embedding) as similarity
  from public.memories m
  where m.business_id = p_business_id
    and m.embedding is not null
  order by m.embedding <=> p_embedding
  limit p_match_count;
$$;

-- ── credit_ledger (ported from Metalyzi) ────────────────────────────────────
-- Append-only. profiles.credits is the running balance; this table is the audit
-- trail. Never UPDATE or DELETE rows here.
create table if not exists public.credit_ledger (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references public.profiles (id) on delete cascade,
  delta       int  not null,   -- negative = spend, positive = grant/top-up
  reason      text not null,   -- 'campaign_generation' | 'stripe_renewal' | 'trial_grant' | ...
  campaign_id uuid references public.campaigns (id) on delete set null,
  created_at  timestamptz not null default now()
);
create index if not exists credit_ledger_user_id_idx on public.credit_ledger (user_id, created_at desc);

-- Spend a credit and write the ledger row in one transaction. Returns the new
-- balance, or raises if the user has none left — this is the hard credit cap.
create or replace function public.spend_credit(
  p_user_id     uuid,
  p_reason      text,
  p_campaign_id uuid default null
)
returns int
language plpgsql
security definer
set search_path = public
as $$
declare
  new_balance int;
begin
  update public.profiles
     set credits = credits - 1
   where id = p_user_id
     and credits > 0
  returning credits into new_balance;

  if new_balance is null then
    raise exception 'insufficient_credits' using errcode = 'P0001';
  end if;

  insert into public.credit_ledger (user_id, delta, reason, campaign_id)
  values (p_user_id, -1, p_reason, p_campaign_id);

  return new_balance;
end;
$$;

-- ============================================================================
-- Row-Level Security
-- ============================================================================
alter table public.profiles         enable row level security;
alter table public.businesses       enable row level security;
alter table public.campaigns        enable row level security;
alter table public.content_assets   enable row level security;
alter table public.campaign_results enable row level security;
alter table public.memories         enable row level security;
alter table public.credit_ledger    enable row level security;

-- profiles: own row only. No INSERT policy — the signup trigger owns creation.
-- No DELETE policy — deleting a profile happens via auth.users cascade.
drop policy if exists profiles_select on public.profiles;
create policy profiles_select on public.profiles
  for select using (id = (select auth.uid()));

-- Row scope only. An RLS policy cannot restrict *which columns* a user may
-- write — without the column grant below, this policy alone lets any user
-- PATCH their own row and set plan='pro', credits=9999 through PostgREST.
drop policy if exists profiles_update on public.profiles;
create policy profiles_update on public.profiles
  for update using (id = (select auth.uid()))
  with check (id = (select auth.uid()));

-- Column-level privileges are what actually confine the user to their name.
-- plan, credits, and stripe_customer_id stay service-role-only (Stripe webhook,
-- spend_credit). Runs after the table exists and after Supabase's default
-- grants, so it overrides them.
revoke update on public.profiles from anon, authenticated;
grant update (full_name) on public.profiles to authenticated;

-- businesses: owned directly.
drop policy if exists businesses_all on public.businesses;
create policy businesses_all on public.businesses
  for all using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

-- Everything below resolves ownership through businesses.
drop policy if exists campaigns_all on public.campaigns;
create policy campaigns_all on public.campaigns
  for all using (
    exists (select 1 from public.businesses b
             where b.id = campaigns.business_id
               and b.user_id = (select auth.uid()))
  )
  with check (
    exists (select 1 from public.businesses b
             where b.id = campaigns.business_id
               and b.user_id = (select auth.uid()))
  );

drop policy if exists content_assets_all on public.content_assets;
create policy content_assets_all on public.content_assets
  for all using (
    exists (select 1 from public.campaigns c
             join public.businesses b on b.id = c.business_id
            where c.id = content_assets.campaign_id
              and b.user_id = (select auth.uid()))
  )
  with check (
    exists (select 1 from public.campaigns c
             join public.businesses b on b.id = c.business_id
            where c.id = content_assets.campaign_id
              and b.user_id = (select auth.uid()))
  );

drop policy if exists campaign_results_all on public.campaign_results;
create policy campaign_results_all on public.campaign_results
  for all using (
    exists (select 1 from public.campaigns c
             join public.businesses b on b.id = c.business_id
            where c.id = campaign_results.campaign_id
              and b.user_id = (select auth.uid()))
  )
  with check (
    exists (select 1 from public.campaigns c
             join public.businesses b on b.id = c.business_id
            where c.id = campaign_results.campaign_id
              and b.user_id = (select auth.uid()))
  );

-- memories: readable by the owner ("what the AI has learned about your
-- business"), but written only by the service role after a retrospective.
drop policy if exists memories_select on public.memories;
create policy memories_select on public.memories
  for select using (
    exists (select 1 from public.businesses b
             where b.id = memories.business_id
               and b.user_id = (select auth.uid()))
  );

-- credit_ledger: read-only audit trail for the user.
drop policy if exists credit_ledger_select on public.credit_ledger;
create policy credit_ledger_select on public.credit_ledger
  for select using (user_id = (select auth.uid()));

-- spend_credit is SECURITY DEFINER, so anyone who can execute it can spend any
-- user's credits by passing their id. Postgres grants EXECUTE to PUBLIC by
-- default and roles inherit that, so revoking from anon/authenticated alone
-- leaves it callable — PUBLIC must be revoked too.
revoke execute on function public.spend_credit(uuid, text, uuid) from public;
revoke execute on function public.spend_credit(uuid, text, uuid) from anon, authenticated;
revoke execute on function public.handle_new_user() from public;
