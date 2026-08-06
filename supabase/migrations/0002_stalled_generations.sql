-- ============================================================================
-- Recovering stranded generations
--
-- A generation runs in a background thread and the request returns 202
-- immediately. If the worker dies mid-run — a deploy, a restart, an OOM — that
-- thread goes with it and the campaign sits in `generating` forever with the
-- user's credit already spent. Observed twice in testing; both needed a manual
-- SQL fix, which no real user can do.
--
-- Two pieces: a heartbeat so staleness is measurable, and an atomic refund so
-- reaping a campaign cannot decrement without crediting.
-- ============================================================================

-- Set every time the orchestrator persists an agent's output, so "no progress
-- for N minutes" is a real measurement rather than a guess from created_at.
-- Existing rows get their creation time, which is the right conservative start.
alter table public.campaigns
  add column if not exists progress_at timestamptz not null default now();

create index if not exists campaigns_stalled_idx
  on public.campaigns (progress_at)
  where status = 'generating';

-- Mirror of spend_credit. Refund and ledger row in one transaction, so a reaped
-- campaign can never leave the balance and the audit trail disagreeing.
--
-- Idempotent on the campaign: a campaign already refunded (a positive ledger row
-- naming it) is skipped, so two workers reaping the same row concurrently — or a
-- retried request — cannot hand out the credit twice.
create or replace function public.refund_credit(
  p_user_id     uuid,
  p_reason      text,
  p_campaign_id uuid
)
returns int
language plpgsql
security definer
set search_path = public
as $$
declare
  new_balance int;
begin
  if exists (
    select 1 from public.credit_ledger
    where campaign_id = p_campaign_id and delta > 0
  ) then
    select credits into new_balance from public.profiles where id = p_user_id;
    return new_balance;
  end if;

  update public.profiles
     set credits = credits + 1
   where id = p_user_id
  returning credits into new_balance;

  if new_balance is null then
    raise exception 'no such profile: %', p_user_id using errcode = 'P0002';
  end if;

  insert into public.credit_ledger (user_id, delta, reason, campaign_id)
  values (p_user_id, 1, p_reason, p_campaign_id);

  return new_balance;
end;
$$;

-- SECURITY DEFINER and it hands out credits, so it must not be reachable from
-- the browser. PUBLIC holds EXECUTE by default and roles inherit it, so
-- revoking from anon/authenticated alone would leave it callable.
revoke execute on function public.refund_credit(uuid, text, uuid) from public;
revoke execute on function public.refund_credit(uuid, text, uuid) from anon, authenticated;
