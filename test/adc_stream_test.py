import pathlib
import sys
import threading


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from klippy.extras import adc_stream as MODULE


class FakeMCU:
    def clock32_to_clock64(self, clock):
        return clock

    def clock_to_print_time(self, clock):
        return clock / 1_000_000.

    def seconds_to_clock(self, seconds):
        return int(seconds * 1_000_000.)


def make_stream(max_pending=16):
    stream = MODULE.ADCStream.__new__(MODULE.ADCStream)
    stream.name = "test"
    stream.pins = ["PA0", "PA1"]
    stream.channel_names = ["pressure", "current"]
    stream.mcu = FakeMCU()
    stream.adc_max = 4095.
    stream.lock = threading.Lock()
    stream.pending = []
    stream.last_values = [None, None]
    stream.max_pending = max_pending
    stream.epoch = stream.last_sequence = None
    stream.sequence_gaps = stream.host_drops = stream.mcu_drops = 0
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


if __name__ == "__main__":
    test_interleaved_scans_get_scan_period_timestamps()
    test_sequence_gaps_and_host_queue_drops_are_explicit()
    print("PASS: ADC stream host decode, timing, gaps, and bounded drops")
