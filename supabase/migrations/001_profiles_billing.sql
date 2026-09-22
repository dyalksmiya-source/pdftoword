-- PDF2Wordly migration: profiles + RLS + auto-creation + atomic free-limit
-- SAFE TO RE-RUN: every statement is additive / IF NOT EXISTS / OR REPLACE,
-- and existing rows are preserved (only missing columns are added).
--
-- Run in: Supabase Dashboard -> SQL Editor (as postgres).

-- ---------------------------------------------------------------------------
-- 1. profiles table (create once, then only add missing columns)
-- ---------------------------------------------------------------------------
create table if not exists public.profiles (
  id uuid primary key references auth.users (id) on delete cascade
);

alter table public.profiles add column if not exists full_name text;
alter table public.profiles add column if not exists email text;
alter table public.profiles add column if not exists avatar_url text;
alter table public.profiles add column if not exists provider text;
alter table public.profiles add column if not exists plan text not null default 'free';
alter table public.profiles add column if not exists subscription_status text;
alter table public.profiles add column if not exists subscription_id text;
alter table public.profiles add column if not exists current_period_end timestamptz;
alter table public.profiles add column if not exists last_free_conversion_at timestamptz;
alter table public.profiles add column if not exists updated_at timestamptz not null default now();

-- Backfill guard: only ever 'free' | 'pro'.
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'profiles_plan_check'
  ) then
    alter table public.profiles
      add constraint profiles_plan_check check (plan in ('free', 'pro'));
  end if;
end $$;

create unique index if not exists profiles_subscription_id_uidx
  on public.profiles (subscription_id) where subscription_id is not null;
create index if not exists profiles_plan_idx on public.profiles (plan);

-- ---------------------------------------------------------------------------
-- 2. Auto-create profile on signup (single trigger, no duplicates)
-- ---------------------------------------------------------------------------
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, email, full_name, avatar_url, provider)
  values (
    new.id,
    new.email,
    coalesce(new.raw_user_meta_data ->> 'full_name', new.raw_user_meta_data ->> 'name'),
    new.raw_user_meta_data ->> 'avatar_url',
    coalesce(new.raw_app_meta_data ->> 'provider', 'email')
  )
  on conflict (id) do update set
    email = excluded.email,
    updated_at = now();
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- ---------------------------------------------------------------------------
-- 3. Row Level Security
--    - users read ONLY their own row
--    - users may update ONLY safe columns (full_name, avatar_url);
--      subscription / quota columns are NOT writable by normal users
--    - no INSERT / DELETE for normal users (rows come from the trigger,
--      updates come from service-role / webhook)
-- ---------------------------------------------------------------------------
alter table public.profiles enable row level security;

drop policy if exists "profiles_select_own" on public.profiles;
drop policy if exists "profiles_update_own_safe" on public.profiles;
drop policy if exists "profiles_insert_own" on public.profiles;
drop policy if exists "profiles_select_all" on public.profiles;
drop policy if exists "profiles_update_all" on public.profiles;

create policy "profiles_select_own"
  on public.profiles for select
  to authenticated
  using (auth.uid() = id);

-- UPDATE is allowed at the RLS layer but forced to safe columns only: the
-- WITH CHECK clause requires every subscription/quota column to stay equal
-- to its old value, so a malicious client can never promote itself to Pro
-- or reset its free-conversion timestamp.
create policy "profiles_update_own_safe"
  on public.profiles for update
  to authenticated
  using (auth.uid() = id)
  with check (
    auth.uid() = id
    and plan IS NOT DISTINCT FROM (select p.plan from public.profiles p where p.id = profiles.id)
    and subscription_status IS NOT DISTINCT FROM (select p.subscription_status from public.profiles p where p.id = profiles.id)
    and subscription_id IS NOT DISTINCT FROM (select p.subscription_id from public.profiles p where p.id = profiles.id)
    and current_period_end IS NOT DISTINCT FROM (select p.current_period_end from public.profiles p where p.id = profiles.id)
    and last_free_conversion_at IS NOT DISTINCT FROM (select p.last_free_conversion_at from public.profiles p where p.id = profiles.id)
  );

-- ---------------------------------------------------------------------------
-- 4. Atomic free-conversion claim / release (race-safe, row lock)
--    Called ONLY with the service-role key from the Flask backend.
-- ---------------------------------------------------------------------------
create or replace function public.claim_free_conversion(p_user_id uuid)
returns table (allowed boolean, reason text, previous timestamptz)
language plpgsql
security definer
set search_path = public
as $$
declare
  v_plan text;
  v_status text;
  v_last timestamptz;
begin
  select plan, subscription_status, last_free_conversion_at
    into v_plan, v_status, v_last
    from public.profiles
   where id = p_user_id
   for update;  -- row lock: concurrent requests serialize here

  if not found then
    return query select false, 'no_profile'::text, null::timestamptz;
    return;
  end if;

  -- Active Pro never consumes quota (backend also bypasses, belt & braces).
  if v_plan = 'pro' and v_status in ('active', 'trialing') then
    return query select true, 'pro'::text, v_last;
    return;
  end if;

  if v_last is null or v_last <= now() - interval '24 hours' then
    update public.profiles
       set last_free_conversion_at = now(), updated_at = now()
     where id = p_user_id;
    return query select true, 'claimed'::text, v_last;
  else
    return query select false, 'limited'::text, v_last;
  end if;
end;
$$;

create or replace function public.release_free_conversion(p_user_id uuid, p_previous timestamptz)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  -- Roll back a claim after a FAILED conversion so the user keeps quota.
  update public.profiles
     set last_free_conversion_at = p_previous, updated_at = now()
   where id = p_user_id
     and (plan is distinct from 'pro' or subscription_status not in ('active', 'trialing'));
end;
$$;

revoke all on function public.claim_free_conversion(uuid) from anon, authenticated;
revoke all on function public.release_free_conversion(uuid, timestamptz) from anon, authenticated;

-- ---------------------------------------------------------------------------
-- 5. Webhook idempotency log (duplicate deliveries are safely ignored)
-- ---------------------------------------------------------------------------
create table if not exists public.lemonsqueezy_events (
  event_id text primary key,
  event_name text,
  subscription_id text,
  user_id uuid,
  created_at timestamptz not null default now()
);

alter table public.lemonsqueezy_events enable row level security;

-- No policies for anon/authenticated: normal users cannot read, insert,
-- update, or delete webhook rows. Only service-role (webhook) touches it.
drop policy if exists "lemonsqueezy_events_select_all" on public.lemonsqueezy_events;
drop policy if exists "lemonsqueezy_events_insert_all" on public.lemonsqueezy_events;
