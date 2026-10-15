# 贡献指南

欢迎提 Issue 和 PR——报 bug、要功能、改文档、修代码都算。

## 提 Issue

- **碰到异常或行为不对**：用 [Bug 报告模板](.github/ISSUE_TEMPLATE/bug_report.yml)，
  它会要求你贴 `--check` 的输出，那一条能省掉一大半来回。
- **想要新功能**：用 [功能请求模板](.github/ISSUE_TEMPLATE/feature_request.yml)，
  重点说清楚"你想解决什么问题"，而不是"你想加什么按钮"。
- 提问也欢迎，但请先说清楚环境（Windows 版本、是装的安装包还是源码运行）。

**贴 `--check` 的输出**（源码运行就把 `KakaCat.exe` 换成 `python main.py`）：

```bash
KakaCat.exe --check
```

它会打印外部工具（mpv / ffplay / ffmpeg）的检测结果、程序目录、运行数据目录，
以及报告落到了哪个文件。下载/播放相关的多数问题看一眼这个就清楚了。

## 提 PR

1. Fork 后从 `master` 开分支（例如 `fix/xxx`、`feature/xxx`）；
2. 改完**确保测试通过**：`python -m pytest`；
3. PR 说明里写清楚**为什么**这么改，不只是改了什么；
4. 别把 `dist/`、`build/`、`bin/`、`data/` 提交进来——它们都在 `.gitignore` 里，
   如果被带进来了说明提交方式有问题。

## 开发环境

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt     # 含运行依赖 + pytest
python -m pytest                        # 全部测试
python -m pytest -m "not integration"   # 跳过需要图形环境的用例
python main.py                          # 起界面
```

Python 3.11+，Windows。需要图形环境的集成测试在没有显示环境时会自动跳过。

## 代码约定

这个项目有几条硬约束，改动时请一并遵守（它们在评审里会被认真检查）：

- **日志里不得出现完整的敏感地址、密钥、Cookie、Authorization 头**。
  需要打印地址时用 `hls_downloader.url_utils.redact_url()`。
  可选参数 A（授权密钥）只参与本地签名计算，永远不写日志、不进请求头。
- **GUI 主线程不做网络请求、不做文件合并**；后台线程通过 `queue.Queue` 汇报，
  主线程用 `after()` 消费，**任何控件都只在主线程里改**。
- **不伪装成功**：分片没下全、合并没产出文件、播放器没播起来，都必须如实报错，
  不允许用假进度或假完成糊过去。
- **加密只做标准整段 AES-128**：密钥地址取自播放列表自身声明的 `#EXT-X-KEY:URI`，
  不推导、不猜测、不破解。不实现付费墙绕过、DRM 破解、访问控制规避。
- 只在**不支持**某项能力时明确报错，不静默跳过。

另外：

- 模块职责单一。`hls_downloader/` 是不依赖界面的核心，`gui/` 只做交互与展示；
  核心逻辑不要写进 GUI 回调里。
- 用类型注解和文档字符串；注释解释**为什么**，不解释代码字面在做什么。
- 测试优先用本地假会话（`tests/_helpers.py` 的 `FakeSession`）和临时目录，
  **不要访问真实视频网站**。
- 新增依赖前先想想能不能不加——核心下载流程刻意只依赖 requests 与 cryptography。

## 提交信息

中文，一句话说清这次改了什么；改动大时在正文里分点说明**为什么**。
参考 `git log` 里已有的写法。

## 许可

本项目使用 [MIT 许可证](LICENSE)。提 PR 即表示你同意以该许可发布你的贡献。

注意：打包时随包分发的 ffmpeg / mpv 是 GPL 程序，相关义务见
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。
