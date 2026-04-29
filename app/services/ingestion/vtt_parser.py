from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class VttSegment:
    """
    A single speaker turn parsed from a VTT cue block.

    Fields
    ------
    speaker  : Display name extracted from the <v Speaker Name> tag.
               Defaults to "Unknown" when the tag is absent.
    text     : Cleaned spoken text — all HTML/VTT tags stripped.
               Never empty; cues with no text are discarded.
    start_ms : Cue start position in milliseconds from the beginning of the meeting.
    end_ms   : Cue end position in milliseconds from the beginning of the meeting.
    """

    speaker: str
    text: str
    start_ms: int
    end_ms: int


# Matches both HH:MM:SS.mmm and MM:SS.mmm timestamp formats used by Teams VTT.
_TIMESTAMP_RE = re.compile(
    r"(\d{2,}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})"
    r"\s*-->\s*"
    r"(\d{2,}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})"
)

# Matches Teams VTT speaker tag:  <v John Doe>spoken text here
_SPEAKER_RE = re.compile(r"^<v ([^>]+)>(.*)", re.DOTALL)

# Strips any remaining HTML / VTT inline tags from cue text.
_TAG_RE = re.compile(r"<[^>]+>")


def _ts_to_ms(ts: str) -> int:
    """
    Convert a VTT timestamp string to milliseconds.

    Accepts both:
      - HH:MM:SS.mmm  (e.g. "01:23:45.678")
      - MM:SS.mmm     (e.g. "23:45.678") — used by Teams for short meetings
    """
    parts = ts.strip().split(":")
    if len(parts) == 3:
        h, m, s = parts
    else:
        # 2-part format: treat first part as minutes, no hours component.
        h, m, s = "0", parts[0], parts[1]
    return int((int(h) * 3600 + int(m) * 60 + float(s)) * 1000)


def parse_vtt(content: str) -> list[VttSegment]:
    """
    Parse a raw Microsoft Teams VTT transcript string into a list of VttSegments.

    How it works
    ------------
    1. Normalise line endings to \\n.
    2. Split the file into cue blocks on blank lines.
    3. Skip non-cue blocks: WEBVTT header, NOTE, STYLE, REGION.
    4. For each cue block:
       a. Find the timestamp line (optionally preceded by a cue ID number).
       b. Convert start/end timestamps to milliseconds.
       c. Extract speaker name from the first <v Speaker> tag.
       d. Strip all HTML/VTT inline tags from the spoken text.
       e. Discard the cue if the resulting text is empty.
    5. Return the ordered list of VttSegments.

    Guarantees
    ----------
    Every returned VttSegment has:
      - speaker   : non-empty string ("Unknown" when no <v> tag is present)
      - text      : non-empty cleaned string
      - start_ms  : integer ≥ 0
      - end_ms    : integer ≥ 0
    """
    # Normalise line endings so the rest of the logic only deals with \n.
    content = content.replace("\r\n", "\n").replace("\r", "\n")

    # VTT cue blocks are separated by one or more blank lines.
    blocks = re.split(r"\n{2,}", content.strip())

    segments: list[VttSegment] = []

    for block in blocks:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue

        # Skip non-cue blocks: WEBVTT declaration, NOTE comments, STYLE sheets, REGION defs.
        if lines[0].upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue

        # Locate the timestamp line.  A cue ID (plain number or string) may appear
        # on the line immediately before it — skip that line by searching all lines.
        ts_idx = next(
            (i for i, ln in enumerate(lines) if _TIMESTAMP_RE.match(ln)), None
        )
        if ts_idx is None:
            continue  # Block has no timestamp — not a cue, skip it.

        m = _TIMESTAMP_RE.match(lines[ts_idx])
        start_ms = _ts_to_ms(m.group(1))
        end_ms = _ts_to_ms(m.group(2))

        # Everything after the timestamp line is the cue payload (spoken text).
        text_lines = lines[ts_idx + 1 :]
        if not text_lines:
            continue  # Timestamp with no text body — skip.

        speaker = "Unknown"
        text_parts: list[str] = []

        for i, line in enumerate(text_lines):
            sm = _SPEAKER_RE.match(line)
            if sm:
                # First line with a <v> tag determines the speaker for this cue.
                if i == 0:
                    speaker = sm.group(1).strip() or "Unknown"
                # The text after the tag on the same line is part of the spoken content.
                tail = sm.group(2).strip()
                if tail:
                    text_parts.append(_TAG_RE.sub("", tail).strip())
            else:
                # Continuation line — strip any remaining inline tags and collect.
                clean = _TAG_RE.sub("", line).strip()
                if clean:
                    text_parts.append(clean)

        text = " ".join(text_parts).strip()
        if not text:
            continue  # Cue contained only tags with no readable text — discard.

        segments.append(
            VttSegment(speaker=speaker, text=text, start_ms=start_ms, end_ms=end_ms)
        )

    return segments
