"""Spoolman Kitty — print Spoolman QR labels on a Bluetooth cat printer.

The server never touches Bluetooth. It watches Spoolman, renders labels into
ready-to-send printer byte streams, and hands them to "print agents":
  * a browser tab using Web Bluetooth (the web UI at /), and/or
  * an ESP32 sitting next to the printer.
Agents long-poll /api/agent/next, stream the bytes to the printer, and report
back on /api/agent/done. When a checkbox job succeeds, the server unticks the
spool's "Print label" field in Spoolman.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from label import build_job, render_label

log = logging.getLogger("spoolman-kitty")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

SPOOLMAN_URL = os.getenv("SPOOLMAN_URL", "http://spoolman:8000").rstrip("/")
PUBLIC_SPOOLMAN_URL = os.getenv("PUBLIC_SPOOLMAN_URL", SPOOLMAN_URL).rstrip("/")  # what goes in the QR
FIELD_KEY = os.getenv("FIELD_KEY", "print_label")
QR_MODE = os.getenv("QR_MODE", "url")  # url | spoolman
ENERGY = int(os.getenv("ENERGY", "65535"))
LABEL_HEIGHT_PX = int(os.getenv("LABEL_HEIGHT_PX", "240"))
FEED_PX = int(os.getenv("FEED_PX", "90"))
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "5"))
LEASE_SECONDS = float(os.getenv("LEASE_SECONDS", "90"))
RETRY_SECONDS = float(os.getenv("RETRY_SECONDS", "15"))
MAX_RETRY_SECONDS = float(os.getenv("MAX_RETRY_SECONDS", "300"))  # backoff ceiling for repeated failures
MAX_MANUAL_ATTEMPTS = int(os.getenv("MAX_MANUAL_ATTEMPTS", "5"))
STATIC = Path(__file__).parent / "static"


@dataclass
class Job:
    spool_id: int
    source: str  # "checkbox" | "manual"
    data: bytes
    label: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = "queued"  # queued | printing | done | failed | cancelled
    agent: str = ""
    created: float = field(default_factory=time.time)
    claimed: float = 0.0
    finished: float = 0.0
    attempts: int = 0
    not_before: float = 0.0
    error: str = ""

    def public(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "data"}
        d["bytes"] = len(self.data)
        return d


class State:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.agents: dict[str, dict] = {}
        self.spools: list[dict] = []
        self.spoolman_ok = False
        self.spoolman_error = ""
        self.field_ok = False
        self.to_clear: set[int] = set()
        self._changed = asyncio.Event()
        self.http: httpx.AsyncClient | None = None

    # ---- job queue -------------------------------------------------------
    def notify(self):
        self._changed.set()
        self._changed = asyncio.Event()

    def active_for(self, spool_id: int, source: str | None = None) -> Job | None:
        for j in self.jobs.values():
            if j.spool_id == spool_id and j.status in ("queued", "printing") and (source is None or j.source == source):
                return j
        return None

    def add_job(self, spool: dict, source: str) -> Job:
        img = render_label(spool, qr_mode=QR_MODE, public_url=PUBLIC_SPOOLMAN_URL, height=LABEL_HEIGHT_PX)
        fil = spool.get("filament") or {}
        label = " ".join(x for x in [fil.get("material"), fil.get("name")] if x) or "spool"
        job = Job(spool_id=spool["id"], source=source, data=build_job(img, ENERGY, FEED_PX), label=label)
        self.jobs[job.id] = job
        self._trim()
        log.info("Queued job %s for spool #%s (%s)", job.id, job.spool_id, source)
        self.notify()
        return job

    def _trim(self, keep: int = 60):
        finished = sorted((j for j in self.jobs.values() if j.status in ("done", "failed", "cancelled")),
                          key=lambda j: j.created)
        for j in finished[:max(0, len(finished) - keep)]:
            self.jobs.pop(j.id, None)

    def expire_leases(self):
        now = time.time()
        for j in self.jobs.values():
            if j.status == "printing" and now - j.claimed > LEASE_SECONDS:
                log.warning("Job %s lease expired on agent %s; requeueing", j.id, j.agent)
                j.status, j.error, j.agent = "queued", f"no answer from {j.agent}", ""

    def claim(self, agent: str) -> Job | None:
        self.expire_leases()
        now = time.time()
        for j in sorted(self.jobs.values(), key=lambda j: j.created):
            if j.status == "queued" and j.not_before <= now:
                j.status, j.agent, j.claimed = "printing", agent, now
                j.attempts += 1
                return j
        return None

    async def wait_change(self, timeout: float):
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    # ---- Spoolman ----------------------------------------------------------
    async def ensure_field(self):
        r = await self.http.get(f"{SPOOLMAN_URL}/api/v1/field/spool")
        r.raise_for_status()
        if not any(f.get("key") == FIELD_KEY for f in r.json()):
            body = {"name": "Print label", "field_type": "boolean", "default_value": "false", "order": 0}
            r = await self.http.post(f"{SPOOLMAN_URL}/api/v1/field/spool/{FIELD_KEY}", json=body)
            r.raise_for_status()
            log.info("Created '%s' checkbox field in Spoolman", FIELD_KEY)
        self.field_ok = True

    def flagged(self, spool: dict) -> bool:
        v = (spool.get("extra") or {}).get(FIELD_KEY)
        try:
            return v is not None and json.loads(v) is True
        except (ValueError, TypeError):
            return False

    def recently_printed(self, sid: int) -> bool:
        # Guards against a poll that fetched the spool list just before the untick landed.
        cutoff = time.time() - (2 * POLL_SECONDS + 10)
        return any(j.spool_id == sid and j.source == "checkbox" and j.status == "done" and j.finished > cutoff
                   for j in self.jobs.values())

    async def get_spool(self, sid: int) -> dict:
        r = await self.http.get(f"{SPOOLMAN_URL}/api/v1/spool/{sid}")
        r.raise_for_status()
        return r.json()

    async def clear_flag(self, sid: int) -> bool:
        try:
            spool = await self.get_spool(sid)
            extra = dict(spool.get("extra") or {})
            extra[FIELD_KEY] = "false"
            r = await self.http.patch(f"{SPOOLMAN_URL}/api/v1/spool/{sid}", json={"extra": extra})
            r.raise_for_status()
            self.to_clear.discard(sid)
            log.info("Unticked '%s' on spool #%s", FIELD_KEY, sid)
            return True
        except Exception as e:
            log.warning("Could not untick spool #%s yet: %s", sid, e)
            self.to_clear.add(sid)
            return False

    async def poll_once(self):
        if not self.field_ok:
            await self.ensure_field()
        r = await self.http.get(f"{SPOOLMAN_URL}/api/v1/spool")
        r.raise_for_status()
        spools = r.json()
        self.spools = spools
        self.spoolman_ok, self.spoolman_error = True, ""
        flagged_ids = set()
        for sp in spools:
            if not self.flagged(sp):
                continue
            flagged_ids.add(sp["id"])
            if sp["id"] in self.to_clear:
                await self.clear_flag(sp["id"])  # already printed, only the untick failed
            elif not self.active_for(sp["id"], "checkbox") and not self.recently_printed(sp["id"]):
                self.add_job(sp, "checkbox")
        # Unticked in Spoolman before it printed -> cancel
        for j in self.jobs.values():
            if j.source == "checkbox" and j.status == "queued" and j.spool_id not in flagged_ids:
                j.status, j.finished = "cancelled", time.time()
                log.info("Spool #%s unticked in Spoolman; cancelled job %s", j.spool_id, j.id)

    async def poll_loop(self):
        while True:
            try:
                await self.poll_once()
            except Exception as e:
                if self.spoolman_ok or not self.spoolman_error:
                    log.warning("Spoolman unreachable: %s", e)
                self.spoolman_ok, self.spoolman_error = False, str(e) or type(e).__name__
            await asyncio.sleep(POLL_SECONDS)


S = State()


@asynccontextmanager
async def lifespan(app):
    S.http = httpx.AsyncClient(timeout=10)
    task = asyncio.create_task(S.poll_loop())
    log.info("Watching Spoolman at %s", SPOOLMAN_URL)
    yield
    task.cancel()
    await S.http.aclose()


# ---- HTTP handlers -----------------------------------------------------------
async def index(_):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


async def api_state(_):
    S.expire_leases()
    now = time.time()
    agents = [
        {"name": n, **a, "online": now - a["last_seen"] < 40}
        for n, a in sorted(S.agents.items(), key=lambda kv: -kv[1]["last_seen"])
        if now - a["last_seen"] < 3600
    ]
    jobs = sorted((j.public() for j in S.jobs.values()), key=lambda j: -j["created"])
    return JSONResponse({
        "spoolman": {"url": SPOOLMAN_URL, "ok": S.spoolman_ok, "error": S.spoolman_error, "field_ok": S.field_ok},
        "agents": agents,
        "jobs": jobs,
        "config": {"qr_mode": QR_MODE, "label_height_px": LABEL_HEIGHT_PX, "field_key": FIELD_KEY},
    })


async def api_spools(_):
    out = []
    for sp in S.spools:
        fil = sp.get("filament") or {}
        out.append({
            "id": sp["id"],
            "material": fil.get("material") or "",
            "name": fil.get("name") or "",
            "vendor": (fil.get("vendor") or {}).get("name", ""),
            "color": fil.get("color_hex") or "",
            "archived": sp.get("archived", False),
            "flagged": S.flagged(sp),
        })
    return JSONResponse(sorted(out, key=lambda s: -s["id"]))


async def api_label(request: Request):
    sid = int(request.path_params["sid"])
    try:
        spool = await S.get_spool(sid)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    img = render_label(spool, qr_mode=QR_MODE, public_url=PUBLIC_SPOOLMAN_URL, height=LABEL_HEIGHT_PX)
    buf = io.BytesIO()
    img.convert("L").save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


async def api_print(request: Request):
    sid = int(request.path_params["sid"])
    try:
        spool = await S.get_spool(sid)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse(S.add_job(spool, "manual").public())


async def api_cancel(request: Request):
    j = S.jobs.get(request.path_params["jid"])
    if not j:
        return JSONResponse({"error": "no such job"}, status_code=404)
    if j.status in ("queued", "printing"):
        j.status, j.finished = "cancelled", time.time()
        if j.source == "checkbox":
            await S.clear_flag(j.spool_id)  # otherwise the next poll would requeue it
    return JSONResponse(j.public())


async def api_retry(request: Request):
    j = S.jobs.get(request.path_params["jid"])
    if not j:
        return JSONResponse({"error": "no such job"}, status_code=404)
    if j.status in ("failed", "cancelled", "done"):
        j.status, j.not_before, j.error, j.attempts, j.agent = "queued", 0, "", 0, ""
        S.notify()
    return JSONResponse(j.public())


def _touch_agent(request: Request) -> str:
    q = request.query_params
    name = (q.get("agent") or "agent")[:40]
    a = S.agents.setdefault(name, {"kind": q.get("kind", "unknown"), "printed": 0})
    a.update(last_seen=time.time(), kind=q.get("kind", a["kind"]), printer=q.get("printer", ""),
             client=request.client.host if request.client else "")
    return name


async def agent_next(request: Request):
    name = _touch_agent(request)
    if request.query_params.get("peek") == "1":  # heartbeat only; agent can't reach its printer
        return Response(status_code=204)
    wait = min(float(request.query_params.get("wait", 20)), 30)
    deadline = time.monotonic() + wait
    while True:
        if await request.is_disconnected():  # tab closed / ESP reset mid-poll: don't hand it a job
            return Response(status_code=204)
        job = S.claim(name)
        if job:
            log.info("Job %s (spool #%s) -> %s, attempt %s", job.id, job.spool_id, name, job.attempts)
            return Response(job.data, media_type="application/octet-stream", headers={
                "X-Job-Id": job.id, "X-Spool-Id": str(job.spool_id), "Cache-Control": "no-store"})
        left = deadline - time.monotonic()
        if left <= 0:
            return Response(status_code=204)
        await S.wait_change(min(left, 1.0))
        S.agents[name]["last_seen"] = time.time()


async def agent_done(request: Request):
    name = _touch_agent(request)
    q = request.query_params
    j = S.jobs.get(q.get("job", ""))
    if not j:
        return JSONResponse({"error": "no such job"}, status_code=404)
    if j.status != "printing" or j.agent != name:
        return JSONResponse({"ignored": True, "status": j.status})
    if q.get("ok") == "1":
        j.status, j.finished, j.error = "done", time.time(), ""
        S.agents[name]["printed"] = S.agents[name].get("printed", 0) + 1
        log.info("Job %s printed by %s", j.id, name)
        if j.source == "checkbox":
            await S.clear_flag(j.spool_id)
    else:
        j.error = (q.get("error") or "agent reported failure")[:200]
        j.agent = ""
        if j.source == "manual" and j.attempts >= MAX_MANUAL_ATTEMPTS:
            j.status, j.finished = "failed", time.time()
        else:
            backoff = min(RETRY_SECONDS * 2 ** (j.attempts - 1), MAX_RETRY_SECONDS)
            j.status, j.not_before = "queued", time.time() + backoff
        log.warning("Job %s failed on %s: %s", j.id, name, j.error)
    S.notify()
    return JSONResponse(j.public())


async def healthz(_):
    return JSONResponse({"ok": True, "spoolman": S.spoolman_ok})


app = Starlette(lifespan=lifespan, routes=[
    Route("/", index),
    Route("/healthz", healthz),
    Route("/api/state", api_state),
    Route("/api/spools", api_spools),
    Route("/api/label/{sid:int}.png", api_label),
    Route("/api/print/{sid:int}", api_print, methods=["POST"]),
    Route("/api/jobs/{jid}/cancel", api_cancel, methods=["POST"]),
    Route("/api/jobs/{jid}/retry", api_retry, methods=["POST"]),
    Route("/api/agent/next", agent_next),
    Route("/api/agent/done", agent_done, methods=["POST"]),
])
