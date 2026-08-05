-- ============================================================
-- MarketAI Analyst Bot — Schema Supabase
-- Jalankan SEKALI di Supabase SQL Editor (Dashboard → SQL → New query)
-- ============================================================

-- ------------------------------------------------------------
-- 1) app_cache — cache persisten (L2)
--    Menyimpan AI response & conversation memory di database,
--    bukan di RAM proses bot (agar memori justrunmy/Railway
--    tidak membengkak). Baris otomatis dibersihkan bot saat
--    melewati expires_at.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.app_cache (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_app_cache_expires_at
    ON public.app_cache (expires_at);

-- ------------------------------------------------------------
-- 2) users — dipakai data/database.py (upsert_user)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.users (
    user_id    BIGINT PRIMARY KEY,
    username   TEXT DEFAULT '',
    first_name TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ------------------------------------------------------------
-- 3) subscribers — dipakai data/database.py (morning brief)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.subscribers (
    chat_id    BIGINT PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- Catatan RLS (Row Level Security):
-- Supabase project baru biasanya mengaktifkan RLS. Agar bot bisa
-- membaca/menulis via REST (anon key), izinkan akses tabel di atas:
--
--   alter table public.app_cache enable row level security;
--   create policy "app_cache anon" on public.app_cache
--     for all using (true) with check (true);
--
--   alter table public.users enable row level security;
--   create policy "users anon" on public.users
--     for all using (true) with check (true);
--
--   alter table public.subscribers enable row level security;
--   create policy "subscribers anon" on public.subscribers
--     for all using (true) with check (true);
--
-- (Aman untuk bot pribadi; untuk produksi publik, ganti 'anon'
--  dengan service_role key dan jangan expose di frontend.)
-- ============================================================
