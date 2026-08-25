> ### ⚠️ 停止维护通知 (Archived)
> 
> 因本人精力有限，本项目已停止维护与更新。目前社区已有功能更完善的替代插件，推荐迁移至：
> https://github.com/Ling-LA/ling-video-bot
> 
> 感谢大家一直以来的关注与支持。
# 使用说明
发送B站视频链接到群里，麦麦会自动解析并发送视频。
觉得好用的话，可以点个star
### 请务必认真填写config.toml！！！！！
### 请根据你的实际运行环境正确配置 runtime_mode（见下方"环境模式配置"章节）！！！
## 使用方法

1. 下载本插件。
2. 将插件解压到麦麦的 `plugins` 目录。
3. 下载 [ffmpeg](https://ffmpeg.org/)。（不要下载源代码！！！下Windows版啊，别拿着源代码来找我说你为什么用不了）
4. 解压 ffmpeg 并将文件夹重命名为 **ffmpeg**
5. 将解压后的 ffmpeg 文件夹放到 `bilibili_video_sender_plugin` 目录下。
6. 安装插件依赖：`pip install -r requirements.txt`。（缺少 `cryptography` 时插件仍能加载，但无法自动续期 B站登录态）
7. 先运行一次麦麦生成 `config.toml`。再按下方说明在 `[auth]` 段填入 B站登录凭据。
8. 在napcat上新建一个正向http（服务器）,并在config.toml内填入端口
9. 使用愉快 😊。

## 环境模式配置（重要！）

在 `config.toml` 的 `[environment]` 段中，需要根据你的实际运行环境配置 `runtime_mode`：

```toml
[environment]
runtime_mode = "windows"  # 根据实际情况选择：windows / wsl / linux
```

### 如何选择？

- **如果你用的是一件包或者不知道什么是 WSL**：选择 `windows`
- **如果你的 NapCat 在 WSL 中运行**：选择 `wsl`
- **如果你在 Linux 服务器上部署**：选择 `linux`

## 清晰度设置（config.toml）

在 `[bilibili]` 段

```toml
[bilibili]
qn = 0
qn_strict = false
```

- `qn=0` 为自动：有 `[auth]` 登录凭据默认请求 720P，无登录凭据默认请求 480P
- `qn_strict=true` 时清晰度不可用会直接报错（默认自动降级）

常见 `qn` 对应表：
- 16 = 360P, 32 = 480P, 64 = 720P, 74 = 720P60, 80 = 1080P, 112 = 1080P+
- 116 = 1080P60, 120 = 4K, 125 = HDR, 126 = 杜比视界, 127 = 8K

### URL 参数覆盖（v1.3.3+）

支持在 URL 中直接指定清晰度，无需修改配置文件：

```
https://www.bilibili.com/video/BV18Cm8BHEeD/?qn=116
```

- URL 中的 `qn` 参数会**覆盖**配置文件中的值
- 如果 URL 未携带 `qn` 参数，则使用配置文件的默认值
- 示例：`?qn=116` 表示下载 1080P60 高帧率版本
- 聊天消息中若链接后带标点，插件会自动清理末尾标点后再解析
- 若链接被平台裁剪导致查询串丢失，插件会从原始消息中兜底提取 `qn=`
- 短链（`b23.tv`）会先跳转再解析 `qn`


---

## B站登录凭据 config.toml 获取方法

1. 使用 Chrome 浏览器打开 B站主页。
2. 按下 `F12` 打开开发者工具。
3. 点击顶部的 `Application`（应用）选项卡。
4. 按 `F5` 刷新页面。
5. 在左侧栏找到 `Cookies` 并展开。
6. 找到 `bilibili` 相关的 Cookie 并点击。
7. 在右侧的 `Value` 列复制 `SESSDATA`、`bili_jct`、`DedeUserID`、`DedeUserID__ckMd5`、`sid`、`buvid3`、`buvid4`。
8. 在插件目录的 `config.toml` 中找到 `[auth]`，按下面格式填写。

> **注意**：`ac_time_value` 已废弃，B站不再在 localStorage 中提供此值，可留空。插件会通过定期 API 请求自动保活 Cookie。

```toml
[auth]
SESSDATA = ""
bili_jct = ""
DedeUserID = ""
DedeUserID__ckMd5 = ""
sid = ""
buvid3 = ""
buvid4 = ""
ac_time_value = ""  # 已废弃，可留空
```

这些值等同于 B站登录凭据，不要发给别人，也不要提交到 Git 仓库。插件会通过定期 keep-alive 请求自动保活 Cookie，并将更新后的值写回 `config.toml` 的 `[auth]`。


## 解析成功通知方式（v2.0.8+）

在 `[bilibili]` 段可以选择解析成功后的通知方式：

```toml
[bilibili]
success_notification_mode = "text"   # text=发送文字通知（默认）| emoji=对原消息贴表情回应
reaction_emoji_id = 124               # 表情回应时使用的 QQ 表情 ID（仅 emoji 模式生效）
```

- `text`（默认）：解析成功后发送一条「解析成功」文字消息
- `emoji`：解析成功后对用户发送的原始链接消息添加表情回应

> **注意**：表情回应仅在**群聊**中生效，私聊始终使用文字通知。

常用 QQ 表情 ID：

| ID | 表情 |
|----|------|
| 124 | OK |
| 144 | 烟花 |

---

## 短链解析与 DoH 兜底（v2.0.10+）

插件支持 `b23.tv` / `bili2233.cn` 短链，也兼容不带 `https://` 的裸链接。

短链需要先请求一次才能展开成真正的视频地址。部分网络环境下 `b23.tv` 的系统 DNS 会被污染到无法连通的 IP，导致短链展不开、消息看起来像被"忽略"。此时插件会自动改用 DoH 查询真实 IP 并直连（SNI 与证书校验仍按真实域名走，不降低安全性）。

```toml
[parser]
doh_enabled = true                                                    # 系统 DNS 解析失败时启用 DoH 兜底（默认开启）
doh_servers = ["https://223.5.5.5/resolve", "https://1.1.1.1/dns-query"]  # 按顺序尝试
```

若短链最终仍解析失败，插件会记录 WARNING 日志并回复一条提示，不再静默丢弃。

---

## 访问控制：群 / 用户黑白名单（v2.0.10+）

在 `[access]` 段可以按**群号**和**用户 QQ 号**两个维度限制哪些消息会触发 B 站解析。

```toml
[access]
group_mode = "off"          # off=不限制 | whitelist=仅名单内群 | blacklist=名单内群不解析
group_whitelist = ["123456789"]
group_blacklist = []
user_mode = "off"           # off=不限制 | whitelist=仅名单内用户 | blacklist=名单内用户不解析
user_whitelist = []
user_blacklist = ["10001"]
```

规则说明：

- **两个维度是「与」关系**：群维度和用户维度都通过，才会解析。
- **同一维度同一时刻只有一份名单生效**，由该维度的 `mode` 决定；`mode = "off"` 时两份名单都被忽略。
- **`whitelist` 模式下名单为空 = 全部拒绝**。想放行所有人请把 `mode` 设回 `"off"`，不要留一个空白名单。
- **私聊不受群名单影响**：私聊消息没有群号，群维度自动跳过；用户名单在群聊和私聊中都生效。
- **被名单拦下的消息会正常交给麦麦回复**，插件只是不解析链接，不会静音这条消息（与 `block_ai_reply` 无关）。
- 名单里的号码请写成**字符串**（带引号），写成数字也能兼容，但字符串更稳妥。

配置示例——只在两个群里启用，并屏蔽一个刷屏的人：

```toml
[access]
group_mode = "whitelist"
group_whitelist = ["123456789", "987654321"]
user_mode = "blacklist"
user_blacklist = ["10001"]
```

---
### napcat配置和config.toml
<img width="645" height="749" alt="image" src="https://github.com/user-attachments/assets/223c491f-8433-4c47-923a-c4c830c9e572" />
<img width="1186" height="807" alt="image" src="https://github.com/user-attachments/assets/10c79e45-048a-46c8-8d1d-ca7a4044070c" />
两个端口要保持一致

### 务必认真填写config.toml!!!!!!


## 完成后的文件夹结构示例
<img width="412" height="131" alt="image" src="https://github.com/user-attachments/assets/63ef60df-99f3-4c79-b124-da566fd15cd0" />
<img width="659" height="182" alt="image" src="https://github.com/user-attachments/assets/ddeb422f-b9fc-49b6-a652-866d06eb812c" />



