"""Terminal rendering helpers.

Plain ASCII, no colour, no cursor tricks beyond a clear-screen for the live
monitor. CI captures this output, so it has to stay readable in a log file.
"""

import json


def render_table(rows, columns=None, align=None, empty="(none)"):
    # type: (list, list, dict, str) -> str
    """Render a list of dicts as a fixed-width table.

    ``columns`` selects and orders the keys; omitted, it is the union of keys in
    first-seen order. ``align`` maps a column to ``"r"`` for right alignment,
    which is what numeric columns want.
    """
    if not rows:
        return empty

    if columns is None:
        columns = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)

    align = align or {}
    cells = [[_cell(row.get(col)) for col in columns] for row in rows]
    widths = [len(str(col)) for col in columns]
    for line in cells:
        for index, value in enumerate(line):
            if len(value) > widths[index]:
                widths[index] = len(value)

    out = [_row([str(c) for c in columns], widths, align, columns),
           "  ".join("-" * width for width in widths)]
    for line in cells:
        out.append(_row(line, widths, align, columns))
    return "\n".join(out)


def _row(values, widths, align, columns):
    parts = []
    for index, value in enumerate(values):
        width = widths[index]
        if align.get(columns[index]) == "r":
            parts.append(value.rjust(width))
        else:
            parts.append(value.ljust(width))
    return "  ".join(parts).rstrip()


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return ("%.4f" % value).rstrip("0").rstrip(".")
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def render_pairs(mapping, order=None):
    """Render a flat mapping as aligned ``key: value`` lines."""
    if not mapping:
        return "(none)"
    keys = order or list(mapping)
    width = max(len(str(key)) for key in keys)
    return "\n".join("%s : %s" % (str(key).ljust(width), _cell(mapping.get(key)))
                     for key in keys)


def as_json(value):
    return json.dumps(value, indent=2, sort_keys=True, default=str)
