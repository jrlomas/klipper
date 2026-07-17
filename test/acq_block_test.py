#!/usr/bin/env python3
import os
import subprocess
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_acq_block_ownership():
    output = os.path.join(tempfile.gettempdir(), 'acq_block_test')
    subprocess.run([
        'cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
        '-I', ROOT, '-I', os.path.join(ROOT, 'src'),
        os.path.join(ROOT, 'test', 'acq_block_test.c'),
        os.path.join(ROOT, 'src', 'generic', 'acq_block.c'),
        '-o', output,
    ], check=True)
    result = subprocess.run([output], check=True, capture_output=True, text=True)
    assert result.stdout.startswith('PASS:')


if __name__ == '__main__':
    test_acq_block_ownership()
    print('PASS: acquisition block ownership test runner')
