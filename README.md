# HLS 视频下载器

一个基于 **CustomTkinter** 的现代化 Windows 桌面应用：输入 M3U8 播放列表链接，
程序获取并解析播放列表、顺序下载视频分片，最后合并成完整的视频文件。

蓝黑科技风深色主题、实时进度、可滚动运行日志，所有文字配色满足 WCAG AA；
下载核心是独立可测试的纯 Python 函数，不依赖任何界面框架。

> **合规声明**：本工具仅用于下载**你本人有权访问和下载**的视频资源。
> 程序**不会**绕过付费墙、破解 DRM、规避访问控制或伪造授权签名。
> 支持标准的整段 **AES-128** 加密：密钥地址由播放列表自己给出，访问权限与分片
> 完全相同，程序只是照做，不猜测也不推导密钥。

---

## 一、功能简介

- 通过图形界面输入 M3U8 链接，支持可选的保存目录与输出文件名。
- 顺序下载分片，实时显示**任务阶段、进度百分比、已完成/总分片数、下载速度、预计剩余时间**。
- 支持随时取消下载，取消或失败时自动清理临时分片文件。
- 运行日志可滚动、可清空，包含下载阶段、进度、警告与错误信息（日志不会输出完整敏感 URL）。
- 已有输出文件默认受保护，需要显式允许才会覆盖。
- 界面自适应：窗口足够宽时左右双栏，变窄时自动切换为单栏堆叠。
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

## 三、当前版本的功能限制

1. 只支持点播（VOD）列表，不支持直播录制。
2. 加密方面只实现了标准的整段 `AES-128`；其余加密方式（如 `SAMPLE-AES`）
   会明确报错。fMP4 的初始化分片（`#EXT-X-MAP`）不参与解密。
3. 顺序下载，不并发；分片失败会有限重试（默认 2 次），不会无限重试。
4. 可选参数 A / 可选参数 B 仅**记录输入值**，当前版本不做任何处理，
   也不会拼接到 URL、请求头、Cookie 或请求参数中。
5. 不接入任何真实业务接口（见 `hls_downloader/interfaces.py` 中的占位实现）。

## 四、环境要求

- **Python 3.11 或更高版本**（开发与验证环境为 Python 3.11.9）
- Windows 10 / 11（其他平台理论上可用，但未做验证）
- FFmpeg：**可选**。只有需要把 TS 分片封装成 `.mp4` 时才需要。

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

Windows 安装方式：

1. 从 <https://www.gyan.dev/ffmpeg/builds/> 下载 `ffmpeg-release-essentials.zip`；
2. 解压到例如 `C:\ffmpeg`；
3. 把 `C:\ffmpeg\bin` 加入系统 `PATH` 环境变量；
4. 打开新的终端验证：`ffmpeg -version`。

程序启动时会自动检测 FFmpeg，并在右侧「运行环境」面板和日志中显示检测结果。
如果未检测到，选择 `.mp4` 输出时会给出明确的安装提示。

## 七、启动程序

```bash
python main.py
```

## 八、基本使用步骤

1. 在「视频 URL（M3U8）」中粘贴媒体播放列表链接（形如 `https://…/index.m3u8`）。
   - 如果粘贴的是主播放列表，程序会提示你改用它下面的具体子列表链接。
2. （可选）填写「可选参数 A / B」——当前版本只记录，不参与任何请求。
3. 选择「保存目录」，必要时修改「输出文件名」。
   - 扩展名决定封装方式：`.ts` 直接拼接；`.mp4` 需要 FFmpeg。
   - 若目标文件已存在，会弹窗询问是否覆盖。
4. 点击「开始下载」。下载过程中可以随时点击「取消下载」。
5. 在底部「运行日志」查看详细过程，在中间的状态面板查看进度。
6. 右侧「快捷操作」可打开输出目录或滚动到最新日志。

## 九、项目目录结构

```
KakaCat/
├── main.py                     # 应用启动入口
├── requirements.txt            # 运行依赖
├── requirements-dev.txt        # 测试依赖
├── pytest.ini                  # 测试配置（含 integration 标记）
├── README.md
├── hls_downloader/             # 下载核心（不含任何 GUI 代码）
│   ├── __init__.py
│   ├── models.py               # 状态枚举、数据结构、异常
│   ├── url_utils.py            # URL 校验、相对地址解析、日志脱敏
│   ├── playlist.py             # M3U8 解析与格式判定
│   ├── crypto.py               # 整段 AES-128 解密（IV 推导 + PKCS7 去填充）
│   ├── downloader.py           # 核心下载函数 download_hls_video()
│   ├── merger.py               # 分片合并 / FFmpeg 重新封装
│   └── interfaces.py           # 预留业务接口（占位，不发请求）
├── gui/                        # CustomTkinter 界面层
│   ├── __init__.py
│   ├── theme.py                # 颜色、字体、间距、圆角常量
│   ├── components.py           # 卡片、输入框、状态面板、日志面板
│   └── main_window.py          # 主窗口、线程调度、消息队列
└── tests/                      # 自动化测试
    ├── _helpers.py             # 本地假 HTTP 会话（不访问真实网站）
    ├── test_url_utils.py
    ├── test_playlist.py
    ├── test_merger.py
    ├── test_downloader.py
    ├── test_interfaces.py
    ├── test_gui_helpers.py
    └── test_gui_integration.py # 需要图形环境，否则自动跳过
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
| 「下载第 N 个分片失败」 | 网络抖动或该分片不可用；程序已按次数重试。可稍后重试整个任务。 |
| 中文显示为方框 | 系统缺少中文字体。程序会依次尝试「Microsoft YaHei UI / 微软雅黑 / Segoe UI」等字体。 |
| 界面没有反应 | 检查底部日志中的错误信息；下载完成后按钮会恢复可用。 |
| 输出文件没有生成 | 任务失败或已取消时不会留下不完整文件；失败原因见日志与弹窗提示。 |

## 十三、线程与安全说明

- GUI 主线程从不执行网络请求或文件合并；后台任务通过 `queue.Queue` 汇报进展，
  主线程用 `after()` 轮询消费，后台线程**不会**直接修改任何控件。
- 临时分片写入系统临时目录下的独立目录，任务成功、失败或取消后按策略清理。
- 合并结果先写入同目录的 `.part` 文件，校验通过后再原子替换到目标路径，
  因此合并失败**不会**破坏你原有的同名文件。
- 日志中的 URL 会经过脱敏处理（隐藏路径中间层级与整个查询串），
  不会记录令牌、Cookie 或 Authorization 头。
