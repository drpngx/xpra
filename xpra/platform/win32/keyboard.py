# This file is part of Xpra.
# Copyright (C) 2010 Nathaniel Smith <njs@pobox.com>
# Copyright (C) 2011-2023 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import ctypes
from ctypes.wintypes import HANDLE
from ctypes import create_string_buffer, byref
from ctypes.wintypes import DWORD
from typing import List, Dict, Tuple, Optional, Callable

from xpra.platform.win32.common import (
    ActivateKeyboardLayout,
    GetKeyState, GetKeyboardLayoutList, GetKeyboardLayout,
    GetIntSystemParametersInfo, GetKeyboardLayoutName,
    GetWindowThreadProcessId,
    )
from xpra.platform.win32 import constants as win32con
from xpra.platform.keyboard_base import KeyboardBase
from xpra.keyboard.layouts import WIN32_LAYOUTS, WIN32_KEYBOARDS
from xpra.gtk_common.keymap import KEY_TRANSLATIONS
from xpra.util import csv, envint, envbool
from xpra.os_util import bytestostr
from xpra.log import Logger

log = Logger("keyboard")


def _GetKeyboardLayoutList() -> List[int]:
    max_items = 32
    #PHANDLE = ctypes.POINTER(HANDLE)
    handle_list = (HANDLE*max_items)()
    GetKeyboardLayoutList.argtypes = [ctypes.c_int, ctypes.POINTER(HANDLE*max_items)]
    count = GetKeyboardLayoutList(max_items, ctypes.byref(handle_list))
    layouts = []
    for i in range(count):
        layouts.append(int(handle_list[i]))
    return layouts

def x11_layouts_to_win32_hkl() -> Dict[str,int]:
    KMASKS = {
        0xffffffff : (0, 16),
        0xffff  : (0, ),
        0x3ff   : (0, ),
        }
    layout_to_hkl = {}
    max_items = 32
    try:
        handle_list = (HANDLE*max_items)()
        count = GetKeyboardLayoutList(max_items, ctypes.byref(handle_list))
        for i in range(count):
            hkl = handle_list[i]
            hkli = int(hkl)
            for mask, bitshifts in KMASKS.items():
                kbid = 0
                for bitshift in bitshifts:
                    kbid = (hkli & mask)>>bitshift
                    if kbid in WIN32_LAYOUTS:
                        break
                if kbid in WIN32_LAYOUTS:
                    code, _, _, _, _layout, _variants = WIN32_LAYOUTS.get(kbid)
                    log("found keyboard layout '%s' / %#x with variants=%s, code '%s' for kbid=%#x",
                        _layout, kbid, _variants, code, hkli)
                    if _layout not in layout_to_hkl:
                        layout_to_hkl[_layout] = hkl
                        break
    except Exception:
        log("x11_layouts_to_win32_hkl()", exc_info=True)
    return layout_to_hkl

EMULATE_ALTGR = envbool("XPRA_EMULATE_ALTGR", True)
EMULATE_ALTGR_CONTROL_KEY_DELAY = envint("XPRA_EMULATE_ALTGR_CONTROL_KEY_DELAY", 50)


class Keyboard(KeyboardBase):
    """ This is for getting keys from the keyboard on the client side.
        Deals with GTK bugs and oddities:
        * missing 'Num_Lock'
        * simulate 'Alt_Gr'
    """

    def __init__(self):
        super().__init__()
        self.hyper_modifier = False

    def init_vars(self) -> None:
        super().init_vars()
        self.num_lock_modifier = None
        self.altgr_modifier = None
        self.delayed_event = None
        self.last_layout_message = None
        #workaround for "period" vs "KP_Decimal" with gtk2 (see ticket #586):
        #translate "period" with keyval=46 and keycode=110 to KP_Decimal:
        KEY_TRANSLATIONS[("period",     46,     110)]   = "KP_Decimal"
        #workaround for "fr" keyboards, which use a different key name under X11:
        KEY_TRANSLATIONS[("dead_tilde", 65107,  50)]    = "asciitilde"
        KEY_TRANSLATIONS[("dead_grave", 65104,  55)]    = "grave"
        self.__x11_layouts_to_win32_hkl = x11_layouts_to_win32_hkl()

    def set_platform_layout(self, layout:str) -> None:
        hkl = self.__x11_layouts_to_win32_hkl.get(layout)
        if hkl is None:
            log(f"asked layout ({layout}) has no corresponding registered keyboard handle")
            return
        # https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-activatekeyboardlayout
        # KLF_SETFORPROCESS|KLF_REORDER = 0x108
        if not ActivateKeyboardLayout(hkl, 0x108):
            log(f"ActivateKeyboardLayout: cannot change layout to {layout}")

    def __repr__(self):
        return "win32.Keyboard"

    def set_modifier_mappings(self, mappings) -> None:
        super().set_modifier_mappings(mappings)
        self.num_lock_modifier = self.modifier_keys.get("Num_Lock")
        log("set_modifier_mappings found 'Num_Lock' with modifier value: %s", self.num_lock_modifier)
        for x in ("ISO_Level3_Shift", "Mode_switch"):
            mod = self.modifier_keys.get(x)
            if mod:
                self.altgr_modifier = mod
                log("set_modifier_mappings found 'AltGr'='%s' with modifier value: %s", x, self.altgr_modifier)
                break

    def mask_to_names(self, mask) -> List[str]:
        """ Patch NUMLOCK and AltGr """
        names = super().mask_to_names(mask)
        if EMULATE_ALTGR:
            rmenu = GetKeyState(win32con.VK_RMENU)
            #log("GetKeyState(VK_RMENU)=%s", rmenu)
            if rmenu not in (0, 1):
                self.AltGr_modifiers(names)
        if self.num_lock_modifier:
            try:
                numlock = GetKeyState(win32con.VK_NUMLOCK)
                if numlock and self.num_lock_modifier not in names:
                    names.append(self.num_lock_modifier)
                elif not numlock and self.num_lock_modifier in names:
                    names.remove(self.num_lock_modifier)
                log("mask_to_names(%s) GetKeyState(VK_NUMLOCK)=%s, names=%s", mask, numlock, names)
            except Exception:
                log("mask_to_names error modifying numlock", exc_info=True)
        else:
            log("mask_to_names(%s)=%s", mask, names)
        return names

    def AltGr_modifiers(self, modifiers, pressed=True):
        add = []
        clear = ["mod1", "mod2", "control"]
        if self.altgr_modifier:
            if pressed:
                add.append(self.altgr_modifier)
            else:
                clear.append(self.altgr_modifier)
        log("AltGr_modifiers(%s, %s) AltGr=%s, add=%s, clear=%s", modifiers, pressed, self.altgr_modifier, add, clear)
        for x in add:
            if x not in modifiers:
                modifiers.append(x)
        for x in clear:
            if x in modifiers:
                modifiers.remove(x)

    def get_keymap_modifiers(self):
        """
            ask the server to manage numlock, and lock can be missing from mouse events
            (or maybe this is virtualbox causing it?)
        """
        return  {}, [], ["lock"]


    def get_all_x11_layouts(self) -> Dict[str,str]:
        x11_layouts = {}
        for win32_layout in WIN32_LAYOUTS.values():
            #("ARA", "Saudi Arabia",   "Arabic",                   1356,   "ar", []),
            x11_layout = win32_layout[4]
            if x11_layout in x11_layouts:
                continue
            name = win32_layout[2]
            x11_layouts[x11_layout] = name
        return x11_layouts


    def get_layout_spec(self) -> Tuple[str,List[str],str,List[str],str]:
        KMASKS = {
            0xffffffff : (0, 16),
            0xffff  : (0, ),
            0x3ff   : (0, ),
            }
        layout = None
        layouts_defs = {}
        variant = None
        variants = None
        options = ""
        layout_code = 0
        try:
            l = _GetKeyboardLayoutList()
            log("GetKeyboardLayoutList()=%s", csv(hex(v) for v in l))
            for hkl in l:
                for mask, bitshifts in KMASKS.items():
                    kbid = 0
                    for bitshift in bitshifts:
                        kbid = (hkl & mask)>>bitshift
                        if kbid in WIN32_LAYOUTS:
                            break
                    if kbid in WIN32_LAYOUTS:
                        code, _, _, _, _layout, _variants = WIN32_LAYOUTS.get(kbid)
                        log("found keyboard layout '%s' / %#x with variants=%s, code '%s' for kbid=%#x",
                            _layout, kbid, _variants, code, hkl)
                        if _layout not in layouts_defs:
                            layouts_defs[_layout] = hkl
                            break
        except Exception as e:
            log("get_layout_spec()", exc_info=True)
            log.error("Error: failed to detect keyboard layouts using GetKeyboardLayoutList:")
            log.estr(e)

        descr = None
        KL_NAMELENGTH = 9
        name_buf = create_string_buffer(KL_NAMELENGTH)
        if GetKeyboardLayoutName(name_buf):
            log("get_layout_spec() GetKeyboardLayoutName()=%s", bytestostr(name_buf.value))
            log("=================== PNG KEYBOARD =========================")
            try:
                #win32 API returns a hex string
                ival = int(name_buf.value, 16)
            except ValueError:
                log.warn("Warning: failed to parse keyboard layout code '%s'", bytestostr(name_buf.value))
            else:
                sublang = (ival & 0xfc00) >> 10
                log("sublang(%#x)=%#x", ival, sublang)
                for mask in KMASKS:
                    val = ival & mask
                    kbdef = WIN32_KEYBOARDS.get(val)
                    log("get_layout_spec() WIN32_KEYBOARDS[%#x]=%s", val, kbdef)
                    if kbdef:
                        _layout, _descr = kbdef
                        if _layout=="??":
                            log.warn("Warning: the X11 codename for %#x is not known", val)
                            log.warn(" only identified as '%s'", _descr)
                            log.warn(" please file a bug report")
                            continue
                        if _layout not in layouts_defs:
                            layouts_defs[_layout] = ival
                        if not layout:
                            layout = _layout
                            descr = _descr
                            layout_code = ival
                            break
                if not layout:
                    log.warn("Warning: unknown keyboard layout %#x", ival)
                    log.warn(" please file a bug report")
                    self.last_layout_message = layout

        try:
            pid = DWORD(0)
            GetWindowThreadProcessId(0, byref(pid))
            tid = GetWindowThreadProcessId(0, pid)
            hkl = GetKeyboardLayout(tid)
            log("GetKeyboardLayout(%i)=%#x", tid, hkl)
            for mask in KMASKS:
                kbid = hkl & mask
                if kbid not in WIN32_LAYOUTS:
                    continue
                code, _, _, _, layout0, variants = WIN32_LAYOUTS.get(kbid)
                log("found keyboard layout '%s' / %#x with variants=%s, code '%s' for kbid=%i (%#x)",
                    layout0, kbid, variants, code, kbid, hkl)
                if layout0 not in layouts_defs:
                    layouts_defs[layout0] = hkl
                #only override "layout" if unset:
                if not layout and layout0:
                    layout = layout0
                    layout_code = hkl
        except Exception:
            log.error("Error: failed to detect keyboard layout using GetKeyboardLayout", exc_info=True)

        layouts = list(layouts_defs.keys())
        if layouts and not layout:
            layout = layouts[0]
            layout_code = layouts_defs.get(layout, 0)

        if layout and self.last_layout_message!=layout:
            if descr:
                log.info(f"keyboard layout {descr!r} : {layout!r} ({layout_code:#x})")
            else:
                log.info(f"keyboard layout {layout!r} ({layout_code:#x})")
            self.last_layout_message = layout
        return layout, layouts, variant, variants, options

    def get_keyboard_repeat(self) -> Optional[Tuple[int,int]]:
        try:
            _delay = GetIntSystemParametersInfo(win32con.SPI_GETKEYBOARDDELAY)
            _speed = GetIntSystemParametersInfo(win32con.SPI_GETKEYBOARDSPEED)
            #now we need to normalize those weird win32 values:
            #0=250, 3=1000:
            delay = (_delay+1) * 250
            #0=1000/30, 31=1000/2.5
            _speed = min(31, max(0, _speed))
            speed = int(1000/(2.5+27.5*_speed/31))
            log("keyboard repeat speed(%s)=%s, delay(%s)=%s", _speed, speed, _delay, delay)
            return delay,speed
        except Exception as e:
            log.error("failed to get keyboard rate: %s", e)
        return None


    def process_key_event(self, send_key_action_cb:Callable, wid:int, key_event) -> None:
        """ Caps_Lock and Num_Lock don't work properly: they get reported more than once,
            they are reported as not pressed when the key is down, etc
            So we just ignore those and rely on the list of "modifiers" passed
            with each keypress to let the server set them for us when needed.
        """
        if key_event.keyval==2**24-1 and key_event.keyname=="VoidSymbol":
            log("process_key_event: ignoring %s", key_event)
            return
        #self.modifier_mappings = None       #{'control': [(37, 'Control_L'), (105, 'Control_R')], 'mod1':
        #self.modifier_keys = {}             #{"Control_L" : "control", ...}
        #self.modifier_keycodes = {}         #{"Control_R" : [105], ...}
        #self.modifier_keycodes = {"ISO_Level3_Shift": [108]}
        #we can only deal with 'Alt_R' and simulate AltGr (ISO_Level3_Shift)
        #if we have modifier_mappings
        # if key_event.keyname in ("Delete", "DELETE", "Meta_L", "LWIN", "RWIN"):
        #     # KeyEvent(modifiers=[], keyname=Meta_L, keyval=65511, keycode=91, group=0, string=, pressed=True)
        #     # KeyEvent(modifiers=[], keyname=Delete, keyval=65535, keycode=46, group=0, string=, pressed=False)
        #     log("===== PNG: %s" % key_event)
        # Need to send the modifier as follows:
        # send_key_action(1, KeyEvent(modifiers=['shift'], keyname=J, keyval=74, keycode=74, group=0, string=J, pressed=False))
        if key_event.keyname == "Delete":
            key_event.keyname = "Hyper_L"
            key_event.keyval = 16777215
            key_event.group = 0
            key_event.keycode = 50
            log("==== PNG: TRANSLATE DELETE TO HYPER [pressed=%s]", key_event.pressed)
            self.hyper_modifier = key_event.pressed
        else:
            log("==== PNG: KEY [%s%s]", 'H-' if self.hyper_modifier else '', key_event.keyname)
            if self.hyper_modifier:
                key_event.modifiers.append('mod4')  # windows?
                # key_event.modifiers.append('shift')
                # key_event.keyname = 'Hyper_' + key_event.keyname.upper()
        if EMULATE_ALTGR and self.altgr_modifier and len(self.modifier_mappings)>0:
            rmenu = GetKeyState(win32con.VK_RMENU)
            if key_event.keyname=="Control_L":
                log("process_key_event: %s pressed=%s, with GetKeyState(VK_RMENU)=%s",
                    key_event.keyname, key_event.pressed, rmenu)
                #AltGr key events are often preceded by a spurious "Control_L" event
                #delay this one a little bit so we can skip it if an "AltGr" does come through next:
                if rmenu in (0, 1):
                    self.delayed_event = (send_key_action_cb, wid, key_event)
                    #needed for altgr emulation timeouts:
                    from gi.repository import GLib  # @UnresolvedImport
                    GLib.timeout_add(EMULATE_ALTGR_CONTROL_KEY_DELAY, self.send_delayed_key)
                return
            if key_event.keyname=="Alt_R":
                log("process_key_event: Alt_R pressed=%s, with GetKeyState(VK_RMENU)=%s", key_event.pressed, rmenu)
                if rmenu in (0, 1):
                    #cancel "Control_L" if one was due:
                    self.delayed_event = None
                #modify the key event so that it will only trigger the modifier update,
                #and not not the key event itself:
                key_event.string = ""
                key_event.keyname = ""
                key_event.group = -1
                key_event.keyval = -1
                key_event.keycode = -1
                self.AltGr_modifiers(key_event.modifiers)
        self.send_delayed_key()
        super().process_key_event(send_key_action_cb, wid, key_event)

    def send_delayed_key(self) -> None:
        #timeout: this must be a real one, send it now
        dk = self.delayed_event
        log("send_delayed_key() delayed_event=%s", dk)
        if dk:
            self.delayed_event = None
            rmenu = GetKeyState(win32con.VK_RMENU)
            log("send_delayed_key() GetKeyState(VK_RMENU)=%s", rmenu)
            if rmenu in (0, 1):
                super().process_key_event(*dk)


# Alt_m:
# send_key_action(1, KeyEvent(modifiers=['mod1'], keyname=m, keyval=109, keycode=77, group=0, string=m, pressed=False))

# get_gtk_keymap((None, 'VoidSymbol', '0xffffff'))=[(65385, 'Cancel', 3, 0, 0), (65288, 'BackSpace', 8, 0, 0), (65289, 'Tab', 9, 0, 0), (65056, 'ISO_Left_Tab', 9, 0, 1), (65291, 'Clear', 12, 0, 0), (65293, 'Return', 13, 0, 0), (65505, 'Shift_L', 16, 0, 0), (65507, 'Control_L', 17, 0, 0), (65513, 'Alt_L', 18, 0, 0), (65299, 'Pause', 19, 0, 0), (65509, 'Caps_Lock', 20, 0, 0), (65307, 'Escape', 27, 0, 0), (32, 'space', 32, 0, 0), (32, 'space', 32, 0, 1), (65365, 'Page_Up', 33, 0, 0), (65366, 'Page_Down', 34, 0, 0), (65367, 'End', 35, 0, 0), (65360, 'Home', 36, 0, 0), (65361, 'Left', 37, 0, 0), (65362, 'Up', 38, 0, 0), (65363, 'Right', 39, 0, 0), (65364, 'Down', 40, 0, 0), (65376, 'Select', 41, 0, 0), (65377, 'Print', 42, 0, 0), (65378, 'Execute', 43, 0, 0), (65377, 'Print', 44, 0, 0), (65379, 'Insert', 45, 0, 0), (65535, 'Delete', 46, 0, 0), (65386, 'Help', 47, 0, 0), (48, '0', 48, 0, 0), (41, 'parenright', 48, 0, 1), (49, '1', 49, 0, 0), (33, 'exclam', 49, 0, 1), (50, '2', 50, 0, 0), (64, 'at', 50, 0, 1), (51, '3', 51, 0, 0), (35, 'numbersign', 51, 0, 1), (52, '4', 52, 0, 0), (36, 'dollar', 52, 0, 1), (53, '5', 53, 0, 0), (37, 'percent', 53, 0, 1), (54, '6', 54, 0, 0), (94, 'asciicircum', 54, 0, 1), (55, '7', 55, 0, 0), (38, 'ampersand', 55, 0, 1), (56, '8', 56, 0, 0), (42, 'asterisk', 56, 0, 1), (57, '9', 57, 0, 0), (40, 'parenleft', 57, 0, 1), (97, 'a', 65, 0, 0), (65, 'A', 65, 0, 1), (98, 'b', 66, 0, 0), (66, 'B', 66, 0, 1), (99, 'c', 67, 0, 0), (67, 'C', 67, 0, 1), (100, 'd', 68, 0, 0), (68, 'D', 68, 0, 1), (101, 'e', 69, 0, 0), (69, 'E', 69, 0, 1), (102, 'f', 70, 0, 0), (70, 'F', 70, 0, 1), (103, 'g', 71, 0, 0), (71, 'G', 71, 0, 1), (104, 'h', 72, 0, 0), (72, 'H', 72, 0, 1), (105, 'i', 73, 0, 0), (73, 'I', 73, 0, 1), (106, 'j', 74, 0, 0), (74, 'J', 74, 0, 1), (107, 'k', 75, 0, 0), (75, 'K', 75, 0, 1), (108, 'l', 76, 0, 0), (76, 'L', 76, 0, 1), (109, 'm', 77, 0, 0), (77, 'M', 77, 0, 1), (110, 'n', 78, 0, 0), (78, 'N', 78, 0, 1), (111, 'o', 79, 0, 0), (79, 'O', 79, 0, 1), (112, 'p', 80, 0, 0), (80, 'P', 80, 0, 1), (113, 'q', 81, 0, 0), (81, 'Q', 81, 0, 1), (114, 'r', 82, 0, 0), (82, 'R', 82, 0, 1), (115, 's', 83, 0, 0), (83, 'S', 83, 0, 1), (116, 't', 84, 0, 0), (84, 'T', 84, 0, 1), (117, 'u', 85, 0, 0), (85, 'U', 85, 0, 1), (118, 'v', 86, 0, 0), (86, 'V', 86, 0, 1), (119, 'w', 87, 0, 0), (87, 'W', 87, 0, 1), (120, 'x', 88, 0, 0), (88, 'X', 88, 0, 1), (121, 'y', 89, 0, 0), (89, 'Y', 89, 0, 1), (122, 'z', 90, 0, 0), (90, 'Z', 90, 0, 1), (65511, 'Meta_L', 91, 0, 0), (65512, 'Meta_R', 92, 0, 0), (65383, 'Menu', 93, 0, 0), (65456, 'KP_0', 96, 0, 0), (65457, 'KP_1', 97, 0, 0), (65458, 'KP_2', 98, 0, 0), (65459, 'KP_3', 99, 0, 0), (65460, 'KP_4', 100, 0, 0), (65461, 'KP_5', 101, 0, 0), (65462, 'KP_6', 102, 0, 0), (65463, 'KP_7', 103, 0, 0), (65464, 'KP_8', 104, 0, 0), (65465, 'KP_9', 105, 0, 0), (65450, 'KP_Multiply', 106, 0, 0), (65451, 'KP_Add', 107, 0, 0), (65452, 'KP_Separator', 108, 0, 0), (65453, 'KP_Subtract', 109, 0, 0), (65454, 'KP_Decimal', 110, 0, 0), (65455, 'KP_Divide', 111, 0, 0), (65470, 'F1', 112, 0, 0), (65471, 'F2', 113, 0, 0), (65472, 'F3', 114, 0, 0), (65473, 'F4', 115, 0, 0), (65474, 'F5', 116, 0, 0), (65475, 'F6', 117, 0, 0), (65476, 'F7', 118, 0, 0), (65477, 'F8', 119, 0, 0), (65478, 'F9', 120, 0, 0), (65479, 'F10', 121, 0, 0), (65480, 'F11', 122, 0, 0), (65481, 'F12', 123, 0, 0), (65482, 'F13', 124, 0, 0), (65483, 'F14', 125, 0, 0), (65484, 'F15', 126, 0, 0), (65485, 'F16', 127, 0, 0), (65486, 'F17', 128, 0, 0), (65487, 'F18', 129, 0, 0), (65488, 'F19', 130, 0, 0), (65489, 'F20', 131, 0, 0), (65490, 'F21', 132, 0, 0), (65491, 'F22', 133, 0, 0), (65492, 'F23', 134, 0, 0), (65493, 'F24', 135, 0, 0), (65407, 'Num_Lock', 144, 0, 0), (65300, 'Scroll_Lock', 145, 0, 0), (65505, 'Shift_L', 160, 0, 0), (65506, 'Shift_R', 161, 0, 0), (65507, 'Control_L', 162, 0, 0), (65508, 'Control_R', 163, 0, 0), (65513, 'Alt_L', 164, 0, 0), (65514, 'Alt_R', 165, 0, 0), (59, 'semicolon', 186, 0, 0), (58, 'colon', 186, 0, 1), (61, 'equal', 187, 0, 0), (43, 'plus', 187, 0, 1), (44, 'comma', 188, 0, 0), (60, 'less', 188, 0, 1), (45, 'minus', 189, 0, 0), (95, 'underscore', 189, 0, 1), (46, 'period', 190, 0, 0), (62, 'greater', 190, 0, 1), (47, 'slash', 191, 0, 0), (63, 'question', 191, 0, 1), (96, 'grave', 192, 0, 0), (126, 'asciitilde', 192, 0, 1), (91, 'bracketleft', 219, 0, 0), (123, 'braceleft', 219, 0, 1), (92, 'backslash', 220, 0, 0), (124, 'bar', 220, 0, 1), (93, 'bracketright', 221, 0, 0), (125, 'braceright', 221, 0, 1), (39, 'apostrophe', 222, 0, 0), (34, 'quotedbl', 222, 0, 1), (92, 'backslash', 226, 0, 0), (124, 'bar', 226, 0, 1)] (keymap=<__gi__.GdkWin32Keymap object at 0x000001c3111fa340 (GdkWin32Keymap at 0x000001c30d4844f0)>)
# 2023-09-03 18:11:05,845 query_xkbmap() keycodes=((65385, 'Cancel', 3, 0, 0), (65288, 'BackSpace', 8, 0, 0), (65289, 'Tab', 9, 0, 0), (65056, 'ISO_Left_Tab', 9, 0, 1), (65291, 'Clear', 12, 0, 0), (65293, 'Return', 13, 0, 0), (65505, 'Shift_L', 16, 0, 0), (65507, 'Control_L', 17, 0, 0), (65513, 'Alt_L', 18, 0, 0), (65299, 'Pause', 19, 0, 0), (65509, 'Caps_Lock', 20, 0, 0), (65307, 'Escape', 27, 0, 0), (32, 'space', 32, 0, 0), (32, 'space', 32, 0, 1), (65365, 'Page_Up', 33, 0, 0), (65366, 'Page_Down', 34, 0, 0), (65367, 'End', 35, 0, 0), (65360, 'Home', 36, 0, 0), (65361, 'Left', 37, 0, 0), (65362, 'Up', 38, 0, 0), (65363, 'Right', 39, 0, 0), (65364, 'Down', 40, 0, 0), (65376, 'Select', 41, 0, 0), (65377, 'Print', 42, 0, 0), (65378, 'Execute', 43, 0, 0), (65377, 'Print', 44, 0, 0), (65379, 'Insert', 45, 0, 0), (65535, 'Delete', 46, 0, 0), (65386, 'Help', 47, 0, 0), (48, '0', 48, 0, 0), (41, 'parenright', 48, 0, 1), (49, '1', 49, 0, 0), (33, 'exclam', 49, 0, 1), (50, '2', 50, 0, 0), (64, 'at', 50, 0, 1), (51, '3', 51, 0, 0), (35, 'numbersign', 51, 0, 1), (52, '4', 52, 0, 0), (36, 'dollar', 52, 0, 1), (53, '5', 53, 0, 0), (37, 'percent', 53, 0, 1), (54, '6', 54, 0, 0), (94, 'asciicircum', 54, 0, 1), (55, '7', 55, 0, 0), (38, 'ampersand', 55, 0, 1), (56, '8', 56, 0, 0), (42, 'asterisk', 56, 0, 1), (57, '9', 57, 0, 0), (40, 'parenleft', 57, 0, 1), (97, 'a', 65, 0, 0), (65, 'A', 65, 0, 1), (98, 'b', 66, 0, 0), (66, 'B', 66, 0, 1), (99, 'c', 67, 0, 0), (67, 'C', 67, 0, 1), (100, 'd', 68, 0, 0), (68, 'D', 68, 0, 1), (101, 'e', 69, 0, 0), (69, 'E', 69, 0, 1), (102, 'f', 70, 0, 0), (70, 'F', 70, 0, 1), (103, 'g', 71, 0, 0), (71, 'G', 71, 0, 1), (104, 'h', 72, 0, 0), (72, 'H', 72, 0, 1), (105, 'i', 73, 0, 0), (73, 'I', 73, 0, 1), (106, 'j', 74, 0, 0), (74, 'J', 74, 0, 1), (107, 'k', 75, 0, 0), (75, 'K', 75, 0, 1), (108, 'l', 76, 0, 0), (76, 'L', 76, 0, 1), (109, 'm', 77, 0, 0), (77, 'M', 77, 0, 1), (110, 'n', 78, 0, 0), (78, 'N', 78, 0, 1), (111, 'o', 79, 0, 0), (79, 'O', 79, 0, 1), (112, 'p', 80, 0, 0), (80, 'P', 80, 0, 1), (113, 'q', 81, 0, 0), (81, 'Q', 81, 0, 1), (114, 'r', 82, 0, 0), (82, 'R', 82, 0, 1), (115, 's', 83, 0, 0), (83, 'S', 83, 0, 1), (116, 't', 84, 0, 0), (84, 'T', 84, 0, 1), (117, 'u', 85, 0, 0), (85, 'U', 85, 0, 1), (118, 'v', 86, 0, 0), (86, 'V', 86, 0, 1), (119, 'w', 87, 0, 0), (87, 'W', 87, 0, 1), (120, 'x', 88, 0, 0), (88, 'X', 88, 0, 1), (121, 'y', 89, 0, 0), (89, 'Y', 89, 0, 1), (122, 'z', 90, 0, 0), (90, 'Z', 90, 0, 1), (65511, 'Meta_L', 91, 0, 0), (65512, 'Meta_R', 92, 0, 0), (65383, 'Menu', 93, 0, 0), (65456, 'KP_0', 96, 0, 0), (65457, 'KP_1', 97, 0, 0), (65458, 'KP_2', 98, 0, 0), (65459, 'KP_3', 99, 0, 0), (65460, 'KP_4', 100, 0, 0), (65461, 'KP_5', 101, 0, 0), (65462, 'KP_6', 102, 0, 0), (65463, 'KP_7', 103, 0, 0), (65464, 'KP_8', 104, 0, 0), (65465, 'KP_9', 105, 0, 0), (65450, 'KP_Multiply', 106, 0, 0), (65451, 'KP_Add', 107, 0, 0), (65452, 'KP_Separator', 108, 0, 0), (65453, 'KP_Subtract', 109, 0, 0), (65454, 'KP_Decimal', 110, 0, 0), (65455, 'KP_Divide', 111, 0, 0), (65470, 'F1', 112, 0, 0), (65471, 'F2', 113, 0, 0), (65472, 'F3', 114, 0, 0), (65473, 'F4', 115, 0, 0), (65474, 'F5', 116, 0, 0), (65475, 'F6', 117, 0, 0), (65476, 'F7', 118, 0, 0), (65477, 'F8', 119, 0, 0), (65478, 'F9', 120, 0, 0), (65479, 'F10', 121, 0, 0), (65480, 'F11', 122, 0, 0), (65481, 'F12', 123, 0, 0), (65482, 'F13', 124, 0, 0), (65483, 'F14', 125, 0, 0), (65484, 'F15', 126, 0, 0), (65485, 'F16', 127, 0, 0), (65486, 'F17', 128, 0, 0), (65487, 'F18', 129, 0, 0), (65488, 'F19', 130, 0, 0), (65489, 'F20', 131, 0, 0), (65490, 'F21', 132, 0, 0), (65491, 'F22', 133, 0, 0), (65492, 'F23', 134, 0, 0), (65493, 'F24', 135, 0, 0), (65407, 'Num_Lock', 144, 0, 0), (65300, 'Scroll_Lock', 145, 0, 0), (65505, 'Shift_L', 160, 0, 0), (65506, 'Shift_R', 161, 0, 0), (65507, 'Control_L', 162, 0, 0), (65508, 'Control_R', 163, 0, 0), (65513, 'Alt_L', 164, 0, 0), (65514, 'Alt_R', 165, 0, 0), (59, 'semicolon', 186, 0, 0), (58, 'colon', 186, 0, 1), (61, 'equal', 187, 0, 0), (43, 'plus', 187, 0, 1), (44, 'comma', 188, 0, 0), (60, 'less', 188, 0, 1), (45, 'minus', 189, 0, 0), (95, 'underscore', 189, 0, 1), (46, 'period', 190, 0, 0), (62, 'greater', 190, 0, 1), (47, 'slash', 191, 0, 0), (63, 'question', 191, 0, 1), (96, 'grave', 192, 0, 0), (126, 'asciitilde', 192, 0, 1), (91, 'bracketleft', 219, 0, 0), (123, 'braceleft', 219, 0, 1), (92, 'backslash', 220, 0, 0), (124, 'bar', 220, 0, 1), (93, 'bracketright', 221, 0, 0), (125, 'braceright', 221, 0, 1), (39, 'apostrophe', 222, 0, 0), (34, 'quotedbl', 222, 0, 1), (92, 'backslash', 226, 0, 0), (124, 'bar', 226, 0, 1))
