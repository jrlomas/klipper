#!/usr/bin/env python3
"""Source-contract regression for STM32 USB-SOF discard windows."""

import os
import re


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    with open(os.path.join(ROOT, path), encoding='utf-8') as source:
        return source.read()


def _function(source, name):
    match = re.search(
        r'\n%s\(.*?\n\{(?P<body>.*?)\n\}' % re.escape(name),
        source, re.S)
    assert match is not None, name
    return match.group('body')


def test_arm_irq_attributes_guard_before_restoring_primask():
    source = _read('src/generic/armcm_irq.c')
    disable = _function(source, 'irq_disable')
    enable = _function(source, 'irq_enable')
    save = _function(source, 'irq_save')
    restore = _function(source, 'irq_restore')
    wait = _function(source, 'irq_wait')

    assert disable.index('irq_timing_guard_probe()') < disable.index(
        'cpsid i')
    assert disable.index('cpsid i') < disable.index(
        'irq_timing_guard_begin')
    assert save.index('irq_timing_guard_probe()') < save.index('cpsid i')
    assert enable.index('irq_timing_guard_end') < enable.index(
        'cpsie i')
    assert restore.index('irq_timing_guard_end') < restore.index(
        'msr primask')
    assert wait.index('irq_timing_guard_end') < wait.index('cpsie i')
    assert wait.index('cpsid i') < wait.rindex('irq_timing_guard_begin')
    assert 'irq_timing_site()' in disable
    assert 'irq_timing_site()' in save
    assert 'mov %0, pc' in source
    print("PASS: ARM PRIMASK guards retain entry/exit caller attribution")


def test_stm32_guard_discards_only_sof():
    source = _read('src/stm32/usbfs.c')
    generic = _read('src/generic/usb_sof.c')
    mask = _function(source, 'usb_irq_mask')
    begin = _function(source, 'usb_sof_board_guard_begin')
    discard = _function(source, 'usb_sof_board_guard_end')
    query = _function(generic, 'command_usb_sof_query')
    guard_query = _function(generic, 'command_usb_sof_guard_query')

    assert 'USB_CNTR_CTRM | USB_CNTR_RESETM' in mask
    assert 'usb_sof_guard_enabled ? USB_CNTR_SOFM : 0' in mask
    assert 'USB->ISTR & USB_ISTR_SOF' in discard
    assert 'USB_SOF_GUARD_ENTRY_PRE_VALID' in begin
    assert 'USB_SOF_GUARD_ENTRY_PRE_PENDING' in begin
    assert 'USB_SOF_GUARD_ENTRY_POST_PENDING' in begin
    assert 'timer_read_time() - usb_sof_guard.start_clock' in discard
    assert 'mrs %0, primask' in discard
    assert 'USB->FNR & USB_FNR_FN' in discard
    assert '~USB_ISTR_SOF' in discard
    assert discard.index('~USB_ISTR_SOF') < discard.index(
        'usb_sof_note_discard')
    assert 'USB->CNTR' not in discard
    assert 'discard_match_primask' in query
    assert 'source=%u' in guard_query
    assert 'source_caller=%u' in guard_query
    assert 'exit_source=%u' in guard_query
    assert 'exit_caller=%u' in guard_query
    assert 'duration=%u' in guard_query
    assert 'entry_flags=%c' in guard_query
    print("PASS: STM32 clears late SOF without touching endpoint IRQ state")
    print("PASS: discarded SOF records callers, duration, and entry state")


def main():
    test_arm_irq_attributes_guard_before_restoring_primask()
    test_stm32_guard_discards_only_sof()
    print("ALL PASS")


if __name__ == '__main__':
    main()
