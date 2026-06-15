from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .db import close_db, fetch_all, fetch_one, get_db, init_db, utcnow
from .formatting import cents_to_yuan, yuan_to_cents
from .security import csrf_token, hash_secret, login_required, require_csrf, verify_secret


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("APP_SECRET", secrets.token_urlsafe(48)),
        APP_NAME=os.getenv("APP_NAME", "Card Issuance"),
        APP_BASE_URL=os.getenv("APP_BASE_URL", "http://localhost:8080").rstrip("/"),
        DATABASE_PATH=os.getenv(
            "DATABASE_PATH",
            str(Path.cwd() / "data" / "card_issuance.sqlite3"),
        ),
        ADMIN_PATH=normalize_admin_path(os.getenv("ADMIN_PATH", "/admin")),
        ORDER_EXPIRE_MINUTES=int(os.getenv("ORDER_EXPIRE_MINUTES", "15")),
    )
    admin_path = app.config["ADMIN_PATH"]

    app.teardown_appcontext(close_db)
    app.jinja_env.filters["yuan"] = cents_to_yuan

    @app.context_processor
    def inject_globals():
        endpoint = request.endpoint or ""
        return {
            "app_name": app.config["APP_NAME"],
            "csrf_token": csrf_token,
            "is_admin_area": endpoint.startswith("admin_"),
        }

    @app.before_request
    def protect_forms():
        exempt = {"payment_notify_placeholder"}
        if request.method == "POST" and request.endpoint not in exempt:
            require_csrf()

    @app.before_request
    def maintain_expirations():
        if request.endpoint not in {"static"}:
            expire_stale_orders()

    @app.route("/")
    def storefront():
        selected_category_id = request.args.get("category_id", type=int)
        sort = request.args.get("sort", "category")
        sort_options = {
            "category": "按类目排序",
            "price_asc": "价格从低到高",
            "price_desc": "价格从高到低",
            "newest": "最新上架",
        }
        if sort not in sort_options:
            sort = "category"

        order_by = {
            "category": "c.sort_order ASC, c.id ASC, p.sort_order ASC, p.id DESC",
            "price_asc": "p.price_cents ASC, p.sort_order ASC, p.id DESC",
            "price_desc": "p.price_cents DESC, p.sort_order ASC, p.id DESC",
            "newest": "p.id DESC",
        }[sort]

        categories = fetch_all(
            """
            SELECT c.*,
                   COUNT(p.id) AS product_count
            FROM categories c
            LEFT JOIN products p
                ON p.category_id = c.id
               AND p.status = 'active'
            WHERE c.is_active = 1
            GROUP BY c.id
            HAVING product_count > 0
            ORDER BY c.sort_order ASC, c.id ASC
            """
        )

        where_parts = ["p.status = 'active'", "c.is_active = 1"]
        params: list[object] = []
        if selected_category_id:
            where_parts.append("p.category_id = ?")
            params.append(selected_category_id)

        products = fetch_all(
            f"""
            SELECT p.*, c.name AS category_name,
                   COALESCE(SUM(CASE WHEN cards.status = 'available' THEN 1 ELSE 0 END), 0) AS stock
            FROM products p
            LEFT JOIN categories c ON c.id = p.category_id
            LEFT JOIN cards ON cards.product_id = p.id
            WHERE {" AND ".join(where_parts)}
            GROUP BY p.id
            ORDER BY {order_by}
            """,
            tuple(params),
        )
        return render_template(
            "storefront.html",
            categories=categories,
            products=products,
            selected_category_id=selected_category_id,
            sort=sort,
            sort_options=sort_options,
        )

    @app.route("/index")
    def legacy_index():
        return redirect(url_for("storefront"), code=301)

    @app.route("/buy/<int:product_id>")
    def buy(product_id: int):
        product = get_product_or_404(product_id, active_only=True)
        stock = available_stock(product_id)
        providers = fetch_all(
            "SELECT * FROM payment_providers WHERE is_enabled = 1 ORDER BY id ASC"
        )
        return render_template(
            "buy.html",
            product=product,
            stock=stock,
            providers=providers,
        )

    @app.post("/orders")
    def create_order_route():
        product_id = int(request.form.get("product_id", "0"))
        quantity = int(request.form.get("quantity", "1"))
        contact = normalize_contact(request.form.get("contact", ""))
        query_password = request.form.get("query_password", "").strip()
        provider_code = request.form.get("payment_provider", "mock").strip()

        try:
            order_no = create_order(
                product_id=product_id,
                quantity=quantity,
                contact=contact,
                email=contact,
                query_password=query_password,
                provider_code=provider_code,
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("buy", product_id=product_id))

        return redirect(url_for("checkout", order_no=order_no))

    @app.route("/checkout/<order_no>")
    def checkout(order_no: str):
        order = get_order_or_404(order_no)
        cards = cards_for_order(order["id"])
        provider = fetch_one(
            "SELECT * FROM payment_providers WHERE code = ?",
            (order["payment_provider"],),
        )
        return render_template(
            "checkout.html",
            order=order,
            cards=cards,
            provider=provider,
        )

    @app.post("/payments/mock/succeed/<order_no>")
    def mock_payment_succeed(order_no: str):
        order = get_order_or_404(order_no)
        trade_no = "MOCK-" + secrets.token_hex(8).upper()
        fulfill_order(
            order_no=order["order_no"],
            provider_code="mock",
            trade_no=trade_no,
            amount_cents=order["amount_cents"],
            payload={"source": "mock_button"},
        )
        flash("模拟支付成功，卡密已自动发放。", "success")
        return redirect(url_for("checkout", order_no=order_no))

    @app.post("/payments/<provider_code>/notify")
    def payment_notify_placeholder(provider_code: str):
        abort(501, f"{provider_code} notify adapter is not implemented yet")

    @app.route("/order-query", methods=["GET", "POST"])
    def order_query():
        found_orders = []
        reveal_cards = {}
        if request.method == "POST":
            order_no = request.form.get("order_no", "").strip().upper()
            contact = normalize_contact(request.form.get("contact", ""))
            query_password = request.form.get("query_password", "").strip()
            if not is_valid_email(contact):
                flash("请填写有效联系邮箱。", "error")
                return render_template("order_query.html", found_orders=found_orders)

            if order_no:
                found_orders = fetch_all(
                    "SELECT * FROM orders WHERE order_no = ? AND contact = ? ORDER BY id DESC",
                    (order_no, contact),
                )
            else:
                found_orders = fetch_all(
                    "SELECT * FROM orders WHERE contact = ? ORDER BY id DESC LIMIT 50",
                    (contact,),
                )

            if not found_orders:
                flash("没有找到匹配的订单。", "error")
            for order in found_orders:
                reveal_cards[order["id"]] = (
                    not order["query_password_hash"]
                    or verify_secret(query_password, order["query_password_hash"])
                )
        return render_template(
            "order_query.html",
            found_orders=found_orders,
            reveal_cards=reveal_cards,
        )

    @app.route("/tickets/new", methods=["GET", "POST"])
    def ticket_new():
        if request.method == "POST":
            contact = normalize_contact(request.form.get("contact", ""))
            order_no = request.form.get("order_no", "").strip().upper()
            subject = request.form.get("subject", "").strip()
            message = request.form.get("message", "").strip()
            if not is_valid_email(contact):
                flash("请填写有效联系邮箱。", "error")
            elif len(subject) < 2:
                flash("请填写工单标题。", "error")
            elif len(message) < 5:
                flash("请填写更完整的问题描述。", "error")
            else:
                ticket_no = "T" + secrets.token_hex(6).upper()
                now = utcnow()
                get_db().execute(
                    """
                    INSERT INTO support_tickets
                        (ticket_no, contact, order_no, subject, message, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ticket_no, contact, order_no, subject, message, now, now),
                )
                flash(f"工单已提交，工单号 {ticket_no}。", "success")
                return redirect(url_for("ticket_new"))
        return render_template("ticket_form.html")

    @app.route(f"{admin_path}/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            admin = fetch_one("SELECT * FROM admin_users WHERE username = ?", (username,))
            if admin and verify_secret(password, admin["password_hash"]):
                session.clear()
                session["admin_user_id"] = admin["id"]
                session["admin_username"] = admin["username"]
                csrf_token()
                flash("已登录后台。", "success")
                return redirect(request.args.get("next") or url_for("admin_dashboard"))
            flash("账号或密码错误。", "error")
        return render_template("admin/login.html")

    @app.post(f"{admin_path}/logout")
    @login_required
    def admin_logout():
        session.clear()
        flash("已退出。", "success")
        return redirect(url_for("admin_login"))

    @app.route(admin_path)
    @login_required
    def admin_dashboard():
        stats = {
            "products": fetch_one("SELECT COUNT(*) AS n FROM products")["n"],
            "available_cards": fetch_one(
                "SELECT COUNT(*) AS n FROM cards WHERE status = 'available'"
            )["n"],
            "pending_orders": fetch_one(
                "SELECT COUNT(*) AS n FROM orders WHERE status = 'pending'"
            )["n"],
            "fulfilled_orders": fetch_one(
                "SELECT COUNT(*) AS n FROM orders WHERE status = 'fulfilled'"
            )["n"],
            "open_tickets": fetch_one(
                "SELECT COUNT(*) AS n FROM support_tickets WHERE status != 'closed'"
            )["n"],
        }
        recent_orders = fetch_all(
            "SELECT * FROM orders ORDER BY id DESC LIMIT 8"
        )
        return render_template(
            "admin/dashboard.html",
            stats=stats,
            recent_orders=recent_orders,
        )

    @app.route(f"{admin_path}/categories", methods=["GET", "POST"])
    @login_required
    def admin_categories():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            sort_order = int(request.form.get("sort_order", "100"))
            is_active = 1 if request.form.get("is_active") else 0
            if not name:
                flash("分类名称不能为空。", "error")
            else:
                now = utcnow()
                get_db().execute(
                    """
                    INSERT INTO categories (name, sort_order, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, sort_order, is_active, now, now),
                )
                flash("分类已创建。", "success")
                return redirect(url_for("admin_categories"))
        categories = fetch_all(
            """
            SELECT c.*,
                   COUNT(p.id) AS product_count
            FROM categories c
            LEFT JOIN products p ON p.category_id = c.id
            GROUP BY c.id
            ORDER BY c.sort_order ASC, c.id ASC
            """
        )
        return render_template("admin/categories.html", categories=categories)

    @app.route(f"{admin_path}/categories/<int:category_id>/edit", methods=["GET", "POST"])
    @login_required
    def admin_category_edit(category_id: int):
        category = fetch_one("SELECT * FROM categories WHERE id = ?", (category_id,))
        if not category:
            abort(404)
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            sort_order = int(request.form.get("sort_order", "100"))
            is_active = 1 if request.form.get("is_active") else 0
            if not name:
                flash("分类名称不能为空。", "error")
                return render_template("admin/category_form.html", category=category)
            get_db().execute(
                """
                UPDATE categories
                SET name = ?, sort_order = ?, is_active = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, sort_order, is_active, utcnow(), category_id),
            )
            flash("分类已更新。", "success")
            return redirect(url_for("admin_categories"))
        return render_template("admin/category_form.html", category=category)

    @app.post(f"{admin_path}/categories/<int:category_id>/delete")
    @login_required
    def admin_category_delete(category_id: int):
        category = fetch_one("SELECT * FROM categories WHERE id = ?", (category_id,))
        if not category:
            abort(404)
        product_count = fetch_one(
            "SELECT COUNT(*) AS n FROM products WHERE category_id = ?",
            (category_id,),
        )["n"]
        if product_count:
            flash("分类下还有商品，先把商品移到其他分类后再删除。", "error")
            return redirect(url_for("admin_categories"))
        get_db().execute("DELETE FROM categories WHERE id = ?", (category_id,))
        flash("分类已删除。", "success")
        return redirect(url_for("admin_categories"))

    @app.route(f"{admin_path}/products")
    @login_required
    def admin_products():
        products = fetch_all(
            """
            SELECT p.*, c.name AS category_name,
                   SUM(CASE WHEN cards.status = 'available' THEN 1 ELSE 0 END) AS available_cards,
                   SUM(CASE WHEN cards.status = 'reserved' THEN 1 ELSE 0 END) AS reserved_cards,
                   SUM(CASE WHEN cards.status = 'sold' THEN 1 ELSE 0 END) AS sold_cards
            FROM products p
            LEFT JOIN categories c ON c.id = p.category_id
            LEFT JOIN cards ON cards.product_id = p.id
            GROUP BY p.id
            ORDER BY p.sort_order ASC, p.id DESC
            """
        )
        return render_template("admin/products.html", products=products)

    @app.route(f"{admin_path}/products/new", methods=["GET", "POST"])
    @login_required
    def admin_product_new():
        if request.method == "POST":
            product_id = save_product()
            flash("商品已创建。", "success")
            return redirect(url_for("admin_product_cards", product_id=product_id))
        return render_template(
            "admin/product_form.html",
            product=None,
            categories=category_options(),
        )

    @app.route(f"{admin_path}/products/<int:product_id>/edit", methods=["GET", "POST"])
    @login_required
    def admin_product_edit(product_id: int):
        product = get_product_or_404(product_id)
        if request.method == "POST":
            save_product(product_id)
            flash("商品已更新。", "success")
            return redirect(url_for("admin_products"))
        return render_template(
            "admin/product_form.html",
            product=product,
            categories=category_options(),
        )

    @app.route(f"{admin_path}/products/<int:product_id>/cards", methods=["GET", "POST"])
    @login_required
    def admin_product_cards(product_id: int):
        product = get_product_or_404(product_id)
        if request.method == "POST":
            raw = request.form.get("cards", "")
            imported, duplicates = import_cards(product_id, raw)
            flash(f"导入 {imported} 条卡密，跳过 {duplicates} 条重复/空行。", "success")
            return redirect(url_for("admin_product_cards", product_id=product_id))
        cards = fetch_all(
            "SELECT * FROM cards WHERE product_id = ? ORDER BY id DESC LIMIT 200",
            (product_id,),
        )
        counts = card_counts(product_id)
        return render_template(
            "admin/cards.html",
            product=product,
            cards=cards,
            counts=counts,
        )

    @app.post(f"{admin_path}/cards/<int:card_id>/void")
    @login_required
    def admin_card_void(card_id: int):
        card = fetch_one("SELECT * FROM cards WHERE id = ?", (card_id,))
        if not card:
            abort(404)
        if card["status"] == "sold":
            flash("已售出的卡密不能作废。", "error")
        else:
            get_db().execute(
                "UPDATE cards SET status = 'void', updated_at = ? WHERE id = ?",
                (utcnow(), card_id),
            )
            flash("卡密已作废。", "success")
        return redirect(url_for("admin_product_cards", product_id=card["product_id"]))

    @app.route(f"{admin_path}/orders")
    @login_required
    def admin_orders():
        orders = fetch_all("SELECT * FROM orders ORDER BY id DESC LIMIT 200")
        return render_template("admin/orders.html", orders=orders)

    @app.route(f"{admin_path}/tickets")
    @login_required
    def admin_tickets():
        tickets = fetch_all("SELECT * FROM support_tickets ORDER BY id DESC LIMIT 200")
        return render_template("admin/tickets.html", tickets=tickets)

    @app.route(f"{admin_path}/payments")
    @login_required
    def admin_payments():
        providers = fetch_all("SELECT * FROM payment_providers ORDER BY id ASC")
        return render_template("admin/payments.html", providers=providers)

    @app.route(f"{admin_path}/payments/<code>/edit", methods=["GET", "POST"])
    @login_required
    def admin_payment_edit(code: str):
        provider = fetch_one("SELECT * FROM payment_providers WHERE code = ?", (code,))
        if not provider:
            abort(404)
        form_values = dict(provider)
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            fee_rate = request.form.get("fee_rate", "0").strip() or "0"
            is_enabled = 1 if request.form.get("is_enabled") else 0
            config_json = request.form.get("config_json", "").strip() or "{}"
            form_values.update(
                {
                    "name": name,
                    "fee_rate": fee_rate,
                    "is_enabled": is_enabled,
                    "config_json": config_json,
                }
            )
            if not name:
                flash("通道名称不能为空。", "error")
                return render_template("admin/payment_form.html", provider=form_values)
            try:
                parsed_config = json.loads(config_json)
            except json.JSONDecodeError:
                flash("配置必须是合法 JSON。", "error")
                return render_template("admin/payment_form.html", provider=form_values)
            pretty_config = json.dumps(parsed_config, ensure_ascii=False, indent=2)
            get_db().execute(
                """
                UPDATE payment_providers
                SET name = ?, is_enabled = ?, fee_rate = ?, config_json = ?, updated_at = ?
                WHERE code = ?
                """,
                (name, is_enabled, fee_rate, pretty_config, utcnow(), code),
            )
            flash("支付通道配置已保存。", "success")
            return redirect(url_for("admin_payments"))
        try:
            form_values["config_json"] = json.dumps(
                json.loads(provider["config_json"] or "{}"),
                ensure_ascii=False,
                indent=2,
            )
        except json.JSONDecodeError:
            form_values["config_json"] = provider["config_json"] or "{}"
        return render_template("admin/payment_form.html", provider=form_values)

    @app.post(f"{admin_path}/payments/<code>/toggle")
    @login_required
    def admin_payment_toggle(code: str):
        provider = fetch_one("SELECT * FROM payment_providers WHERE code = ?", (code,))
        if not provider:
            abort(404)
        next_value = 0 if provider["is_enabled"] else 1
        get_db().execute(
            "UPDATE payment_providers SET is_enabled = ?, updated_at = ? WHERE code = ?",
            (next_value, utcnow(), code),
        )
        flash("支付通道状态已更新。", "success")
        return redirect(url_for("admin_payments"))

    @app.cli.command("init-db")
    def init_db_command():
        bootstrap()
        print("Database initialized.")

    with app.app_context():
        bootstrap()

    return app


def bootstrap() -> None:
    init_db()
    ensure_default_admin()
    ensure_default_data()
    ensure_payment_providers()


def ensure_default_admin() -> None:
    username = os.getenv("ADMIN_USERNAME", "admin@example.com")
    password = os.getenv("ADMIN_PASSWORD", "change-me-now")
    existing = fetch_one("SELECT id FROM admin_users WHERE username = ?", (username,))
    if existing:
        get_db().execute(
            "UPDATE admin_users SET password_hash = ? WHERE id = ?",
            (hash_secret(password), existing["id"]),
        )
        if username != "admin":
            get_db().execute("DELETE FROM admin_users WHERE username = 'admin'")
        return
    get_db().execute(
        "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, hash_secret(password), utcnow()),
    )
    if username != "admin":
        get_db().execute("DELETE FROM admin_users WHERE username = 'admin'")


def ensure_default_data() -> None:
    category = fetch_one("SELECT id FROM categories LIMIT 1")
    if not category:
        now = utcnow()
        get_db().execute(
            """
            INSERT INTO categories (name, sort_order, is_active, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            ("默认分类", 100, now, now),
        )


def ensure_payment_providers() -> None:
    defaults = [
        ("mock", "模拟支付", "mock", 1, "0"),
        ("alipay", "支付宝官方接口", "alipay", 0, "0"),
        ("wechat", "微信支付官方接口", "wechat", 0, "0"),
        ("aggregate", "聚合支付接口", "aggregate", 0, "0"),
    ]
    now = utcnow()
    for code, name, provider_type, enabled, fee_rate in defaults:
        exists = fetch_one("SELECT id FROM payment_providers WHERE code = ?", (code,))
        if not exists:
            get_db().execute(
                """
                INSERT INTO payment_providers
                    (code, name, provider_type, is_enabled, fee_rate, config_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, '{}', ?, ?)
                """,
                (code, name, provider_type, enabled, fee_rate, now, now),
            )


def expire_stale_orders() -> None:
    if not Path(os.getenv("DATABASE_PATH", "data/card_issuance.sqlite3")).exists():
        return
    db = get_db()
    now = utcnow()
    try:
        db.execute("BEGIN IMMEDIATE")
        expired = db.execute(
            "SELECT id FROM orders WHERE status = 'pending' AND expires_at <= ?",
            (now,),
        ).fetchall()
        if expired:
            ids = [row["id"] for row in expired]
            placeholders = ",".join("?" for _ in ids)
            db.execute(
                f"UPDATE orders SET status = 'expired' WHERE id IN ({placeholders})",
                ids,
            )
            db.execute(
                f"""
                UPDATE cards
                SET status = 'available', order_id = NULL, reserved_until = NULL, updated_at = ?
                WHERE status = 'reserved' AND order_id IN ({placeholders})
                """,
                [now, *ids],
            )
        db.execute("COMMIT")
    except sqlite3.OperationalError:
        db.execute("ROLLBACK")
    except Exception:
        db.execute("ROLLBACK")
        raise


def available_stock(product_id: int) -> int:
    row = fetch_one(
        "SELECT COUNT(*) AS n FROM cards WHERE product_id = ? AND status = 'available'",
        (product_id,),
    )
    return int(row["n"])


def normalize_contact(value: str) -> str:
    return value.strip().lower()


def normalize_admin_path(value: str) -> str:
    path = (value or "/admin").strip()
    if not path.startswith("/"):
        path = "/" + path
    path = path.rstrip("/") or "/admin"
    if path in {"/", "/index", "/buy", "/orders", "/checkout", "/payments", "/order-query", "/tickets"}:
        raise ValueError("ADMIN_PATH conflicts with public routes.")
    if not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9_-]*(/[A-Za-z0-9][A-Za-z0-9_-]*)*", path):
        raise ValueError("ADMIN_PATH may only contain letters, numbers, dashes and underscores.")
    return path


def is_valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))


def get_product_or_404(product_id: int, active_only: bool = False):
    query = "SELECT * FROM products WHERE id = ?"
    params: tuple = (product_id,)
    if active_only:
        query += " AND status = 'active'"
    product = fetch_one(query, params)
    if not product:
        abort(404)
    return product


def get_order_or_404(order_no: str):
    order = fetch_one("SELECT * FROM orders WHERE order_no = ?", (order_no.upper(),))
    if not order:
        abort(404)
    return order


def category_options():
    return fetch_all("SELECT * FROM categories WHERE is_active = 1 ORDER BY sort_order ASC, id ASC")


def create_order(
    *,
    product_id: int,
    quantity: int,
    contact: str,
    email: str,
    query_password: str,
    provider_code: str,
) -> str:
    if quantity < 1:
        raise ValueError("购买数量必须大于 0。")
    if not is_valid_email(contact):
        raise ValueError("请填写有效联系邮箱。")
    if query_password and not (6 <= len(query_password) <= 20):
        raise ValueError("取卡密码长度应为 6-20 位。")

    provider = fetch_one(
        "SELECT * FROM payment_providers WHERE code = ? AND is_enabled = 1",
        (provider_code,),
    )
    if not provider:
        raise ValueError("请选择可用的支付方式。")

    db = get_db()
    now = utcnow()
    expires_at = (
        datetime.now(UTC) + timedelta(minutes=int(os.getenv("ORDER_EXPIRE_MINUTES", "15")))
    ).replace(microsecond=0).isoformat()

    try:
        db.execute("BEGIN IMMEDIATE")
        product = db.execute(
            "SELECT * FROM products WHERE id = ? AND status = 'active'",
            (product_id,),
        ).fetchone()
        if not product:
            raise ValueError("商品不存在或已下架。")
        if quantity > product["buy_limit"]:
            raise ValueError(f"该商品单次最多购买 {product['buy_limit']} 件。")

        cards = db.execute(
            """
            SELECT id, content
            FROM cards
            WHERE product_id = ? AND status = 'available'
            ORDER BY id ASC
            LIMIT ?
            """,
            (product_id, quantity),
        ).fetchall()
        if len(cards) != quantity:
            raise ValueError("库存不足，请选择其他商品或稍后再试。")

        order_no = secrets.token_hex(8).upper()
        query_hash = hash_secret(query_password) if query_password else ""
        amount_cents = product["price_cents"] * quantity
        cursor = db.execute(
            """
            INSERT INTO orders
                (order_no, product_id, product_name, quantity, contact, email,
                 query_password_hash, amount_cents, status, payment_provider,
                 expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                order_no,
                product_id,
                product["name"],
                quantity,
                contact,
                email,
                query_hash,
                amount_cents,
                provider_code,
                expires_at,
                now,
            ),
        )
        order_id = cursor.lastrowid
        card_ids = [row["id"] for row in cards]
        placeholders = ",".join("?" for _ in card_ids)
        db.execute(
            f"""
            UPDATE cards
            SET status = 'reserved', order_id = ?, reserved_until = ?, updated_at = ?
            WHERE id IN ({placeholders})
            """,
            [order_id, expires_at, now, *card_ids],
        )
        db.execute("COMMIT")
        return order_no
    except Exception:
        db.execute("ROLLBACK")
        raise


def fulfill_order(
    *,
    order_no: str,
    provider_code: str,
    trade_no: str,
    amount_cents: int,
    payload: dict,
):
    db = get_db()
    now = utcnow()
    try:
        db.execute("BEGIN IMMEDIATE")
        order = db.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
        if not order:
            raise ValueError("订单不存在。")
        if order["status"] == "fulfilled":
            db.execute("COMMIT")
            return order
        if order["status"] != "pending":
            db.execute(
                """
                UPDATE orders
                SET status = 'abnormal', error_message = ?, payment_trade_no = ?
                WHERE id = ?
                """,
                ("支付回调到达时订单已过期或状态异常。", trade_no, order["id"]),
            )
            db.execute("COMMIT")
            return get_order_or_404(order_no)
        if int(order["amount_cents"]) != int(amount_cents):
            db.execute(
                "UPDATE orders SET status = 'abnormal', error_message = ? WHERE id = ?",
                ("支付金额与订单金额不一致。", order["id"]),
            )
            db.execute("COMMIT")
            return get_order_or_404(order_no)

        try:
            db.execute(
                """
                INSERT INTO payment_events
                    (provider_code, trade_no, order_no, amount_cents, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    provider_code,
                    trade_no,
                    order_no,
                    amount_cents,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            db.execute("COMMIT")
            return get_order_or_404(order_no)

        cards = db.execute(
            """
            SELECT id, content
            FROM cards
            WHERE order_id = ? AND status = 'reserved'
            ORDER BY id ASC
            """,
            (order["id"],),
        ).fetchall()
        if len(cards) != order["quantity"]:
            db.execute(
                "UPDATE orders SET status = 'abnormal', error_message = ? WHERE id = ?",
                ("预占卡密数量与订单数量不一致。", order["id"]),
            )
            db.execute("COMMIT")
            return get_order_or_404(order_no)

        delivered_content = "\n".join(row["content"] for row in cards)
        card_ids = [row["id"] for row in cards]
        placeholders = ",".join("?" for _ in card_ids)
        db.execute(
            f"UPDATE cards SET status = 'sold', updated_at = ? WHERE id IN ({placeholders})",
            [now, *card_ids],
        )
        db.execute(
            """
            UPDATE orders
            SET status = 'fulfilled',
                payment_trade_no = ?,
                delivered_content = ?,
                paid_at = ?,
                completed_at = ?
            WHERE id = ?
            """,
            (trade_no, delivered_content, now, now, order["id"]),
        )
        db.execute("COMMIT")
        return get_order_or_404(order_no)
    except Exception:
        db.execute("ROLLBACK")
        raise


def cards_for_order(order_id: int):
    return fetch_all("SELECT * FROM cards WHERE order_id = ? ORDER BY id ASC", (order_id,))


def save_product(product_id: int | None = None) -> int:
    category_id = int(request.form.get("category_id") or "0") or None
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    price_cents = yuan_to_cents(request.form.get("price", "0"))
    status = request.form.get("status", "active")
    sort_order = int(request.form.get("sort_order", "100"))
    buy_limit = int(request.form.get("buy_limit", "1"))
    if not name:
        raise ValueError("商品名称不能为空。")
    if status not in {"active", "inactive"}:
        status = "inactive"
    if buy_limit < 1:
        buy_limit = 1

    db = get_db()
    now = utcnow()
    if product_id:
        db.execute(
            """
            UPDATE products
            SET category_id = ?, name = ?, description = ?, price_cents = ?,
                status = ?, sort_order = ?, buy_limit = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                category_id,
                name,
                description,
                price_cents,
                status,
                sort_order,
                buy_limit,
                now,
                product_id,
            ),
        )
        return product_id

    cursor = db.execute(
        """
        INSERT INTO products
            (category_id, name, description, price_cents, status, sort_order,
             buy_limit, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            category_id,
            name,
            description,
            price_cents,
            status,
            sort_order,
            buy_limit,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def import_cards(product_id: int, raw: str) -> tuple[int, int]:
    lines = [line.strip() for line in raw.replace("\r\n", "\n").split("\n")]
    seen = set()
    imported = 0
    duplicates = 0
    now = utcnow()
    db = get_db()
    for line in lines:
        if not line or line in seen:
            duplicates += 1
            continue
        seen.add(line)
        try:
            db.execute(
                """
                INSERT INTO cards (product_id, content, status, created_at, updated_at)
                VALUES (?, ?, 'available', ?, ?)
                """,
                (product_id, line, now, now),
            )
            imported += 1
        except sqlite3.IntegrityError:
            duplicates += 1
    return imported, duplicates


def card_counts(product_id: int) -> dict[str, int]:
    rows = fetch_all(
        """
        SELECT status, COUNT(*) AS n
        FROM cards
        WHERE product_id = ?
        GROUP BY status
        """,
        (product_id,),
    )
    counts = {"available": 0, "reserved": 0, "sold": 0, "void": 0}
    for row in rows:
        counts[row["status"]] = row["n"]
    return counts


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=True)
