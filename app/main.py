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
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import crypto
from .db import close_db, fetch_all, fetch_one, get_db, init_db, is_database_busy, utcnow
from .formatting import CURRENCIES, cents_to_yuan, format_money, normalize_currency, yuan_to_cents
from .security import (
    authorize_order,
    can_reveal_order,
    csrf_token,
    hash_secret,
    login_required,
    require_csrf,
    safe_redirect_path,
    verify_secret,
)


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
        ENABLE_MOCK_PAYMENTS=os.getenv("ENABLE_MOCK_PAYMENTS", "").strip().lower()
        in {"1", "true", "yes", "on"},
        # 同一 IP 在 LOGIN_LOCK_MINUTES 内登录失败 LOGIN_MAX_FAILURES 次后暂时锁定。
        LOGIN_MAX_FAILURES=int(os.getenv("LOGIN_MAX_FAILURES", "5")),
        LOGIN_LOCK_MINUTES=int(os.getenv("LOGIN_LOCK_MINUTES", "15")),
        # 前面有几层可信反向代理（内置 Caddy 为 1）。直连模式保持 0，防止伪造 X-Forwarded-For。
        TRUSTED_PROXY_COUNT=int(os.getenv("TRUSTED_PROXY_COUNT", "0")),
        # 后台自动扫描 USDT 入账的间隔秒数；0 表示关闭（仅订单页轮询和后台手动扫描）。
        CRYPTO_WATCH_INTERVAL=float(os.getenv("CRYPTO_WATCH_INTERVAL", "0") or 0),
        # 防止匿名访客用未付款订单占光库存：同一邮箱 / 同一 IP 同时待付款的订单数，
        # 以及同一 IP 每小时最多下单次数。
        ORDER_MAX_PENDING_PER_CONTACT=int(os.getenv("ORDER_MAX_PENDING_PER_CONTACT", "2")),
        ORDER_MAX_PENDING_PER_IP=int(os.getenv("ORDER_MAX_PENDING_PER_IP", "3")),
        ORDER_CREATE_LIMIT_PER_HOUR=int(os.getenv("ORDER_CREATE_LIMIT_PER_HOUR", "10")),
        # 查单 / 取卡密码：同一 IP 或同一邮箱在 15 分钟内失败这么多次后暂时锁定。
        QUERY_MAX_FAILURES=int(os.getenv("QUERY_MAX_FAILURES", "8")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    app.config["SESSION_COOKIE_SECURE"] = app.config["APP_BASE_URL"].startswith("https://")
    check_app_secret(os.getenv("APP_SECRET"), app.config["APP_BASE_URL"])
    admin_path = app.config["ADMIN_PATH"]
    if app.config["TRUSTED_PROXY_COUNT"] > 0:
        from werkzeug.middleware.proxy_fix import ProxyFix

        hops = app.config["TRUSTED_PROXY_COUNT"]
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)

    app.teardown_appcontext(close_db)
    app.jinja_env.filters["yuan"] = cents_to_yuan
    app.jinja_env.filters["money"] = format_money
    app.jinja_env.filters["usdt"] = crypto.units_to_text

    @app.context_processor
    def inject_globals():
        endpoint = request.endpoint or ""
        return {
            "app_name": app.config["APP_NAME"],
            "csrf_token": csrf_token,
            "is_admin_area": endpoint.startswith("admin_"),
            "currencies": CURRENCIES,
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

    @app.after_request
    def protect_order_responses(response):
        if request.endpoint in {"checkout", "order_query", "checkout_status"} or (
            request.endpoint or ""
        ).startswith("admin_"):
            response.headers["Cache-Control"] = "no-store"
        headers = response.headers
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "same-origin")
        if request.is_secure:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

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
            # 不同币种的标价不能直接比较，先按币种分组再按价格排序。
            "price_asc": "p.currency ASC, p.price_cents ASC, p.sort_order ASC, p.id DESC",
            "price_desc": "p.currency ASC, p.price_cents DESC, p.sort_order ASC, p.id DESC",
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
            """SELECT * FROM payment_providers
               WHERE is_enabled = 1 AND (code != 'mock' OR ?)
               ORDER BY id ASC""",
            (app.config["ENABLE_MOCK_PAYMENTS"],),
        )
        return render_template(
            "buy.html",
            product=product,
            stock=stock,
            providers=providers,
        )

    @app.post("/orders")
    def create_order_route():
        try:
            product_id = int(request.form.get("product_id", "0"))
            quantity = int(request.form.get("quantity", "1"))
        except ValueError:
            flash("商品和购买数量必须是有效整数。", "error")
            return redirect(url_for("storefront"))
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
                client_ip=client_ip(),
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("buy", product_id=product_id))

        authorize_order(order_no)
        return redirect(url_for("checkout", order_no=order_no))

    @app.route("/checkout/<order_no>", methods=["GET", "POST"])
    def checkout(order_no: str):
        order = get_order_or_404(order_no)
        if request.method == "POST":
            query_password = request.form.get("query_password", "").strip()
            if can_reveal_order(order):
                return redirect(url_for("checkout", order_no=order["order_no"]))
            if query_locked(order["contact"]):
                flash("取卡密码错误次数过多，请 15 分钟后再试。", "error")
            elif check_order_password(order, query_password):
                authorize_order(order["order_no"])
                return redirect(url_for("checkout", order_no=order["order_no"]))
            else:
                flash("取卡密码错误，请重新输入。", "error")
        provider = fetch_one(
            "SELECT * FROM payment_providers WHERE code = ?",
            (order["payment_provider"],),
        )
        crypto_payment = None
        if crypto.is_crypto_provider(provider) and order["crypto_amount"]:
            crypto_payment = {
                "network": "TRON (TRC20)",
                "token": "USDT",
                "address": order["crypto_address"],
                "amount": crypto.units_to_text(order["crypto_amount"]),
                "qr_svg": crypto.address_qr_svg(order["crypto_address"]),
            }
        return render_template(
            "checkout.html",
            order=order,
            provider=provider,
            crypto_payment=crypto_payment,
            reveal_cards=can_reveal_order(order),
            mock_payment_available=bool(
                app.config["ENABLE_MOCK_PAYMENTS"]
                and provider
                and provider["is_enabled"]
                and order["payment_provider"] == "mock"
            ),
        )

    @app.post("/payments/mock/succeed/<order_no>")
    def mock_payment_succeed(order_no: str):
        if not app.config["ENABLE_MOCK_PAYMENTS"]:
            abort(404)
        order = get_order_or_404(order_no)
        provider = fetch_one(
            "SELECT is_enabled FROM payment_providers WHERE code = 'mock'"
        )
        if not provider or not provider["is_enabled"]:
            abort(403, "Mock payments are disabled.")
        if order["payment_provider"] != "mock":
            abort(400, "Payment provider does not match this order.")
        trade_no = "MOCK-" + secrets.token_hex(8).upper()
        result = fulfill_order(
            order_no=order["order_no"],
            provider_code="mock",
            trade_no=trade_no,
            amount_cents=order["amount_cents"],
            payload={"source": "mock_button"},
        )
        if result["status"] == "fulfilled":
            flash("模拟支付成功，卡密已自动发放。", "success")
        else:
            flash("订单未完成发卡，请查看订单状态并联系管理员。", "error")
        return redirect(url_for("checkout", order_no=order_no))

    @app.get("/checkout/<order_no>/status")
    def checkout_status(order_no: str):
        """订单页轮询用：只读数据库。待支付的 USDT 订单会在后台线程里
        请求一次节流的链上扫描，不占用当前 Web worker。"""
        order = get_order_or_404(order_no)
        if order["status"] == "pending" and order["crypto_amount"]:
            crypto.request_background_scan(app, order["payment_provider"], min_interval=10)
        return jsonify({"status": order["status"]})

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
            elif not query_password:
                flash("请填写下单时设置的取卡密码。", "error")
            elif query_locked(contact):
                flash("查询失败次数过多，请 15 分钟后再试。", "error")
            else:
                # 只校验少量候选订单，避免一次查询触发大量密码哈希计算。
                if order_no:
                    candidates = fetch_all(
                        """SELECT * FROM orders
                           WHERE order_no = ? AND contact = ? AND query_password_hash != ''""",
                        (order_no, contact),
                    )
                else:
                    candidates = fetch_all(
                        """SELECT * FROM orders
                           WHERE contact = ? AND query_password_hash != ''
                           ORDER BY id DESC LIMIT ?""",
                        (contact, QUERY_CANDIDATE_LIMIT),
                    )
                found_orders = [
                    order for order in candidates
                    if verify_secret(query_password, order["query_password_hash"])
                ]
                if found_orders:
                    for order in found_orders:
                        authorize_order(order["order_no"])
                        reveal_cards[order["id"]] = True
                else:
                    record_query_failure(contact)
                    flash("没有找到匹配的订单，请核对邮箱、订单号和取卡密码。", "error")
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
            client_ip = request.remote_addr or "unknown"
            if login_failure_count(client_ip) >= app.config["LOGIN_MAX_FAILURES"]:
                flash(
                    f"登录失败次数过多，请 {app.config['LOGIN_LOCK_MINUTES']} 分钟后再试。",
                    "error",
                )
                return render_template("admin/login.html"), 429
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            admin = fetch_one("SELECT * FROM admin_users WHERE username = ?", (username,))
            if not (admin and verify_secret(password, admin["password_hash"])):
                record_login_failure(client_ip)
                app.logger.warning("Admin login failed from %s", client_ip)
            else:
                clear_login_failures(client_ip)
                session.clear()
                session["admin_user_id"] = admin["id"]
                session["admin_username"] = admin["username"]
                session["admin_session_version"] = admin["session_version"]
                csrf_token()
                flash("已登录后台。", "success")
                return redirect(
                    safe_redirect_path(request.args.get("next"), url_for("admin_dashboard"))
                )
            flash("账号或密码错误。", "error")
        return render_template("admin/login.html")

    @app.post(f"{admin_path}/logout")
    @login_required
    def admin_logout():
        # 递增版本号：本次和其他设备上复制的旧会话 Cookie 都会失效。
        get_db().execute(
            "UPDATE admin_users SET session_version = session_version + 1 WHERE id = ?",
            (session.get("admin_user_id"),),
        )
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
        changed = get_db().execute(
            """UPDATE cards SET status = 'void', updated_at = ?
               WHERE id = ? AND status = 'available'""",
            (utcnow(), card_id),
        ).rowcount
        if changed:
            flash("卡密已作废。", "success")
        else:
            flash("仅未售出且未被订单预占的卡密可以作废。", "error")
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
            if crypto.is_crypto_provider(provider) and is_enabled:
                try:
                    crypto.parse_config(parsed_config)
                except crypto.CryptoConfigError as exc:
                    flash(f"无法启用：{exc}", "error")
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
        if next_value and crypto.is_crypto_provider(provider):
            try:
                crypto.parse_config(provider["config_json"])
            except crypto.CryptoConfigError as exc:
                flash(f"无法启用：{exc}", "error")
                return redirect(url_for("admin_payment_edit", code=code))
        get_db().execute(
            "UPDATE payment_providers SET is_enabled = ?, updated_at = ? WHERE code = ?",
            (next_value, utcnow(), code),
        )
        flash("支付通道状态已更新。", "success")
        return redirect(url_for("admin_payments"))

    @app.route(f"{admin_path}/crypto-transfers")
    @login_required
    def admin_crypto_transfers():
        transfers = fetch_all(
            "SELECT * FROM crypto_transfers ORDER BY block_time DESC, id DESC LIMIT 200"
        )
        return render_template(
            "admin/crypto_transfers.html",
            transfers=transfers,
            providers=crypto.enabled_crypto_providers(),
        )

    @app.post(f"{admin_path}/crypto-transfers/scan")
    @login_required
    def admin_crypto_scan():
        for provider in crypto.enabled_crypto_providers():
            try:
                result = crypto.scan_provider(provider)
            except Exception as exc:
                app.logger.exception("Manual USDT scan failed")
                flash(f"{provider['name']} 扫描失败：{exc}", "error")
                continue
            if result["scanned"]:
                flash(
                    f"{provider['name']}：读取 {result['transfers']} 笔入账，处理结果 {result['results'] or '无新入账'}。",
                    "success",
                )
            else:
                flash(f"{provider['name']}：当前没有待支付或近期过期的订单，无需扫描。", "success")
        return redirect(url_for("admin_crypto_transfers"))

    @app.post(f"{admin_path}/crypto-transfers/<int:transfer_id>/assign")
    @login_required
    def admin_crypto_assign(transfer_id: int):
        transfer = fetch_one("SELECT * FROM crypto_transfers WHERE id = ?", (transfer_id,))
        if not transfer:
            abort(404)
        order_no = request.form.get("order_no", "").strip().upper()
        try:
            status = crypto.assign_transfer(
                transfer["provider_code"], transfer["tx_id"], order_no, admin_override=True
            )
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            if status == "matched":
                flash(f"入账已指派给订单 {order_no}，卡密已发放。", "success")
            else:
                flash(f"入账已记录到订单 {order_no}，但未能发卡（可能库存不足），请查看订单。", "error")
        return redirect(url_for("admin_crypto_transfers"))

    @app.post(f"{admin_path}/orders/<order_no>/confirm-paid")
    @login_required
    def admin_order_confirm_paid(order_no: str):
        """管理员核实收款后手动补单发卡（适用于金额不符、迟到付款或其他收款方式）。"""
        order = get_order_or_404(order_no)
        reference = request.form.get("reference", "").strip()
        if not reference:
            flash("请填写收款凭证（链上交易号或备注）。", "error")
            return redirect(url_for("admin_orders"))
        if TX_HASH_RE.fullmatch(reference):
            reference = reference.lower()
        # 同一笔收款只能用于一个订单：自动入账、链上指派和人工补单共用同一套去重。
        used = fetch_one(
            """SELECT order_no FROM payment_events
               WHERE trade_no IN (?, ?) AND order_no != ?
               UNION ALL
               SELECT order_no FROM crypto_transfers
               WHERE tx_id = ? AND status = 'matched' AND order_no IS NOT NULL AND order_no != ?
               LIMIT 1""",
            (reference, f"MANUAL:{reference}", order["order_no"], reference, order["order_no"]),
        )
        if used:
            flash(f"该收款凭证已用于订单 {used['order_no']}，不能重复补单。", "error")
            return redirect(url_for("admin_orders"))
        provider = fetch_one(
            "SELECT * FROM payment_providers WHERE code = ?", (order["payment_provider"],)
        )
        if crypto.is_crypto_provider(provider):
            transfer = fetch_one(
                "SELECT tx_id FROM crypto_transfers WHERE provider_code = ? AND tx_id = ?",
                (order["payment_provider"], reference),
            )
            if transfer:
                # 已登记的链上入账：直接认领这笔入账并发卡，与“链上入账 → 指派”同一路径。
                try:
                    status = crypto.assign_transfer(
                        order["payment_provider"], transfer["tx_id"], order["order_no"],
                        admin_override=True,
                    )
                except ValueError as exc:
                    flash(str(exc), "error")
                else:
                    if status == "matched":
                        flash(f"订单 {order_no} 已认领链上入账并发卡。", "success")
                    else:
                        flash(f"订单 {order_no} 已认领入账，但未能发卡，请查看订单。", "error")
                return redirect(url_for("admin_orders"))
            if TX_HASH_RE.fullmatch(reference):
                flash("链上入账中没有这笔交易。请先在“链上入账”页扫描，确认到账后再补单。", "error")
                return redirect(url_for("admin_orders"))
        try:
            result = fulfill_order(
                order_no=order["order_no"],
                provider_code=order["payment_provider"],
                trade_no=f"MANUAL:{reference}"[:200],
                amount_cents=order["amount_cents"],
                payload={"admin": session.get("admin_username"), "reference": reference},
                admin_override=True,
            )
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            if result["status"] == "fulfilled":
                flash(f"订单 {order_no} 已手动确认收款并发卡。", "success")
            else:
                flash(f"订单 {order_no} 未能发卡：{result['error_message']}", "error")
        return redirect(url_for("admin_orders"))

    @app.cli.command("init-db")
    def init_db_command():
        bootstrap()
        print("Database initialized.")

    @app.cli.command("crypto-scan")
    def crypto_scan_command():
        """立即扫描一次所有已启用的 USDT 通道。"""
        for provider in crypto.enabled_crypto_providers():
            print(provider["code"], crypto.scan_provider(provider))

    with app.app_context():
        bootstrap()

    crypto.start_background_watcher(app, app.config["CRYPTO_WATCH_INTERVAL"])
    return app


def bootstrap() -> None:
    init_db()
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        ensure_default_admin()
        ensure_default_data()
        ensure_payment_providers()
        db.commit()
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise


def ensure_default_admin() -> None:
    username = os.getenv("ADMIN_USERNAME", "admin@example.com")
    password = os.getenv("ADMIN_PASSWORD", "change-me-now")
    db = get_db()
    existing = db.execute(
        "SELECT id, password_hash FROM admin_users WHERE username = ?", (username,)
    ).fetchone()
    if not existing:
        db.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_secret(password), utcnow()),
        )
    elif not verify_secret(password, existing["password_hash"]):
        # 密码已更换：更新哈希并让所有旧会话失效。
        db.execute(
            """UPDATE admin_users
               SET password_hash = ?, session_version = session_version + 1
               WHERE id = ?""",
            (hash_secret(password), existing["id"]),
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
    mock_enabled = bool(current_app.config["ENABLE_MOCK_PAYMENTS"])
    defaults = [
        ("mock", "模拟支付", "mock", int(mock_enabled), "0"),
        ("alipay", "支付宝官方接口", "alipay", 0, "0"),
        ("wechat", "微信支付官方接口", "wechat", 0, "0"),
        ("aggregate", "聚合支付接口", "aggregate", 0, "0"),
        ("usdt_trc20", "USDT (TRC20)", crypto.PROVIDER_TYPE, 0, "0"),
    ]
    now = utcnow()
    for code, name, provider_type, enabled, fee_rate in defaults:
        exists = fetch_one("SELECT id FROM payment_providers WHERE code = ?", (code,))
        if not exists:
            config_json = (
                crypto.default_config_json() if provider_type == crypto.PROVIDER_TYPE else "{}"
            )
            get_db().execute(
                """
                INSERT INTO payment_providers
                    (code, name, provider_type, is_enabled, fee_rate, config_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (code, name, provider_type, enabled, fee_rate, config_json, now, now),
            )
    if not mock_enabled:
        get_db().execute(
            "UPDATE payment_providers SET is_enabled = 0, updated_at = ? WHERE code = 'mock'",
            (now,),
        )


def expire_stale_orders() -> None:
    db = get_db()
    now = utcnow()
    needs_cleanup = db.execute(
        """SELECT 1 FROM orders o
           WHERE (o.status = 'pending' AND o.expires_at <= ?)
              OR (o.status IN ('expired', 'abnormal') AND EXISTS (
                  SELECT 1 FROM cards c WHERE c.order_id = o.id AND c.status = 'reserved'
              )) LIMIT 1""",
        (now,),
    ).fetchone()
    if not needs_cleanup:
        return
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE orders SET status = 'expired' WHERE status = 'pending' AND expires_at <= ?",
            (now,),
        )
        # Also repair reservations left by abnormal orders from older versions.
        db.execute(
            """UPDATE cards
               SET status = 'available', order_id = NULL, reserved_until = NULL, updated_at = ?
               WHERE status = 'reserved' AND order_id IN (
                   SELECT id FROM orders WHERE status IN ('expired', 'abnormal')
               )""",
            (now,),
        )
        db.commit()
    except sqlite3.OperationalError as exc:
        if db.in_transaction:
            db.rollback()
        if not is_database_busy(exc):
            raise
        current_app.logger.warning("Order expiration deferred because the database is busy.")
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise


def _login_window_start() -> str:
    minutes = current_app.config["LOGIN_LOCK_MINUTES"]
    return (datetime.now(UTC) - timedelta(minutes=minutes)).replace(microsecond=0).isoformat()


def login_failure_count(ip: str) -> int:
    return fetch_one(
        "SELECT COUNT(*) AS n FROM login_failures WHERE ip = ? AND created_at > ?",
        (ip, _login_window_start()),
    )["n"]


def record_login_failure(ip: str) -> None:
    db = get_db()
    db.execute("INSERT INTO login_failures (ip, created_at) VALUES (?, ?)", (ip, utcnow()))
    old = (datetime.now(UTC) - timedelta(days=1)).replace(microsecond=0).isoformat()
    db.execute("DELETE FROM login_failures WHERE created_at < ?", (old,))


def clear_login_failures(ip: str) -> None:
    get_db().execute("DELETE FROM login_failures WHERE ip = ?", (ip,))


# 一次无订单号的查单最多校验这么多笔订单的密码，限制单次请求的 CPU 开销。
QUERY_CANDIDATE_LIMIT = 5
QUERY_LOCK_WINDOW = timedelta(minutes=15)
TX_HASH_RE = re.compile(r"[0-9a-fA-F]{64}")
PLACEHOLDER_SECRETS = {"change-this-to-a-long-random-string", "change-me", "secret", "dev-secret"}


def check_app_secret(secret: str | None, base_url: str) -> None:
    """拒绝用公开的示例密钥启动：知道密钥的人可以伪造后台会话。"""
    if secret and secret.strip() == "change-this-to-a-long-random-string":
        raise RuntimeError("APP_SECRET 仍是 .env.example 的示例值，请改为随机长字符串。")
    if base_url.startswith("https://") and (
        not secret or len(secret) < 32 or secret.strip() in PLACEHOLDER_SECRETS
    ):
        raise RuntimeError("生产环境的 APP_SECRET 必须是至少 32 位的随机字符串。")


def client_ip() -> str:
    return request.remote_addr or "unknown"


def _since(window: timedelta) -> str:
    return (datetime.now(UTC) - window).replace(microsecond=0).isoformat()


def rate_count(bucket: str, key: str, window: timedelta) -> int:
    return fetch_one(
        "SELECT COUNT(*) AS n FROM rate_events WHERE bucket = ? AND key = ? AND created_at > ?",
        (bucket, key, _since(window)),
    )["n"]


def record_rate_event(bucket: str, key: str) -> None:
    db = get_db()
    db.execute(
        "INSERT INTO rate_events (bucket, key, created_at) VALUES (?, ?, ?)",
        (bucket, key, utcnow()),
    )
    db.execute("DELETE FROM rate_events WHERE created_at < ?", (_since(timedelta(days=1)),))


def query_locked(contact: str) -> bool:
    """查单 / 取卡密码按 IP 和邮箱分别计失败次数，任一超限即暂时锁定。"""
    limit = current_app.config["QUERY_MAX_FAILURES"]
    return (
        rate_count("query_ip", client_ip(), QUERY_LOCK_WINDOW) >= limit
        or rate_count("query_contact", contact, QUERY_LOCK_WINDOW) >= limit
    )


def record_query_failure(contact: str) -> None:
    record_rate_event("query_ip", client_ip())
    record_rate_event("query_contact", contact)


def check_order_password(order, password: str) -> bool:
    """校验单个订单的取卡密码；空密码不做哈希计算，失败计入限流。"""
    ok = bool(password and order["query_password_hash"]) and verify_secret(
        password, order["query_password_hash"]
    )
    if not ok and password:
        record_query_failure(order["contact"])
    return ok


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


def enforce_order_limits(db, contact: str, ip: str) -> None:
    """未付款订单会预占库存：限制每个买家同时待付款的订单数和下单频率。"""
    config = current_app.config
    pending_by_contact = db.execute(
        "SELECT COUNT(*) FROM orders WHERE contact = ? AND status = 'pending'", (contact,)
    ).fetchone()[0]
    if pending_by_contact >= config["ORDER_MAX_PENDING_PER_CONTACT"]:
        raise ValueError("该邮箱已有未支付的订单，请先完成支付或等待订单过期后再下单。")
    if not ip:
        return
    pending_by_ip = db.execute(
        "SELECT COUNT(*) FROM orders WHERE client_ip = ? AND status = 'pending'", (ip,)
    ).fetchone()[0]
    if pending_by_ip >= config["ORDER_MAX_PENDING_PER_IP"]:
        raise ValueError("当前网络下未支付的订单过多，请先完成支付或等待订单过期后再下单。")
    recent = db.execute(
        "SELECT COUNT(*) FROM orders WHERE client_ip = ? AND created_at > ?",
        (ip, _since(timedelta(hours=1))),
    ).fetchone()[0]
    if recent >= config["ORDER_CREATE_LIMIT_PER_HOUR"]:
        raise ValueError("下单过于频繁，请稍后再试。")


def create_order(
    *,
    product_id: int,
    quantity: int,
    contact: str,
    email: str,
    query_password: str,
    provider_code: str,
    client_ip: str = "",
) -> str:
    if quantity < 1:
        raise ValueError("购买数量必须大于 0。")
    if not is_valid_email(contact):
        raise ValueError("请填写有效联系邮箱。")
    # 取卡密码是查看卡密的唯一凭证，必须设置（邮箱未经验证，不能单独作为取卡依据）。
    if not (6 <= len(query_password or "") <= 20):
        raise ValueError("请设置 6-20 位取卡密码。")
    if provider_code == "mock" and not current_app.config["ENABLE_MOCK_PAYMENTS"]:
        raise ValueError("模拟支付未启用。")

    provider = fetch_one(
        "SELECT * FROM payment_providers WHERE code = ? AND is_enabled = 1",
        (provider_code,),
    )
    if not provider:
        raise ValueError("请选择可用的支付方式。")

    db = get_db()
    now = utcnow()
    expires_at = (
        datetime.now(UTC) + timedelta(minutes=current_app.config["ORDER_EXPIRE_MINUTES"])
    ).replace(microsecond=0).isoformat()

    try:
        db.execute("BEGIN IMMEDIATE")
        # Recheck the channel while holding the same write lock as allocation.
        provider = db.execute(
            "SELECT * FROM payment_providers WHERE code = ? AND is_enabled = 1",
            (provider_code,),
        ).fetchone()
        if not provider:
            raise ValueError("请选择可用的支付方式。")
        product = db.execute(
            "SELECT * FROM products WHERE id = ? AND status = 'active'",
            (product_id,),
        ).fetchone()
        if not product:
            raise ValueError("商品不存在或已下架。")
        if quantity > product["buy_limit"]:
            raise ValueError(f"该商品单次最多购买 {product['buy_limit']} 件。")
        # 在写锁内检查，防止并发下单绕过限制。
        enforce_order_limits(db, contact, client_ip)

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
        crypto_amount = None
        crypto_address = ""
        if crypto.is_crypto_provider(provider):
            try:
                cfg = crypto.parse_config(provider["config_json"])
            except crypto.CryptoConfigError as exc:
                current_app.logger.error("USDT provider misconfigured: %s", exc)
                raise ValueError("该支付方式暂不可用，请选择其他方式。") from exc
            crypto_address = cfg.address
            # 唯一金额在写锁内分配，并发下单不会拿到相同金额。
            crypto_amount = crypto.allocate_amount(
                db, provider_code, cfg, amount_cents, product["currency"]
            )
        cursor = db.execute(
            """
            INSERT INTO orders
                (order_no, product_id, product_name, quantity, contact, email,
                 query_password_hash, amount_cents, currency, status, payment_provider,
                 crypto_amount, crypto_address, client_ip, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
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
                product["currency"],
                provider_code,
                crypto_amount,
                crypto_address,
                client_ip,
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
        if db.in_transaction:
            db.rollback()
        raise


def fulfill_order(
    *,
    order_no: str,
    provider_code: str,
    trade_no: str,
    amount_cents: int,
    payload: dict,
    paid_at: str | None = None,
    admin_override: bool = False,
):
    """Fulfill a confirmed payment after its adapter has verified authenticity.

    paid_at: 付款发生的时间（如链上区块时间）。扫描滞后时，按时付款的订单即使已被
    标记过期，也会尝试重新预占库存后发卡。
    admin_override: 管理员已人工核实收款，允许为过期/异常订单补发。
    """
    if not isinstance(trade_no, str) or not trade_no.strip():
        raise ValueError("支付流水号不能为空。")
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents < 0:
        raise ValueError("支付金额必须是非负整数分。")
    trade_no = trade_no.strip()
    order_no = order_no.upper()
    db = get_db()
    now = utcnow()
    try:
        db.execute("BEGIN IMMEDIATE")
        order = db.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
        if not order:
            raise ValueError("订单不存在。")
        if provider_code != order["payment_provider"]:
            raise ValueError("支付通道与订单不一致。")
        if provider_code == "mock":
            provider = db.execute(
                "SELECT is_enabled FROM payment_providers WHERE code = 'mock'"
            ).fetchone()
            if (
                not current_app.config["ENABLE_MOCK_PAYMENTS"]
                or not provider
                or not provider["is_enabled"]
            ):
                raise ValueError("模拟支付未启用。")
        previous_event = db.execute(
            "SELECT order_no, amount_cents FROM payment_events WHERE provider_code = ? AND trade_no = ?",
            (provider_code, trade_no),
        ).fetchone()
        if previous_event and (
            previous_event["order_no"] != order_no
            or previous_event["amount_cents"] != amount_cents
        ):
            raise ValueError("支付流水号已用于其他订单或金额。")
        if order["status"] == "fulfilled":
            if amount_cents != order["amount_cents"]:
                raise ValueError("支付金额与订单金额不一致。")
            db.commit()
            return order
        if not previous_event:
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

        on_time = (paid_at or now) < order["expires_at"]
        if order["status"] == "pending" and (on_time or admin_override):
            pass
        elif order["status"] in {"expired", "abnormal"} and (
            admin_override or (order["status"] == "expired" and paid_at and on_time)
        ):
            if not reserve_cards_again(order, now):
                mark_order_abnormal(
                    order, "已收到付款，但原预占卡密已释放且库存不足，请补货后手动补单。",
                    trade_no, now,
                )
                db.commit()
                return get_order_or_404(order_no)
        else:
            mark_order_abnormal(
                order,
                order["error_message"] or "支付到账时订单已过期或状态异常。",
                trade_no,
                now,
            )
            db.commit()
            return get_order_or_404(order_no)
        if order["amount_cents"] != amount_cents:
            mark_order_abnormal(order, "支付金额与订单金额不一致。", trade_no, now)
            db.commit()
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
            mark_order_abnormal(
                order, "预占卡密数量与订单数量不一致。", trade_no, now
            )
            db.commit()
            return get_order_or_404(order_no)

        delivered_content = "\n".join(row["content"] for row in cards)
        card_ids = [row["id"] for row in cards]
        placeholders = ",".join("?" for _ in card_ids)
        db.execute(
            f"UPDATE cards SET status = 'sold', reserved_until = NULL, updated_at = ? WHERE id IN ({placeholders})",
            [now, *card_ids],
        )
        db.execute(
            """
            UPDATE orders
            SET status = 'fulfilled',
                payment_trade_no = ?,
                error_message = '',
                delivered_content = ?,
                paid_at = ?,
                completed_at = ?
            WHERE id = ?
            """,
            (trade_no, delivered_content, now, now, order["id"]),
        )
        db.commit()
        return get_order_or_404(order_no)
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise


def mark_order_abnormal(order, reason: str, trade_no: str, now: str) -> None:
    """Record an unusable payment without leaving inventory reserved forever."""
    db = get_db()
    db.execute(
        """UPDATE orders
           SET status = 'abnormal', error_message = ?, payment_trade_no = ?,
               paid_at = COALESCE(paid_at, ?)
           WHERE id = ?""",
        (reason, trade_no, now, order["id"]),
    )
    db.execute(
        """UPDATE cards
           SET status = 'available', order_id = NULL, reserved_until = NULL, updated_at = ?
           WHERE order_id = ? AND status = 'reserved'""",
        (now, order["id"]),
    )


def reserve_cards_again(order, now: str) -> bool:
    """为已释放预占的订单重新预占库存（调用方持有写锁）。"""
    db = get_db()
    held = db.execute(
        "SELECT COUNT(*) AS n FROM cards WHERE order_id = ? AND status = 'reserved'",
        (order["id"],),
    ).fetchone()["n"]
    need = order["quantity"] - held
    if need <= 0:
        return True
    cards = db.execute(
        """SELECT id FROM cards WHERE product_id = ? AND status = 'available'
           ORDER BY id ASC LIMIT ?""",
        (order["product_id"], need),
    ).fetchall()
    if len(cards) != need:
        return False
    ids = [row["id"] for row in cards]
    db.execute(
        f"""UPDATE cards SET status = 'reserved', order_id = ?, updated_at = ?
            WHERE id IN ({",".join("?" for _ in ids)})""",
        [order["id"], now, *ids],
    )
    return True


def cards_for_order(order_id: int):
    return fetch_all("SELECT * FROM cards WHERE order_id = ? ORDER BY id ASC", (order_id,))


def save_product(product_id: int | None = None) -> int:
    category_id = int(request.form.get("category_id") or "0") or None
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    price_cents = yuan_to_cents(request.form.get("price", "0"))
    currency = normalize_currency(request.form.get("currency"))
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
            SET category_id = ?, name = ?, description = ?, price_cents = ?, currency = ?,
                status = ?, sort_order = ?, buy_limit = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                category_id,
                name,
                description,
                price_cents,
                currency,
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
            (category_id, name, description, price_cents, currency, status, sort_order,
             buy_limit, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            category_id,
            name,
            description,
            price_cents,
            currency,
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
