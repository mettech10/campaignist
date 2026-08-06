-- ── Render jobs ─────────────────────────────────────────────────────────────
-- A queue of video renders for a rented GPU worker to pick up.
--
-- Why a table and not a watched folder: the worker runs on an interruptible
-- Vast.ai instance, which is what makes it £1.8/day rather than £6. Interruption
-- is routine, not exceptional — the box can vanish mid-render at any moment. A
-- job file on that box's disk vanishes with it, silently, and the user waits
-- forever for a video nobody is making. The same failure mode as a stranded
-- campaign generation, which is already handled here with a lease and a
-- heartbeat, so this reuses that shape rather than inventing a second one.
--
-- The worker never talks to this table. It holds one narrow token and speaks
-- only to the Render API, because a rented box from an anonymous marketplace
-- host is not somewhere the service-role key belongs.

create table if not exists public.render_jobs (
  id            uuid primary key default gen_random_uuid(),
  asset_id      uuid not null references public.content_assets (id) on delete cascade,
  campaign_id   uuid not null references public.campaigns (id) on delete cascade,

  -- Denormalised from the asset so the worker gets one self-contained payload
  -- and never needs a second round trip to render.
  format        text not null,   -- b-roll-montage | ugc-testimonial | ...
  aspect_ratio  text not null,   -- 9:16 | 1:1 | 16:9
  prompt        jsonb not null,  -- { brief, hook, cta, seconds }

  status        text not null default 'queued'
                  check (status in ('queued','claimed','done','error')),

  -- Lease. claimed_by is the worker id; progress_at is its heartbeat, and is
  -- what tells a stalled render from a slow one.
  claimed_by    text,
  claimed_at    timestamptz,
  progress_at   timestamptz,
  attempts      int  not null default 0,

  video_path    text,            -- object path inside the renders bucket
  error         text,

  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- The claim query: oldest queued job first. Partial, because queued rows are a
-- small slice of the table once this is busy and the index should stay hot.
create index if not exists render_jobs_queued_idx
  on public.render_jobs (created_at)
  where status = 'queued';

-- The reaper query: find leases that stopped beating.
create index if not exists render_jobs_claimed_idx
  on public.render_jobs (progress_at)
  where status = 'claimed';

create index if not exists render_jobs_campaign_idx
  on public.render_jobs (campaign_id);

-- One live job per asset. Re-rendering an asset should replace its video, not
-- queue a second render alongside the first and race over the same upload.
create unique index if not exists render_jobs_one_live_per_asset
  on public.render_jobs (asset_id)
  where status in ('queued', 'claimed');

alter table public.render_jobs enable row level security;

-- Owners can watch their own renders. Nobody writes through RLS: every write
-- goes through the API with the service role, so there is no insert, update or
-- delete policy here on purpose.
drop policy if exists render_jobs_select_own on public.render_jobs;
create policy render_jobs_select_own on public.render_jobs
  for select using (
    exists (
      select 1
        from public.campaigns c
        join public.businesses b on b.id = c.business_id
       where c.id = render_jobs.campaign_id
         and b.user_id = (select auth.uid())
    )
  );

-- ── Storage ─────────────────────────────────────────────────────────────────
-- Private bucket. Finished videos are served through short-lived signed URLs
-- minted by the API, so a leaked path is not a leaked video.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('renders', 'renders', false, 524288000, array['video/mp4','video/webm'])
on conflict (id) do nothing;

-- Owners can read their own finished renders directly, for the dashboard. The
-- first path segment is the campaign id, which is what ties an object back to a
-- user without a second lookup table.
--
-- storage.objects.name is spelled out in full on purpose. Written as bare
-- `name`, it resolves against the subquery's own scope first, where the join to
-- businesses has brought a `name` column in — so the policy silently asks
-- whether foldername('Alice Bakery') matches a campaign id, gets NULL, and
-- denies every user their own videos. It fails closed, which is the good
-- direction, but it fails closed for everyone and looks like a broken feature
-- rather than a broken policy.
drop policy if exists renders_read_own on storage.objects;
create policy renders_read_own on storage.objects
  for select using (
    bucket_id = 'renders'
    and exists (
      select 1
        from public.campaigns c
        join public.businesses b on b.id = c.business_id
       where b.user_id = (select auth.uid())
         and c.id::text = (storage.foldername(storage.objects.name))[1]
    )
  );
