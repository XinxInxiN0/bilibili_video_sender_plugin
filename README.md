在群里发送b站视频链接，麦麦就会自动解析并发送视频
使用方法
1.下载本插件
2.把插件解压到麦麦的plugins目录
3.下载ffmpeg(https://ffmpeg.org/)
4.解压ffmpeg
5.把ffmpeg放到bilibili_video_sender_plugin目录下
6.打开config.toml并填入sessdata和buvid3（获取方式在下面）
7.用的开心😊
sessdata和buvid3获取
1.打开浏览器并打开b站（以Chrome为例）
2.按F12打开开发者工具
<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/d8b040de-a038-4772-b588-26df92d5ce73" />
3.找到Application或者应用一栏，点开
<img width="1054" height="34" alt="image" src="https://github.com/user-attachments/assets/0b8a5954-d6cd-47b6-95b9-126115203907" />
4.按F5刷新页面
5.找到Cookie点开
<img width="220" height="28" alt="image" src="https://github.com/user-attachments/assets/4dc9c217-f78d-4d68-bb00-71ace2d3381f" />
6.找到bilibili点开
<img width="292" height="29" alt="image" src="https://github.com/user-attachments/assets/d82e3b15-64cd-490b-8eea-c6258ca0f6e2" />
7.Value一栏里边就是要找的东西
<img width="714" height="483" alt="image" src="https://github.com/user-attachments/assets/607aa291-c927-4d00-8975-5e85fa0d1214" />
8.把找到的sessdata和buvid3填到config.toml里就好了
完成后的文件夹结构应该是这样的
│  config.toml
│  LICENSE
│  plugin.py
│  _manifest.json
│
└─ffmpeg
    │  LICENSE
    │  README.txt
    │
    ├─bin
    │      ffmpeg.exe
    │      ffplay.exe
    │      ffprobe.exe
    │
    ├─doc
    │      bootstrap.min.css
    │      community.html
    │      default.css
    │      developer.html
    │      faq.html
    │      fate.html
    │      ffmpeg-all.html
    │      ffmpeg-bitstream-filters.html
    │      ffmpeg-codecs.html
    │      ffmpeg-devices.html
    │      ffmpeg-filters.html
    │      ffmpeg-formats.html
    │      ffmpeg-protocols.html
    │      ffmpeg-resampler.html
    │      ffmpeg-scaler.html
    │      ffmpeg-utils.html
    │      ffmpeg.html
    │      ffplay-all.html
    │      ffplay.html
    │      ffprobe-all.html
    │      ffprobe.html
    │      general.html
    │      git-howto.html
    │      libavcodec.html
    │      libavdevice.html
    │      libavfilter.html
    │      libavformat.html
    │      libavutil.html
    │      libswresample.html
    │      libswscale.html
    │      mailing-list-faq.html
    │      nut.html
    │      platform.html
    │      style.min.css
    │
    └─presets
            libvpx-1080p.ffpreset
            libvpx-1080p50_60.ffpreset
            libvpx-360p.ffpreset
            libvpx-720p.ffpreset
            libvpx-720p50_60.ffpreset


