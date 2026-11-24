"""Helpers for the Linux CPU-list text format (e.g. ``0-3,6,8-9``)."""

from __future__ import annotations

import re
from collections.abc import Iterable

_ITEM_RE = re.compile(r"^(?:(\d+)|\d+-\d+)$")


class CpuListError(ValueError):
    """Raised when a CPU list cannot be parsed."""


def parse_cpu_list(text: str) -> set[int]:
    """Parse a kernel-style CPU list into a set of CPU numbers.

    Accepts ``0``, ``0-3``, ``0-3,6`` and whitespace/newlines around items.
    An empty string means "no CPUs".
    """

    cpus: set[int] = set()
    text = text.strip()
    if not text:
        return cpus

    for raw_item in text.split(","):
        item = raw_item.strip()
        if not item or not _ITEM_RE.match(item):
            raise CpuListError(f"invalid CPU list item: {raw_item!r}")
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start < 0 or end < start:
                raise CpuListError(f"invalid CPU range: {item!r}")
            cpus.update(range(start, end + 1))
        else:
            cpu = int(item)
            if cpu < 0:
                raise CpuListError(f"invalid CPU number: {item!r}")
            cpus.add(cpu)
    return cpus


def format_cpu_list(cpus: Iterable[int]) -> str:
    """Render a set of CPU numbers in kernel-style range notation."""

    ordered = sorted({int(cpu) for cpu in cpus})
    if not ordered:
        return ""

    chunks: list[str] = []
    start = prev = ordered[0]
    for cpu in ordered[1:]:
        if cpu == prev + 1:
            prev = cpu
            continue
        chunks.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = cpu
    chunks.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(chunks)
