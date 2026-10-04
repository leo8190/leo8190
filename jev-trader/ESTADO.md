# Jev Trader · Estado

Verificado: 2026-10-04T15:04:24.002041-03:00 (Argentina). Rama: `claude/jeb-crypto-trading-app-vp8iqt`.

## Confirmado

- Las dos simulaciones estaban detenidas; se reanudaron el 04/10/2026 conservando el estado y los journals. Procesos independientes con salida inmediata al log.
- Datos públicos de Binance y Jev real: respuesta HTTP 200 y nueva decisión HOLD en ambas sesiones.
- Rama actualizada con el nuevo `jev doctor` de solo lectura. Su prueba de CLI quedó aislada de la `.env` real; suite completa verificada: **666 tests pasan**.
- Los archivos temporales SQLite `-wal` y `-shm` también quedan ignorados por Git.
- Solo dinero simulado. `JEV_MODE=paper` y `JEV_USE_TESTNET=true` forzados al iniciar. Sin posiciones abiertas ni kill switch.

| Sesión | Saldo simulado USDT | Operaciones cerradas | PnL realizado USDT | Decisiones | Costo IA del journal USD |
|---|---:|---:|---:|---:|---:|
| 5m | 1000.00 | 0 | 0.00 | 506 | 0.02650292 |
| 1h | 997.81 | 1 | -2.19 | 42 | 0.00207333 |

Los costos son los registrados por el bot con su tarifa configurada; no una conciliación de la facturación del proveedor. Las cifras de rendimiento cubren las decisiones procesadas, con una pausa entre el 03/10 y la reanudación del 04/10. No constituyen una medición continua.

## Revisar y detener

Desde `jev-trader/`:

```bash
.venv/bin/jev status
.venv/bin/jev status --journal jev_paper_1h.sqlite3
```

Logs y PIDs: `logs/paper_5m.log`, `logs/paper_1h.log`, `logs/paper_5m.pid`, `logs/paper_1h.pid` (ignorados por Git). No hay un servicio de reinicio automático instalado.

## Próximo paso

- Faltan `JEV_API_KEY` y `JEV_API_SECRET` de Binance Spot Testnet. No se ejecutó `jev live`. La guía de creación de claves ya fue entregada en este chat. Guardarlas en `.env`, nunca en el chat ni en Git.
- Una vez disponibles, prueba corta con journal separado y capital acotado de prueba:

```bash
JEV_MODE=live JEV_USE_TESTNET=true JEV_LIVE_CONFIRM= JEV_LIVE_MAX_CAPITAL=100 JEV_TIMEFRAME=1h .venv/bin/jev live --max-iterations 1 --journal jev_testnet.sqlite3
```

Este comando está preparado; no fue ejecutado. Una decisión HOLD puede comprobar conectividad sin enviar órdenes.

- Dinero real: pendiente de confirmación explícita de Leonardo; monto chico, capital máximo, key sin retiros y whitelist de IP.
