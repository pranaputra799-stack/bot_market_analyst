-- ============================================================
-- MarketAI Analyst Bot — Schema Supabase
-- Jalankan SEKALI di Supabase SQL Editor (Dashboard → SQL → New query)
-- File ini IDEMPOTENT: aman dijalankan ulang (CREATE IF NOT EXISTS).
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

-- ------------------------------------------------------------
-- 4) watchlist — instrumen favorit per user (fitur /watch)
--    Satu user boleh punya banyak simbol; satu simbol sekali per
--    user (unique constraint dipakai PostgREST merge-duplicates).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.watchlist (
    chat_id    BIGINT NOT NULL,
    symbol     TEXT NOT NULL,
    label      TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_symbol
    ON public.watchlist (symbol);

-- ------------------------------------------------------------
-- 5) price_history — snapshot harga berkala (fitur /riwayat)
--    Job recorder bot menyimpan harga tiap interval; riwayat
--    otomatis dibersihkan setelah >30 hari (job harian).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.price_history (
    id         BIGSERIAL PRIMARY KEY,
    symbol     TEXT NOT NULL,
    price      DOUBLE PRECISION NOT NULL,
    bid        DOUBLE PRECISION,
    ask        DOUBLE PRECISION,
    change_pct DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_price_history_symbol_created
    ON public.price_history (symbol, created_at DESC);

-- ============================================================
-- RLS (Row Level Security) & GRANT
--
-- Supabase project baru mengaktifkan RLS secara default. Tanpa
-- policy, request REST memakai anon key akan DITOLAK (data tak
-- terlihat / error), sehingga bot tidak bisa memakai cache L2.
-- Policy di bawah mengizinkan akses anon — aman untuk bot
-- pribadi. Untuk produksi publik: ganti role 'anon' dengan
-- service_role (jangan expose key-nya di frontend).
-- ============================================================

-- app_cache
ALTER TABLE public.app_cache ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "app_cache_all_anon" ON public.app_cache;
CREATE POLICY "app_cache_all_anon" ON public.app_cache
    FOR ALL TO anon, authenticated USING (true) WITH CHECK (true);

-- watchlist
ALTER TABLE public.watchlist ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "watchlist_all_anon" ON public.watchlist;
CREATE POLICY "watchlist_all_anon" ON public.watchlist
    FOR ALL TO anon, authenticated USING (true) WITH CHECK (true);

-- price_history
ALTER TABLE public.price_history ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "price_history_all_anon" ON public.price_history;
CREATE POLICY "price_history_all_anon" ON public.price_history
    FOR ALL TO anon, authenticated USING (true) WITH CHECK (true);

-- users
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "users_all_anon" ON public.users;
CREATE POLICY "users_all_anon" ON public.users
    FOR ALL TO anon, authenticated USING (true) WITH CHECK (true);

-- subscribers
ALTER TABLE public.subscribers ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "subscribers_all_anon" ON public.subscribers;
CREATE POLICY "subscribers_all_anon" ON public.subscribers
    FOR ALL TO anon, authenticated USING (true) WITH CHECK (true);

-- Grant eksplisit (pengaman tambahan; Supabase biasanya sudah
-- memberi default privileges untuk schema public)
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.app_cache TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.users    TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.subscribers TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.watchlist TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.price_history TO anon, authenticated, service_role;

-- ============================================================
-- Verifikasi cepat (jalankan setelah semua statement di atas):
--
--   select table_name from information_schema.tables
--   where table_schema = 'public'
--   and table_name in ('app_cache', 'users', 'subscribers', 'watchlist', 'price_history');
--
-- Harus mengembalikan 5 baris. Jika sudah pernah punya tabel
-- users/subscribers sebelumnya, baris lama tetap aman (IF NOT EXISTS).
-- ============================================================
