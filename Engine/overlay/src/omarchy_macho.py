# SPDX-License-Identifier: MIT
"""Check a Mach-O Stage 1 boot object before the engine fills and stages it.

On an execute-window chip (omarchy_chip.uses_execute_window) Stage 1 is aura's
Mach-O fileset boot object, not a raw image. kmutil records the size of
__TEXT_BOOT_EXEC as the boot policy's kcbz, and iBoot starts SPTM only when
kcbz is nonzero, so the object must not have that segment. Aura's boot
objects also keep every segment fully file-backed at its virtual offset, which
is how iBoot places them, and enter inside __TEXT_EXEC.

The native Recovery pads __TEXT_EXEC for the execute window just before
`kmutil configure-boot --compress`; the engine only checks the unpadded
object and fills its Stage 1 configuration block in place.

Ported from aurora-silicon/aurora-installer 7be5339 (MIT):
MachOBootObject.layout and readOnlySize in
macOS/Recovery/Sources/Account/AuraStage1.swift.
"""

from collections import namedtuple
import struct

MH_MAGIC_64 = 0xFEEDFACF
CPU_TYPE_ARM64 = 0x0100000C
MH_FILESET = 12
LC_SEGMENT_64 = 0x19
LC_UNIXTHREAD = 0x5
ARM_THREAD_STATE64 = 6
REQUIRED = ("__TEXT", "__TEXT_EXEC", "__PRELINK_INFO", "__LINKEDIT")

Segment = namedtuple("Segment", "vmaddr vmsize fileoff filesize")


class MachOError(ValueError):
    pass


def layout(data):
    """The object's segments by name and its entry point, or MachOError."""
    data = bytes(data)

    def u32(offset):
        if offset < 0 or offset + 4 > len(data):
            raise MachOError("truncated")
        return struct.unpack_from("<I", data, offset)[0]

    def u64(offset):
        return u32(offset) | u32(offset + 4) << 32

    if u32(0) != MH_MAGIC_64 or u32(4) != CPU_TYPE_ARM64:
        raise MachOError("not a 64-bit Arm Mach-O image")
    if u32(12) != MH_FILESET:
        raise MachOError("not a fileset boot object")
    count, size = u32(16), u32(20)
    if 32 + size > len(data):
        raise MachOError("load commands are truncated")
    segments, entry, offset = {}, None, 32
    for _ in range(count):
        command, length = u32(offset), u32(offset + 4)
        if length < 8 or length % 8 or offset + length > 32 + size:
            raise MachOError("malformed load command")
        if command == LC_SEGMENT_64:
            if length < 72:
                raise MachOError("malformed segment")
            name = data[offset + 8:offset + 24].split(b"\0", 1)[0].decode("ascii", "replace")
            if name in segments:
                raise MachOError(f"duplicate segment {name}")
            segments[name] = Segment(u64(offset + 24), u64(offset + 32), u64(offset + 40), u64(offset + 48))
        elif command == LC_UNIXTHREAD:
            if length < 16 + 34 * 8 or u32(offset + 8) != ARM_THREAD_STATE64:
                raise MachOError("malformed entry thread")
            entry = u64(offset + 16 + 32 * 8)
        offset += length
    if offset != 32 + size:
        raise MachOError("load command sizes disagree")
    boot = segments.get("__TEXT_BOOT_EXEC")
    if boot is not None and (boot.vmsize or boot.filesize):
        raise MachOError("it has __TEXT_BOOT_EXEC, so iBoot would start SPTM")
    for name in REQUIRED:
        if name not in segments:
            raise MachOError(f"it has no {name} segment")
    base = segments["__TEXT"].vmaddr
    if segments["__TEXT"].fileoff != 0:
        raise MachOError("__TEXT is not at offset 0")
    for name, segment in segments.items():
        if not segment.vmsize:
            continue
        if (segment.vmaddr < base or segment.fileoff != segment.vmaddr - base
                or segment.filesize != segment.vmsize
                or segment.fileoff > len(data) or segment.filesize > len(data) - segment.fileoff):
            raise MachOError(f"{name} is not file-backed at its virtual offset")
    code = segments["__TEXT_EXEC"]
    if entry is None or not code.vmaddr <= entry < code.vmaddr + code.vmsize:
        raise MachOError("its entry point is outside __TEXT_EXEC")
    return segments, entry


def read_only_size(data):
    """The kcrz kmutil should derive: the load address to the end of __TEXT_EXEC."""
    code = layout(data)[0]["__TEXT_EXEC"]
    return code.fileoff + code.vmsize


def text_exec_range(data):
    """The file range of __TEXT_EXEC, where aura's boot object carries its raw Stage 1."""
    code = layout(data)[0]["__TEXT_EXEC"]
    return code.fileoff, code.fileoff + code.filesize
