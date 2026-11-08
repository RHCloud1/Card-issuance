"""Regression checks for checkout authorization and inventory integrity.

Run from the repository root with ``python -m unittest discover -s tests -v``.
Every database and temporary file stays beneath .review-artifacts/tmp.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from app.db import get_db, utcnow
from app.main import create_app, create_order, fulfill_order, import_cards


ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = ROOT / ".review-artifacts" / "tmp"


class FormTokens(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.token = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "input" and fields.get("name") == "csrf_token":
            self.token = fields.get("value")


class CommerceTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="commerce-", dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "shop.sqlite3"
        env = {
            "DATABASE_PATH": str(self.database),
            "APP_SECRET": "isolated-regression-test-session-secret",
            "ADMIN_USERNAME": "operator@example.com",
            "ADMIN_PASSWORD": "test-operator-password",
            "ADMIN_PATH": "/test-admin",
            "ENABLE_MOCK_PAYMENTS": "true",
            "ORDER_EXPIRE_MINUTES": "15",
            "TEMP": self.temp.name,
            "TMP": self.temp.name,
        }
        self.environment = patch.dict(os.environ, env)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.product = self.seed_product(3)

    def seed_product(self, count: int) -> int:
        with self.app.app_context():
            db = get_db()
            category = db.execute("SELECT id FROM categories LIMIT 1").fetchone()[0]
            now = utcnow()
            result = db.execute(
                """INSERT INTO products
                   (category_id, name, price_cents, status, buy_limit, created_at, updated_at)
                   VALUES (?, 'Regression product', 1234, 'active', 10, ?, ?)""",
                (category, now, now),
            )
            product_id = result.lastrowid
            import_cards(
                product_id,
                "\n".join(f"PRIVATE-CARD-{product_id}-{index}" for index in range(count)),
            )
            return product_id

    def rows(self, sql: str, params=()):
        with self.app.app_context():
            return [dict(row) for row in get_db().execute(sql, params).fetchall()]

    def execute(self, sql: str, params=()):
        with self.app.app_context():
            get_db().execute(sql, params)

    def token(self, client, page="/order-query") -> str:
        response = client.get(page)
        self.assertEqual(response.status_code, 200)
        token = FormTokens(response.get_data(as_text=True)).token
        self.assertTrue(token, f"No CSRF input at {page}")
        return token

    def post_form(self, client, path, data=None, *, token_page="/order-query"):
        values = dict(data or {})
        values["csrf_token"] = self.token(client, token_page)
        return client.post(path, data=values, follow_redirects=True)

    def order(self, *, client=None, password="buyer-password", quantity=1,
              provider="mock", product=None):
        client = client or self.client
        product = product or self.product
        values = {
            "product_id": str(product),
            "quantity": str(quantity),
            "contact": "buyer@example.com",
            "query_password": password,
            "payment_provider": provider,
            "csrf_token": self.token(client, f"/buy/{product}"),
        }
        response = client.post("/orders", data=values)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/checkout/", response.location)
        return response.location.rsplit("/", 1)[1]

    def fulfill(self, order_no, *, provider="mock", trade="REGRESSION-TRADE", amount=1234):
        with self.app.app_context():
            return dict(fulfill_order(
                order_no=order_no, provider_code=provider, trade_no=trade,
                amount_cents=amount, payload={"test": True},
            ))

    def persisted_order(self, order_no):
        return self.rows("SELECT * FROM orders WHERE order_no = ?", (order_no,))[0]

    def card_contents(self, order_no):
        return [row["content"] for row in self.rows(
            "SELECT content FROM cards WHERE order_id = (SELECT id FROM orders WHERE order_no = ?)",
            (order_no,),
        )]

    def assert_no_cards(self, response, secrets):
        body = response.get_data(as_text=True)
        for secret in secrets:
            self.assertNotIn(secret, body)

    def test_protected_checkout_creator_and_password_unlock(self):
        order_no = self.order()
        secrets = self.card_contents(order_no)
        paid = self.post_form(self.client, f"/payments/mock/succeed/{order_no}")
        self.assertEqual(paid.status_code, 200)
        self.assertEqual(self.persisted_order(order_no)["status"], "fulfilled")
        self.assertIn(secrets[0], paid.get_data(as_text=True))

        stranger = self.app.test_client()
        self.assert_no_cards(stranger.get(f"/checkout/{order_no}"), secrets)
        wrong = self.post_form(stranger, f"/checkout/{order_no}", {"query_password": "wrong-password"})
        self.assert_no_cards(wrong, secrets)
        self.assert_no_cards(stranger.get(f"/checkout/{order_no}"), secrets)
        unlocked = self.post_form(stranger, f"/checkout/{order_no}", {"query_password": "buyer-password"})
        self.assertIn(secrets[0], unlocked.get_data(as_text=True))
        self.assertIn(secrets[0], stranger.get(f"/checkout/{order_no}").get_data(as_text=True))

    def test_email_query_cannot_bypass_password_via_checkout(self):
        order_no = self.order()
        self.fulfill(order_no)
        secrets = self.card_contents(order_no)
        stranger = self.app.test_client()
        for password in ("", "wrong-password"):
            with self.subTest(password=password):
                response = self.post_form(stranger, "/order-query", {
                    "contact": "buyer@example.com", "query_password": password,
                })
                self.assertIn(order_no, response.get_data(as_text=True))
                self.assert_no_cards(response, secrets)
                self.assert_no_cards(stranger.get(f"/checkout/{order_no}"), secrets)

        response = self.post_form(stranger, "/order-query", {
            "contact": "buyer@example.com", "query_password": "buyer-password",
        })
        self.assertIn(secrets[0], response.get_data(as_text=True))
        self.assertIn(secrets[0], stranger.get(f"/checkout/{order_no}").get_data(as_text=True))

    def test_password_authorization_is_limited_to_matching_orders(self):
        first = self.order(password="first-password")
        second = self.order(password="second-password")
        self.fulfill(first, trade="FIRST-TRADE")
        self.fulfill(second, trade="SECOND-TRADE")
        first_secret = self.card_contents(first)[0]
        second_secret = self.card_contents(second)[0]
        stranger = self.app.test_client()
        response = self.post_form(stranger, "/order-query", {
            "contact": "buyer@example.com", "query_password": "first-password",
        })
        self.assertIn(first_secret, response.get_data(as_text=True))
        self.assertNotIn(second_secret, response.get_data(as_text=True))
        self.assert_no_cards(stranger.get(f"/checkout/{second}"), [second_secret])

    def test_mock_is_disabled_without_explicit_environment_opt_in(self):
        existing_order = self.order()
        # Leave the database channel enabled: the deployment switch must still win.
        with patch.dict(os.environ):
            os.environ.pop("ENABLE_MOCK_PAYMENTS", None)
            safe_app = create_app()
            safe_app.config.update(TESTING=True)
            self.execute("UPDATE payment_providers SET is_enabled = 1 WHERE code = 'mock'")
            client = safe_app.test_client()
            response = self.post_form(client, f"/payments/mock/succeed/{existing_order}")
            self.assertNotEqual(self.persisted_order(existing_order)["status"], "fulfilled")
            self.assert_no_cards(response, self.card_contents(existing_order))
            before = len(self.rows("SELECT id FROM orders"))
            self.post_form(client, "/orders", {
                "product_id": self.product, "quantity": "1", "contact": "buyer@example.com",
                "payment_provider": "mock",
            })
            self.assertEqual(len(self.rows("SELECT id FROM orders")), before)
            self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM cards WHERE status = 'sold'")[0]["n"], 0)
            with safe_app.app_context():
                with self.assertRaises(ValueError):
                    fulfill_order(order_no=existing_order, provider_code="mock", trade_no="DISABLED-TRADE",
                                  amount_cents=1234, payload={})

    def test_admin_login_rejects_external_redirects_and_preserves_local_destination(self):
        targets = (
            "https://evil.example/steal", "//evil.example/steal", "/\\evil.example/steal",
            "/%2f%2fevil.example/steal", "/test-admin/orders",
        )
        for target in targets:
            with self.subTest(target=target):
                client = self.app.test_client()
                response = client.post("/test-admin/login?" + urlencode({"next": target}), data={
                    "username": "operator@example.com", "password": "test-operator-password",
                    "csrf_token": self.token(client, "/test-admin/login"),
                })
                self.assertEqual(response.status_code, 302)
                expected = "/test-admin/orders" if target == "/test-admin/orders" else "/test-admin"
                self.assertEqual(response.location, expected)

    def test_disabling_mock_channel_blocks_existing_order_payment(self):
        order_no = self.order()
        self.execute("UPDATE payment_providers SET is_enabled = 0 WHERE code = 'mock'")
        response = self.post_form(self.client, f"/payments/mock/succeed/{order_no}")
        self.assertNotEqual(self.persisted_order(order_no)["status"], "fulfilled")
        self.assert_no_cards(response, self.card_contents(order_no))
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM payment_events")[0]["n"], 0)

    def test_mock_endpoint_cannot_fulfill_real_provider_order(self):
        self.execute("UPDATE payment_providers SET is_enabled = 1 WHERE code = 'alipay'")
        order_no = self.order(provider="alipay")
        response = self.post_form(self.client, f"/payments/mock/succeed/{order_no}")
        self.assert_no_cards(response, self.card_contents(order_no))
        self.assertEqual(self.persisted_order(order_no)["status"], "pending")
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM payment_events")[0]["n"], 0)

    def test_fulfillment_rejects_mismatched_provider(self):
        self.execute("UPDATE payment_providers SET is_enabled = 1 WHERE code = 'alipay'")
        order_no = self.order(provider="alipay")
        with self.assertRaises(ValueError):
            self.fulfill(order_no, provider="mock")
        order = self.persisted_order(order_no)
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["delivered_content"], "")
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM payment_events")[0]["n"], 0)

    def test_mock_payment_requires_csrf_even_when_enabled(self):
        order_no = self.order()
        response = self.client.post(f"/payments/mock/succeed/{order_no}")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.persisted_order(order_no)["status"], "pending")

    def test_admin_cannot_void_reserved_inventory_but_can_void_available_card(self):
        order_no = self.order()
        reserved = self.rows("SELECT id FROM cards WHERE status = 'reserved'")[0]["id"]
        available = self.rows("SELECT id FROM cards WHERE status = 'available' LIMIT 1")[0]["id"]
        admin = self.app.test_client()
        login = self.post_form(admin, "/test-admin/login", {
            "username": "operator@example.com", "password": "test-operator-password",
        }, token_page="/test-admin/login")
        self.assertEqual(login.status_code, 200)
        self.assertNotIn("/login", login.request.path)
        self.post_form(admin, f"/test-admin/cards/{reserved}/void")
        self.assertEqual(self.rows("SELECT status FROM cards WHERE id = ?", (reserved,))[0]["status"], "reserved")
        self.post_form(admin, f"/test-admin/cards/{available}/void")
        self.assertEqual(self.rows("SELECT status FROM cards WHERE id = ?", (available,))[0]["status"], "void")
        self.assertEqual(self.fulfill(order_no)["status"], "fulfilled")

    def test_amount_mismatch_releases_reservations_without_delivery(self):
        order_no = self.order(quantity=2)
        reserved_ids = [row["id"] for row in self.rows("SELECT id FROM cards WHERE status = 'reserved'")]
        result = self.fulfill(order_no, amount=1)
        self.assertEqual(result["status"], "abnormal")
        self.assertEqual(result["delivered_content"], "")
        for card_id in reserved_ids:
            card = self.rows("SELECT * FROM cards WHERE id = ?", (card_id,))[0]
            self.assertEqual(card["status"], "available")
            self.assertIsNone(card["order_id"])
            self.assertIsNone(card["reserved_until"])

    def test_inventory_inconsistency_releases_remaining_reservations(self):
        order_no = self.order(quantity=2)
        cards = self.rows("SELECT id FROM cards WHERE status = 'reserved' ORDER BY id")
        # Simulate preexisting corrupted inventory, independent of the fixed admin UI.
        self.execute("UPDATE cards SET status = 'void' WHERE id = ?", (cards[0]["id"],))
        result = self.fulfill(order_no, amount=2468)
        self.assertEqual(result["status"], "abnormal")
        self.assertEqual(result["delivered_content"], "")
        remaining = self.rows("SELECT * FROM cards WHERE id = ?", (cards[1]["id"],))[0]
        self.assertEqual(remaining["status"], "available")
        self.assertIsNone(remaining["order_id"])
        self.assertEqual(self.rows("SELECT status FROM cards WHERE id = ?", (cards[0]["id"],))[0]["status"], "void")

    def test_expiration_releases_stock_and_late_payment_never_delivers(self):
        order_no = self.order()
        self.execute("UPDATE orders SET expires_at = '2000-01-01T00:00:00+00:00' WHERE order_no = ?", (order_no,))
        self.client.get("/")
        self.assertEqual(self.persisted_order(order_no)["status"], "expired")
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM cards WHERE status = 'reserved'")[0]["n"], 0)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM cards WHERE status = 'available'")[0]["n"], 3)
        self.fulfill(order_no)
        order = self.persisted_order(order_no)
        self.assertNotEqual(order["status"], "fulfilled")
        self.assertEqual(order["delivered_content"], "")
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM cards WHERE status = 'sold'")[0]["n"], 0)

    def test_repeated_fulfillment_keeps_one_delivery_and_one_payment_event(self):
        order_no = self.order()
        first = self.fulfill(order_no)
        self.fulfill(order_no)
        self.fulfill(order_no, trade="REPEATED-OTHER-TRADE")
        order = self.persisted_order(order_no)
        self.assertEqual(order["delivered_content"], first["delivered_content"])
        self.assertEqual(order["payment_trade_no"], first["payment_trade_no"])
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM cards WHERE status = 'sold'")[0]["n"], 1)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM payment_events")[0]["n"], 1)

    def test_one_provider_trade_cannot_pay_two_orders(self):
        first = self.order()
        second = self.order()
        self.fulfill(first)
        try:
            self.fulfill(second)
        except ValueError:
            pass  # Rejecting the replay explicitly is also a valid response.
        second_order = self.persisted_order(second)
        self.assertNotEqual(second_order["status"], "fulfilled")
        self.assertEqual(second_order["delivered_content"], "")
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM cards WHERE status = 'sold'")[0]["n"], 1)

    def test_insufficient_stock_rolls_back_without_partial_reservations(self):
        product = self.seed_product(1)
        with self.app.app_context():
            with self.assertRaises(ValueError):
                create_order(product_id=product, quantity=2, contact="buyer@example.com",
                             email="buyer@example.com", query_password="", provider_code="mock")
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM orders WHERE product_id = ?", (product,))[0]["n"], 0)
        self.assertEqual(self.rows("SELECT status FROM cards WHERE product_id = ?", (product,))[0]["status"], "available")

    def test_concurrent_buyers_cannot_reserve_the_same_last_card(self):
        product = self.seed_product(1)
        barrier = threading.Barrier(2)

        def reserve(index):
            with self.app.app_context():
                barrier.wait(timeout=10)
                try:
                    return create_order(
                        product_id=product, quantity=1, contact=f"buyer{index}@example.com",
                        email=f"buyer{index}@example.com", query_password="", provider_code="mock",
                    )
                except ValueError:
                    return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reserve, range(2)))
        self.assertEqual(sum(result is not None for result in outcomes), 1)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM orders WHERE product_id = ?", (product,))[0]["n"], 1)
        cards = self.rows("SELECT * FROM cards WHERE product_id = ?", (product,))
        self.assertEqual(cards[0]["status"], "reserved")

    def test_simultaneous_duplicate_callbacks_issue_only_one_card(self):
        order_no = self.order()
        barrier = threading.Barrier(2)

        def pay(_):
            barrier.wait(timeout=10)
            return self.fulfill(order_no)["status"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(pay, range(2))), ["fulfilled", "fulfilled"])
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM cards WHERE status = 'sold'")[0]["n"], 1)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM payment_events")[0]["n"], 1)


if __name__ == "__main__":
    unittest.main()
