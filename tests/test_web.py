# -*- coding: utf-8 -*-
"""Tests del modo web (web_server.py): API, runtime y login con un MCP falso."""
import http.cookiejar
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # necesario para dataclasses en Python 3.12+
    spec.loader.exec_module(mod)
    return mod


vw = load("vw", "vfat_watchdog.py")
ws = load("ws", "web_server.py")

WALLET = "0xabc0000000000000000000000000000000000001"


def pos(nft_id, tick_low, tick_up, tick, wallet=WALLET, chain=8453):
    return {
        "chainId": chain, "wallet": wallet, "symbol": "UNI-V3", "decimals": 0,
        "address": "0xpool", "id": nft_id, "balance": "1", "type": "AERO_SLIPSTREAM_GAUGE",
        "tick": tick, "tickSpacing": 100,
        "protocol": {"id": "aerodrome", "name": "Aerodrome"},
        "pendingRewards": [{"amount": "1059998483846442349637",
                            "token": {"symbol": "AERO", "decimals": 18, "price": 0.6132}}],
        "underlying": [
            {"symbol": "WETH", "decimals": 18, "balance": "403.58", "price": 2488.32},
            {"symbol": "cbBTC", "decimals": 8, "balance": "1.1867", "price": 78537.75},
        ],
        "nft": {"id": nft_id, "tickLow": tick_low, "tickUp": tick_up,
                "fees0": "500000000000000000", "fees1": "30000",
                "managerAddress": "0xm", "ownerAddress": wallet, "poolAddress": "0xpool"},
        "farm": {"address": "0xgauge", "protocol": {"name": "Aerodrome"}},
    }


PAYLOAD = {"data": {"farms": [
    pos("111", -264800, -264700, -270000),          # fuera por abajo
    pos("222", 25200, 27200, 31500),                # fuera por arriba
    pos("333", -264800, -264700, -264750),          # en rango
], "liquidity": [
    {"chainId": 8453, "wallet": WALLET, "symbol": "USDC/USDT LP", "balance": "10",
     "address": "0xlp", "type": "LP_TOKEN", "underlying": []},
]}}


class FakeMcp:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def initialize(self):
        return {"jsonrpc": "2.0", "result": {"serverInfo": {"name": "fake"}}}

    def call_tool(self, name, arguments):
        self.calls += 1
        return self.payload


def make_runtime(**config_overrides):
    cfg = vw.Config(wallets=[WALLET], interval=5, telegram_token=None, telegram_chat_id=None)
    for key, value in config_overrides.items():
        setattr(cfg, key, value)
    return ws.WatchdogRuntime(cfg, mcp_factory=lambda _cfg: FakeMcp(PAYLOAD))


def request(opener, url, data=None, headers=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method="POST" if body else "GET")
    for key, value in {"Content-Type": "application/json", **(headers or {})}.items():
        req.add_header(key, value)
    try:
        with opener.open(req, timeout=10) as res:
            return res.status, json.loads(res.read().decode())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode())


def main():
    ws.setup_logging(None, False)

    # --- runtime: comprobación directa -----------------------------------
    rt = make_runtime()
    result = rt._do_check()
    assert result and result.ok, "la comprobación debería salir bien"
    assert len(result.out_range) == 2 and len(result.in_range) == 1, (result.in_range, result.out_range)
    assert any("0xlp" in entry for entry in result.uncheckable), result.uncheckable
    data = result.to_dict()
    assert data["total"] == 3 and len(data["positions"]) == 3
    first = data["positions"][0]
    assert first["in_range"] is False and first["distance_pct"] > 0
    assert first["price_now"] and first["value_usd"] > 0
    assert all(isinstance(p["price_now"], (int, float)) for p in data["positions"])
    print(">>> runtime: check directo OK")

    # --- servidor sin contraseña ------------------------------------------
    server = ws.WatchdogServer(("127.0.0.1", 0), rt, "")
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    opener = urllib.request.build_opener()
    base = f"http://127.0.0.1:{port}"

    status, payload = request(opener, base + "/api/status")
    assert status == 200 and payload["running"] is False
    assert payload["last_result"]["out_range"] == 2
    assert payload["auth_enabled"] is False
    assert payload["interval"] == 5
    print(">>> API /api/status OK")

    with urllib.request.urlopen(base + "/", timeout=10) as res:
        html = res.read().decode()
    assert "vfat" in html.lower() and "watchdog@vfat" in html
    print(">>> interfaz web servida OK")

    status, payload = request(opener, base + "/api/logs?after=0")
    assert status == 200 and payload["last_id"] > 0 and payload["lines"], payload
    print(f">>> API /api/logs OK ({len(payload['lines'])} líneas)")

    # --- acciones ----------------------------------------------------------
    status, payload = request(opener, base + "/api/action", {"action": "check"})
    assert status == 200 and payload["ok"], payload
    deadline = time.time() + 10
    while rt.checking and time.time() < deadline:
        time.sleep(0.05)
    assert rt.last_result and rt.last_result.ok
    print(">>> acción 'check' OK")

    status, payload = request(opener, base + "/api/action",
                              {"action": "save", "config": {"interval": 60, "chain_ids": "8453"}})
    assert status == 200 and payload["ok"] and payload["config"]["interval"] == 60
    assert rt.cfg.interval == 60 and rt.cfg.chain_ids == {8453}
    status, payload = request(opener, base + "/api/action",
                              {"action": "save", "config": {"wallets": "0xnoesunawallet"}})
    assert status == 400 and payload["errors"]
    assert rt.cfg.wallets == [WALLET], "no debe aplicar cambios inválidos"
    print(">>> acción 'save' (válida e inválida) OK")

    status, payload = request(opener, base + "/api/action", {"action": "nonsense"})
    assert status == 400
    print(">>> acción desconocida rechazada OK")

    # --- start/stop del bucle ------------------------------------------------
    status, payload = request(opener, base + "/api/action", {"action": "start"})
    assert status == 200 and payload["ok"] and rt.is_running()
    status, payload = request(opener, base + "/api/status")
    assert payload["running"] is True and payload["next_check_at"]
    status, payload = request(opener, base + "/api/action", {"action": "stop"})
    assert status == 200 and not rt.is_running()
    print(">>> start/stop del demonio web OK")
    server.shutdown()
    server.server_close()

    # --- servidor con contraseña --------------------------------------------
    rt2 = make_runtime()
    server2 = ws.WatchdogServer(("127.0.0.1", 0), rt2, "sekreto")
    port2 = server2.server_address[1]
    threading.Thread(target=server2.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    base2 = f"http://127.0.0.1:{port2}"
    jar = http.cookiejar.CookieJar()
    authed = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    anon = urllib.request.build_opener()

    status, payload = request(anon, base2 + "/api/status")
    assert status == 401, "sin sesión no debería dejar ver el estado"
    status, payload = request(anon, base2 + "/api/login", {"password": "mala"})
    assert status == 401
    status, payload = request(authed, base2 + "/api/login", {"password": "sekreto"})
    assert status == 200 and payload["ok"], payload
    print(">>> login: rechaza sin sesión y acepta contraseña OK")

    status, payload = request(anon, base2 + "/api/status")
    assert status == 401, "otro cliente sin cookie no debe heredar la sesión"
    status, payload = request(authed, base2 + "/api/status")
    assert status == 200 and payload["auth_enabled"] is True
    status, payload = request(authed, base2 + "/api/action", {"action": "logout"})
    assert status == 200
    status, payload = request(authed, base2 + "/api/status")
    assert status == 401, "tras logout la sesión debe morir"
    print(">>> cookie de sesión + logout OK")
    server2.shutdown()
    server2.server_close()

    print("\n>>> tests web OK")


if __name__ == "__main__":
    main()
