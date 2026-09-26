# vfat-position-watchdog

Demonio de terminal (Linux, Python 3.8+, **solo librería estándar**) que vigila
indefinidamente las **posiciones LP abiertas de una wallet** mediante el MCP
público de [vfat](https://vfat.io/mcp#tools) y envía un aviso por **Telegram**
en cada ejecución en la que alguna posición de liquidez concentrada esté
**fuera de rango**.

```
┌────────────┐   cada CHECK_INTERVAL_SECONDS    ┌──────────────┐
│ vfat_watchdog.py ─────────────────────────────▶ │ MCP vfat.io  │
│ (demonio)  │   tools/call:                     │  (HTTP)      │
│            │   get_wallet_portfolio             └──────────────┘
│            │        │  tickLow/tickUp vs tick actual del pool
│            │        ▼
│            │   ¿alguna posición fuera de rango? ──▶ 🚨 Telegram
└────────────┘
```

## Cómo decide si una posición está fuera de rango

El tool `get_wallet_portfolio` del MCP devuelve, para cada posición NFT de
liquidez concentrada (Uniswap V3, Aerodrome Slipstream, etc.), el rango de la
posición (`nft.tickLow` / `nft.tickUp`) y el tick actual del pool (`tick`). La
posición está **en rango** si:

```
tickLow ≤ tick ≤ tickUp
```

Las posiciones sin rango (pares clásicos sin liquidez concentrada) se
registran en el log como *no vigilables* pero no generan avisos. El precio
humano se recalcula como `1.0001^tick · 10^(dec0−dec1)` para mostrar el
porcentaje de distancia al límite del rango.

## Instalación

```bash
# 1) Python 3.8+ (no hay dependencias externas)
python3 --version

# 2) Configuración
cp .env.example .env
nano .env          # wallet, bot de Telegram, etc.

# 3) Dar permisos de ejecución
chmod +x vfat_watchdog.py
```

### Crear el bot de Telegram

1. Habla con [@BotFather](https://t.me/BotFather) → `/newbot` → copia el token.
2. Escribe un mensaje a tu bot (o al canal/grupo donde lo añadas).
3. Obtén el `chat_id` abriendo `https://api.telegram.org/bot<TOKEN>/getUpdates`
   y copiando `result[].message.chat.id`.

## Uso

```bash
./vfat_watchdog.py                    # demonio: bucle infinito cada 3 min
./vfat_watchdog.py --env /ruta/.env   # fichero de configuración alternativo
./vfat_watchdog.py --interval 60      # pisa CHECK_INTERVAL_SECONDS
./vfat_watchdog.py --once             # una comprobación y sale (ideal para cron)
./vfat_watchdog.py --once --dry-run   # igual pero sin enviar Telegram (pruebas)
./vfat_watchdog.py --verbose          # logs de depuración
```

Parar el demonio: `Ctrl+C` o `SIGTERM` (termina limpiamente tras el ciclo en
curso). En segundo plano: `nohup ./vfat_watchdog.py >/dev/null 2>&1 &`.

### Primeras pruebas recomendadas

```bash
./vfat_watchdog.py --once --dry-run   # ¿lee bien las posiciones?
./vfat_watchdog.py --once             # ¿llega el mensaje de arranque a Telegram?
```

## Configuración (`.env`)

| Variable | Por defecto | Descripción |
|---|---|---|
| `WALLET_ADDRESS` | — | **Obligatoria.** Wallet EVM a vigilar (admite lista separada por comas, máx. 5). |
| `TELEGRAM_BOT_TOKEN` | — | Obligatorio salvo `--dry-run`. Token de @BotFather. |
| `TELEGRAM_CHAT_ID` | — | Obligatorio salvo `--dry-run`. Chat/canal destino. |
| `CHECK_INTERVAL_SECONDS` | `180` | Segundos entre comprobaciones (3 min). |
| `CHAIN_IDS` | *(todas)* | Filtro opcional de chains, p. ej. `8453,42161`. |
| `ALERT_ONLY_CHANGES` | `false` | `true` = avisa solo al *cambiar* a fuera de rango en vez de en cada ejecución. |
| `ALERT_ON_RECOVERY` | `true` | Avisa también cuando la posición vuelve a entrar en rango. |
| `NOTIFY_ON_START` | `true` | Mensaje de bienvenida al arrancar. |
| `NOTIFY_ON_ERROR` | `true` | Avisa si una comprobación falla y cuando se recupera. |
| `HEARTBEAT_HOURS` | `0` | Resumen periódico "todo en orden" (0 = desactivado). |
| `LOG_FILE` | *(stdout)* | Fichero adicional de log. |
| `MCP_URL` | `https://mcp.vfat.io/mcp` | Endpoint del MCP (no requiere API key). |

## Modo web: interfaz estilo terminal

Además del demonio CLI, `web_server.py` sirve una **interfaz web con estética
de terminal CRT** (verde fósforo, scanlines, consola con comandos) y una API
JSON. Usa el mismo núcleo que el CLI —mismos avisos de Telegram, misma
matemática de rangos— y **solo librería estándar** (sin Flask/Django/Node).

```
navegador ──▶ web_server.py ──▶ vfat_watchdog.run_once() ──▶ MCP vfat
   ▲                │                     │
   └── /api/status  │  hilo de vigilancia └──▶ Telegram (avisos)
       /api/logs    │
       /api/action ─┘  start/stop/check/set …
```

### Arranque rápido

```bash
python web_server.py                 # → http://127.0.0.1:8000
python web_server.py --port 8080     # otro puerto
python web_server.py --password xyz  # exige contraseña en la web
python web_server.py --no-autostart  # sirve la web sin vigilar aún
```

Variables web en el `.env` (todas opcionales): `WEB_HOST` (por defecto
`127.0.0.1`), `WEB_PORT` (`8000`), `WEB_PASSWORD` (vacía = sin contraseña),
`WEB_AUTOSTART` (`true`, arranca la vigilancia al levantar el servidor).

### Comandos de la consola web

| Comando | Acción |
|---|---|
| `help` | lista de comandos |
| `status` | demonio, intervalo, wallets, última comprobación |
| `pos` | tabla de posiciones (en rango / fuera, precio, distancia, valor) |
| `check` | fuerza una comprobación inmediata |
| `start` / `stop` | arranca / para el bucle de vigilancia |
| `config` | muestra la configuración actual (token enmascarado) |
| `set <clave> <valor>` | cambia ajustes en caliente (`wallets`, `interval`, `chain_ids`, `alert_only_changes`, `alert_on_recovery`, `heartbeat_hours`, `telegram_token`, `telegram_chat_id`; `-` para vaciar) |
| `test-telegram` | envía un mensaje de prueba |
| `theme [perfil]` | perfil de color de la consola: `verde`, `ambar`, `cian`, `magenta`, `blanco` (sin argumento, los lista; también seleccionable con los cuadros de color de la cabecera; se recuerda en el navegador) |
| `clear` / `logout` | limpia la consola / cierra sesión |

### Alojarlo detrás de un servidor web

El Python escucha en su propio puerto; pon un proxy inverso delante si
quieres dominio y HTTPS. **No sirvas este directorio como estático**: el `.env`
lleva el token del bot (hay un `.htaccess` de defensa, pero mejor no exponerlo).

**Apache** (p. ej. Laragon/`httpd-vhosts.conf`, requiere `mod_proxy`):

```apache
<VirtualHost *:80>
    ServerName watchdog.test
    ProxyPreserveHost On
    ProxyPass / http://127.0.0.1:8000/
    ProxyPassReverse / http://127.0.0.1:8000/
</VirtualHost>
```

**nginx**:

```nginx
server {
    listen 443 ssl;
    server_name watchdog.midominio.com;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
    }
}
```

Y en el `.env` deja `WEB_HOST=127.0.0.1` (solo el proxy lo ve) y define
`WEB_PASSWORD`. En Linux, `vfat-watchdog-web.service` es la unidad systemd
equivalente a la del CLI.

### Seguridad

- `WEB_PASSWORD` activa login con cookie de sesión (HttpOnly, SameSite=Strict,
  7 días) y limita intentos fallidos por IP. Sin contraseña, el servidor solo
  debe escuchar en `127.0.0.1`.
- Para exponerlo a Internet: proxy inverso con TLS + `WEB_PASSWORD`.
- El token de Telegram nunca viaja al navegador (la API lo enmascara).

## Ejecutar como servicio (systemd)

```bash
sudo cp vfat-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vfat-watchdog
journalctl -u vfat-watchdog -f          # ver logs
```

## Ejecutar con cron (alternativa a systemd)

```cron
*/3 * * * * cd /home/ubuntu/CryptoRobotFlash && ./vfat_watchdog.py --once >> watchdog.cron.log 2>&1
```

## Pruebas

`tests/test_watchdog.py` verifica con datos sintéticos el cálculo de rango, el
precio humano (`1.0001^tick · 10^(dec0−dec1)`), el filtrado por wallet/chain,
los avisos de salida y de retorno a rango, y el modo `ALERT_ONLY_CHANGES`.
`tests/test_web.py` prueba la API del modo web (estado, logs, acciones,
configuración en caliente y login) con un MCP falso:

```bash
python3 tests/test_watchdog.py
python3 tests/test_web.py
```

## Notas

- **Rate limit**: el MCP de vfat limita las peticiones; el demonio hace 1
  llamada cada 3 minutos (hasta 5 wallets por llamada), muy por debajo del
  límite. Ante errores HTTP 429/5xx reintenta con backoff.
- **Errores transitorios**: si una comprobación falla (red, MCP caído) el
  demonio no muere: lo registra, avisa por Telegram y reintenta en el siguiente
  ciclo. Con `--once` el proceso termina con código 1 para que cron lo detecte.
- **Sin claves privadas**: solo se lee la wallet (dirección pública). El MCP de
  vfat devuelve datos y calldata sin firmar; el demonio nunca firma ni envía
  transacciones.
- El estado "en rango / fuera de rango" entre reinicios se mantiene en memoria;
  con `ALERT_ONLY_CHANGES=true`, tras reiniciar el demonio se avisa otra vez de
  las posiciones que sigan fuera de rango.
