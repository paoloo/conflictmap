"""Terminal colours -> curses colour pairs, ported from Terrascope's
ui/colors.py (MIT) with the same 255-pair budget guard.

Pair ids live in 8 bits of each screen cell, so ids above 255 corrupt
attributes (reverse/bold bits). The budget is capped and once exhausted a new
combination reuses the closest existing pair.
"""
from __future__ import annotations

import curses


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    text = hex_color.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    if len(text) != 6:
        raise ValueError(f"Invalid hex color: {hex_color!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def parse_rgba(color) -> tuple[int, int, int, float]:
    """Parse false/None/"#rgb"/"#rgba"/"#rrggbb"/"#rrggbbaa" -> (r, g, b, a 0..1)."""
    if not color:
        return 0, 0, 0, 0.0
    text = str(color).strip().lstrip("#")
    if len(text) in (3, 4):
        text = "".join(character * 2 for character in text)
    if len(text) == 6:
        text += "ff"
    if len(text) != 8:
        raise ValueError(f"Invalid hex color: {color!r}")
    red, green, blue, alpha = (int(text[i: i + 2], 16) for i in range(0, 8, 2))
    return red, green, blue, alpha / 255


def xterm_256_index_to_rgb(index: int) -> tuple[int, int, int]:
    if index < 16:
        return _XTERM_BASIC_16_RGB[index]
    if index < 232:
        index -= 16
        red, green, blue = index // 36, (index % 36) // 6, index % 6
        return (_XTERM_CUBE_LEVELS[red], _XTERM_CUBE_LEVELS[green], _XTERM_CUBE_LEVELS[blue])
    level = 8 + (index - 232) * 10
    return (level, level, level)


def rgb_to_nearest_xterm_256(rgb: tuple[int, int, int], exclude=()) -> int:
    red, green, blue = rgb
    best_index, best_distance = 0, None
    for index in range(256):
        if index in exclude:
            continue
        candidate = xterm_256_index_to_rgb(index)
        distance = sum((c - t) ** 2 for c, t in zip(candidate, rgb))
        if best_distance is None or distance < best_distance:
            best_index, best_distance = index, distance
    return best_index


def rgb_to_nearest_basic(rgb: tuple[int, int, int]) -> int:
    candidates = ((color, _XTERM_BASIC_16_RGB[color]) for color in range(8))
    red, green, blue = rgb
    best_index, best_distance = 0, None
    for index, candidate in candidates:
        distance = sum((c - t) ** 2 for c, t in zip(candidate, rgb))
        if best_distance is None or distance < best_distance:
            best_index, best_distance = index, distance
    return best_index


def _rgb_distance(a, b) -> int:
    return sum((x - y) ** 2 for x, y in zip(a, b))


_XTERM_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)
_XTERM_BASIC_16_RGB = (
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
)


class ColorManager:
    """Hex colours -> curses colour pairs, with the pair-budget guard."""

    FIRST_FREE_SLOT = 16

    def __init__(self) -> None:
        self.enabled = False
        self.supports_exact_color = False
        self.exhausted = False
        self._pair_by_hex: dict[str, int] = {}
        self._pair_colors: dict[str, tuple] = {}
        self._slot_by_hex: dict[str, int] = {}
        self._redefined: set[int] = set()
        self._next_pair_id = 1
        self._pair_limit = 256
        self._next_color_slot = self.FIRST_FREE_SLOT

    def setup(self) -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        self.enabled = True
        self.supports_exact_color = (
            curses.can_change_color() and curses.COLORS > self.FIRST_FREE_SLOT
        )
        limit = min(curses.COLOR_PAIRS, (curses.A_COLOR >> 8) + 1)
        self._pair_limit = max(2, limit)

    def reset(self) -> None:
        self._pair_by_hex.clear()
        self._pair_colors.clear()
        self._next_pair_id = 1
        self.exhausted = False

    def restore(self) -> None:
        if self._redefined:
            try:
                import sys
                if sys.stdout.isatty():
                    sys.stdout.write("\033]104\007")
                    sys.stdout.flush()
            except OSError:
                pass
            self._redefined.clear()

    def get_pair(self, hex_color: str, background: str | None = None) -> int:
        if not self.enabled:
            return 0
        fg = hex_color.strip().lower()
        bg = background.strip().lower() if background else None
        key = f"{fg}|{bg}"
        cached = self._pair_by_hex.get(key)
        if cached is not None:
            return cached
        attribute = self._create_pair(key, fg, bg)
        self._pair_by_hex[key] = attribute
        return attribute

    def _slot_for_hex(self, hex_color: str) -> int | None:
        cached = self._slot_by_hex.get(hex_color)
        if cached is not None:
            return cached
        try:
            rgb = hex_to_rgb(hex_color)
        except ValueError:
            return None
        slot = self._resolve_color_slot(rgb)
        self._slot_by_hex[hex_color] = slot
        return slot

    def _create_pair(self, key: str, hex_color: str, background: str | None = None) -> int:
        try:
            fg_rgb = hex_to_rgb(hex_color)
            bg_rgb = hex_to_rgb(background) if background else None
        except ValueError:
            return 0
        if self._next_pair_id >= self._pair_limit:
            self.exhausted = True
            return self._closest_pair(fg_rgb, bg_rgb)
        foreground = self._slot_for_hex(hex_color)
        if foreground is None:
            return 0
        bg_slot = -1
        if background:
            resolved = self._slot_for_hex(background)
            if resolved is not None:
                bg_slot = resolved
        pair_id = self._next_pair_id
        self._next_pair_id += 1
        try:
            curses.init_pair(pair_id, foreground, bg_slot)
        except curses.error:
            try:
                curses.init_pair(pair_id, foreground, curses.COLOR_BLACK)
            except curses.error:
                return 0
        self._pair_colors[key] = (fg_rgb, bg_rgb)
        return curses.color_pair(pair_id)

    def _closest_pair(self, fg_rgb, bg_rgb) -> int:
        best_key, best_distance = None, None
        for key, (fg, bg) in self._pair_colors.items():
            distance = 4 * _rgb_distance(fg, fg_rgb)
            if bg is None and bg_rgb is None:
                pass
            elif bg is None or bg_rgb is None:
                distance += 30000
            else:
                distance += _rgb_distance(bg, bg_rgb)
            if best_distance is None or distance < best_distance:
                best_key, best_distance = key, distance
        return self._pair_by_hex.get(best_key, 0) if best_key is not None else 0

    def _resolve_color_slot(self, rgb: tuple[int, int, int]) -> int:
        if self.supports_exact_color and self._next_color_slot < curses.COLORS:
            slot = self._next_color_slot
            red, green, blue = rgb
            try:
                curses.init_color(slot, round(red / 255 * 1000),
                                  round(green / 255 * 1000), round(blue / 255 * 1000))
                self._next_color_slot += 1
                self._redefined.add(slot)
                return slot
            except curses.error:
                pass
        if curses.COLORS >= 256:
            return rgb_to_nearest_xterm_256(rgb, self._redefined)
        return rgb_to_nearest_basic(rgb)
