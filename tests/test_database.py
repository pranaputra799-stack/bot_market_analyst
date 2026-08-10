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
        # ...lalu prune via DELETE yang SELALU punya WHERE clause
        # (lolos proteksi Supabase "DELETE requires a WHERE clause" / 400 21000).
        sess.delete.assert_called_once()
        del_url = sess.delete.call_args.args[0]
        self.assertIn("key=not.in.(a,b)", del_url)

    def test_save_event_alert_notified_url_encodes_key_values(self):
        """Kunci mengandung karakter khusus ('|', ':', '+') yang harus di-URL-
        encode dalam filter not.in.() agar query string tidak rusak."""
        sess = mock.Mock()
        delete_fake = mock.Mock()
        delete_fake.raise_for_status = mock.Mock()
        sess.delete.return_value = delete_fake
        post_fake = mock.Mock()
        post_fake.raise_for_status = mock.Mock()
        sess.post.return_value = post_fake
        key = "NFP|2026-08-10T12:30:00+00:00"
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("data.database._session", return_value=sess):
            self.assertTrue(Database.save_event_alert_notified({key}))
        del_url = sess.delete.call_args.args[0]
        self.assertIn("%7C", del_url)  # '|'
        self.assertIn("%3A", del_url)  # ':'
        self.assertIn("%2B", del_url)  # '+'
        # Nilai asli tidak boleh muncul mentah (harus ter-encode).
        self.assertNotIn(key, del_url)

    def test_save_event_alert_subscribers_replace_all(self):
        sess = mock.Mock()
        delete_fake = mock.Mock()
        delete_fake.raise_for_status = mock.Mock()
        sess.delete.return_value = delete_fake
        post_fake = mock.Mock()
        post_fake.raise_for_status = mock.Mock()
        sess.post.return_value = post_fake
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
        # Prune: DELETE dengan WHERE clause, bukan DELETE massal tanpa filter.
        sess.delete.assert_called_once()
        del_url = sess.delete.call_args.args[0]
        self.assertIn("chat_id=not.in.(1,2)", del_url)

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
        self.assertIn("chat_id=not.in.(2,5)", sess.delete.call_args.args[0])

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
        """>200 chat_id => DELETE prune dipecah per 200 agar URL tidak
        kepanjangan (2 panggilan DELETE untuk 250 id)."""
        sess = mock.Mock()
        delete_fake = mock.Mock()
        delete_fake.raise_for_status = mock.Mock()
        sess.delete.return_value = delete_fake
        post_fake = mock.Mock()
        post_fake.raise_for_status = mock.Mock()
        sess.post.return_value = post_fake
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("data.database._session", return_value=sess):
            self.assertTrue(Database.save_event_alert_subscribers(set(range(250))))
        self.assertEqual(sess.post.call_count, 1)
        self.assertEqual(sess.delete.call_count, 2)


if __name__ == "__main__":
    unittest.main()
