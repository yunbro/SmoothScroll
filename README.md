# SmoothScroll for Windows

一个 Windows 版「平滑滚动 + 惯性动量」小工具，功能对标 SmoothScroll：把鼠标滚轮一格一格的跳跃，变成连续的滚动动画，并带一点滑行惯性。

- `SmoothScroll.py` —— 源码（标准库实现核心，托盘用 pystray/Pillow）
- `selftest_inject.py` —— 自检用的小工具，向系统注入滚轮事件
- `assets\SmoothScroll.ico` —— 程序图标（深色圆角底 + 白色鼠标（绿色滚轮）+ S 形下落箭头，多尺寸 16~256）
- `assets\icon_s_down.ico` / `icon_mouse_trail.ico` / `icon_mouse_s.ico` / `icon_wheel2.ico` —— 备选图标方案，想换就替换 `SmoothScroll.ico` 后重新打包
- `build.bat` —— 一键打包成单文件 exe
- `LICENSE` —— MIT 许可
- `docs\ARCHITECTURE.md` —— **架构与二次开发说明**（数据流、公式、调参旋钮、改法、坑）

## 目录结构

```
SmoothScroll/
├─ SmoothScroll.py        # 全部程序逻辑（单文件，约 1500 行）
├─ selftest_inject.py     # 自检用：向系统注入滚轮事件
├─ requirements.txt       # 运行时依赖：pystray、Pillow
├─ requirements-dev.txt   # 打包依赖：PyInstaller
├─ build.bat              # Windows 一键打包
├─ assets/                # 图标（多尺寸 ico）
├─ docs/
│  └─ ARCHITECTURE.md     # 架构与二次开发说明
├─ LICENSE                # MIT
├─ README.md
└─ .gitignore
```

## 环境要求

- Windows 10 1809 及以上（用到深色标题栏属性；Win11 更佳）
- Python 3.9+（开发在 3.12 上验证）
- 运行：`pip install -r requirements.txt`；打包：`pip install -r requirements-dev.txt`

> 仓库里没有放 `dist/` 和 `build/`，`config.json`、`smoothscroll.log`、`trace.csv` 都是运行时在本机生成的，已在 `.gitignore` 里排除。打包好的 exe 建议走 Releases 而不是提交进仓库。

---

## 怎么用

**免安装版**：把 `dist\SmoothScroll.exe` 放到任意目录，双击运行，托盘出现图标即生效，无需管理员权限（想覆盖管理员窗口的话才需要提权）。

**源码版**：`pythonw SmoothScroll.py`（要后台常驻就用 `pythonw` 而不是 `python`，否则会挂一个黑窗口）。

托盘图标右键菜单：

| 菜单项 | 说明 |
| --- | --- |
| 平滑滚动 (开) | 勾选/取消，即时开关接管 |
| 参数设置… | 打开带滑杆的设置面板，改完「保存并应用」立即生效 |
| 重新加载配置 | 手动改过 config.json 后不用退出程序 |
| 开机自启 | 勾选后写入 `HKCU\...\CurrentVersion\Run` |
| 打开配置文件夹 | 定位到 `%LOCALAPPDATA%\SmoothScroll` |
| 退出 | 停止接管并退出 |

配置文件：`%LOCALAPPDATA%\SmoothScroll\config.json`
运行日志：`%LOCALAPPDATA%\SmoothScroll\smoothscroll.log`

---

## 重复启动会怎样

只允许跑一个实例。再次双击启动时，第二个进程**不会**开出第二个托盘图标，也不会弹任何对话框，而是：

1. 通过命名事件通知已经在跑的那个实例；
2. 主实例弹一个 Windows 托盘气泡：「我已在后台运行，不用再开第二个。已为你打开设置面板。」；
3. 顺手把设置面板摆到面前，然后第二个进程自己安静退出（退出码 0）。

实现上用的是 `Local\SmoothScroll.SingleInstance` 命名互斥体判重，`Local\SmoothScroll.Activate` 命名事件做唤起通道。互斥体是内核对象，进程退出（包括被强制结束）就自动释放，所以不会留下"僵尸锁"导致再也启动不了。

如果主实例卡住不响应，任务管理器里结束 `SmoothScroll.exe` 再启动即可。

> 注：`--test` 自检模式不参与单例，方便在实例运行的同时做验证。

---

## 参数怎么调

| 参数 | 默认 | 作用 / 调法 |
| --- | --- | --- |
| `duration_ms` | 300 | 一次滚动走完的时长。调小更跟手（180~220），调大更绵（400~500） |
| `momentum` | 0.35 | 惯性：决定"手停下以后那一段收尾"有多绵长。0 = 立刻刹住，1 = 明显滑行 |
| `sensitivity` | 1.0 | 每次滚轮的实际滚动量倍率。觉得整体太慢就调 1.2~1.5 |
| `granularity` | 0.1 | 单次下发的**上限**粒度，单位「格」。0.1 = 最多拆成 1/10 格一段；**1.0 = 整格下发，兼容性最好**。尾段会自动细化（见下） |
| `invert` | false | 反向滚动 |
| `theme` | "auto" | 设置面板外观：`auto` 跟随 Windows 深浅色 / `dark` / `light` |
| `blacklist` | `game.exe` / `mstsc.exe` | 这些进程里完全不接管，滚轮原样交给它 |

调参建议：先只动 `duration_ms` 和 `momentum`，这两个决定 90% 的手感；`granularity` 只在遇到「某个程序里滚动变慢或滚不动」时才调。

**末段不再冲一下**：临界阻尼弹簧的"落后量"和速度成正比（稳态落后 = 2v/ω）。固定 ω 时滚得越快，落后越多——实测 30 格/秒时攒下 3.9 格；手一停，这 3.9 格就得一次性补走，看起来就是"快要停了又猛冲一段"。现在两级处理：① 跟随刚度按你自己的滚轮速率来抬，从第一格就跟得住，落后量始终压在 1 格以内，不需要事后追赶；② 松手（50ms 无新滚轮）后改用三次 ease-out 补间，速度只降不升、精确走完剩下的距离，不会再出现末段加速。

**尾段自适应粒度**：滑行快结束时速度很低，如果还按固定粒度（比如 1/10 格）攒够才发，会出现"停一下、再突然跳一下"的顿挫。所以下发阈值会跟着当前速度自动变细：快的时候按配置粒度走，慢下来时粒度一路降到 1 个滚轮单位，等价于每 8~15ms 发一小步，减速到停是连续的。`granularity` 只是这个阈值的上限。

---

## 已知限制（实话实说）

1. **游戏和远程桌面**：这类程序走原生的 Raw Input 或直通输入，合成的滚轮事件不生效，所以我们直接放行进 `blacklist`，不接管更安全。
2. **管理员权限窗口**（任务管理器等）：Windows 的 UIPI 会拦截低权限进程向高权限窗口注入输入。想覆盖这些窗口，需要以管理员身份运行本程序。
3. **少数旧程序只认整格**：如果某个程序里滚轮变成「很慢」或「完全不动」，把 `granularity` 改成 `1.0` 即可（兼容性优先，顺滑度下降）。
4. 触控板/精滚鼠标的 `WM_MOUSEHWHEEL`（横向滚）不接管，双向滚动只处理纵向。
5. 原理上是「吞掉原始事件 → 自己补发」，所以本程序退出前一直占用滚轮；如果进程崩溃，滚轮反而会恢复正常，不会卡死。

---

## 自检 / 二次开发

```powershell
# 自检模式：启动钩子，8 秒后打印统计（吞掉了多少、补发了多少、总量是否守恒）
python SmoothScroll.py --test 8

# 加上 --trace 会把每一帧的 target/pos/vel/单次下发量落盘成 %LOCALAPPDATA%\SmoothScroll\trace.csv
python SmoothScroll.py --test 10 --trace

# 配合注入脚本验证（--test 模式下才放行合成事件，正常运行时只认真实滚轮）
python selftest_inject.py 3 0.06
```

自检日志会出现类似：

```
自检统计: {"raw": 3, "swallowed": 3, "sent_calls": 28, "sent_units": -391}
位移校验 target=-391.5 pos=-391.5 emitted=-391.0
```

`target` / `pos` / `emitted` 三者应当基本相等 —— 相等说明「输入多少就还你多少」，没有漂移。

重新打包（两种方式任选）：

```bat
:: 方式一：双击 build.bat，自动装依赖并打包
build.bat
```

```powershell
:: 方式二：手动
python -m pip install pystray pillow pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name SmoothScroll `
  --icon assets\SmoothScroll.ico --distpath dist --workpath build --specpath build `
  SmoothScroll.py
```

改公式、加参数、换图标、加横向滚动等具体做法，见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

---

## 变更记录

- **v8** 设置面板细节：滑杆改为自绘（已选区间整段填满强调色 + 白色圆形滑块，一眼看出当前值在哪），外观区加「跟随系统 / 深色 / 浅色」三档切换，点了立即换肤不用关窗口；主题选择存进 config.json 的 `theme` 字段。
- **v7** 设置面板重做：不再用系统灰底，改为跟随 Windows「应用主题」自动切深/浅两套配色（深色下连标题栏一起染黑），滑杆数值用强调色右对齐、按钮扁平化带悬停反馈。同时修掉松手瞬间的速度反弹——滚动过程中速度上限锁在"你自己的滚轮速率"，实测 15 格急停松手后余量 1.24 格、速度全程只降不升。
- **v6** 图标再定稿：把"鼠标 + 滚轮"和"S 形流动"合到一起——深色圆角底上一只白色鼠标（绿色滚轮作点睛），底部接一条 S 曲线落成粗箭头，一眼能看出"鼠标滚动"和"往下平滑"。同步更新 exe 内嵌图标、托盘图标、`assets\SmoothScroll.ico`。
- **v5** 换图标：深色圆角底 + 白色 S 形向下丝带箭头（既读得出"S"，也明确指向滚动方向）。托盘图标改为内嵌 base64 PNG，与 exe 图标一致，单文件版不再有"托盘和 exe 长得不一样"的问题。
- **v4** 修复"滚动马上停止时再猛冲一下"：根因是弹簧落后量随速度线性增长（30 格/秒攒到 3.9 格 = 470 单位），松手后被一次性补走。改为「刚度按滚轮速率自适应（落后量压在 1 格内）+ 松手后单调 ease-out 收尾」；惯性参数语义随之改为"收尾绵长度"。离线仿真：15 格急停后滑行从 3.9 格降到 1.2 格，且速度全程单调下降。
- **v3** 修复"滚动刚要停时突然跳一下"：下发阈值改为随速度自适应（尾段细化到 1 个滚轮单位）。实测尾段单步从 12 单位/200ms 收敛到 1~2 单位/≤60ms，总量守恒不变。
- **v2** 单实例运行：重复启动不再开第二个托盘图标，改为托盘气泡提示 + 打开设置面板；日志写入加跨进程锁，避免两进程同时写花。
- **v1** 首版：全局平滑滚动 + 惯性动量、托盘菜单、参数面板、开机自启、免安装 exe。

---

## 实现原理（一百字）

用 `WH_MOUSE_LL` 低级鼠标钩子拦住全局滚轮消息并吞掉，把位移累积成「目标位置」；动画线程用临界阻尼二阶系统（弹簧-阻尼，阻尼比正好 1，不过冲）逐帧逼近目标，每帧把差值切成若干小段用 `SendInput` 注入回去。阻尼比 1 的响应本身就是「平滑 + 尾段自然减速」，把惯性调到 0 时它退化成一条纯缓动曲线；自己注入的事件带 `dwExtraInfo` 签名，钩子见到就放行，不会递归。

---

## 许可

MIT License，见 [LICENSE](LICENSE)。可以自由使用、修改、分发、商用，保留版权声明即可。

本项目大量使用 Windows 未公开文档化的输入注入方式（`WH_MOUSE_LL` + `SendInput`），在部分程序（游戏、远程桌面、管理员窗口）中不生效，属已知限制，见上文「已知限制」。
