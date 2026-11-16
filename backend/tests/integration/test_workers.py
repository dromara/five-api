"""Real Uvicorn workers sharing MySQL and Redis: queued charges written once across workers."""
import asyncio
import os
import socket
import sys
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request

from app.models import APIKey, Channel, ModelPrice, RequestLog, Settlement, Tenant
from app.services.auth import hash_api_key
from tests.integration.conftest import serve

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
ROOT = Path(__file__).resolve().parents[2]
MODEL = "worker-model"
STALE_SECONDS = 3


@asynccontextmanager
async def workers(tmp_path, count=2):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    env = {**os.environ, "MYSQL_DATABASE": os.environ["FIVE_TEST_MYSQL_DB"],
           "REDIS_URL": os.environ["FIVE_TEST_REDIS_URL"], "SECRETS_FILE": str(tmp_path / "secrets.json"),
           "WORKERS": str(count), "PROCESS_STALE_SECONDS": str(STALE_SECONDS)}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
        "--workers", str(count), "--no-access-log", "--log-level", "warning", "--timeout-graceful-shutdown", "3",
        cwd=ROOT, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            for _ in range(150):
                try:
                    if (await client.get(base + "/healthz")).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Workers never became ready")
        yield base
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()


def upstream():
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        prompt = (await request.json())["messages"][0]["content"]
        await asyncio.sleep({"hang": 60, "slow": STALE_SECONDS * 2 + 2}.get(prompt, 0.2))
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 10}}
    return app


async def ready(origin, quota, key_concurrency=-1):
    tenant = await Tenant.create(name="workers", quota_total=Decimal(quota), rpm_limit=-1, tpm_limit=-1,
                                 concurrent_limit=-1)
    raw = "sk-worker-integration-client"
    await APIKey.create(tenant_id=tenant.id, name="workers", key_hash=hash_api_key(raw), key_prefix=raw[:8],
                        quota_total=-1, concurrent_limit=key_concurrency)
    await Channel.create(name="workers", provider="openai", base_url=origin, models=[MODEL], api_key="",
                         max_retries=0, timeout=90, priority=10)
    # Output-only pricing makes every request cost exactly 0.1.
    await ModelPrice.create(model=MODEL, prompt_price=0, completion_price=10000, cached_price=0, cache_write_price=0)
    return tenant, {"Authorization": "Bearer " + raw}


def body(prompt="hi"):
    return {"model": MODEL, "max_tokens": 10, "messages": [{"role": "user", "content": prompt}]}


async def test_two_workers_charge_every_admitted_request_exactly_once(tmp_path):
    async with serve(upstream()) as origin, workers(tmp_path) as base:
        tenant, auth = await ready(origin, quota="3")
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            responses = await asyncio.gather(*(client.post(base + "/v1/chat/completions", json=body(), headers=auth)
                                               for _ in range(60)))
            # Concurrent admission may pass the balance; every admitted request is still charged once.
            succeeded = sum(response.status_code == 200 for response in responses)
            assert succeeded >= 30
            assert {response.status_code for response in responses} <= {200, 429}
            async with asyncio.timeout(10):
                while await RequestLog.filter(tenant_id=tenant.id, status_code=200).count() < succeeded:
                    await asyncio.sleep(0.1)
        tenant = await Tenant.get(id=tenant.id)
        logged = await RequestLog.filter(tenant_id=tenant.id, status_code=200).values_list("cost", flat=True)
        assert len(logged) == succeeded and sum(logged) == tenant.quota_used == Decimal("0.1") * succeeded
        assert not await Settlement.exists()


async def test_key_concurrency_limit_is_shared_by_all_workers(tmp_path):
    async with serve(upstream()) as origin, workers(tmp_path) as base:
        _, auth = await ready(origin, quota="10", key_concurrency=3)
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            responses = await asyncio.gather(*(client.post(base + "/v1/chat/completions", json=body(), headers=auth)
                                               for _ in range(20)))
        assert 1 <= sum(response.status_code == 200 for response in responses) <= 3
        assert {response.status_code for response in responses} == {200, 429}


async def test_long_request_of_a_live_worker_is_charged_once(tmp_path):
    async with serve(upstream()) as origin, workers(tmp_path) as base:
        tenant, auth = await ready(origin, quota="10")
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            assert (await client.post(base + "/v1/chat/completions", json=body("slow"), headers=auth)).status_code == 200
        async with asyncio.timeout(10):
            while not await RequestLog.filter(tenant_id=tenant.id).exists():
                await asyncio.sleep(0.1)
        assert (await Tenant.get(id=tenant.id)).quota_used == Decimal("0.1")
        assert not await Settlement.exists()
