from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

# 商品可选的计价货币：代码 -> (前缀, 后缀, 名称)。金额统一按“分”（两位小数）存储。
CURRENCIES = {
    "CNY": ("￥", "", "人民币 CNY"),
    "USD": ("$", "", "美元 USD"),
    "USDT": ("", " USDT", "USDT"),
}
DEFAULT_CURRENCY = "CNY"


def cents_to_yuan(cents: int) -> str:
    return f"{Decimal(cents) / Decimal(100):.2f}"


def yuan_to_cents(value: str) -> int:
    amount = Decimal(value.strip()).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount < 0:
        raise ValueError("price must be non-negative")
    return int(amount * 100)


def normalize_currency(value: str | None) -> str:
    code = (value or DEFAULT_CURRENCY).strip().upper()
    return code if code in CURRENCIES else DEFAULT_CURRENCY


def format_money(cents: int, currency: str | None = None) -> str:
    prefix, suffix, _ = CURRENCIES[normalize_currency(currency)]
    return f"{prefix}{cents_to_yuan(cents)}{suffix}"
