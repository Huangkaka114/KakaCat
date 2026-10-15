# 第三方组件声明

KakaCat **自身**的代码使用 [MIT 许可证](LICENSE)。但它在运行与分发时会用到下面这些
第三方组件，它们各自遵循自己的许可，与 MIT 无关。

如果你只是**从源码运行**（`python main.py`），本文件里和你相关的只有"Python 依赖"
一节——那都是宽松许可，不带来额外义务。

如果你**分发打包好的安装包**（`KakaCat-*-setup.exe`），那就在分发 GPL 程序，
请把"随包分发的可执行程序"一节读完。

---

## 一、随包分发的可执行程序（只有用 `--bundle-tools` 打包时才有）

`python packaging/build.py --bundle-tools` 会把下面这些程序**原样**拷进包的
`_internal/bin/`，安装包会带着它们一起分发。KakaCat 通过**独立的子进程**调用它们
（命令行参数与 JSON IPC），不对它们做任何修改，属于聚合使用。

| 程序 | 版本 | 许可证 | 用途 | 源码 |
|---|---|---|---|---|
| `ffmpeg.exe` | 7.1 (gyan.dev essentials) | **GPL-3.0** | 把 TS 分片重新封装成 `.mp4` | <https://ffmpeg.org/download.html> · <https://www.gyan.dev/ffmpeg/builds/> |
| `ffplay.exe` | 7.1 (同上) | **GPL-3.0** | 没有 mpv 时的在线播放兜底 | 同上 |
| `mpv.exe` | 0.41.0 (shinchiro build) | **GPL-2.0 或更新** | 在线播放与倍速控制 | <https://github.com/mpv-player/mpv> · <https://mpv.io/installation/> |

> ffmpeg 的这个构建带 `--enable-gpl --enable-version3` 编译选项，因此是 **GPLv3**
> 而不是 LGPL。不带 GPL 组件的 LGPL 构建也能满足本项目的重新封装需求
> （`-c copy` 不需要 x264/x265 之类的编码器），但 **mpv 本身就是 GPL**，
> 所以换成 LGPL 的 ffmpeg 只能解决一半问题。

**GPL 要求你做什么**：分发这些二进制时，必须随附许可证全文，并提供对应的源代码
（或有效的书面承诺）。这两件事本仓库已经准备好：

- 许可证全文：[`licenses/GPL-3.0.txt`](licenses/GPL-3.0.txt)、
  [`licenses/GPL-2.0.txt`](licenses/GPL-2.0.txt)；
- 对应源码：见上表的源码链接。最稳妥的做法是从对应构建方取得**完全一致的**源码快照
  （gyan.dev 提供构建对应的源码包），在分发渠道一并提供。

打包脚本会把 `LICENSE`、本文件与 `licenses/` 一起装进安装目录，所以安装包本身
自带这些材料。

## 二、Python 依赖（从源码运行时也需要）

全部是宽松许可，**不要求你公开自己的代码**。KakaCat 不做修改地使用它们。

| 依赖 | 许可证 | 说明 |
|---|---|---|
| [`customtkinter`](https://github.com/TomSchimansky/CustomTkinter) | **CC0-1.0** | 界面框架（公共领域奉献，比 MIT 还宽松） |
| [`requests`](https://github.com/psf/requests) | **Apache-2.0** | HTTP 请求 |
| [`cryptography`](https://github.com/pyca/cryptography) | **Apache-2.0 或 BSD-3-Clause**（双许可） | AES-128 解密 |
| [`Pillow`](https://github.com/python-pillow/Pillow) | **MIT-CMU** | customtkinter 的图片依赖；本项目只用它生成图标 |
| [`PyInstaller`](https://github.com/pyinstaller/pyinstaller) | **GPL-2.0 带 bootloader 例外** | 仅打包时使用。该例外允许用它打包出来的程序采用任意许可，**不会**传染 GPL |

## 三、不随包分发、但运行时可能调用

KakaCat 会**查找**用户自己安装在系统 `PATH` 或常见位置上的 ffmpeg / mpv。
这种情况下它们不是本项目分发的一部分，相关许可义务由用户与那些程序的提供方之间解决。

---

## 说明

本文件按工程上的通行做法编写，**不是法律意见**。若要把带 GPL 组件的安装包用于
商业分发，建议就"对应源码的提供方式"咨询专业人士。
