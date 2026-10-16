"""FastAPI app: Facebook webhook, demo page, and the read-only lead feed."""
from __future__ import annotations

import json
import logging
import statistics
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import fb
from .config import ROOT, Settings
from .llm import AnthropicQualifier, MockQualifier
from .pipeline import Pipeline, now_iso
from .sinks import AppsScriptSheet, ConsoleNotifier, DiscordNotifier, MockSheet, SlackNotifier
from .store import LeadStore

log = logging.getLogger("blaze")
DEMO_HTML = ROOT / "app" / "static" / "demo.html"


def build_pipeline(settings: Settings, http: httpx.AsyncClient) -> Pipeline:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = LeadStore(settings.leads_path)
    qualifier = MockQualifier(settings) if settings.mock_llm else AnthropicQualifier(settings)
    sheet = (
        MockSheet(settings.data_dir / "mock_sheet.csv") if settings.mock_sheet
        else AppsScriptSheet(settings.sheets_url, settings.sheets_token, http)
    )
    if settings.mock_notify:
        notifier = ConsoleNotifier(settings.data_dir / "mock_alerts.log")
    elif settings.notifier == "slack":
        notifier = SlackNotifier(settings.slack_webhook_url, http)
    else:
        notifier = DiscordNotifier(settings.discord_webhook_url, http)
    return Pipeline(settings, store, qualifier, notifier, sheet)


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = max(0, min(len(v) - 1, round(p / 100 * (len(v) - 1))))
    return v[k]


def create_app(settings: Settings | None = None, pipeline: Pipeline | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        app.state.pipeline = pipeline or build_pipeline(settings, http)
        for p in settings.problems():
            log.warning("config: %s", p)
        log.info(
            "ready: llm=%s sheet=%s notify=%s",
            "mock" if settings.mock_llm else settings.model,
            "mock" if settings.mock_sheet else "apps-script",
            "mock" if settings.mock_notify else settings.notifier,
        )
        yield
        await app.state.pipeline.wait_idle(timeout=30)
        await http.aclose()

    app = FastAPI(title="Blaze speed-to-lead", lifespan=lifespan)
    app.state.settings = settings
    if pipeline is not None:  # tests can inject without running lifespan
        app.state.pipeline = pipeline

    # ---------------- Facebook webhook ----------------
    @app.get("/webhook")
    async def verify(request: Request):
        q = request.query_params
        if (
            q.get("hub.mode") == "subscribe"
            and settings.fb_verify_token
            and q.get("hub.verify_token") == settings.fb_verify_token
        ):
            return PlainTextResponse(q.get("hub.challenge", ""))
        return PlainTextResponse("forbidden", status_code=403)

    @app.post("/webhook")
    async def webhook(request: Request):
        t0 = time.perf_counter()  # the clock starts the moment the request reaches us
        received_at = now_iso()
        body = await request.body()
        if not fb.verify_signature(settings.fb_app_secret, body, request.headers.get("x-hub-signature-256")):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        try:
            payload = json.loads(body)
            result = app.state.pipeline.accept(payload, t0, received_at)
        except (ValueError, TypeError) as e:
            return JSONResponse({"error": f"bad payload: {e}"}, status_code=400)
        ack_ms = (time.perf_counter() - t0) * 1000
        app.state.pipeline.note_ack(result["leadgen_id"], ack_ms)
        return JSONResponse(result | {"ack_ms": round(ack_ms, 1)})

    # ---------------- read-only feed ----------------
    @app.get("/api/leads")
    async def api_leads(limit: int = Query(50, ge=1, le=500), ids: str = ""):
        leads = app.state.pipeline.store.leads()
        processed = [r for r in leads if r["status"] in ("scored", "fallback")]
        lat = [r["latency_ms"] for r in processed if r.get("latency_ms") is not None]
        stats = {
            "processed": len(processed),
            "scored": sum(r["status"] == "scored" for r in processed),
            "fallback": sum(r["status"] == "fallback" for r in processed),
            "duplicates": sum(r["status"] == "duplicate" for r in leads),
            "median_ms": statistics.median(lat) if lat else None,
            "p95_ms": _pct(lat, 95),
            "total_cost_usd": round(sum(r.get("cost_usd") or 0 for r in processed), 6),
            "mode": {
                "llm": "mock" if settings.mock_llm else settings.model,
                "sheet": "mock" if settings.mock_sheet else "apps-script",
                "notify": "mock" if settings.mock_notify else settings.notifier,
            },
        }
        if ids:
            wanted = set(ids.split(","))
            leads = [r for r in leads if r["leadgen_id"] in wanted]
        return {"stats": stats, "leads": leads[:limit]}

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    # ---------------- demo page (isolated: failures here never touch /webhook) ----------------
    @app.get("/demo", response_class=HTMLResponse)
    async def demo():
        if not settings.demo_enabled:
            return HTMLResponse("demo disabled", status_code=404)
        try:
            return HTMLResponse(DEMO_HTML.read_text(encoding="utf-8"))
        except Exception as e:
            return HTMLResponse(f"demo page unavailable: {e}", status_code=500)

    @app.post("/demo/submit")
    async def demo_submit(request: Request):
        if not settings.demo_enabled:
            return JSONResponse({"error": "demo disabled"}, status_code=404)
        try:
            form = await request.json()
            payload = fb.build_payload(
                leadgen_id=f"demo-{uuid.uuid4().hex[:12]}",
                created_time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000"),
                name=str(form.get("name", "")), email=str(form.get("email", "")),
                phone=str(form.get("phone", "")), company=str(form.get("company", "")),
                doors=str(form.get("doors", "")), message=str(form.get("message", "")),
            )
            body = json.dumps(payload).encode()
            # Sign server-side and replay through the real /webhook route in-process,
            # so the demo exercises the exact production path (HMAC included).
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://demo.local") as c:
                r = await c.post(
                    "/webhook", content=body,
                    headers={"content-type": "application/json",
                             "x-hub-signature-256": fb.sign(settings.fb_app_secret, body)},
                )
            return JSONResponse(r.json(), status_code=r.status_code)
        except Exception as e:
            log.exception("demo submit failed")
            return JSONResponse({"error": f"demo submit failed: {type(e).__name__}"}, status_code=500)

    return app
