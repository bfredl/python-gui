"""Neovim Gtk+ UI."""
from __future__ import print_function, division
import math
import sys

from functools import partial

import cairo

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import GLib, GObject, Gdk, Gtk, Pango, PangoCairo

from .screen import Screen


__all__ = ('GtkUI',)


SHIFT = Gdk.ModifierType.SHIFT_MASK
CTRL = Gdk.ModifierType.CONTROL_MASK
ALT = Gdk.ModifierType.MOD1_MASK


# Translation table for the names returned by Gdk.keyval_name that don't match
# the corresponding nvim key names.
KEY_TABLE = {
    'slash': '/',
    'backslash': '\\',
    'dead_circumflex': '^',
    'at': '@',
    'numbersign': '#',
    'dollar': '$',
    'percent': '%',
    'ampersand': '&',
    'asterisk': '*',
    'parenleft': '(',
    'parenright': ')',
    'underscore': '_',
    'plus': '+',
    'minus': '-',
    'bracketleft': '[',
    'bracketright': ']',
    'braceleft': '{',
    'braceright': '}',
    'dead_diaeresis': '"',
    'dead_acute': "'",
    'less': "<",
    'greater': ">",
    'comma': ",",
    'period': ".",
    'BackSpace': 'BS',
    'Return': 'CR',
    'Escape': 'Esc',
    'Delete': 'Del',
    'Page_Up': 'PageUp',
    'Page_Down': 'PageDown',
    'Enter': 'CR',
    'ISO_Left_Tab': 'Tab'
}


if (GLib.MAJOR_VERSION, GLib.MINOR_VERSION,) <= (2, 32,):
    GLib.threads_init()


def Rectangle(x, y, w, h):
    r = Gdk.Rectangle()
    r.x, r.y, r.width, r.height = x, y, w, h
    return r

class Grid(object):
    pass

class GtkUI(object):

    """Gtk+ UI class."""

    def __init__(self, font):
        """Initialize the UI instance."""
        self._redraw_arg = None
        self._foreground = -1
        self._background = -1
        self._font_name = font[0]
        self._font_size = font[1]
        self._attrs = None
        self._busy = False
        self._mouse_enabled = True
        self._insert_cursor = False
        self._blink = False
        self._blink_timer_id = None
        self._pressed = None
        self._invalid = None
        self._reset_cache()
        self._attr_defs = {}
        self._curgrid = 0
        self.grids = {}
        self.g = None

    def create_drawing_area(self, handle):
        self.g = Grid()
        self.g.handle = handle
        self.g._resize_timer_id = None
        drawing_area = Gtk.DrawingArea()
        drawing_area.connect('draw', partial(self._gtk_draw, self.g))
        window = Gtk.Window()
        window.add(drawing_area)
        window.set_events(window.get_events() |
                          Gdk.EventMask.BUTTON_PRESS_MASK |
                          Gdk.EventMask.BUTTON_RELEASE_MASK |
                          Gdk.EventMask.POINTER_MOTION_MASK |
                          Gdk.EventMask.SCROLL_MASK)
        window.connect('configure-event', partial(self._gtk_configure, self.g))
        window.connect('delete-event', self._gtk_quit)
        window.connect('key-press-event', self._gtk_key)
        window.connect('key-release-event', self._gtk_key_release)
        window.connect('button-press-event', partial(self._gtk_button_press, self.g))
        window.connect('button-release-event', partial(self._gtk_button_release, self.g))
        window.connect('motion-notify-event', partial(self._gtk_motion_notify, self.g))
        window.connect('scroll-event', partial(self._gtk_scroll, self.g))
        window.connect('focus-in-event', self._gtk_focus_in)
        window.connect('focus-out-event', self._gtk_focus_out)
        window.show_all()
        self.g._pango_context = drawing_area.create_pango_context()
        self.g._drawing_area = drawing_area
        self.g._window = window
        self.g._pending = [0, 0, 0]
        self.g._screen = None

        self.grids[handle] = self.g


    def start(self, bridge):
        """Start the UI event loop."""
        bridge.attach(80, 24, rgb=True, ext_multigrid=True)#, ext_cmdline=False)#, ext_messages=True)
        im_context = Gtk.IMMulticontext()
        im_context.set_use_preedit(False)  # TODO: preedit at cursor position
        im_context.connect('commit', self._gtk_input)
        self._im_context = im_context
        self.create_drawing_area(1)
        self._window = self.g._window
        self._bridge = bridge
        Gtk.main()

    def quit(self):
        """Exit the UI event loop."""
        GObject.idle_add(Gtk.main_quit)

    def schedule_screen_update(self, apply_updates):
        """Schedule screen updates to run in the UI event loop."""
        def wrapper():
            apply_updates()
            self._flush()
            self._start_blinking()
            try: 
                print("ctx", self._curgrid, self._screen.row, self._screen.col, file=sys.stderr)
            except Exception:
                pass
            self._im_context.set_client_window(self.g._drawing_area.get_window())
            #self._screen_invalid()
            for g in self.grids.values():
                g._drawing_area.queue_draw()
        GObject.idle_add(wrapper)

    def _screen_invalid(self):
        self.g._drawing_area.queue_draw()

    def _nvim_grid_cursor_goto(self, handle, row, col):
        print("I",handle,file=sys.stderr)
        self._curgrid = handle
        self.g = self.grids[handle]
        self._screen = self.g._screen
        if self._screen is not None:
            # TODO: this should really be asserted on the nvim side
            row, col = min(row, self._screen.rows), min(col, self._screen.columns)
            self._screen.cursor_goto(row,col)
        self._drawing_area = self.g._drawing_area
        self._window= self.g._window


    def _nvim_grid_resize(self, grid, columns, rows):
        print("da")
        if grid not in self.grids:
            self.create_drawing_area(grid)
        da = self.grids[grid]._drawing_area
        # create FontDescription object for the selected font/size
        font_str = '{0} {1}'.format(self._font_name, self._font_size)
        self._font, pixels, normal_width, bold_width = _parse_font(font_str)
        # calculate the letter_spacing required to make bold have the same
        # width as normal
        self._bold_spacing = normal_width - bold_width
        cell_pixel_width, cell_pixel_height = pixels
        # calculate the total pixel width/height of the drawing area
        pixel_width = cell_pixel_width * columns
        pixel_height = cell_pixel_height * rows
        gdkwin = da.get_window()
        content = cairo.CONTENT_COLOR
        self.g._cairo_surface = gdkwin.create_similar_surface(content,
                                                            pixel_width,
                                                            pixel_height)
        self.g._cairo_context = cairo.Context(self.g._cairo_surface)
        self.g._pango_layout = PangoCairo.create_layout(self.g._cairo_context)
        self.g._pango_layout.set_alignment(Pango.Alignment.LEFT)
        self.g._pango_layout.set_font_description(self._font)
        self.g._pixel_width, self.g._pixel_height = pixel_width, pixel_height
        self._cell_pixel_width = cell_pixel_width
        self._cell_pixel_height = cell_pixel_height
        self.g._screen = Screen(columns, rows)
        self._screen = self.g._screen
        self.g._window.resize(pixel_width, pixel_height)

    def _nvim_grid_clear(self, grid):
        g = self.grids[grid]
        self._clear_region(g, self._screen.top, self._screen.bot + 1,
                           self._screen.left, self._screen.right + 1)
        self._screen.clear()

    def _nvim_eol_clear(self):
        row, col = self._screen.row, self._screen.col
        self._clear_region(row, row + 1, col, self._screen.right + 1)
        self._screen.eol_clear()

    def _nvim_busy_start(self):
        self._busy = True

    def _nvim_busy_stop(self):
        self._busy = False

    def _nvim_mouse_on(self):
        self._mouse_enabled = True

    def _nvim_mouse_off(self):
        self._mouse_enabled = False

    def _nvim_mode_change(self, mode):
        self._insert_cursor = mode == 'insert'

    def _nvim_grid_scroll(self, grid, top, bot, left, right, rows, cols):
        g = self.grids[grid]
        self._flush(g)
        # The diagrams below illustrate what will happen, depending on the
        # scroll direction. "=" is used to represent the SR(scroll region)
        # boundaries and "-" the moved rectangles. note that dst and src share
        # a common region
        if rows > 0:
            # move an rectangle in the SR up, this can happen while scrolling
            # down
            # +-------------------------+
            # | (clipped above SR)      |            ^
            # |=========================| dst_top    |
            # | dst (still in SR)       |            |
            # +-------------------------+ src_top    |
            # | src (moved up) and dst  |            |
            # |-------------------------| dst_bot    |
            # | src (cleared)           |            |
            # +=========================+ src_bot
            src_top, src_bot = top + rows , bot
            dst_top, dst_bot = top, bot - rows 
            clr_top, clr_bot = dst_bot, src_bot
        else:
            # move a rectangle in the SR down, this can happen while scrolling
            # up
            # +=========================+ src_top
            # | src (cleared)           |            |
            # |------------------------ | dst_top    |
            # | src (moved down) and dst|            |
            # +-------------------------+ src_bot    |
            # | dst (still in SR)       |            |
            # |=========================| dst_bot    |
            # | (clipped below SR)      |            v
            # +-------------------------+
            src_top, src_bot = top, bot + rows 
            dst_top, dst_bot = top - rows , bot
            clr_top, clr_bot = src_top, dst_top
        g._cairo_surface.flush()
        g._cairo_context.save()
        # The move is performed by setting the source surface to itself, but
        # with a coordinate transformation.
        _, y = self._get_coords(dst_top - src_top, 0)
        g._cairo_context.set_source_surface(g._cairo_surface, 0, y)
        # Clip to ensure only dst is affected by the change
        self._mask_region(g, dst_top, dst_bot, left, right)
        # Do the move
        g._cairo_context.paint()
        g._cairo_context.restore()
        # Clear the emptied region
        self._clear_region(g, clr_top, clr_bot, left, right)
        g._screen.scroll(rows )

    def _nvim_highlight_set(self, attrs):
        self._attrs = self._get_pango_attrs(attrs)

    def _nvim_hl_attr_define(self, hlid, attr, info):
        self._attr_defs[hlid] = attr

    def _nvim_put(self, text):
        if self._screen.row != self.g._pending[0]:
            # flush pending text if jumped to a different row
            self._flush(self.g)
        # work around some redraw glitches that can happen
        #self._redraw_glitch_fix()
        # Update internal screen
        self._screen.put(self._get_pango_text(text), self._attrs)
        self.g._pending[1] = min(self._screen.col - 1, self.g._pending[1])
        self.g._pending[2] = max(self._screen.col, self.g._pending[2])

    def _nvim_grid_line(self, grid, row, col_start, cells):
        g = self.grids[grid]
        screen = self.grids[grid]._screen
        # TODO: delet this
        if row != g._pending[0] and g._pending[1] < g._pending[2]:
            # flush pending text if jumped to a different row
            self._flush(g)
        g._pending[0] = row
        # work around some redraw glitches that can happen
        #self._redraw_glitch_fix()
        # Update internal screen
        col = col_start
        attr = None # will be set in first cell
        for cell in cells:
            text = cell[0]
            if len(cell) > 1:
                hl = cell[1]
                attr = self._get_pango_attrs(self._attr_defs.get(hl, {}))
            repeat = cell[2] if len(cell) > 2 else 1
            for i in range(repeat):
                screen.put_cell(row, col, self._get_pango_text(text), attr)
                col += 1


        g._pending[1] = min(col_start, self.g._pending[1])
        g._pending[2] = max(col, self.g._pending[2])

    def _nvim_bell(self):
        self._window.get_window().beep()

    def _nvim_visual_bell(self):
        pass

    def _nvim_default_colors_set(self, fg, bg, sp, cterm_fg, cterm_bg):
        self._foreground= fg
        self._background = bg
        self._reset_cache()



    def _nvim_suspend(self):
        self._window.iconify()

    def _nvim_set_title(self, title):
        self._window.set_title(title)

    def _nvim_set_icon(self, icon):
        self._window.set_icon_name(icon)

    def _gtk_draw(self, g, wid, cr):
        if not g._screen:
            return
        # from random import random
        # cr.rectangle(0, 0, self._pixel_width, self._pixel_height)
        # cr.set_source_rgb(random(), random(), random())
        # cr.fill()
        g._cairo_surface.flush()
        cr.save()
        cr.rectangle(0, 0, g._pixel_width, g._pixel_height)
        cr.clip()
        cr.set_source_surface(g._cairo_surface, 0, 0)
        cr.paint()
        cr.restore()
        if not self._busy and self._blink and g is self.g:
            # Cursor is drawn separately in the window. This approach is
            # simpler because it doesn't taint the internal cairo surface,
            # which is used for scrolling
            row, col = g._screen.row, g._screen.col
            text, attrs = g._screen.get_cursor()
            self._pango_draw(g, row, col, [(text, attrs,)], cr=cr, cursor=True)
            x, y = self._get_coords(row, col)
            currect = Rectangle(x, y, self._cell_pixel_width,
                                self._cell_pixel_height)
            self._im_context.set_cursor_location(currect)

    def _gtk_configure(self, g, widget, event):
        def resize(*args):
            self._resize_timer_id = None
            width, height = g._window.get_size()
            columns = width // self._cell_pixel_width
            rows = height // self._cell_pixel_height
            if g._screen.columns == columns and g._screen.rows == rows:
                return
            ## TODO: this must tell the grid
            self._bridge.resize(g.handle, columns, rows)

        if not g._screen:
            return
        if event.width == g._pixel_width and \
           event.height == g._pixel_height:
            return
        if g._resize_timer_id is not None:
            GLib.source_remove(g._resize_timer_id)
        g._resize_timer_id = GLib.timeout_add(250, resize)

    def _gtk_quit(self, *args):
        self._bridge.exit()

    def _gtk_key(self, widget, event, *args):
        # This function was adapted from pangoterm source code
        keyval = event.keyval
        state = event.state
        # GtkIMContext will eat a Shift-Space and not tell us about shift.
        # Also don't let IME eat any GDK_KEY_KP_ events
        done = (False if state & SHIFT and keyval == ord(' ') else
                False if Gdk.KEY_KP_Space <= keyval <= Gdk.KEY_KP_Divide else
                self._im_context.filter_keypress(event))
        if done:
            # input method handled keypress
            return True
        if event.is_modifier:
            # We don't need to track the state of modifier bits
            return
        # translate keyval to nvim key
        key_name = Gdk.keyval_name(keyval)
        if key_name.startswith('KP_'):
            key_name = key_name[3:]
        input_str = _stringify_key(KEY_TABLE.get(key_name, key_name), state)
        self._bridge.input(input_str)

    def _gtk_key_release(self, widget, event, *args):
        self._im_context.filter_keypress(event)

    def _gtk_button_press(self, g, widget, event, *args):
        if not self._mouse_enabled or event.type != Gdk.EventType.BUTTON_PRESS:
            return
        button = 'Left'
        if event.button == 2:
            button = 'Middle'
        elif event.button == 3:
            button = 'Right'
        col = int(math.floor(event.x / self._cell_pixel_width))
        row = int(math.floor(event.y / self._cell_pixel_height))
        input_str = _stringify_key(button + 'Mouse', event.state)
        input_str += '<{},{},{}>'.format(g.handle, col, row)
        print(input_str,file=sys.stderr)
        self._bridge.input(input_str)
        self._pressed = button
        return True

    def _gtk_button_release(self, g, widget, event, *args):
        self._pressed = None

    def _gtk_motion_notify(self, g, widget, event, *args):
        if not self._mouse_enabled or not self._pressed:
            return
        col = int(math.floor(event.x / self._cell_pixel_width))
        row = int(math.floor(event.y / self._cell_pixel_height))
        input_str = _stringify_key(self._pressed + 'Drag', event.state)
        input_str += '<{},{},{}>'.format(g.handle, col, row)
        self._bridge.input(input_str)

    def _gtk_scroll(self, g, widget, event, *args):
        if not self._mouse_enabled:
            return
        col = int(math.floor(event.x / self._cell_pixel_width))
        row = int(math.floor(event.y / self._cell_pixel_height))
        if event.direction == Gdk.ScrollDirection.UP:
            key = 'ScrollWheelUp'
        elif event.direction == Gdk.ScrollDirection.DOWN:
            key = 'ScrollWheelDown'
        else:
            return
        input_str = _stringify_key(key, event.state)
        input_str += '<{},{},{}>'.format(g.handle, col, row)
        self._bridge.input(input_str)

    def _gtk_focus_in(self, *a):
        self._im_context.focus_in()

    def _gtk_focus_out(self, *a):
        self._im_context.focus_out()

    def _gtk_input(self, widget, input_str, *args):
        self._bridge.input(input_str.replace('<', '<lt>'))

    def _start_blinking(self):
        def blink(*args):
            self._blink = not self._blink
            self.g._drawing_area.queue_draw()
            #self._screen_invalid()
            self._blink_timer_id = GLib.timeout_add(500, blink)
        if self._blink_timer_id:
            GLib.source_remove(self._blink_timer_id)
        self._blink = False
        blink()

    def _clear_region(self, grid, top, bot, left, right):
        self._flush(grid)
        grid._cairo_context.save()
        self._mask_region(grid, top, bot, left, right)
        r, g, b = _split_color(self._background)
        r, g, b = r / 255.0, g / 255.0, b / 255.0
        grid._cairo_context.set_source_rgb(r, g, b)
        grid._cairo_context.paint()
        grid._cairo_context.restore()

    def _mask_region(self, grid, top, bot, left, right):
        cr = grid._cairo_context
        x1, y1, x2, y2 = self._get_rect(top, bot, left, right)
        cr.rectangle(x1, y1, x2 - x1, y2 - y1)
        cr.clip()

    def _get_rect(self, top, bot, left, right):
        x1, y1 = self._get_coords(top, left)
        x2, y2 = self._get_coords(bot, right)
        return x1, y1, x2, y2

    def _get_coords(self, row, col):
        x = col * self._cell_pixel_width
        y = row * self._cell_pixel_height
        return x, y

    def _flush(self,g=None):
        gs = [g] if g is not None else self.grids.values()
        for g in gs:
            row, startcol, endcol = g._pending
            g._pending[1] = 9001
            g._pending[2] = 0
            if startcol > endcol:
                continue
            g._cairo_context.save()
            ccol = startcol
            buf = []
            bold = False
            for _, col, text, attrs in g._screen.iter(row, row, startcol,
                                                         endcol - 1):
                newbold = attrs and 'bold' in attrs[0]
                if newbold != bold or not text:
                    if buf:
                        self._pango_draw(g, row, ccol, buf)
                    bold = newbold
                    buf = [(text, attrs,)]
                    ccol = col
                else:
                    buf.append((text, attrs,))
            if buf:
                self._pango_draw(g, row, ccol, buf)
            g._cairo_context.restore()

    def _pango_draw(self, g, row, col, data, cr=None, cursor=False):
        markup = []
        for text, attrs in data:
            if not attrs:
                attrs = self._get_pango_attrs(None)
            attrs = attrs[1] if cursor else attrs[0]
            markup.append('<span {0}>{1}</span>'.format(attrs, text))
        markup = ''.join(markup)
        g._pango_layout.set_markup(markup, -1)
        # Draw the text
        if not cr:
            cr = g._cairo_context
        x, y = self._get_coords(row, col)
        if cursor and self._insert_cursor and g is self.g:
            cr.rectangle(x, y, self._cell_pixel_width / 4,
                         self._cell_pixel_height)
            cr.clip()
        cr.move_to(x, y)
        PangoCairo.update_layout(cr, g._pango_layout)
        PangoCairo.show_layout(cr, g._pango_layout)
        _, r = g._pango_layout.get_pixel_extents()

    def _get_pango_text(self, text):
        rv = self._pango_text_cache.get(text, None)
        if rv is None:
            rv = GLib.markup_escape_text(text or '')
            self._pango_text_cache[text] = rv
        return rv

    def _get_pango_attrs(self, attrs):
        key = tuple(sorted((k, v,) for k, v in (attrs or {}).items()))
        rv = self._pango_attrs_cache.get(key, None)
        if rv is None:
            fg = self._foreground if self._foreground != -1 else 0
            bg = self._background if self._background != -1 else 0xffffff
            n = {
                'foreground': _split_color(fg),
                'background': _split_color(bg),
            }
            if attrs:
                # make sure that foreground and background are assigned first
                for k in ['foreground', 'background']:
                    if k in attrs:
                        n[k] = _split_color(attrs[k])
                for k, v in attrs.items():
                    if k == 'reverse':
                        n['foreground'], n['background'] = \
                            n['background'], n['foreground']
                    elif k == 'italic':
                        n['font_style'] = 'italic'
                    elif k == 'bold':
                        n['font_weight'] = 'bold'
                        if self._bold_spacing:
                            n['letter_spacing'] = str(self._bold_spacing)
                    elif k == 'underline':
                        n['underline'] = 'single'
            c = dict(n)
            c['foreground'] = _invert_color(*_split_color(fg))
            c['background'] = _invert_color(*_split_color(bg))
            c['foreground'] = _stringify_color(*c['foreground'])
            c['background'] = _stringify_color(*c['background'])
            n['foreground'] = _stringify_color(*n['foreground'])
            n['background'] = _stringify_color(*n['background'])
            n = ' '.join(['{0}="{1}"'.format(k, v) for k, v in n.items()])
            c = ' '.join(['{0}="{1}"'.format(k, v) for k, v in c.items()])
            rv = (n, c,)
            self._pango_attrs_cache[key] = rv
        return rv

    def _reset_cache(self):
        self._pango_text_cache = {}
        self._pango_attrs_cache = {}

    def _redraw_glitch_fix(self):
        row, col = self._screen.row, self._screen.col
        text, attrs = self._screen.get_cursor()
        # when updating cells in italic or bold words, the result can become
        # messy(characters can be clipped or leave remains when removed). To
        # prevent that, always update non empty sequences of cells and the
        # surrounding space.
        # find the start of the sequence
        lcol = col - 1
        while lcol >= 0:
            text, _ = self._screen.get_cell(row, lcol)
            lcol -= 1
            if text == ' ':
                break
        self.g._pending[1] = min(lcol + 1, self.g._pending[1])
        # find the end of the sequence
        rcol = col + 1
        while rcol < self._screen.columns:
            text, _ = self._screen.get_cell(row, rcol)
            rcol += 1
            if text == ' ':
                break
        self.g._pending[2] = max(rcol, self.g._pending[2])


def _split_color(n):
    return ((n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff,)


def _invert_color(r, g, b):
    return (255 - r, 255 - g, 255 - b,)


def _stringify_color(r, g, b):
    return '#{0:0{1}x}'.format((r << 16) + (g << 8) + b, 6)


def _stringify_key(key, state):
    send = []
    if state & SHIFT:
        send.append('S')
    if state & CTRL:
        send.append('C')
    if state & ALT:
        send.append('A')
    send.append(key)
    return '<' + '-'.join(send) + '>'


def _parse_font(font, cr=None):
    if not cr:
        ims = cairo.ImageSurface(cairo.FORMAT_RGB24, 300, 300)
        cr = cairo.Context(ims)
    fd = Pango.font_description_from_string(font)
    layout = PangoCairo.create_layout(cr)
    layout.set_font_description(fd)
    layout.set_alignment(Pango.Alignment.LEFT)
    layout.set_markup('<span font_weight="bold">A</span>')
    bold_width, _ = layout.get_size()
    layout.set_markup('<span>A</span>')
    pixels = layout.get_pixel_size()
    normal_width, _ = layout.get_size()
    return fd, pixels, normal_width, bold_width
