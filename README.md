# 使用说明
发送B站视频链接到群里，麦麦会自动解析并发送视频。
还有问题加qq3087033824
觉得好用的话，可以点个star
### 请务必认真填写config.toml！！！！！
### 请根据你的实际运行环境正确配置 runtime_mode（见下方"环境模式配置"章节）！！！
## 使用方法

1. 下载本插件。
2. 将插件解压到麦麦的 `plugins` 目录。
3. 下载 [ffmpeg](https://ffmpeg.org/)。（不要下载源代码！！！下Windows版啊，别拿着源代码来找我说你为什么用不了）
4. 解压 ffmpeg 并将文件夹重命名为 **ffmpeg**
5. 将解压后的 ffmpeg 文件夹放到 `bilibili_video_sender_plugin` 目录下。
6. 先运行一次麦麦生成config.toml。再打开 `config.toml`，填入 `sessdata` 和 `buvid3`（获取方法见下方）。
7. 在napcat上新建一个正向http（服务器）,并在config.toml内填入端口
8. 使用愉快 😊。

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

- `qn=0` 为自动：有 SESSDATA 默认请求 720P，无 SESSDATA 默认请求 480P
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

## sessdata 和 buvid3 获取方法

1. 使用 Chrome 浏览器打开 B站主页。
2. 按下 `F12` 打开开发者工具。
3. 点击顶部的 `Application`（应用）选项卡。
4. 按 `F5` 刷新页面。
5. 在左侧栏找到 `Cookies` 并展开。
6. 找到 `bilibili` 相关的 Cookie 并点击。
7. 在右侧的 `Value` 列找到 `sessdata` 和 `buvid3` 的值。
8. 将这两个值填入 `config.toml` 文件中对应的位置。

### 参考截图

- 开发者工具打开界面  
  ![开发者工具界面](https://github.com/user-attachments/assets/d8b040de-a038-4772-b588-26df92d5ce73)

- Application 栏  
  ![Application 栏](https://github.com/user-attachments/assets/0b8a5954-d6cd-47b6-95b9-126115203907)

- Cookie 位置  
  ![Cookie 位置](https://github.com/user-attachments/assets/4dc9c217-f78d-4d68-bb00-71ace2d3381f)

- bilibili Cookie  
  ![bilibili Cookie](https://github.com/user-attachments/assets/d82e3b15-64cd-490b-8eea-c6258ca0f6e2)

- sessdata 和 buvid3 示例  
  ![sessdata 和 buvid3](https://github.com/user-attachments/assets/607aa291-c927-4d00-8975-5e85fa0d1214)

---
### napcat配置和config.toml
<img width="645" height="749" alt="image" src="https://github.com/user-attachments/assets/223c491f-8433-4c47-923a-c4c830c9e572" />
<img width="1186" height="807" alt="image" src="https://github.com/user-attachments/assets/10c79e45-048a-46c8-8d1d-ca7a4044070c" />
两个端口要保持一致

### 务必认真填写config.toml!!!!!!


## 完成后的文件夹结构示例
<img width="412" height="131" alt="image" src="https://github.com/user-attachments/assets/63ef60df-99f3-4c79-b124-da566fd15cd0" />
<img width="659" height="182" alt="image" src="https://github.com/user-attachments/assets/ddeb422f-b9fc-49b6-a652-866d06eb812c" />



