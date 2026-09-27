# JEB Trader

Bot de trading de criptomonedas **spot** que usa **Claude Haiku 4.5** como cerebro de decisión rápida,
un **motor de reglas determinista** como primera etapa y un **gestor de riesgo estricto que siempre
tiene la última palabra**. Por defecto opera en **paper trading** (dinero simulado) y, si se activa el
modo live, en la **testnet** del exchange.

> **Aviso:** JEB es un proyecto educativo y experimental. **No es asesoramiento financiero** ni promete
> ganancias. Operar criptomonedas puede hacerte perder todo el capital. Si lo usás con dinero real, es
> bajo tu exclusiva responsabilidad.

---

## Qué hace

- **Decide en cada vela cerrada** (por defecto BTC/USDT, 5 minutos) si comprar, vender o esperar.
- **Tres motores de decisión** intercambiables:
  - `rules`: tendencia con EMA 9/21/50, MACD y RSI. Es determinista, instantáneo y gratis.
  - `claude`: Claude Haiku 4.5 recibe un snapshot compacto del mercado y responde con *structured
    outputs*: acción, confianza, tamaño, stop, take-profit y un razonamiento breve.
  - `hybrid` (el valor por defecto): las reglas filtran y Claude confirma, así que solo se paga el LLM
    cuando aporta algo.
- **Gestor de riesgo** con la última palabra:
  - dimensiona cada orden por riesgo fijo (el % de equity que se pierde si salta el stop);
  - aplica posición máxima, confianza mínima, cooldown, límite diario de operaciones y kill switches.
- **Salidas protectoras** (stop-loss y take-profit) y **kill switches** (pérdida diaria y drawdown) que
  **no dependen del LLM**.
- **Backtesting sin look-ahead**, con informe HTML autocontenido: equity frente a buy & hold, drawdown y
  operaciones.
- **Journal SQLite**: guarda cada decisión, orden ejecutada (fill), operación, punto de equity y
  evento, y persiste el estado para reanudar después de un reinicio.
- **CLI** `jeb` con los comandos `backtest`, `decide`, `paper`, `live`, `download` y `status`.

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
  |  3. Motor de decisión: rules | claude | hybrid                  |
  |        (si falla o se agota el presupuesto -> HOLD)             |
  |  4. RiskManager: aprueba, achica o rechaza      <- última       |
  |                                                    palabra      |
  |  5. Broker: PaperBroker (simulado) | CcxtBroker (real)          |
  |  6. Portfolio (costo, PnL, límites diarios) + Journal (SQLite)  |
  +-----------------------------------------------------------------+
```

| Módulo | Responsabilidad |
|---|---|
| `jeb/models.py`, `jeb/config.py` | Contratos compartidos (pydantic) y configuración desde el entorno |
| `jeb/market/` | Fuentes de datos: `CcxtMarket`, `CsvMarket`, `SyntheticMarket` (regímenes alcista, bajista y lateral) |
| `jeb/indicators.py`, `jeb/features.py` | Indicadores puros y construcción del `MarketSnapshot` |
| `jeb/brain/` | Motores `rules`, `claude` e `hybrid`, y su fábrica |
| `jeb/risk.py`, `jeb/portfolio.py` | Gestión de riesgo, kill switches, contabilidad de la posición |
| `jeb/execution/` | `PaperBroker` (slippage y comisiones) y `CcxtBroker` (órdenes reales, testnet por defecto) |
| `jeb/engine.py` | Bucle en tiempo real (paper y live) |
| `jeb/backtest.py` | Backtester sin look-ahead que reutiliza el mismo riesgo, portfolio y broker simulado |
| `jeb/journal.py`, `jeb/metrics.py`, `jeb/report.py` | Journal, métricas (Sharpe, Sortino, drawdown…) e informe HTML |
| `jeb/cli.py` | Línea de comandos |

## Cómo se toma una decisión

1. Llegan solo **velas cerradas**; la vela que todavía se está formando nunca se usa.
2. Se calculan los indicadores sobre una ventana fija de historia (`JEB_HISTORY_CANDLES`, 200 por
   defecto). El backtest usa la misma ventana, así los valores coinciden con los del modo en vivo.
3. Si hay una posición abierta, primero se revisan **stop-loss y take-profit** en todas las velas nuevas.
   Si alguno se tocó, se cierra la posición y en esa vela no se consulta al motor.
4. Se marca la equity y se evalúa el **kill switch**. Si salta y `JEB_FLATTEN_ON_KILL=true`, se cierra la
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
- Una llamada a Claude suma la latencia de red y del modelo: `jeb decide` la mide, y el timeout por
  defecto es de 8 s.
- En modo `hybrid`, Claude se consulta solo en tres casos:
  - **para confirmar una entrada** cuando las reglas ven un setup. Sin confirmación no se compra, y el
    stop que se usa es el más ajustado de los dos;
  - **en un heartbeat** cada `JEB_HYBRID_HEARTBEAT_CANDLES` velas (12 por defecto), para detectar una
    salida anticipada o una entrada que el filtro de tendencia permita;
  - **nunca para salir**: si las reglas dicen SELL, se vende sin esperar al LLM. Stops, take-profits y
    kill switches no pasan por el LLM.
- Si Claude falla (sin clave, timeout, error de la API, respuesta inválida o presupuesto diario agotado),
  la decisión es **HOLD**. Un fallo del LLM nunca abre una posición ni bloquea una salida.

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

`JEB_MAX_LLM_COST_USD_PER_DAY` (US$1 por defecto) corta las llamadas al alcanzar el tope diario, y JEB
sigue con HOLD. En backtests con LLM, `jeb backtest` estima el costo antes de empezar y pide `--yes` si el
peor caso supera US$1. `--max-llm-calls` (200 por defecto) pasa al motor de reglas cuando se alcanza el
tope.

## Inicio rápido

Requisitos: Python 3.10 o superior.

```bash
cd jeb-trader
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # completá lo que necesites; .env nunca se sube al repo

# 1) Backtest offline con datos sintéticos y el motor de reglas (sin claves, sin red)
jeb backtest --source synthetic --engine rules
#    -> imprime el resumen y escribe reports/backtest.html

# 2) Una decisión rápida de demostración (no envía órdenes)
jeb decide --synthetic                    # usa el motor de JEB_ENGINE
jeb decide --synthetic --engine rules

# 3) Paper trading
jeb paper --synthetic --fast --max-iterations 200 --engine rules   # simulación offline acelerada
jeb paper                                                          # precios reales, dinero simulado

# 4) Estado y journal
jeb status
```

Para usar Claude, poné `ANTHROPIC_API_KEY` en `.env` o en el entorno. Sin clave, JEB funciona igual: cada
consulta al LLM devuelve HOLD y la CLI lo avisa.

### Testnet de Binance (modo live sin dinero real)

1. Creá claves en la *Spot Test Network* de Binance (`testnet.binance.vision`).
2. En `.env`, configurá `JEB_MODE=live`, `JEB_USE_TESTNET=true`, `JEB_API_KEY=...` y `JEB_API_SECRET=...`.
3. Ejecutá `jeb live`. Se muestra un banner con la red (TESTNET) y el bot opera con fondos de prueba.

## Modelo de seguridad

- **Paper por defecto** (`JEB_MODE=paper`). `paper`, `decide`, `download` y `backtest` nunca envían
  órdenes y solo leen datos públicos, sin claves.
- **Testnet por defecto** (`JEB_USE_TESTNET=true`) cuando se activa el modo live.
- **Dinero real requiere tres opt-ins explícitos**: `JEB_MODE=live`, `JEB_USE_TESTNET=false` y
  `JEB_LIVE_CONFIRM=YES_I_ACCEPT_REAL_MONEY_RISK`. Además, `jeb live` pide escribir el símbolo exacto para
  confirmar; solo se puede omitir con `--yes` **y** `JEB_LIVE_CONFIRM` configurado. Recién después de esas
  comprobaciones se crea el broker con `allow_mainnet=True`.
- **Claves de API**:
  - **sin permiso de retiro**;
  - con **whitelist de IP**;
  - solo con permiso de trading spot.
  - JEB nunca imprime ni guarda las claves: el journal redacta los campos sensibles.
- **Kill switches**:
  - pérdida diaria (`JEB_MAX_DAILY_LOSS_PCT`): bloquea nuevas entradas hasta el próximo día UTC;
  - drawdown desde el pico (`JEB_MAX_DRAWDOWN_PCT`): bloquea hasta un reset manual con
    `jeb status --reset-halt`.
  - Con `JEB_FLATTEN_ON_KILL=true` también se cierra la posición. El estado de halt se persiste, así que
    reiniciar no lo borra.
- **Cooldown** tras cada salida (`JEB_COOLDOWN_CANDLES`), **límite diario de operaciones** y **posición
  máxima** (`JEB_MAX_POSITION_PCT`).
- **El LLM solo puede achicar**: su `size_pct` es un tope adicional. Nunca supera el tamaño que calcula el
  riesgo, y los stops se recortan a `[JEB_MIN_STOP_PCT, JEB_MAX_STOP_PCT]`.
- **Salidas protectoras sin LLM**: stop-loss y take-profit se evalúan antes de consultar a cualquier motor.
- **Fallo del LLM = HOLD**, con presupuesto diario en USD.
- **Estado incierto de una orden**: si el exchange no confirma si una orden se ejecutó, JEB bloquea nuevas
  entradas y lo registra para que una persona lo revise.
- **Spot, solo largo y sin apalancamiento**: SELL solo reduce una posición existente.

## Configuración

Todo se configura con variables de entorno o con `.env`; las variables del entorno tienen prioridad. Los
flags de la CLI (`--engine`, `--symbol`, `--timeframe`, `--journal`) pisan esos valores.

| Variable | Por defecto | Descripción |
|---|---|---|
| `ANTHROPIC_API_KEY` | (vacía) | Clave de la API de Anthropic. Sin clave, el LLM responde HOLD |
| `JEB_MODEL` | `claude-haiku-4-5` | Modelo de Claude |
| `JEB_ENGINE` | `hybrid` | `hybrid`, `claude` o `rules` |
| `JEB_LLM_TIMEOUT_S` | `8` | Timeout por llamada al LLM, en segundos |
| `JEB_MAX_LLM_COST_USD_PER_DAY` | `1.0` | Presupuesto diario del LLM (0 lo desactiva) |
| `JEB_EXCHANGE` | `binance` | Id del exchange en ccxt |
| `JEB_SYMBOL` | `BTC/USDT` | Par spot `BASE/QUOTE` |
| `JEB_TIMEFRAME` | `5m` | Timeframe de las velas (`1m`, `5m`, `1h`, `1d`…) |
| `JEB_MODE` | `paper` | `paper` o `live` |
| `JEB_USE_TESTNET` | `true` | En modo live: testnet (`true`) o mainnet (`false`) |
| `JEB_LIVE_CONFIRM` | (vacía) | Para mainnet: `YES_I_ACCEPT_REAL_MONEY_RISK` |
| `JEB_API_KEY` / `JEB_API_SECRET` | (vacías) | Claves del exchange (solo modo live) |
| `JEB_PAPER_START_CASH` | `1000` | Capital inicial simulado, en moneda quote |
| `JEB_FEE_PCT` | `0.1` | Comisión por lado, en % |
| `JEB_SLIPPAGE_PCT` | `0.05` | Slippage simulado por lado, en % |
| `JEB_RISK_PER_TRADE_PCT` | `1.0` | % de equity que se pierde si salta el stop |
| `JEB_MAX_POSITION_PCT` | `25` | Posición máxima, en % de la equity |
| `JEB_MAX_DAILY_LOSS_PCT` | `3` | Kill switch por pérdida desde el inicio del día UTC |
| `JEB_MAX_DRAWDOWN_PCT` | `10` | Kill switch por caída desde el pico de equity |
| `JEB_MAX_TRADES_PER_DAY` | `10` | Fills por día UTC (compras y ventas) a partir de los cuales no se abren posiciones nuevas |
| `JEB_MIN_CONFIDENCE` | `0.6` | Confianza mínima para comprar (para vender, la mitad) |
| `JEB_COOLDOWN_CANDLES` | `3` | Velas de espera después de cerrar una posición |
| `JEB_DEFAULT_STOP_PCT` | `2` | Stop si el motor no propone uno |
| `JEB_DEFAULT_TAKE_PROFIT_PCT` | `4` | Take-profit si el motor no propone uno |
| `JEB_MIN_ORDER_NOTIONAL` | `10` | Tamaño mínimo de orden, en moneda quote |
| `JEB_FLATTEN_ON_KILL` | `true` | Cerrar la posición cuando salta un kill switch |

Variables avanzadas, opcionales:

| Variable | Por defecto | Descripción |
|---|---|---|
| `JEB_LLM_MAX_RETRIES` | `1` | Reintentos del cliente de Anthropic |
| `JEB_LLM_MAX_TOKENS` | `400` | Tope de tokens de salida por decisión |
| `JEB_HYBRID_HEARTBEAT_CANDLES` | `12` | Cada cuántas velas el modo híbrido consulta a Claude sin setup |
| `JEB_HISTORY_CANDLES` | `200` | Velas de historia por decisión (mínimo 60) |
| `JEB_JOURNAL_PATH` | `jeb_journal.sqlite3` | Archivo SQLite del journal |
| `JEB_MIN_STOP_PCT` / `JEB_MAX_STOP_PCT` | `0.3` / `10` | Límites del stop aceptado por el riesgo |

## Comandos

| Comando | Qué hace |
|---|---|
| `jeb backtest [--source synthetic\|csv\|exchange] [--csv RUTA] [--candles N] [--seed S] [--engine E] [--max-llm-calls N] [--warmup N] [--report RUTA] [--journal RUTA] [--yes]` | Backtest sin look-ahead. Imprime el resumen y escribe el informe HTML (`reports/backtest.html` por defecto) |
| `jeb decide [--synthetic] [--engine E]` | Una decisión de demostración: snapshot, decisión, latencia y costo. **No envía órdenes** |
| `jeb paper [--synthetic] [--fast] [--max-iterations N] [--journal RUTA] [--fresh]` | Paper trading en tiempo real: espera cada cierre de vela más 2 s. `--synthetic --fast` simula sin esperas |
| `jeb live [--max-iterations N] [--yes]` | Órdenes reales por ccxt (testnet por defecto). Requiere `JEB_MODE=live` |
| `jeb download --since YYYY-MM-DD [--until YYYY-MM-DD] --out RUTA.csv` | Descarga velas históricas públicas a CSV |
| `jeb status [--journal RUTA] [--limit N] [--reset-halt]` | Resumen del journal, estado guardado y últimas decisiones y eventos |

Todos aceptan `-v` (INFO) o `-vv` (DEBUG), y `--env-file`. También funciona `python -m jeb …`. Códigos de
salida: `0` si todo salió bien, `1` si hubo un error en ejecución y `2` si hubo un error de configuración
o de uso.

Cómo funcionan `paper` y `live`:

- Guardan el estado en el journal después de cada vela y lo reanudan al reiniciar: posición, stops, saldo
  simulado, halts y última vela procesada. `--fresh` ignora el estado guardado en `paper`.
- **Ctrl+C** (o SIGTERM) detiene el bot guardando el estado.

## Backtesting sin look-ahead

- La decisión de la vela *i* ve solo `velas[: i + 1]`, con una ventana acotada, igual que en vivo.
- La orden se ejecuta en la **apertura de la vela *i + 1*** a través del `PaperBroker`, con slippage y
  comisiones.
- Stops y take-profits:
  - se revisan en cada vela desde la entrada, con su mínimo y su máximo;
  - se ejecutan en el nivel, o en la apertura si la vela abrió más allá del stop (el peor de los dos
    precios).
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
jeb backtest --source synthetic --engine rules --candles 3000 --report reports/demo.html
```

Corre con la semilla 42, 5 minutos por vela, la configuración por defecto y el motor `rules`. **Los datos
son sintéticos, no de mercado:**

| Métrica | Valor |
|---|---|
| Retorno total | −4,01 % |
| Buy & hold (mismo período) | −10,41 % |
| Máximo drawdown | 4,24 % |
| Operaciones cerradas | 51 (21,6 % ganadoras, profit factor 0,31) |
| Comisiones pagadas | 25,14 USDT sobre 1.000 iniciales |
| Exposición | 16,6 % del tiempo |

La estrategia de reglas **perdió dinero** con esta serie. Perdió menos que buy & hold sobre todo porque
estuvo invertida solo el 16,6 % del tiempo en un mercado que cayó, no porque tenga ventaja. Los datos sintéticos son un paseo aleatorio con regímenes
y **no tienen un edge explotable**: sirven para probar el sistema de punta a punta, no para evaluar la
estrategia. Las comisiones y el slippage (~0,3 % por operación completa) se comen cualquier ventaja
pequeña.

## Tests

```bash
.venv/bin/pytest -q
```

La suite (~600 tests) corre **offline** en pocos segundos. Usa fakes para el exchange, el broker y el
cliente de Anthropic, y no hace ninguna llamada de red.

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
