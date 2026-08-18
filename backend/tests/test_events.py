import asyncio

import pytest
from pixel_relay.events import EventBroker


@pytest.mark.asyncio
async def test_shutdown_wakes_and_terminates_subscribers() -> None:
    events = EventBroker()
    subscription = events.subscribe()
    waiting = asyncio.create_task(anext(subscription))
    await asyncio.sleep(0)

    shutdown = events.request_shutdown()

    assert await asyncio.wait_for(waiting, timeout=1) == shutdown
    assert shutdown.kind == "server"
    assert shutdown.data == {"action": "shutdown"}
    with pytest.raises(StopAsyncIteration):
        await anext(subscription)


def test_shutdown_request_is_idempotent() -> None:
    events = EventBroker()

    first = events.request_shutdown()
    second = events.request_shutdown()

    assert first is second
    assert events.shutdown_requested is True
