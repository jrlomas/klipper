#!/usr/bin/env python3
"""Run the native gateway fuzz target deterministically without libFuzzer."""

import os
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        output = os.path.join(tmp, 'gateway-fuzz')
        subprocess.check_call([
            'cc', '-std=c11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
            '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
            '-DHELIX_FUZZ_STANDALONE', '-I' + os.path.join(ROOT, 'src'),
            os.path.join(ROOT, 'test/fuzz/helix_gateway_fuzz.c'),
            os.path.join(ROOT, 'src/generic/gateway_protocol.c'),
            os.path.join(ROOT, 'src/generic/gateway_runtime.c'),
            '-o', output])
        # LeakSanitizer cannot inspect /proc while this test runs in ptrace-
        # restricted CI sandboxes; ASan/UBSan bounds and lifetime checks stay
        # enabled, while the target itself performs no allocation.
        env = dict(os.environ, ASAN_OPTIONS='detect_leaks=0:abort_on_error=1',
                   UBSAN_OPTIONS='halt_on_error=1')
        subprocess.check_call([output], env=env)
    print('helix_gateway_native_fuzz_test: PASS (200000 mutations)')


if __name__ == '__main__':
    main()
