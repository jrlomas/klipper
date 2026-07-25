from helix.module import module, on_start
from helix.types import state, u8, u32


@state
class CounterState:
    boots: u32
    active_lane: u8


@module(name="counter", api="0.1")
class Counter:
    state: CounterState

    @on_start
    def start(self, ctx):
        self.state.boots = self.state.boots + u32(1)
        self.state.active_lane = u8(3)
