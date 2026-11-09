"""USDT-TRC20 收款：固定收款地址 + 唯一金额匹配 + 链上到账自动发卡。

工作方式
--------
1. 下单时按后台配置的汇率把人民币金额换算为 USDT，并在同一收款地址的
   未完成订单之间分配一个唯一的“尾数”（例如 13.71 / 13.72 / 13.73）。
2. 买家向收款地址转入精确金额。
3. 后台线程（或买家停留在订单页时的状态轮询）通过 TronGrid 公共接口
   拉取该地址已确认的 USDT 入账，按“金额 + 时间窗口”匹配订单并发卡。
4. 每笔链上交易号只会被使用一次；无法匹配的入账会记录下来，
   管理员可以在后台手动指派给订单。

不需要私钥，也不需要第三方支付商户号，只要一个 TRON 钱包地址。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_UP, Decimal, InvalidOperation
from functools import lru_cache

from flask import current_app

from .db import fetch_all, fetch_one, get_db, utcnow

PROVIDER_TYPE = "usdt_trc20"
USDT_DECIMALS = 6
UNIT = 10**USDT_DECIMALS
MAINNET_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
DEFAULT_API_BASE = "https://api.trongrid.io"
# 订单过期后仍继续扫描一段时间，用来识别“按时付款但扫描滞后”和“迟到付款”。
LATE_SCAN_WINDOW = timedelta(hours=6)
# 过期订单的唯一金额在这段时间内不会分给新订单。必须不短于 LATE_SCAN_WINDOW，
# 否则旧买家的迟到转账会和复用同一金额的新订单混淆，把卡发给新买家。
SLOT_COOLDOWN = LATE_SCAN_WINDOW + timedelta(hours=1)

DEFAULT_CONFIG = {
    "address": "",
    "cny_per_usdt": "7.20",
    "amount_step": "0.01",
    "max_slots": 100,
    "trongrid_api_key": "",
    "api_base": DEFAULT_API_BASE,
    "token_contract": MAINNET_USDT_CONTRACT,
}

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class CryptoConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CryptoConfig:
    address: str
    cny_per_usdt: Decimal
    step_units: int
    max_slots: int
    api_key: str
    api_base: str
    token_contract: str


@dataclass(frozen=True)
class Transfer:
    tx_id: str
    from_address: str
    to_address: str
    units: int
    block_time: str  # 与 utcnow() 相同的 ISO 格式


def is_crypto_provider(provider) -> bool:
    return bool(provider) and provider["provider_type"] == PROVIDER_TYPE


def default_config_json() -> str:
    return json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 地址与配置


def _b58decode(value: str) -> bytes:
    number = 0
    for char in value:
        index = _B58_ALPHABET.find(char)
        if index < 0:
            raise ValueError("invalid base58 character")
        number = number * 58 + index
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    pad = len(value) - len(value.lstrip("1"))
    return b"\x00" * pad + raw


def is_valid_tron_address(address: str) -> bool:
    if not isinstance(address, str) or len(address) != 34 or not address.startswith("T"):
        return False
    try:
        raw = _b58decode(address)
    except ValueError:
        return False
    if len(raw) != 25 or raw[0] != 0x41:
        return False
    payload, checksum = raw[:-4], raw[-4:]
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] == checksum


def parse_config(config_json: str | dict) -> CryptoConfig:
    try:
        raw = json.loads(config_json or "{}") if isinstance(config_json, str) else dict(config_json)
    except json.JSONDecodeError as exc:
        raise CryptoConfigError("配置必须是合法 JSON。") from exc
    if not isinstance(raw, dict):
        raise CryptoConfigError("配置必须是 JSON 对象。")
    merged = {**DEFAULT_CONFIG, **raw}

    address = str(merged["address"]).strip()
    if not is_valid_tron_address(address):
        raise CryptoConfigError("address 不是有效的 TRON 地址（应以 T 开头，34 位）。")
    contract = str(merged["token_contract"]).strip()
    if not is_valid_tron_address(contract):
        raise CryptoConfigError("token_contract 不是有效的 TRON 合约地址。")
    try:
        rate = Decimal(str(merged["cny_per_usdt"]).strip())
        step = Decimal(str(merged["amount_step"]).strip())
        max_slots = int(merged["max_slots"])
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CryptoConfigError("cny_per_usdt、amount_step、max_slots 必须是数字。") from exc
    if not rate.is_finite() or rate <= 0:
        raise CryptoConfigError("cny_per_usdt（1 USDT 兑多少人民币）必须大于 0。")
    if not step.is_finite() or step <= 0 or step > 1:
        raise CryptoConfigError("amount_step 必须在 0 到 1 之间，例如 0.01。")
    step_units = step * UNIT
    if step_units != step_units.to_integral_value():
        raise CryptoConfigError("amount_step 最多 6 位小数。")
    if not 1 <= max_slots <= 1000:
        raise CryptoConfigError("max_slots 必须在 1 到 1000 之间。")
    api_base = str(merged["api_base"]).strip().rstrip("/")
    if not api_base.startswith("https://"):
        raise CryptoConfigError("api_base 必须是 https 地址。")
    return CryptoConfig(
        address=address,
        cny_per_usdt=rate,
        step_units=int(step_units),
        max_slots=max_slots,
        api_key=str(merged["trongrid_api_key"]).strip(),
        api_base=api_base,
        token_contract=contract,
    )


def units_to_text(units: int | None) -> str:
    """6 位整数单位转展示文本，去掉多余的 0 但至少保留 2 位小数。"""
    if units is None:
        return ""
    text = f"{Decimal(units) / UNIT:.6f}".rstrip("0")
    whole, _, frac = text.partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"


# ---------------------------------------------------------------- 唯一金额


def allocate_amount(
    db, provider_code: str, cfg: CryptoConfig, amount_cents: int, currency: str = "CNY"
) -> int:
    """在调用方已持有写锁（BEGIN IMMEDIATE）时调用，返回本订单应付的 USDT 单位数。

    USD / USDT 计价按 1:1 收 USDT；人民币计价按 cny_per_usdt 换算。
    """
    amount = Decimal(amount_cents) / 100
    if currency == "CNY":
        amount = amount / cfg.cny_per_usdt
    base = amount * UNIT
    base_units = int((base / cfg.step_units).to_integral_value(rounding=ROUND_UP)) * cfg.step_units
    base_units = max(base_units, cfg.step_units)
    cooldown_since = (datetime.now(UTC) - SLOT_COOLDOWN).replace(microsecond=0).isoformat()
    used = {
        row["crypto_amount"]
        for row in db.execute(
            """SELECT crypto_amount FROM orders
               WHERE payment_provider = ? AND crypto_address = ? AND crypto_amount IS NOT NULL
                 AND (status = 'pending' OR (status = 'expired' AND expires_at > ?))""",
            (provider_code, cfg.address, cooldown_since),
        )
    }
    for slot in range(cfg.max_slots):
        candidate = base_units + slot * cfg.step_units
        if candidate not in used:
            return candidate
    raise ValueError("当前待支付订单过多，请稍后再试。")


# ---------------------------------------------------------------- 链上查询


def fetch_transfers(cfg: CryptoConfig, min_time: datetime) -> list[Transfer]:
    """拉取收款地址自 min_time 起已确认的 USDT 入账（TronGrid v1 接口）。"""
    params = {
        "only_confirmed": "true",
        "only_to": "true",
        "contract_address": cfg.token_contract,
        "min_timestamp": str(int(min_time.timestamp() * 1000)),
        "order_by": "block_timestamp,asc",
        "limit": "200",
    }
    headers = {"Accept": "application/json", "User-Agent": "card-issuance/1.0"}
    if cfg.api_key:
        headers["TRON-PRO-API-KEY"] = cfg.api_key
    url_base = f"{cfg.api_base}/v1/accounts/{cfg.address}/transactions/trc20"
    transfers: list[Transfer] = []
    for _ in range(10):  # 最多 2000 条，足够覆盖扫描窗口
        url = url_base + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("success", True):
            raise RuntimeError(f"TronGrid error: {body.get('error') or body}")
        for item in body.get("data", []):
            transfer = _parse_transfer(item, cfg)
            if transfer:
                transfers.append(transfer)
        fingerprint = (body.get("meta") or {}).get("fingerprint")
        if not fingerprint:
            break
        params["fingerprint"] = fingerprint
    return transfers


def _parse_transfer(item: dict, cfg: CryptoConfig) -> Transfer | None:
    token = item.get("token_info") or {}
    if item.get("type") != "Transfer" or item.get("to") != cfg.address:
        return None
    if token.get("address") != cfg.token_contract or int(token.get("decimals", -1)) != USDT_DECIMALS:
        return None
    try:
        units = int(item["value"])
        block_ms = int(item["block_timestamp"])
        tx_id = str(item["transaction_id"])
    except (KeyError, TypeError, ValueError):
        return None
    if units <= 0 or not tx_id:
        return None
    block_time = datetime.fromtimestamp(block_ms / 1000, UTC).replace(microsecond=0).isoformat()
    return Transfer(tx_id, str(item.get("from", "")), cfg.address, units, block_time)


# ---------------------------------------------------------------- 匹配与发卡


def _find_order_for_transfer(provider_code: str, transfer: Transfer):
    """只在唯一确定时自动匹配：按时付款的订单，或迟到窗口内唯一的过期订单。

    固定地址下只能靠“金额 + 时间”区分订单；只要有两笔订单都可能对应这笔入账，
    就不自动发卡，留给管理员在“链上入账”页人工核对指派。
    """
    candidates = fetch_all(
        """SELECT * FROM orders
           WHERE payment_provider = ? AND crypto_address = ? AND crypto_amount = ?
             AND status IN ('pending', 'expired') AND created_at <= ?
           ORDER BY id DESC LIMIT 20""",
        (provider_code, transfer.to_address, transfer.units, transfer.block_time),
    )
    late_since = (
        datetime.fromisoformat(transfer.block_time) - LATE_SCAN_WINDOW
    ).isoformat()
    on_time = [o for o in candidates if transfer.block_time < o["expires_at"]]
    late = [
        o for o in candidates
        if o["expires_at"] <= transfer.block_time and o["expires_at"] >= late_since
    ]
    if len(on_time) + len(late) != 1:
        return None  # 无匹配或有歧义
    return (on_time or late)[0]


def record_and_apply(provider_code: str, transfer: Transfer) -> str:
    """登记一笔入账并尝试发卡，返回该入账的处理状态。可重复调用。"""
    db = get_db()
    now = utcnow()
    db.execute(
        """INSERT INTO crypto_transfers
               (provider_code, tx_id, from_address, to_address, amount_units, block_time,
                status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'unmatched', ?, ?)
           ON CONFLICT(provider_code, tx_id) DO NOTHING""",
        (provider_code, transfer.tx_id, transfer.from_address, transfer.to_address,
         transfer.units, transfer.block_time, now, now),
    )
    row = fetch_one(
        "SELECT * FROM crypto_transfers WHERE provider_code = ? AND tx_id = ?",
        (provider_code, transfer.tx_id),
    )
    if row["status"] != "unmatched":
        return row["status"]
    order = _find_order_for_transfer(provider_code, transfer)
    if not order:
        return "unmatched"
    return assign_transfer(provider_code, transfer.tx_id, order["order_no"], admin_override=False)


def assign_transfer(provider_code: str, tx_id: str, order_no: str, *, admin_override: bool) -> str:
    """把一笔已登记的入账用于某个订单。admin_override 时允许迟到或金额不符（管理员已核实）。"""
    from .main import fulfill_order  # 避免循环导入

    row = fetch_one(
        "SELECT * FROM crypto_transfers WHERE provider_code = ? AND tx_id = ?",
        (provider_code, tx_id),
    )
    if not row:
        raise ValueError("找不到这笔入账。")
    if row["status"] == "matched" and row["order_no"] != order_no.upper():
        raise ValueError("这笔入账已用于其他订单。")
    order = fetch_one("SELECT * FROM orders WHERE order_no = ?", (order_no.upper(),))
    if not order or order["payment_provider"] != provider_code:
        raise ValueError("订单不存在或不是该收款通道的订单。")
    if not admin_override and order["crypto_amount"] != row["amount_units"]:
        raise ValueError("入账金额与订单应付金额不一致。")

    result = fulfill_order(
        order_no=order["order_no"],
        provider_code=provider_code,
        trade_no=tx_id,
        amount_cents=order["amount_cents"],
        payload={
            "tx_id": tx_id,
            "from": row["from_address"],
            "to": row["to_address"],
            "amount_units": row["amount_units"],
            "block_time": row["block_time"],
            "admin_override": admin_override,
        },
        paid_at=row["block_time"],
        admin_override=admin_override,
    )
    status = "matched" if result["status"] == "fulfilled" else "late"
    get_db().execute(
        "UPDATE crypto_transfers SET status = ?, order_no = ?, updated_at = ? WHERE id = ?",
        (status, order["order_no"], utcnow(), row["id"]),
    )
    return status


# ---------------------------------------------------------------- 扫描调度


def _acquire_lease(key: str, interval_seconds: float) -> bool:
    """跨 worker 节流：同一 key 在 interval 内只允许一个进程执行。"""
    db = get_db()
    now = time.time()
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        if row and now - float(row["value"]) < interval_seconds:
            db.rollback()
            return False
        db.execute(
            """INSERT INTO app_state (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, repr(now)),
        )
        db.commit()
        return True
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise


def scan_provider(provider, *, min_interval: float = 0, fetch=None) -> dict:
    """扫描一个 USDT 通道。没有待匹配订单时不访问网络。

    按订单里保存的收款地址分别扫描：后台更换收款地址后，新订单用新地址，
    仍在付款期或迟到窗口内的旧订单继续按旧地址对账。
    """
    cfg = parse_config(provider["config_json"])
    late_since = (datetime.now(UTC) - LATE_SCAN_WINDOW).replace(microsecond=0).isoformat()
    open_addresses = fetch_all(
        """SELECT crypto_address AS address, MIN(created_at) AS t FROM orders
           WHERE payment_provider = ? AND crypto_address != ''
             AND (status = 'pending' OR (status = 'expired' AND expires_at > ?))
           GROUP BY crypto_address""",
        (provider["code"], late_since),
    )
    if not open_addresses:
        return {"scanned": False, "reason": "no_open_orders"}
    if min_interval and not _acquire_lease(f"scan:{provider['code']}", min_interval):
        return {"scanned": False, "reason": "throttled"}
    transfers: list[Transfer] = []
    for row in open_addresses:
        since = datetime.fromisoformat(row["t"]) - timedelta(minutes=1)
        address_cfg = replace(cfg, address=row["address"])
        transfers.extend((fetch or fetch_transfers)(address_cfg, since))
    results: dict[str, int] = {}
    for transfer in transfers:
        status = record_and_apply(provider["code"], transfer)
        results[status] = results.get(status, 0) + 1
    return {"scanned": True, "transfers": len(transfers), "results": results}


def enabled_crypto_providers():
    return fetch_all(
        "SELECT * FROM payment_providers WHERE provider_type = ? AND is_enabled = 1",
        (PROVIDER_TYPE,),
    )


def scan_all(min_interval: float) -> None:
    for provider in enabled_crypto_providers():
        try:
            scan_provider(provider, min_interval=min_interval)
        except Exception:  # 网络或配置问题不能影响其它通道和网页
            current_app.logger.exception("USDT scan failed for %s", provider["code"])


_on_demand_lock = threading.Lock()


def request_background_scan(app, provider_code: str, *, min_interval: float) -> None:
    """订单页轮询触发的扫描放到后台线程执行，不阻塞 Web worker。

    每个进程同时最多一个按需扫描线程；数据库租约再限制全局频率。
    """
    if not _on_demand_lock.acquire(blocking=False):
        return

    def run():
        try:
            with app.app_context():
                provider = fetch_one(
                    "SELECT * FROM payment_providers WHERE code = ? AND is_enabled = 1",
                    (provider_code,),
                )
                if is_crypto_provider(provider):
                    scan_provider(provider, min_interval=min_interval)
        except Exception:
            app.logger.exception("On-demand USDT scan failed")
        finally:
            _on_demand_lock.release()

    try:
        threading.Thread(target=run, name="usdt-on-demand", daemon=True).start()
    except Exception:
        _on_demand_lock.release()
        raise


def start_background_watcher(app, interval: float) -> None:
    """每个 worker 启动一个守护线程；数据库租约保证同一时间只有一个真正扫描。"""
    if interval <= 0 or getattr(app, "_crypto_watcher_started", False):
        return
    app._crypto_watcher_started = True

    def loop():
        while True:
            time.sleep(interval)
            try:
                with app.app_context():
                    scan_all(min_interval=interval * 0.8)
            except Exception:
                app.logger.exception("USDT watcher iteration failed")

    threading.Thread(target=loop, name="usdt-watcher", daemon=True).start()


@lru_cache(maxsize=32)
def address_qr_svg(address: str) -> str:
    try:
        import segno
    except ImportError:
        return ""
    return segno.make(address, error="m").svg_inline(scale=5, border=2, dark="#111", light="#fff")
