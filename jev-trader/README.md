# Jev Trader

Bot de trading de criptomonedas **spot** que usa **Claude Haiku 4.5** como cerebro de decisión rápida,
un **motor de reglas determinista** como primera etapa y un **gestor de riesgo estricto que siempre
tiene la última palabra**. Por defecto opera en **paper trading** (dinero simulado) y, si se activa el
modo live, en la **testnet** del exchange.

> **Aviso:** Jev Trader es un proyecto educativo y experimental. **No es asesoramiento financiero** ni promete
> ganancias. Operar criptomonedas puede hacerte perder todo el capital. Si lo usás con dinero real, es
> bajo tu exclusiva responsabilidad.

---

## Qué hace

- **Decide en cada vela cerrada** (por defecto BTC/USDT, 5 minutos) si comprar, vender o esperar.
- **Cuatro motores de decisión** intercambiables:
  - `rules`: tendencia con EMA 9/21/50, MACD y RSI. Es determinista, instantáneo y gratis.
  - `claude`: Claude Haiku 4.5 recibe un snapshot compacto del mercado y responde con *structured
    outputs*: acción, confianza, tamaño, stop, take-profit y un razonamiento breve.
  - `hybrid` (el valor por defecto): las reglas filtran y Claude confirma, así que solo se paga el LLM
    cuando aporta algo.
  - `jev` (opcional, acceso anticipado): el modelo *System One* de TypeSafe AI responde preguntas
    tipadas (elección, puntaje, probabilidad) sobre el estado del mercado en una sola pasada. Necesita
    `TYPESAFE_API_KEY`; sin clave devuelve HOLD.
- **Gestor de riesgo** con la última palabra:
  - dimensiona cada orden por riesgo fijo (el % de equity que se pierde si salta el stop);
  - aplica posición máxima, confianza mínima, cooldown, límite diario de operaciones y kill switches.
- **Salidas protectoras** (stop-loss y take-profit) y **kill switches** (pérdida diaria y drawdown) que
  **no dependen del LLM**.
- **Backtesting sin look-ahead**, con informe HTML autocontenido: equity frente a buy & hold, drawdown y
  operaciones.
- **Journal SQLite**: guarda cada decisión, orden ejecutada (fill), operación, punto de equity y
  evento, y persiste el estado para reanudar después de un reinicio.
- **CLI** `jev` con los comandos `backtest`, `decide`, `paper`, `live`, `download` y `status`.

## Arquitectura

```
  +------------------------------+
  | MarketDataSource             |   ccxt (exchange) | CSV | sintético
  +--------------+---------------+
                 | solo velas CERRADAS, de la más vieja a la más nueva
                 v
  +------------------------------+
  | features.build_snapshot      |   EMA, RSI, MACD, ATR, Bollinger, retornos,
  |  -> MarketSnapshot           |   volatilidad, volumen relativo
  +--------------+---------------+
                 v
  +-----------------------------------------------------------------+
  | TradingEngine.step()  (engine.py; backtest.py reusa las piezas) |
  |                                                                 |
  |  1. Salidas protectoras: stop / take-profit     <- sin LLM      |
  |  2. Marca de equity + kill switch (pérdida      <- sin LLM      |
  |     diaria, drawdown) y cierre forzado                          |
  |  3. Motor de decisión: rules | claude | hybrid | jev            |
  |        (si falla o se agota el presupuesto -> HOLD)             |
  |  4. RiskManager: aprueba, achica o rechaza      <- última       |
  |                                                    palabra      |
  |  5. Broker: PaperBroker (simulado) | CcxtBroker (real)          |
  |  6. Portfolio (costo, PnL, límites diarios) + Journal (SQLite)  |
  +-----------------------------------------------------------------+
```

| Módulo | Responsabilidad |
|---|---|
| `jev/models.py`, `jev/config.py` | Contratos compartidos (pydantic) y configuración desde el entorno |
| `jev/market/` | Fuentes de datos: `CcxtMarket`, `CsvMarket`, `SyntheticMarket` (regímenes alcista, bajista y lateral) |
| `jev/indicators.py`, `jev/features.py` | Indicadores puros y construcción del `MarketSnapshot` |
| `jev/brain/` | Motores `rules`, `claude`, `hybrid` y `jev`, y su fábrica |
| `jev/risk.py`, `jev/portfolio.py` | Gestión de riesgo, kill switches, contabilidad de la posición |
| `jev/execution/` | `PaperBroker` (slippage y comisiones) y `CcxtBroker` (órdenes reales, testnet por defecto) |
| `jev/engine.py` | Bucle en tiempo real (paper y live) |
| `jev/backtest.py` | Backtester sin look-ahead que reutiliza el mismo riesgo, portfolio y broker simulado |
| `jev/journal.py`, `jev/metrics.py`, `jev/report.py` | Journal, métricas (Sharpe, Sortino, drawdown…) e informe HTML |
| `jev/cli.py` | Línea de comandos |

## Cómo se toma una decisión

1. Llegan solo **velas cerradas**; la vela que todavía se está formando nunca se usa.
2. Se calculan los indicadores sobre una ventana fija de historia (`JEV_HISTORY_CANDLES`, 200 por
   defecto). El backtest usa la misma ventana, así los valores coinciden con los del modo en vivo.
3. Si hay una posición abierta, primero se revisan **stop-loss y take-profit** en todas las velas nuevas.
   Si alguno se tocó, se cierra la posición y en esa vela no se consulta al motor.
4. Se marca la equity y se evalúa el **kill switch**. Si salta y `JEV_FLATTEN_ON_KILL=true`, se cierra la
   posición. Si el trading queda detenido y no hay posición, no se consulta al motor.
5. El motor propone una acción: `BUY`, `SELL` o `HOLD`.
6. El **RiskManager** decide:
   - **BUY**: tamaño = mínimo entre riesgo por operación / distancia al stop, posición máxima, tamaño
     sugerido por el motor y efectivo disponible. Rechaza la compra si hay un kill switch activo, si ya
     hay posición, si la confianza es baja, si se alcanzó el límite diario, si está en cooldown o si la
     orden queda por debajo del mínimo.
   - **SELL**: reducir riesgo se permite incluso con el trading detenido; solo exige la mitad de la
     confianza mínima.
7. El broker ejecuta la orden a mercado. El portfolio registra el fill y el journal guarda todo.

## Por qué híbrido: latencia y costo

- Las reglas evalúan cada vela en microsegundos y sin costo.
- Una llamada a Claude suma la latencia de red y del modelo: `jev decide` la mide, y el timeout por
  defecto es de 8 s.
- En modo `hybrid`, Claude se consulta solo en tres casos:
  - **para confirmar una entrada** cuando las reglas ven un setup. Sin confirmación no se compra, y el
    stop que se usa es el más ajustado de los dos;
  - **en un heartbeat** cada `JEV_HYBRID_HEARTBEAT_CANDLES` velas (12 por defecto), para detectar una
    salida anticipada o una entrada que el filtro de tendencia permita;
  - **nunca para salir**: si las reglas dicen SELL, se vende sin esperar al LLM. Stops, take-profits y
    kill switches no pasan por el LLM.
- Si Claude falla (sin clave, timeout, error de la API, respuesta inválida o presupuesto diario agotado),
  la decisión es **HOLD**. Un fallo del LLM nunca abre una posición ni bloquea una salida: los reintentos
  esperan como máximo 1 s (nunca el `Retry-After` completo del servidor) y solo mientras quepan en el
  timeout.

### Costo estimado por decisión (Claude Haiku 4.5)

Precios de Haiku 4.5: **US$1 por millón de tokens de entrada** y **US$5 por millón de salida**.
Una decisión usa aproximadamente **~700 tokens de entrada** (instrucciones de sistema, snapshot y esquema)
y **~150 de salida**. Es una estimación, no una medición contra la API:

```
entrada: 700 × 1 / 1.000.000 = US$0,00070
salida : 150 × 5 / 1.000.000 = US$0,00075
total  ≈ US$0,0015 por decisión
```

Con velas de 5 minutos hay 288 velas por día:

| Modo | Llamadas/día | Costo/día aprox. |
|---|---|---|
| `rules` | 0 | US$0 |
| `hybrid` | mínimo 24 (heartbeats) más 1 por cada vela con setup de las reglas | ~US$0,035 como mínimo; peor caso US$0,43 |
| `claude` | 288 (una por vela) | ~US$0,43 (~US$13/mes) |

Referencia para `hybrid`: en el backtest sintético de 3000 velas descripto más abajo, las reglas
propusieron BUY en ~13 % de las decisiones. Eso daría del orden de 60 llamadas por día (~US$0,09 por día). Es
una referencia sintética, no una predicción.

`JEV_MAX_LLM_COST_USD_PER_DAY` (US$1 por defecto) corta las llamadas al alcanzar el tope diario, y Jev Trader
sigue con HOLD. En backtests con LLM, `jev backtest` estima el costo antes de empezar y pide `--yes` si el
peor caso supera US$1. `--max-llm-calls` (200 por defecto) pasa al motor de reglas cuando se alcanza el
tope.

## Inicio rápido

Requisitos: Python 3.10 o superior.

```bash
cd jev-trader
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # completá lo que necesites; .env nunca se sube al repo

# 1) Backtest offline con datos sintéticos y el motor de reglas (sin claves, sin red)
jev backtest --source synthetic --engine rules
#    -> imprime el resumen y escribe reports/backtest.html

# 2) Una decisión rápida de demostración (no envía órdenes)
jev decide --synthetic                    # usa el motor de JEV_ENGINE
jev decide --synthetic --engine rules

# 3) Paper trading
jev paper --synthetic --fast --max-iterations 200 --engine rules   # simulación offline acelerada
jev paper                                                          # precios reales, dinero simulado

# 4) Estado y journal
jev status
```

Para usar Claude, poné `ANTHROPIC_API_KEY` en `.env` o en el entorno. Sin clave, Jev Trader funciona igual: cada
consulta al LLM devuelve HOLD y la CLI lo avisa.

### Testnet de Binance (modo live sin dinero real)

1. Creá claves en la *Spot Test Network* de Binance (`testnet.binance.vision`).
2. En `.env`, configurá `JEV_MODE=live`, `JEV_USE_TESTNET=true`, `JEV_API_KEY=...` y `JEV_API_SECRET=...`.
3. Ejecutá `jev live`. Se muestra un banner con la red (TESTNET) y el bot opera con fondos de prueba.

## Cargar dinero (modo live)

El bot **no deposita ni retira nada**: opera con el saldo que haya en tu cuenta del exchange, y las claves
de API no deben tener permiso de retiro.

1. **Probá primero sin dinero real**: `jev paper` (simulado) y después la testnet (arriba). La testnet usa
   fondos de prueba y claves propias de `testnet.binance.vision`; las claves de tu cuenta real no sirven ahí.
2. **Depositá en el exchange** (por ejemplo Binance) con los medios que ofrezca en tu país y convertí a la
   moneda *quote* del par (USDT para `BTC/USDT`).
3. **Poné solo lo que querés arriesgar al alcance del bot.** En live el bot usa **todo el USDT libre de la
   cuenta** para calcular el tamaño de cada orden (1 % de riesgo, posición máxima 25 %…). Dos formas de
   limitarlo, combinables:
   - una **subcuenta dedicada** con solo ese monto (lo más seguro);
   - `JEV_LIVE_MAX_CAPITAL=200`: el bot opera como máximo con 200 USDT más su PnL realizado, aunque la
     cuenta tenga más.
4. Configurá `.env` para mainnet (`JEV_MODE=live`, `JEV_USE_TESTNET=false`,
   `JEV_LIVE_CONFIRM=YES_I_ACCEPT_REAL_MONEY_RISK`, claves) y ejecutá `jev live`: pide escribir el símbolo
   para confirmar.

Depósitos, retiros y compras o ventas manuales con el bot andando se detectan como **movimientos externos**
(evento `external_flow` en el journal): no cuentan como ganancia ni pérdida, así que no disparan el kill
switch ni esconden una pérdida real. Las monedas que el bot no compró (por ejemplo, el saldo inicial de la
testnet) no las gestiona ni les pone stop, y `jev live` lo avisa al arrancar.

## Modelo de seguridad

- **Paper por defecto** (`JEV_MODE=paper`). `paper`, `decide`, `download` y `backtest` nunca envían
  órdenes y solo leen datos públicos, sin claves.
- **Testnet por defecto** (`JEV_USE_TESTNET=true`) cuando se activa el modo live.
- **Dinero real requiere tres opt-ins explícitos**: `JEV_MODE=live`, `JEV_USE_TESTNET=false` y
  `JEV_LIVE_CONFIRM=YES_I_ACCEPT_REAL_MONEY_RISK`. Además, `jev live` pide escribir el símbolo exacto para
  confirmar; solo se puede omitir con `--yes` **y** `JEV_LIVE_CONFIRM` configurado. Recién después de esas
  comprobaciones se crea el broker con `allow_mainnet=True`.
- **Claves de API**:
  - **sin permiso de retiro**;
  - con **whitelist de IP**;
  - solo con permiso de trading spot.
  - Jev Trader nunca imprime ni guarda las claves: el journal redacta los campos sensibles y los logs
    (incluso con `-vv`) reemplazan las claves por `***`.
- **Capital en live**: todo el saldo libre de la moneda quote, o como máximo `JEV_LIVE_MAX_CAPITAL` (ver
  [Cargar dinero](#cargar-dinero-modo-live)).
- **Kill switches** (miden solo el PnL de trading: depósitos y retiros no cuentan):
  - pérdida diaria (`JEV_MAX_DAILY_LOSS_PCT`), medida desde la última equity del día UTC anterior (también
    funciona con velas de 1d): bloquea nuevas entradas hasta el próximo día UTC;
  - drawdown desde el pico (`JEV_MAX_DRAWDOWN_PCT`): bloquea hasta un reset manual con
    `jev status --reset-halt`.
  - Con `JEV_FLATTEN_ON_KILL=true` también se cierra la posición. El estado de halt se persiste, así que
    reiniciar no lo borra.
- **Cooldown** tras cada salida (`JEV_COOLDOWN_CANDLES`), **límite diario de operaciones** y **posición
  máxima** (`JEV_MAX_POSITION_PCT`).
- **El LLM solo puede achicar**: su `size_pct` es un tope adicional. Nunca supera el tamaño que calcula el
  riesgo, y los stops se recortan a `[JEV_MIN_STOP_PCT, JEV_MAX_STOP_PCT]`.
- **Salidas protectoras sin LLM**: stop-loss y take-profit se evalúan antes de consultar a cualquier motor.
- **Fallo del LLM = HOLD**, con presupuesto diario en USD.
- **Estado incierto de una orden**: si el exchange no confirma si una orden se ejecutó, o el proceso se cortó
  mientras la enviaba, Jev Trader bloquea nuevas entradas y lo registra para que una persona lo revise
  (`jev status --reset-halt` lo libera).
- **Datos viejos**: si la última vela cerrada tiene más de un timeframe (+2 min) de antigüedad, no se abren
  posiciones; stops y kill switch se siguen revisando. Stop y take-profit se calculan desde el precio real de
  la compra, no desde el cierre de la vela.
- **Una posición por par**: `jev live` no arranca si otra sesión del mismo exchange y par (con otro
  `JEV_TIMEFRAME`) tiene una posición abierta, para que no quede sin stop.
- **Spot, solo largo y sin apalancamiento**: SELL solo reduce una posición existente.

## Configuración

Todo se configura con variables de entorno o con `.env`; las variables del entorno tienen prioridad. Los
flags de la CLI (`--engine`, `--symbol`, `--timeframe`, `--journal`) pisan esos valores. Los booleanos
aceptan `true`/`false` (también `1`/`0`, `yes`/`no`, `on`/`off`, `si`); cualquier otro valor es un error
de configuración, nunca un `false` silencioso.

| Variable | Por defecto | Descripción |
|---|---|---|
| `ANTHROPIC_API_KEY` | (vacía) | Clave de la API de Anthropic. Sin clave, el LLM responde HOLD |
| `JEV_MODEL` | `claude-haiku-4-5` | Modelo de Claude |
| `JEV_ENGINE` | `hybrid` | `hybrid`, `claude`, `rules` o `jev` |
| `TYPESAFE_API_KEY` | (vacía) | Clave de TypeSafe AI para el motor `jev`. Sin clave, Jev responde HOLD |
| `JEV_LLM_TIMEOUT_S` | `8` | Timeout por llamada al LLM, en segundos |
| `JEV_MAX_LLM_COST_USD_PER_DAY` | `1.0` | Presupuesto diario del LLM (0 lo desactiva) |
| `JEV_EXCHANGE` | `binance` | Id del exchange en ccxt |
| `JEV_SYMBOL` | `BTC/USDT` | Par spot `BASE/QUOTE` |
| `JEV_TIMEFRAME` | `5m` | Timeframe de las velas (`1m`, `5m`, `1h`, `1d`…) |
| `JEV_MODE` | `paper` | `paper` o `live` |
| `JEV_USE_TESTNET` | `true` | En modo live: testnet (`true`) o mainnet (`false`) |
| `JEV_LIVE_CONFIRM` | (vacía) | Para mainnet: `YES_I_ACCEPT_REAL_MONEY_RISK` |
| `JEV_API_KEY` / `JEV_API_SECRET` | (vacías) | Claves del exchange (solo modo live) |
| `JEV_PAPER_START_CASH` | `1000` | Capital inicial simulado, en moneda quote |
| `JEV_LIVE_MAX_CAPITAL` | `0` | Live: capital máximo que usa el bot (más su PnL realizado); `0` = todo el saldo libre |
| `JEV_FEE_PCT` | `0.1` | Comisión por lado, en % |
| `JEV_SLIPPAGE_PCT` | `0.05` | Slippage simulado por lado, en % |
| `JEV_RISK_PER_TRADE_PCT` | `1.0` | % de equity que se pierde si salta el stop |
| `JEV_MAX_POSITION_PCT` | `25` | Posición máxima, en % de la equity |
| `JEV_MAX_DAILY_LOSS_PCT` | `3` | Kill switch por pérdida desde el inicio del día UTC |
| `JEV_MAX_DRAWDOWN_PCT` | `10` | Kill switch por caída desde el pico de equity |
| `JEV_MAX_TRADES_PER_DAY` | `10` | Fills por día UTC (compras y ventas) a partir de los cuales no se abren posiciones nuevas |
| `JEV_MIN_CONFIDENCE` | `0.6` | Confianza mínima para comprar (para vender, la mitad) |
| `JEV_COOLDOWN_CANDLES` | `3` | Velas cerradas de espera después de cerrar una posición (igual en backtest y en vivo) |
| `JEV_DEFAULT_STOP_PCT` | `2` | Stop si el motor no propone uno |
| `JEV_DEFAULT_TAKE_PROFIT_PCT` | `4` | Take-profit si el motor no propone uno |
| `JEV_MIN_ORDER_NOTIONAL` | `10` | Tamaño mínimo de orden, en moneda quote |
| `JEV_FLATTEN_ON_KILL` | `true` | Cerrar la posición cuando salta un kill switch |

Variables avanzadas, opcionales:

| Variable | Por defecto | Descripción |
|---|---|---|
| `JEV_LLM_MAX_RETRIES` | `1` | Reintentos rápidos del LLM (espera ≤1 s, dentro del timeout) |
| `JEV_LLM_MAX_TOKENS` | `400` | Tope de tokens de salida por decisión |
| `JEV_HYBRID_HEARTBEAT_CANDLES` | `12` | Cada cuántas velas el modo híbrido consulta a Claude sin setup |
| `JEV_HISTORY_CANDLES` | `200` | Velas de historia por decisión (mínimo 60) |
| `JEV_JOURNAL_PATH` | `jev_journal.sqlite3` | Archivo SQLite del journal |
| `JEV_MIN_STOP_PCT` / `JEV_MAX_STOP_PCT` | `0.3` / `10` | Límites del stop aceptado por el riesgo |

## Comandos

| Comando | Qué hace |
|---|---|
| `jev backtest [--source synthetic\|csv\|exchange] [--csv RUTA] [--candles N] [--seed S] [--engine E] [--max-llm-calls N] [--warmup N] [--report RUTA] [--journal RUTA] [--yes]` | Backtest sin look-ahead. Imprime el resumen y escribe el informe HTML (`reports/backtest.html` por defecto) |
| `jev decide [--synthetic] [--engine E]` | Una decisión de demostración: snapshot, decisión, latencia y costo. **No envía órdenes** |
| `jev paper [--synthetic] [--fast] [--max-iterations N] [--journal RUTA] [--fresh]` | Paper trading en tiempo real: espera cada cierre de vela más 2 s. `--synthetic --fast` simula sin esperas |
| `jev live [--max-iterations N] [--yes]` | Órdenes reales por ccxt (testnet por defecto). Requiere `JEV_MODE=live` |
| `jev download --since YYYY-MM-DD [--until YYYY-MM-DD] --out RUTA.csv` | Descarga velas históricas públicas a CSV |
| `jev status [--journal RUTA] [--limit N] [--reset-halt]` | Resumen del journal, estado guardado y últimas decisiones y eventos |

Todos aceptan `-v` (INFO) o `-vv` (DEBUG), y `--env-file`. También funciona `python -m jev …`. Códigos de
salida: `0` si todo salió bien, `1` si hubo un error en ejecución y `2` si hubo un error de configuración
o de uso.

Cómo funcionan `paper` y `live`:

- Guardan el estado en el journal después de cada vela y lo reanudan al reiniciar: posición, stops, saldo
  simulado, halts y última vela procesada. `--fresh` ignora el estado guardado en `paper`.
- **Ctrl+C** (o SIGTERM) detiene el bot al terminar el paso en curso (una orden en vuelo se registra
  completa) y guarda el estado; un segundo Ctrl+C fuerza la salida. Si el proceso se corta mientras envía
  una orden, al reiniciar se bloquean las entradas hasta revisar el exchange.
- `jev status` suma todas las sesiones del journal (sintético, paper y live); el PnL de cada sesión aparece
  en "Estado guardado". Usá `--journal` distintos para separarlas.

## Backtesting sin look-ahead

- La decisión de la vela *i* ve solo `velas[: i + 1]`, con una ventana acotada, igual que en vivo.
- La orden se ejecuta en la **apertura de la vela *i + 1*** a través del `PaperBroker`, con slippage y
  comisiones.
- Stops y take-profits:
  - se calculan desde el precio real de entrada (apertura con slippage), igual que en vivo;
  - se revisan en cada vela desde la entrada, con su mínimo y su máximo;
  - se ejecutan en el nivel, o en la apertura si la vela abrió más allá del nivel (por debajo del stop o
    por encima del take-profit).
- La equity se marca en cada cierre. Kill switch, cooldown y límites diarios usan los timestamps de las
  velas.
- El backtest usa los mismos `RiskManager`, `Portfolio` y `PaperBroker` que el modo en vivo.
- El informe advierte cuando corresponde:
  - datos sintéticos;
  - **contaminación** si se usa un LLM sobre datos históricos, porque el modelo pudo haber visto ese
    período;
  - llamadas al LLM que fallaron;
  - posición abierta al final;
  - muestra demasiado chica.

### Resultado de ejemplo (datos **sintéticos**)

```
jev backtest --source synthetic --engine rules --candles 3000 --report reports/demo.html
```

Corre con la semilla 42, 5 minutos por vela, la configuración por defecto y el motor `rules`. **Los datos
son sintéticos, no de mercado:**

| Métrica | Valor |
|---|---|
| Retorno total | −3,97 % |
| Buy & hold (mismo período) | −10,41 % |
| Máximo drawdown | 4,20 % |
| Operaciones cerradas | 51 (21,6 % ganadoras, profit factor 0,30) |
| Comisiones pagadas | 25,15 USDT sobre 1.000 iniciales |
| Exposición | 16,7 % del tiempo |

La estrategia de reglas **perdió dinero** con esta serie. Perdió menos que buy & hold sobre todo porque
estuvo invertida solo el 16,7 % del tiempo en un mercado que cayó, no porque tenga ventaja. Los datos sintéticos son un paseo aleatorio con regímenes
y **no tienen un edge explotable**: sirven para probar el sistema de punta a punta, no para evaluar la
estrategia. Las comisiones y el slippage (~0,3 % por operación completa) se comen cualquier ventaja
pequeña.

## Tests

```bash
.venv/bin/pytest -q
```

La suite (~640 tests) corre **offline** en pocos segundos. Usa fakes para el exchange, el broker y los
clientes de Anthropic y TypeSafe, y no hace ninguna llamada de red.

## Limitaciones (honestas)

- **No hay garantía de ganancias.** La estrategia de reglas incluida es una base simple y perdió dinero en
  el backtest sintético.
- **Comisiones y slippage** (~0,1 % + 0,05 % por lado por defecto) hacen inviables los movimientos chicos.
- **Los backtests de LLMs sobre datos pasados están contaminados**: el modelo pudo haber visto ese
  período durante el entrenamiento, así que el resultado sobreestima el rendimiento real. La evaluación
  honesta es paper trading hacia adelante.
- **Los datos sintéticos no son el mercado real.**
- **El modelo de ejecución es simple**:
  - slippage fijo en %;
  - órdenes completas;
  - sin libro de órdenes ni latencia.
- **Salidas protectoras en tiempo real**:
  - se evalúan al cierre de cada vela y se ejecutan a mercado; no hay órdenes stop en el exchange, así que
    el precio puede pasar el stop entre cierres;
  - el backtest, en cambio, ejecuta en el nivel del stop o en la apertura si hubo gap, y por eso es algo
    más optimista.
- **Un solo símbolo, spot, solo largo**, sin pyramiding.
- `paper`, `decide` y `download` leen datos públicos del exchange real. `live` usa el mismo exchange
  (testnet o mainnet) para datos y órdenes.
- El presupuesto diario del LLM se lleva en memoria y se reinicia al reiniciar el proceso.
- Los tests no ejercitan la API real de Anthropic ni la de los exchanges (todo es offline). Probá en
  testnet antes de cualquier otra cosa.

## Roadmap

- Órdenes stop/OCO en el exchange para el modo live, para que la protección no dependa del cierre de vela.
- Evaluación *walk-forward* y validación de parámetros fuera de muestra.
- Paper trading prolongado con el LLM sobre datos posteriores a su fecha de corte, para medir sin
  contaminación.
- Varios símbolos y un límite de riesgo a nivel cartera.
- Alertas (email o Telegram) cuando salta un kill switch o una orden queda en estado incierto.
- Panel web de solo lectura sobre el journal.
- Modelo de slippage basado en la profundidad del libro.

## Licencia y aviso legal

MIT. Este software se entrega "tal cual", sin garantías. Nada de lo que hay en este repositorio es
asesoramiento financiero, y los resultados pasados o simulados no garantizan resultados futuros.
