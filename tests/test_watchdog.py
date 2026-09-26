import importlib.util, os, sys, logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("vw", os.path.join(ROOT, "vfat_watchdog.py"))
vw = importlib.util.module_from_spec(spec)
sys.modules["vw"] = vw  # necesario para dataclasses en Python 3.12+
spec.loader.exec_module(vw)

WALLET = "0xabc0000000000000000000000000000000000001"

def pos(nft_id, tick_low, tick_up, tick, wallet=WALLET, chain=8453):
    # WETH(18)/cbBTC(8): precio humano = 1.0001^tick * 10^(18-8)
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

payload = {"data": {"farms": [
    pos("111", -264800, -264700, -270000),          # fuera por abajo
    pos("222", 25200, 27200, 31500),                # fuera por arriba
    pos("333", -264800, -264700, -264750),          # en rango
    pos("444", 0, 100, 50, wallet="0xOTRA"),        # otra wallet -> se filtra
], "liquidity": [
    {"chainId": 8453, "wallet": WALLET, "symbol": "USDC/USDT LP", "underlying": [
        {"symbol": "USDC", "decimals": 6, "balance": "10", "price": 1.0},
        {"symbol": "USDT", "decimals": 6, "balance": "10", "price": 1.0}]},  # sin rango
]}}

positions, uncheckable = vw.parse_portfolio(payload, {WALLET}, None)
print(f"\n>>> parseadas={len(positions)} (esperadas 3) · sin-rango={len(uncheckable)} (esperada 1)\n")

cfg = vw.Config(wallets=[WALLET], telegram_token=None, telegram_chat_id=None)


class FakeMcp:
    def __init__(self, payload): self.payload = payload
    def call_tool(self, name, arguments): return self.payload


mcp, state = FakeMcp(payload), vw.State()

print("=== Ejecución 1 (debe avisar de 2 fuera de rango) ===")
vw.run_once(cfg, mcp, None, state, dry_run=True)

print("\n=== Ejecución 2 sin cambios (ALERT_ONLY_CHANGES=false -> reavisa) ===")
vw.run_once(cfg, mcp, None, state, dry_run=True)

cfg2 = vw.Config(wallets=[WALLET], alert_only_changes=True)
print("\n=== Ejecución 3 sin cambios con ALERT_ONLY_CHANGES=true (no debe avisar) ===")
vw.run_once(cfg2, mcp, None, state, dry_run=True)

# La posición 222 vuelve a rango
payload["data"]["farms"][1]["tick"] = 26000
positions, _ = vw.parse_portfolio(payload, {WALLET}, None)
print("\n=== Ejecución 4: posición 222 vuelve a rango (aviso de recuperación) ===")
vw.run_once(cfg2, mcp, None, state, dry_run=True)

# Chequeo de la matemática de precio humano: tick=-270000 con dec0=18, dec1=8
p = positions[0]
import math
esperado = 1.0001 ** -270000 * 1e10
print(f"\n>>> precio humano actual={p.price_now():.8f} · esperado={esperado:.8f} · ok={math.isclose(p.price_now(), esperado)}")
