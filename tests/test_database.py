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

    def test_price_alerts_async_delegates(self):
        rows = [{"id": 1, "chat_id": 777, "user_id": 9999, "symbol": "EURUSD=X",
                 "display_name": "EUR/USD", "target": 1.1, "direction": "above"}]
        with mock.patch.object(Database, "get_price_alerts", return_value=rows) as m1, \
             mock.patch.object(Database, "save_price_alerts", return_value=True) as m2:
            self.assertEqual(asyncio.run(Database.get_price_alerts_async()), rows)
            self.assertTrue(asyncio.run(Database.save_price_alerts_async(rows)))
        m1.assert_called_once()
        m2.assert_called_once_with(rows)

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
        sess.delete.assert_called_once()
        sess.post.assert_called_once()
        payload = sess.post.call_args.kwargs["json"]
        self.assertEqual(payload, [{"key": "a"}, {"key": "b"}])

    def test_get_price_alerts_parses_rows_and_skips_malformed(self):
        """Parsing baris DB → dict alert; baris rusak dilewati tanpa crash."""
        fake = mock.Mock()
        fake.raise_for_status = mock.Mock()
        fake.json = mock.Mock(return_value=[
            {"id": "1", "chat_id": 777, "user_id": 9999, "symbol": "EURUSD=X",
             "display_name": "EUR/USD", "target": "1.1", "direction": "above"},
            {"id": "bogus", "chat_id": 1, "user_id": 1, "symbol": "X",
             "target": 1.0, "direction": "above"},
        ])
        sess = mock.Mock()
        sess.get.return_value = fake
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("data.database._session", return_value=sess):
            alerts = Database.get_price_alerts()
        sess.get.assert_called_once()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["symbol"], "EURUSD=X")
        self.assertEqual(alerts[0]["target"], 1.1)

    def test_save_price_alerts_replace_all(self):
        """save_price_alerts menghapus semua lalu insert ulang (bulk)."""
        sess = mock.Mock()
        delete_fake = mock.Mock()
        delete_fake.raise_for_status = mock.Mock()
        sess.delete.return_value = delete_fake
        post_fake = mock.Mock()
        post_fake.raise_for_status = mock.Mock()
        sess.post.return_value = post_fake
        with mock.patch("data.database._is_configured", return_value=True), \
             mock.patch("data.database._session", return_value=sess):
            ok = Database.save_price_alerts([
                {"id": 1, "chat_id": 7, "user_id": 9, "symbol": "GC=F",
                 "display_name": "Gold", "target": 2350.0, "direction": "below"},
            ])
        self.assertTrue(ok)
        sess.delete.assert_called_once()
        sess.post.assert_called_once()
        payload = sess.post.call_args.kwargs["json"]
        self.assertEqual(payload[0]["target"], 2350.0)

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
        sess.delete.assert_called_once()
        sess.post.assert_called_once()
        payload = sess.post.call_args.kwargs["json"]
        self.assertEqual(payload, [{"chat_id": 1}, {"chat_id": 2}])


if __name__ == "__main__":
    unittest.main()
