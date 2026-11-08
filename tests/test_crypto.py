"""USDT-TRC20 收款：唯一金额、链上匹配、迟到/滞后付款与后台补单。

链上接口全部用假数据替换，不访问网络。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app import crypto
from app.db import get_db, utcnow
from app.main import create_app, expire_stale_orders, import_cards

from tests.test_commerce import FormTokens

ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = ROOT / ".review-artifacts" / "tmp"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def tron_address(seed: bytes) -> str:
    payload = b"\x41" + hashlib.sha256(seed).digest()[:20]
    raw = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    number = int.from_bytes(raw, "big")
    out = ""
    while number:
        number, rem = divmod(number, 58)
        out = B58[rem] + out
    return out


SHOP_ADDRESS = tron_address(b"shop")
BUYER_ADDRESS = tron_address(b"buyer")


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


class CryptoPaymentTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="crypto-", dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        env = {
            "DATABASE_PATH": str(Path(self.temp.name) / "shop.sqlite3"),
            "APP_SECRET": "isolated-crypto-test-secret",
            "ADMIN_USERNAME": "operator@example.com",
            "ADMIN_PASSWORD": "test-operator-password",
            "ADMIN_PATH": "/test-admin",
            "ENABLE_MOCK_PAYMENTS": "",
            "CRYPTO_WATCH_INTERVAL": "0",
        }
        self.environment = patch.dict(os.environ, env)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        config = dict(crypto.DEFAULT_CONFIG, address=SHOP_ADDRESS, cny_per_usdt="7.20")
        self.execute(
            "UPDATE payment_providers SET is_enabled = 1, config_json = ? WHERE code = 'usdt_trc20'",
            (json.dumps(config),),
        )
        with self.app.app_context():
            db = get_db()
            now = utcnow()
            category = db.execute("SELECT id FROM categories LIMIT 1").fetchone()[0]
            self.product = db.execute(
                """INSERT INTO products (category_id, name, price_cents, status, buy_limit,
                       created_at, updated_at)
                   VALUES (?, 'USDT product', 1234, 'active', 5, ?, ?)""",
                (category, now, now),
            ).lastrowid
            import_cards(self.product, "\n".join(f"SECRET-{i}" for i in range(4)))

    # ------------------------------------------------------------ helpers

    def execute(self, sql, params=()):
        with self.app.app_context():
            get_db().execute(sql, params)

    def rows(self, sql, params=()):
        with self.app.app_context():
            return [dict(r) for r in get_db().execute(sql, params).fetchall()]

    def order_row(self, order_no):
        return self.rows("SELECT * FROM orders WHERE order_no = ?", (order_no,))[0]

    def token(self, client, page):
        return FormTokens(client.get(page).get_data(as_text=True)).token

    def place_order(self, client=None, quantity=1):
        client = client or self.client
        response = client.post("/orders", data={
            "product_id": str(self.product),
            "quantity": str(quantity),
            "contact": "buyer@example.com",
            "payment_provider": "usdt_trc20",
            "csrf_token": self.token(client, f"/buy/{self.product}"),
        })
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        return response.location.rsplit("/", 1)[1]

    def transfer(self, units, *, tx="TX1", when=None):
        return crypto.Transfer(tx, BUYER_ADDRESS, SHOP_ADDRESS, units, when or utcnow())

    def scan(self, *transfers):
        with self.app.app_context():
            provider = get_db().execute(
                "SELECT * FROM payment_providers WHERE code = 'usdt_trc20'"
            ).fetchone()
            return crypto.scan_provider(provider, fetch=lambda cfg, since: list(transfers))

    def admin_client(self):
        admin = self.app.test_client()
        admin.post("/test-admin/login", data={
            "username": "operator@example.com",
            "password": "test-operator-password",
            "csrf_token": self.token(admin, "/test-admin/login"),
        })
        return admin

    def backdate(self, order_no, *, created_ago, expires_ago):
        now = datetime.now(UTC)
        self.execute(
            "UPDATE orders SET created_at = ?, expires_at = ? WHERE order_no = ?",
            (iso(now - created_ago), iso(now - expires_ago), order_no),
        )

    # ------------------------------------------------------------ tests

    def test_address_validation(self):
        self.assertTrue(crypto.is_valid_tron_address(crypto.MAINNET_USDT_CONTRACT))
        self.assertTrue(crypto.is_valid_tron_address(SHOP_ADDRESS))
        broken = SHOP_ADDRESS[:-1] + ("A" if SHOP_ADDRESS[-1] != "A" else "B")
        self.assertFalse(crypto.is_valid_tron_address(broken))
        self.assertFalse(crypto.is_valid_tron_address("0x" + "1" * 40))
        with self.assertRaises(crypto.CryptoConfigError):
            crypto.parse_config({"address": "", "cny_per_usdt": "7.2"})
        with self.assertRaises(crypto.CryptoConfigError):
            crypto.parse_config({"address": SHOP_ADDRESS, "cny_per_usdt": "0"})

    def test_cannot_enable_channel_without_valid_address(self):
        self.execute(
            "UPDATE payment_providers SET is_enabled = 0, config_json = '{}' WHERE code = 'usdt_trc20'"
        )
        admin = self.admin_client()
        admin.post("/test-admin/payments/usdt_trc20/toggle", data={
            "csrf_token": self.token(admin, "/test-admin/payments"),
        })
        enabled = self.rows("SELECT is_enabled FROM payment_providers WHERE code = 'usdt_trc20'")
        self.assertEqual(enabled[0]["is_enabled"], 0)

    def test_concurrent_orders_get_distinct_amounts(self):
        first = self.place_order()
        second = self.place_order()
        # 12.34 CNY / 7.20 = 1.7139 -> 向上取到 1.72，第二单加一个步长。
        self.assertEqual(self.order_row(first)["crypto_amount"], 1_720_000)
        self.assertEqual(self.order_row(second)["crypto_amount"], 1_730_000)
        self.assertEqual(self.order_row(first)["crypto_address"], SHOP_ADDRESS)
        page = self.client.get(f"/checkout/{first}").get_data(as_text=True)
        self.assertIn("1.72", page)
        self.assertIn(SHOP_ADDRESS, page)
        self.assertIn("<svg", page)

    def test_exact_transfer_fulfills_once(self):
        order_no = self.place_order()
        units = self.order_row(order_no)["crypto_amount"]
        self.scan(self.transfer(units))
        order = self.order_row(order_no)
        self.assertEqual(order["status"], "fulfilled")
        self.assertEqual(order["payment_trade_no"], "TX1")
        self.assertTrue(order["delivered_content"].startswith("SECRET-"))
        # 重复扫描不会重复发卡或重复记账
        self.scan(self.transfer(units))
        self.assertEqual(len(self.rows("SELECT * FROM payment_events")), 1)
        self.assertEqual(self.rows("SELECT status FROM crypto_transfers")[0]["status"], "matched")
        self.assertEqual(len(self.rows("SELECT * FROM cards WHERE status = 'sold'")), 1)

    def test_wrong_amount_stays_unmatched_until_admin_assigns(self):
        order_no = self.place_order()
        self.scan(self.transfer(1_700_000))
        self.assertEqual(self.order_row(order_no)["status"], "pending")
        transfer = self.rows("SELECT * FROM crypto_transfers")[0]
        self.assertEqual(transfer["status"], "unmatched")

        admin = self.admin_client()
        response = admin.post(f"/test-admin/crypto-transfers/{transfer['id']}/assign", data={
            "order_no": order_no,
            "csrf_token": self.token(admin, "/test-admin/crypto-transfers"),
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.order_row(order_no)["status"], "fulfilled")
        self.assertEqual(self.rows("SELECT status FROM crypto_transfers")[0]["status"], "matched")

    def test_one_transfer_cannot_pay_two_orders(self):
        first = self.place_order()
        second = self.place_order()
        self.scan(self.transfer(self.order_row(first)["crypto_amount"]))
        transfer = self.rows("SELECT * FROM crypto_transfers")[0]
        with self.app.app_context(), self.assertRaises(ValueError):
            crypto.assign_transfer("usdt_trc20", transfer["tx_id"], second, admin_override=True)
        self.assertEqual(self.order_row(second)["status"], "pending")

    def test_on_time_payment_seen_after_expiry_is_still_delivered(self):
        order_no = self.place_order()
        units = self.order_row(order_no)["crypto_amount"]
        self.backdate(order_no, created_ago=timedelta(minutes=20), expires_ago=timedelta(minutes=5))
        with self.app.app_context():
            expire_stale_orders()
        self.assertEqual(self.order_row(order_no)["status"], "expired")
        self.assertEqual(len(self.rows("SELECT * FROM cards WHERE status = 'reserved'")), 0)

        paid = iso(datetime.now(UTC) - timedelta(minutes=10))
        self.scan(self.transfer(units, when=paid))
        order = self.order_row(order_no)
        self.assertEqual(order["status"], "fulfilled")
        self.assertEqual(order["error_message"], "")

    def test_late_payment_is_recorded_not_delivered_then_admin_can_fulfil(self):
        order_no = self.place_order()
        units = self.order_row(order_no)["crypto_amount"]
        self.backdate(order_no, created_ago=timedelta(minutes=40), expires_ago=timedelta(minutes=25))
        with self.app.app_context():
            expire_stale_orders()
        self.scan(self.transfer(units, when=utcnow()))
        order = self.order_row(order_no)
        self.assertEqual(order["status"], "abnormal")
        self.assertEqual(order["delivered_content"], "")
        self.assertEqual(self.rows("SELECT status FROM crypto_transfers")[0]["status"], "late")

        admin = self.admin_client()
        admin.post(f"/test-admin/orders/{order_no}/confirm-paid", data={
            "reference": "TX1",
            "csrf_token": self.token(admin, "/test-admin/orders"),
        })
        self.assertEqual(self.order_row(order_no)["status"], "fulfilled")

    def test_slot_of_recently_expired_order_is_not_reused(self):
        first = self.place_order()
        self.backdate(first, created_ago=timedelta(minutes=20), expires_ago=timedelta(minutes=5))
        with self.app.app_context():
            expire_stale_orders()
        second = self.place_order()
        self.assertNotEqual(
            self.order_row(first)["crypto_amount"], self.order_row(second)["crypto_amount"]
        )

    def test_status_endpoint_triggers_scan(self):
        order_no = self.place_order()
        units = self.order_row(order_no)["crypto_amount"]
        with patch.object(crypto, "fetch_transfers", return_value=[self.transfer(units)]):
            response = self.client.get(f"/checkout/{order_no}/status")
        self.assertEqual(response.get_json(), {"status": "fulfilled"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_no_network_call_without_open_orders(self):
        with patch.object(crypto, "fetch_transfers", side_effect=AssertionError("network")):
            result = self.scan()
        self.assertFalse(result["scanned"])

    def test_trongrid_response_parsing_and_pagination(self):
        cfg = crypto.parse_config({"address": SHOP_ADDRESS})
        usdt = {"address": crypto.MAINNET_USDT_CONTRACT, "decimals": 6, "symbol": "USDT"}
        fake = {"address": tron_address(b"fake-token"), "decimals": 6, "symbol": "USDT"}
        pages = [
            {"success": True, "meta": {"fingerprint": "next"}, "data": [
                {"transaction_id": "A", "type": "Transfer", "from": BUYER_ADDRESS,
                 "to": SHOP_ADDRESS, "value": "1720000", "block_timestamp": 1_762_000_000_000,
                 "token_info": usdt},
                {"transaction_id": "FAKE", "type": "Transfer", "from": BUYER_ADDRESS,
                 "to": SHOP_ADDRESS, "value": "1720000", "block_timestamp": 1_762_000_000_000,
                 "token_info": fake},
            ]},
            {"success": True, "meta": {}, "data": [
                {"transaction_id": "B", "type": "Transfer", "from": BUYER_ADDRESS,
                 "to": SHOP_ADDRESS, "value": "5000000", "block_timestamp": 1_762_000_060_000,
                 "token_info": usdt},
            ]},
        ]
        seen_urls = []

        def fake_urlopen(request, timeout):
            seen_urls.append(request.full_url)
            return io.BytesIO(json.dumps(pages[len(seen_urls) - 1]).encode())

        with patch("urllib.request.urlopen", fake_urlopen):
            transfers = crypto.fetch_transfers(cfg, datetime.now(UTC))
        self.assertEqual([t.tx_id for t in transfers], ["A", "B"])  # 假币被过滤
        self.assertEqual(transfers[0].units, 1_720_000)
        self.assertIn("only_confirmed=true", seen_urls[0])
        self.assertIn("fingerprint=next", seen_urls[1])


if __name__ == "__main__":
    unittest.main()
