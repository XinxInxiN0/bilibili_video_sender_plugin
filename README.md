在群里发送B站视频链接，麦麦自动解析并发送视频
使用方法
下载本插件

将插件解压到麦麦的 plugins 目录

下载 ffmpeg

解压 ffmpeg

把解压后的 ffmpeg 文件夹放到 bilibili_video_sender_plugin 目录下

打开 config.toml 文件，填写 sessdata 和 buvid3（获取方法见下方）

用的开心 😊

sessdata 和 buvid3 获取方法
打开浏览器，进入 B 站（以 Chrome 为例）

按 F12 打开开发者工具

<img width="1920" height="1080" alt="图像"> src="https://github.com/user-attachments/assets/d8b040de-a038-4772-b588-26df92d5ce73" />
找到 “Application” 或 “应用” 栏目，点击展开

<img width="1054" height="34" alt="图片"> src="https://github.com/user-attachments/assets/0b8a5954-d6cd-47b6-95b9-126115203907" />
按 F5 刷新页面

找到 “Cookies” 并展开

<img width="220" height="28" alt="图像"> src="https://github.com/user-attachments/assets/4dc9c217-f78d-4d68-bb00-71ace2d3381f" />
找到域名为 bilibili 的 Cookie 并点击

<img width="292" height="29" alt="图像"> src="https://github.com/user-attachments/assets/d82e3b15-64cd-490b-8eea-c6258ca0f6e2" />
在 “Value” 一栏中找到 sessdata 和 buvid3 的值

<img width="714" height="483" alt="图像"> src="https://github.com/user-attachments/assets/607aa291-c927-4d00-8975-5e85fa0d1214" />
将获取到的 sessdata 和 buvid3 填入 config.toml 文件中即可
