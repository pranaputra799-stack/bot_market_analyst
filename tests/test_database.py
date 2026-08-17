"""Unit tests untuk async wrapper Database — TANPA network.

Metode sinkron di-mock sehingga tidak ada request HTTP keluar. Verifikasi:
wrapper *_async mendelegasikan ke metode sinkron dan mengembalikan hasil yang
sama (dijalankan via asyncio.to_thread, bukan di event loop).
"""

import asyncio
import unittest
from unittest import mock

from data.database import Database


class TestDatabaseAsyncWrappers(unittest.TestCase):
    def test_upsert_user_async_delegates_to_sync(self):
        with mock.patch.object(Database, "upsert_user", return_value=True) as m:
            result = asyncio.run(Database.upsert_user_async(42, "user", "Nama"))
        m.assert_called_once_with(42, "user", "Nama")
        self.assertTrue(result)

    def test_get_all_subscribers_async_delegates(self):
        with mock.patch.object(Database, "get_all_subscribers", return_value=[1, 2, 3]) as m:
            result = asyncio.run(Database.get_all_subscribers_async())
        self.assertEqual(result, [1, 2, 3])
        m.assert_called_once()

    def test_is_subscribed_async_delegates(self):
        with mock.patch.object(Database, "is_subscribed", return_value=True) as m:
            self.assertTrue(asyncio.run(Database.is_subscribed_async(99)))
        m.assert_called_once_with(99)

    def test_add_remove_subscriber_async_delegates(self):
        with mock.patch.object(Database, "add_subscriber", return_value=True) as m1, \
             mock.patch.object(Database, "remove_subscriber", return_value=False) as m2:
            self.assertTrue(asyncio.run(Database.add_subscriber_async(10)))
            self.assertFalse(asyncio.run(Database.remove_subscriber_async(10)))
        m1.assert_called_once_with(10)
        m2.assert_called_once_with(10)

    def test_event_alert_subscribers_async_delegates(self):
        with mock.patch.object(Database, "get_event_alert_subscribers", return_value={1, 2}) as m1, \
             mock.patch.object(Database, "save_event_alert_subscribers", return_value=True) as m2:
            self.assertEqual(asyncio.run(Database.get_event_alert_subscribers_async()), {1, 2})
            self.assertTrue(asyncio.run(Database.save_event_alert_subscribers_async({3})))
        m1.assert_called_once()
        m2.assert_called_once_with({3})

    def test_event_alert_notified_async_delegates(self):
        with mock.patch.object(Database, "get_event_alert_notified", return_value={"k1", "k2"}) as m1, \
             mock.patch.object(Database, "save_event_alert_notified", return_value=True) as m2:
            self.assertEqual(asyncio.run(Database.get_event_alert_notified_async()), {"k1", "k2"})
            self.assertTrue(asyncio.run(Database.save_event_alert_notified_async({"k3"})))
        m1.assert_called_once()
        m2.assert_called_once_with({"k3"})

    def test_save_event_alert_notified_replace_all(self):
        sess = mock.Mock()
        delete_fake = mock.Mock()
        delete_fake.raise_for_status = mock.Mock()
        sess.delete.return_value = delete_fake
        post_fake = mock.Mock()
        post_fake.raise_for_status = mock.Mock()
        sess.post.return_value = post_fake
        # Tabel berisi key lama "stale1" + key baru "a" → hanya stale1 yang di-prune
        get_fake = mock.Mock()
        get_fake.json.return_value = [{"key": "a"}, {"key": "stale1"}]
        sess.get.return_value = get_fake
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("data.database._session", return_value=sess):
            self.assertTrue(Database.save_event_alert_notified({"b", "a"}))
        # Upsert dulu (merge-duplicates + created_at eksplisit)...
        sess.post.assert_called_once()
        payload = sess.post.call_args.kwargs["json"]
        self.assertEqual([p["key"] for p in payload], ["a", "b"])
        for row in payload:
            self.assertIn("created_at", row)
            self.assertTrue(row["created_at"])
        prefer = sess.post.call_args.kwargs["headers"]["Prefer"]
        self.assertIn("merge-duplicates", prefer)
        # ...lalu prune HANYA key lama yang tidak ada di daftar baru
        # (bukan not.in yang bisa menghapus id baru di daftar besar).
        sess.delete.assert_called_once()
        del_url = sess.delete.call_args.args[0]
        self.assertIn("key=in.(stale1)", del_url)
        self.assertNotIn("key=not.in", del_url)

    def test_save_event_alert_notified_url_encodes_key_values(self):
        """Kunci mengandung karakter khusus ('|', ':', '+') yang harus di-URL-
        encode dalam filter in.() agar query string tidak rusak."""
        sess = mock.Mock()
        delete_fake = mock.Mock()
        delete_fake.raise_for_status = mock.Mock()
        sess.delete.return_value = delete_fake
        post_fake = mock.Mock()
        post_fake.raise_for_status = mock.Mock()
        sess.post.return_value = post_fake
        key = "NFP|2026-08-10T12:30:00+00:00"
        stale = "OLD|2026-08-01T00:00:00+00:00"
        get_fake = mock.Mock()
        get_fake.json.return_value = [{"key": stale}]
        sess.get.return_value = get_fake
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("data.database._session", return_value=sess):
            self.assertTrue(Database.save_event_alert_notified({key}))
        del_url = sess.delete.call_args.args[0]
        self.assertIn("key=in.(", del_url)
        self.assertIn("%7C", del_url)  # '|' di-encode
        self.assertIn("%3A", del_url)  # ':' di-encode
        self.assertIn("%2B", del_url)  # '+' di-encode
        # Nilai asli tidak boleh muncul mentah (harus ter-encode).
        self.assertNotIn(stale, del_url)

    def test_save_event_alert_subscribers_replace_all(self):
        sess = mock.Mock()
        delete_fake = mock.Mock()
        delete_fake.raise_for_status = mock.Mock()
        sess.delete.return_value = delete_fake
        post_fake = mock.Mock()
        post_fake.raise_for_status = mock.Mock()
        sess.post.return_value = post_fake
        # Tabel berisi chat_id 1 (masih aktif) + 99 (stale) → hanya 99 di-prune
        get_fake = mock.Mock()
        get_fake.json.return_value = [{"chat_id": 1}, {"chat_id": 99}]
        sess.get.return_value = get_fake
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("data.database._session", return_value=sess):
            self.assertTrue(Database.save_event_alert_subscribers({2, 1}))
        sess.post.assert_called_once()
        payload = sess.post.call_args.kwargs["json"]
        self.assertEqual([p["chat_id"] for p in payload], [1, 2])
        for row in payload:
            self.assertIn("created_at", row)
            self.assertTrue(row["created_at"])
        prefer = sess.post.call_args.kwargs["headers"]["Prefer"]
        self.assertIn("merge-duplicates", prefer)
        # Prune: DELETE dengan WHERE clause, TANPA menghapus id baru (1, 2).
        sess.delete.assert_called_once()
        del_url = sess.delete.call_args.args[0]
        self.assertIn("chat_id=in.(99)", del_url)
        self.assertNotIn("chat_id=not.in", del_url)

    def test_save_event_alert_subscribers_filters_invalid_chat_ids(self):
        """Nilai non-int (string/bool) tidak boleh ikut dikirim — satu nilai
        aneh membuat seluruh batch insert ditolak Postgres (400)."""
        sess = mock.Mock()
        delete_fake = mock.Mock()
        delete_fake.raise_for_status = mock.Mock()
        sess.delete.return_value = delete_fake
        post_fake = mock.Mock()
        post_fake.raise_for_status = mock.Mock()
        sess.post.return_value = post_fake
        get_fake = mock.Mock()
        get_fake.json.return_value = [{"chat_id": 2}, {"chat_id": 999}]
        sess.get.return_value = get_fake
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("data.database._session", return_value=sess):
            # Catatan: True tidak boleh dipakai bareng 1 (True == 1 di Python,
            # keduanya ter-dedupe dalam satu set). Pakai 5 agar tidak bentrok.
            self.assertTrue(Database.save_event_alert_subscribers({2, "abc", True, 5}))
        payload = sess.post.call_args.kwargs["json"]
        self.assertEqual([p["chat_id"] for p in payload], [2, 5])
        for row in payload:
            self.assertIn("created_at", row)
        sess.delete.assert_called_once()
        # Stale = existing − baru = {999} — id baru (2, 5) tidak ikut terhapus.
        self.assertIn("chat_id=in.(999)", sess.delete.call_args.args[0])

    def test_save_event_alert_subscribers_empty_clears_table(self):
        """Daftar kosong => tidak ada upsert, cukup DELETE semua via WHERE
        clause (not.is.null) agar lolos proteksi 400/21000."""
        sess = mock.Mock()
        delete_fake = mock.Mock()
        delete_fake.raise_for_status = mock.Mock()
        sess.delete.return_value = delete_fake
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("data.database._session", return_value=sess):
            self.assertTrue(Database.save_event_alert_subscribers(set()))
        sess.post.assert_not_called()
        sess.delete.assert_called_once()
        self.assertIn("chat_id=not.is.null", sess.delete.call_args.args[0])

    def test_save_event_alert_subscribers_prune_chunked(self):
        """>200 stale => DELETE prune dipecah per 200 via in.(...) agar URL tidak
        kepanjangan — dan TIDAK menghapus id baru (regresi: not.in per-chunk
        hanya menyisakan chunk terakhir)."""
        sess = mock.Mock()
        delete_fake = mock.Mock()
        delete_fake.raise_for_status = mock.Mock()
        sess.delete.return_value = delete_fake
        post_fake = mock.Mock()
        post_fake.raise_for_status = mock.Mock()
        sess.post.return_value = post_fake
        # Existing: 500 baris (0..499); daftar baru: 250..499 → stale = 0..249
        get_fake = mock.Mock()
        get_fake.json.return_value = [{"chat_id": c} for c in range(500)]
        sess.get.return_value = get_fake
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("data.database._session", return_value=sess):
            self.assertTrue(Database.save_event_alert_subscribers(set(range(250, 500))))
        self.assertEqual(sess.post.call_count, 1)
        self.assertEqual(sess.delete.call_count, 2)
        urls = [c.args[0] for c in sess.delete.call_args_list]
        # Hanya stale yang dihapus, dipecah per 200: 0..199 lalu 200..249
        self.assertIn("chat_id=in.(0,1,2", urls[0])
        self.assertIn("chat_id=in.(200,201,202", urls[1])
        # Tidak ada DELETE not.in (yang bisa menghapus id baru).
        self.assertNotIn("not.in", urls[0])
        self.assertNotIn("not.in", urls[1])

    # ===================== USER ACTIVITY =====================

    def test_update_user_activity_batch_upsert(self):
        """Satu request POST batch untuk semua user (bukan per-user per pesan)
        dengan merge-duplicates agar kolom lain tidak tertimpa."""
        sess = mock.Mock()
        post_fake = mock.Mock()
        post_fake.raise_for_status = mock.Mock()
        sess.post.return_value = post_fake
        rows = [(1, "2026-08-13T00:00:00+00:00", 5), (2, "2026-08-13T00:01:00+00:00", 2)]
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("data.database._session", return_value=sess):
            self.assertTrue(Database.update_user_activity(rows))
        sess.post.assert_called_once()
        payload = sess.post.call_args.kwargs["json"]
        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["user_id"], 1)
        self.assertEqual(payload[0]["total_questions"], 5)
        self.assertEqual(payload[1]["user_id"], 2)
        prefer = sess.post.call_args.kwargs["headers"]["Prefer"]
        self.assertIn("merge-duplicates", prefer)

    def test_update_user_activity_empty_is_noop(self):
        sess = mock.Mock()
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("data.database._session", return_value=sess):
            self.assertTrue(Database.update_user_activity([]))
        sess.post.assert_not_called()

    def test_update_user_activity_filters_invalid_ids(self):
        """user_id non-int tidak boleh ikut dikirim (bool == int di Python)."""
        sess = mock.Mock()
        post_fake = mock.Mock()
        post_fake.raise_for_status = mock.Mock()
        sess.post.return_value = post_fake
        rows = [(7, "2026-08-13T00:00:00+00:00", 1), ("abc", "2026-08-13T00:00:00+00:00", 1)]
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("data.database._session", return_value=sess):
            self.assertTrue(Database.update_user_activity(rows))
        payload = sess.post.call_args.kwargs["json"]
        self.assertEqual([p["user_id"] for p in payload], [7])

    def test_update_user_activity_async_delegates(self):
        rows = [(1, "2026-08-13T00:00:00+00:00", 3)]
        with mock.patch.object(Database, "update_user_activity", return_value=True) as m:
            self.assertTrue(asyncio.run(Database.update_user_activity_async(rows)))
        m.assert_called_once_with(rows)

    def test_get_user_stats_async_delegates(self):
        with mock.patch.object(Database, "get_user_stats", return_value={"total_users": 10}) as m:
            self.assertEqual(asyncio.run(Database.get_user_stats_async()), {"total_users": 10})
        m.assert_called_once()

    def test_get_counts_async_delegates(self):
        with mock.patch.object(Database, "get_counts", return_value={"subscribers": 5}) as m:
            self.assertEqual(asyncio.run(Database.get_counts_async()), {"subscribers": 5})
        m.assert_called_once()

    # ===================== WATCHLIST / PROFILE / COT CACHE =====================

    def test_watchlist_async_delegates(self):
        with mock.patch.object(Database, "add_watchlist_symbol", return_value=True) as m1, \
             mock.patch.object(Database, "remove_watchlist_symbol", return_value=True) as m2, \
             mock.patch.object(Database, "get_watchlist", return_value=["EUR/USD"]) as m3, \
             mock.patch.object(Database, "clear_watchlist", return_value=True) as m4:
            self.assertTrue(asyncio.run(Database.add_watchlist_symbol_async(42, "EUR/USD")))
            self.assertTrue(asyncio.run(Database.remove_watchlist_symbol_async(42, "EUR/USD")))
            self.assertEqual(asyncio.run(Database.get_watchlist_async(42)), ["EUR/USD"])
            self.assertTrue(asyncio.run(Database.clear_watchlist_async(42)))
        m1.assert_called_once_with(42, "EUR/USD")
        m2.assert_called_once_with(42, "EUR/USD")
        m3.assert_called_once_with(42)
        m4.assert_called_once_with(42)

    def test_user_profile_async_delegates(self):
        profile = {"balance": 1000.0, "risk_per_trade": 2.0}
        with mock.patch.object(Database, "upsert_user_profile", return_value=True) as m1, \
             mock.patch.object(Database, "get_user_profile", return_value=profile) as m2, \
             mock.patch.object(Database, "delete_user_profile", return_value=True) as m3:
            self.assertTrue(asyncio.run(Database.upsert_user_profile_async(42, profile)))
            self.assertEqual(asyncio.run(Database.get_user_profile_async(42)), profile)
            self.assertTrue(asyncio.run(Database.delete_user_profile_async(42)))
        m1.assert_called_once_with(42, profile)
        m2.assert_called_once_with(42)
        m3.assert_called_once_with(42)

    def test_cot_cache_async_delegates(self):
        with mock.patch.object(Database, "get_cot_cache", return_value={"market_key": "cot:gold"}) as m1, \
             mock.patch.object(Database, "set_cot_cache", return_value=True) as m2:
            self.assertEqual(asyncio.run(Database.get_cot_cache_async("cot:gold")), {"market_key": "cot:gold"})
            self.assertTrue(asyncio.run(Database.set_cot_cache_async("cot:gold", {"a": 1})))
        m1.assert_called_once_with("cot:gold")
        m2.assert_called_once_with("cot:gold", {"a": 1}, 7 * 24 * 3600)

    def test_add_watchlist_requires_configured_db(self):
        sess = mock.Mock()
        post_fake = mock.Mock()
        post_fake.raise_for_status = mock.Mock()
        sess.post.return_value = post_fake
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("data.database._session", return_value=sess):
            self.assertTrue(Database.add_watchlist_symbol(42, "EUR/USD"))
        payload = sess.post.call_args.kwargs["json"]
        self.assertEqual(payload["user_id"], 42)
        self.assertEqual(payload["symbol"], "EUR/USD")
        self.assertIn("merge-duplicates", sess.post.call_args.kwargs["headers"]["Prefer"])

    def test_get_watchlist_orders_by_symbol(self):
        sess = mock.Mock()
        resp = mock.Mock()
        resp.json.return_value = [{"symbol": "EUR/USD"}, {"symbol": "XAU/USD (Gold)"}]
        sess.get.return_value = resp
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("data.database._session", return_value=sess):
            self.assertEqual(Database.get_watchlist(42), ["EUR/USD", "XAU/USD (Gold)"])
        self.assertIn("order=symbol.asc", sess.get.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
