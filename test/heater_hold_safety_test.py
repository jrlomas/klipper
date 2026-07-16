#!/usr/bin/env python3
"""Compile and run the heater-hold raw-ADC safety predicates."""

import os
import subprocess
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    output = os.path.join(tempfile.gettempdir(), 'heater_hold_safety_test')
    command = [os.environ.get('CC', 'cc'), '-std=gnu11',
               '-I' + os.path.join(ROOT, 'src'),
               os.path.join(ROOT, 'test', 'heater_hold_safety_test.c'),
               '-o', output]
    subprocess.run(command, check=True)
    subprocess.run([output], check=True)


if __name__ == '__main__':
    main()
