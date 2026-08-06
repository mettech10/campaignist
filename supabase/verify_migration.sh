#!/usr/bin/env bash
# Apply the migrations to a throwaway local Postgres and assert the security
# properties that RLS alone does not give you.
#
# Two of these assertions exist because the first version of 0001_init.sql
# failed them:
#   * an RLS policy scopes rows, not columns — without a column-level GRANT a
#     user can PATCH their own profile and set plan='pro', credits=9999
#   * Postgres grants EXECUTE to PUBLIC by default, so revoking a SECURITY
#     DEFINER function from anon/authenticated alone leaves it callable
#
# Usage:  ./supabase/verify_migration.sh
# Needs:  postgresql@17 with pgvector (brew install postgresql@17 pgvector)

set -euo pipefail
export LC_ALL=C LANG=C

PG_BIN="${PG_BIN:-/usr/local/opt/postgresql@17/bin}"
DATA=/tmp/campaignist-verify-data
SOCK=/tmp/campaignist-verify
PORT="${PORT:-5433}"
MIGRATIONS="$(cd "$(dirname "$0")" && pwd)/migrations"

[ -x "$PG_BIN/initdb" ] || { echo "postgres 17 not at $PG_BIN — set PG_BIN"; exit 1; }

cleanup() { "$PG_BIN/pg_ctl" -D "$DATA" stop -m immediate >/dev/null 2>&1 || true; rm -rf "$DATA" "$SOCK"; }
trap cleanup EXIT

rm -rf "$DATA" "$SOCK"; mkdir -p "$SOCK"
"$PG_BIN/initdb" -D "$DATA" -U postgres --auth=trust --locale=C >/dev/null
"$PG_BIN/pg_ctl" -D "$DATA" -o "-p $PORT -k $SOCK -c listen_addresses=''" -l "$DATA/log" start >/dev/null
sleep 2

psqlv() { "$PG_BIN/psql" -h "$SOCK" -p "$PORT" -U postgres -d postgres -q "$@"; }

# Stand in for what Supabase provides: the auth schema, auth.uid(), the anon and
# authenticated roles, and the default grants on public.
psqlv -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
do $$ begin create role anon; exception when duplicate_object then null; end $$;
do $$ begin create role authenticated; exception when duplicate_object then null; end $$;
create schema auth;
create table auth.users (
  id uuid primary key default gen_random_uuid(),
  email text,
  raw_user_meta_data jsonb default '{}'::jsonb
);
create or replace function auth.uid() returns uuid language sql stable as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
$$;
grant usage on schema auth to anon, authenticated;
grant execute on function auth.uid() to anon, authenticated;
grant usage on schema public to anon, authenticated;
alter default privileges in schema public grant all on tables to anon, authenticated;
SQL

for migration in "$MIGRATIONS"/*.sql; do
  echo "applying $(basename "$migration")"
  psqlv -v ON_ERROR_STOP=1 -f "$migration" >/dev/null
done

psqlv -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
insert into auth.users (id, email, raw_user_meta_data) values
 ('11111111-1111-1111-1111-111111111111','alice@example.com','{"full_name":"Alice"}'),
 ('22222222-2222-2222-2222-222222222222','bob@example.com','{}');
insert into public.businesses (id, user_id, name) values
 ('aaaaaaaa-0000-0000-0000-000000000001','11111111-1111-1111-1111-111111111111','Alice Bakery'),
 ('bbbbbbbb-0000-0000-0000-000000000002','22222222-2222-2222-2222-222222222222','Bob Gym');
insert into public.campaigns (id, business_id, goal) values
 ('cccccccc-0000-0000-0000-000000000001','aaaaaaaa-0000-0000-0000-000000000001','Alice goal'),
 ('dddddddd-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000002','Bob goal');
SQL

# Each assertion raises if the property does not hold, so ON_ERROR_STOP fails
# the script rather than printing a passing-looking log.
psqlv -v ON_ERROR_STOP=1 <<'SQL'
begin;
set local role authenticated;
set local request.jwt.claim.sub = '11111111-1111-1111-1111-111111111111';

do $$
declare n int;
begin
  -- signup trigger created the profile with a trial credit
  select credits into n from public.profiles where id = auth.uid();
  if n is distinct from 1 then raise exception 'signup trigger did not grant 1 trial credit (got %)', n; end if;

  -- tenant isolation
  select count(*) into n from public.businesses;
  if n <> 1 then raise exception 'RLS leak: sees % businesses, expected 1', n; end if;
  select count(*) into n from public.campaigns;
  if n <> 1 then raise exception 'RLS leak: sees % campaigns, expected 1', n; end if;

  -- privilege escalation via own profile row
  begin
    update public.profiles set credits = 9999, plan = 'pro' where id = auth.uid();
    raise exception 'ESCALATION: user set their own plan/credits';
  exception when insufficient_privilege then null;
  end;

  -- but the user must still be able to edit their own name
  update public.profiles set full_name = 'Alice Renamed' where id = auth.uid();

  -- SECURITY DEFINER credit spend must not be reachable from the browser
  begin
    perform public.spend_credit('22222222-2222-2222-2222-222222222222', 'theft');
    raise exception 'ESCALATION: spend_credit callable by authenticated';
  exception when insufficient_privilege then null;
  end;

  -- cross-tenant write
  begin
    insert into public.campaigns (business_id, goal)
    values ('bbbbbbbb-0000-0000-0000-000000000002', 'stolen');
    raise exception 'ESCALATION: cross-tenant campaign insert allowed';
  exception when insufficient_privilege then null;
  end;

  -- memories are written by the service role only
  begin
    insert into public.memories (business_id, kind, content)
    values ('aaaaaaaa-0000-0000-0000-000000000001', 'fact', 'injected');
    raise exception 'ESCALATION: user wrote to memories';
  exception when insufficient_privilege then null;
  end;

  -- refund_credit hands out credits; it must not be reachable from the browser
  begin
    perform public.refund_credit(auth.uid(), 'free money', null);
    raise exception 'ESCALATION: refund_credit callable by authenticated';
  exception when insufficient_privilege then null;
  end;

  raise notice 'all assertions passed';
end $$;
rollback;
SQL

echo "OK — migrations apply and all security assertions hold"
