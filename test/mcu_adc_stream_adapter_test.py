#!/usr/bin/env python3
import pathlib
import sys
import types


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))
for dependency in ("serialhdl", "msgproto", "pins", "chelper", "clocksync"):
    sys.modules[dependency] = types.ModuleType(dependency)
import mcu as MODULE
from extras import adc_scaled


SUBSCRIBE_FORMAT = (
    "adc_stream_subscribe oid=%c sub=%c channel=%c input_div=%hu"
    " osr=%hu shift=%c report_div=%hu report_class=%c")


class FakePrinter:
    def config_error(self, message):
        return RuntimeError(message)


class FakeMCU:
    def __init__(self, stream=True, mode="off", max_scan_ticks=0,
                 channel_order=None, hardware_oversample=1):
        self.callbacks = []
        self.commands = []
        self.responses = []
        self.next_oid = 0
        self.constants = {"ADC_MAX": 4095}
        if channel_order is None:
            channel_order = {
                "PA2": 2, "PA3": 3,
                "gpio26": 0, "gpio27": 1,
                "ADC_TEMPERATURE": 255,
            }
        self.enumerations = {"adc_stream_channel": channel_order}
        self._adc_stream_mode = mode
        self._adc_stream_hardware_oversample = hardware_oversample
        if stream:
            self.constants.update({
                "ADC_STREAM_V1": 1, "ADC_STREAM_MAX_CHANNELS": 4,
                "ADC_STREAM_MAX_SUBSCRIPTIONS": 8,
                "ADC_STREAM_MAX_OSR": 256,
                "ADC_STREAM_MAX_BLOCK_VALUES": 64,
            })
            if max_scan_ticks:
                self.constants["ADC_STREAM_MAX_SCAN_TICKS_PER_CHANNEL"] = (
                    max_scan_ticks)
    def register_config_callback(self, callback):
        self.callbacks.append(callback)
    def add_config_cmd(self, command, is_init=False):
        self.commands.append((command, is_init))
    def create_oid(self):
        oid, self.next_oid = self.next_oid, self.next_oid + 1
        return oid
    def get_constants(self):
        return self.constants
    def get_enumerations(self):
        return self.enumerations
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
    assert any("input_div=1 osr=5 shift=0 report_div=1" in command
               for command in commands)
    assert "adc_stream_set_options oid=0 raw_output=0" in commands
    assert any("block_values=5 traffic_class=2" in command
               for command in commands)
    assert not any(command.startswith("config_analog_in")
                   for command in commands)

    manager = fake._helix_adc_stream_manager
    manager._handle_summary({
        "sub": 0, "count": 1, "sum_lo": 5 * 2048,
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
    # The legacy 8x1ms/300ms heater burst and 1x1ms/100ms observer become
    # evenly distributed 37.5ms and 100ms samples on a common 12.5ms scan.
    # Their report cycles both end on the selected eight-scan block boundary.
    assert any("sub=0 channel=0 input_div=3 osr=8 shift=0 report_div=1"
               in command for command in commands)
    assert any("sub=1 channel=1 input_div=8 osr=1 shift=0 report_div=1"
               in command for command in commands)
    assert any("period_ticks=12500 block_values=16 traffic_class=0"
               in command for command in commands)
    assert sum("summary_mode=1" in command for command in commands) == 2


def test_backend_period_limit_uses_exact_input_decimation():
    # A 5-sample/100ms consumer asks for a 20ms scan, but this synthetic
    # backend can pace at most 3ms.  The adapter chooses the largest exact
    # divisor (2.5ms) and decimates by eight before each 5x OSR report.  The
    # requested 100ms report interval remains exact.
    fake = FakeMCU(mode="force", max_scan_ticks=3000)
    adc = MODULE.MCU_adc(fake, {"pin": "gpio27"})
    adc.setup_adc_sample(.100, .005, 5)
    finalize(fake)
    commands = [command for command, _ in fake.commands]
    assert any("input_div=8 osr=5 shift=0 report_div=1" in command
               for command in commands)
    assert any("period_ticks=2500 block_values=40 traffic_class=2"
               in command for command in commands)


def test_auto_mode_sorts_rp2040_consumers_by_physical_channel():
    fake = FakeMCU(mode="auto")
    callbacks = {}
    for pin in ("gpio27", "ADC_TEMPERATURE", "gpio26"):
        adc = MODULE.MCU_adc(fake, {"pin": pin})
        adc.setup_adc_sample(.300, .001, 8)
        callbacks[pin] = []
        adc.setup_adc_callback(callbacks[pin].extend)
    finalize(fake)
    commands = [command for command, _ in fake.commands]
    channels = [command for command in commands
                if command.startswith("adc_stream_add_channel")]
    assert channels == [
        "adc_stream_add_channel oid=0 pin=gpio26",
        "adc_stream_add_channel oid=0 pin=gpio27",
        "adc_stream_add_channel oid=0 pin=ADC_TEMPERATURE",
    ]
    # Subscription zero now belongs to gpio26, not to the first constructed
    # consumer.  This proves reports retain their logical sensor identity.
    fake._helix_adc_stream_manager._handle_summary({
        "sub": 0, "count": 1, "sum_lo": 8 * 2048,
        "sum_hi": 0, "last_clock": 200000, "status": 0,
    })
    assert len(callbacks["gpio26"]) == 1
    assert callbacks["gpio27"] == []
    assert callbacks["ADC_TEMPERATURE"] == []


def test_old_firmware_without_order_metadata_falls_back_safely():
    fake = FakeMCU(mode="auto", channel_order={})
    for pin in ("gpio27", "gpio26"):
        adc = MODULE.MCU_adc(fake, {"pin": pin})
        adc.setup_adc_sample(.300, .001, 8)
    finalize(fake)
    commands = [command for command, _ in fake.commands]
    assert not any(command.startswith("config_adc_stream")
                   for command in commands)
    assert sum(command.startswith("config_analog_in")
               for command in commands) == 2


def test_auto_mode_configures_hardware_oversampling_at_native_scale():
    fake = FakeMCU(mode="force", hardware_oversample=16)
    fake.constants["ADC_STREAM_CAPS"] = 1 << 8
    adc = MODULE.MCU_adc(fake, {"pin": "PA3"})
    adc.setup_adc_sample(.300, .001, 8)
    finalize(fake)
    commands = [command for command, _ in fake.commands]
    assert ("adc_stream_set_hardware_oversample oid=0 ratio=16 shift=4"
            in commands)
    assert any("osr=8 shift=0" in command for command in commands)


def test_per_consumer_firmware_window_and_alpha():
    fake = FakeMCU(mode="force", channel_order={
        "PA3": 3, "ADC_TEMPERATURE": 12})
    external = MODULE.MCU_adc(fake, {"pin": "PA3"})
    external.setup_adc_stream_filter(4, .25)
    external.setup_adc_sample(.300, .001, 8)
    internal = MODULE.MCU_adc(fake, {"pin": "ADC_TEMPERATURE"})
    internal.setup_adc_stream_filter(1, 1.)
    internal.setup_adc_sample(.300, .001, 8)
    finalize(fake)
    commands = [command for command, _ in fake.commands]
    assert any("sub=0 channel=0 input_div=1 osr=4" in command
               for command in commands)
    assert any("sub=1 channel=1 input_div=4 osr=1" in command
               for command in commands)
    assert ("adc_stream_set_subscription_filter oid=0 sub=0"
            " window_divisor=4 alpha_q15=8192" in commands)
    assert ("adc_stream_set_subscription_filter oid=0 sub=1"
            " window_divisor=1 alpha_q15=32768" in commands)
    assert any("period_ticks=75000 block_values=8" in command
               for command in commands)
    received = []
    external.setup_adc_callback(received.extend)
    fake._helix_adc_stream_manager._handle_summary({
        "sub": 0, "count": 1, "sum_lo": 2048, "sum_hi": 0,
        "last_clock": 300000, "status": 0,
    })
    assert received == [(.3, 2048 / 4095.)]


def test_scaled_adc_preserves_stream_configuration_surface():
    class PhysicalADC:
        def __init__(self):
            self.calls = []
            self.callback = None
        def setup_adc_sample(self, *args, **kwargs):
            self.calls.append(('sample', args, kwargs))
        def setup_adc_stream(self, *args, **kwargs):
            self.calls.append(('stream', args, kwargs))
        def setup_adc_stream_filter(self, *args, **kwargs):
            self.calls.append(('filter', args, kwargs))
        def setup_adc_callback(self, callback):
            self.callback = callback
        def get_mcu(self):
            return object()
    class PinMCU:
        def __init__(self, physical):
            self.physical = physical
        def setup_pin(self, pin_type, pin_params):
            assert pin_type == 'adc'
            return self.physical
    class QueryADC:
        def register_adc(self, name, adc):
            pass
    class Printer:
        def lookup_object(self, name):
            assert name == 'query_adc'
            return QueryADC()
    class Main:
        def __init__(self, physical):
            self.mcu = PinMCU(physical)
            self.printer = Printer()
            self.name = 'scaled'
            self.last_vref = (0., 1.)
            self.last_vssa = (0., 0.)

    physical = PhysicalADC()
    scaled = adc_scaled.MCU_scaled_adc(Main(physical), {'pin': 'PA2'})
    scaled.setup_adc_stream(report_class=1)
    scaled.setup_adc_stream_filter(4, .25)
    assert physical.calls == [
        ('stream', (), {'report_class': 1}),
        ('filter', (4, .25), {}),
    ]
    received = []
    scaled.setup_adc_callback(received.extend)
    physical.callback([(1., .75)])
    assert received == [(1., .75)]


if __name__ == "__main__":
    test_opted_consumer_uses_one_filtered_dma_subscription()
    test_unsupported_firmware_falls_back_once_to_legacy_adc()
    test_unmigrated_consumer_prevents_split_adc_ownership()
    test_auto_mode_migrates_heater_thresholds_to_local_shutdown()
    test_backend_period_limit_uses_exact_input_decimation()
    test_auto_mode_sorts_rp2040_consumers_by_physical_channel()
    test_old_firmware_without_order_metadata_falls_back_safely()
    test_auto_mode_configures_hardware_oversampling_at_native_scale()
    test_per_consumer_firmware_window_and_alpha()
    test_scaled_adc_preserves_stream_configuration_surface()
    print("PASS: MCU_adc merged DMA adapter and legacy fallback")
