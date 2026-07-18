#!/usr/bin/env python3
import pathlib
import sys
import types


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))
for dependency in ("serialhdl", "msgproto", "pins", "chelper", "clocksync"):
    sys.modules[dependency] = types.ModuleType(dependency)
import mcu as MODULE


SUBSCRIBE_FORMAT = (
    "adc_stream_subscribe oid=%c sub=%c channel=%c input_div=%hu"
    " osr=%hu shift=%c report_div=%hu report_class=%c")


class FakePrinter:
    def config_error(self, message):
        return RuntimeError(message)


class FakeMCU:
    def __init__(self, stream=True, mode="off"):
        self.callbacks = []
        self.commands = []
        self.responses = []
        self.next_oid = 0
        self.constants = {"ADC_MAX": 4095}
        self._adc_stream_mode = mode
        if stream:
            self.constants.update({
                "ADC_STREAM_V1": 1, "ADC_STREAM_MAX_CHANNELS": 4,
                "ADC_STREAM_MAX_SUBSCRIPTIONS": 8,
                "ADC_STREAM_MAX_OSR": 256,
            })
    def register_config_callback(self, callback):
        self.callbacks.append(callback)
    def add_config_cmd(self, command, is_init=False):
        self.commands.append((command, is_init))
    def create_oid(self):
        oid, self.next_oid = self.next_oid, self.next_oid + 1
        return oid
    def get_constants(self):
        return self.constants
    def get_constant_float(self, name):
        return float(self.constants[name])
    def try_lookup_command(self, fmt):
        if fmt == SUBSCRIBE_FORMAT:
            return object() if "ADC_STREAM_V1" in self.constants else None
        return object()
    def get_query_slot(self, oid):
        return 1000 + oid
    def seconds_to_clock(self, seconds):
        return int(seconds * 1_000_000.)
    def register_serial_response(self, callback, fmt, oid=None):
        self.responses.append((callback, fmt, oid))
    def get_name(self):
        return "test"
    def get_printer(self):
        return FakePrinter()
    def clock32_to_clock64(self, clock):
        return clock
    def clock_to_print_time(self, clock):
        return clock / 1_000_000.


def finalize(fake):
    for callback in list(fake.callbacks):
        callback()


def test_opted_consumer_uses_one_filtered_dma_subscription():
    fake = FakeMCU()
    adc = MODULE.MCU_adc(fake, {"pin": "PA2"})
    adc.setup_adc_sample(.100, .005, 5)
    received = []
    adc.setup_adc_callback(received.extend)
    adc.setup_adc_stream(report_class=1)
    finalize(fake)
    commands = [command for command, _ in fake.commands]
    assert commands[0] == "config_adc_stream oid=0"
    assert "adc_stream_add_channel oid=0 pin=PA2" in commands
    assert any("input_div=1 osr=5 shift=0 report_div=4" in command
               for command in commands)
    assert "adc_stream_set_options oid=0 raw_output=0" in commands
    assert any("block_values=10 traffic_class=2" in command
               for command in commands)
    assert not any(command.startswith("config_analog_in")
                   for command in commands)

    manager = fake._helix_adc_stream_manager
    manager._handle_summary({
        "sub": 0, "count": 4, "sum_lo": 5 * 4 * 2048,
        "sum_hi": 0, "last_clock": 200000, "status": 0,
    })
    assert received == [(.2, 2048 / 4095.)]
    assert adc.get_last_value() == (.2, 2048 / 4095.)


def test_unsupported_firmware_falls_back_once_to_legacy_adc():
    fake = FakeMCU(stream=False)
    adc = MODULE.MCU_adc(fake, {"pin": "PA2"})
    adc.setup_adc_sample(.100, .005, 5)
    adc.setup_adc_stream()
    finalize(fake)
    commands = [command for command, _ in fake.commands]
    assert commands.count("config_analog_in oid=0 pin=PA2") == 1
    assert not any(command.startswith("config_adc_stream")
                   for command in commands)


def test_unmigrated_consumer_prevents_split_adc_ownership():
    fake = FakeMCU()
    pressure = MODULE.MCU_adc(fake, {"pin": "PA2"})
    pressure.setup_adc_sample(.100, .005, 5)
    pressure.setup_adc_stream()
    safety = MODULE.MCU_adc(fake, {"pin": "PA3"})
    safety.setup_adc_sample(.300, .001, 8)
    finalize(fake)
    commands = [command for command, _ in fake.commands]
    assert not any(command.startswith("config_adc_stream")
                   for command in commands)
    assert sum(command.startswith("config_analog_in")
               for command in commands) == 2


def test_auto_mode_migrates_heater_thresholds_to_local_shutdown():
    fake = FakeMCU(mode="auto")
    heater = MODULE.MCU_adc(fake, {"pin": "PA2"})
    heater.setup_adc_sample(.300, .001, 8, minval=.1, maxval=.9,
                            range_check_count=4)
    status = MODULE.MCU_adc(fake, {"pin": "PA3"})
    status.setup_adc_sample(.100, .001, 1)
    finalize(fake)
    commands = [command for command, _ in fake.commands]
    assert not any(command.startswith("config_analog_in")
                   for command in commands)
    assert any("sub=0 deadline_ticks=0 fail_action=3" in command
               and "fault_count=4" in command for command in commands)
    assert any("sub=1 deadline_ticks=0 fail_action=0" in command
               for command in commands)
    assert any("traffic_class=0" in command for command in commands)


if __name__ == "__main__":
    test_opted_consumer_uses_one_filtered_dma_subscription()
    test_unsupported_firmware_falls_back_once_to_legacy_adc()
    test_unmigrated_consumer_prevents_split_adc_ownership()
    test_auto_mode_migrates_heater_thresholds_to_local_shutdown()
    print("PASS: MCU_adc merged DMA adapter and legacy fallback")
