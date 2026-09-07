-- Phase 1: make pattern rows queryable by format family / niche without
-- digging through jsonb, and allow campaign-less builtin seeds later if needed.
-- Existing tiktok_patterns.pattern jsonb remains the source of truth for the
-- full structure; these columns are denormalised indexes.

alter table public.tiktok_patterns
  add column if not exists format_family text,
  add column if not exists hook_style text,
  add column if not exists niche_tags text[] not null default '{}';

create index if not exists tiktok_patterns_family_idx
  on public.tiktok_patterns (campaign_id, format_family);

comment on column public.tiktok_patterns.format_family is
  'slideshow | hook_demo | meme | ugc — rebuild template family';
