# 架构与二次开发说明

面向想改编这个程序的人。读完这一篇你大概能知道：消息从哪进、被谁处理、参数在哪个公式里、改哪一行会有什么效果、以及哪些坑别踩。

---

## 1. 整体数据流

```
物理鼠标滚轮
   │  ① 系统消息 WM_MOUSEWHEEL
   ▼
WH_MOUSE_LL 低级钩子（钩子线程）
   │  ② 认出是自己发的事件 / 黑名单程序 → 原样放行
   │  ③ 其余一律 return 1 吞掉，不交给目标窗口
   ▼
Engine.feed(delta)
   │  ④ target += delta × 灵敏度，估算"用户滚轮速率"
   ▼
动画线程 Engine._anim_loop（约每 4ms 一轮）
   │  ⑤ 临界阻尼弹簧 / 松手补间 → 算出这一帧的 pos 与 vel
   │  ⑥ 差值 rem = pos − emitted，按自适应粒度切成整数段
   ▼
send_wheel(delta) → SendInput
   │  ⑦ 事件带 dwExtraInfo = SIG 签名，回到钩子时被 ② 认出来放行
   ▼
目标窗口真正滚动
```

关键设计：**永远只下发整数**（滚轮 delta 是 16 位整数），所以 `emitted` 记的是"已经真实发出去多少"，`pos` 是"应该到哪"，两者的差就是这一帧要发的小段。这样无论怎么细分，总量都不会漂。

---

## 2. 代码分区

单文件 `SmoothScroll.py`，按注释分隔线分段，用这些关键字跳转：

| 分段 | 锚点关键字 | 内容 |
| --- | --- | --- |
| 配置默认值 | `DEFAULTS = {` | 所有参数与默认值；新增参数从这里开始 |
| Win32 常量 | `WH_MOUSE_LL =` | 常量、`SIG` 签名、错误码 |
| 单实例 | `class Activator` / `class InstanceGuard` | 命名互斥体判重 + 命名事件唤起的封装 |
| 结构体 | `class MSLLHOOKSTRUCT` | `POINT` / `MSLLHOOKSTRUCT` / `MOUSEINPUT` / `INPUT` |
| 基础工具 | `def log(msg` | 带跨进程互斥锁的日志、`send_wheel`、`process_name_at`、配置读写、`_trace_add/_trace_dump` |
| 核心引擎 | `class Engine` | 钩子回调、`feed`、`_anim_loop`、钩子线程、启停 |
| 托盘 + 设置界面 | `def make_icon_image` / `class SettingsWindow` | 图标解码、主题读取、自绘滑杆 `_flat_slider`、设置面板 |
| 开机自启 / 托盘 | `def set_autostart` / `def run_tray` | 注册表 Run 项、托盘菜单 |
| 入口 | `def main()` | 参数解析（`--test` / `--trace`）、单例、启动 |

---

## 3. Engine 的状态变量

| 变量 | 含义 | 单位 |
| --- | --- | --- |
| `target` | 目标位移。收到滚轮就累加，只增不减（除反向） | 滚轮单位，1 格 = 120 |
| `pos` | 虚拟位置，弹簧算出来的"当前应该在哪" | 同上 |
| `vel` | 虚拟速度，正负号代表方向 | 单位/秒 |
| `emitted` | 已经**真实下发**的累计量（整数之和） | 同上 |
| `last_input_t` | 最后一次收到滚轮的时刻，用于判断"松手" | `perf_counter` 秒 |
| `wheel_rate` | 用户滚轮速率估算，EMA：新值占 0.3 | 单位/秒 |
| `_rel` | 松手补间状态 `(t0, p0, 剩余距离, 时长)`；来新输入就清空 | — |
| `stats` | 自检统计 `raw/swallowed/sent_calls/sent_units/skip_blacklist` | — |

**为什么 `pos` 与 `emitted` 两个都要**：pos 是连续的理想轨迹，emitted 是离散的已发送量。每帧只发 `round(pos - emitted)`，四舍五入的误差会留在两者差里、下一帧继续补，所以长时间滚动也不会累积漂移。

---

## 4. 数学模型（调手感的全部旋钮都在这里）

### 4.1 跟随阶段（有滚轮输入，或松手 50ms 内）

```python
w  = 4.7 / duration            # 固有角频率 ω，duration = duration_ms/1000
lag_ref = 120 * 0.75           # 允许的稳态落后，单位（这里是 0.75 格）
w_eff  = min(250, max(w, 2*rate/lag_ref))
k      = w_eff ** 2            # 刚度
d      = 2 * w_eff             # 阻尼，取 2ω 即阻尼比 ζ=1（临界阻尼，不过冲）
v_cap  = max(700, rate)        # 速度上限 = 你自己的滚轮速率
```

- 阻尼比 1 的意义：**不过冲、不振荡**，尾段自然减速。这是"平滑"的来源。
- `2*rate/lag_ref` 这段：临界阻尼的稳态落后是 `2v/ω`，ω 固定时滚得越快落后越多（实测 30 格/秒能攒 3.9 格）。手一停这段要一次性补走，用户感知就是"临停又猛冲一下"。所以 ω 跟着用户速率抬，把落后压在 0.75 格内。
- `v_cap`：弹簧追目标时速度会超过用户的手速，超过就是那一下"冲"。钳到 `rate` 后，速度不会比手更快。
- 积分用固定子步 `h = dt/n, n = ceil(dt/0.002)`（上限 25 步），避免帧间隔大时数值发散。

### 4.2 松手阶段（超过 50ms 没有新滚轮）

```python
T = clamp(3 * |剩余距离| / max(80, |v|), 0.06, 0.50) * (1 + 1.2 * momentum)
p = p0 + d0 * (1 - (1-u)^3)     # 三次 ease-out
v = d0 * 3 * (1-u)^2 / T        # 导数解析求出，速度只降不升
u = (now - t0) / T
```

- 用解析曲线而不是继续跑弹簧，是为了**保证速度单调下降**——弹簧在减速阶段可能因为追赶而反冲。
- `momentum` 只在这里出现：把它理解为"收尾那段有多绵长"。0 → 立刻刹；1 → 明显滑一段。

### 4.3 下发

```python
settled = |target - pos| < 0.6 and |vel| < 8        # 认为已经停稳
gran = min(granularity*120, max(1.0, |v|*0.012))    # 自适应粒度
rem  = pos - emitted
if |rem| >= gran or (settled and |rem| >= 0.5):
    sent = round(rem)，单次上限 ±720（6 格）
```

- `granularity` 只是**上限**。`|v|*0.012` 让下发间隔稳定在约 12ms：速度快就一次多发点，快停时退化到 1 个滚轮单位一小步，减速看起来是连续的。
- `settled` 时把 `pos` 直接对齐 `target`，并补发残余（哪怕小于粒度），保证"输入多少还你多少"。

### 4.4 自检判定标准

日志里 `位移校验 target=… pos=… emitted=…` 三者应基本相等；`sanitens` 类的 `sent_units` 应等于注入总量。不相等就说明有漂移或丢步。

---

## 5. 单实例机制

| 对象 | 类型 | 作用 |
| --- | --- | --- |
| `Local\SmoothScroll.SingleInstance` | 命名互斥体 | 判重。`CreateMutexW` 后检查 `GetLastError()==183` 即已有实例 |
| `Local\SmoothScroll.Activate` | 命名事件 | 第二个进程 `SetEvent` 通知主实例"有人又点了一次" |
| `Local\SmoothScroll.LogLock` | 命名互斥体 | 日志写入互斥，避免两进程行写花 |

`InstanceGuard.listen()` 起一个线程 `WaitForSingleObject` 等事件，触发时回调 → 主实例弹**托盘气泡**（`pystray` 的 `icon.notify`）+ 打开设置面板。第二个进程通知完就 `exit(0)`，不弹对话框。

`InstanceGuard(suffix="…")` 可以在名字后面加后缀，得到一套独立的互斥体/事件（默认空后缀）。`--test` 自检模式在 `main()` 里直接走独立分支，根本不创建 `InstanceGuard`，所以能和正式实例同时跑。

---

## 6. 二次开发常见改法

### 6.1 加一个参数到设置面板

三步，都在同一个文件里：

1. `DEFAULTS` 里加键与默认值（类型决定 `load_config` 怎么校验：bool / int / float / list / str）。
2. `SettingsWindow._run()` 的 `build()` 里加一行
   `add_slider(root, "新键", "显示名", 最小值, 最大值, 步长)`，
   非数值就仿照 `反向滚动` 那段加 `tk.Checkbutton`。
3. 引擎里 `self.cfg.get("新键", 默认)` 读取即可。

`collect()` 会自动把滑杆值写回 `self.cfg`，不用手写映射。

### 6.2 调手感（不建议动公式，先动参数）

| 想要的效果 | 改法 |
| --- | --- |
| 更跟手、更硬 | `duration_ms` 降到 180~220 |
| 更绵、更"滑" | `duration_ms` 抬到 400~500 |
| 收尾更长 | `momentum` 抬到 0.6~0.8 |
| 收尾几乎立刻停 | `momentum` 到 0 |
| 整体滚得更多 | `sensitivity` 1.2~1.5 |
| 某程序滚不动 | `granularity` 改 1.0 |
| 松手后滑行距离还想更短 | 把 `lag_ref = WHEEL_DELTA * 0.75` 调成 0.6 格，同时 `active` 判定窗口 `0.05` 可降到 0.03 |

### 6.3 换缓动曲线

`_anim_loop` 的 `else`（松手）分支里 `p` 和 `v` 两行。任何 `p(u)` 都可以，只要 `p(0)=p0`、`p(1)=p0+d0`，并且 `v` 用 `p` 的解析导数（否则速度和位置会对不上，出现一顿一顿）。想加"回弹"就把 `p` 换成 back-out 曲线，但要知道 `v` 会变号。

### 6.4 加横向滚动

- 钩子回调里再判 `WM_MOUSEHWHEEL`（`0x020E`），把 delta 走同一套 `feed`，但需要给 Engine 加一个独立通道（`target_x/pos_x/emitted_x`），因为横纵不能共用一个累加器。
- `send_wheel(delta, horizontal=True)` 已经支持（`dwFlags = 0x01000`，即 `MOUSEEVENTF_HWHEEL`）。
- 注意方向：横向滚 Windows 的正负语义与纵向相反，一般要取反。

### 6.5 换图标

1. 准备一张 1024×1024 PNG 母版。
2. 生成多尺寸 ico（Pillow）：

```python
from PIL import Image
im = Image.open("master.png").convert("RGBA").resize((1024, 1024))
im.save("assets/SmoothScroll.ico", format="ICO",
        sizes=[(256,256),(128,128),(64,64),(48,48),(32,32),(24,24),(16,16)])
```

3. 托盘图标是内嵌的 `ICON_PNG_B64` 常量（64×64）。用下面这段把它换掉（只替换常量，不动 `make_icon_image()`）：

```python
import base64, io, re, textwrap
from PIL import Image

b = io.BytesIO()
Image.open("master.png").convert("RGBA").resize((64, 64)).save(b, "PNG", optimize=True)
b64 = "\n".join('    "%s"' % l for l in textwrap.wrap(base64.b64encode(b.getvalue()).decode(), 100))
src = open("SmoothScroll.py", encoding="utf-8").read()
src = re.sub(r"ICON_PNG_B64 = \(\n.*?\n\)\n", "ICON_PNG_B64 = (\n%s\n)\n" % b64, src, flags=re.S)
open("SmoothScroll.py", "w", encoding="utf-8").write(src)
```

4. `build.bat` 重新打包（`--icon` 已指向 `assets\SmoothScroll.ico`）。

### 6.6 加托盘菜单项

`run_tray()` 里的 `Menu(...)`，加一行 `Item("标题", 回调, checked=lambda i: 状态)`。回调签名是 `(icon, item)`；改完状态记得 `icon.update_menu()` 才会重画勾选。

---

## 7. 坑与限制（都踩过）

| 坑 | 现象 | 说明 / 对策 |
| --- | --- | --- |
| 锁屏 / 休眠 | `SendInput` 全部失败，日志 `raw: 0`、`sendinput_fail=15` | 会话被锁时无法注入输入，属系统限制。做端到端测试前先确认桌面是解锁的 |
| UIPI | 任务管理器等管理员窗口里滚不动 | 低权限进程不能向高权限窗口注入。要以管理员身份运行才能覆盖 |
| 游戏 / 远程桌面 | 接管了也没反应 | 常用 Raw Input 或直通输入，合成事件不生效。加进 `blacklist` |
| onefile 双进程 | 任务管理器里看到两个 `SmoothScroll.exe` | PyInstaller onefile 的 bootloader 父进程 + 真身，正常现象。**只杀父进程，子进程仍持锁**，要杀就两个都杀 |
| 钩子线程不能慢 | 全局鼠标卡顿 | `_hook_proc` 里只做常量时间的事；`process_name_at` 已经做了快速失败，别在里面加 IO |
| `GetModuleHandleW` 不设 `restype` | `hook failed 0` | 64 位下句柄被截断成 32 位。`SetWindowsHookExW` 的 hmod 直接传 `None` 最省事 |
| 两进程同时写日志 | 行写花 | 用 `Local\SmoothScroll.LogLock` 互斥，见 `log()` |
| DWM 深色标题栏 | 白标题栏配黑窗口 | `_set_dark_titlebar()` 用属性号 20（`DWMWA_USE_IMMERSIVE_DARK_MODE`），Win10 1809+ 有效 |
| tkinter 原生控件是系统灰 | 深色面板上刺眼 | 这就是为什么滑杆用 Canvas 自绘（`_flat_slider`），别退回 `tk.Scale` |
| 打包路径 | `--icon` 相对路径失效 | `build.bat` 里先 `cd /d "%~dp0"`，PyInstaller 用相对路径才稳 |
| Run 键写了但开机不启动 | 注册表里有值，登录后没进程 | 三种可能：① exe 被移动过，命令指向旧路径；② 用户在「任务管理器 → 启动」里禁用过，Explorer 在 `HKCU\...\Explorer\StartupApproved\Run` 写了首字节非 `0x02` 的二进制标记，它优先级高于 Run 键；③ 安全软件拦截。`_heal_autostart()` 负责 ①，`_clear_startup_approved()` 负责 ② |
| 读 StartupApproved 报权限错 | 删除禁用标记没生效 | 该键既要读又要写，`OpenKey` 必须传 `KEY_READ \| KEY_SET_VALUE`；只传 `KEY_SET_VALUE` 会在 `QueryValueEx` 抛异常被 `except` 吞掉，表现为"清了但没清" |

---

## 8. 自检流程（改完公式务必跑一遍）

```powershell
# 1) 起引擎（自检模式，10 秒后自动退出并打印统计）
python SmoothScroll.py --test 10 --trace

# 2) 另一个终端注入 15 格、每格 40ms、启动延迟 0.8s（模拟"快速滚完急停"）
python selftest_inject.py 15 0.04 0.8

# 3) 看结论
#    日志：%LOCALAPPDATA%\SmoothScroll\smoothscroll.log
#    轨迹：%LOCALAPPDATA%\SmoothScroll\trace.csv
```

`trace.csv` 列含义：

| 列 | 含义 |
| --- | --- |
| `w` | 收到了一个原始滚轮事件（delta、dwExtraInfo、flags） |
| `f` | 一帧动画（dt、target、pos、vel、本次下发量、帧前剩余量） |

判定"尾段没有冲量"的标准：松手后速度序列**单调不增**，且首个峰值不超过松手瞬间速度。总量看 `sent_units` 是否等于注入总和（15 格 = 1800）。
