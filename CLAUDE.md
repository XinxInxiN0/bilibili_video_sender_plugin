# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

**麦麦发 B 站视频** (`xinxinxin0.bilibili-video-sender`) 是一个 MaiBot SDK 2.0 插件，自动解析用户消息中的 B 站视频链接并发送视频到群聊或私聊。插件在独立子进程中运行（进程隔离），通过 MsgPack-RPC 代理与宿主机通信。

## 架构概览

### 核心模块

| 文件 | 作用 |
|------|------|
| `plugin.py` | 主插件类、事件处理器、视频处理流水线 |
| `parser.py` | B站 API 调用、URL 解析、WBI 签名、流选择 |
| `downloader.py` | DASH/durl 视频下载、FFmpeg 合并、临时文件管理 |
| `sender.py` | SDK 发送 + OneBot HTTP API 降级发送 |
| `ffmpeg.py` | 跨平台 FFmpeg 检测、硬件编码器检测、视频压缩 |
| `utils.py` | WSL 路径转换、进度条、临时目录管理 |
| `config.toml` | 用户配置（B站认证、FFmpeg、环境模式等） |
| `_manifest.json` | 插件元数据与 SDK 版本约束 |

### 处理流水线

```
用户消息 → @HookHandler(chat.receive.after_process, BLOCKING, EARLY)
    ↓
解析 B 站 URL（支持 processed_plain_text 和小卡片 type="dict"）
    ↓
spawn asyncio.create_task(_process_video_task)  ← 立即返回，不阻塞 hook 链
    ↓
return {"action": "abort"}  ← 阻止 maisaka 处理（block_ai_reply=True 时）
    ↓
[后台任务]
    loop.run_in_executor → _resolve_video_sync   (B站 API, 选质量)
    await send_text "解析成功"
    loop.run_in_executor → download_video        (DASH/durl 下载+合并)
    loop.run_in_executor → _maybe_compress       (超限时压缩)
    await send_video                             (SDK → 降级 OneBot HTTP)
    loop.run_in_executor → _cleanup_files
```

**关键异步模式**：Hook handler 是 `async def`，但阻塞操作（下载、编码）通过 `loop.run_in_executor(None, ...)` 移到线程池，避免阻塞 hook 执行链。

**为何使用 HookHandler 而非 EventHandler**：MaiBot `bot.py` 中 `ON_MESSAGE` 事件触发代码已被注释（`# TODO: 修复事件预处理部分`），EventHandler 永远不会被调用。`chat.receive.after_process` Hook 在 `message.process()` 完成后、maisaka 路由之前触发，此时 `processed_plain_text` 已填充，是正确的拦截点。注意：`before_process` 在 `message.process()` 调用前触发，`processed_plain_text` 尚为 `None`，不可用于 URL 检测。

### 发送降级策略

1. `ctx.send.custom(custom_type="video", ...)` — SDK 标准路径
2. OneBot HTTP API (`/send_group_msg` 或 `/send_private_msg`) — 降级路径

### 配置模型层次（Pydantic）

```
PluginConfig
├── BilibiliConfig   → sessdata, qn, group_at_only, block_ai_reply, 压缩/时长限制
├── ParserConfig     → enable_miniapp_card
├── FFmpegConfig     → 硬件加速、编码器优先级
├── EnvironmentConfig → runtime_mode (windows|wsl|linux)
└── ApiConfig        → OneBot HTTP host:port:token
```

配置通过 `self.config.bilibili.xxx` 访问（强类型），或 `await self.ctx.config.get(...)` 动态访问。

## 关键设计约定

- **不能导入 `src.*`**：插件运行在隔离子进程，所有与宿主机交互必须通过 `self.ctx.*` RPC 代理。
- **`block_ai_reply`**：Hook handler 返回 `{"action": "abort"}` 阻止 maisaka 处理，或返回 `None` 允许 maisaka 继续。
- **临时文件**：`utils.get_download_temp_dir()` 返回环境感知路径（Docker → `/MaiMBot/data/tmp`，Linux → `/tmp/maibot_bilibili`，Windows → `plugin_dir/tmp`）。临时文件前缀为 `bilibili*`，在 `on_unload()` 时自动清理。
- **WSL 路径转换**：`runtime_mode = "wsl"` 时，发送视频前需用 `convert_windows_to_wsl_path()` 将 Windows 路径转为 WSL 挂载路径。
- **FFmpeg 查找顺序**：插件目录 `ffmpeg/bin/{windows|linux|darwin}/` → 系统 PATH。
- **视频质量 `qn`**：`0` = 自动（登录态默认 720P，未登录默认 480P）；严格模式 (`qn_strict=true`) 只接受精确匹配的质量级别。
- **WBI 签名**：B站 API 的反爬签名由 `parser.py` 中 `BilibiliWbiSigner` 处理，需定期刷新 mixin key。

## 插件元数据约束

`_manifest.json` 中声明：
- `sdk.min_version`: `2.3.0`，`sdk.max_version`: `2.99.0`
- `capabilities`: `["send.text", "send.custom", "config.get"]`

## 配置修改原则

修改 `config.toml` 时只需更新模版文件，并同步更新版本号注释行（`# 配置版本: x.x.x`）。无需为配置变更编写测试文件。

<!-- SPECKIT START -->
For additional context about technologies to be used, project structure,
shell commands, and other important information, read the current plan
<!-- SPECKIT END -->
