import asyncio
from types import SimpleNamespace

import pytest

from retail import behavior


@pytest.mark.parametrize('stall', [0, .08])
def test_pointer_accounts_for_transport_time_without_replaying_frames(monkeypatch, stall):
    async def scenario():
        clock = [0.0]
        points, starts = [], []

        async def sleep(delay):
            clock[0] += delay

        async def move(x, y):
            starts.append(clock[0])
            points.append((x, y))
            clock[0] += stall if len(points) == 5 else .006

        monkeypatch.setattr(behavior, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(behavior.asyncio, 'sleep', sleep)
        page = SimpleNamespace(context=SimpleNamespace(), viewport_size={'width':1000,'height':700},
                               mouse=SimpleNamespace(move=move))
        controller = behavior.PacedInput(page, seed=40)
        await controller.move(800, 500)
        assert controller.position == (800, 500)
        assert all(b[0] >= a[0] and b[1] >= a[1] for a,b in zip(points,points[1:]))
        # Each dispatch uses a new elapsed-time sample; no queued old frames.
        assert all(b-a >= behavior.POINTER_FRAME_SECONDS - 1e-8 for a,b in zip(starts,starts[1:-1]))
        assert starts[5]-starts[4] >= stall
        assert len(points) > 50
        assert clock[0] < 1.1 + .02  # Transport overhead no longer accumulates per point.
        # Smooth acceleration/deceleration on both axes.
        distances = [((b[0]-a[0])**2+(b[1]-a[1])**2)**.5 for a,b in zip(points,points[1:])]
        assert distances[0] < max(distances) / 10
        assert distances[-1] < max(distances) / 10

    asyncio.run(scenario())
