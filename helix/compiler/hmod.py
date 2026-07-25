"""Constrained HELIX native-module container.

Version 1 is intentionally small: fixed binary loader tables, canonical JSON
for host inspection, independently hashed sections, and a content-root hash.
It accepts only self-contained ARM callbacks today.  Imports and relocations
will become additive table types; arbitrary ELF is never sent to the MCU.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
import subprocess

from .elf import (
    EM_ARM,
    SHF_ALLOC,
    SHF_EXECINSTR,
    SHF_WRITE,
    SHT_ARM_EXIDX,
    SHT_NOBITS,
    STB_GLOBAL,
    STT_FUNC,
    ElfError,
    parse_elf32,
)


MAGIC = b"HMOD"
CONTAINER_VERSION = 1

SECTION_EXEC = 0x01
SECTION_WRITE = 0x02
SECTION_ZERO = 0x04
EXPORT_CALLBACK = 1
EXPORT_THUMB = 0x01

# magic, version, header size, target id, flags, manifest offset/size,
# section/export/import/relocation counts, table offsets, payload offset/size,
# and SHA-256 of every byte following the header.
HEADER = struct.Struct("<4sHHIIIIHHHHIIII32s")
SECTION = struct.Struct("<IIIIII32s")
EXPORT = struct.Struct("<IHHIII")


class HmodError(ValueError):
    pass


@dataclass(frozen=True)
class Hmod:
    manifest: dict
    sections: tuple
    exports: tuple
    payload: bytes
    digest: str


def _align(value, alignment):
    alignment = max(1, alignment)
    return (value + alignment - 1) & ~(alignment - 1)


def _name_id(name):
    return int.from_bytes(
        hashlib.sha256(name.encode("utf-8")).digest()[:4], "little"
    )


def _state_layout(layout):
    cursor = 0
    maximum_alignment = 1
    result = []
    for field in layout.fields:
        alignment = field.type.alignment
        cursor = _align(cursor, alignment)
        result.append(
            {
                "name": field.name,
                "type": field.type.name,
                "bits": field.type.bits,
                "signed": field.type.signed,
                "offset": cursor,
            }
        )
        cursor += field.type.bits // 8
        maximum_alignment = max(maximum_alignment, alignment)
    return result, _align(cursor, maximum_alignment), maximum_alignment


def _is_cantunwind(section):
    if section.type != SHT_ARM_EXIDX or section.size % 8:
        return False
    return all(
        struct.unpack_from("<I", section.data, offset + 4)[0] == 1
        for offset in range(0, section.size, 8)
    )


def _compiler_identity():
    result = subprocess.run(
        ["llc", "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    first = result.stdout.splitlines()[0].strip() if result.stdout else "llc"
    return first


def _extract_object(model, target_name, object_data):
    try:
        elf = parse_elf32(object_data)
    except ElfError as exc:
        raise HmodError(str(exc)) from exc
    if elf.machine != EM_ARM:
        raise HmodError("target %s requires an ARM ELF object" % target_name)

    ignored_sections = set()
    kept = []
    for section in elf.sections:
        if not (section.flags & SHF_ALLOC):
            continue
        if section.type == SHT_ARM_EXIDX:
            if not _is_cantunwind(section):
                raise HmodError("nontrivial ARM unwind data is forbidden")
            ignored_sections.add(section.index)
            continue
        if not (
            section.name == ".text"
            or section.name.startswith(".text.")
            or section.name == ".rodata"
            or section.name.startswith(".rodata.")
            or section.name == ".data"
            or section.name.startswith(".data.")
            or section.name == ".bss"
            or section.name.startswith(".bss.")
        ):
            raise HmodError(
                "allocated ELF section %s is not allowed" % section.name
            )
        kept.append(section)
    if not kept or not any(section.flags & SHF_EXECINSTR for section in kept):
        raise HmodError("module object has no executable section")

    kept_indexes = {section.index for section in kept}
    for relocation in elf.relocations:
        if relocation.target_section in ignored_sections:
            continue
        if relocation.target_section in kept_indexes:
            raise HmodError(
                "relocation type %d in section %s is not supported by"
                " container version 1"
                % (
                    relocation.type,
                    elf.sections[relocation.target_section].name,
                )
            )

    section_entries = []
    payload = bytearray()
    section_to_container = {}
    for section in kept:
        offset = _align(len(payload), section.alignment)
        payload.extend(b"\0" * (offset - len(payload)))
        file_data = b"" if section.type == SHT_NOBITS else section.data
        payload.extend(file_data)
        flags = 0
        if section.flags & SHF_EXECINSTR:
            flags |= SECTION_EXEC
        if section.flags & SHF_WRITE:
            flags |= SECTION_WRITE
        if section.type == SHT_NOBITS:
            flags |= SECTION_ZERO
        section_to_container[section.index] = len(section_entries)
        section_entries.append(
            {
                "name": section.name,
                "name_id": _name_id(section.name),
                "flags": flags,
                "alignment": section.alignment,
                "payload_offset": offset,
                "file_size": len(file_data),
                "memory_size": section.size,
                "sha256": hashlib.sha256(file_data).hexdigest(),
            }
        )

    exports = []
    for symbol in elf.symbols:
        if (
            symbol.binding != STB_GLOBAL
            or symbol.type != STT_FUNC
            or not symbol.name.startswith("helix_module_on_")
        ):
            continue
        if symbol.section_index not in section_to_container:
            raise HmodError("callback export is outside a packaged section")
        exports.append(
            {
                "name": symbol.name,
                "name_id": _name_id(symbol.name),
                "kind": EXPORT_CALLBACK,
                "section": section_to_container[symbol.section_index],
                "offset": symbol.value & ~1,
                "size": symbol.size,
                "thumb": bool(symbol.value & 1),
            }
        )
    if not exports:
        raise HmodError("module object exports no HELIX callbacks")
    exports.sort(key=lambda item: item["name"])
    return section_entries, exports, bytes(payload)


def pack_hmod(model, target_name, object_data):
    sections, exports, payload = _extract_object(
        model, target_name, object_data
    )
    state_fields, state_size, state_alignment = _state_layout(model.state)
    source_data = model.source_path.read_bytes()
    manifest = {
        "container": CONTAINER_VERSION,
        "module": model.name,
        "class": model.class_name,
        "source_api": model.api,
        "profile": model.profile,
        "target": target_name,
        "target_id": _name_id(target_name),
        "source": model.source_path.name,
        "source_sha256": hashlib.sha256(source_data).hexdigest(),
        "compiler": _compiler_identity(),
        "state": {
            "name": model.state.name,
            "size": state_size,
            "alignment": state_alignment,
            "fields": state_fields,
        },
        "sections": sections,
        "exports": exports,
        "imports": [],
        "relocations": [],
    }
    manifest_bytes = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    manifest_offset = HEADER.size
    section_table_offset = _align(
        manifest_offset + len(manifest_bytes), 4
    )
    export_table_offset = (
        section_table_offset + SECTION.size * len(sections)
    )
    payload_offset = _align(
        export_table_offset + EXPORT.size * len(exports), 4
    )
    body = bytearray()
    body.extend(manifest_bytes)
    body.extend(b"\0" * (section_table_offset - HEADER.size - len(body)))
    for section in sections:
        body.extend(
            SECTION.pack(
                section["name_id"],
                section["flags"],
                section["alignment"],
                section["payload_offset"],
                section["file_size"],
                section["memory_size"],
                bytes.fromhex(section["sha256"]),
            )
        )
    for export in exports:
        body.extend(
            EXPORT.pack(
                export["name_id"],
                export["kind"],
                export["section"],
                EXPORT_THUMB if export["thumb"] else 0,
                export["offset"],
                export["size"],
            )
        )
    body.extend(b"\0" * (payload_offset - HEADER.size - len(body)))
    body.extend(payload)
    digest = hashlib.sha256(body).digest()
    header = HEADER.pack(
        MAGIC,
        CONTAINER_VERSION,
        HEADER.size,
        _name_id(target_name),
        0,
        manifest_offset,
        len(manifest_bytes),
        len(sections),
        len(exports),
        0,
        0,
        section_table_offset,
        export_table_offset,
        payload_offset,
        len(payload),
        digest,
    )
    return header + bytes(body)


def parse_hmod(data):
    if len(data) < HEADER.size:
        raise HmodError("HMOD header is truncated")
    values = HEADER.unpack_from(data)
    (
        magic,
        version,
        header_size,
        target_id,
        _flags,
        manifest_offset,
        manifest_size,
        section_count,
        export_count,
        import_count,
        relocation_count,
        section_table_offset,
        export_table_offset,
        payload_offset,
        payload_size,
        expected_digest,
    ) = values
    if magic != MAGIC or version != CONTAINER_VERSION:
        raise HmodError("unsupported HMOD container")
    if header_size != HEADER.size:
        raise HmodError("unexpected HMOD header size")
    actual_digest = hashlib.sha256(data[header_size:]).digest()
    if actual_digest != expected_digest:
        raise HmodError("HMOD content-root hash mismatch")
    if import_count or relocation_count:
        raise HmodError("container uses unsupported import/relocation tables")
    manifest_end = manifest_offset + manifest_size
    payload_end = payload_offset + payload_size
    if (
        manifest_offset < header_size
        or manifest_end > len(data)
        or payload_offset < header_size
        or payload_end > len(data)
    ):
        raise HmodError("HMOD range lies outside the container")
    try:
        manifest = json.loads(data[manifest_offset:manifest_end])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HmodError("invalid HMOD manifest") from exc
    manifest_target = manifest.get("target")
    if (
        not isinstance(manifest_target, str)
        or manifest.get("target_id") != target_id
        or _name_id(manifest_target) != target_id
    ):
        raise HmodError("target id disagrees with manifest")
    expected_section_table = _align(manifest_end, 4)
    expected_export_table = (
        expected_section_table + section_count * SECTION.size
    )
    expected_payload = _align(
        expected_export_table + export_count * EXPORT.size, 4
    )
    if (
        section_table_offset != expected_section_table
        or export_table_offset != expected_export_table
        or payload_offset != expected_payload
        or payload_end != len(data)
    ):
        raise HmodError("HMOD tables are not in canonical layout")
    if (
        len(manifest.get("sections", ())) != section_count
        or len(manifest.get("exports", ())) != export_count
    ):
        raise HmodError("HMOD table counts disagree with manifest")

    sections = []
    previous_end = 0
    for index in range(section_count):
        offset = section_table_offset + index * SECTION.size
        if offset + SECTION.size > len(data):
            raise HmodError("HMOD section table is truncated")
        entry = SECTION.unpack_from(data, offset)
        section = {
            "name_id": entry[0],
            "flags": entry[1],
            "alignment": entry[2],
            "payload_offset": entry[3],
            "file_size": entry[4],
            "memory_size": entry[5],
            "sha256": entry[6].hex(),
        }
        alignment = section["alignment"]
        if (
            not alignment
            or alignment & (alignment - 1)
            or section["flags"] & ~(SECTION_EXEC | SECTION_WRITE | SECTION_ZERO)
            or section["memory_size"] < section["file_size"]
            or (
                section["flags"] & SECTION_ZERO
                and section["file_size"] != 0
            )
            or section["payload_offset"] % alignment
            or section["payload_offset"] < previous_end
        ):
            raise HmodError("HMOD section entry is invalid")
        start = payload_offset + section["payload_offset"]
        end = start + section["file_size"]
        if start < payload_offset or end > payload_end:
            raise HmodError("HMOD section payload lies outside the payload")
        if hashlib.sha256(data[start:end]).hexdigest() != section["sha256"]:
            raise HmodError("HMOD section hash mismatch")
        sections.append(section)
        previous_end = section["payload_offset"] + section["file_size"]

    exports = []
    for index in range(export_count):
        offset = export_table_offset + index * EXPORT.size
        if offset + EXPORT.size > len(data):
            raise HmodError("HMOD export table is truncated")
        entry = EXPORT.unpack_from(data, offset)
        if entry[2] >= section_count:
            raise HmodError("HMOD export references an invalid section")
        if (
            entry[1] != EXPORT_CALLBACK
            or entry[3] & ~EXPORT_THUMB
            or entry[4] + entry[5] > sections[entry[2]]["memory_size"]
        ):
            raise HmodError("HMOD export entry is invalid")
        exports.append(
            {
                "name_id": entry[0],
                "kind": entry[1],
                "section": entry[2],
                "flags": entry[3],
                "thumb": bool(entry[3] & EXPORT_THUMB),
                "offset": entry[4],
                "size": entry[5],
            }
        )
    return Hmod(
        manifest=manifest,
        sections=tuple(sections),
        exports=tuple(exports),
        payload=data[payload_offset:payload_end],
        digest=actual_digest.hex(),
    )


def write_hmod(model, target_name, object_path, output_path):
    data = pack_hmod(model, target_name, Path(object_path).read_bytes())
    Path(output_path).write_bytes(data)
    return Path(output_path)
