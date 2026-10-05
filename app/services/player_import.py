"""Parse pasted player lines (spec §79.6, §80.3). Pure: no database, no I/O.

A line is `fid`, `fid,kid` or `fid,kid,name` (a name may contain commas). A
second field that is not a kingdom number is read as the name. Several IDs on
one line (`111111111,222222222`) are read as separate players, the shape the
community scripts use for a plain ID list. `#` starts a comment line.
"""
import re
from dataclasses import dataclass, field

MAX_LINES = 500
MAX_KID = 999_999
NAME_MAX = 60
_FID_RE = re.compile(r"^\d{5,20}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f​-‏ -‮⁠﻿]")


class TooManyLines(ValueError):
    pass


@dataclass
class ParsedPlayer:
    fid: str
    kid: int | None = None
    name: str | None = None


@dataclass
class ParsedPlayers:
    entries: list[ParsedPlayer] = field(default_factory=list)
    rejected: list[tuple[int, str]] = field(default_factory=list)  # (line number, reason)
    repeated: int = 0  # lines naming an ID already seen in this paste


def clean_name(raw: str) -> str:
    return _CONTROL_RE.sub("", raw).strip()


def _looks_like_kingdom(field_text: str) -> bool:
    return field_text.isdigit() and len(field_text) <= 6 and 1 <= int(field_text) <= MAX_KID


def parse_player_lines(text: str) -> ParsedPlayers:
    lines = [(i, raw) for i, raw in enumerate(text.splitlines(), start=1) if raw.strip() and not raw.strip().startswith("#")]
    if len(lines) > MAX_LINES:
        raise TooManyLines(f"Paste at most {MAX_LINES} lines at a time")

    result = ParsedPlayers()
    seen: dict[str, ParsedPlayer] = {}

    def add(number: int, entry: ParsedPlayer) -> None:
        if entry.fid in seen:
            result.repeated += 1
            earlier = seen[entry.fid]
            earlier.kid = entry.kid if entry.kid is not None else earlier.kid
            earlier.name = entry.name if entry.name is not None else earlier.name
            return
        seen[entry.fid] = entry
        result.entries.append(entry)

    for number, raw in lines:
        tokens = raw.replace("\t", ",").split(",")
        fields = [f.strip() for f in tokens]
        while fields and not fields[-1]:
            fields.pop()
        if not fields:
            continue
        if len(fields) == 1 and " " in fields[0]:
            fields = tokens = fields[0].split()  # "111111111 222222222"
        if len(fields) >= 2 and all(f.isdigit() for f in fields) and not _looks_like_kingdom(fields[1]):
            for f in fields:
                if _FID_RE.match(f):
                    add(number, ParsedPlayer(f))
                else:
                    result.rejected.append((number, f"{f[:20]} is not a player ID (5 to 20 digits)"))
            continue

        fid, rest = fields[0], tokens[1:]
        if not _FID_RE.match(fid):
            result.rejected.append((number, f"{fid[:20]} is not a player ID (5 to 20 digits)"))
            continue
        kid = None
        if rest and _looks_like_kingdom(rest[0].strip()):
            kid, rest = int(rest[0].strip()), rest[1:]
        elif rest and not rest[0].strip():
            rest = rest[1:]
        name = clean_name(",".join(rest)) or None
        if name and len(name) > NAME_MAX:
            result.rejected.append((number, f"The name for {fid} is longer than {NAME_MAX} characters"))
            continue
        add(number, ParsedPlayer(fid, kid, name))
    return result
