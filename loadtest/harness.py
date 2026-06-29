#!/usr/bin/env python3
"""Harness de carga (asyncio + httpx) para el chatbot.

Mide la capacidad de CONCURRENCIA del servicio: cuántos requests simultáneos
aguanta antes de que la latencia se dispare. Apunta a endpoints calientes
(/chat, /webhook/meta, /health) de una instancia YA levantada.

Usa solo httpx, que ya es dependencia del repo: no hace falta instalar k6/locust.

QUÉ MIDE
  - Latencia p50 / p95 / p99 (ms) por endpoint.
  - Throughput (requests/s efectivos).
  - Tasa de error (no-2xx + fallos de conexión).
  - Curva de concurrencia: corre el mismo escenario a varios niveles de
    concurrencia para ver dónde se rompe (la latencia crece, el throughput
    se aplana = saturación del event loop / pool de DB).

SEGURIDAD
  - Por DEFAULT apunta a http://127.0.0.1:8000 (localhost).
  - Rechaza explícitamente cualquier host que parezca producción
    (railway.app, https://, IP no-loopback) salvo --i-know-what-im-doing.
  - NUNCA correr contra prod.

USO
  # 1) Levantar la app stub (Claude/Meta mockeados, SQLite) en otra terminal:
  python loadtest/stub_app.py
  # 2) Correr el harness:
  python loadtest/harness.py --scenario chat --requests 200 --concurrency 25
  python loadtest/harness.py --scenario webhook --requests 200 --concurrency 50
  python loadtest/harness.py --scenario sweep   # barre 1,5,10,25,50,100

  Ver --help para todas las opciones.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("Falta httpx. Instalá las deps del repo: pip install -r requirements.txt")


# --------------------------------------------------------------------------- #
# Resultado de un request individual
# --------------------------------------------------------------------------- #
@dataclass
class Sample:
    latency_ms: float
    status: int          # 0 = fallo de conexión/timeout
    ok: bool


@dataclass
class Report:
    label: str
    concurrency: int
    samples: list[Sample] = field(default_factory=list)
    wall_seconds: float = 0.0

    @property
    def n(self) -> int:
        return len(self.samples)

    @property
    def errors(self) -> int:
        return sum(1 for s in self.samples if not s.ok)

    @property
    def error_rate(self) -> float:
        return self.errors / self.n if self.n else 0.0

    @property
    def throughput(self) -> float:
        return self.n / self.wall_seconds if self.wall_seconds else 0.0

    def pct(self, p: float) -> float:
        oks = sorted(s.latency_ms for s in self.samples if s.ok)
        if not oks:
            return float("nan")
        k = max(0, min(len(oks) - 1, int(round((p / 100.0) * (len(oks) - 1)))))
        return oks[k]

    def line(self) -> str:
        return (
            f"  conc={self.concurrency:<4} n={self.n:<5} "
            f"err={self.error_rate * 100:5.1f}%  "
            f"thr={self.throughput:7.1f} req/s  "
            f"p50={self.pct(50):7.1f}  p95={self.pct(95):7.1f}  "
            f"p99={self.pct(99):7.1f} ms"
        )


# --------------------------------------------------------------------------- #
# Generadores de payload por escenario
# --------------------------------------------------------------------------- #
def chat_request(base: str, i: int) -> dict:
    """POST /chat — sesión distinta por request (peor caso: crea conversación)."""
    return {
        "method": "POST",
        "url": f"{base}/chat/",
        "json": {
            "session_id": f"load-{i}",
            "project": "agencia",
            "message": "Hola, busco un auto usado economico",
            "channel": "web",
        },
    }


def chat_same_session_request(base: str, i: int, session: str = "load-shared") -> dict:
    """POST /chat reusando UNA sesión: mide contención sobre la misma conversación
    (lecturas de historial crecientes + commits sobre filas calientes)."""
    return {
        "method": "POST",
        "url": f"{base}/chat/",
        "json": {
            "session_id": session,
            "project": "agencia",
            "message": f"mensaje numero {i}",
            "channel": "web",
        },
    }


def webhook_request(base: str, i: int) -> dict:
    """POST /webhook/meta — payload de WhatsApp tipo, cada uno con wamid único.

    Requiere que la firma esté desactivada en el server de carga
    (ALLOW_UNSIGNED_WEBHOOKS=1 + sin META_APP_SECRET), como hace stub_app.py."""
    body = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messages": [
                                {
                                    "id": f"wamid_load_{i}",
                                    "type": "text",
                                    "from": f"54911{i:08d}",
                                    "text": {"body": "hola, info por favor"},
                                }
                            ]
                        },
                    }
                ]
            }
        ]
    }
    return {"method": "POST", "url": f"{base}/webhook/meta", "json": body}


def health_request(base: str, i: int) -> dict:
    """GET /health/ready — toca la DB (SELECT 1). Baseline barato del round-trip."""
    return {"method": "GET", "url": f"{base}/health/ready"}


SCENARIOS = {
    "chat": chat_request,
    "chat_same": chat_same_session_request,
    "webhook": webhook_request,
    "health": health_request,
}


# --------------------------------------------------------------------------- #
# Motor de carga
# --------------------------------------------------------------------------- #
async def _one(client: httpx.AsyncClient, spec: dict) -> Sample:
    t0 = time.perf_counter()
    try:
        r = await client.request(
            spec["method"], spec["url"], json=spec.get("json"), timeout=30.0
        )
        dt = (time.perf_counter() - t0) * 1000.0
        return Sample(dt, r.status_code, 200 <= r.status_code < 300)
    except Exception:
        dt = (time.perf_counter() - t0) * 1000.0
        return Sample(dt, 0, False)


async def run_scenario(
    base: str, scenario: str, total: int, concurrency: int, label: str
) -> Report:
    gen = SCENARIOS[scenario]
    sem = asyncio.Semaphore(concurrency)
    report = Report(label=label, concurrency=concurrency)
    limits = httpx.Limits(
        max_connections=concurrency + 10, max_keepalive_connections=concurrency + 10
    )

    async with httpx.AsyncClient(limits=limits) as client:

        async def worker(i: int):
            async with sem:
                report.samples.append(await _one(client, gen(base, i)))

        t0 = time.perf_counter()
        await asyncio.gather(*(worker(i) for i in range(total)))
        report.wall_seconds = time.perf_counter() - t0

    return report


# --------------------------------------------------------------------------- #
# Guardas de seguridad
# --------------------------------------------------------------------------- #
def assert_safe_target(base: str, override: bool) -> None:
    if override:
        return
    u = urlparse(base)
    host = (u.hostname or "").lower()
    looks_prod = (
        u.scheme == "https"
        or host.endswith("railway.app")
        or host.endswith(".app")
        or (host not in ("127.0.0.1", "localhost", "0.0.0.0", "::1"))
    )
    if looks_prod:
        sys.exit(
            f"NEGADO: '{base}' parece NO-local. El harness es solo para localhost/staging.\n"
            f"Si de verdad querés apuntar ahí (NUNCA prod), usá --i-know-what-im-doing."
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    p = argparse.ArgumentParser(description="Harness de carga del chatbot")
    p.add_argument("--base", default="http://127.0.0.1:8000", help="URL base (default localhost:8000)")
    p.add_argument(
        "--scenario",
        default="chat",
        choices=[*SCENARIOS.keys(), "sweep"],
        help="Escenario o 'sweep' (barre concurrencias sobre --sweep-scenario)",
    )
    p.add_argument("--requests", type=int, default=200, help="Total de requests (default 200)")
    p.add_argument("--concurrency", type=int, default=25, help="Requests en vuelo a la vez (default 25)")
    p.add_argument(
        "--sweep-scenario",
        default="webhook",
        choices=list(SCENARIOS.keys()),
        help="Escenario a usar en 'sweep' (default webhook)",
    )
    p.add_argument(
        "--sweep-levels",
        default="1,5,10,25,50,100",
        help="Niveles de concurrencia para 'sweep' (coma-separados)",
    )
    p.add_argument("--json", action="store_true", help="Salida JSON (para guardar/comparar)")
    p.add_argument("--i-know-what-im-doing", action="store_true", help="Saltea la guarda de target no-local")
    args = p.parse_args()

    base = args.base.rstrip("/")
    assert_safe_target(base, args.i_know_what_im_doing)

    print(f"# target: {base}")
    reports: list[Report] = []

    if args.scenario == "sweep":
        levels = [int(x) for x in args.sweep_levels.split(",") if x.strip()]
        print(f"# sweep '{args.sweep_scenario}': concurrencias {levels}, {args.requests} reqs c/u\n")
        for c in levels:
            rep = asyncio.run(
                run_scenario(base, args.sweep_scenario, args.requests, c, f"{args.sweep_scenario}@{c}")
            )
            reports.append(rep)
            if not args.json:
                print(rep.line())
    else:
        print(f"# scenario '{args.scenario}': {args.requests} reqs, concurrency {args.concurrency}\n")
        rep = asyncio.run(run_scenario(base, args.scenario, args.requests, args.concurrency, args.scenario))
        reports.append(rep)
        if not args.json:
            print(rep.line())

    if args.json:
        out = [
            {
                "label": r.label,
                "concurrency": r.concurrency,
                "n": r.n,
                "errors": r.errors,
                "error_rate": round(r.error_rate, 4),
                "throughput_rps": round(r.throughput, 2),
                "p50_ms": round(r.pct(50), 2),
                "p95_ms": round(r.pct(95), 2),
                "p99_ms": round(r.pct(99), 2),
                "wall_s": round(r.wall_seconds, 3),
            }
            for r in reports
        ]
        print(json.dumps(out, indent=2))

    # Exit code != 0 si alguna corrida tuvo error-rate alto (útil para CI/gates).
    worst = max((r.error_rate for r in reports), default=0.0)
    sys.exit(1 if worst > 0.10 else 0)


if __name__ == "__main__":
    main()
