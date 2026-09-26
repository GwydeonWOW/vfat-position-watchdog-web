#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vfat-position-watchdog · servidor web
=====================================

Interfaz web estilo terminal para el watchdog: sirve una consola HTML (webapp.html)
y una API JSON sobre el mismo núcleo que el demonio CLI (vfat_watchdog.py).
Solo usa la librería estándar de Python 3.8+.

Uso:
    python web_server.py                          # http://127.0.0.1:8000
    python web_server.py --port 8080              # otro puerto
    python web_server.py --host 0.0.0.0           # accesible desde la red
    python web_server.py --password sekreto       # exige contraseña en la web
    python web_server.py --no-autostart           # no vigilar al arrancar

Configuración web (en el .env, ver .env.example):
    WEB_HOST, WEB_PORT, WEB_PASSWORD, WEB_AUTOSTART
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import secrets
import sys
import threading
import time
from http import cookies as http_cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import vfat_watchdog as vw  # noqa: E402  (necesita sys.path apuntando a este directorio)

log = logging.getLogger("vfat-web")

APP_FILE = os.path.join(BASE_DIR, "webapp.html")
MAX_LOG_LINES = 2000            # líneas de log conservadas para la consola web
SESSION_TTL = 7 * 24 * 3600     # validez de la sesión web (s)
MAX_LOGIN_FAILS = 5             # intentos de login fallidos ...
LOGIN_WINDOW = 120              # ... dentro de esta ventana (s) -> bloqueo
MAX_BODY_BYTES = 8192


# --------------------------------------------------------------------------
# Log con búfer circular (para /api/logs)
# --------------------------------------------------------------------------

class RingLogHandler(logging.Handler):
    """Handler que conserva las últimas líneas de log para la interfaz web."""

    def __init__(self, capacity: int = MAX_LOG_LINES):
        super().__init__()
        self.buffer: Deque[Dict[str, Any]] = collections.deque(maxlen=capacity)
        self._next_id = 0
        self._lock_ids = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self._lock_ids:
                self._next_id += 1
                line_id = self._next_id
                line = {
                    "id": line_id,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
                    "level": record.levelname,
                    "msg": self.format(record),
                }
            with self._lock_ids:
                self.buffer.append(line)
        except Exception:  # pragma: no cover
            self.handleError(record)

    def since(self, after_id: int) -> List[Dict[str, Any]]:
        with self._lock_ids:
            return [line for line in self.buffer if line["id"] > after_id]

    def last_id(self) -> int:
        with self._lock_ids:
            return self._next_id


RING = RingLogHandler()


def setup_logging(log_file: Optional[str], verbose: bool) -> None:
    root = logging.getLogger()
    if any(isinstance(h, RingLogHandler) for h in root.handlers):
        return  # ya configurado (p. ej. en tests que importan el módulo dos veces)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")
    fmt.converter = time.gmtime  # logs en UTC, como el demonio CLI

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    RING.setFormatter(fmt)
    root.addHandler(RING)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)


# --------------------------------------------------------------------------
# Runtime del watchdog (bucle en un hilo, controlable desde la web)
# --------------------------------------------------------------------------

class WatchdogRuntime:
    """Ejecuta el bucle de vigilancia en un hilo y expone su estado."""

    def __init__(self, cfg: vw.Config,
                 mcp_factory: Optional[Callable[[vw.Config], Any]] = None):
        self.cfg = cfg
        self.state = vw.State()
        self.lock = threading.RLock()          # protege cfg y los campos de estado
        self.check_lock = threading.Lock()     # solo una comprobación a la vez
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()    # "check ahora" interrumpe la espera
        self.thread: Optional[threading.Thread] = None
        self.started_at: Optional[float] = None
        self.next_check_at: Optional[float] = None
        self.checking = False
        self.last_result: Optional[vw.CheckResult] = None
        self.last_error: Optional[str] = None
        self.was_error = False
        self.mcp_factory = mcp_factory or (lambda config: vw.McpClient(config.mcp_url))

    # -- control -----------------------------------------------------------

    def is_running(self) -> bool:
        return not self.stop_event.is_set() and bool(self.thread and self.thread.is_alive())

    def start(self, notify: bool = True) -> bool:
        with self.lock:
            if self.is_running():
                return False
            cfg = self.cfg
            self.stop_event.clear()
            self.wake_event.clear()
            self.started_at = time.time()
            self.thread = threading.Thread(target=self._loop, name="watchdog-loop", daemon=True)
            self.thread.start()
        if notify and cfg.notify_start and cfg.telegram_token and cfg.telegram_chat_id:
            try:
                vw._send(vw.Telegram(cfg.telegram_token, cfg.telegram_chat_id), (
                    "🤖 <b>vfat-position-watchdog activo</b> (modo web)\n"
                    f"Wallets: <code>{'</code><code>'.join(cfg.wallets)}</code>\n"
                    f"Cada {cfg.interval} s · MCP {vw._esc(cfg.mcp_url)}"
                ), dry_run=False)
            except Exception as err:
                log.warning("No se pudo enviar el mensaje de arranque: %s", err)
        return True

    def stop(self) -> bool:
        if not self.is_running():
            return False
        self.stop_event.set()
        self.wake_event.set()  # interrumpe la espera
        thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=10)
        with self.lock:
            self.thread = None
            self.started_at = None
            self.next_check_at = None
        return True

    def check_now(self) -> Dict[str, Any]:
        """Fuerza una comprobación inmediata (encola si el bucle está activo)."""
        if self.is_running():
            self.wake_event.set()
            return {"scheduled": True, "message": "comprobación inmediata encolada"}
        if self.check_lock.locked():
            return {"scheduled": False, "message": "ya hay una comprobación en curso"}
        threading.Thread(target=self._do_check, name="watchdog-manual", daemon=True).start()
        return {"scheduled": False, "message": "comprobación manual lanzada"}

    # -- bucle -------------------------------------------------------------

    def _loop(self) -> None:
        with self.lock:
            interval = self.cfg.interval
        log.info("Demonio web arrancado · intervalo=%ds · wallets=%s", interval,
                 ",".join(self.cfg.wallets) or "(ninguna)")
        while not self.stop_event.is_set():
            self._do_check()
            with self.lock:
                interval = max(5, self.cfg.interval)  # releído: puede cambiar en caliente
            if self.stop_event.is_set():
                break
            self.next_check_at = time.time() + interval
            self.wake_event.clear()
            interrupted = self.wake_event.wait(timeout=interval)
            if interrupted and not self.stop_event.is_set():
                log.info("Comprobación inmediata solicitada desde la web")
        self.next_check_at = None
        log.info("Demonio web detenido")

    def _do_check(self) -> Optional[vw.CheckResult]:
        if not self.check_lock.acquire(blocking=False):
            log.info("Se ignora una solicitud de comprobación: ya hay una en curso")
            return None
        self.checking = True
        try:
            with self.lock:
                cfg = self.cfg
            mcp = self.mcp_factory(cfg)
            if hasattr(mcp, "initialize"):
                try:
                    mcp.initialize()
                except Exception as err:
                    log.debug("Handshake MCP omitido: %s", err)
            telegram = (vw.Telegram(cfg.telegram_token, cfg.telegram_chat_id)
                        if cfg.telegram_token and cfg.telegram_chat_id else None)
            try:
                result = vw.run_once(cfg, mcp, telegram, self.state, dry_run=False)
                with self.lock:
                    self.last_result = result
                    self.last_error = None
                if self.was_error and cfg.notify_errors and telegram is not None:
                    try:
                        vw._send(telegram, "✅ <b>vfat-watchdog: la comprobación vuelve a funcionar</b>", dry_run=False)
                    except Exception as err:
                        log.warning("No se pudo avisar de la recuperación: %s", err)
                self.was_error = False
                return result
            except Exception as err:
                error = f"{type(err).__name__}: {err}"[:500]
                with self.lock:
                    self.last_error = error
                log.exception("Fallo en la comprobación: %s", err)
                if not self.was_error and cfg.notify_errors and telegram is not None:
                    try:
                        vw._send(telegram, (
                            f"⚠️ <b>vfat-watchdog: la comprobación ha fallado</b>\n"
                            f"{vw._esc(str(err)[:500])}"
                        ), dry_run=False)
                    except Exception as notify_err:
                        log.warning("No se pudo enviar el aviso de error: %s", notify_err)
                self.was_error = True
                return vw.CheckResult(ts=time.time(), ok=False, error=error)
        finally:
            self.checking = False
            self.check_lock.release()

    # -- configuración en caliente ------------------------------------------

    def apply_config(self, updates: Dict[str, Any]) -> List[str]:
        """Valida y aplica cambios de configuración. Devuelve lista de errores."""
        errors: List[str] = []
        parsed: Dict[str, Any] = {}

        if "wallets" in updates:
            raw = updates["wallets"]
            if isinstance(raw, str):
                items = vw._split_csv(raw)
            elif isinstance(raw, list):
                items = [str(w).strip().lower() for w in raw if str(w).strip()]
            else:
                items = []
                errors.append("wallets debe ser una lista o un CSV de direcciones")
            if not errors:
                items = [w.lower() for w in items]
                if not items:
                    errors.append("wallets no puede quedar vacío (edita el .env si quieres vaciarlo)")
                for wallet in items:
                    if not vw.WALLET_RE.match(wallet):
                        errors.append(f"wallet inválida: {wallet!r} (se espera 0x + 40 hex)")
                if len(items) > 5:
                    errors.append("máximo 5 wallets (límite del tool get_wallet_portfolio)")
                if not errors:
                    parsed["wallets"] = items

        if "interval" in updates:
            try:
                value = int(str(updates["interval"]).strip())
            except (TypeError, ValueError):
                errors.append("interval debe ser un entero de segundos")
            else:
                if value < 5:
                    errors.append("interval debe ser >= 5")
                else:
                    parsed["interval"] = value

        if "chain_ids" in updates:
            raw = updates["chain_ids"]
            if isinstance(raw, str):
                items = vw._split_csv(raw)
            elif isinstance(raw, list):
                items = [str(c).strip() for c in raw if str(c).strip()]
            else:
                items = []
                errors.append("chain_ids debe ser una lista o un CSV de números")
            ids: List[int] = []
            for item in items:
                try:
                    ids.append(int(item))
                except ValueError:
                    errors.append(f"chain_ids contiene un valor no numérico: {item!r}")
            if not errors:
                parsed["chain_ids"] = set(ids) if ids else None

        for key in ("alert_only_changes", "alert_on_recovery", "notify_start", "notify_errors"):
            if key in updates:
                raw = str(updates[key]).strip().lower()
                if raw in vw._TRUE:
                    parsed[key] = True
                elif raw in vw._FALSE:
                    parsed[key] = False
                else:
                    errors.append(f"{key} debe ser true/false")

        if "heartbeat_hours" in updates:
            try:
                value = float(str(updates["heartbeat_hours"]).strip())
            except (TypeError, ValueError):
                errors.append("heartbeat_hours debe ser un número de horas")
            else:
                if value < 0:
                    errors.append("heartbeat_hours debe ser >= 0")
                else:
                    parsed["heartbeat_hours"] = value

        for key in ("telegram_token", "telegram_chat_id"):
            if key in updates:
                value = str(updates[key]).strip()
                if key == "telegram_token" and value and ":" not in value:
                    errors.append("telegram_token no parece un token de BotFather (debe llevar ':')")
                else:
                    parsed[key] = value or None

        if errors:
            return errors

        with self.lock:
            for key, value in parsed.items():
                setattr(self.cfg, key, value)
        log.info("Configuración actualizada desde la web: %s", ", ".join(sorted(parsed)))
        return []


def masked_config(cfg: vw.Config) -> Dict[str, Any]:
    token = cfg.telegram_token or ""
    return {
        "wallets": cfg.wallets,
        "interval": cfg.interval,
        "chain_ids": sorted(cfg.chain_ids) if cfg.chain_ids else [],
        "alert_only_changes": cfg.alert_only_changes,
        "alert_on_recovery": cfg.alert_on_recovery,
        "notify_start": cfg.notify_start,
        "notify_errors": cfg.notify_errors,
        "heartbeat_hours": cfg.heartbeat_hours,
        "telegram_token": ("…" + token[-4:]) if len(token) > 4 else ("…" if token else ""),
        "telegram_chat_id": cfg.telegram_chat_id or "",
        "mcp_url": cfg.mcp_url,
    }


# --------------------------------------------------------------------------
# Servidor HTTP
# --------------------------------------------------------------------------

class WatchdogServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], runtime: WatchdogRuntime,
                 web_password: str, app_file: str = APP_FILE):
        self.runtime = runtime
        self.web_password = web_password
        self.app_file = app_file
        self.sessions: Dict[str, float] = {}
        self.login_failures: Dict[str, List[float]] = collections.defaultdict(list)
        super().__init__(address, WatchdogHandler)


class WatchdogHandler(BaseHTTPRequestHandler):
    server_version = "vfat-watchdog-web/1.0"
    protocol_version = "HTTP/1.1"
    timeout = 65  # cierra conexiones keep-alive paradas

    # -- infraestructura -----------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s %s", self.client_address[0], fmt % args)

    def _json(self, payload: Any, status: int = 200,
              extra_headers: Optional[List[Tuple[str, str]]] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or []):
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Content-Length inválido")
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("cuerpo vacío o demasiado grande")
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("se esperaba un objeto JSON")
        return data

    def _authorized(self) -> bool:
        password = self.server.web_password  # type: ignore[attr-defined]
        if not password:
            return True
        jar = http_cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except Exception:
            return False
        morsel = jar.get("vfat_sid")
        if not morsel:
            return False
        sessions = self.server.sessions  # type: ignore[attr-defined]
        sid = morsel.value
        now = time.time()
        last_seen = sessions.get(sid)
        if last_seen is None or now - last_seen > SESSION_TTL:
            sessions.pop(sid, None)
            return False
        sessions[sid] = now  # renueva la sesión (validez deslizante)
        return True

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            return urlsplit(origin).netloc == (self.headers.get("Host") or "")
        except ValueError:
            return False

    def _serve_app(self) -> None:
        try:
            with open(self.server.app_file, "rb") as handle:  # type: ignore[attr-defined]
                body = handle.read()
        except OSError:
            self._json({"error": "webapp.html no encontrado junto a web_server.py"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; connect-src 'self'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- rutas ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                self._serve_app()
            elif path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif not self._authorized():
                self._json({"error": "no autenticado"}, 401)
            elif path == "/api/status":
                self._api_status()
            elif path == "/api/logs":
                self._api_logs(parsed.query)
            elif path == "/api/config":
                self._json(masked_config(self.server.runtime.cfg))  # type: ignore[attr-defined]
            else:
                self._json({"error": "ruta no encontrada"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as err:
            log.exception("Error atendiendo GET %s: %s", self.path, err)
            try:
                self._json({"error": "error interno"}, 500)
            except Exception:
                pass

    def do_POST(self) -> None:  # noqa: N802
        try:
            if not self._origin_ok():
                self._json({"error": "origen no permitido"}, 403)
                return
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/login":
                self._api_login()
                return
            if not self._authorized():
                self._json({"error": "no autenticado"}, 401)
                return
            if path == "/api/logout":
                self._api_logout()
            elif path == "/api/action":
                self._api_action()
            else:
                self._json({"error": "ruta no encontrada"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as err:
            log.exception("Error atendiendo POST %s: %s", self.path, err)
            try:
                self._json({"error": "error interno"}, 500)
            except Exception:
                pass

    # -- API -----------------------------------------------------------------

    def _api_status(self) -> None:
        runtime: WatchdogRuntime = self.server.runtime  # type: ignore[attr-defined]
        with runtime.lock:
            cfg = runtime.cfg
        payload = {
            "running": runtime.is_running(),
            "checking": runtime.checking,
            "now": time.time(),
            "next_check_at": runtime.next_check_at,
            "uptime": (time.time() - runtime.started_at) if (runtime.is_running() and runtime.started_at) else None,
            "interval": cfg.interval,
            "wallets": cfg.wallets,
            "config_errors": cfg.validate(require_telegram=False),
            "telegram_configured": bool(cfg.telegram_token and cfg.telegram_chat_id),
            "auth_enabled": bool(self.server.web_password),  # type: ignore[attr-defined]
            "last_error": runtime.last_error,
            "last_result": runtime.last_result.to_dict() if runtime.last_result else None,
        }
        self._json(payload)

    def _api_logs(self, query: str) -> None:
        params = parse_qs(query)
        try:
            after = int(params.get("after", ["0"])[0])
        except (TypeError, ValueError):
            after = 0
        self._json({"lines": RING.since(after), "last_id": RING.last_id()})

    def _api_login(self) -> None:
        try:
            body = self._read_json()
        except (ValueError, json.JSONDecodeError) as err:
            self._json({"ok": False, "message": f"petición inválida: {err}"}, 400)
            return
        password = str(body.get("password") or "")
        expected = self.server.web_password or ""  # type: ignore[attr-defined]
        if not expected:
            self._json({"ok": True, "message": "no hay contraseña configurada"})
            return
        ip = self.client_address[0]
        now = time.time()
        failures = self.server.login_failures  # type: ignore[attr-defined]
        recent = [t for t in failures.get(ip, []) if now - t < LOGIN_WINDOW]
        if len(recent) >= MAX_LOGIN_FAILS:
            self._json({"ok": False, "message": "demasiados intentos fallidos; espera un momento"}, 429)
            return
        if secrets.compare_digest(password.encode("utf-8"), expected.encode("utf-8")):
            failures.pop(ip, None)
            sid = secrets.token_urlsafe(32)
            self.server.sessions[sid] = now  # type: ignore[attr-defined]
            self._json({"ok": True, "message": "acceso concedido"},
                       extra_headers=[("Set-Cookie",
                                       f"vfat_sid={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}")])
        else:
            recent.append(now)
            failures[ip] = recent
            self._json({"ok": False, "message": "contraseña incorrecta"}, 401)

    def _api_logout(self) -> None:
        jar = http_cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except Exception:
            pass
        morsel = jar.get("vfat_sid")
        if morsel:
            self.server.sessions.pop(morsel.value, None)  # type: ignore[attr-defined]
        self._json({"ok": True, "message": "sesión cerrada"},
                   extra_headers=[("Set-Cookie", "vfat_sid=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")])

    def _api_action(self) -> None:
        try:
            body = self._read_json()
        except (ValueError, json.JSONDecodeError) as err:
            self._json({"ok": False, "message": f"petición inválida: {err}"}, 400)
            return
        runtime: WatchdogRuntime = self.server.runtime  # type: ignore[attr-defined]
        action = str(body.get("action") or "")

        if action == "logout":
            self._api_logout()
        elif action == "start":
            changed = runtime.start()
            self._json({"ok": True, "message": "demonio arrancado" if changed else "el demonio ya estaba en marcha"})
        elif action == "stop":
            changed = runtime.stop()
            self._json({"ok": True, "message": "demonio detenido" if changed else "el demonio ya estaba parado"})
        elif action == "check":
            self._json({"ok": True, **runtime.check_now()})
        elif action == "test-telegram":
            with runtime.lock:
                cfg = runtime.cfg
            if not (cfg.telegram_token and cfg.telegram_chat_id):
                self._json({"ok": False,
                            "message": "Telegram no está configurado (set telegram_token … / set telegram_chat_id …)"}, 400)
                return
            try:
                vw.Telegram(cfg.telegram_token, cfg.telegram_chat_id).send(
                    "🔔 <b>vfat-watchdog</b> · mensaje de prueba desde la interfaz web")
                self._json({"ok": True, "message": "mensaje de prueba enviado"})
            except Exception as err:
                self._json({"ok": False, "message": f"falló el envío: {err}"}, 502)
        elif action == "save":
            updates = body.get("config")
            if not isinstance(updates, dict):
                self._json({"ok": False, "message": "config debe ser un objeto"}, 400)
                return
            errors = runtime.apply_config(updates)
            if errors:
                self._json({"ok": False, "errors": errors, "message": "no se guardó nada"}, 400)
            else:
                with runtime.lock:
                    shown = masked_config(runtime.cfg)
                self._json({"ok": True, "message": "configuración guardada", "config": shown})
        else:
            self._json({"ok": False, "message": f"acción desconocida: {action!r}"}, 400)


# --------------------------------------------------------------------------
# Entrada
# --------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="web_server.py",
        description="vfat-position-watchdog · interfaz web estilo terminal (solo librería estándar).",
    )
    parser.add_argument("--env", default=".env", help="ruta del fichero .env (por defecto ./.env)")
    parser.add_argument("--host", help="dirección de escucha (por defecto WEB_HOST o 127.0.0.1)")
    parser.add_argument("--port", type=int, help="puerto TCP (por defecto WEB_PORT o 8000)")
    parser.add_argument("--password", help="contraseña de acceso a la web (por defecto WEB_PASSWORD del .env)")
    parser.add_argument("--no-autostart", action="store_true",
                        help="no arrancar el bucle de vigilancia al iniciar el servidor")
    parser.add_argument("--verbose", action="store_true", help="logs de depuración")
    args = parser.parse_args(argv)

    vw.load_env(args.env)
    try:
        cfg = vw.Config.from_env(os.environ)
    except vw.ConfigError as err:
        print(f"ERROR de configuración: {err}", file=sys.stderr)
        return 2

    setup_logging(cfg.log_file, args.verbose)

    host = (args.host or os.environ.get("WEB_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = args.port if args.port is not None else int((os.environ.get("WEB_PORT") or "8000").strip())
    except ValueError:
        print("WEB_PORT debe ser un entero", file=sys.stderr)
        return 2
    password = args.password if args.password is not None else (os.environ.get("WEB_PASSWORD") or "").strip()
    autostart = not args.no_autostart and vw._env_bool(os.environ, "WEB_AUTOSTART", True)

    for error in cfg.validate(require_telegram=False):
        log.warning("Configuración: %s", error)
    if cfg.validate(require_telegram=False):
        log.warning("El servidor arranca igualmente; corrige la configuración desde la web ('set …') o en el .env")

    runtime = WatchdogRuntime(cfg)
    try:
        server = WatchdogServer((host, port), runtime, password)
    except OSError as err:
        log.error("No se pudo escuchar en %s:%d: %s", host, port, err)
        return 1

    log.info("vfat-watchdog web escuchando en http://%s:%d · contraseña %s · autostart %s",
             host, port, "activada" if password else "DESACTIVADA (usa --password si lo expones)",
             "sí" if autostart else "no")
    print(f"\n  vfat-position-watchdog · interfaz web → http://{host}:{port}\n")

    if autostart:
        runtime.start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Cerrando el servidor web…")
        runtime.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
