"""The board's palette, checked as numbers rather than by eye.

``style.css`` states three rules about itself: every colour is a token, both
themes define the same tokens, and every token that carries text is legible on
every surface it can land on. The first two are conventions a person can break
without noticing; the third was broken for a long time -- the dimmest ink sat at
2.8:1 and the board read as grey on grey on a dim monitor -- because nobody had
a number for it. These are those numbers.

The floor is WCAG AA for body text, 4.5:1, which is the right one here: the
board's small type is 11px monospace, so the large-text allowance of 3:1 never
applies to it.
"""

import os
import re
import unittest

STYLESHEET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "exchangesim", "web", "static", "style.css")

#: WCAG AA for body text.
FLOOR = 4.5

#: Tokens that carry text, and the opaque surfaces text can land on.
INK_TOKENS = ("ink", "ink-dim", "ink-faint", "ask", "bid", "accent", "ok", "down")
SURFACES = ("bg", "panel", "sunken", "field")

#: A coloured cell is that colour's text over a tint of the same hue, so the
#: pairing has to be checked as well as the plain surfaces.
WASHES = (("ask", "ask-bg"), ("bid", "bid-bg"))


def _linear(channel):
    channel /= 255.0
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _luminance(colour):
    red, green, blue = colour
    return (0.2126 * _linear(red) + 0.7152 * _linear(green) + 0.0722 * _linear(blue))


def contrast(foreground, background):
    """The WCAG contrast ratio between two opaque colours."""
    first, second = _luminance(foreground), _luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def composite(colour, background):
    """A translucent colour over an opaque one, as the browser would paint it."""
    red, green, blue, alpha = colour
    return tuple(part * alpha + base * (1 - alpha)
                 for part, base in zip((red, green, blue), background))


def parse_colour(value):
    """``#rrggbb`` or ``rgba(r, g, b, a)`` as a tuple, or None for anything else."""
    value = value.strip()
    match = re.match(r"^#([0-9a-fA-F]{6})$", value)
    if match:
        digits = match.group(1)
        return tuple(int(digits[index:index + 2], 16) for index in (0, 2, 4))
    match = re.match(r"^rgba?\(([^)]*)\)$", value)
    if match:
        parts = [float(part) for part in match.group(1).split(",")]
        if len(parts) == 3:
            parts.append(1.0)
        return tuple(parts)
    return None


def read_stylesheet():
    with open(STYLESHEET, encoding="utf-8") as handle:
        return handle.read()


def palettes(text):
    """The dark and light token tables, as ``{name: value}``.

    Dark is the bare ``:root`` block, light the one inside the media query, in
    file order -- which is also the order the cascade applies them.
    """
    blocks = re.findall(r":root\s*\{([^}]*)\}", text)
    if len(blocks) != 2:
        raise AssertionError("expected exactly two :root blocks, found %d"
                             % len(blocks))
    tables = []
    for block in blocks:
        table = {}
        for name, value in re.findall(r"--([a-z0-9-]+)\s*:\s*([^;]+);", block):
            table[name] = value.strip()
        tables.append(table)
    return tables[0], tables[1]


class PaletteStructureTest(unittest.TestCase):

    def setUp(self):
        self.text = read_stylesheet()
        self.dark, self.light = palettes(self.text)

    def test_both_themes_define_the_same_tokens(self):
        # A token defined in one theme only is a colour that vanishes, or turns
        # illegible, the moment the machine's preference changes.
        self.assertEqual(sorted(self.dark), sorted(self.light))

    def test_every_ink_and_surface_token_exists(self):
        for name in INK_TOKENS + SURFACES:
            self.assertIn(name, self.dark)

    def test_no_colour_is_written_outside_the_two_palettes(self):
        """The rule the stylesheet states about itself, enforced.

        A hex value further down the file exists in whichever theme it was
        written for and is invisible or illegible in the other.
        """
        body = self.text[self.text.index("* { box-sizing"):]
        # Strip comments first: prose may quote a value while explaining it.
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        stray = re.findall(r"(#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\))", body)
        self.assertEqual(stray, [])


class ContrastTest(unittest.TestCase):
    """Every token that carries text, against every surface it can land on."""

    def setUp(self):
        self.dark, self.light = palettes(read_stylesheet())

    def _check(self, palette, theme):
        surfaces = {}
        for name in SURFACES:
            colour = parse_colour(palette[name])
            self.assertIsNotNone(colour, "%s --%s is not an opaque colour"
                                 % (theme, name))
            surfaces[name] = colour

        for ink_name in INK_TOKENS:
            ink = parse_colour(palette[ink_name])
            self.assertIsNotNone(ink)
            for surface_name, surface in surfaces.items():
                ratio = contrast(ink, surface)
                self.assertGreaterEqual(
                    ratio, FLOOR,
                    "%s: --%s on --%s is %.2f:1, below the %.1f:1 floor"
                    % (theme, ink_name, surface_name, ratio, FLOOR))

    def test_dark_palette_clears_the_floor(self):
        self._check(self.dark, "dark")

    def test_light_palette_clears_the_floor(self):
        self._check(self.light, "light")

    def _check_washes(self, palette, theme):
        panel = parse_colour(palette["panel"])
        for ink_name, wash_name in WASHES:
            ink = parse_colour(palette[ink_name])
            wash = parse_colour(palette[wash_name])
            self.assertIsNotNone(wash, "--%s must be a colour" % wash_name)
            ratio = contrast(ink, composite(wash, panel))
            self.assertGreaterEqual(
                ratio, FLOOR,
                "%s: --%s on its own --%s is %.2f:1, below the %.1f:1 floor"
                % (theme, ink_name, wash_name, ratio, FLOOR))

    def test_coloured_text_is_legible_on_its_own_wash(self):
        self._check_washes(self.dark, "dark")
        self._check_washes(self.light, "light")

    def test_a_button_fill_carries_its_own_ink(self):
        for palette, theme in ((self.dark, "dark"), (self.light, "light")):
            ratio = contrast(parse_colour(palette["on-accent"]),
                             parse_colour(palette["accent"]))
            self.assertGreaterEqual(
                ratio, FLOOR,
                "%s: --on-accent on --accent is %.2f:1" % (theme, ratio))

    def test_the_surfaces_are_a_step_apart(self):
        """Panels, borders and rows must be seen, not merely sensed.

        Text contrast alone does not carry a dense board: on a cheap panel at
        an angle, a table whose rows and edges have dissolved is unreadable
        however bright its ink.
        """
        for palette, theme in ((self.dark, "dark"), (self.light, "light")):
            panel = parse_colour(palette["panel"])
            page = parse_colour(palette["bg"])
            edge = parse_colour(palette["panel-edge"])
            self.assertGreaterEqual(contrast(panel, page), 1.15,
                                    "%s: panel and page are one flat field" % theme)
            self.assertGreaterEqual(contrast(edge, panel), 1.5,
                                    "%s: panel borders are invisible" % theme)
            for row in ("zebra", "row-hover"):
                blended = composite(parse_colour(palette[row]), panel)
                self.assertGreaterEqual(
                    contrast(blended, panel), 1.05,
                    "%s: --%s does not separate one row from the next"
                    % (theme, row))


if __name__ == "__main__":
    unittest.main()
