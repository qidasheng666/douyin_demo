# Douyin Demo

一个用于获取抖音视频信息的 Python 工具，支持：

- 获取视频标题与真实播放地址
- 下载视频到本地
- 使用阿里云百炼语音识别提取视频文案
- 获取抖音博主主页中的视频链接

## 环境要求

- Python 3.10+
- Google Chrome 或 Chromium
- 阿里云百炼 API Key（仅“视频转文字”功能需要）

## 安装

```bash
git clone https://github.com/qidasheng666/douyin_demo.git
cd douyin_demo

python -m venv .venv
source .venv/bin/activate  # Windows 请使用 .venv\Scripts\activate

pip install -r requirements.txt
