# -*- coding: utf-8 -*-
"""自检辅助：向系统注入未标记的滚轮事件（模拟真实滚轮）。"""
import ctypes
import ctypes.wintypes as wt
import sys
import time


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


u = ctypes.WinDLL("user32")
u.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
u.SendInput.restype = wt.UINT


def wheel(delta):
    i = INPUT()
    i.type = 0
    i.mi.mouseData = ctypes.c_ulong(delta & 0xFFFFFFFF).value
    i.mi.dwFlags = 0x0800
    i.mi.dwExtraInfo = ctypes.c_void_p(0)
    return u.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


def main():
    notches = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    gap = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05
    delay = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
    print("inject target window in %.1fs ..." % delay, flush=True)
    time.sleep(delay)
    fail = 0
    for n in range(notches):
        if not wheel(-120):
            fail += 1
        print("injected notch %d" % (n + 1), flush=True)
        time.sleep(gap)
    print("done, sendinput_fail=%d" % fail, flush=True)


if __name__ == "__main__":
    main()
