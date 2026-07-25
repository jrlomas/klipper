import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from helix.compiler import (
    CompileError,
    HmodError,
    build_object,
    emit_llvm,
    pack_hmod,
    parse_hmod,
    parse_module,
)
from helix.module import module, on_start
from helix.types import state, u8, u32


FIXTURE = ROOT / "test" / "fixtures" / "helix_counter.py"


@state
class HostState:
    counter: u32


@module(name="host-metadata", api="0.1")
class HostModule:
    state: HostState

    @on_start
    def start(self, ctx):
        self.state.counter = u32(1)


class TypeTests(unittest.TestCase):
    def test_fixed_integer_bounds(self):
        self.assertEqual(u8(255), 255)
        with self.assertRaises(OverflowError):
            u8(256)
        self.assertEqual(u8(255) + u8(1), u8(0))
        self.assertIsInstance(u8(255) + u8(1), u8)

    def test_host_decorators_retain_metadata(self):
        self.assertEqual(HostModule.__helix_module__.name, "host-metadata")
        self.assertEqual(HostModule.start.__helix_callback__.kind, "start")


class FrontendTests(unittest.TestCase):
    def test_resolves_module_state_and_callback(self):
        model = parse_module(FIXTURE)
        self.assertEqual(model.name, "counter")
        self.assertEqual(model.api, "0.1")
        self.assertEqual(
            [(field.name, field.type.name) for field in model.state.fields],
            [("boots", "u32"), ("active_lane", "u8")],
        )
        self.assertEqual(
            [(callback.kind, callback.method_name)
             for callback in model.callbacks],
            [("start", "start")],
        )

    def test_rejects_unbounded_loop(self):
        source = """
from helix.module import module, on_start
from helix.types import state, u32
@state
class S:
    value: u32
@module(name="bad", api="0.1")
class Bad:
    state: S
    @on_start
    def start(self, ctx):
        while True:
            self.state.value = u32(1)
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.py"
            path.write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(CompileError, "While"):
                parse_module(path)

    def test_rejects_non_helix_import(self):
        source = """
import os
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.py"
            path.write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(CompileError, "explicit"):
                parse_module(path)

    def test_rejects_out_of_range_fixed_literal(self):
        source = """
from helix.module import module, on_start
from helix.types import state, u8
@state
class S:
    value: u8
@module(name="bad", api="0.1")
class Bad:
    state: S
    @on_start
    def start(self, ctx):
        self.state.value = u8(256)
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.py"
            path.write_text(source, encoding="utf-8")
            model = parse_module(path)
            with self.assertRaisesRegex(CompileError, "outside"):
                emit_llvm(model, "rp2040")


class LlvmTests(unittest.TestCase):
    def test_emits_direct_llvm_ir(self):
        ir = emit_llvm(parse_module(FIXTURE), "stm32f767")
        self.assertIn('target triple = "thumbv7em-none-eabi"', ir)
        self.assertIn("%helix.state = type { i32, i8 }", ir)
        self.assertIn("define i32 @helix_module_on_start", ir)
        self.assertIn("store i32", ir)
        self.assertIn("store i8 3", ir)
        self.assertNotIn("generated C", ir)

    def test_builds_arm_objects_for_each_initial_target(self):
        with tempfile.TemporaryDirectory() as directory:
            for target in ("stm32g0b1", "rp2040", "stm32f767", "stm32h723"):
                first = Path(directory) / ("%s-first.o" % target)
                second = Path(directory) / ("%s-second.o" % target)
                build_object(parse_module(FIXTURE), target, first)
                build_object(parse_module(FIXTURE), target, second)
                self.assertTrue(first.read_bytes().startswith(b"\x7fELF"))
                self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_cli_inspect_and_build(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "counter.o"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "helixc.py"),
                    "inspect",
                    str(FIXTURE),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["module"], "counter")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "helixc.py"),
                    "build-object",
                    str(FIXTURE),
                    "--target",
                    "rp2040",
                    "-o",
                    str(output),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.read_bytes().startswith(b"\x7fELF"))


class HmodTests(unittest.TestCase):
    def _build(self, directory, target="stm32f767"):
        model = parse_module(FIXTURE)
        obj = Path(directory) / "counter.o"
        build_object(model, target, obj)
        return model, pack_hmod(model, target, obj.read_bytes())

    def test_packages_fixed_loader_tables_not_elf(self):
        with tempfile.TemporaryDirectory() as directory:
            _model, data = self._build(directory)
            self.assertTrue(data.startswith(b"HMOD"))
            self.assertNotEqual(data[:4], b"\x7fELF")
            module = parse_hmod(data)
            self.assertEqual(module.manifest["module"], "counter")
            self.assertEqual(module.manifest["target"], "stm32f767")
            self.assertEqual(module.manifest["state"]["size"], 8)
            self.assertEqual(
                [field["offset"]
                 for field in module.manifest["state"]["fields"]],
                [0, 4],
            )
            self.assertEqual(len(module.exports), 1)
            self.assertTrue(module.exports[0]["thumb"])
            self.assertEqual(module.manifest["imports"], [])
            self.assertEqual(module.manifest["relocations"], [])

    def test_container_is_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            _model, first = self._build(directory, "rp2040")
            _model, second = self._build(directory, "rp2040")
            self.assertEqual(first, second)

    def test_content_corruption_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            _model, data = self._build(directory)
            damaged = bytearray(data)
            damaged[-1] ^= 0x80
            with self.assertRaisesRegex(HmodError, "content-root"):
                parse_hmod(bytes(damaged))

    def test_cli_builds_and_inspects_hmod(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "counter.hmod"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "helixc.py"),
                    "build",
                    str(FIXTURE),
                    "--target",
                    "stm32h723",
                    "-o",
                    str(output),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.read_bytes().startswith(b"HMOD"))
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "helixc.py"),
                    "inspect-module",
                    str(output),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(result.stdout)["manifest"]
            self.assertEqual(manifest["target"], "stm32h723")


if __name__ == "__main__":
    unittest.main()
