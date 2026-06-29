# Load test del chatbot

Harness de carga para responder: **¿qué pasa cuando muchos webhooks / usuarios pegan a la vez?**

Mide la capacidad de **concurrencia del servicio** (event loop, pool de DB, manejo de
requests), no la latencia de Anthropic ni de Meta. Por eso esas APIs se **mockean**.

## Qué mide

| Métrica | Significado |
|---|---|
| **p50 / p95 / p99 (ms)** | Latencia de respuesta. El p99 es el que duele bajo carga. |
| **throughput (req/s)** | Requests completados por segundo. Si sube la concurrencia y el throughput **no sube** (o baja), hay saturación. |
| **error-rate (%)** | no-2xx + fallos de conexión/timeout. |
| **sweep** | Corre el mismo escenario a varias concurrencias para encontrar **dónde se rompe**. |

## Seguridad

- Por **default** apunta a `http://127.0.0.1:8000` (localhost).
- El harness **rechaza** targets que parezcan prod (`https://`, `*.railway.app`, IPs no-loopback)
  salvo `--i-know-what-im-doing`.
- **NUNCA correr contra producción.** Esto genera tráfico sintético y escribe en la DB.

## Cómo correrlo (2 terminales)

Requiere las deps del repo (`pip install -r requirements.txt`) — usa solo `httpx`, ya presente.

```bash
# Terminal 1 — app REAL con Claude/Meta mockeados + SQLite (se recrea limpia):
python loadtest/stub_app.py
#   AI_DELAY_MS=150  -> simula latencia del modelo (default). Subilo para realismo.
#   AI_DELAY_MS=0    -> aísla el techo puro de DB/event-loop.
#   PORT=8001        -> otro puerto.

# Terminal 2 — el harness:
python loadtest/harness.py --scenario webhook --requests 200 --concurrency 50
python loadtest/harness.py --scenario chat    --requests 15  --concurrency 10   # OJO: /chat tiene rate-limit 20/min
python loadtest/harness.py --scenario sweep --sweep-scenario webhook --requests 300 --sweep-levels 1,10,25,50,100
python loadtest/harness.py --scenario sweep --json > resultados.json            # para comparar antes/después
```

### Escenarios

| `--scenario` | Endpoint | Para qué |
|---|---|---|
| `webhook`   | `POST /webhook/meta` | Hot path de Meta. **No** tiene rate-limit (todos los webhooks vienen de la misma IP de Facebook). |
| `chat`      | `POST /chat/`        | Chat web. **Rate-limited a 20/min por IP** → usar pocos requests o subir el límite en el server de carga. |
| `chat_same` | `POST /chat/`        | Misma `session_id`: mide contención sobre una conversación caliente (historial creciente). |
| `health`    | `GET /health/ready`  | Baseline barato: round-trip + `SELECT 1`. |

## Caveat importante: SQLite vs Postgres

`stub_app.py` usa **SQLite** por simplicidad (cero setup). SQLite **serializa las escrituras**
(un solo writer) y usa el `QueuePool` de SQLAlchemy con defaults (5+10). Prod usa **Postgres**
con pool 10+20. Por eso:

- Los **números absolutos** de acá no son los de prod.
- Pero la **forma de la curva** (throughput que se aplana / latencia que se dispara al subir la
  concurrencia) sí refleja el cuello de botella real: **I/O de DB síncrono dentro de handlers
  `async`** que bloquea el event loop mientras tiene tomada una conexión del pool.

Para un test fiel a prod, apuntá `DATABASE_URL` a un **Postgres de staging** antes de arrancar
`stub_app.py` y volvé a correr el sweep.

## Resultados reales medidos (2026-06-28, SQLite local, M-series)

Sweep de `/webhook/meta`, 300 reqs por nivel, `AI_DELAY_MS=0` (techo puro DB/event-loop):

```
conc=1    thr= 50.4 req/s  p50= 19.4  p95=  22.0  p99=  29.7 ms
conc=10   thr=199.8 req/s  p50= 35.6  p95=  88.6  p99= 464.7 ms
conc=25   thr=109.4 req/s  p50=156.0  p95= 538.0  p99= 875.4 ms   <- throughput ya cae
conc=50   thr= 68.1 req/s  p50=436.6  p95=2178.6  p99=3264.8 ms
conc=100  thr= 55.6 req/s  p50=1105   p95=4694    p99=4982   ms   <- p99 ~5s
```

Lectura: el throughput **toca techo cerca de concurrencia 10** y **baja** a partir de 25,
mientras el p99 se dispara de ~30 ms a ~5 s. Es la firma del **I/O síncrono que bloquea el
event loop** (y, en SQLite, del writer único). En prod (Postgres) el techo es más alto pero la
curva es la misma: las secciones de DB de cada request son secuenciales en el hilo del loop.

Además, con `AI_DELAY_MS=150`, `/chat` a 25 reqs en paralelo **agota el pool** y los requests
encolan hasta `pool_timeout` (30s) y luego fallan con `QueuePool limit ... reached` — porque la
conexión queda **tomada durante todo el `await` a Claude**.
```
sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached, connection timed out
```
