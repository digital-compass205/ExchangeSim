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

_STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "exchangesim", "web", "static")

STYLESHEET = os.path.join(_STATIC, "style.css")
FAVICON = os.path.join(_STATIC, "favicon.svg")
INDEX = os.path.join(_STATIC, "index.html")

#: WCAG AA for body text.
FLOOR = 4.5

#: The hues a side may be configured to wear. Any of them can end up as --bid
#: or --ask, so every one has to clear the floor, not just the two the shipped
#: config happens to select.
HUES = ("red", "green", "blue", "amber")

#: Tokens that carry text, and the opaque surfaces text can land on.
INK_TOKENS = (("ink", "ink-dim", "ink-faint", "ask", "bid", "accent", "ok",
               "down") + tuple("hue-" + name for name in HUES))
SURFACES = ("bg", "panel", "sunken", "field")

#: A coloured cell is that colour's text over a tint of the same hue, so the
#: pairing has to be checked as well as the plain surfaces.
WASHES = ((("ask", "ask-bg"), ("bid", "bid-bg"))
          + tuple(("hue-" + name, "hue-%s-wash" % name) for name in HUES))


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


def resolve(palette, name, depth=0):
    """A token's value, following ``var(--other)`` to the colour behind it.

    ``--bid`` and ``--ask`` are aliases onto a hue rather than literals, which
    is what lets the page repoint them at boot without losing the light theme's
    version of that hue. The checks below want the colour, so they come
    through here.
    """
    if depth > 8:
        raise AssertionError("--%s resolves in a loop" % name)
    value = palette.get(name)
    if value is None:
        raise AssertionError("--%s is not defined" % name)
    match = re.match(r"^var\(\s*--([a-z0-9-]+)\s*\)$", value.strip())
    if match:
        return resolve(palette, match.group(1), depth + 1)
    return value


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

    def test_the_configurable_hues_are_the_ones_the_server_offers(self):
        """One list of colours, not two that drift apart.

        ``build_board`` refuses a name the stylesheet has no token for; a token
        the server will not accept is a colour nobody can select.
        """
        from exchangesim.web.main import SIDE_COLOURS

        self.assertEqual(sorted(HUES), sorted(SIDE_COLOURS))
        for name in SIDE_COLOURS:
            self.assertIn("hue-" + name, self.dark)
            self.assertIn("hue-%s-wash" % name, self.dark)

    def test_the_sides_are_aliases_onto_a_hue(self):
        # A literal here would be the colour of one theme, and the page swaps
        # these two at boot by repointing them at another hue token.
        for palette in (self.dark, self.light):
            for name in ("bid", "ask", "bid-bg", "ask-bg"):
                self.assertTrue(palette[name].startswith("var(--hue-"),
                                "--%s must alias a hue, not %s"
                                % (name, palette[name]))

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


class FaviconTest(unittest.TestCase):
    """The one place outside the stylesheet that is allowed to write a colour.

    ``favicon.svg`` is loaded through ``<img>`` and as the tab icon, and an SVG
    loaded that way cannot see the page's custom properties -- so its two hues
    have to be literals. That is precisely the drift
    :meth:`PaletteStructureTest.test_no_colour_is_written_outside_the_two_palettes`
    exists to prevent, so the exception is paid for here: the four literals must
    be the palette's own red and green, in both themes. Repoint a hue and this
    fails rather than leaving the tab a different red from the board.
    """

    def setUp(self):
        self.dark, self.light = palettes(read_stylesheet())
        with open(FAVICON, encoding="utf-8") as handle:
            self.svg = handle.read()

    def _fills(self):
        """``{"dark": {class: colour}, "light": {...}}`` from the SVG's style.

        Split on the media query the same way :func:`palettes` splits the
        stylesheet: what precedes it is the default (dark), what follows is the
        light override.
        """
        style = re.search(r"<style>(.*?)</style>", self.svg, re.S)
        self.assertIsNotNone(style, "favicon.svg defines no <style> block")
        text = style.group(1)
        split = text.index("@media")
        blocks = {"dark": text[:split], "light": text[split:]}
        return {theme: dict(re.findall(r"\.([a-z]+)\s*\{\s*fill:\s*([^;]+);", body))
                for theme, body in blocks.items()}

    def test_the_mark_wears_the_palette_s_own_hues(self):
        fills = self._fills()
        for theme, palette in (("dark", self.dark), ("light", self.light)):
            for role, hue in (("buy", "hue-red"), ("sell", "hue-green")):
                self.assertIn(role, fills[theme],
                              "favicon.svg defines no .%s fill for %s"
                              % (role, theme))
                self.assertEqual(
                    parse_colour(resolve(palette, hue)),
                    parse_colour(fills[theme][role]),
                    "favicon.svg's %s %s is %s, but --%s is %s"
                    % (theme, role, fills[theme][role], hue,
                       resolve(palette, hue)))

    def test_the_mark_states_a_light_theme_of_its_own(self):
        """Without the media query the tab icon keeps the dark hues on a light
        desktop, which is the same mistake the stylesheet's two blocks avoid."""
        self.assertIn("prefers-color-scheme: light", self.svg)

    def test_the_page_asks_for_the_mark_twice(self):
        """Once as the tab icon and once beside the wordmark -- one file, so
        the two can never disagree."""
        with open(INDEX, encoding="utf-8") as handle:
            page = handle.read()
        self.assertIn('rel="icon"', page)
        self.assertEqual(2, page.count("/static/favicon.svg"))


class ContrastTest(unittest.TestCase):
    """Every token that carries text, against every surface it can land on."""

    def setUp(self):
        self.dark, self.light = palettes(read_stylesheet())

    def _check(self, palette, theme):
        surfaces = {}
        for name in SURFACES:
            colour = parse_colour(resolve(palette, name))
            self.assertIsNotNone(colour, "%s --%s is not an opaque colour"
                                 % (theme, name))
            surfaces[name] = colour

        for ink_name in INK_TOKENS:
            ink = parse_colour(resolve(palette, ink_name))
            self.assertIsNotNone(ink, "%s --%s is not a colour" % (theme, ink_name))
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
        panel = parse_colour(resolve(palette, "panel"))
        for ink_name, wash_name in WASHES:
            ink = parse_colour(resolve(palette, ink_name))
            wash = parse_colour(resolve(palette, wash_name))
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
            ratio = contrast(parse_colour(resolve(palette, "on-accent")),
                             parse_colour(resolve(palette, "accent")))
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
            panel = parse_colour(resolve(palette, "panel"))
            page = parse_colour(resolve(palette, "bg"))
            edge = parse_colour(resolve(palette, "panel-edge"))
            self.assertGreaterEqual(contrast(panel, page), 1.15,
                                    "%s: panel and page are one flat field" % theme)
            self.assertGreaterEqual(contrast(edge, panel), 1.5,
                                    "%s: panel borders are invisible" % theme)
            for row in ("zebra", "row-hover"):
                blended = composite(parse_colour(resolve(palette, row)), panel)
                self.assertGreaterEqual(
                    contrast(blended, panel), 1.05,
                    "%s: --%s does not separate one row from the next"
                    % (theme, row))


if __name__ == "__main__":
    unittest.main()
