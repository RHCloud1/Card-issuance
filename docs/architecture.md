# v0.1 Architecture

This project is a self-hosted automatic card issuance system for a single merchant.

## Business Model

- Public storefront does not require login.
- Admin creates products, edits prices, toggles products on/off, and imports card secrets.
- A buyer creates an order and the system reserves cards for a short time.
- Payment provider callback confirms payment.
- The order is fulfilled automatically and the reserved cards become sold.
- The buyer can reopen the order page or use order query to retrieve cards.

## Order States

- `pending`: order created, cards reserved, waiting for payment.
- `fulfilled`: payment confirmed and cards delivered.
- `expired`: payment window elapsed, reserved cards released.
- `abnormal`: payment or inventory state needs admin attention.

## Card States

- `available`: can be reserved by a new order.
- `reserved`: attached to a pending order.
- `sold`: delivered to a paid order.
- `void`: removed from sale.

SQLite uses `BEGIN IMMEDIATE` around order creation and fulfillment. On a small VPS this keeps allocation simple and prevents the same card being issued twice.

## Payment Providers

The payment layer is intentionally adapter-based:

- `mock`: implemented now; simulates a successful callback.
- `alipay`: reserved for Alipay official merchant API.
- `wechat`: reserved for WeChat Pay official merchant API.
- `aggregate`: reserved for a licensed aggregate provider.

Real providers should implement:

1. Create payment request.
2. Redirect or QR-code response.
3. Asynchronous notify endpoint.
4. Signature verification.
5. Amount check.
6. Idempotent fulfillment by provider trade number.

## Dujiaoka Reference

Dujiaoka stores payment channels in a `pays` table with fields such as `pay_check`, `merchant_id`, `merchant_key`, `merchant_pem`, and `pay_handleroute`. Each gateway then has its own controller and routes.

This project keeps the same idea, but makes it a cleaner adapter boundary: one provider registry, one fulfillment path, and provider-specific code only for gateway request/notify handling.
