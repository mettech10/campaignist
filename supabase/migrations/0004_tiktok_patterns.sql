-- ── TikTok market patterns ──────────────────────────────────────────────────
-- Store oEmbed metadata for a public TikTok URL so Campaignist can adapt an
-- *owned* source clip to that market pattern. The other creator's video file
-- is never downloaded — TikTok has no official download API, and remixing it
-- would be their content.
--
-- production = adapt  → ffmpeg edit of owner upload / stock / prior UGC
-- production = generate → fal (product UGC + motion-design only)

create table if not exists public.tiktok_patterns (
  id            uuid primary key default gen_random_uuid(),
  campaign_id   uuid not null references public.campaigns (id) on delete cascade,
  source_url    text not null,
  oembed        jsonb not null,
  pattern       jsonb not null,  -- { hook, hashtags, author_*, thumbnail_url, notes }
  created_at    timestamptz not null default now()
);

create index if not exists tiktok_patterns_campaign_idx
  on public.tiktok_patterns (campaign_id, created_at desc);

alter table public.tiktok_patterns enable row level security;

drop policy if exists tiktok_patterns_select_own on public.tiktok_patterns;
create policy tiktok_patterns_select_own on public.tiktok_patterns
  for select using (
    exists (
      select 1
        from public.campaigns c
        join public.businesses b on b.id = c.business_id
       where c.id = tiktok_patterns.campaign_id
         and b.user_id = (select auth.uid())
    )
  );

-- Writes go through the service-role API only (ingest validates the URL).
