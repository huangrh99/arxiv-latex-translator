# ArXiv LaTeX Translator

本地运行的 arXiv LaTeX 翻译工具。输入 arXiv 链接或 ID，下载源码后使用 Codex CLI 翻译正文、标题、章节标题和 figure/table caption，并编译出中文 PDF。

## 启动

```bash
cd arxiv-latex-translator
python3 -m arxiv_latex_translator.app
```

浏览器打开 <http://127.0.0.1:8000>。

项目只使用 Python 标准库。需要本机安装 `codex` 和 `latexmk`、`xelatex` 或 `tectonic`，并确保 Codex CLI 已完成登录。

## Mac mini 后端与 GitHub Pages 前端

可以把后端和翻译结果放在 Mac mini，GitHub Pages 只托管不含论文数据的前端页面。Mac mini 上建议把任务目录放在 `papers-sync` 同步目录之外，例如：

```bash
ARXIV_HOST=127.0.0.1 \
ARXIV_PORT=8000 \
ARXIV_OUTPUT_DIR="$HOME/arxiv-latex-translator-data/jobs" \
python3 -m arxiv_latex_translator.app
```

Mac mini 没有管理员权限安装 BasicTeX 时，可以使用用户级 Homebrew 安装 `tectonic`：

```bash
/opt/homebrew/bin/brew install tectonic
```

编译器会在找不到 `latexmk` 和 `xelatex` 时自动回退到 Tectonic。

GitHub Pages 页面通过 `arxiv-translator/config.js` 中的 `window.ARXIV_API_BASE` 访问后端。该地址需要是浏览器可访问的 HTTPS 地址，并且后端需要允许 GitHub Pages 的跨域请求。纯 HTTP 的 Mac mini 内网地址会被 HTTPS 页面拦截，适合通过 Tailscale、Cloudflare Tunnel 或其他 HTTPS 反向代理提供访问。

当前论文数据不会写入个人主页仓库。仓库根目录的 `.stignore` 也忽略 `arxiv-latex-translator/data/`，避免误把默认本地任务目录交给 Syncthing。

任务状态会记录 Codex CLI 返回的 `input_tokens`、`cached_input_tokens`、`output_tokens`、`reasoning_output_tokens` 和 `total_tokens`。其中 `total_tokens` 按输入 token 加输出 token 计算，重复调用和保护标记修复调用也会计入。

## Codex CLI 配置

默认适配器执行 `codex exec --skip-git-repo-check --sandbox read-only --ephemeral --color never -`。可用环境变量配置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CODEX_EXECUTABLE` | `codex` | Codex 可执行文件 |
| `CODEX_MODEL` | 空 | `--model` 值 |
| `CODEX_PROFILE` | 空 | `--profile` 值 |
| `CODEX_WORKDIR` | 空 | Codex 工作目录 |
| `CODEX_EXTRA_ARGS` | 空 | 额外 CLI 参数 |
| `CODEX_TIMEOUT_SECONDS` | `600` | 单个翻译块超时 |
| `CODEX_CHUNK_CHARS` | `2600` | 单个翻译块的最大字符数 |

## LaTeX 配置

默认优先使用 `latexmk -xelatex`，其次使用 `xelatex`，最后使用 `tectonic`。可以用 `LATEX_COMMAND` 指定完整命令，例如 `LATEX_COMMAND='latexmk -xelatex'`。编译超时由 `LATEX_TIMEOUT_SECONDS` 控制。

检测到中文内容时，编译器会为没有 CJK 支持的文档注入 `xeCJK` 和 macOS 的 `Songti SC` 字体设置，并关闭 `microtype` 的自动字形探测，避免中文字体触发 XeTeX 错误。使用自定义编译命令时，需要确保该命令支持 XeLaTeX 和 CJK 字体。

## 输出

任务文件默认写入 `data/jobs/`。每个任务包含源归档、解压后的源文件、翻译后的源文件、PDF、日志和 `job.json`。`ARXIV_OUTPUT_DIR` 可以修改输出目录。

资源保护可以通过 `ARXIV_MAX_DOWNLOAD_BYTES`、`ARXIV_MAX_ARCHIVE_BYTES`、`ARXIV_MAX_EXTRACTED_BYTES`、`ARXIV_MAX_EXTRACTED_FILES` 和 `ARXIV_MAX_CONCURRENT_JOBS` 调整。默认值分别为 200 MiB、200 MiB、512 MiB、10000 个文件和 1 个并发任务。

首页的「历史翻译」列表读取已持久化任务，支持按 arXiv ID、输入链接或任务 ID搜索。接口为 `GET /api/jobs?query=关键词`。

完成任务后，页面会提供中文译文 PDF、原文 arXiv PDF、翻译源码和编译日志。通过「对照阅读」可以在同一页面左右打开原文 PDF 与中文译文 PDF。原文 PDF 由 arXiv 在线提供，中文 PDF 由本服务生成并在线提供。

## 保护规则

数学公式、LaTeX 结构、引用、标签、URL、图片命令、宏定义、表格环境及表格单元格会被保护。图片文件和参考文献条目保持原文。caption 允许翻译。

## 测试

```bash
python3 -m pytest
```
