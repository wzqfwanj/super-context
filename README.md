# 超级上下文（super-context）

把资料传到页面上，自动生成描述；把**一个链接**交给智能体，智能体按描述取用对应资料。

- 线上地址：<https://wkeeli.cn/context/>（复用 wkeeli.cn 全站登录）
- 源码：<https://github.com/wzqfwanj/super-context>（MIT License）
- 技术栈：Python 3 标准库 + 原生 HTML/CSS/JS，零第三方 Python 依赖、零构建
- 描述生成：阿里云百炼（`qwen3-vl-plus` 看图 / 抽帧，`qwen3-asr-flash` 转写，`qwen-plus` 写摘要）
- 数据：`data/items.json` + `data/files/`，原子写入，重启不丢

## 功能

- 上传任意资料：图片、文档、音频、视频（含 `.docx/.pptx/.xlsx/.pdf` 等）
- 上传后后台自动生成两类内容：
  - `description`：≤300 字的中文描述，给智能体判断相关性用
  - `content`：可作上下文的正文 / 转写 / 画面描述（文档全文、音频转写、视频画面+语音）
- 页面：拖拽/多选上传、上传进度、图片缩略图、音视频原生播放器、描述生成中自动轮询、删除、复制链接
- 智能体链接：不需要账号密码，凭 URL 里的 32 位 token 访问
  - `GET /context/s/<token>/index.md` —— 全部资料清单（描述 + 绝对链接）
  - `GET /context/s/<token>/index.json` —— 同上 JSON
  - `GET /context/s/<token>/items/<id>.md` —— 单份资料详情（描述 + 正文/转写）
  - `GET /context/s/<token>/files/<id>` —— 原始文件
- 页面主题沿用「黑白极简模板」：明 / 灰 / 暗三态、`?theme=light|gray|dark`

## 智能体怎么用

把页面「智能体链接」里的地址直接发给智能体：

```
https://wkeeli.cn/context/s/<token>/index.md
```

它打开后看到的是类似这样的清单：

```markdown
# 超级上下文 · 资料清单

> 共 2 份资料 · 更新于 2026-11-23 11:50
> 使用方式：阅读每份资料的「描述」，需要哪份就 GET 该条「资料链接」获取原始文件……

## 1. 需求说明.docx（文档）
- 资料链接：https://wkeeli.cn/context/s/<token>/files/8aa52100
- 详情链接：https://wkeeli.cn/context/s/<token>/items/8aa52100.md
- 类型：文档 · 大小：825 B · 上传：2026-11-23 11:46
- 描述：用户可上传图片、文档、音频、视频四类资料……
```

智能体按描述选需要的条目，再 GET「资料链接」或「详情链接」即可，无需任何认证。

## 本地运行

```bash
cd super-context
python3 server.py            # 默认 http://127.0.0.1:8082
python3 server.py 8083       # 或指定端口
```

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | 监听地址 |
| `PORT` | `8082` | 监听端口（命令行参数优先） |
| `CONTEXT_DATA` | `./data` | 数据目录（`items.json` + `files/`） |
| `CONTEXT_WEB` | `./web` | 静态页面目录 |
| `CONTEXT_PUBLIC_PREFIX` | `http://127.0.0.1:<port>` | 对外前缀，拼智能体链接用；线上为 `https://wkeeli.cn/context` |
| `CONTEXT_MAX_UPLOAD_MB` | `200` | 单个上传文件上限（MB） |
| `CONTEXT_API_KEY` | 无 | 百炼 API Key（必填，缺失时只保存文件、描述走兜底） |
| `CONTEXT_BASE_URL` | `https://dashscope.aliyuncs.com` | 百炼 OpenAI 兼容 base_url；Token Plan/专属工作空间填控制台的专属地址 |
| `CONTEXT_VISION_MODEL` | `qwen3-vl-plus` | 图片/视频画面描述模型 |
| `CONTEXT_TEXT_MODEL` | `qwen-plus` | 文档/音频/视频摘要模型 |
| `CONTEXT_ASR_MODEL` | `qwen3-asr-flash` | 音视频转写模型 |
| `CONTEXT_MAX_TEXT_CHARS` | `30000` | `content` 最大字数 |
| `CONTEXT_TIMEOUT` | `120` | 单次模型调用超时（秒） |

本地跑音视频描述需要系统有 `ffmpeg` / `ffprobe`；PDF 需要 `pdftotext` / `pdftoppm`
（Ubuntu：`apt-get install -y poppler-utils`）。缺命令时该类型会返回可读的失败原因，不会崩溃。

测试：

```bash
python test_server.py     # 31 个接口/存储测试，不需要网络
python test_describe.py   # 分类/文本提取/兜底等离线测试，不需要网络
```

## API

页面与 UI 接口（页面在 `/context/` 下，前端全部用相对路径；线上需全站登录）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `api/state` | `{updated_at, token, max_upload_mb, items}` |
| POST | `api/items?name=<文件名>` | 请求体=文件原始字节 → `201 {item}`（status=processing） |
| PATCH | `api/items/{id}` | `{name?, description?, content?}` → `{item}` |
| DELETE | `api/items/{id}` | 删除条目与文件 → `{ok:true}` |
| GET | `api/items/{id}/raw` | 原始文件（inline，带下载文件名） |
| GET | `healthz` | 健康检查 → `{ok:true}` |

智能体分享接口（**免登录**，token 自校验；Caddy 中 `/context/s/*` 已加入全站 Basic Auth 白名单）。

## 目录

```
super-context/
├── server.py            # HTTP 服务 + 存储 + 上传 + 后台描述 worker + 分享接口（标准库）
├── describe.py          # 媒体分类与描述生成流水线（ffmpeg/poppler + 百炼）
├── test_server.py       # 接口测试（unittest，注入假描述器）
├── test_describe.py     # 描述流水线离线测试
├── web/                 # 页面（黑白极简模板 + 本页面样式/脚本）
├── docs/
│   ├── CONTRACT.md      # 实现契约（API/数据模型/验收清单）
│   └── preview*.png     # 页面截图
├── deploy/              # systemd / frp / Caddy / 发布与验收脚本、部署说明
└── data/                # 运行时数据（gitignore，升级代码不覆盖）
```

## 部署

拓扑与首次部署/回滚/排障见 [deploy/README.md](deploy/README.md)。简述：

```
浏览器/智能体 ──HTTPS──> wkeeli.cn Caddy（全站 Basic Auth）
                            ├─ /context/*        → 127.0.0.1:18082（frp 隧道）→ dev 8082
                            └─ /context/s/*      免登录（token 自校验）
```

发布：`powershell -ExecutionPolicy Bypass -File deploy/push.ps1`（不覆盖 `data/` 与 `.env`）。
验收：`powershell -ExecutionPolicy Bypass -File deploy/verify.ps1 -Credential "wkeeli:<站点密码>"`。

## 限制

- 单文件上限 200MB（`CONTEXT_MAX_UPLOAD_MB` 可调）；总容量受 dev 主机磁盘限制
- 旧版 Office 二进制格式（`.doc/.ppt/.xls`）不支持，请另存为 `.docx/.pptx/.xlsx`
- 音频/视频超过 30 分钟只转写前 30 分钟（内容里会注明）
- 扫描版 PDF 用视觉模型识别前 3 页；超大 PDF 只处理前 3 页
- 视频抽 6 帧 + 音轨转写后由模型综合描述，画面细节以帧为准

## 开源许可

[MIT License](LICENSE) © 2026 wzqfwanj

## 验收记录（2026-11-23）

- `python test_server.py`：31 个测试全部通过
- 真实上传实测（dev 主机）：图片、`.md`、`.docx`、`.pdf`、`.wav`、`.mp4` 均生成中文描述；
  音频出转写、视频出「画面描述 + 语音转写」、PDF 走 `pdftotext` 提取
- 外网：`https://wkeeli.cn/context/` 未登录 401，登录后页面正常；智能体 token 链接免登录 200，
  错误 token 404；页面三态主题正常；375px 视口无横向溢出（实测 `scrollWidth == clientWidth`）
- 服务重启后条目与 token 保留；`deploy/push.ps1` 发布流程与 `deploy/verify.ps1` 验收脚本已跑通
