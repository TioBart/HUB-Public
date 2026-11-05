-- Tabela de preferências do Hub Econômico.
-- Rode uma vez no Supabase: SQL Editor > New query > colar tudo > Run.

create table if not exists public.preferencias (
  user_id uuid primary key references auth.users (id) on delete cascade,
  dados jsonb not null default '{}'::jsonb,
  atualizado_em timestamptz not null default now()
);

-- Cada pessoa só enxerga e altera a própria linha.
alter table public.preferencias enable row level security;

drop policy if exists "ler as próprias preferências" on public.preferencias;
create policy "ler as próprias preferências" on public.preferencias
  for select using (auth.uid() = user_id);

drop policy if exists "criar as próprias preferências" on public.preferencias;
create policy "criar as próprias preferências" on public.preferencias
  for insert with check (auth.uid() = user_id);

drop policy if exists "alterar as próprias preferências" on public.preferencias;
create policy "alterar as próprias preferências" on public.preferencias
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
