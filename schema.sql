PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    -- 退出登录或改密码时递增，使所有旧会话 Cookie 失效。
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 100,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    status TEXT NOT NULL DEFAULT 'active',
    sort_order INTEGER NOT NULL DEFAULT 100,
    buy_limit INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (price_cents >= 0),
    CHECK (status IN ('active', 'inactive')),
    CHECK (buy_limit >= 1)
);

CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',
    order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    reserved_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (status IN ('available', 'reserved', 'sold', 'void'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cards_product_content
ON cards(product_id, content);

CREATE INDEX IF NOT EXISTS idx_cards_product_status
ON cards(product_id, status);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL UNIQUE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    product_name TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    contact TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    query_password_hash TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    status TEXT NOT NULL DEFAULT 'pending',
    payment_provider TEXT NOT NULL DEFAULT 'mock',
    payment_trade_no TEXT NOT NULL DEFAULT '',
    crypto_amount INTEGER,
    crypto_address TEXT NOT NULL DEFAULT '',
    client_ip TEXT NOT NULL DEFAULT '',
    delivered_content TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    paid_at TEXT,
    completed_at TEXT,
    CHECK (quantity >= 1),
    CHECK (amount_cents >= 0),
    CHECK (status IN ('pending', 'fulfilled', 'expired', 'abnormal'))
);

CREATE INDEX IF NOT EXISTS idx_orders_status_expires
ON orders(status, expires_at);

CREATE INDEX IF NOT EXISTS idx_orders_contact
ON orders(contact);

CREATE TABLE IF NOT EXISTS payment_providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    is_enabled INTEGER NOT NULL DEFAULT 0,
    fee_rate TEXT NOT NULL DEFAULT '0',
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_code TEXT NOT NULL,
    trade_no TEXT NOT NULL,
    order_no TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (provider_code, trade_no)
);

-- 链上入账（USDT-TRC20 等）。amount_units 为 6 位小数的整数单位。
CREATE TABLE IF NOT EXISTS crypto_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_code TEXT NOT NULL,
    tx_id TEXT NOT NULL,
    from_address TEXT NOT NULL DEFAULT '',
    to_address TEXT NOT NULL,
    amount_units INTEGER NOT NULL,
    block_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unmatched',
    order_no TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (provider_code, tx_id),
    CHECK (status IN ('unmatched', 'matched', 'late', 'ignored'))
);

CREATE INDEX IF NOT EXISTS idx_crypto_transfers_status
ON crypto_transfers(status, block_time);

-- 后台登录失败记录，用于按 IP 限制尝试次数。
CREATE TABLE IF NOT EXISTS login_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_login_failures_ip
ON login_failures(ip, created_at);

-- 前台访问频率限制（查单/取卡密码失败、下单次数等），按 bucket + key 计数。
CREATE TABLE IF NOT EXISTS rate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket TEXT NOT NULL,
    key TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rate_events_lookup
ON rate_events(bucket, key, created_at);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS support_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_no TEXT NOT NULL UNIQUE,
    contact TEXT NOT NULL,
    order_no TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    email_verified INTEGER NOT NULL DEFAULT 0,
    admin_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (status IN ('open', 'processing', 'closed'))
);

CREATE INDEX IF NOT EXISTS idx_support_tickets_contact
ON support_tickets(contact);

CREATE INDEX IF NOT EXISTS idx_support_tickets_status
ON support_tickets(status);
