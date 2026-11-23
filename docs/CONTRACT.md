# 超级上下文 · 实现契约（所有成员必须遵守）

项目根：仓库根目录（团队共享同一工作目录）
线上地址：`https://wkeeli.cn/context/`（Caddy 子路径 + frp 隧道 + dev 主机 systemd）
技术栈：Python 3 标准库服务 + 原生 HTML/CSS/JS，零第三方 Python 依赖、零构建。
页面风格：沿用「黑白极简页面模板」（`web/app.js` 为主题脚本，`web/style.css` 前半部分为模板基础样式）。

## 1. 目录与归属（写作用域，互不越界）

| 文件/目录 | 负责人 | 说明 |
| --- | --- | --- |
| `server.py`、`test_server.py` | lead | HTTP 服务、存储、上传、智能体分享接口、测试 |
| `describe.py`、`test_describe.py` | media-pipeline | 媒体分类 + 描述生成流水线 |
| `web/*` | web-ui | 页面（index.html / style.css / main.js；app.js 保持模板原样） |
| `deploy/*` | deploy-ops | systemd / frp / Caddy / 发布脚本 / 部署 README |
| `README.md`、`.gitignore`、`docs/` | lead | 文档与项目说明 |
| `data/` | 运行时生成 | `items.json` + `files/`，**禁止提交**，部署时保留 |

## 2. 数据模型（items.json 中的 item）

```json
{
  "id": "8 位小写 hex",
  "name": "原始文件名（去路径，≤120 字符）",
  "kind": "image | document | audio | video | other",
  "mime": "image/png",
  "size": 123456,
  "description": "上传时生成的描述，≤300 字，供智能体判断相关性",
  "content": "可作上下文的正文/转写/画面描述，≤30000 字，可为空字符串",
  "status": "processing | ready | error",
  "error": "仅 error 时存在，中文可读原因",
  "created_at": "2026-11-23T11:30:00+08:00",
  "updated_at": "2026-11-23T11:30:00+08:00"
}
```

items.json 结构：`{"version": 1, "token": "<32 位 hex 访问令牌>", "items": [...], "updated_at": "..."}`。
令牌首次启动自动生成并持久化；智能体分享链接使用它。

## 3. describe.py 接口（media-pipeline 交付）

```python
KINDS = ("image", "document", "audio", "video", "other")

def classify(name: str) -> str          # 按扩展名，未知 -> "other"
def guess_mime(name: str) -> str        # 复用标准库 mimetypes，未知 -> application/octet-stream

class Settings:
    api_key: str; base_url: str; vision_model: str; text_model: str; asr_model: str
    max_text_chars: int; timeout: int
    @classmethod
    def from_env(cls) -> "Settings"

def describe_file(path: str, name: str, settings: Settings) -> dict:
    """阻塞生成描述，返回 {"kind","mime","description","content","error"}。
    - 不向外抛普通异常；失败时 error 为中文可读原因，description 用文件名兜底，content 可为 ""。
    - 未配置 api_key 时返回 error="未配置百炼 API Key（CONTEXT_API_KEY）"，仍给出兜底描述。
    """
```

环境变量（server 通过 `Settings.from_env()` 读取；部署时写入 dev 主机 `/opt/super-context/.env`）：

| 变量 | 默认值 |
| --- | --- |
| `CONTEXT_API_KEY` | 无（必填） |
| `CONTEXT_BASE_URL` | `https://dashscope.aliyuncs.com`（Token Plan 用专属地址覆盖） |
| `CONTEXT_VISION_MODEL` | `qwen3-vl-plus` |
| `CONTEXT_TEXT_MODEL` | `qwen-plus` |
| `CONTEXT_ASR_MODEL` | `qwen3-asr-flash` |
| `CONTEXT_MAX_TEXT_CHARS` | `30000` |
| `CONTEXT_TIMEOUT` | `120`（单次模型调用秒数） |

调用方式（已实测可用，不要再换 endpoint）：OpenAI 兼容
`POST {base_url}/compatible-mode/v1/chat/completions`，`Authorization: Bearer <key>`。

- 图片：`messages[0].content = [{"type":"text","text":提示词},{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,<...>"}}]`；JPEG/PNG 都可用 data URL。图片 >8MB 时先用 ffmpeg 压到最长边 1600 的 jpg。
- 音频：`content=[{"type":"input_audio","input_audio":{"data":"data:audio/wav;base64,<...>","format":"wav"}}]`，model=`qwen3-asr-flash`，`modalities=["text"]`，取 `choices[0].message.content` 即转写。
- 文本：普通 chat。
- 视频：ffprobe 取时长 → ffmpeg 均匀抽 6 帧 JPG（最长边 ≤ 960）→ 多张 `image_url` 给 `qwen3-vl-plus`；同时（有音轨时）ffmpeg 抽 16k 单声道 wav → ASR。`content` = `【画面描述】...\n\n【语音转写】...`（无音轨只写画面），`description` 综合两者 ≤300 字。

媒体处理细则：

| 类型 | 处理 |
| --- | --- |
| 图片 `image` | `qwen3-vl-plus` 中文描述：主体、场景、图中文字（OCR 原文）、风格/用途，≤250 字。`content` 可为空字符串 |
| 文档 `document` | 先提取文本：纯文本类直接 decode（utf-8 → gbk 兜底）；`.docx` 用 zipfile 读 `word/document.xml`；`.pptx` 读幻灯片 `a:t`；`.xlsx` 读 `sharedStrings.xml`（+ inline strings）；`.pdf` 用 `pdftotext -layout`（无命令时用 `pdftoppm` 渲前 3 页 → `qwen3-vl-plus` 识图；都没有则报错"PDF 文本提取不可用"）。提取结果截断到 `max_text_chars` 作为 `content`；再用 `qwen-plus` 写 ≤120 字描述。文本为空且无法识图时给出兜底描述 |
| 音频 `audio` | ffprobe 取时长；ffmpeg 转 16k 单声道 wav；>240s 按 240s 分段 ASR 后拼接（总时长 >30 分钟只转前 30 分钟并注明）。`content` = 转写全文，`description` = `qwen-plus` ≤120 字摘要 |
| 视频 `video` | 见上 |
| 其他 `other` | 兜底描述 `《文件名》（大小 X）`，尝试按文本 decode 一次，失败则 content 为空 |

约束：只用标准库 + `subprocess` 调 ffmpeg/ffprobe/pdftotext/pdftoppm；临时文件用 `tempfile`，必须清理；所有网络调用有超时与一次重试；日志写 stderr（带时间戳）；单个文件处理建议控制在 5 分钟内。

## 4. server.py 接口（lead 交付）

- 启动：`python3 server.py`，环境变量：`HOST`（默认 127.0.0.1）、`PORT`（默认 8082）、`CONTEXT_DATA`（默认 `./data`）、`CONTEXT_WEB`（默认 `./web`）、`CONTEXT_PUBLIC_PREFIX`（默认 `http://127.0.0.1:<port>`，线上 `https://wkeeli.cn/context`）、`CONTEXT_MAX_UPLOAD_MB`（默认 200）。
- 对外可测入口：`create_server(host=None, port=None, data_dir=None, web_dir=None, describer=None, public_prefix=None)`；`describer(path, name) -> dict` 默认调 `describe.describe_file`；测试可注入假 describer。
- 页面/UI API（路径不带 `/context` 前缀，Caddy 已剥）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `api/state` | `{updated_at, token, items:[...]}` |
| POST | `api/items?name=<urlencoded>` | 请求体=文件原始字节；201 返回 `{item}`，item.status=processing |
| PATCH | `api/items/{id}` | JSON `{name?, description?, content?}` → `{item}` |
| DELETE | `api/items/{id}` | 删除条目与文件 → `{ok:true}` |
| GET | `api/items/{id}/raw` | 原始文件（Content-Type 按 mime，inline） |
| GET | `healthz` | `{ok:true}` |

- 智能体分享 API（免登录，靠 token；Caddy 中 `/context/s/*` 免 Basic Auth）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `s/{token}/index.md` | 全部资料 Markdown 清单（描述 + 绝对链接） |
| GET | `s/{token}/index.json` | 同上 JSON |
| GET | `s/{token}/items/{id}.md` | 单份资料详情（描述 + content + 原始文件链接） |
| GET | `s/{token}/files/{id}` | 原始文件 |
| 其他 token | `s/{wrong}/...` | 404 |

- index.md 格式（必须包含）：

```markdown
# 超级上下文 · 资料清单

> 共 N 份资料 · 更新于 2026-11-23 11:30
> 使用方式：阅读每份资料的「描述」，需要哪份就 GET 该条「资料链接」下载原始文件；需要文字内容/转写时 GET「详情链接」。
> 链接已含访问令牌，直接请求即可，无需账号密码。

## 1. 文件名（图片）
- 资料链接：https://wkeeli.cn/context/s/<token>/files/<id>
- 详情链接：https://wkeeli.cn/context/s/<token>/items/<id>.md
- 类型：图片 · 大小：1.2 MB · 上传：2026-11-23 11:30
- 描述：……
```

- 安全/健壮性：文件名去路径与控制字符；静态目录防穿越；请求体上限 413；未知 API 404、方法错 405；全部错误 `{"error":"中文原因"}`；JSON 原子落盘（临时文件 + os.replace）；上传后后台线程生成描述，前端轮询。

## 5. web 前端（web-ui 交付）

- 页面标题/`<title>` = `超级上下文`；`.sub` = 一句话介绍（≤30 字）。
- 主题三态逻辑、localStorage、`?theme=`、CSS 变量、560px 断点、375px 不横向滚动等全部沿用模板 AGENTS.md 硬性约束；颜色只用 `--bg/--fg/--muted/--line/--hover`；零外部请求。
- 结构：页头 + 工具栏（统计、复制智能体链接、刷新）+ 上传区（点击/拖拽，多文件，逐个上传并显示进度）+ 智能体链接展示区 + 资料列表（缩略图/类型/大小/时间/描述/生成中状态/失败原因/复制单份链接/下载/删除）+ 页脚。
- 交互：`fetch("api/state")` 拉取；有 `processing` 条目时每 3 秒轮询；上传用 XHR 以拿到进度；`navigator.clipboard` 复制失败时降级 `execCommand`；删除二次确认；所有用户内容用 `textContent`，不拼 HTML。
- 智能体链接 = `location.origin + location.pathname.replace(/\/$/, "") + "/s/" + token + "/index.md"`（页面在 `/context/` 下，location.pathname 以 `/` 结尾）。
- 单份链接 = 同一前缀 + `/s/<token>/items/<id>.md`。

## 6. deploy（deploy-ops 交付）

- `deploy/super-context.service`：`/opt/super-context`，HOST=127.0.0.1，PORT=8082，`EnvironmentFile=/opt/super-context/.env`，CONTEXT_DATA=/opt/super-context/data，CONTEXT_PUBLIC_PREFIX=https://wkeeli.cn/context，ExecStart=/usr/bin/python3 server.py。
- `deploy/push.ps1`：scp `server.py describe.py web/ deploy/` 到 `/opt/super-context`，不覆盖 `data/` 与 `.env`，restart + healthz。
- `deploy/frpc.snippet.toml`：name=`super-context`，localPort 8082 → remotePort 18082（追加到 dev 主机 `/opt/frp/frpc.toml`）。
- `deploy/Caddyfile.snippet`：`redir /context /context/ 308` + `handle_path /context/* { reverse_proxy 127.0.0.1:18082 }`；并在 aliyun `/etc/caddy/Caddyfile` 的 `@not_local` 中追加 `not path /context/s/*`（智能体免登录）。
- `deploy/.env.example` 只放占位符；真实 key 写 dev 主机 `/opt/super-context/.env`（600），不进 Git。
- `deploy/README.md`：拓扑、首次部署、验证命令、回滚、排障。
- 远端准备：dev 主机安装 `poppler-utils`（pdftotext/pdftoppm），确认 ffmpeg/ffprobe 存在；Caddyfile/frpc.toml 改动前备份；`caddy validate` 通过才 reload。

## 7. 验收（部署后逐条实测）

1. `https://wkeeli.cn/context/` 打开出现页面（走全站登录），主题三态正常，375px 无横向溢出。
2. 上传一张图片 → 列表出现条目，几秒内出现中文描述；缩略图可看；删除可用。
3. 上传 txt/md/docx/pdf 各一 → 有描述，详情页可读正文。
4. 上传音频（wav/mp3）→ 有转写与摘要。
5. 上传视频（mp4，含声音）→ 有画面描述 + 语音转写。
6. 复制智能体链接，`curl` 无账号密码可 GET index.md；能按描述 GET 到对应 files/、items/。
7. `systemctl status super-context` 正常；重启服务后数据仍在；`curl http://127.0.0.1:18082/healthz`（aliyun 侧）返回 ok。
