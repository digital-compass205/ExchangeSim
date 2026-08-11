#!/usr/bin/env python3
"""Extract text from a PDF using only the standard library.

The venue specifications this simulator is built from are published as PDFs,
and the project takes no third-party dependencies -- so reading them needs to
be possible with what ships in Python.

Handles the common case these documents use: FlateDecode content streams with
WinAnsi-encoded text. It is a reading aid for developers, not a general-purpose
PDF library -- CID/Type0 fonts with custom encodings will come out as mojibake,
and layout is approximated from text-positioning operators.

Usage::

    python tools/pdftext.py spec.pdf                # whole document
    python tools/pdftext.py spec.pdf --pages 5-9    # a page range
    python tools/pdftext.py spec.pdf --grep OrdType # matching lines only
"""

import argparse
import re
import sys
import zlib

BACKSLASH = 92

# PDF string escapes that map to a single character.
SIMPLE_ESCAPES = {
    ord("n"): "\n", ord("r"): "\r", ord("t"): "\t",
    ord("b"): "\b", ord("f"): "\f",
    ord("("): "(", ord(")"): ")", BACKSLASH: chr(BACKSLASH),
}

# Operators after which a line break approximates the original layout.
BREAKING_OPERATORS = frozenset(("Td", "TD", "T*", "Tm", "ET"))

# Language markers Word embeds as text; pure noise in the output.
NOISE = ("en-US", "ja-JP", "en-GB")


def content_streams(data):
    """Yield decompressed streams that look like page content."""
    for match in re.finditer(rb"stream\r?\n", data):
        start = match.end()
        end = data.find(b"endstream", start)
        if end < 0:
            continue
        try:
            decoded = zlib.decompress(data[start:end])
        except zlib.error:
            continue  # not Flate-compressed, or an image
        if b"BT" in decoded:
            yield decoded


def tokenize(stream):
    """Split a content stream into ('s', text) and ('o', operator) tokens."""
    tokens = []
    index = 0
    length = len(stream)

    while index < length:
        char = stream[index]

        if char == ord("("):
            text, index = _read_string(stream, index)
            tokens.append(("s", text))
            continue

        if _is_operator_char(char):
            start = index
            while index < length and _is_operator_char(stream[index]):
                index += 1
            tokens.append(("o", stream[start:index].decode("latin-1")))
            continue

        index += 1

    return tokens


def _is_operator_char(char):
    return (65 <= char <= 90) or (97 <= char <= 122) or char in (ord("*"), ord("'"))


def _read_string(stream, index):
    """Read a PDF literal string starting at '(' and return (text, next_index)."""
    index += 1
    length = len(stream)
    depth = 1
    out = []

    while index < length:
        char = stream[index]

        if char == BACKSLASH:
            nxt = stream[index + 1] if index + 1 < length else 0
            if nxt in SIMPLE_ESCAPES:
                out.append(SIMPLE_ESCAPES[nxt])
                index += 2
                continue
            if 48 <= nxt <= 55:  # octal escape, up to three digits
                digits = ""
                index += 1
                while index < length and len(digits) < 3 and 48 <= stream[index] <= 55:
                    digits += chr(stream[index])
                    index += 1
                out.append(chr(int(digits, 8)))
                continue
            index += 2  # unknown escape; skip it
            continue

        if char == ord("("):
            depth += 1
        elif char == ord(")"):
            depth -= 1
            if depth == 0:
                index += 1
                break

        out.append(chr(char))
        index += 1

    return "".join(out), index


def page_text(stream):
    parts = []
    for kind, value in tokenize(stream):
        if kind == "s":
            parts.append(value)
        elif value in BREAKING_OPERATORS:
            parts.append("\n")
    return "".join(parts)


def clean(text):
    for marker in NOISE:
        text = text.replace(marker, "")
    text = re.sub(r"\.{4,}", "..", text)          # table-of-contents leaders
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def extract(path, first=None, last=None):
    """Return a list of cleaned page texts."""
    with open(path, "rb") as handle:
        data = handle.read()

    pages = [clean(page_text(stream)) for stream in content_streams(data)]
    if first is not None:
        pages = pages[first - 1:last]
    return pages


def parse_pages(spec):
    """Parse ``N`` or ``N-M`` into (first, last)."""
    if not spec:
        return None, None
    if "-" in spec:
        first, _, last = spec.partition("-")
        return int(first), int(last)
    value = int(spec)
    return value, value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("pdf", help="path to the PDF")
    parser.add_argument("--pages", help="page or range, e.g. 5 or 5-9")
    parser.add_argument("--grep", help="print only lines matching this regex")
    parser.add_argument("--no-breaks", action="store_true",
                        help="omit page separators")
    opts = parser.parse_args(argv)

    try:
        first, last = parse_pages(opts.pages)
    except ValueError:
        sys.stderr.write("pdftext: --pages must be N or N-M\n")
        return 2

    try:
        pages = extract(opts.pdf, first, last)
    except (IOError, OSError) as exc:
        sys.stderr.write("pdftext: %s\n" % exc)
        return 1

    if opts.grep:
        pattern = re.compile(opts.grep, re.IGNORECASE)
        for number, text in enumerate(pages, start=first or 1):
            for line in text.split("\n"):
                if pattern.search(line):
                    _write("p%d: %s\n" % (number, line))
        return 0

    for number, text in enumerate(pages, start=first or 1):
        if not opts.no_breaks:
            _write("\n===== page %d =====\n" % number)
        _write(text + "\n")
    return 0


def _write(text):
    """Write text that may contain characters the console encoding lacks."""
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        sys.stdout.write(text.encode(encoding, "replace").decode(encoding))


if __name__ == "__main__":
    sys.exit(main())
