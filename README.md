# 知错 · AI 错题本

一个本地优先的完整错题整理工作台，基于原 `ai-solve-proxy` 的 139 云盘拍题能力扩展而来。错题、图片和复习记录保存在本机，可通过 AI 拍题识别，也可完全脱离 AI 手工使用。

## 主要功能

- **错题整理**：手工录入、编辑、删除、复制、归档、收藏、批量操作
- **结构化记录**：学科、年级、题型、错因、难度、掌握度、知识点、标签、来源、错题本、原作答、复盘笔记
- **AI 拍题**：上传前自动纠正 EXIF 方向并压缩；密集整页练习逐题裁剪并执行 11 次独立检索，突破上游单次仅返回 3 个候选的限制
- **AI 自动拆题**：通过本机 CLIProxyAPI 的可选视觉模型（默认 `pp/gemini-3.8-flash`）识别整页题框，失败时自动回退本地 OCR；题框支持整体拖动、八向缩放、微调和撤销
- **AI 解原题**：单题、整页、跨页、深度思考，支持流式结果并整理入库
- **智能复习**：按到期时间生成每日复习队列；四档反馈自动调整掌握度和下次复习日期
- **检索统计**：全文搜索、多条件筛选、排序、学科分布、七日趋势、重点收藏和今日待复习
- **OCR Word 导出**：优先使用 139 搜题返回的题干文字与图片，先排成一张连续长图，再交给 CamScanner OCR 脚本生成 `.docx`；另保留打印 `.pdf`
- **数据备份**：SQLite 数据库与本地图片一键打包 ZIP
- **隐私优先**：139 Token 使用 AES-256-GCM 认证加密；工作台使用独立访问密钥和 HttpOnly 会话保护，只有主动使用 AI 拍题时才访问 139 官方接口

## 启动

Windows：双击 `start.bat`。

macOS / Linux：

```bash
bash run.sh
```

也可以手动启动：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python server.py
```

默认端口是 `50005`。启动时会自动检测 Tailscale IPv4，并只监听该地址；请使用启动日志中显示的地址访问，例如：

```text
http://100.x.x.x:50005
```

同一 Tailnet 中的电脑、手机和平板均可使用该地址访问。这样不会默认把含本地错题和 AI Token 的页面暴露给其他网卡。

没有安装或未连接 Tailscale 时，服务会回退到 `127.0.0.1:50005`；如果该回环端口已被本机代理占用，请通过 `MISTAKE_BOOK_PORT` 指定其他端口。

如需自定义端口：

```bash
MISTAKE_BOOK_PORT=19000 bash "run.sh"
```

如确实需要同时允许局域网访问，可显式监听全部网卡：

```bash
MISTAKE_BOOK_HOST=0.0.0.0 MISTAKE_BOOK_PORT=50005 bash "run.sh"
```

## 访问鉴权

服务默认会在首次启动时生成访问密钥，并保存到 `data/.access-token`（权限为 `0600`）；密钥只在首次生成的启动日志中显示一次。打开启动日志中的地址后输入该密钥即可进入工作台。

生产环境或多机部署建议显式设置环境变量，避免依赖本地密钥文件：

```bash
MISTAKE_BOOK_AUTH_TOKEN='至少 16 位的随机密钥' bash "run.sh"
```

访问密钥不会写入 URL，也不会返回给前端接口；登录后仅通过随机的 `HttpOnly`、`SameSite=Lax` Cookie 维持 12 小时会话。连续输错 8 次后会暂时限流 1 分钟。

服务已启用低带宽优化：HTML、CSS、JavaScript、JSON 等文本响应自动使用 gzip，图片、PDF、Word 和 ZIP 不重复压缩；静态资源带长期缓存，页面和业务接口分别使用重新验证与不缓存策略。

## 配置 AI 拍题

不配置 Token 也可使用除 AI 拍题外的全部错题本功能。

整页 AI 拆题默认连接 `http://127.0.0.1:50002`，并读取本机 CLIProxyAPI 配置中的接入密钥。可通过
`MISTAKE_BOOK_CLIPROXY_URL`、`MISTAKE_BOOK_CLIPROXY_API_KEY` 和
`MISTAKE_BOOK_SEGMENT_MODEL` 覆盖，默认模型为 `pp/gemini-3.8-flash`。页面会从
`/api/ai/models` 加载可用视觉模型，AI Word 与 AI 拆题都支持选择模型。

1. 安装 Tampermonkey，新建脚本并粘贴 `tampermonkey/139-token-export.user.js`。
2. 登录 <https://yun.139.com/archive-book-h5/>。
3. 点击页面右下角工具，下载 `config.json`。
4. 进入本应用的“AI 与数据”，选择文件导入。

导入后，凭证使用 AES-256-GCM 加密写入 `data/credentials.enc`，随机主密钥保存在权限为 `0600` 的 `data/.credential.key`。旧版本若存在明文 `config.json`，首次启动会自动加密迁移并删除明文文件。

错题备份不会包含凭证或主密钥。如果复制整个项目到另一台机器，需要同时安全迁移 `credentials.enc` 和 `.credential.key`，否则旧凭证无法解密。也可以通过环境变量 `MISTAKE_BOOK_MASTER_KEY` 提供 32 字节 URL-safe Base64 主密钥，适用于容器或密钥管理系统。

此设计可防止 Token 直接出现在磁盘、日志和普通备份中；但若攻击者已经取得运行服务的系统账户权限，则仍可能同时读取密文与本地主密钥。需要更强隔离时，应通过系统密钥管理或环境变量托管主密钥。

## 数据与备份

```text
data/
├── mistakes.db       # 错题与复习记录
├── media/            # 本地上传的题目图片
├── credentials.enc   # AES-256-GCM 加密后的 139 凭证
├── .credential.key   # 139 凭证主密钥，权限 0600
└── .access-token     # 工作台访问密钥，权限 0600（自动生成时）
```

“AI 与数据 → 下载完整备份 ZIP”会包含错题数据库和媒体文件，不会包含凭证、主密钥或访问密钥。恢复时停止服务，将备份中的数据文件放回项目目录即可；如需保留原登录密钥，请另外安全迁移 `data/.access-token`。覆盖现有数据前请自行备份。

## 导出说明

- AI 拍题收入错题本时，题干只保存 139 搜题返回的 HTML、文字和远程图片，不再保存用户拍摄的整页图或裁剪图。
- “OCR Word”会解析题干中的 139 图片和文字，按当前纸张、边距、字号、行距、留白、答案与解析选项排版为一张连续 PNG，再调用 `MISTAKE_BOOK_CAMSCANNER_SCRIPT` 指定的脚本生成可编辑 Word。
- “导出 Word”中的 LaTeX 公式会转换为 Word 原生 OMML 公式对象，不再以公式截图插入；常见的分数、上下标、根号、矩阵、求和与积分均可继续在 Word 中编辑。
- CamScanner 需要该脚本中的有效 `S2`、`_cssu` 凭证，并依赖 `d82.intsig.net` 网络连通。OCR 服务失败时会明确提示，并自动下载 `python-docx` 生成的原生 Word 兜底。
- PDF 使用 `reportlab` 生成，优先调用系统中文字体。Linux 建议安装 Droid Sans Fallback、文泉驿或将 `NotoSansSC-Regular.ttf` 放到 `assets/`。
- 本地 `image_urls`（用户手工上传或旧数据中的拍摄图）不会进入 OCR Word 题干。浏览器“打印预览”提供快速外观检查，最终文件以服务端生成结果为准。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/mistakes` | 查询 / 新增错题 |
| GET/PATCH/DELETE | `/api/mistakes/{id}` | 详情 / 编辑 / 删除 |
| POST | `/api/mistakes/batch` | 批量收藏、归档、恢复、删除 |
| POST | `/api/mistakes/{id}/review` | 提交复习反馈 |
| GET | `/api/dashboard` | 统计与趋势 |
| POST | `/api/media` | 本地保存题目图片 |
| POST | `/api/export/camscanner` | 139 题干排版整图 → CamScanner OCR Word |
| POST | `/api/export/docx` | 导出 Word |
| POST | `/api/export/pdf` | 导出 PDF |
| POST/GET | `/api/ai-word/jobs`、`/api/export/jobs/{id}` | 后台 AI OCR/排版 Word 任务 |
| GET | `/api/ai/models` | 查询本机 CLIProxyAPI 可用视觉模型 |
| GET | `/api/backup` | 完整数据备份 |
| POST | `/api/upload`, `/api/search`, `/api/solve` | 原 139 AI 拍题能力 |

## 注意

139 接口及权益可能调整；AI 结果也可能有误，重要题目请人工核对。本项目仅供个人学习整理使用。
