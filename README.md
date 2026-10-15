# KakaCat

一个基于 **CustomTkinter** 的现代化 HLS 视频下载器（Windows 桌面应用）：输入 M3U8
播放列表链接，程序获取并解析播放列表、并发下载视频分片，最后合并成完整的视频文件。

蓝黑科技风深色主题、实时进度、可滚动运行日志，所有文字配色满足 WCAG AA；
下载核心是独立可测试的纯 Python 函数，不依赖任何界面框架。

> **合规声明**：本工具仅用于下载**你本人有权访问和下载**的视频资源。
> 程序**不会**绕过付费墙、破解 DRM、规避访问控制或伪造授权签名。
> 支持标准的整段 **AES-128** 加密：密钥地址由播放列表自己给出，访问权限与分片
> 完全相同，程序只是照做，不猜测也不推导密钥。

---

## 一、功能简介

- 通过图形界面输入 M3U8 链接，支持可选的保存目录与输出文件名。
- 分片支持**多路并发下载**（默认 4 路，可调 1/2/4/8/16），结果始终按播放列表顺序合并。
- **断点续传**：失败或取消后保留已下好的分片，重新开始同一任务时自动跳过，从中断处继续。
- 实时显示**任务阶段、进度百分比、已完成/总分片数、下载速度、预计剩余时间**。
- 支持随时取消下载；关闭续传时失败或取消会自动清理临时分片文件。
- 临时分片默认放在**输出文件所在的目录**下，不会占用系统盘。
- 运行日志可滚动、可清空，包含下载阶段、进度、警告与错误信息（日志不会输出完整敏感 URL）。
- 已有输出文件默认受保护，需要显式允许才会覆盖。
- 界面自适应：窗口足够宽时左右双栏，变窄时自动切换为单栏堆叠（阈值按逻辑
  像素换算，高 DPI 缩放下同样准确）。
- 布局：标题栏 / 设置区 / **横跨整窗的下载状态带** / 运行日志；「开始下载」
  固定在设置区下方，窗口再矮也不用翻找。
- **在线播放**：不下载、不落盘，直接把链接交给外部播放器边取流边播放；点「播放」
  前会先拉一次播放列表做预检，把分片数、总时长、加密情况显示出来。在线播放与
  下载互不阻塞，可以同时进行。
- **进度条与倍速**：装了 **mpv** 时，在**播放器窗口里**拖动进度条跳转，用面板上的
  `−` / `＋` 按钮或在窗口里按 `[` / `]` 调速（都实时生效，不用重启播放器）。播放器
  按 mpv 优先、ffplay 兜底挑选；用 ffplay 时这些都没有，面板上会说明原因。
- **复制播放地址**：一键把按可选参数签名后的播放列表地址放到剪贴板。该地址
  在有效期内是自包含的，可以直接粘到手机上的 VLC / mpv 等播放器播放。
- 输出文件名默认用 **`.mp4`**（更好播）；没检测到 FFmpeg 时自动退回 `.ts`。
- 下载核心 `download_hls_video()` 与界面完全解耦，可被脚本调用或用 Mock 测试。

## 二、支持的播放列表类型

| 类型 | 是否支持 | 说明 |
| --- | --- | --- |
| 普通 HLS **VOD** 媒体播放列表（media playlist） | ✅ | 必须包含 `#EXT-X-ENDLIST` |
| MPEG-TS 分片（`.ts`） | ✅ | 直接二进制拼接，不需要 FFmpeg |
| fMP4 / CMAF 分片（`.m4s`、`.mp4`） | ✅ | 存在 `#EXT-X-MAP` 时会把初始化分片放在最前面 |
| 整段 `AES-128` 加密（`METHOD=AES-128`） | ✅ | 密钥地址取自播放列表；按 RFC 8216 推导 IV 并逐片解密 |
| 主播放列表（master playlist） | ❌ | 会明确提示需要具体码率的媒体列表 |
| 直播 / 尚未结束的播放列表 | ❌ | 会明确提示当前版本只支持点播 |
| `SAMPLE-AES` / `SAMPLE-AES-CTR` 等其它加密方式 | ❌ | 当前只实现了整段 AES-128，其余会明确报错 |
| `#EXT-X-BYTERANGE` 字节范围分片 | ❌ | 会明确提示不支持 |
| 纯音频播放列表 | ❌ | 会明确提示当前版本仅支持视频 |
| 字幕 / 缩略图等非视频分片（`.vtt`、`.srt`、`.jpg`…） | ❌ | 会明确提示仅支持视频，不会把文本当成视频下载后报成功 |

`#EXT-X-KEY:METHOD=NONE` 视为未加密，正常处理；它也会解除此前声明的密钥。

> 上表是**下载**的能力边界。**在线播放**比下载宽松：主播放列表、直播列表，
> 以及上面标 ❌ 的加密方式与分片类型，在线播放都会交给播放器去试，界面上只给出
> 提示而不拒绝——播放能力本来就强于下载。
> 在线播放只有三种情况会直接报错：链接无效、取不到播放列表、播放列表格式本身错误。

## 三、当前版本的功能限制

1. 只支持点播（VOD）列表，不支持直播录制。
2. 加密方面只实现了标准的整段 `AES-128`；其余加密方式（如 `SAMPLE-AES`）
   会明确报错。fMP4 的初始化分片（`#EXT-X-MAP`）不参与解密。
3. 分片并发下载（默认 4 路）；单个分片失败会有限重试（默认 4 次，退避
   1/2/4/8 秒），不会无限重试。重试仍失败则整条任务失败——不会跳过缺失分片
   假装成功。
4. 断点续传假设「同一链接 + 同一输出文件 + 同一份播放列表 ⇒ 同一份内容」。
   三者中任意一项变化，都会重新下载全部（或剩余）分片，不会混用旧分片。
5. 可选参数 A 用作授权密钥，只会为**播放列表请求**生成签名
   （`?wsSecret=…&wsTime=…`）；分片与密钥地址按播放列表给出的原样请求。
   密钥只参与本地签名计算，不会写入日志，也不会拼接到请求头或 Cookie。
   留空则按原始链接请求。
6. 可选参数 B 是该服务的基础地址。填写后（且参数 A 一并填写）会按固定模板
   `/mu/{资源段}/hls/1/index.m3u8` 从「视频 URL」里的下载链接推导真实媒体
   播放列表地址，资源段取链接里第一个含 `__` 的路径段；只填 B 不填 A 时
   不起作用。
7. 不接入任何真实业务接口（见 `hls_downloader/interfaces.py` 中的占位实现）。
8. 在线播放依赖外部的 **ffplay**（与 FFmpeg 同源）。在线播放时程序自身不写任何
   文件、不产出成品，分片由播放器在线取流后自行丢弃；因此在线播放**不会**加速
   也不会影响下载任务。
9. 在线播放时签名后的完整 URL 会作为**命令行参数**传给播放器，因此它在本机进程
   列表中可见。程序日志里仍然只输出脱敏后的 URL。

## 四、环境要求

- **Python 3.11 或更高版本**（开发与验证环境为 Python 3.11.9）
- Windows 10 / 11（其他平台理论上可用，但未做验证）
- FFmpeg：**可选**。只有需要把 TS 分片封装成 `.mp4` 时才需要。
- mpv：**可选但推荐**。装了它在线播放才有**进度条与倍速**（走 JSON IPC 控制）。
- ffplay：**可选**。随 FFmpeg 一起分发，在同一 `bin` 目录里；没有 mpv 时用它
  兜底，只能基本播放，没有进度条与倍速。

## 五、依赖安装

建议使用虚拟环境：

```bash
cd KakaCat
python -m venv .venv
.venv\Scripts\activate          # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`requirements.txt` 内容：

| 依赖 | 用途 |
| --- | --- |
| `customtkinter` | 现代化桌面界面 |
| `requests` | HTTP 请求与流式下载 |
| `cryptography` | 整段 AES-128 解密（AES-128-CBC + PKCS7） |
| `Pillow` | `customtkinter` 的图片资源依赖（本项目未直接使用图片） |

`threading` / `queue` / `pathlib` / `logging` / `urllib.parse` / `subprocess`
均为 Python 标准库，无需额外安装。

安装测试依赖：

```bash
pip install -r requirements-dev.txt
```

## 六、FFmpeg 的安装与配置（可选）

只有当你希望输出 `.mp4` 而分片是 MPEG-TS 时，才需要 FFmpeg：

- **输出 `.ts`**：不需要 FFmpeg，程序直接二进制拼接。
- **输出 `.mp4`**：需要 FFmpeg 做无损重新封装（`-c copy`，不重新编码）。
- **在线播放**：不需要 FFmpeg——mpv 自己就静态链接了 ffmpeg 的解码库。
- **fMP4 / CMAF 分片**：不需要 FFmpeg，初始化分片加媒体分片直接拼接。

因为默认输出改成了 `.mp4`，FFmpeg 实际上成了**推荐项**（没装也能用，默认值会
自动退回 `.ts`）。也可以把它随程序一起分发，见第十四节。

Windows 安装方式：

1. 从 <https://www.gyan.dev/ffmpeg/builds/> 下载 `ffmpeg-release-essentials.zip`；
2. 解压到例如 `C:\ffmpeg`；
3. 把 `C:\ffmpeg\bin` 加入系统 `PATH` 环境变量；
4. 打开新的终端验证：`ffmpeg -version`。

程序启动时会自动检测 FFmpeg 与 ffplay，并在右侧「运行环境」面板和日志中显示
检测结果。如果未检测到，选择 `.mp4` 输出时会给出明确的安装提示；点击
「在线播放」的「播放」按钮时也会提示需要安装什么。

`ffplay` 与 `ffmpeg` 在同一个压缩包里，按上面的步骤装好后两者会一起可用，
不需要单独配置。

### mpv（想要进度条与倍速就装它）

在线播放的**进度条与倍速**依赖 mpv，因为它提供 JSON IPC 控制通道。最省事的装法：

```powershell
winget install shinchiro.mpv
```

或者手动来：从 <https://mpv.io/installation/> 下载 Windows 版（绿色压缩包即可），
解压到例如 `C:\mpv`，再把 `C:\mpv` 加入 `PATH`。

> **注意**：`winget` 装的 mpv 会落在 `C:\Program Files\MPV Player\`，但
> **不会**自动加进 `PATH`。程序对此做了兜底——查找顺序是「随包分发的 →
> `PATH` → 常见安装位置」，所以装完不用额外配置也能被认出来。手动解压的那种
> 放哪儿都行，只要加进 `PATH`。

或者把它随程序一起分发：把 `mpv.exe` 放进程序目录的 `bin\` 里（详见第十四节）。

程序启动时会在右侧「运行环境」面板里显示 mpv 与 ffplay 的检测结果。没装 mpv 时
在线播放会退回 ffplay，进度条与倍速不可用并给出提示；下载功能不受影响。

## 七、启动程序

```bash
python main.py
```

## 八、基本使用步骤

1. 在「视频 URL（M3U8）」中粘贴媒体播放列表链接（形如 `https://…/index.m3u8`）。
   - 如果粘贴的是主播放列表，程序会提示你改用它下面的具体子列表链接。
2. （可选）填写两个预留参数：
   - 「可选参数 A」填授权密钥 —— 程序会用它为 M3U8 请求生成签名（有效期 2 小时）。
   - 「可选参数 B」填服务的基础地址（如 `https://example.com`）—— 此时上面的
     「视频 URL」可以填下载链接，程序会推导出真实的多媒体播放列表地址。
   - 两个都留空时，按「视频 URL」填的链接直接请求。
3. 选择「保存目录」，必要时修改「输出文件名」。
   - 默认扩展名是 `.mp4`（双击就能播、兼容性最好），它会用到 FFmpeg 做无损
     重新封装；**没检测到 FFmpeg 时自动退回 `.ts`**，免得默认值一上来就注定失败。
   - 也可以自己改成 `.ts`：不需要 FFmpeg，直接二进制拼接。
   - 若目标文件已存在，会弹窗询问是否覆盖。
4. 点击「开始下载」。下载过程中可以随时点击「取消下载」。
5. 在底部「运行日志」查看详细过程，在中间的状态面板查看进度。
6. 想在手机或别的设备上看，点「复制播放地址（含签名）」：算出来的地址是自包含的，
   有效期内直接粘进 VLC / mpv 就能播，不需要在那台设备上装本程序。
   - 地址里带着授权签名，**别外传**；
   - 签名默认 2 小时过期，过期后重新复制一次即可。
7. 想先看看再决定下不下，点右侧「在线播放」里的「播放」：程序用同一套 URL 处理
   和同一组可选参数请求播放列表，确认可播后启动播放器直接在线播放。
   - 在线播放不写任何文件，也不占用「保存目录」；
   - 播放器窗口关掉后，面板会自动回到可播放状态；点「停止播放」可立即结束；
   - 在线播放和下载可以同时进行，互不影响。
8. 播放期间的控制（装了 mpv 时）：

   | 操作 | 在哪 | 怎么做 |
   | --- | --- | --- |
   | 跳转 | **播放器窗口** | 拖动窗口底部的进度条；`←` / `→` 退/进 5 秒，`↑` / `↓` 退/进 60 秒；滚轮滚动 |
   | 暂停 / 全屏 | **播放器窗口** | 空格或 `p` / `f`；双击画面也可全屏 |
   | 音量 | **播放器窗口** | `9` / `0` 或 `/` / `*` |
   | **倍速** | **本程序面板** | 面板上的 `−` / `＋` 按钮，档位 0.5× / 0.75× / 1× / 1.25× / 1.5× / 2×；也可以直接按播放器窗口里的 `[` `]`（`Backspace` 回到 1×） |

   为什么跳转在窗口里、倍速在面板上？因为 **mpv 的窗口里根本没有倍速控件**——它的
   界面脚本是编译进 exe 的，只提供可拖的进度条，调速只有快捷键。所以进度条留在
   窗口里（不重复做一遍），倍速由面板补上按钮。两者都通过 IPC 实时生效，改倍速
   不需要重启播放器。选过的倍速会被记住，下次播放沿用。

   用 ffplay 兜底时这些都没有：窗口里没进度条，面板上的倍速按钮是灰的，并会提示装 mpv。
9. 右侧「快捷操作」可打开输出目录或滚动到最新日志。

## 九、项目目录结构

```
KakaCat/
├── main.py                     # 应用启动入口（--check 只报告运行环境）
├── requirements.txt            # 运行依赖
├── requirements-dev.txt        # 测试依赖
├── requirements-build.txt      # 打包依赖（只在打 exe / 安装程序时需要）
├── pytest.ini                  # 测试配置（含 integration 标记）
├── README.md
├── hls_downloader/             # 下载核心（不含任何 GUI 代码）
│   ├── __init__.py
│   ├── models.py               # 状态枚举、数据结构、异常
│   ├── url_utils.py            # URL 校验、相对地址解析、日志脱敏
│   ├── playlist.py             # M3U8 解析与格式判定
│   ├── crypto.py               # 整段 AES-128 解密（IV 推导 + PKCS7 去填充）
│   ├── app_paths.py            # 程序目录 / 运行数据目录 / 随包工具的位置
│   ├── downloader.py           # 核心下载函数 download_hls_video()
│   ├── merger.py               # 分片合并 / FFmpeg 重新封装
│   ├── playback.py              # 在线播放的播放列表预检（只取列表，不下载）
│   ├── player.py               # 播放器查找与启动（mpv + IPC 控制 / ffplay 兜底）
│   └── interfaces.py           # 预留业务接口（占位，不发请求）
├── gui/                        # CustomTkinter 界面层
│   ├── __init__.py
│   ├── theme.py                # 颜色、字体、间距、圆角常量
│   ├── components.py           # 卡片、输入框、状态面板、日志面板、在线播放面板
│   └── main_window.py          # 主窗口、线程调度、消息队列
└── tests/                      # 自动化测试
    ├── _helpers.py             # 本地假 HTTP 会话（不访问真实网站）
    ├── test_app_paths.py
    ├── test_url_utils.py
    ├── test_playlist.py
    ├── test_merger.py
    ├── test_downloader.py
    ├── test_player.py
    ├── test_playback.py
    ├── test_interfaces.py
    ├── test_gui_helpers.py
    └── test_gui_integration.py # 需要图形环境，否则自动跳过
├── packaging/                  # 打包配置（见第十四节）
│   ├── build.py                # 一条命令打包 / 生成安装程序
│   ├── KakaCat.spec            # PyInstaller 配置
│   ├── KakaCat.iss             # Inno Setup 安装程序脚本
│   ├── make_icon.py            # 重新生成应用图标
│   ├── languages/              # 安装程序的中文语言文件
│   └── resources/KakaCat.ico   # 应用图标
├── bin/                        # 随包分发的第三方工具（不入库，用时现拷）
├── dist/  build/               # 打包产物与中间文件（不入库）
```

## 十、作为库调用

下载核心可以脱离界面独立使用：

```python
from hls_downloader import download_hls_video

result = download_hls_video(
    playlist_url="https://example.com/video/index.m3u8",
    output_path="D:/videos/movie.ts",
    optional_a=None,          # 预留参数，当前不处理
    optional_b=None,
    progress_callback=lambda r: print(f"{r.stage_label} {r.percent:.1f}%"),
    cancel_event=None,        # 传入 threading.Event 可随时取消
)
print(result.output_path, result.total_bytes)
```

所有异常都继承自 `hls_downloader.models.HLSDownloaderError`，
其中 `OutputExistsError`、`MasterPlaylistError`、`LivePlaylistError`、
`UnsupportedEncryptionError`、`SegmentDecryptionError`、`SegmentDownloadError`、
`MergeError`、`DownloadCancelled` 等带有可直接展示给用户的中文 `message`。

在线播放的预检同样可以脱离界面调用（它只发一次请求取播放列表，不下载分片）：

```python
from hls_downloader.playback import inspect_playlist
from hls_downloader.player import launch_playback, stop_playback

info = inspect_playlist("https://example.com/video/index.m3u8")
print(info.segment_count, info.total_duration, info.encryption_method)

player = launch_playback(info.url)              # 有 mpv 就用 mpv，否则用 ffplay

if player.supports_control:                     # mpv 才有控制通道
    print(player.position(), player.duration())  # 需要一点时间才开始有值
    player.seek(120.0)                           # 也可以由脚本驱动跳转
    player.set_speed(2.0)                        # 实时改成 2 倍速

stop_playback(player)
```

`launch_playback()` 按 mpv → ffplay 的顺序挑可用播放器；想指定就用
`launch_mpv()` / `launch_ffplay()`。两者都返回同一个句柄接口
（`poll`/`terminate`/`elapsed_seconds`/`failure_hint`），只有 mpv 的
`supports_control` 为 `True`。界面上不使用 `seek`/`set_speed`——播放控制留在
播放器窗口里，这两个方法是给脚本调用准备的。

`inspect_playlist` 与 `download_hls_video` 共用同一个 URL 处理入口
（`hls_downloader.downloader.prepare_playlist_url`），因此同一个链接、同一组
可选参数在两条路径上算出的请求地址完全一致。

## 十一、自动化测试

```bash
# 全部测试
python -m pytest

# 只跑不需要图形环境的单元测试
python -m pytest -m "not integration"

# 只跑需要图形环境的界面集成测试
python -m pytest -m integration
```

- 单元测试全部使用**本地假 HTTP 响应**与临时目录，**不访问任何真实视频网站**。
- 界面集成测试会真实创建主窗口；在没有图形环境（如无显示的 CI）时自动跳过。
- 测试覆盖：播放列表解析（含主列表/直播/加密/字节范围/纯音频/非视频分片）、
  相对地址解析、HTTP 错误与超时、分片失败不报告成功、
  取消下载（含合并阶段与密钥获取阶段）、两个可选参数为空时正常执行、
  Mock 调用核心函数、合并失败处理、已有输出文件保护、临时文件清理、
  进度语义（单调不减、完成前不满格）、后台任务异常后界面状态恢复、
  日志行数上限、线程启动失败兜底、关闭窗口时清理临时目录，
  以及整段 AES-128 解密的加解密往返（显式 IV、序号推导 IV、密钥错误必须报错）。

  在线播放部分另覆盖：预检只请求播放列表、不碰分片与密钥；在线播放与下载对同一
  链接算出的请求地址一致；主播放列表 / 直播 / 未实现加密 / 字节范围分片只提示
  不拒绝；格式错误、HTTP 错误、超时重试与取消；播放器命令行参数（不改走 shell、
  播放器输出只进内存且脱敏）；停止与关窗时结束播放器进程；在线播放面板的状态机
  与过期消息丢弃；进度条的拖动/程序性刷新之别、倍速换算、以及没有控制通道时
  控件必须禁用。

  mpv 的 IPC 用**假通道**测（离线）：订阅了哪些属性、seek 与倍速发出去的是什么
  命令、`end-file` 原因与错误日志如何变成可展示的失败说明、连不上通道时
  `supports_control` 必须为假。

  > 这些测试**不会真的启动播放器**：`subprocess.Popen` 被替换成记录调用的替身。

## 十二、常见错误与排查

| 现象 | 原因与处理 |
| --- | --- |
| 「这是一个主播放列表」 | 链接指向多码率的 master 列表。请打开具体码率的子列表，用它的 `.m3u8` 链接。 |
| 「未包含 #EXT-X-ENDLIST」 | 直播或尚未生成完毕的列表，当前版本不支持。 |
| 「获取解密密钥失败」 | AES-128 密钥地址不可访问（可能需要登录或已失效）。程序不会绕过访问控制。 |
| 「播放列表使用加密方式 SAMPLE-AES」 | 该加密方式当前版本没有实现，只实现了整段 AES-128。 |
| 「解密结果的填充不合法」 | 密钥或 IV 不匹配，通常说明密钥地址已失效或播放列表被改动。 |
| 「把 TS 分片封装为 MP4 需要 FFmpeg」 | 安装 FFmpeg 并加入 PATH，或把输出文件名改为 `.ts`。 |
| 「服务器返回 HTTP 403 / 404」 | 链接失效、需要登录或不允许访问。本工具不会绕过访问控制。 |
| 「下载第 N 个分片失败」 | 网络抖动或该分片不可用；程序已按次数重试。开启续传时直接再点一次「开始下载」即可从断点继续。 |
| 下载速度慢 | 把「并发分片数」调到 8 或 16；仍慢通常是服务器限速或线路问题。 |
| 中文显示为方框 | 系统缺少中文字体。程序会依次尝试「Microsoft YaHei UI / 微软雅黑 / Segoe UI」等字体。 |
| 界面没有反应 | 检查底部日志中的错误信息；下载完成后按钮会恢复可用。 |
| 输出文件没有生成 | 任务失败或已取消时不会留下不完整文件；失败原因见日志与弹窗提示。 |
| 「未检测到可用的播放器，无法在线播放」 | 只影响在线播放，不影响下载。装 mpv（有进度条与倍速）或装 FFmpeg（自带 ffplay，只能基本播放）并把它们加入 PATH。 |
| 播放器窗口里没有进度条、按 `[` `]` 也不调速 | 当前用的是 ffplay，它窗口里既没有进度条也没有控制通道。装 mpv 后自动就有（面板与日志里都有提示）。 |
| 面板上的倍速按钮是灰的 | 没在播放，或当前用的是 ffplay。播放开始后自动可用；ffplay 没有控制通道，只能用 mpv。 |
| 拖动进度条没反应 | 直播流通常不能往回拖；另外分片还没下载到的位置也跳不过去。 |
| 在线播放时提示「播放器已在播放」 | 同一个窗口同一时刻只跑一个在线播放。先点「停止播放」，或直接关掉播放器窗口。 |
| 在线播放能播、下载却报错 | 正常现象：在线播放交给播放器，能力比下载宽松。日志里的报错说明了下载为什么拒绝。 |
| 播放器窗口关不掉 / 关窗后播放器还在 | 正常情况下关窗会一并结束播放器；若播放器卡死，用任务管理器结束 `mpv.exe` / `ffplay.exe`。 |
| 装了 mpv 但「运行环境」显示未检测到 | 手动解压的那种需要把目录加进 `PATH`；`winget` 装的会自动被找到（程序会去 `C:\Program Files\MPV Player\` 等常见位置兜底）。 |
| 默认文件名是 `.ts` 而不是 `.mp4` | 说明没检测到 FFmpeg。装好 FFmpeg 并加入 `PATH`（或放进程序目录的 `bin\`）后重启程序即可。 |
| 「复制播放地址」后粘过去打不开 | 签名有有效期（默认 2 小时），过期就重新复制一次；另外该地址可能绑定了请求来源，换个网络环境未必能用。 |
| 保存目录里多出一个 `hls_resume_……` 目录 | 断点续传的分片缓存，和成品放在同一个盘上以免写满系统盘。任务成功后会自动删除；失败或取消时会保留，以便下次续传。不想要它就关掉「断点续传」。 |

## 十三、线程与安全说明

- GUI 主线程从不执行网络请求或文件合并；后台任务通过 `queue.Queue` 汇报进展，
  主线程用 `after()` 轮询消费，后台线程**不会**直接修改任何控件。
- 临时分片写在**输出文件所在目录**下的独立子目录里（`hls_resume_xxxxxxxxxxxx/`）。
  分片可能有几个 GB，默认丢进系统临时目录会写满系统盘；输出目录是你自己挑的、
  空间够用的地方，它的可写性在开始下载前就已校验。也可以在调用核心时用
  `temp_dir=` 显式指定别的位置。
  - 开启续传时目录名由「链接 + 输出文件 + 播放列表内容」决定，成功后删除、
    失败或取消后保留，因此**在保存目录里会看到一个 `hls_resume_` 临时目录**；
    下次重新开始同一任务会自动跳过里面已下好的分片，任务成功后会自行删除。
  - 关闭续传时用会话内的随机目录，任务结束即清理；关闭窗口时也会兜底删除。
- 合并结果先写入同目录的 `.part` 文件，校验通过后再原子替换到目标路径，
  因此合并失败**不会**破坏你原有的同名文件。
- 日志中的 URL 会经过脱敏处理（隐藏路径中间层级与整个查询串），
  不会记录令牌、Cookie 或 Authorization 头。
- 在线播放的播放器是**独立子进程**，由主窗口持有句柄：播放器的输出一律收进
  内存、不写文件（ffplay 会把含签名的地址打进 stderr，写文件等于把签名落到
  磁盘上），展示前再把地址脱敏。预检在后台线程里做，播放器进程的启动、
  轮询与终止都只在 GUI 主线程执行；关窗时会主动结束播放器，不留下孤儿进程。
- mpv 走**命名管道上的 JSON IPC**（`--input-ipc-server`），因此位置、时长、
  seek、倍速都精确可控。有一条 Windows 专属的坑值得记下来：命名管道的句柄是
  同步的，**一个线程停在阻塞读上时，同一个句柄的写也会被一起挡住**——如果直接
  阻塞读，读线程一挂住，seek 与倍速就永远发不出去。所以读之前先用
  `PeekNamedPipe` 问「有没有数据」，有才读（见 `hls_downloader/player.py` 的
  `_IpcTransport`）。POSIX 上没有这个问题，那边正常阻塞读。
- 停止播放时结束子进程可能要先请求退出、再强杀，这段等待放在一个短命的守护
  线程里，避免点一下按钮界面就卡住。
- **程序自己的**运行数据（临时文件、将来的日志与配置）放在程序目录下的 `data\`
  （便携模式）；程序目录不可写时退回 `%LOCALAPPDATA%\KakaCat\`。不写注册表、
  不写 `%APPDATA%`。下载产物与分片临时目录则一律跟着用户选择的输出目录走——
  既不进安装目录，也不占系统盘。详见 `hls_downloader/app_paths.py`。

## 十四、打包成客户端

程序**没有任何外部资源文件**（图标、图片、配置文件都没有），所以打包本身很简单。
真正的坑只有两个：customtkinter 的主题 JSON 必须一起收进去，以及别让程序往
安装目录之外乱写。

打包脚本与配置都在 `packaging/` 下，已经准备好，直接跑就行。

### 一条命令打包

```bash
pip install -r requirements-build.txt      # 只有打包才需要 PyInstaller
python packaging/build.py                  # → dist/KakaCat/KakaCat.exe
python packaging/build.py --bundle-tools   # 顺手把 ffmpeg/mpv 打进包里
```

产物是**目录**（onedir）而不是单文件：程序要在自己目录下找随包分发的 `bin/`
工具，onedir 的结构天然就是这个形状；单文件每次启动都要先解包到临时目录，
又慢又容易让人误以为程序在往系统盘写东西。

`packaging/build.py` 干的事：从 `hls_downloader.__version__` 读版本号（唯一来源）、
生成 Windows 版本资源、调 PyInstaller、最后**跑一遍打出来的 exe 自检**并把结果
打出来。想直接敲 pyinstaller 的话请用 `packaging/KakaCat.spec`，别手敲命令行——
版本资源、图标、随包工具都在 spec 里接好了。

> `--collect-all customtkinter`（spec 里已经写了）**不能省**：customtkinter 读取
> 主题与字体的 JSON 用的是自己包目录里的相对路径，不一起打包的话，exe 一启动就报
> `FileNotFoundError: ... theme ... json`，界面根本出不来。

### 生成安装程序

```bash
winget install JRSoftware.InnoSetup        # 一次就够
python packaging/build.py --installer      # → dist/KakaCat-<版本>-setup.exe
```

安装程序脚本是 `packaging/KakaCat.iss`（Inno Setup 6/7）。几个刻意的选择：

- **按用户安装**（`PrivilegesRequired=lowest`，装到
  `%LOCALAPPDATA%\Programs\KakaCat`），**不弹 UAC**。这也是程序能正常工作的前提：
  便携模式要往自己目录下的 `data\` 写运行数据，装进 `Program Files` 会因为只读而
  退回 `%LOCALAPPDATA%`。
- 中文向导：Inno Setup 官方包**不带简体中文**（只有日韩泰等），所以
  `packaging/languages/ChineseSimplified.isl` 是从 Inno Setup 官方仓库取来放进
  仓库的，打包不依赖网络。它要求 Inno Setup 6.5.0+。
- 卸载**明确清干净**，连 `data\` 一起删，不留说不清来历的空目录。

### 把 ffmpeg / mpv 随包分发（可选，但推荐）

```bash
python packaging/build.py --bundle-tools
```

它会把能在本机找到的 `ffmpeg` / `ffplay` / `mpv` 拷进仓库的 `bin\`，再一起打进
包里（打出来的样子见下）。这样目标机器上什么都不用装。`bin\` **不入库**——那是
几十 MB 的第三方二进制、各有各的许可（见下面的许可提醒），要用时现拷。

```
dist/KakaCat/
├── KakaCat.exe
├── _internal/
│   └── bin/                 # ← 随包工具在这里（PyInstaller 6 把 onedir 的东西都收进 _internal）
│       ├── ffmpeg.exe       # 输出 .mp4 用（只做 -c copy，不重新编码）
│       ├── ffplay.exe       # 没有 mpv 时的在线播放兜底
│       └── mpv.exe          # 进度条与倍速用
└── data/                    # 运行数据，首次运行或 --check 时自动创建
```

查找外部工具的顺序是 **随包分发的 → `PATH` → 常见安装位置**，所以随包的那份会被
优先使用。

### 「不侵入安装目录外」具体指什么

- 程序自己的运行数据在程序目录下的 `data\`；目录不可写（例如装在
  `C:\Program Files`）时自动退回 `%LOCALAPPDATA%\KakaCat\`，不会因为权限直接崩。
- 下载产物与分片临时目录**不在**这里：它们跟着用户选的输出目录走。关闭续传时的
  会话临时目录也建在输出目录下（`.hls_session_*`，任务结束即删）。
- 不写注册表、不写 `%APPDATA%`、不留日志文件。在线播放启动 mpv 时显式加了
  `--save-position-on-quit=no`，连 `watch_later` 都不会写。
- 唯一"碰"到安装目录之外的，是启动用户自己装在 `PATH` 上的 ffmpeg / mpv——
  那是执行程序，不是写文件。

### 许可提醒

FFmpeg 与 mpv 都是第三方程序，各自有许可（LGPL/GPL）。**随包分发前请自行确认**：
例如 gyan.dev 的 essentials 构建包含 GPL 组件（x264/x265），把它打进你的分发物
通常意味着整体分发需要满足 GPL 的相应要求。只让用户自己安装则不涉及。（这不是
法律意见，只是提醒。）

### 打包后自检

打包脚本自己就会跑第 1 步。手动排查时用这个开关，它不开界面、只报告环境：

```bash
dist\KakaCat\KakaCat.exe --check
```

在源码里同样能用（`python main.py --check`）。打包后的程序是窗口程序、**没有控制台**，
`print` 出去没人看得见，所以它会把同一份报告另写到 `data\diagnostics.txt`。

1. `--check` 应显示 mpv / ffplay / ffmpeg 都被找到，随包分发的会标注「（随包分发）」；
2. 下载一个小片源，确认默认文件名是 `.mp4`、产物能双击播放；
3. 检查程序目录：只应多出 `data\`（如果用了便携模式），不应出现分片或临时文件；
4. 把整个程序目录拷到另一台没装 ffmpeg/mpv 的机器上，用随包的 `bin\` 再走一遍
   第 2、3 步。
