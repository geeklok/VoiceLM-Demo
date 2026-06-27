from __future__ import annotations

import asyncio

import pytest

from app.orchestration.limiter import ConcurrencyLimitError, GpuLimiter


def test_slot_acquire_and_release():
    async def run():
        limiter = GpuLimiter(asr_concurrency=1)
        async with limiter.asr_slot():
            pass
        # 释放后可再次获取
        async with limiter.asr_slot():
            pass

    asyncio.run(run())


def test_reject_when_full_and_timeout():
    async def run():
        limiter = GpuLimiter(asr_concurrency=1, acquire_timeout=0.05, retry_after=3)
        started = asyncio.Event()
        release = asyncio.Event()

        async def hold():
            async with limiter.asr_slot():
                started.set()
                await release.wait()

        holder = asyncio.create_task(hold())
        await started.wait()

        with pytest.raises(ConcurrencyLimitError) as ei:
            async with limiter.asr_slot():
                pass
        assert ei.value.retry_after == 3

        release.set()
        await holder

    asyncio.run(run())


def test_queued_request_proceeds_after_release():
    async def run():
        limiter = GpuLimiter(asr_concurrency=1, acquire_timeout=5.0)
        started = asyncio.Event()
        release = asyncio.Event()
        got = asyncio.Event()

        async def hold():
            async with limiter.asr_slot():
                started.set()
                await release.wait()

        async def waiter():
            async with limiter.asr_slot():
                got.set()

        holder = asyncio.create_task(hold())
        await started.wait()
        w = asyncio.create_task(waiter())
        # 在持有者释放前, 排队者拿不到许可
        await asyncio.sleep(0.05)
        assert not got.is_set()
        release.set()
        await asyncio.wait_for(w, timeout=1.0)
        assert got.is_set()
        await holder

    asyncio.run(run())


def test_asr_and_tts_pools_are_independent():
    async def run():
        limiter = GpuLimiter(asr_concurrency=1, tts_concurrency=1, acquire_timeout=0.05)
        started = asyncio.Event()
        release = asyncio.Event()

        async def hold_asr():
            async with limiter.asr_slot():
                started.set()
                await release.wait()

        holder = asyncio.create_task(hold_asr())
        await started.wait()
        # ASR 池满, 但 TTS 池独立, 应可正常获取
        async with limiter.tts_slot():
            pass
        release.set()
        await holder

    asyncio.run(run())
