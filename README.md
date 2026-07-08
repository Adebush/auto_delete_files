# auto_delete_files

> **v6.3.0** — 基于 AstrBot 框架的群文件自动清理插件，支持定时清理、手动命令、自然语言触发与 Web 管理面板。
- 注意，在使用本插件时请将你的Napcat的http设置为3000
---

## 目录

- [架构概览](#架构概览)
- [项目文件结构](#项目文件结构)
- [模块详解](#模块详解)
- [核心功能](#核心功能)
- [交互流程](#交互流程)
- [功能变更记录](#功能变更记录)
- [安装配置指南](#安装配置指南)
- [使用方法](#使用方法)
- [Web 管理面板](#web-管理面板)
- [API 接口文档](#api-接口文档)
- [常见问题与故障排除](#常见问题与故障排除)

---

## 架构概览

```
┌─────────────────────────────────────────────────────────┐
│                      AstrBot 框架                        │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │                  main.py (入口)                   │   │
│  │  ┌──────────┐ ┌───────────┐ ┌────────────────┐  │   │
│  │  │ 命令处理器 │ │ LLM 工具   │ │ 定时检查器      │  │   │
│  │  │ /立即删除  │ │ delete_   │ │ _ensure_task() │  │   │
│  │  │ /确认删除  │ │ group_files│ │ (每60秒轮询)   │  │   │
│  │  │ /测文件   │ │ check_    │ │                │  │   │
│  │  │          │ │ group_files│ │                │  │   │
│  │  └─────┬────┘ └─────┬─────┘ └───────┬────────┘  │   │
│  │        │             │               │           │   │
│  │        └──────┬──────┘               │           │   │
│  │              ▼                       │           │   │
│  │     ┌──────────────────┐             │           │   │
│  │     │ _delete_all_files │◄────────────┘           │   │
│  │     │  (dry_run / 执行)  │                         │   │
│  │     └────────┬─────────┘                         │   │
│  │              │                                    │   │
│  │     ┌────────▼─────────┐                         │   │
│  │     │ _build_forward_  │                         │   │
│  │     │ nodes() 合并转发  │                         │   │
│  │     └────────┬─────────┘                         │   │
│  │              │                                    │   │
│  │     ┌────────▼─────────┐                         │   │
│  │     │ add_history_entry│                         │   │
│  │     │ () 记录历史       │                         │   │
│  │     └──────────────────┘                         │   │
│  └─────────────────────────────────────────────────┘   │
│                     │                                   │
│     ┌───────────────┼───────────────┐                   │
│     ▼               ▼               ▼                   │
│ ┌─────────┐  ┌────────────┐  ┌───────────┐             │
│ │storage  │  │web_server  │  │web/       │             │
│ │.py      │  │.py (aiohttp│  │index.html │             │
│ │JSON 持久│  │端口 1655)  │  │前端单页应用│             │
│ │化       │  │7 个 API    │  │3 个标签页  │             │
│ └────┬────┘  └──────┬─────┘  └─────┬─────┘             │
│      │              │               │                   │
│      ▼              ▼               │                   │
│ ┌──────────┐  ┌──────────┐          │                   │
│ │data/     │  │OneBot     │◄─────────┘                  │
│ │config    │  │HTTP API   │   (浏览器 → 前端 → API      │
│ │history   │  │:3000      │    → OneBot → QQ)           │
│ └──────────┘  └──────────┘                              │
└─────────────────────────────────────────────────────────┘
```

**数据流方向：**

| 触发方式 | 路径 |
|----------|------|
| QQ 命令 (`/立即删除`) | QQ → OneBot → AstrBot → `main.py` 命令处理器 |
| 自然语言 (LLM) | QQ → OneBot → AstrBot → LLM → function call → `main.py` LLM 工具 |
| 定时清理 | `_ensure_task()` 轮询 → `_delete_all_files()` |
| Web 面板 | 浏览器 → `web_server.py` API → OneBot → QQ / `storage.py` JSON |

---

## 项目文件结构

```
auto_delete_files/
├── main.py              # 插件入口：命令处理、LLM 工具、定时任务、生命周期
├── web_server.py        # aiohttp HTTP 服务端，端口 1655，提供 REST API
├── storage.py           # JSON 文件持久化：清理历史 + 系统配置
├── web/
│   └── index.html       # 前端单页应用（嵌入 CSS/JS，零外部依赖）
├── metadata.yaml         # AstrBot 插件元数据
├── requirements.txt      # Python 依赖：httpx, aiohttp
├── .gitignore
└── README.md
```

---

## 模块详解

### main.py — 插件核心

| 组件 | 位置 | 职责 |
|------|------|------|
| `AutoDeleteFiles` 类 | L143 | 插件主类，继承 `Star`，管理生命周期与所有功能 |
| `initialize()` | L150 | AstrBot 加载时触发：启动后台定时任务 + 启动 Web 服务 |
| `terminate()` | L155 | AstrBot 卸载时触发：取消定时任务 + 停止 Web 服务 |
| `_ensure_task()` | L162 | 创建 asyncio 后台任务，每 60 秒轮询当前时间是否匹配配置的执行时刻 |
| `_delete_all_files()` | L206 | 核心删除逻辑，支持 `dry_run=True`（仅预览）和实际删除两种模式 |
| `_get_group_id()` | L291 | 从事件对象中兼容多种属性路径提取 `group_id` |
| `_get_sender_id()` | L304 | 从事件对象中提取发送者 QQ 号 |
| `_check_is_admin()` | L317 | 通过 OneBot `get_group_member_info` API 校验群主/管理员身份 |
| `_send_group_forward_msg()` | L332 | 通过 OneBot `send_group_forward_msg` 发送合并转发消息 |
| `delete_now()` | L343 | `/立即删除` 命令处理器（两阶段确认） |
| `confirm_delete()` | L384 | `/确认删除` 命令处理器（第二阶段执行） |
| `delete_group_files()` | L431 | `@filter.llm_tool` LLM 工具，支持 `confirmed` 参数分阶段执行 |
| `check_group_files()` | L496 | `@filter.llm_tool` LLM 工具，返回文件列表供 LLM 回答用户 |
| `test_file()` | L530 | `/测文件` 调试命令，输出 API 原始返回数据 |

**关键模块级函数：**

| 函数 | 位置 | 职责 |
|------|------|------|
| `extract_file_list()` | L28 | 从 OneBot API 返回数据中兼容提取文件列表，过滤文件夹和 `busid=2` 条目 |
| `_format_file_size()` | L44 | 字节数转可读大小（B/KB/MB/GB/TB） |
| `_build_forward_nodes()` | L59 | 将删除条目列表转换为 OneBot v11 合并转发 `node` 消息段数组 |

**全局状态：**

| 变量 | 用途 |
|------|------|
| `_last_exec_month` | 记录上次自动清理的年月，防止同一分钟重复执行 |
| `_pending_confirmations` | `{(group_id, user_id): (expiry, group_id)}` 删除确认暂存，60 秒超时 |
| `_CONFIRMATION_TIMEOUT` | 确认超时秒数（默认 60） |

### web_server.py — HTTP 服务端

基于 `aiohttp` 框架，在插件 `initialize()` 时通过 `start_web_server()` 启动，`terminate()` 时通过 `stop_web_server()` 安全关闭。

| 路由 | 方法 | 功能 | 对应 OneBot API |
|------|------|------|-----------------|
| `/` | GET | 返回 Web 管理面板前端页面 | — |
| `/api/groups` | GET | 获取所有群聊列表 | `get_group_list` |
| `/api/files/{group_id}` | GET | 获取指定群根目录文件列表 | `get_group_root_files` |
| `/api/delete/{group_id}` | POST | 删除指定群的指定文件（需传 `file_ids` 数组） | `delete_group_file` |
| `/api/history` | GET | 获取清理历史记录 | `storage.load_history()` |
| `/api/config` | GET | 获取系统配置 | `storage.load_config()` |
| `/api/config` | POST | 更新系统配置 | `storage.save_config()` |
| `/api/permission/{group_id}/{user_id}` | GET | 校验用户是否为群主/管理员 | `get_group_member_info` |

### storage.py — 持久化层

所有数据存储在 `data/` 目录下的 JSON 文件中：

| 文件 | 格式 | 用途 |
|------|------|------|
| `data/auto_delete_config.json` | `{"auto_clean_day": 1, "auto_clean_time": "00:00"}` | 自动清理执行时刻配置 |
| `data/auto_delete_history.json` | `[{"time": "...", "group_id": ..., ...}]` | 清理历史记录数组（保留最近 500 条） |

### web/index.html — 前端管理面板

单页应用（无构建工具 / 无 CDN 依赖），3 个标签页：
- **群文件列表**：下拉选群 → 权限验证 → 文件表格（名称/大小/上传者/时间）→ 勾选 → 弹窗确认删除
- **清理历史**：表格展示历史记录，含时间/群号/操作者/成功数/失败数/文件列表
- **系统配置**：表单设置每月清理日期（1-28）和时间（HH:MM），前端校验格式

---

## 核心功能

### 1. 定时自动清理

每月在指定的日期和时间（默认 1 号 00:00），自动遍历所有群聊，删除每个群根目录下的所有非文件夹、非 `busid=2` 文件。

**技术实现：**
- `_ensure_task()` 创建 asyncio 后台任务，每 60 秒轮询一次
- 比对当前北京时间与 `data/auto_delete_config.json` 中的 `auto_clean_day` 和 `auto_clean_time`
- 使用 `_last_exec_month` 全局变量防止同一分钟内重复执行

**场景：** 某公司群每天都有大量临时文件上传，设置每月 1 号凌晨自动清空，无需人工干预。

### 2. 手动命令删除（两阶段确认）

```
用户: /立即删除
Bot:  ⚠️ 即将删除群 123456 根目录下 15 个文件（共 23.5MB）：
       1. 周报.pdf  [2.1MB]
       2. 截图.png  [500.4KB]
       ...
       请在 60 秒内回复 "确认删除" 以执行操作

用户: /确认删除
Bot:  ⏳ 正在执行删除...
     [合并转发消息] 删除报告
     ✅ 删除完成：成功 14 个，失败 1 个
```

**安全设计：**
1. 第一阶段 `/立即删除` 仅执行 `dry_run=True` 预览，不实际删除
2. 60 秒超时自动作废
3. 仅同一用户在同一个群内可确认（按 `(group_id, user_id)` 绑定）
4. 必须通过权限校验（群主/管理员）

### 3. LLM 自然语言调用

**注册的 LLM 工具：**

| 工具名 | 参数 | 功能 |
|--------|------|------|
| `delete_group_files` | `confirmed: str` | 预览/删除群文件。`"false"` → 返回预览；`"true"` → 执行删除 |
| `check_group_files` | 无 | 查看群根目录文件列表 |

**使用场景示例：**

```
用户: 帮我看看群里有哪些文件
LLM:  [调用 check_group_files] → 返回文件列表 → 组织为自然语言回复

用户: 把这些文件都删了吧
LLM:  [调用 delete_group_files(confirmed=false)] → 返回预览列表
Bot:  ⚠️ 检测到以下 15 个文件将被删除，请回复确认
用户: 确认
LLM:  [调用 delete_group_files(confirmed=true)] → 执行删除 → 发送报告
```

### 4. Web 管理面板（端口 1655）

在浏览器访问 `http://服务器IP:1655/`，提供可视化的群文件管理与配置功能。详见 [Web 管理面板](#web-管理面板) 章节。

### 5. 合并转发删除报告

删除完成后自动生成 OneBot v11 规范的合并转发消息，结构如下：

```
┌─ 节点1 (头部): 群号 + 执行时间 + 待处理文件数
├─ 节点2..N:     "已删除" 列表（每组≤10条，含文件名+大小）
├─ 节点N+1:      "删除失败" 列表（含错误原因，仅在有失败时出现）
│               (跳过条目在 footer 中统计但无独立节点)
└─ 末节点:       "删除完成" 汇总 (成功/失败/跳过/总计)
```

---

## 交互流程

### 命令删除完整流程

```
用户发送 /立即删除
     │
     ▼
权限校验 (_check_is_admin)
     ├── 非管理员 → "❌ 权限不足" (终止)
     └── 管理员 ────────────────────┐
                                    ▼
                          _delete_all_files(dry_run=True)
                          获取文件列表预览
                                    │
                     ┌──────────────┴──────────────┐
                     │ 无文件                        │ 有文件
                     ▼                               ▼
            "暂无文件" (终止)              存入 _pending_confirmations
                                            (60秒超时)
                                                     │
                                                     ▼
                                            展示预览列表
                                            等待 /确认删除
                                                     │
用户发送 /确认删除 ─────────────────────────────────┘
     │
     ▼
检查 _pending_confirmations
     ├── 无待确认 → "没有待确认的操作"
     ├── 已超时   → "确认已超时"
     └── 有效 ────────────────────────┐
                                      ▼
                            _delete_all_files()
                            逐个调用 OneBot delete_group_file
                                      │
                                      ▼
                            _build_forward_nodes()
                            构建合并转发消息节点
                                      │
                                      ▼
                            _send_group_forward_msg()
                            发送合并转发到群
                                      │
                                      ▼
                            add_history_entry()
                            写入 JSON 历史记录
```

### LLM 工具交互流程

```
用户自然语言请求
     │
     ▼
AstrBot → LLM → function call 匹配
     │
     ├── delete_group_files
     │   ├── confirmed=false → 返回预览 (LLM 告知用户)
     │   └── confirmed=true  → 执行删除 → 合并转发 + 历史记录
     │
     └── check_group_files
         └── 调用 OneBot API → 返回文件列表 (LLM 组织回答)
```

---

## 功能变更记录

### v6.3.0（当前版本）

- **新增** 删除操作两阶段确认机制（`/立即删除` 预览 → `/确认删除` 执行，60 秒超时）
- **新增** Web 管理面板（端口 1655），含群文件列表、清理历史、系统配置三个模块
- **新增** 权限控制机制：删除操作需群主/管理员身份，通过 OneBot `get_group_member_info` API 校验
- **新增** `storage.py` 持久化模块：清理历史记录（JSON）+ 系统配置（JSON）
- **新增** `web_server.py` aiohttp HTTP 服务，7 个 REST API 路由
- **新增** `web/index.html` 前端管理页面（响应式设计，零外部依赖）
- **更新** `delete_group_files` LLM 工具新增 `confirmed` 参数，支持分阶段执行
- **更新** `_ensure_task` 定时检查从硬编码改为读取 `data/auto_delete_config.json` 可配置时间
- **更新** `_delete_all_files` 新增 `dry_run` 参数，支持仅预览不删除
- **更新** `requirements.txt` 新增 `aiohttp` 依赖
- **更新** 所有删除操作完成后自动记录清理历史

### v6.2.x（前版）

- 合并转发删除报告（OneBot v11 node 消息段）
- 多字段兼容 `extract_file_list`、`_get_group_id`
- `_send_group_forward_msg` 静态方法
- LLM 工具注册（`delete_group_files`、`check_group_files`）
- 项目结构规范化（`metadata.yaml`、`requirements.txt`、`.gitignore`）

### v6.0.x（初版）

- 基础 `/立即删除` 命令
- 每月 1 号 00:00 定时自动清理
- `/测文件` 调试命令

---

## 安装配置指南

### 环境要求

- **Python** 3.10+
- **AstrBot** 框架（含 OneBot v11 协议适配器）
- **OneBot 实现**（NapCatQQ / LLOneBot / Lagrange.Core 等），HTTP API 端口默认 3000

### 安装步骤

1. **安装依赖**

```bash
# 进入插件目录
cd data/plugins/auto_delete_files

# 安装 Python 依赖
pip install -r requirements.txt
# → httpx + aiohttp
```

2. **确保目录结构正确**

```
AstrBot/
└── data/
    └── plugins/
        └── auto_delete_files/
            ├── main.py
            ├── metadata.yaml
            ├── requirements.txt
            ├── storage.py
            ├── web_server.py
            ├── web/
            │   └── index.html
            └── README.md
```

3. **重启 AstrBot**

在 WebUI 插件管理页面重载插件，或重启 AstrBot 进程。

启动后控制台应输出：

```
[WebUI] 管理面板已启动: http://0.0.0.0:1655
```

### 配置项

**OneBot API 地址：** 编辑 `main.py` L16：

```python
HTTP_BASE = "http://127.0.0.1:3000"  # 修改为你的 OneBot HTTP API 地址
```

**Web 管理面板端口：** 编辑 `main.py` L17：

```python
WEB_PORT = 1655
```

**自动清理时间：** 通过 Web 管理面板 "系统配置" 标签页修改，或直接编辑 `data/auto_delete_config.json`：

```json
{
  "auto_clean_day": 1,
  "auto_clean_time": "03:00"
}
```

首次运行后会自动生成默认配置。

---

## 使用方法

### QQ 群聊命令

| 命令 | 说明 | 权限要求 |
|------|------|----------|
| `/立即删除` | 扫描并预览群根目录文件，等待二次确认 | 群主/管理员 |
| `/确认删除` | 在 `/立即删除` 后 60 秒内执行实际删除 | 群主/管理员 |
| `/测文件` | 输出 API 原始返回数据 + 文件解析结果（调试用） | 无限制 |

### LLM 自然语言触发

| 自然语言示例 | 对应工具 |
|-------------|----------|
| "帮我清理群文件" | `delete_group_files` |
| "删除群里所有文件" | `delete_group_files` |
| "看看群里有啥文件" | `check_group_files` |
| "群文件列表" | `check_group_files` |

### 定时清理

无需手动干预。插件启动后自动在配置的日期和时间执行清理。

日志示例：

```
[INFO] [自动删除] 触发每月1号03:00清理
[INFO] [自动删除] 群123456: 处理12个文件, 日志=🗑️ 已删：报告.pdf; 🗑️ 已删：截图.png; ...
```

---

## Web 管理面板

### 启动方式

插件加载后自动启动，无需手动配置。浏览器访问：

```
http://<服务器IP>:1655/
```

### 功能模块

#### 群文件列表

1. 从下拉菜单选择目标群聊
2. 输入自己的 QQ 号，点击 **验证权限**（蓝色标签表示已授权）
3. 文件以表格形式展示：文件名、大小、上传者、上传时间
4. 勾选文件 → 点击 **删除选中** → 弹窗确认 → 执行删除

#### 清理历史

- 展示最近 500 条清理记录
- 每行包含：执行时间、群号、操作者、成功/失败数、文件名（悬停查看完整列表）

#### 系统配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| 执行日期 | 每月第几天执行自动清理（1-28） | 1 |
| 执行时间 | 当日执行的具体时刻（HH:MM） | 00:00 |

点击 **保存配置** 后即时生效，无需重启插件。

---

## API 接口文档

### 通用说明

- **Base URL:** `http://<服务器IP>:1655`
- **Content-Type:** `application/json`
- **字符编码:** UTF-8
- 所有接口返回 JSON

---

### GET /api/groups

获取所有群聊列表。

**响应示例：**

```json
[
  {"group_id": 123456789, "group_name": "技术交流群"},
  {"group_id": 987654321, "group_name": "项目管理群"}
]
```

---

### GET /api/files/{group_id}

获取指定群根目录文件列表（自动过滤文件夹和 `busid=2` 条目）。

**响应示例：**

```json
{
  "files": [
    {
      "file_id": "abc123",
      "file_name": "周报.pdf",
      "file_size": 2150400,
      "busid": 102,
      "upload_time": 1720000000,
      "uploader": "张三",
      "is_folder": false
    }
  ],
  "group_id": 123456789
}
```

---

### POST /api/delete/{group_id}

删除指定群的指定文件。

**请求体：**

```json
{
  "file_ids": ["abc123", "def456"]
}
```

**响应示例：**

```json
{
  "deleted": 1,
  "failed": 1,
  "results": [
    {"file_id": "abc123", "status": "deleted"},
    {"file_id": "def456", "status": "failed", "error": "文件不存在"}
  ]
}
```

---

### GET /api/history

获取清理历史记录（最多 500 条）。

**响应示例：**

```json
[
  {
    "time": "2026-07-01 03:00:05",
    "group_id": 123456789,
    "operator": "张三",
    "deleted_count": 12,
    "failed_count": 1,
    "total": 13,
    "files": ["周报.pdf", "截图.png", "..."]
  }
]
```

---

### GET /api/config

获取当前系统配置。

```json
{"auto_clean_day": 1, "auto_clean_time": "03:00"}
```

---

### POST /api/config

更新系统配置。

**请求体：**

```json
{"auto_clean_day": 5, "auto_clean_time": "02:30"}
```

**响应：**

```json
{"status": "ok", "config": {"auto_clean_day": 5, "auto_clean_time": "02:30"}}
```

---

### GET /api/permission/{group_id}/{user_id}

校验用户在指定群的角色。

```json
{"role": "admin", "is_admin": true}
```

---

## 常见问题与故障排除

### Q1: 插件加载后 Web 面板无法访问？

- 检查服务器防火墙是否放行 **1655** 端口
- 确认没有其他程序占用 1655 端口：`netstat -ano | findstr 1655`
- 确认 AstrBot 日志中有 `[WebUI] 管理面板已启动` 输出

### Q2: `/立即删除` 或 `/测文件` 提示 API 请求失败？

- 检查 OneBot 实现（NapCatQQ / LLOneBot 等）是否正常运行
- 确认 `main.py` 中 `HTTP_BASE` 地址与 OneBot HTTP API 地址一致
- 使用 `/测文件` 查看原始 API 返回数据，排查数据格式问题

### Q3: 权限验证失败？

- 确认你确实是该群的 **群主** 或 **管理员**
- Web 面板中需输入 QQ 号（数字），非昵称

### Q4: 删除报告没有以合并转发形式发送？

- 确认 OneBot 实现支持 `send_group_forward_msg` API（NapCatQQ ≥ v3.0 支持）
- 查看 AstrBot 日志中的错误信息

### Q5: 自动清理没有在配置的时间执行？

- 插件必须在该时刻处于运行状态（进程未重启）
- 每月仅执行一次，通过 `_last_exec_month` 防重
- 查看 AstrBot 日志确认是否触发

### Q6: 文件列表为空或缺少文件？

- 插件仅处理 **根目录** 文件，不处理子文件夹
- `busid=2` 的文件（如群在线文档）会被自动过滤
- 使用 `/测文件` 查看 API 原始返回数据

### Q7: 如何修改 Web 面板端口？

编辑 `main.py` L17：

```python
WEB_PORT = 1655  # 修改为其他端口
```

重启插件生效。

---

> 本插件遵循 AstrBot 插件规范开发，依赖 OneBot v11 HTTP API 进行 QQ 通信且仅在Window+Napcat测试（本插件为AI辅助开发制作，AI仅制作重复代码，不负责核心代码）
