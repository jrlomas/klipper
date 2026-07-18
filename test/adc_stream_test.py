import pathlib
import sys
import threading


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from klippy.extras import adc_stream as MODULE


class FakeMCU:
    def __init__(self):
        self.fileoutput = False

    def clock32_to_clock64(self, clock):
        return clock

    def clock_to_print_time(self, clock):
        return clock / 1_000_000.

    def seconds_to_clock(self, seconds):
        return int(seconds * 1_000_000.)

    def is_fileoutput(self):
        return self.fileoutput


class FakeCommand:
    def __init__(self):
        self.messages = []

    def send(self, args):
        self.messages.append(args)


def make_stream(max_pending=16):
    stream = MODULE.ADCStream.__new__(MODULE.ADCStream)
    stream.name = "test"
    stream.pins = ["PA0", "PA1"]
    stream.channel_names = ["pressure", "current"]
    stream.mcu = FakeMCU()
    stream.adc_max = 4095.
    stream.sample_rate = 1000.
    stream.lock = threading.Lock()
    stream.pending = []
    stream.pending_summaries = []
    stream.last_values = [None, None]
    stream.max_pending = max_pending
    stream.epoch = stream.last_sequence = None
    stream.sequence_gaps = stream.host_drops = stream.mcu_drops = 0
    stream.ready_highwater = stream.dma_errors = stream.adc_errors = 0
    stream.overruns = stream.telemetry_drops = stream.watchdog_events = 0
    stream.safety_events = 0
    stream.last_safety = None
    stream.oid = 9
    stream.ack_cmd = FakeCommand()
    stream.summary_sequences = [None, None]
    stream.summary_epochs = [None, None]
    stream.summary_gaps = 0
    stream.oversamples = [4, 2]
    stream.filter_shifts = [2, 1]
    stream.capabilities = {}
    stream.last_status = stream.last_uncertainty = 0
    stream.state = "armed"
    return stream


def message(sequence, values, first_clock=1_000_000, period_num=1000):
    payload = b"".join(int(value).to_bytes(2, "little") for value in values)
    return {
        "channels": 2, "values": payload, "epoch": 7,
        "sequence": sequence, "first_clock": first_clock,
        "period_num": period_num, "period_den": 1,
        "uncertainty": 5, "status": 0,
    }


def test_interleaved_scans_get_scan_period_timestamps():
    stream = make_stream()
    stream._handle_data(message(0, [0, 4095, 2048, 1024]))
    assert len(stream.pending) == 2
    assert stream.pending[0] == [1.0, 0.0, 1.0]
    assert stream.pending[1][0] == 1.001
    assert stream.last_values == [2048 / 4095., 1024 / 4095.]
    assert stream.last_uncertainty == 0.000005


def test_sequence_gaps_and_host_queue_drops_are_explicit():
    stream = make_stream(max_pending=3)
    stream._handle_data(message(2, [1, 2, 3, 4]))
    stream._handle_data(message(5, [5, 6, 7, 8]))
    assert stream.sequence_gaps == 2
    assert stream.host_drops == 1
    assert len(stream.pending) == 3


def test_summary_decode_uses_filter_scale_and_tracks_gaps():
    stream = make_stream()
    params = {
        "sub": 0, "sequence": 2, "epoch": 8, "first_clock": 1000,
        "last_clock": 2000, "uncertainty": 2, "status": 0,
        "count": 2, "min": 1024, "max": 3072,
        "sum_lo": 4096, "sum_hi": 0, "shift": 2,
    }
    stream._handle_summary(params)
    assert abs(stream.last_values[0] - 2048 / 4095.) < 1.e-12
    params["sequence"] = 5
    stream._handle_summary(params)
    assert stream.summary_gaps == 2
    params["epoch"] = 9
    params["sequence"] = 0
    stream._handle_summary(params)
    assert stream.summary_gaps == 2
    assert stream.pending_summaries[-1]["channel"] == "pressure"


def test_capability_contract_is_exposed():
    stream = make_stream()
    stream._handle_capabilities({
        "version": 1, "max_channels": 4, "max_subscriptions": 8,
        "max_osr": 256, "caps": 31, "dma_pool": 512,
        "dma_used": 64, "dma_claims": 3,
    })
    assert stream.capabilities["version"] == 1
    assert stream.capabilities["caps"] == 31
    assert stream.capabilities["dma_used"] == 64


def test_status_exposes_ring_and_error_counters():
    stream = make_stream()
    stream._handle_status({
        "state": 2, "dropped": 3, "status": 0x12, "epoch": 4,
        "sequence": 99, "ready_highwater": 2, "dma_errors": 5,
        "adc_errors": 6, "overruns": 7, "telemetry_drops": 8,
        "watchdog_events": 9,
    })
    status = stream.get_status(0.)
    assert status["state"] == "running"
    assert status["ready_highwater"] == 2
    assert status["dma_errors"] == 5
    assert status["watchdog_events"] == 9


def test_scheduled_summary_is_stored_then_acknowledged():
    stream = make_stream()
    params = {
        "sub": 1, "sequence": 4, "epoch": 2, "first_clock": 1000,
        "last_clock": 2000, "uncertainty": 2, "status": 0,
        "count": 1, "min": 1024, "max": 1024,
        "sum_lo": 1024, "sum_hi": 0, "shift": 1, "deadline": 2200,
    }
    stream._handle_scheduled(params)
    assert stream.pending_summaries[-1]["sequence"] == 4
    assert stream.ack_cmd.messages == [[9, 1, 4]]


def test_safety_event_is_observable():
    stream = make_stream()
    stream._handle_safety({
        "sub": 0, "event": 2, "action": 1, "clock": 99,
        "status": 0x80, "count": 3,
    })
    assert stream.safety_events == 3
    assert stream.last_safety["event"] == 2
    assert stream.last_status == 0x80


if __name__ == "__main__":
    test_interleaved_scans_get_scan_period_timestamps()
    test_sequence_gaps_and_host_queue_drops_are_explicit()
    test_summary_decode_uses_filter_scale_and_tracks_gaps()
    test_capability_contract_is_exposed()
    test_status_exposes_ring_and_error_counters()
    test_scheduled_summary_is_stored_then_acknowledged()
    test_safety_event_is_observable()
    print("PASS: ADC stream host decode, timing, gaps, and bounded drops")
