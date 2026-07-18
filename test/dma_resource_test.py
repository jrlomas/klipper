#!/usr/bin/env python3
import os
import subprocess
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_dma_resource_manager():
    output = os.path.join(tempfile.gettempdir(), "dma_resource_test")
    command = [
        "gcc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
        "-DCONFIG_DMA_POOL_SIZE=512", "-I", ROOT, "-I", ROOT + "/src",
        os.path.join(ROOT, "test", "dma_resource_test.c"),
        os.path.join(ROOT, "src", "generic", "dma_resource.c"),
        "-o", output,
    ]
    subprocess.check_call(command)
    subprocess.check_call([output])


if __name__ == "__main__":
    test_dma_resource_manager()
