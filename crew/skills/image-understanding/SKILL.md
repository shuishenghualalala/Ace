---
name: image-understanding
description: 使用用户配置的 OpenAI 兼容视觉模型分析本地图片。适用于描述图片、识别图片内容、回答图片相关问题，也为 Crew LLM Wiki 的图片材料解析提供能力。
metadata:
  skillCategoryName: 图像处理
  version: 1.0.0
  crew:
    emoji: 🖼️
    requires:
      bins: [python3]
      env: [VLM_BASE_URL, VLM_MODEL]
    primaryEnv: VLM_API_KEY
  zh_name: 图片理解
  zh_description: 使用用户配置的 OpenAI 兼容视觉模型分析本地图片，并支持 Crew LLM Wiki 图片材料解析。
  query_examples:
    - 这张图片里有什么？
    - 帮我描述一下这张图片
    - 分析这张图片的内容
python: ">=3.8"
---

# 图片理解

本 Skill 通过用户自行配置的 OpenAI 兼容视觉模型分析单张本地图片。Crew 不提供默认密钥，也不会在配置不完整时发起网络请求。

## 配置

在 `CREW_ENV_FILE` 指向的文件或进程环境中配置：

```dotenv
VLM_BASE_URL=https://your-model-service.example/v1
VLM_MODEL=your-vision-model
VLM_API_KEY=your-api-key
```

`VLM_API_KEY` 对不需要鉴权的本地模型服务可以留空。接口需兼容 `POST /chat/completions`，并支持消息中的 `image_url` data URL。

## 使用

```bash
python scripts/image_understand.py /path/to/image.png
python scripts/image_understand.py /path/to/image.png --prompt "图片中的表格有哪些数据？"
```

支持 `jpg`、`jpeg`、`png`、`webp`、`bmp`、`gif`，单个文件上限 10 MB。图片内容会发送到用户配置的模型服务，请先确认该服务满足数据安全要求。
