"""Command line entry point for the HELIX native-module compiler."""

import argparse
import json
from pathlib import Path
import tempfile

from .frontend import CompileError, parse_module
from .hmod import HmodError, parse_hmod, write_hmod
from .llvm import build_object, emit_llvm
from .targets import TARGETS


def _manifest(model):
    return {
        "source": str(model.source_path),
        "module": model.name,
        "class": model.class_name,
        "source_api": model.api,
        "profile": model.profile,
        "state": [
            {
                "name": field.name,
                "type": field.type.name,
                "bits": field.type.bits,
                "signed": field.type.signed,
            }
            for field in model.state.fields
        ],
        "callbacks": [
            {"kind": callback.kind, "method": callback.method_name}
            for callback in model.callbacks
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="helixc")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("source", type=Path)

    llvm_parser = subparsers.add_parser("emit-llvm")
    llvm_parser.add_argument("source", type=Path)
    llvm_parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    llvm_parser.add_argument("-o", "--output", type=Path)

    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("source", type=Path)
    build_parser.add_argument(
        "--target", required=True, choices=sorted(TARGETS)
    )
    build_parser.add_argument("-o", "--output", type=Path, required=True)
    build_parser.add_argument("--emit-llvm", type=Path)
    build_parser.add_argument("--emit-object", type=Path)

    object_parser = subparsers.add_parser("build-object")
    object_parser.add_argument("source", type=Path)
    object_parser.add_argument(
        "--target", required=True, choices=sorted(TARGETS)
    )
    object_parser.add_argument("-o", "--output", type=Path, required=True)
    object_parser.add_argument("--emit-llvm", type=Path)

    module_parser = subparsers.add_parser("inspect-module")
    module_parser.add_argument("module", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "inspect-module":
            module = parse_hmod(args.module.read_bytes())
            print(
                json.dumps(
                    {
                        "digest": module.digest,
                        "manifest": module.manifest,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        model = parse_module(args.source)
        if args.command == "inspect":
            print(json.dumps(_manifest(model), indent=2, sort_keys=True))
        elif args.command == "emit-llvm":
            text = emit_llvm(model, args.target)
            if args.output:
                args.output.write_text(text, encoding="utf-8")
            else:
                print(text, end="")
        elif args.command == "build":
            if args.emit_object is not None:
                object_path = args.emit_object
                build_object(
                    model,
                    args.target,
                    object_path,
                    llvm_output=args.emit_llvm,
                )
                write_hmod(model, args.target, object_path, args.output)
            else:
                with tempfile.TemporaryDirectory() as directory:
                    object_path = Path(directory) / "module.o"
                    build_object(
                        model,
                        args.target,
                        object_path,
                        llvm_output=args.emit_llvm,
                    )
                    write_hmod(model, args.target, object_path, args.output)
        elif args.command == "build-object":
            build_object(
                model, args.target, args.output, llvm_output=args.emit_llvm
            )
    except (CompileError, HmodError, OSError, RuntimeError, ValueError) as exc:
        parser.exit(2, "helixc: error: %s\n" % exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
