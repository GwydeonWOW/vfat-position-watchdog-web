#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vfat-position-watchdog
======================

Demonio que vigila indefinidamente las posiciones de liquidez (LP / NFT de
liquidez concentrada) de una o varias wallets usando el MCP público de vfat
(https://vfat.io/mcp) y envía un aviso por Telegram en cada ejecución en la
que alguna posición abierta esté fuera de su rango de precio.

Solo usa la librería estándar de Python 3.8+ (sin dependencias externas).

Uso:
    ./vfat_watchdog.py --help
    ./vfat_watchdog.py                     # bucle infinito, lee .env
    ./vfat_watchdog.py --env /ruta/.env    # fichero de configuración alternativo
    ./vfat_watchdog.py --once              # una sola comprobación y termina (cron)
    ./vfat_watchdog.py --once --dry-run    # como arriba, pero sin enviar a Telegram
    ./vfat_watchdog.py --interval 30       # pisa CHECK_INTERVAL_SECONDS (pruebas)
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

log = logging.getLogger("vfat-watchdog")

DEFAULT_MCP_URL = "https://mcp.vfat.io/mcp"
DEFAULT_INTERVAL = 180          # 3 minutos
TOOL_TIMEOUT = 90.0             # el MCP de vfat documenta 60 s por tool
HTTP_ATTEMPTS = 3
TELEGRAM_MAX_CHARS = 4000
TICK_BASE = 1.0001              # base exponencial de los ticks de Uniswap V3/Slipstream

WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

CHAIN_NAMES: Dict[int, str] = {
    1: "Ethereum", 10: "Optimism", 56: "BNB Chain", 100: "Gnosis", 137: "Polygon",
    250: "Fantom", 324: "zkSync Era", 480: "World Chain", 8453: "Base",
    42161: "Arbitrum", 42220: "Celo", 43114: "Avalanche", 81457: "Blast",
    34443: "Morph", 59144: "Linea", 534352: "Scroll", 7777777: "Zora",
}


class ConfigError(Exception):
    """Configuración inválida o incompleta."""


class McpError(Exception):
    """Fallo hablando con el servidor MCP de vfat."""


class TelegramError(Exception):
    """Fallo enviando el mensaje a Telegram."""


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

_TRUE = {"1", "true", "yes", "y", "on", "sí", "si"}
_FALSE = {"0", "false", "no", "n", "off"}


def _env_bool(env: Dict[str, str], key: str, default: bool) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    log.warning("Valor no reconocido para %s=%r; se usa %s", key, raw, default)
    return default


def _env_float(env: Dict[str, str], key: str, default: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{key} debe ser un número (recibido: {raw!r})")


def _env_int(env: Dict[str, str], key: str, default: int) -> int:
    return int(_env_float(env, key, default))


def _split_csv(raw: str) -> List[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _as_float(value: Any) -> Optional[float]:
    """Convierte números o strings numéricos a float; None si no se puede."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _chunks(seq: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(seq), size):
        yield seq[start:start + size]


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def _chain_name(chain_id: int) -> str:
    return CHAIN_NAMES.get(chain_id, f"chain {chain_id}")


def _fmt_usd(amount: float) -> str:
    return f"${amount:,.2f}"


def _fmt_price(price: float) -> str:
    if price != price or price in (float("inf"), float("-inf")):
        return "?"
    if price == 0:
        return "0"
    if 1e-4 <= abs(price) < 1e6:
        text = f"{price:.6f}".rstrip("0").rstrip(".")
        return text or "0"
    return f"{price:.3e}"


def load_env(path: str) -> List[str]:
    """Carga un fichero .env sencillo (KEY=VALUE) sin pisar variables ya exportadas."""
    loaded: List[str] = []
    if not os.path.isfile(path):
        return loaded
    line_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
    with open(path, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            match = line_re.match(line)
            if not match:
                log.warning("%s:%d: línea ignorada (no es KEY=VALUE): %s", path, lineno, line)
                continue
            key, value = match.group(1), match.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].strip()
            os.environ.setdefault(key, value)
            loaded.append(key)
    return loaded


def setup_logging(log_file: Optional[str], verbose: bool) -> None:
    handler: logging.Handler = logging.StreamHandler(sys.stdout)
    handlers: List[logging.Handler] = [handler]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        handlers=handlers,
    )
    logging.Formatter.converter = time.gmtime  # logs en UTC


# --------------------------------------------------------------------------
# Configuración (.env)
# --------------------------------------------------------------------------

@dataclass
class Config:
    wallets: List[str] = field(default_factory=list)
    telegram_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    interval: int = DEFAULT_INTERVAL
    chain_ids: Optional[Set[int]] = None
    alert_only_changes: bool = False
    alert_on_recovery: bool = True
    notify_start: bool = True
    notify_errors: bool = True
    heartbeat_hours: float = 0.0
    log_file: Optional[str] = None
    mcp_url: str = DEFAULT_MCP_URL

    @classmethod
    def from_env(cls, env: Dict[str, str]) -> "Config":
        cfg = cls()
        raw_wallets = _split_csv(env.get("WALLET_ADDRESS") or env.get("WALLET_ADDRESSES") or "")
        cfg.wallets = [w.lower() for w in raw_wallets]

        cfg.telegram_token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip() or None
        cfg.telegram_chat_id = (env.get("TELEGRAM_CHAT_ID") or "").strip() or None

        cfg.interval = _env_int(env, "CHECK_INTERVAL_SECONDS", DEFAULT_INTERVAL)
        cfg.mcp_url = (env.get("MCP_URL") or "").strip() or DEFAULT_MCP_URL
        cfg.log_file = (env.get("LOG_FILE") or "").strip() or None

        chain_raw = _split_csv(env.get("CHAIN_IDS") or "")
        if chain_raw:
            try:
                cfg.chain_ids = {int(c) for c in chain_raw}
            except ValueError:
                raise ConfigError("CHAIN_IDS debe ser una lista de IDs EVM separados por comas")

        cfg.alert_only_changes = _env_bool(env, "ALERT_ONLY_CHANGES", False)
        cfg.alert_on_recovery = _env_bool(env, "ALERT_ON_RECOVERY", True)
        cfg.notify_start = _env_bool(env, "NOTIFY_ON_START", True)
        cfg.notify_errors = _env_bool(env, "NOTIFY_ON_ERROR", True)
        cfg.heartbeat_hours = _env_float(env, "HEARTBEAT_HOURS", 0.0)
        return cfg

    def validate(self, require_telegram: bool = True) -> List[str]:
        errors: List[str] = []
        if not self.wallets:
            errors.append("WALLET_ADDRESS no está definida en el .env (se admite una lista separada por comas, máx. 5 por llamada)")
        for wallet in self.wallets:
            if not WALLET_RE.match(wallet):
                errors.append(f"WALLET_ADDRESS inválida: {wallet!r} (se espera 0x + 40 hex)")
        if len(self.wallets) > 5:
            errors.append("Se admiten como máximo 5 wallets (límite del tool get_wallet_portfolio)")
        if require_telegram:
            if not self.telegram_token:
                errors.append("TELEGRAM_BOT_TOKEN no está definido en el .env")
            if not self.telegram_chat_id:
                errors.append("TELEGRAM_CHAT_ID no está definido en el .env")
        if self.interval < 5:
            errors.append("CHECK_INTERVAL_SECONDS debe ser >= 5")
        if self.heartbeat_hours < 0:
            errors.append("HEARTBEAT_HOURS debe ser >= 0")
        return errors


# --------------------------------------------------------------------------
# Cliente MCP (Streamable HTTP, JSON-RPC 2.0)
# --------------------------------------------------------------------------

class McpClient:
    """Cliente MCP mínimo por HTTP. Soporta respuestas JSON y SSE y reintentos."""

    def __init__(self, url: str, timeout: float = TOOL_TIMEOUT):
        self.url = url
        self.timeout = timeout
        self._session_id: Optional[str] = None
        self._next_id = 0

    # -- transporte ---------------------------------------------------------

    def _post(self, payload: Dict[str, Any]) -> str:
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            # Cloudflare bloquea el User-Agent por defecto de urllib (error 1010)
            "User-Agent": "vfat-position-watchdog/1.0",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        last_error: Optional[Exception] = None
        for attempt in range(1, HTTP_ATTEMPTS + 1):
            request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    session_id = response.headers.get("Mcp-Session-Id")
                    if session_id:
                        self._session_id = session_id
                    return response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as err:
                body = ""
                try:
                    body = err.read().decode("utf-8", "replace")[:500]
                except Exception:
                    pass
                last_error = McpError(f"HTTP {err.code} del MCP: {body or err.reason}")
                if err.code in (408, 429, 500, 502, 503, 504) and attempt < HTTP_ATTEMPTS:
                    time.sleep(2 * attempt)
                    continue
                raise last_error from err
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                last_error = McpError(f"Error de red con el MCP: {err}")
                if attempt < HTTP_ATTEMPTS:
                    time.sleep(2 * attempt)
                    continue
                raise last_error from err
        raise last_error or McpError("Error desconocido llamando al MCP")

    @staticmethod
    def _decode(body: str) -> Dict[str, Any]:
        body = body.strip()
        if not body:
            return {}
        if body[0] in "{[":
            return json.loads(body)
        # El servidor puede responder como text/event-stream
        chunks = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        if not chunks:
            raise McpError(f"Respuesta MCP no reconocida: {body[:200]}")
        return json.loads(chunks[-1])

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        self._next_id += 1
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        expect_reply = not method.startswith("notifications/")
        if expect_reply:
            payload["id"] = self._next_id
        if params is not None:
            payload["params"] = params

        body = self._post(payload)
        if not expect_reply:
            return None
        message = self._decode(body)
        if message.get("error"):
            raise McpError(f"RPC {method}: {message['error']}")
        return message.get("result") or {}

    # -- sesión / tools -----------------------------------------------------

    def initialize(self) -> None:
        """Handshake MCP. El MCP de vfat es stateless: si falla, seguimos igual."""
        try:
            self._rpc("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "vfat-position-watchdog", "version": "1.0.0"},
            })
            self._rpc("notifications/initialized")
        except Exception as err:  # no crítico
            log.debug("Handshake MCP omitido: %s", err)

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments}) or {}
        if result.get("isError"):
            texts = " ".join(b.get("text", "") for b in result.get("content", []) if isinstance(b, dict))
            raise McpError(f"El tool {name} devolvió error: {texts[:500]}")
        texts = [b.get("text", "") for b in result.get("content", []) if isinstance(b, dict) and b.get("type") == "text"]
        raw = "\n".join(t for t in texts if t).strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"text": raw}


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

class Telegram:
    def __init__(self, token: str, chat_id: str, timeout: float = 30.0):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode("utf-8")

        last_error: Optional[Exception] = None
        for attempt in range(1, HTTP_ATTEMPTS + 1):
            request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    response.read()
                    return
            except urllib.error.HTTPError as err:
                body = ""
                try:
                    body = err.read().decode("utf-8", "replace")[:500]
                except Exception:
                    pass
                if err.code == 429 and attempt < HTTP_ATTEMPTS:
                    retry_after = 3
                    try:
                        retry_after = int(json.loads(body).get("parameters", {}).get("retry_after", 3))
                    except Exception:
                        pass
                    time.sleep(min(retry_after, 15))
                    last_error = TelegramError(f"Telegram 429 (rate limit): {body}")
                    continue
                raise TelegramError(f"HTTP {err.code} de Telegram: {body or err.reason}") from err
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                last_error = TelegramError(f"Error de red con Telegram: {err}")
                if attempt < HTTP_ATTEMPTS:
                    time.sleep(2 * attempt)
                    continue
                raise last_error from err
        raise last_error or TelegramError("Error desconocido enviando a Telegram")


# --------------------------------------------------------------------------
# Posiciones
# --------------------------------------------------------------------------

@dataclass
class Position:
    wallet: str
    chain_id: int
    protocol: str
    token0: str
    token1: str
    dec0: int
    dec1: int
    pos_id: str
    tick_low: float
    tick_up: float
    tick: float
    kind: str                     # "farm" (staked) | "liquidity" (sin farmear)
    value_usd: float
    rewards_usd: float
    fees_usd: float

    @property
    def key(self) -> str:
        return f"{self.chain_id}:{self.pos_id}:{self.wallet}"

    @property
    def label(self) -> str:
        return f"{self.token0}/{self.token1}"

    @property
    def in_range(self) -> bool:
        return self.tick_low <= self.tick <= self.tick_up

    def _human_price(self, tick_value: float) -> float:
        """1.0001^tick es el precio en unidades raw (token1/token0); se ajusta a decimales humanos."""
        return (TICK_BASE ** tick_value) * (10 ** (self.dec0 - self.dec1))

    def price_now(self) -> float:
        return self._human_price(self.tick)

    def price_low(self) -> float:
        return self._human_price(self.tick_low)

    def price_up(self) -> float:
        return self._human_price(self.tick_up)

    def side(self) -> Tuple[Optional[str], float]:
        """(lado por el que salió, % de distancia fuera del rango)."""
        if self.in_range:
            return None, 0.0
        now, low, up = self.price_now(), self.price_low(), self.price_up()
        if self.tick < self.tick_low:
            return "abajo", (low - now) / low * 100.0
        return "arriba", (now - up) / up * 100.0


def _find_tick(entry: Dict[str, Any]) -> Optional[float]:
    """Busca el tick actual del pool en los niveles habituales de la respuesta."""
    candidates = [entry]
    pool = entry.get("pool")
    if isinstance(pool, dict):
        candidates.append(pool)
    farm = entry.get("farm")
    if isinstance(farm, dict):
        candidates.append(farm)
        farm_pool = farm.get("pool")
        if isinstance(farm_pool, dict):
            candidates.append(farm_pool)
    for candidate in candidates:
        tick = _as_float(candidate.get("tick"))
        if tick is not None:
            return tick
    return None


def _protocol_name(entry: Dict[str, Any]) -> str:
    for holder in (entry, entry.get("farm") if isinstance(entry.get("farm"), dict) else None):
        if isinstance(holder, dict) and isinstance(holder.get("protocol"), dict):
            name = holder["protocol"].get("name")
            if name:
                return str(name)
    return str(entry.get("type") or "desconocido")


def _rewards_usd(entry: Dict[str, Any]) -> float:
    """pendingRewards[].amount viene en unidades base (raw); se divide por 10^decimals."""
    total = 0.0
    for reward in entry.get("pendingRewards") or []:
        if not isinstance(reward, dict):
            continue
        token = reward.get("token") or {}
        decimals = _as_float(token.get("decimals"))
        amount = _as_float(reward.get("amount"))
        price = _as_float(token.get("price"))
        if decimals is None or amount is None or price is None:
            continue
        total += amount / (10 ** int(decimals)) * price
    return total


def _fees_usd(nft: Dict[str, Any], underlying: List[Dict[str, Any]]) -> float:
    """fees0/fees1 son montos raw de token0/token1 pendientes de cobrar."""
    total = 0.0
    for index, key in enumerate(("fees0", "fees1")):
        if index >= len(underlying):
            break
        fees_raw = _as_float(nft.get(key))
        token = underlying[index]
        decimals = _as_float(token.get("decimals"))
        price = _as_float(token.get("price"))
        if fees_raw is None or decimals is None or price is None:
            continue
        total += fees_raw / (10 ** int(decimals)) * price
    return total


def parse_portfolio(
    payload: Dict[str, Any],
    wallets: Set[str],
    chain_ids: Optional[Set[int]],
) -> Tuple[List[Position], List[str]]:
    """Extrae posiciones con rango vigilable del resultado de get_wallet_portfolio.

    Devuelve (posiciones_vigilables, descripciones_sin_datos_de_rango).
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    positions: List[Position] = []
    uncheckable: List[str] = []

    for section, kind in (("farms", "farm"), ("liquidity", "liquidez")):
        entries = data.get(section)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            wallet = str(entry.get("wallet") or "").lower()
            if wallets and wallet not in wallets:
                continue
            try:
                chain_id = int(entry.get("chainId"))
            except (TypeError, ValueError):
                continue
            if chain_ids and chain_id not in chain_ids:
                continue

            underlying = [u for u in (entry.get("underlying") or []) if isinstance(u, dict)]
            nft = entry.get("nft") if isinstance(entry.get("nft"), dict) else {}
            tick_low = _as_float(nft.get("tickLow"))
            tick_up = _as_float(nft.get("tickUp"))
            tick = _find_tick(entry)
            token0 = str((underlying[0].get("symbol") if len(underlying) > 0 else None) or "?")
            token1 = str((underlying[1].get("symbol") if len(underlying) > 1 else None) or "?")
            pos_id = str(nft.get("id") or entry.get("id") or entry.get("address") or "?")
            summary = f"{_chain_name(chain_id)} · {token0}/{token1} · #{pos_id} · wallet {wallet or '?'}"

            if tick_low is None or tick_up is None or tick is None:
                # Par clásico (sin liquidez concentrada) o datos incompletos: no tiene rango que vigilar
                uncheckable.append(summary)
                continue
            if len(underlying) < 2:
                uncheckable.append(summary)
                continue

            value_usd = 0.0
            for token in underlying:
                balance = _as_float(token.get("balance"))
                price = _as_float(token.get("price"))
                if balance is None or price is None:
                    continue
                value_usd += balance * price

            positions.append(Position(
                wallet=wallet or "?",
                chain_id=chain_id,
                protocol=_protocol_name(entry),
                token0=token0,
                token1=token1,
                dec0=int(_as_float(underlying[0].get("decimals")) or 18),
                dec1=int(_as_float(underlying[1].get("decimals")) or 18),
                pos_id=pos_id,
                tick_low=tick_low,
                tick_up=tick_up,
                tick=tick,
                kind=kind,
                value_usd=value_usd,
                rewards_usd=_rewards_usd(entry),
                fees_usd=_fees_usd(nft, underlying),
            ))

    return positions, uncheckable


# --------------------------------------------------------------------------
# Mensajes
# --------------------------------------------------------------------------

def _describe_position(pos: Position) -> str:
    side, distance = pos.side()
    pair = f"{_esc(pos.token1)}/{_esc(pos.token0)}"
    state = f"FUERA por {side} ({distance:.2f}% {'bajo' if side == 'abajo' else 'sobre'} el límite)"
    extras: List[str] = []
    if pos.rewards_usd > 0:
        extras.append(f"recompensas {_fmt_usd(pos.rewards_usd)}")
    if pos.fees_usd > 0:
        extras.append(f"fees {_fmt_usd(pos.fees_usd)}")
    extra_text = (" · " + " · ".join(extras)) if extras else ""
    return "\n".join([
        f"<b>{_esc(pos.label)}</b> · {_esc(pos.protocol)} · {_chain_name(pos.chain_id)} · ID {_esc(pos.pos_id)} · {pos.kind}",
        f"  Wallet: <code>{pos.wallet}</code>",
        f"  Estado: <b>❌ {state}</b>",
        f"  Rango: {_fmt_price(pos.price_low())} → {_fmt_price(pos.price_up())} {pair}"
        f"  (ticks {int(pos.tick_low)} … {int(pos.tick_up)})",
        f"  Precio actual: {_fmt_price(pos.price_now())} {pair}",
        f"  Valor: {_fmt_usd(pos.value_usd)}{extra_text}",
    ])


def _describe_recovery(pos: Position) -> str:
    return (f"<b>{_esc(pos.label)}</b> · {_esc(pos.protocol)} · {_chain_name(pos.chain_id)} · "
            f"ID {_esc(pos.pos_id)} · wallet <code>{pos.wallet}</code>")


def _split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> List[str]:
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            parts.append(current)
            current = block
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _send(tg: Optional[Telegram], text: str, dry_run: bool) -> None:
    if tg is None or dry_run:
        log.info("[dry-run] Mensaje para Telegram:\n%s", text)
        return
    for part in _split_message(text):
        tg.send(part)


# --------------------------------------------------------------------------
# Estado entre ejecuciones
# --------------------------------------------------------------------------

@dataclass
class State:
    """Memoria entre ciclos: último estado por posición y del propio demonio."""
    range_status: Dict[str, str] = field(default_factory=dict)  # key -> "in" | "out"
    last_run_failed: bool = False
    last_heartbeat: float = field(default_factory=time.time)


# --------------------------------------------------------------------------
# Resultado de una comprobación (serializable, para la interfaz web)
# --------------------------------------------------------------------------

def _finite(value: Any) -> Optional[float]:
    """float finito para JSON (None si es NaN/infinito o no convertible)."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def position_summary(pos: Position) -> Dict[str, Any]:
    """Representación serializable (JSON) de una posición, para la interfaz web."""
    side, distance = pos.side()

    def _call(fn) -> Optional[float]:
        try:
            return _finite(fn())
        except (OverflowError, ValueError, ZeroDivisionError):
            return None

    return {
        "key": pos.key,
        "pair": pos.label,
        "protocol": pos.protocol,
        "chain": _chain_name(pos.chain_id),
        "chain_id": pos.chain_id,
        "id": pos.pos_id,
        "wallet": pos.wallet,
        "kind": pos.kind,
        "in_range": pos.in_range,
        "side": side,
        "distance_pct": _finite(distance),
        "price_now": _call(pos.price_now),
        "price_low": _call(pos.price_low),
        "price_up": _call(pos.price_up),
        "value_usd": _finite(pos.value_usd),
        "rewards_usd": _finite(pos.rewards_usd),
        "fees_usd": _finite(pos.fees_usd),
        "tick": _finite(pos.tick),
        "tick_low": _finite(pos.tick_low),
        "tick_up": _finite(pos.tick_up),
    }


@dataclass
class CheckResult:
    """Resumen de una comprobación (para la interfaz web y logs estructurados)."""
    ts: float
    ok: bool
    duration: float = 0.0
    in_range: List[Position] = field(default_factory=list)
    out_range: List[Position] = field(default_factory=list)
    uncheckable: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "ok": self.ok,
            "duration": round(_finite(self.duration) or 0.0, 2),
            "total": len(self.in_range) + len(self.out_range),
            "in_range": len(self.in_range),
            "out_range": len(self.out_range),
            # primero las que están fuera de rango: son las interesantes
            "positions": ([position_summary(p) for p in self.out_range] +
                          [position_summary(p) for p in self.in_range]),
            "uncheckable": self.uncheckable,
            "error": self.error,
        }


# --------------------------------------------------------------------------
# Ciclo de comprobación
# --------------------------------------------------------------------------

def run_once(cfg: Config, mcp: McpClient, tg: Optional[Telegram], state: State,
             dry_run: bool) -> CheckResult:
    started = time.monotonic()
    positions: List[Position] = []
    uncheckable: List[str] = []

    for batch in _chunks(cfg.wallets, 5):  # límite del tool get_wallet_portfolio
        payload = mcp.call_tool("get_wallet_portfolio", {
            "addresses": list(batch),
            "includeTokens": False,
            "includeBorrows": False,
            "includeFarms": True,
            "includeLiquidity": True,
        })
        found, missing = parse_portfolio(payload, set(batch), cfg.chain_ids)
        positions.extend(found)
        uncheckable.extend(missing)

    in_range = [p for p in positions if p.in_range]
    out_range = [p for p in positions if not p.in_range]
    elapsed = time.monotonic() - started
    log.info("Comprobación: %d posición(es) vigiladas · %d en rango · %d fuera de rango (%.1fs)",
             len(positions), len(in_range), len(out_range), elapsed)
    if uncheckable:
        log.info("Sin datos de rango (no vigilables): %s", " | ".join(uncheckable))

    # Estado actual de cada posición
    now_status = {p.key: ("out" if not p.in_range else "in") for p in positions}
    previous_status = state.range_status

    # 1) Aviso de posiciones fuera de rango
    to_alert = out_range
    if cfg.alert_only_changes:
        newly_out = [p for p in out_range if previous_status.get(p.key) != "out"]
        to_alert = newly_out
    if to_alert:
        header = f"🚨 <b>vfat · {len(to_alert)} posición(es) FUERA de rango</b>"
        body = "\n\n".join(_describe_position(p) for p in to_alert)
        _send(tg, f"{header}\n\n{body}", dry_run)
    elif not out_range:
        log.info("Todas las posiciones vigiladas están dentro de rango ✔")
    else:
        log.info("%d posición(es) siguen fuera de rango (sin aviso nuevo: ALERT_ONLY_CHANGES=true)",
                 len(out_range))

    # 2) Aviso de retorno a rango
    if cfg.alert_on_recovery:
        recovered = [p for p in in_range if previous_status.get(p.key) == "out"]
        if recovered:
            lines = "\n".join(f"  • {line}" for line in map(_describe_recovery, recovered))
            _send(tg, f"✅ <b>vfat · {len(recovered)} posición(es) de nuevo EN rango</b>\n{lines}", dry_run)

    state.range_status = now_status

    # 3) Latido periódico opcional
    if cfg.heartbeat_hours > 0 and (time.time() - state.last_heartbeat) >= cfg.heartbeat_hours * 3600:
        _send(tg, (
            f"💓 <b>vfat-watchdog activo</b>\n"
            f"Posiciones vigiladas: {len(positions)} · en rango: {len(in_range)} · fuera: {len(out_range)}"
        ), dry_run)
        state.last_heartbeat = time.time()

    return CheckResult(
        ts=time.time(),
        ok=True,
        duration=elapsed,
        in_range=in_range,
        out_range=out_range,
        uncheckable=uncheckable,
    )


# --------------------------------------------------------------------------
# Demonio
# --------------------------------------------------------------------------

def _sleep_interruptible(seconds: float, stop: Dict[str, bool]) -> None:
    deadline = time.monotonic() + seconds
    while not stop["flag"]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vfat_watchdog.py",
        description="Vigila las posiciones LP de una wallet vía MCP de vfat y avisa por Telegram al salir de rango.",
    )
    parser.add_argument("--env", default=".env", help="ruta del fichero .env (por defecto ./.env)")
    parser.add_argument("--once", action="store_true", help="ejecuta una comprobación y termina")
    parser.add_argument("--dry-run", action="store_true", help="no envía nada a Telegram (solo logs)")
    parser.add_argument("--interval", type=int, help="segundos entre comprobaciones (pisa CHECK_INTERVAL_SECONDS)")
    parser.add_argument("--verbose", action="store_true", help="logs de depuración")
    args = parser.parse_args(argv)

    load_env(args.env)
    try:
        cfg = Config.from_env(os.environ)
    except ConfigError as err:
        print(f"ERROR de configuración: {err}", file=sys.stderr)
        return 2
    if args.interval is not None:
        cfg.interval = args.interval

    setup_logging(cfg.log_file, args.verbose)

    errors = cfg.validate(require_telegram=not args.dry_run)
    if errors:
        for error in errors:
            log.error("Configuración: %s", error)
        log.error("Corrige el .env (ver .env.example) y vuelve a lanzar el demonio.")
        return 2

    log.info("vfat-position-watchdog iniciado · wallets=%d (%s) · intervalo=%ds · MCP=%s%s",
             len(cfg.wallets), ", ".join(cfg.wallets), cfg.interval, cfg.mcp_url,
             " · dry-run" if args.dry_run else "")

    telegram: Optional[Telegram] = None
    if not args.dry_run and cfg.telegram_token and cfg.telegram_chat_id:
        telegram = Telegram(cfg.telegram_token, cfg.telegram_chat_id)

    mcp = McpClient(cfg.mcp_url)
    mcp.initialize()

    state = State()
    stop: Dict[str, bool] = {"flag": False}

    def _handle_signal(signum: int, _frame: Any) -> None:
        stop["flag"] = True
        log.info("Señal %s recibida: cerrando tras el ciclo actual…", signal.Signals(signum).name)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if cfg.notify_start and telegram and not args.dry_run:
        try:
            _send(telegram, (
                "🤖 <b>vfat-position-watchdog activo</b>\n"
                f"Wallets: <code>{'</code><code>'.join(cfg.wallets)}</code>\n"
                f"Cada {cfg.interval} s · MCP {_esc(cfg.mcp_url)}\n"
                "Avisaré en cada ejecución en la que haya posiciones fuera de rango."
            ), dry_run=False)
        except Exception as err:
            log.warning("No se pudo enviar el mensaje de arranque: %s", err)

    last_run_ok = True
    while not stop["flag"]:
        try:
            run_once(cfg, mcp, telegram, state, dry_run=args.dry_run)
            if state.last_run_failed and cfg.notify_errors and telegram and not args.dry_run:
                try:
                    _send(telegram, "✅ <b>vfat-watchdog: la comprobación vuelve a funcionar</b>", dry_run=False)
                except Exception as err:
                    log.warning("No se pudo avisar de la recuperación: %s", err)
            state.last_run_failed = False
            last_run_ok = True
        except KeyboardInterrupt:
            break
        except Exception as err:
            log.exception("Fallo en la comprobación: %s", err)
            last_run_ok = False
            if cfg.notify_errors and not state.last_run_failed and telegram and not args.dry_run:
                try:
                    _send(telegram, (
                        f"⚠️ <b>vfat-watchdog: la comprobación ha fallado</b>\n"
                        f"{_esc(str(err)[:500])}\nReintento en {cfg.interval} s."
                    ), dry_run=False)
                except Exception as notify_err:
                    log.warning("No se pudo enviar el aviso de error: %s", notify_err)
            state.last_run_failed = True
            # el demonio sigue vivo; el reintento ocurre en el próximo ciclo

        if args.once:
            break
        _sleep_interruptible(cfg.interval, stop)

    log.info("vfat-position-watchdog detenido")
    return 0 if last_run_ok else 1


if __name__ == "__main__":
    sys.exit(main())
