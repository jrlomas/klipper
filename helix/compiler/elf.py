"""Small, strict ELF32 reader used by the HELIX module packer.

The printer never parses ELF.  This host-only reader extracts allowlisted
sections and symbols from the LLVM relocatable object before producing a
bounded ``.hmod`` container.
"""

from dataclasses import dataclass
import struct


ELF_MAGIC = b"\x7fELF"
ELFCLASS32 = 1
ELFDATA2LSB = 1
ET_REL = 1
EM_ARM = 40

SHT_NULL = 0
SHT_PROGBITS = 1
SHT_SYMTAB = 2
SHT_STRTAB = 3
SHT_NOBITS = 8
SHT_REL = 9
SHT_ARM_EXIDX = 0x70000001

SHF_WRITE = 0x1
SHF_ALLOC = 0x2
SHF_EXECINSTR = 0x4

STB_GLOBAL = 1
STT_FUNC = 2

ELF_HEADER = struct.Struct("<16sHHIIIIIHHHHHH")
SECTION_HEADER = struct.Struct("<IIIIIIIIII")
SYMBOL = struct.Struct("<IIIBBH")
REL = struct.Struct("<II")


class ElfError(ValueError):
    pass


@dataclass(frozen=True)
class Section:
    index: int
    name: str
    type: int
    flags: int
    address: int
    offset: int
    size: int
    link: int
    info: int
    alignment: int
    entry_size: int
    data: bytes


@dataclass(frozen=True)
class Symbol:
    name: str
    value: int
    size: int
    binding: int
    type: int
    section_index: int


@dataclass(frozen=True)
class Relocation:
    relocation_section: int
    target_section: int
    offset: int
    type: int
    symbol_index: int


@dataclass(frozen=True)
class ElfObject:
    machine: int
    flags: int
    sections: tuple
    symbols: tuple
    relocations: tuple

    def section(self, name):
        for section in self.sections:
            if section.name == name:
                return section
        raise KeyError(name)


def _slice(data, offset, size, label):
    end = offset + size
    if offset < 0 or size < 0 or end < offset or end > len(data):
        raise ElfError("%s lies outside the ELF file" % label)
    return data[offset:end]


def _cstring(table, offset, label):
    if offset < 0 or offset >= len(table):
        if offset == 0 and not table:
            return ""
        raise ElfError("%s string offset lies outside its table" % label)
    end = table.find(b"\0", offset)
    if end < 0:
        raise ElfError("%s is not NUL-terminated" % label)
    try:
        return table[offset:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ElfError("%s is not UTF-8" % label) from exc


def parse_elf32(data):
    if len(data) < ELF_HEADER.size:
        raise ElfError("ELF header is truncated")
    header = ELF_HEADER.unpack_from(data)
    ident = header[0]
    if ident[:4] != ELF_MAGIC:
        raise ElfError("not an ELF file")
    if ident[4] != ELFCLASS32 or ident[5] != ELFDATA2LSB:
        raise ElfError("only little-endian ELF32 objects are supported")
    file_type, machine, version = header[1:4]
    if file_type != ET_REL:
        raise ElfError("module input must be a relocatable ELF object")
    if version != 1:
        raise ElfError("unsupported ELF version")
    flags = header[7]
    section_offset = header[6]
    section_entry_size = header[11]
    section_count = header[12]
    string_section_index = header[13]
    if section_entry_size != SECTION_HEADER.size:
        raise ElfError("unexpected ELF32 section-header size")
    if not section_count or string_section_index >= section_count:
        raise ElfError("invalid ELF section table")
    table_size = section_entry_size * section_count
    section_table = _slice(
        data, section_offset, table_size, "section-header table"
    )
    raw_headers = [
        SECTION_HEADER.unpack_from(section_table, index * section_entry_size)
        for index in range(section_count)
    ]
    string_header = raw_headers[string_section_index]
    section_names = _slice(
        data, string_header[4], string_header[5], "section-name table"
    )

    sections = []
    for index, values in enumerate(raw_headers):
        (name_offset, section_type, section_flags, address, offset, size,
         link, info, alignment, entry_size) = values
        name = _cstring(section_names, name_offset, "section name")
        contents = (
            b""
            if section_type == SHT_NOBITS
            else _slice(data, offset, size, "section %s" % name)
        )
        sections.append(
            Section(
                index=index,
                name=name,
                type=section_type,
                flags=section_flags,
                address=address,
                offset=offset,
                size=size,
                link=link,
                info=info,
                alignment=max(1, alignment),
                entry_size=entry_size,
                data=contents,
            )
        )

    symbols = []
    symbol_indexes = {}
    for section in sections:
        if section.type != SHT_SYMTAB:
            continue
        if section.link >= len(sections):
            raise ElfError("symbol table has an invalid string-table link")
        strings = sections[section.link].data
        if section.entry_size != SYMBOL.size or section.size % SYMBOL.size:
            raise ElfError("malformed ELF32 symbol table")
        table_symbols = []
        for offset in range(0, section.size, SYMBOL.size):
            name_offset, value, size, info, _other, section_index = (
                SYMBOL.unpack_from(section.data, offset)
            )
            table_symbols.append(
                Symbol(
                    name=_cstring(strings, name_offset, "symbol name"),
                    value=value,
                    size=size,
                    binding=info >> 4,
                    type=info & 0xF,
                    section_index=section_index,
                )
            )
        symbol_indexes[section.index] = len(symbols)
        symbols.extend(table_symbols)

    relocations = []
    for section in sections:
        if section.type != SHT_REL:
            continue
        if section.link not in symbol_indexes:
            raise ElfError("relocation section does not link to a symbol table")
        if section.info >= len(sections):
            raise ElfError("relocation section has an invalid target")
        if section.entry_size != REL.size or section.size % REL.size:
            raise ElfError("malformed ELF32 relocation section")
        symbol_base = symbol_indexes[section.link]
        for offset in range(0, section.size, REL.size):
            relocation_offset, info = REL.unpack_from(section.data, offset)
            relocations.append(
                Relocation(
                    relocation_section=section.index,
                    target_section=section.info,
                    offset=relocation_offset,
                    type=info & 0xFF,
                    symbol_index=symbol_base + (info >> 8),
                )
            )
    return ElfObject(
        machine=machine,
        flags=flags,
        sections=tuple(sections),
        symbols=tuple(symbols),
        relocations=tuple(relocations),
    )
