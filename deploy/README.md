# 部署说明 · 超级上下文

线上地址：<https://wkeeli.cn/context/>（Caddy 子路径 + 全站 Basic Auth + frp 隧道 + dev 主机 systemd）

## 1. 拓扑

```
浏览器 / 智能体
      │ HTTPS
      ▼
wkeeli.cn（aliyun 跳板机，Caddy v2.11.4，全站 Basic Auth）
  ├─ @not_local 白名单：/pc2mb/*、/record/*、/context/s/*（智能体分享接口免登录）
  ├─ /context/*  ── handle_path（剥前缀）──> 127.0.0.1:18082（frps 隧道端口）
  └─ 兜底 ──> reverse_proxy localhost:5000
                                    │
                         frpc（dev 主机，/opt/frp/frpc.toml）
                                    │ tcp 隧道
                                    ▼
                         dev 主机 127.0.0.1:8082（systemd: super-context）
                                    │
                         /opt/super-context/server.py（Python 3.12 标准库，零第三方依赖）
```

| 项 | 值 |
| --- | --- |
| dev 主机 | `ssh dev`（Python 3.12.3） |
| aliyun 跳板机 | `ssh aliyun` |
| 应用目录 | `/opt/super-context`（`server.py` / `describe.py` / `web/` / `deploy/` / `data/` / `.env`） |
| 监听 | dev 主机 `127.0.0.1:8082`（仅本机，不暴露公网） |
| 隧道 | dev `127.0.0.1:8082` → aliyun `127.0.0.1:18082`（frpc `name = "super-context"`） |
| 公网入口 | `https://wkeeli.cn/context/` |
| 认证 | 页面与 UI API（`/context/`、`/context/api/...`）走全站 Basic Auth（账号 `wkeeli`）；智能体分享接口 `/context/s/<token>/...` 由 URL token 自校验，免登录 |
| 数据 | `/opt/super-context/data/items.json` + `data/files/`（**运行时数据，push 不覆盖，禁止提交**） |
| 密钥 | `/opt/super-context/.env`（`chmod 600`，不进 Git） |

Caddy 用 `handle_path /context/*` 剥掉 `/context` 前缀，所以后端 `server.py` 收到的路径仍是
`api/state`、`s/<token>/index.md`、`healthz` 等（**不带** `/context` 前缀）；前端全部使用相对
路径。`CONTEXT_PUBLIC_PREFIX=https://wkeeli.cn/context` 只用于 index.md 里的绝对链接。

## 2. 本次远端准备状态（已由 deploy-ops 完成）

- [x] `ssh dev` / `ssh aliyun` 连通（BatchMode 可用，均 root + 免密 sudo）
- [x] dev 主机 Python 3.12.3、`ffmpeg`、`ffprobe` 就绪
- [x] dev 主机 `poppler-utils` 已安装（`/usr/bin/pdftotext`、`/usr/bin/pdftoppm`）
- [x] dev 主机目录已建：`/opt/super-context/{web,deploy,data/files}`
- [x] dev 主机 `/opt/super-context/.env` 已写入（600，含 `CONTEXT_API_KEY` / `CONTEXT_BASE_URL` /
      `CONTEXT_PUBLIC_PREFIX`，key 取自本机 `~/.bailian/config.json`）
- [x] dev 主机出网正常：`https://dashscope.aliyuncs.com/` 可连通
- [x] 端口：dev `8082`、aliyun `18082` 均未被占用
- [x] aliyun `/etc/caddy/Caddyfile` 中当前**没有**任何 `context` 规则，不存在冲突
- [x] 配置备份（改动前）：
  - dev：`/opt/frp/frpc.toml.bak-supercontext-20261123-114026`
  - aliyun：`/etc/caddy/Caddyfile.bak-supercontext-20261123-114027`
- [ ] **尚未**改 frpc.toml 隧道、**尚未**改 Caddyfile 路由、**尚未**创建 systemd 服务
      （等代码就绪后由 task-5 上线执行，见第 6 节步骤）

## 3. 首次部署（重装/迁移时复现）

前置：本机 `~/.ssh/config` 已有 `Host dev` 与 `Host aliyun`；dev 主机可 `sudo` 且可出网。

### 3.1 dev 主机：目录 + .env

```bash
# 在 dev 主机执行（root 或 sudo）
mkdir -p /opt/super-context/{web,deploy,data/files}

# 方法 A：手工写 .env
sudo install -m 600 -o root -g root /dev/null /opt/super-context/.env
sudo vi /opt/super-context/.env
#   CONTEXT_API_KEY=<真实 key>
#   CONTEXT_BASE_URL=https://dashscope.aliyuncs.com   # Token Plan/专属工作空间用控制台地址
#   CONTEXT_PUBLIC_PREFIX=https://wkeeli.cn/context
```

```powershell
# 方法 B：本机 Windows 一行生成并上传（不回显 key）
$cfg = Get-Content -Raw "$env:USERPROFILE\.bailian\config.json" | ConvertFrom-Json
$t = Join-Path $env:TEMP "sc-env.txt"
[IO.File]::WriteAllText($t, "CONTEXT_API_KEY=$($cfg.api_key)`nCONTEXT_BASE_URL=$($cfg.base_url)`nCONTEXT_PUBLIC_PREFIX=https://wkeeli.cn/context`n")
scp -q $t dev:/tmp/sc-env.txt
ssh dev 'install -m 600 -o root -g root /tmp/sc-env.txt /opt/super-context/.env && rm -f /tmp/sc-env.txt'
Remove-Item $t -Force
```

### 3.2 dev 主机：上传代码 + systemd

```powershell
# 本机执行：上传 server.py / describe.py / test_*.py / web/ / deploy/，重启并 healthz
powershell -ExecutionPolicy Bypass -File deploy/push.ps1
```

```bash
# dev 主机执行（首次）
sudo cp /opt/super-context/deploy/super-context.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now super-context
sudo systemctl status super-context --no-pager
curl -s http://127.0.0.1:8082/healthz     # {"ok": true}
```

> `push.ps1` 只同步代码，**不会**覆盖 `/opt/super-context/data/`（线上数据）与 `.env`（密钥）。
> 备份/迁移数据只需拷贝 `/opt/super-context/data/` 整个目录（含 `files/`）。

### 3.3 dev 主机：frp 隧道

把 `deploy/frpc.snippet.toml` 追加到 `/opt/frp/frpc.toml` 末尾（先备份！），然后：

```bash
cp -p /opt/frp/frpc.toml /opt/frp/frpc.toml.bak-supercontext-$(date +%Y%m%d-%H%M%S)
cat /opt/super-context/deploy/frpc.snippet.toml >> /opt/frp/frpc.toml
systemctl restart frpc
systemctl is-active frpc
```

在 aliyun 跳板机上验证隧道（本机请求）：

```bash
curl -s http://127.0.0.1:18082/healthz    # {"ok": true}
```

### 3.4 aliyun 跳板机：Caddy 反代

`deploy/Caddyfile.snippet` 含**两处**改动（文件内有注释说明）：

1. 顶部 `@not_local { ... }` 追加 `not path /context/s/*`（智能体免登录）；
2. 在 `wkeeli.cn { ... }` 块内、兜底 `reverse_proxy localhost:5000` 之前插入
   `redir /context /context/ 308` + `handle_path /context/* { reverse_proxy 127.0.0.1:18082 }`。

```bash
cp -p /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-supercontext-$(date +%Y%m%d-%H%M%S)
vi /etc/caddy/Caddyfile        # 按上面两处改动编辑
caddy validate --config /etc/caddy/Caddyfile    # 必须输出 Valid configuration
systemctl reload caddy                          # 只有 validate 通过才 reload
```

跳板机本机验证（127.0.0.1 免全站 Basic Auth）：

```bash
curl -sk https://127.0.0.1/context/ -H 'Host: wkeeli.cn' | head -20
curl -sk https://127.0.0.1/context/api/state -H 'Host: wkeeli.cn'
```

### 3.5 外网验收

```powershell
# 有外网的本机；-Credential 为全站 Basic Auth（wkeeli:<密码>）
powershell -ExecutionPolicy Bypass -File deploy/verify.ps1 -Credential "wkeeli:<密码>"
powershell -ExecutionPolicy Bypass -File deploy/verify.ps1 -Credential "wkeeli:<密码>" -UploadTest
```

脚本检查：dev healthz、公网 `/context/` 未登录返回 401、登录后 200、智能体链接免登录 200、
错 token 404、可选上传往返（上传后自动删除）。完整验收清单见 `docs/CONTRACT.md` 第 7 节。

## 4. 日常发布

```powershell
powershell -ExecutionPolicy Bypass -File deploy/push.ps1
```

改动 `server.py` / `describe.py` / `web/` 后重复执行即可；无需动 frp 与 Caddy。
若改了 `deploy/*.service`，需在 dev 主机重新 `cp` 到 `/etc/systemd/system/` 并
`daemon-reload` 后 restart。

## 5. 回滚

| 层 | 回滚方式 |
| --- | --- |
| 应用 | 恢复旧代码后 `deploy/push.ps1`；或 `sudo systemctl disable --now super-context` |
| 隧道 | 注释/删除 `/opt/frp/frpc.toml` 中 `name = "super-context"` 的 `[[proxies]]` 段，`systemctl restart frpc` |
| 入口 | `cp -p /etc/caddy/Caddyfile.bak-supercontext-<时间戳> /etc/caddy/Caddyfile && caddy validate ... && systemctl reload caddy` |
| 数据 | 恢复 `/opt/super-context/data/` 备份（含 `items.json` 与 `files/`），再 restart |
| 全量 | 上述四层按需倒序回滚；`.env` 可随时重新生成，不影响数据 |

备份命名约定：同目录 `*.bak-supercontext-YYYYmmdd-HHMMSS`。

## 6. 上线步骤清单（task-5 执行）

1. 确认代码就绪（`server.py` / `describe.py` / `web/` 已由 lead 汇总，测试通过）。
2. `deploy/push.ps1` 上传代码到 dev（不覆盖 data/ 与 .env）。
3. dev：`cp deploy/super-context.service /etc/systemd/system/ && daemon-reload && enable --now`，
   `curl 127.0.0.1:8082/healthz` 通过。
4. dev：备份 frpc.toml → 追加 `deploy/frpc.snippet.toml` → `systemctl restart frpc`；
   aliyun：`curl 127.0.0.1:18082/healthz` 通过。
5. aliyun：备份 Caddyfile → 按 `deploy/Caddyfile.snippet` 改两处 → `caddy validate` 通过 → `reload`。
6. 跑 `deploy/verify.ps1`（含上传往返），再按 `docs/CONTRACT.md` 第 7 节逐条人工验收
   （页面/主题/375px、图片、txt/md/docx/pdf、音频、视频、智能体链接）。
7. 记录本次备份路径与验收结果。

**风险与注意**

- 修改 Caddyfile 必须 `caddy validate` 通过后才 `reload`；reload 不中断现有连接，但配置写错会
  影响全站（wkeeli.cn 下所有子路径），所以务必保留备份、改前先跑一次基线 validate。
- `handle_path` 会剥前缀：前端资源与 fetch 必须用相对路径，否则 404。
- `/context/s/*` 免 Basic Auth 后，安全性完全依赖 token（32 位 hex）；不要外泄，删除条目不影响
  token。token 在 `data/items.json` 首次启动时自动生成。
- `push.ps1` 不覆盖 `data/`；**不要**手工 `scp -r web/` 之外的删除操作，避免误删线上数据。
- dev 主机 apt 安装 poppler 后无需重启任何服务；`describe.py` 运行时可随时用上 pdftotext/pdftoppm。
- 上传大文件（默认上限 200MB）经 frp + Caddy 中转，超时/内存异常时看 dev `journalctl -u super-context`。

## 7. 排障

| 现象 | 排查 |
| --- | --- |
| 502 Bad Gateway | `systemctl status super-context`；`journalctl -u super-context -n 100`；`ss -ltnp \| grep 8082` |
| aliyun `curl 127.0.0.1:18082` 失败 | `systemctl status frpc`；`tail -50 /var/log/frpc.log`；确认 frpc.toml 里 super-context 段已追加且未破坏 TOML |
| 页面 404 / 静态资源 404 | 确认 Caddy 用的是 `handle_path /context/*`（剥前缀），前端全用相对路径 |
| 页面反复弹登录 | `@not_local` 有语法错误或 `not path /context/s/*` 写错位置（应在 `@not_local` 块内）；`caddy validate` 确认 |
| 智能体链接 401 | `/context/s/*` 未加入 `@not_local` 白名单，或 Caddy 未 reload |
| 智能体链接 404 | token 错误/条目不存在；`ssh dev 'cat /opt/super-context/data/items.json \| python3 -m json.tool \| head'` 核对 token |
| 上传后一直 processing | `journalctl -u super-context -f` 看 describe 报错；确认 `.env` 里 key 有效、dev 能出网；`ls /opt/super-context/data/files` |
| 描述为"未配置百炼 API Key" | `/opt/super-context/.env` 未被读到：确认 `EnvironmentFile=`、文件 600 且 root 可读、`systemctl show super-context -p Environment` 核对 |
| PDF 提取报错 | `which pdftotext pdftoppm`；`apt-get install -y poppler-utils` |
| 视频/音频处理失败 | `which ffmpeg ffprobe`；`ffprobe` 能否解析该文件；`journalctl -u super-context -n 200` |
| 数据丢失 | 恢复 `/opt/super-context/data/` 备份后 restart；`items.json` 为原子写入，损坏概率低 |
