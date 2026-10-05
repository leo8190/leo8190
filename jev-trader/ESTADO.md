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

- API key y secret de Binance Spot Testnet creados el 04/10/2026. Con la regla nueva del 05/10/2026, Leonardo debe guardarlos en 1Password; `.env` sólo tendrá referencias `op://` verificadas para `JEV_API_KEY` y `JEV_API_SECRET`. No se ejecutó `jev live`. No copiar valores secretos al archivo, al chat ni a Git.
- Una vez disponibles, prueba corta con journal separado y capital acotado de prueba:

```bash
.venv/bin/python -m jev.secure live
```

Comando de la prueba futura: no fue ejecutado. Requiere primero referencias verificadas en `.env` y el bootstrap de la cuenta de servicio desde el Llavero, según la guía canónica de credenciales. Una decisión HOLD puede comprobar conectividad sin enviar órdenes.

- Dinero real: pendiente de confirmación explícita de Leonardo; monto chico, capital máximo, key sin retiros y whitelist de IP.

## Creación de clave testnet — 04/10/2026

- Preparado 2026-10-04T19:46:14.961411-03:00: ingreso oficial completado en Binance Spot Test Network; formulario `https://testnet.binance.vision/key/generate`, nombre `jev-trader-20261004`, permisos TRADE y USER_DATA. USER_STREAM desactivado.
- Estado inicial: preparado; luego Leonardo confirmó expresamente en este chat y se pulsó Generate. **Clave creada**, verificada por el mensaje visible `HMAC-SHA-256 Key registered`. Nombre `jev-trader-20261004`, permisos TRADE y USER_DATA. No se leyeron, copiaron ni guardaron secretos por parte del agente.
- Pestaña de resultado abierta y visible en el Browser integrado de este chat, título `Binance Spot Test Network`; conservada para que Leonardo copie las claves. No cerrar ni navegar hasta que las haya guardado. Comprobante recortado sin secretos: `logs/testnet-key-created.png`.
- El pedido inicial de guardar valores en `.env` quedó sustituido el 05/10/2026: los secretos van en 1Password; el archivo contendrá sólo referencias. No cerrar la pantalla de resultado hasta que Leonardo guarde las claves en 1Password.

Confirmación de creación registrada: 2026-10-04T19:51:14.226135-03:00. Próximo paso actualizado el 05/10: Leonardo guarda la API key y el secret en 1Password; después se configura el acceso de solo lectura y la prueba acotada en testnet. Sin uso de dinero real.

## Credenciales — verificación 05/10/2026

- Verificación inicial: CLI ausente. Actualización del 05/10/2026: `op --version` devuelve **2.40.0**, en `/opt/homebrew/bin/op`. El acceso todavía no está disponible: la entrada prevista en Llavero no fue encontrada al consultar sólo metadatos, sin leer secretos.
- La autorización de CLI es de sólo lectura, limitada a la bóveda dedicada; el agente no crea bóvedas ni escribe/comparte ítems con ese acceso.
- Se detuvieron las acciones dependientes de secretos. No se leyó ni modificó `.env`, ni se extrajeron claves del navegador o del portapapeles.
- Guía canónica: `/Users/leonardoapollonio/Documents/Codex/2026-08-09/nuevo-chat-de-voz-en-tiempo-2/CREDENCIALES_1PASSWORD.md`.

## Configuración local de 1Password — 05/10/2026

- Preparado 2026-10-05T08:48:55.702934-03:00: `jev/secure.py` arranca sólo paper o Binance testnet mediante `op run`; no permite mainnet. Separa la clave TypeSafe de las claves de Binance según el modo, verifica cuenta/bóveda, captura internamente el token desde la entrada específica del Llavero y lo elimina del entorno antes de arrancar el bot.
- Archivos locales preparados: `.env.1password` y `.env.testnet.1password`, únicamente con referencias propuestas al ítem `Jev Trader` de la bóveda `Codex`. Son archivos ignorados por Git y todavía no se verificó que los campos existan. Sus ejemplos quedan versionados. El `.env` anterior no se leyó, modificó ni duplicó.
- **686 tests pasan**, incluidos 20 controles offline del nuevo lanzador: rechaza claves reales en el archivo, accesos distintos, otras bóvedas, cambios de red y credenciales innecesarias; no arranca sin acceso autorizado. Sólo se usaron datos sintéticos para probarlo.
- **Conexión real pendiente**. General ya dejó preparado `Codex lectura` con sólo lectura de `Codex` y espera la confirmación inmediata de su navegador. No se duplicó esa cuenta ni se operó su pestaña. Después deben verificarse el token del Llavero y los tres campos de claves del ítem `Jev Trader`. No se repitió la pregunta de confirmación.
- Guardado de las claves en 1Password, resolución real de referencias y prueba live testnet: no ejecutados. No se generaron claves adicionales ni se usó dinero real.
- Entrega de esta fase mediante commit local en la misma rama. Publicación remota pendiente: no se usaron credenciales Git por otra vía mientras falta el acceso autorizado de 1Password.
