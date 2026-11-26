# -*- coding: utf-8 -*-
"""
SmoothScroll for Windows
========================
全局鼠标滚轮平滑滚动 + 惯性动量。

原理：
  1. 用 WH_MOUSE_LL 低级鼠标钩子拦截全局滚轮消息，原始事件不再下发（吞掉）。
  2. 把这次滚轮的量累加成"目标位移"，用临界阻尼二阶跟随（弹簧-阻尼模型）逐帧逼近，
     每帧只下发一小段合成滚轮事件（SendInput），于是滚动就变成连续的动画。
  3. 惯性：输入停止后阻尼系统仍会继续滑行一段并平滑减速 —— 这就是"动量"。
  4. 自己发出的事件带 MAGIC 签名并被钩子忽略，不会形成回环。

配置文件：%LOCALAPPDATA%\\SmoothScroll\\config.json
日志：    %LOCALAPPDATA%\\SmoothScroll\\smoothscroll.log

用法：
  pythonw SmoothScroll.py          正常启动（托盘常驻）
  python  SmoothScroll.py --test 8 自检模式，8 秒后打印统计并退出
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import sys
import threading
import io
import base64
import time
import traceback

APP_NAME = "SmoothScroll"
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_PATH = os.path.join(APP_DIR, "smoothscroll.log")

# ---------------------------------------------------------------- 配置默认值
DEFAULTS = {
    "enabled": True,          # 总开关
    "duration_ms": 300,       # 平滑时长(ms)：一次滚轮走完所需时间，越大越"绵"
    "momentum": 0.35,         # 惯性(0~1)：决定松手后"待走完的余量"有多长(约 0.54~1.8 格)
    "sensitivity": 1.0,       # 灵敏度：每次滚轮实际滚动量倍率
    "granularity": 0.1,       # 下发粒度（格）：1.0=整格下发(最兼容)，0.1=最细腻
    "invert": False,          # 反向滚动
    "theme": "auto",          # 设置面板外观：auto=跟随系统 / dark=深色 / light=浅色
    "blacklist": [            # 这些进程里不接管滚轮（游戏/远程桌面等）
        "game.exe",
        "mstsc.exe",
    ],
}

# ---------------------------------------------------------------- Win32 常量
WH_MOUSE_LL = 14
WM_MOUSEWHEEL = 0x020A
WM_QUIT = 0x0012
LLMHF_INJECTED = 0x00000001
WHEEL_DELTA = 120
MOUSEEVENTF_WHEEL = 0x0800
INPUT_MOUSE = 0
SIG = 0x5C0DE5          # 我们自己注入事件的签名（dwExtraInfo）
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# ---------------------------------------------------------------- 单实例
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
MUTEX_NAME = "Local\\SmoothScroll.SingleInstance"
EVENT_NAME = "Local\\SmoothScroll.Activate"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# ---------------------------------------------------------------- 结构体
class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)

user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wt.DWORD]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.UnhookWindowsHookEx.restype = wt.BOOL
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.GetMessageW.restype = wt.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.SendInput.restype = wt.UINT
user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.WindowFromPoint.restype = wt.HWND
user32.WindowFromPoint.argtypes = [POINT, ]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.PostThreadMessageW.restype = wt.BOOL
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetCurrentThreadId.restype = wt.DWORD
kernel32.GetCurrentThreadId.argtypes = []
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE, wt.DWORD, ctypes.c_wchar_p, ctypes.POINTER(wt.DWORD)
]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, ctypes.c_wchar_p]
kernel32.CreateEventW.restype = wt.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, ctypes.c_wchar_p]
kernel32.OpenEventW.restype = wt.HANDLE
kernel32.OpenEventW.argtypes = [wt.DWORD, wt.BOOL, ctypes.c_wchar_p]
kernel32.SetEvent.restype = wt.BOOL
kernel32.SetEvent.argtypes = [wt.HANDLE]
kernel32.WaitForSingleObject.restype = wt.DWORD
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.ReleaseMutex.restype = wt.BOOL
kernel32.ReleaseMutex.argtypes = [wt.HANDLE]


# ---------------------------------------------------------------- 基础工具
_log_lock = threading.Lock()
_log_buf: list[str] = []
_verbose = False
_log_mutex = None

# ---- 帧级追踪（--trace 时打开，只存内存，结束时落盘成 CSV 供排查）----
_trace = False
_trace_buf: list = []
_t0 = 0.0


def _trace_add(*row) -> None:
    if _trace:
        _trace_buf.append((round(time.perf_counter() - _t0, 4),) + row)


def _trace_dump(path: str) -> None:
    if not _trace_buf:
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write("t,kind,dt,target,pos,vel,sent,rem,raw,extra,flags\n")
        for r in _trace_buf:
            t, kind = r[0], r[1]
            if kind == "f":
                dt, tgt, p, v, sent, rem = r[2:8]
                f.write("%.4f,f,%.5f,%.2f,%.2f,%.2f,%d,%.2f,,\n" % (t, dt, tgt, p, v, sent, rem))
            else:
                raw, extra, flags = r[2], r[3], r[4]
                f.write("%.4f,w,,,,,,,%d,%s,%s\n" % (t, raw, extra, flags))
    log("追踪已输出: %s (%d 行)" % (path, len(_trace_buf)))


def _log_mutex_handle():
    """跨进程日志锁：第二个实例退出前也会写日志，避免两个进程把同一行写花。"""
    global _log_mutex
    if _log_mutex is None:
        _log_mutex = kernel32.CreateMutexW(None, False, "Local\\SmoothScroll.LogLock")
    return _log_mutex


def log(msg: str) -> None:
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    with _log_lock:
        _log_buf.append(line)
        h = _log_mutex_handle()
        locked = False
        try:
            if h:
                locked = kernel32.WaitForSingleObject(h, 1000) == 0
            os.makedirs(APP_DIR, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        finally:
            if locked:
                kernel32.ReleaseMutex(h)
    if _verbose:
        try:
            print(line, file=sys.stderr, flush=True)
        except Exception:
            pass
    if len(_log_buf) > 4000:
        del _log_buf[:2000]


def send_wheel(delta: int, horizontal: bool = False) -> bool:
    """注入一段滚轮事件。delta 单位为系统滚轮单位(120=一格)。"""
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.dx = 0
    inp.mi.dy = 0
    inp.mi.mouseData = ctypes.c_ulong(delta & 0xFFFFFFFF).value
    inp.mi.dwFlags = 0x01000 if horizontal else MOUSEEVENTF_WHEEL
    inp.mi.time = 0
    inp.mi.dwExtraInfo = ctypes.c_void_p(SIG)
    ok = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    return bool(ok)


_pname_cache: dict = {"t": 0.0, "name": ""}


def process_name_at(x: int, y: int) -> str:
    """光标处窗口所属进程名(小写)。带 0.4s 缓存，钩子回调里调用要快。"""
    now = time.perf_counter()
    if now - _pname_cache["t"] < 0.4:
        return _pname_cache["name"]
    _pname_cache["t"] = now
    name = ""
    try:
        hwnd = user32.WindowFromPoint(POINT(x, y))
        if hwnd:
            pid = wt.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if h:
                try:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wt.DWORD(1024)
                    if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        name = os.path.basename(buf.value).lower()
                finally:
                    kernel32.CloseHandle(h)
    except Exception:
        name = ""
    _pname_cache["name"] = name
    return name


def load_config() -> dict:
    os.makedirs(APP_DIR, exist_ok=True)
    data = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            log("配置文件读取失败，使用默认值")
            data = {}
    cfg = dict(DEFAULTS)
    if isinstance(data, dict):
        for k, dv in DEFAULTS.items():
            if k not in data:
                continue
            v = data[k]
            if isinstance(dv, bool):
                cfg[k] = bool(v)
            elif isinstance(dv, (int, float)):
                try:
                    cfg[k] = type(dv)(v)
                except (TypeError, ValueError):
                    cfg[k] = dv
            elif isinstance(dv, list):
                cfg[k] = [str(x).lower() for x in v] if isinstance(v, list) else list(dv)
            elif isinstance(dv, str):
                cfg[k] = str(v) if isinstance(v, str) else dv
    save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        log("配置写入失败: %s" % traceback.format_exc(limit=1))


# ---------------------------------------------------------------- 单实例守卫
class Activator:
    """主实例里的"被激活"回调容器：托盘还没起来时先攒着，起来后补触发。"""

    def __init__(self):
        self._cb = None
        self._pending = False
        self._lock = threading.Lock()

    def set_callback(self, cb):
        with self._lock:
            self._cb = cb
            fire = self._pending
            self._pending = False
        if fire:
            self._safe_call()

    def fire(self):
        with self._lock:
            if self._cb is None:
                self._pending = True
                return
        self._safe_call()

    def _safe_call(self):
        try:
            with self._lock:
                cb = self._cb
            if cb:
                cb()
        except Exception:
            log("激活处理异常: %s" % traceback.format_exc(limit=2))


class InstanceGuard:
    """命名互斥体判单例；第二个实例通过命名事件去激活第一个实例后安静退出。

    注意：互斥体/事件都是 Local\\ 命名（按登录会话），进程退出内核对象自动释放，
    所以崩溃过也不会留下"僵尸锁"。
    """

    def __init__(self, suffix: str = ""):
        self._mutex_name = MUTEX_NAME + suffix
        self._event_name = EVENT_NAME + suffix
        self._mutex = None
        self._event = None
        self._stop = threading.Event()

    def is_primary(self) -> bool:
        ctypes.set_last_error(0)
        h = kernel32.CreateMutexW(None, False, self._mutex_name)
        err = ctypes.get_last_error()
        if not h:
            log("互斥体创建失败 err=%d，按单实例继续运行" % err)
            return True
        if err == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(h)
            return False
        self._mutex = h          # 整个进程生命周期持有，别让 GC 关掉
        return True

    def signal_primary(self) -> bool:
        """第二个实例：请求已在运行的那个实例回应我们。"""
        for _ in range(40):      # 最多等 2 秒，避开主实例刚启动还没建事件窗口的竞态
            h = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, self._event_name)
            if h:
                kernel32.SetEvent(h)
                kernel32.CloseHandle(h)
                return True
            time.sleep(0.05)
        return False

    def listen(self, on_activate) -> None:
        """主实例：起个线程等激活信号。"""
        self._event = kernel32.CreateEventW(None, False, False, self._event_name)
        if not self._event:
            log("激活事件创建失败 err=%d" % ctypes.get_last_error())
            return

        def loop():
            while not self._stop.is_set():
                r = kernel32.WaitForSingleObject(self._event, 500)
                if r == 0:                     # WAIT_OBJECT_0
                    log("收到激活请求：已有实例，改为唤起本实例")
                    on_activate()

        threading.Thread(target=loop, daemon=True, name="activate").start()

    def close(self):
        self._stop.set()


# ---------------------------------------------------------------- 核心引擎
class Engine:
    def __init__(self, cfg: dict, allow_injected: bool = False):
        self.cfg = cfg
        self.enabled = threading.Event()
        if cfg.get("enabled", True):
            self.enabled.set()
        self.allow_injected = allow_injected

        self._lock = threading.Lock()
        self.target = 0.0      # 目标位移（滚轮单位，1 格 = 120）
        self.pos = 0.0         # 当前虚拟位置
        self.vel = 0.0         # 当前速度（单位/秒）
        self.emitted = 0.0     # 已经真实下发掉的量（整数）
        self.last_input_t = 0.0  # 最后一次收到滚轮的时刻（用于判断"松手"）
        self.wheel_rate = 0.0  # 用户滚轮速率估算（单位/秒）
        self._rel = None       # 松手补间的状态 (t0, p0, 余量, 时长)

        self._stop = threading.Event()
        self._hook_handle = None
        self._hook_tid = 0
        self._proc_ref = HOOKPROC(self._hook_proc)

        self.stats = {"raw": 0, "swallowed": 0, "sent_calls": 0, "sent_units": 0,
                      "skip_blacklist": 0}

    # -- 钩子回调：运行在钩子线程，必须极快 --
    def _hook_proc(self, nCode, wParam, lParam):
        try:
            if nCode >= 0 and wParam == WM_MOUSEWHEEL and self.enabled.is_set():
                info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if info.dwExtraInfo == SIG:            # 自己发出去的，放行
                    return user32.CallNextHookEx(None, nCode, wParam, lParam)
                if (info.flags & LLMHF_INJECTED) and not self.allow_injected:
                    return user32.CallNextHookEx(None, nCode, wParam, lParam)
                raw = (info.mouseData >> 16) & 0xFFFF
                if raw >= 0x8000:
                    raw -= 0x10000
                if raw:
                    _trace_add("w", raw, hex(info.dwExtraInfo or 0), hex(info.flags))
                    name = process_name_at(info.pt.x, info.pt.y)
                    bl = self.cfg.get("blacklist") or []
                    if name and name in [str(b).lower() for b in bl]:
                        self.stats["skip_blacklist"] += 1
                        return user32.CallNextHookEx(None, nCode, wParam, lParam)
                    self.stats["raw"] += 1
                    self.feed(raw)
                    self.stats["swallowed"] += 1
                    return 1                            # 吞掉原事件
        except Exception:
            log("钩子异常: %s" % traceback.format_exc(limit=2))
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def feed(self, raw_delta: int) -> None:
        cfg = self.cfg
        mult = float(cfg.get("sensitivity", 1.0))
        if cfg.get("invert"):
            raw_delta = -raw_delta
        amount = raw_delta * mult
        now = time.perf_counter()
        with self._lock:
            self.target += amount
            # 估算"用户自己滚多快"（单位/秒），用来定跟随刚度
            if self.last_input_t > 0 and now > self.last_input_t:
                inst = abs(amount) / (now - self.last_input_t)
                self.wheel_rate = (inst if self.wheel_rate <= 0
                                   else self.wheel_rate * 0.7 + inst * 0.3)
            self.last_input_t = now
            self._rel = None               # 新输入打断松手补间

    # -- 动画线程：逐帧把 pos 逼近 target，并把差值切成小段下发 --
    def _anim_loop(self):
        last = time.perf_counter()
        while not self._stop.is_set():
            time.sleep(0.004)
            now = time.perf_counter()
            dt = now - last
            last = now
            if dt > 0.05:
                dt = 0.05
            try:
                if not self.enabled.is_set():
                    # 关闭时把虚拟位置对齐到已下发量，重新开启时不会跳一下
                    with self._lock:
                        self.target = self.pos = float(round(self.emitted))
                        self.vel = 0.0
                        self._rel = None
                    continue

                cfg = self.cfg
                dur = float(cfg.get("duration_ms", 300)) / 1000.0
                mom = max(0.0, min(1.0, float(cfg.get("momentum", 0.35))))
                w = 4.7 / max(0.05, dur)   # 临界阻尼系统固有角频率

                # 允许"待走完的余量"。临界阻尼的稳态落后 = 2v/ω：ω 固定时滚得越快，
                # 积压越多（实测 30 格/秒能攒到 3.9 格），手一停这段就一次性补冲出去，
                # 也就是"马上要停时又猛冲一下"。所以刚度按"用户自己的滚轮速率"来抬：
                # 从第一格就跟得住，积压始终停在 1 格以内，不需要事后追赶。
                lag_ref = WHEEL_DELTA * 0.75

                with self._lock:
                    tgt = self.target
                    p = self.pos
                    v = self.vel
                    gap = tgt - p
                    rate = self.wheel_rate
                active = (now - self.last_input_t) <= 0.05

                if active:
                    w_eff = min(250.0, max(w, 2.0 * rate / lag_ref))
                    self._rel = None
                    k = w_eff * w_eff
                    d = 2.0 * w_eff
                    # 速度不超过"你自己滚轮的速度"：弹簧想追得更快时在这里刹住，
                    # 手停下就不会再冒出一段比手更快的冲量。
                    v_cap = max(700.0, rate)
                    n = max(1, min(25, int(math.ceil(dt / 0.002))))
                    h = dt / n
                    for _ in range(n):
                        a = k * (tgt - p) - d * v
                        v += a * h
                        p += v * h
                        if abs(v) > v_cap:
                            v = math.copysign(v_cap, v)
                else:
                    # 松手：以松手时的速度为初速做三次 ease-out，精确走完 gap。
                    # 速度只降不升，所以不会出现"末段又加速"的冲量。
                    if self._rel is None:
                        T = min(0.50, max(0.06, 3.0 * abs(gap) / max(80.0, abs(v))))
                        T *= 1.0 + 1.2 * mom      # 惯性越大，这段收尾越绵长
                        self._rel = (now, p, gap, T)
                    t0, p0, d0, T = self._rel
                    u = min(1.0, (now - t0) / T)
                    p = p0 + d0 * (1.0 - (1.0 - u) ** 3)
                    v = d0 * 3.0 * (1.0 - u) ** 2 / T
                    if u >= 1.0:
                        p = p0 + d0
                        v = 0.0

                with self._lock:
                    self.pos = p
                    self.vel = v
                    tgt = self.target
                    settled = abs(tgt - p) < 0.6 and abs(v) < 8.0
                    if settled:
                        p = self.pos = tgt
                        self.vel = 0.0
                        self._rel = None
                    rem = self.pos - self.emitted
                    gran_cfg = WHEEL_DELTA * max(0.05, float(cfg.get("granularity", 0.1)))
                    # 自适应下发粒度：尾段速度降下来时粒度同步变细。
                    # 若一直用固定粒度，末段会"憋够 12 单位才发一次" —— 表现为
                    # 停一下、再突然跳一下。这里让相邻两次下发的间隔稳定在约 12ms。
                    gran = min(gran_cfg, max(1.0, (0.0 if settled else abs(v)) * 0.012))

                    sent = 0
                    # 静止时把残余(哪怕小于粒度)也补发掉，保证总量不丢、没有累积漂移
                    if abs(rem) >= gran or (settled and abs(rem) >= 0.5):
                        sent = int(round(rem))
                        if abs(sent) > 6 * WHEEL_DELTA:
                            sent = 6 * WHEEL_DELTA if sent > 0 else -6 * WHEEL_DELTA
                        self.emitted += sent
                if sent:
                    if send_wheel(sent):
                        self.stats["sent_calls"] += 1
                        self.stats["sent_units"] += sent
                _trace_add("f", dt, tgt, p, v, sent, rem)
            except Exception:
                log("动画线程异常: %s" % traceback.format_exc(limit=2))
        log("动画线程退出")

    # -- 钩子线程 --
    def _hook_loop(self):
        self._hook_tid = kernel32.GetCurrentThreadId()
        hmod = kernel32.GetModuleHandleW(None)
        self._hook_handle = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc_ref, hmod, 0)
        if not self._hook_handle:
            log("鼠标钩子安装失败, GetLastError=%d" % ctypes.get_last_error())
            return
        log("鼠标钩子已安装 tid=%d" % self._hook_tid)
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if self._hook_handle:
            user32.UnhookWindowsHookEx(self._hook_handle)
            self._hook_handle = None
        log("鼠标钩子已卸载")

    def start(self):
        threading.Thread(target=self._hook_loop, daemon=True, name="hook").start()
        threading.Thread(target=self._anim_loop, daemon=True, name="anim").start()

    def stop(self):
        self._stop.set()
        if self._hook_tid:
            user32.PostThreadMessageW(self._hook_tid, WM_QUIT, 0, 0)

    def set_enabled(self, on: bool):
        if on:
            self.enabled.set()
        else:
            self.enabled.clear()
        self.cfg["enabled"] = bool(on)
        save_config(self.cfg)


# ---------------------------------------------------------------- 托盘 + 设置界面
# 托盘/窗口图标：64px PNG 的 base64，嵌在源码里，exe 单文件运行不依赖外部资源
ICON_PNG_B64 = (
    "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"
)


def make_icon_image():
    from PIL import Image
    try:
        return Image.open(io.BytesIO(base64.b64decode(ICON_PNG_B64))).convert("RGBA")
    except Exception:
        # 兜底：万一 base64 损坏，退回到一个纯色方块，至少不让托盘崩掉
        return Image.new("RGBA", (64, 64), (16, 28, 38, 255))

def _windows_apps_light_theme() -> bool:
    """读系统"应用主题"：True=浅色，False=深色。读不到按深色。"""
    import winreg
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return bool(int(v))
    except Exception:
        return False


def _set_dark_titlebar(root) -> None:
    """把窗口标题栏也切成深色（Tk 不会自动跟随系统主题）。"""
    try:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = u32.GetParent(root.winfo_id()) or root.winfo_id()
        dwm = ctypes.WinDLL("dwmapi")
        val = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20, ctypes.byref(val),
                                 ctypes.sizeof(val))
    except Exception:
        pass


def _flat_slider(parent, pal, var, lo, hi, res, on_change=None):
    """Canvas 自绘滑杆：已选区间整段填满强调色 + 圆形滑块，比 tk.Scale 的灰条醒目。"""
    import tkinter as tk

    W, H = 226, 28
    cv = tk.Canvas(parent, width=W, height=H, bg=pal["bg"],
                   highlightthickness=0, bd=0, cursor="hand2")
    geo = {"x1": 10, "x2": W - 10, "y": H // 2}

    def snap(x):
        t = max(0.0, min(1.0, (x - geo["x1"]) / float(geo["x2"] - geo["x1"])))
        v = round((lo + t * (hi - lo)) / res) * res
        return max(lo, min(hi, round(v, 6)))

    def redraw():
        cv.delete("all")
        y, th, r = geo["y"], 5, 7
        x1, x2 = geo["x1"], geo["x2"]
        t = 0.0 if hi == lo else (var.get() - lo) / float(hi - lo)
        kx = x1 + t * (x2 - x1)

        # 轨道（两端圆头 + 中间矩形拼成胶囊）
        cv.create_oval(x1, y - th, x1 + 2 * th, y + th, fill=pal["track"], outline="")
        cv.create_oval(x2 - 2 * th, y - th, x2, y + th, fill=pal["track"], outline="")
        cv.create_rectangle(x1 + th, y - th, x2 - th, y + th,
                            fill=pal["track"], outline="")

        # 已选区间：整段强调色，一眼看出当前值在哪个位置
        if kx > x1 + 1:
            cv.create_oval(x1, y - th, x1 + 2 * th, y + th, fill=pal["accent"], outline="")
            cv.create_rectangle(x1 + th, y - th, min(kx, x2 - th), y + th,
                                fill=pal["accent"], outline="")
            if x1 + th < kx < x2 - th:
                cv.create_oval(kx - th, y - th, kx + th, y + th,
                               fill=pal["accent"], outline="")

        # 滑块：亮芯 + 强调色描边，深浅底都看得清
        cv.create_oval(kx - r - 1, y - r - 1, kx + r + 1, y + r + 1,
                       fill=pal["bg"], outline="")
        cv.create_oval(kx - r, y - r, kx + r, y + r,
                       fill=pal["knob"], outline=pal["accent"], width=2)

    def drag(e):
        var.set(snap(e.x))
        redraw()
        if on_change:
            on_change(var.get())

    for ev in ("<Button-1>", "<B1-Motion>", "<ButtonRelease-1>"):
        cv.bind(ev, drag)
    redraw()
    return cv, redraw


class SettingsWindow:
    def __init__(self, cfg: dict, engine: Engine):
        self.cfg = cfg
        self.engine = engine
        self._thread = None

    def open(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="settings")
        self._thread.start()

    def _run(self):
        import tkinter as tk
        try:
            root = tk.Tk()
        except Exception:
            log("设置窗口创建失败: %s" % traceback.format_exc(limit=1))
            return

        PALS = {
            "dark": dict(bg="#12161B", card="#1A2029", fg="#E7EDF4", sub="#8B98A9",
                         track="#2B3440", accent="#4C93FF", accent_fg="#FFFFFF",
                         btn="#232B36", btn_hover="#2F3A48", btn_fg="#E7EDF4",
                         knob="#F2F6FA"),
            "light": dict(bg="#F7F8FA", card="#FFFFFF", fg="#1F2328", sub="#6B7280",
                          track="#DDE1E6", accent="#2563EB", accent_fg="#FFFFFF",
                          btn="#EEF1F4", btn_hover="#E2E7EC", btn_fg="#1F2328",
                          knob="#FFFFFF"),
        }
        FONT = ("Microsoft YaHei UI", 9)
        FONT_B = ("Microsoft YaHei UI", 10, "bold")
        FONT_S = ("Microsoft YaHei UI", 8)

        def mode_now() -> str:
            m = str(self.cfg.get("theme") or "auto").lower()
            if m in ("dark", "light"):
                return m
            return "light" if _windows_apps_light_theme() else "dark"

        root.title("SmoothScroll 参数设置")
        root.geometry("460x480")
        root.resizable(False, False)
        root.attributes("-topmost", True)

        scale_vars = {}
        redraws = []
        updaters = []
        flags = {"enable": self.engine.enabled.is_set(),
                 "invert": bool(self.cfg.get("invert"))}

        def collect():
            """把界面上当前的值收回 cfg（切主题重建界面时用，不写盘）。"""
            for k, v in scale_vars.items():
                self.cfg[k] = int(v.get()) if k == "duration_ms" else round(float(v.get()), 3)
            self.cfg["invert"] = bool(flags["invert"])
            flags["enable"] = bool(flags["enable"])

        def build():
            for w in root.winfo_children():
                w.destroy()
            scale_vars.clear()
            redraws.clear()
            updaters.clear()
            pal = PALS[mode_now()]
            root.configure(bg=pal["bg"])

            def divider(pady=(10, 0)):
                tk.Frame(root, bg=pal["track"], height=1).pack(fill="x", padx=20, pady=pady)

            def make_btn(parent, text, cmd, primary=False, width=10, font=FONT):
                b = tk.Button(parent, text=text, command=cmd, width=width,
                              font=font, relief="flat", bd=0, cursor="hand2",
                              bg=pal["accent"] if primary else pal["btn"],
                              fg=pal["accent_fg"] if primary else pal["btn_fg"],
                              activebackground=pal["accent"] if primary else pal["btn_hover"],
                              activeforeground=pal["accent_fg"] if primary else pal["btn_fg"],
                              highlightthickness=0, padx=10, pady=6)
                b.bind("<Enter>", lambda e: b.config(bg=pal["accent"] if primary else pal["btn_hover"]))
                b.bind("<Leave>", lambda e: b.config(bg=pal["accent"] if primary else pal["btn"]))
                return b

            head = tk.Frame(root, bg=pal["bg"])
            head.pack(fill="x", padx=20, pady=(16, 0))
            tk.Label(head, text="滚动参数", bg=pal["bg"], fg=pal["fg"],
                     font=FONT_B, anchor="w").pack(side="left")
            tk.Label(head, text="改完点“保存并应用”即时生效", bg=pal["bg"], fg=pal["sub"],
                     font=FONT).pack(side="right")

            # ---------------- 外观（主题切换）
            divider((10, 2))
            trow = tk.Frame(root, bg=pal["bg"])
            trow.pack(fill="x", padx=20, pady=(10, 2))
            tk.Label(trow, text="外观", bg=pal["bg"], fg=pal["fg"], font=FONT,
                     width=6, anchor="w").pack(side="left")

            def set_theme(m):
                collect()
                self.cfg["theme"] = m
                save_config(self.cfg)
                log("设置面板外观 -> %s" % m)
                build()

            cur = str(self.cfg.get("theme") or "auto").lower()
            for i, (text, m) in enumerate((("跟随系统", "auto"), ("深色", "dark"), ("浅色", "light"))):
                on = (cur == m)
                btn = tk.Button(trow, text=text, width=6, font=FONT, relief="flat", bd=0,
                                cursor="hand2", highlightthickness=0, padx=6, pady=3,
                                command=(lambda mm=m: set_theme(mm)),
                                bg=pal["accent"] if on else pal["card"],
                                fg=pal["accent_fg"] if on else pal["sub"],
                                activebackground=pal["accent"] if on else pal["btn_hover"],
                                activeforeground=pal["accent_fg"] if on else pal["fg"])
                btn.pack(side="left", padx=(0 if i == 0 else 6, 0))

            tk.Label(trow, text="跟随系统＝自动", bg=pal["bg"],
                     fg=pal["sub"], font=FONT_S).pack(side="left", padx=(14, 0))

            # ---------------- 滑杆
            divider((12, 2))

            def add_slider(parent, key, label, lo, hi, res):
                row = tk.Frame(parent, bg=pal["bg"])
                row.pack(fill="x", padx=20, pady=(10, 0))
                tk.Label(row, text=label, bg=pal["bg"], fg=pal["fg"], font=FONT,
                         width=13, anchor="w").pack(side="left")
                val = tk.DoubleVar(value=float(self.cfg.get(key)))
                lbl = tk.Label(row, text="", bg=pal["bg"], fg=pal["accent"], font=FONT,
                               width=6, anchor="e")
                lbl.pack(side="right")

                def on_move(v):
                    lbl.config(text=("%.2f" % v) if res < 1 else ("%d" % v))

                cv, redraw = _flat_slider(row, pal, val, lo, hi, res, on_move)
                cv.pack(side="left", padx=(0, 12))
                scale_vars[key] = val
                redraws.append(redraw)
                updaters.append(lambda k=key: on_move(scale_vars[k].get()))
                on_move(val.get())

            add_slider(root, "duration_ms", "平滑时长 (ms)", 60, 900, 10)
            add_slider(root, "momentum", "惯性(松手余量)", 0.0, 1.0, 0.05)
            add_slider(root, "sensitivity", "灵敏度", 0.3, 3.0, 0.05)
            add_slider(root, "granularity", "下发粒度 (格)", 0.05, 1.0, 0.05)

            divider((14, 4))

            opts = tk.Frame(root, bg=pal["bg"])
            opts.pack(fill="x", padx=20, pady=(8, 0))
            invert = tk.BooleanVar(value=bool(flags["invert"]))
            enable = tk.BooleanVar(value=bool(flags["enable"]))
            for text, var, key in (("反向滚动", invert, "invert"), ("启用平滑", enable, "enable")):
                def on_toggle(k=key, v=var):
                    flags[k] = bool(v.get())
                cb = tk.Checkbutton(opts, text=text, variable=var, bg=pal["bg"], fg=pal["fg"],
                                    font=FONT, activebackground=pal["bg"],
                                    activeforeground=pal["accent"], selectcolor=pal["card"],
                                    highlightthickness=0, bd=0, cursor="hand2",
                                    command=on_toggle)
                cb.pack(side="left", padx=(0, 20))

            tk.Label(root, text="提示：某些程序（游戏 / 远程桌面）对合成滚轮不响应，已列入 config.json 的 blacklist。",
                     wraplength=416, justify="left", bg=pal["bg"], fg=pal["sub"],
                     font=FONT_S).pack(fill="x", padx=20, pady=(12, 0))

            btns = tk.Frame(root, bg=pal["bg"])
            btns.pack(fill="x", padx=20, pady=16, side="bottom")

            def apply_all():
                collect()
                self.engine.set_enabled(bool(flags["enable"]))
                save_config(self.cfg)
                log("参数已应用: %s" % json.dumps(
                    {k: self.cfg[k] for k in ("duration_ms", "momentum", "sensitivity", "granularity")},
                    ensure_ascii=False))

            def restore():
                for k in ("duration_ms", "momentum", "sensitivity", "granularity"):
                    scale_vars[k].set(DEFAULTS[k])
                for f in redraws:
                    f()
                for f in updaters:
                    f()
                flags["invert"] = DEFAULTS["invert"]
                flags["enable"] = True
                invert.set(DEFAULTS["invert"])
                enable.set(True)

            make_btn(btns, "关闭", root.destroy, width=7).pack(side="right")
            make_btn(btns, "恢复默认", restore, width=9).pack(side="right", padx=(0, 8))
            make_btn(btns, "保存并应用", apply_all, primary=True, width=10).pack(side="right", padx=(0, 8))

            root.update_idletasks()
            if mode_now() == "dark":
                _set_dark_titlebar(root)      # 深色模式下把标题栏也染黑

        def on_close():
            try:
                root.destroy()
            except Exception:
                pass

        root.protocol("WM_DELETE_WINDOW", on_close)
        try:
            build()
            root.mainloop()
        except Exception:
            log("设置窗口异常: %s" % traceback.format_exc(limit=1))


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "SmoothScroll"


def _launch_command() -> str:
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = exe
    return '"%s" "%s"' % (pyw, os.path.abspath(__file__))


def autostart_command() -> str:
    """注册表里当前登记的自启命令（没登记就是空串）。"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, RUN_VALUE)
            return str(v or "")
    except OSError:
        return ""


def autostart_enabled() -> bool:
    return bool(autostart_command())


# Explorer 会在这里记"这条自启被用户在任务管理器里关掉了"。存在且带禁用位的条目
# 会让 Run 键失效——所以在写入自启时顺手把它清掉，重新开启才算真的开。
STARTUP_APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def _clear_startup_approved(name: str = RUN_VALUE) -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
            try:
                winreg.QueryValueEx(k, name)
            except FileNotFoundError:
                return                      # 没有标记 = 本来就是启用状态
            winreg.DeleteValue(k, name)
            log("已清除任务管理器里的自启禁用标记")
    except Exception:
        pass                                # 键不存在/无权限都无所谓


def _startup_suppressed(name: str = RUN_VALUE) -> bool:
    """任务管理器「启动」里被关掉过：Run 值还在，但 Explorer 写了禁用标记。
    该二进制值首字节 0x02 表示启用，其它（0x03/0x09…）表示禁用。"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED) as k:
            data, _ = winreg.QueryValueEx(k, name)
            return bool(data) and data[0] != 0x02
    except OSError:
        return False


# 这些目录随时会被清理，把 exe 放里面再开自启，重启后大概率找不到
_UNSTABLE_DIRS = ("\\temp\\", "\\downloads\\", "\\clipboxtemp\\", "\\chats\\",
                  "\\xiazaizhinlei\\", "\\desktop\\temp")


def _path_looks_unstable(cmd: str) -> bool:
    p = cmd.lower()
    return any(h in p for h in _UNSTABLE_DIRS)


def autostart_status() -> dict:
    """给托盘菜单/诊断命令用：当前是否开启、登记的命令、是否被系统压制。"""
    cmd = autostart_command()
    return {
        "on": bool(cmd),
        "command": cmd,
        "current": _launch_command(),
        "matches": cmd == _launch_command(),
        "suppressed": _startup_suppressed() if cmd else False,
        "unstable_path": _path_looks_unstable(cmd) if cmd else False,
    }


def set_autostart(on: bool) -> bool:
    """写入/删除自启项，写完回读校验；顺带清掉任务管理器的禁用标记。"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, _launch_command())
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
        if on:
            _clear_startup_approved()
            ok = autostart_command() == _launch_command()
            log("开机自启 -> True, 命令=%s, 校验=%s" % (_launch_command(), ok))
            return ok
        log("开机自启 -> False")
        return autostart_command() == ""
    except Exception:
        log("开机自启设置失败: %s" % traceback.format_exc(limit=1))
        return False


def _heal_autostart() -> None:
    """启动时自愈：开着自启但登记的路径不是当前程序（换过目录/改过名/从源码切到 exe），
    就按当前位置重写一遍，避免"开机启动没反应"。"""
    if not autostart_enabled():
        return
    cur = _launch_command()
    old = autostart_command()
    if old != cur:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, cur)
            _clear_startup_approved()
            log("自启路径已自愈: %s -> %s" % (old, cur))
        except Exception:
            log("自启路径自愈失败: %s" % traceback.format_exc(limit=1))


def run_tray(cfg: dict, engine: Engine, activator: "Activator" = None):
    import pystray
    from pystray import MenuItem as Item, Menu

    settings = SettingsWindow(cfg, engine)

    def toggle(icon, item):
        engine.set_enabled(not engine.enabled.is_set())
        log("平滑滚动 -> %s" % ("开启" if engine.enabled.is_set() else "关闭"))

    def reload_cfg(icon, item):
        new = load_config()
        cfg.clear()
        cfg.update(new)
        engine.set_enabled(bool(cfg.get("enabled", True)))
        log("配置已重新加载")

    def open_settings(icon, item):
        settings.open()

    def autostart_text(item):
        st = autostart_status()
        if not st["on"]:
            return "开机自启（未开启）"
        if st["suppressed"]:
            return "开机自启（被系统禁用）"
        if not st["matches"]:
            return "开机自启（路径已变，将自动修正）"
        return "开机自启（已开启）"

    def toggle_autostart(icon, item):
        want = not autostart_enabled()
        ok = set_autostart(want)
        icon.update_menu()
        if not want:
            return
        if not ok:
            try:
                icon.notify("自启写入没成功，可能被安全软件拦了。日志在配置文件夹里。",
                            "SmoothScroll")
            except Exception:
                pass
        elif _path_looks_unstable(_launch_command()):
            try:
                icon.notify("已开启。不过程序现在在临时目录里，重启后可能找不到它；"
                            "建议先把 exe 挪到固定文件夹（比如 D:\\Tools），再开一次自启。",
                            "SmoothScroll")
            except Exception:
                pass

    def open_folder(icon, item):
        try:
            os.startfile(APP_DIR)
        except Exception:
            pass

    def fix_autostart(icon, item):
        """把 exe 复制到 %LOCALAPPDATA%\\SmoothScroll（不会被清理的位置）并登记自启。"""
        if not getattr(sys, "frozen", False):
            try:
                icon.notify("当前是源码方式运行。建议先打包成 exe，再用这个功能。",
                            "SmoothScroll")
            except Exception:
                pass
            return
        try:
            import shutil
            os.makedirs(APP_DIR, exist_ok=True)
            dst = os.path.join(APP_DIR, "SmoothScroll.exe")
            if os.path.abspath(dst) != os.path.abspath(sys.executable):
                shutil.copy2(sys.executable, dst)
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, '"%s"' % dst)
            _clear_startup_approved()
            icon.update_menu()
            log("自启已修复：exe 固定到 %s" % dst)
            try:
                icon.notify("已复制到 %s 并登记开机自启。以后请用这个位置的程序。"
                            % dst, "SmoothScroll")
            except Exception:
                pass
        except Exception:
            log("修复自启失败: %s" % traceback.format_exc(limit=1))
            try:
                icon.notify("修复失败，详见日志。", "SmoothScroll")
            except Exception:
                pass

    def quit_app(icon, item):
        engine.stop()
        icon.stop()

    menu = Menu(
        Item("平滑滚动 (开)", toggle, checked=lambda i: engine.enabled.is_set()),
        Menu.SEPARATOR,
        Item("参数设置…", open_settings),
        Item("重新加载配置", reload_cfg),
        Item(autostart_text, toggle_autostart, checked=lambda i: autostart_enabled()),
        Item("修复自启（复制到固定位置）", fix_autostart),
        Item("打开配置文件夹", open_folder),
        Menu.SEPARATOR,
        Item("退出", quit_app),
    )
    icon = pystray.Icon(APP_NAME, make_icon_image(), "SmoothScroll 平滑滚动", menu)

    def activate():
        """有人又双击了一次启动：用托盘气泡提示，并直接把设置面板摆到他面前。"""
        try:
            icon.notify("我已在后台运行，不用再开第二个。已为你打开设置面板。",
                        "SmoothScroll")
        except Exception:
            log("气泡通知发送失败: %s" % traceback.format_exc(limit=1))
        settings.open()

    if activator is not None:
        activator.set_callback(activate)

    log("托盘已启动")
    icon.run()


# ---------------------------------------------------------------- 入口
def main():
    global _verbose, _trace, _t0
    args = sys.argv[1:]
    _verbose = True
    _t0 = time.perf_counter()
    if "--trace" in args:
        _trace = True

    test_seconds = None
    if "--test" in args:
        i = args.index("--test")
        test_seconds = float(args[i + 1]) if i + 1 < len(args) and args[i + 1].replace('.', '', 1).isdigit() else 8.0

    # ---- 自启诊断：--autostart status | on | off（结果同时写到日志和 status 文件）
    if "--autostart" in args:
        i = args.index("--autostart")
        sub = args[i + 1].lower() if i + 1 < len(args) else "status"
        if sub in ("on", "enable", "1"):
            set_autostart(True)
        elif sub in ("off", "disable", "0"):
            set_autostart(False)
        st = autostart_status()
        text = json.dumps(st, ensure_ascii=False, indent=2)
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            with open(os.path.join(APP_DIR, "autostart-status.txt"), "w",
                      encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass
        log("自启诊断: %s" % text.replace("\n", " "))
        print(text)
        return

    if test_seconds:
        # 自检模式不参与单例（否则没法在运行中的实例旁边做验证）
        cfg = load_config()
        engine = Engine(cfg, allow_injected=True)
        engine.start()
        time.sleep(0.4)   # 等钩子就绪
        log("自检模式 %.1fs ..." % test_seconds)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < test_seconds:
            time.sleep(0.2)
        log("自检统计: %s" % json.dumps(engine.stats, ensure_ascii=False))
        log("位移校验 target=%.1f pos=%.1f emitted=%.1f" %
            (engine.target, engine.pos, engine.emitted))
        _trace_dump(os.path.join(APP_DIR, "trace.csv"))
        engine.stop()
        time.sleep(0.3)
        return

    # ---- 单实例：已经有实例在跑就别再开一个，改为唤起它 ----
    guard = InstanceGuard()
    if not guard.is_primary():
        if guard.signal_primary():
            log("已有实例在运行，已请求它接管本次启动（本次不做任何事）")
            time.sleep(0.15)
        else:
            log("已有实例在运行，但它没有响应激活请求（可能刚退出），请稍后重试")
        return

    cfg = load_config()
    _heal_autostart()          # 开过自启但路径变了（换目录/改名/源码→exe）就自动修正
    engine = Engine(cfg)
    engine.start()
    time.sleep(0.4)   # 等钩子就绪

    activator = Activator()
    guard.listen(activator.fire)
    try:
        run_tray(cfg, engine, activator)
    finally:
        guard.close()
        engine.stop()
        log("退出")


if __name__ == "__main__":
    main()
