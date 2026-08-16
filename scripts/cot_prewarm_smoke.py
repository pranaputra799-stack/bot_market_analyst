"""Uji end-to-end pre-warm COT dengan data CFTC ASLI (read-only).

Menjalankan pipeline persis seperti SchedulerJobsMixin.prewarm_cot_cache:
  1. Download arsip tahun berjalan (legacy + TFF) dari situs CFTC — NYATA.
  2. Parse + extract data untuk SEMUA instrumen COT_INSTRUMENTS.
  3. Serialisasi JSON-safe (round-trip cot_data_to_json/from_json) —
     memastikan data siap ditulis ke Supabase.
  4. Simulasi dedup 'data identik → skip tulis': dua run berurutan harus
     menghasilkan payload identik.

TANPA menulis ke database (tidak ada SUPABASE_URL di env ini). Jalankan:
    python scripts/cot_prewarm_smoke.py
Exit code 0 = semua instrumen ter-ekstrak & JSON-safe; selain itu 1.
"""

import os
import sys
import time

# Pastikan root project ada di sys.path (script dijalankan dari mana pun)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.cot import (
    COT_INSTRUMENTS,
    cot_data_from_json,
    cot_data_to_json,
    extract_market,
    fetch_tff_rows,
    fetch_year_rows,
)


def main() -> int:
    t0 = time.time()
    print(f"Uji pre-warm COT dengan data CFTC asli — {len(COT_INSTRUMENTS)} instrumen")
    print("=" * 70)

    legacy = fetch_year_rows(force=True)
    tff = fetch_tff_rows(force=True)
    print(f"Arsip terunduh: legacy={len(legacy)} baris | tff={len(tff)} baris\n")

    cached = 0       # ter-ekstrak & JSON-safe (siap ditulis ke Supabase)
    skipped = 0      # tidak ter-ekstrak (instrumen tidak ada di laporan — mis. NZD)
    failures = []
    for cfg in COT_INSTRUMENTS:
        rows = tff if cfg.get("report") == "tff" else legacy
        data = extract_market(rows, cfg)
        if not data:
            skipped += 1
            failures.append(("TIDAK ADA DI LAPORAN", cfg["display"]))
            continue
        try:
            # Simulasi langkah set_cot_cache: data harus JSON-safe (date → str)
            payload = cot_data_to_json(data)
            # Round-trip: harus bisa dibaca kembali seperti get_cot_cache
            restored = cot_data_from_json(payload)
            assert restored["report_date"] == data["report_date"], "round-trip tanggal gagal"
            assert payload["report_date"] == restored["report_date"].isoformat()
        except Exception as e:
            failures.append((f"JSON-SAFE GAGAL: {e}", cfg["display"]))
            continue
        cached += 1
        nc = data["noncommercial"]
        print(
            f"  [OK]  {cfg['display'][:38]:38s} OI={data['open_interest']:>10,}  "
            f"net_spec={nc.get('net', 0):>+10,}  {data['report_date']}"
        )

    # Simulasi dedup: dua payload untuk data yang sama harus identik
    # (ini yang membuat run harian berikutnya men-skip tulis ke Supabase).
    dup = 0
    dup_fail = []
    for cfg in COT_INSTRUMENTS:
        rows = tff if cfg.get("report") == "tff" else legacy
        d1 = extract_market(rows, cfg)
        d2 = extract_market(rows, cfg)
        if not d1:
            continue
        dup += 1
        if cot_data_to_json(d1) != cot_data_to_json(d2):
            dup_fail.append(cfg["display"])

    print("\n" + "=" * 70)
    print(f"Hasil: {cached}/{len(COT_INSTRUMENTS)} instrumen siap di-cache | {skipped} tidak ada di laporan")
    if dup_fail:
        print(f"[WARN] Dedup TIDAK identik ({len(dup_fail)}): {dup_fail}")
    else:
        print(f"[OK] Dedup konsisten untuk {dup} instrumen (run berikutnya men-skip tulis)")
    if failures:
        print("Gagal/tidak tersedia:")
        for kind, name in failures:
            print(f"   - {name}: {kind}")
    print(f"Waktu total: {time.time() - t0:.1f}s")
    return 0 if not dup_fail and cached >= len(COT_INSTRUMENTS) - 1 else 1


if __name__ == "__main__":
    sys.exit(main())
