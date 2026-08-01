---
name: video-understanding
description: 使用用户配置的外部视频分析服务理解本地视频。适用于描述视频、回答视频内容问题，也为 Crew LLM Wiki 的视频材料解析提供能力；上传前必须取得用户明确确认。
metadata:
  skillCategoryName: 音视频处理
  version: 1.0.0
  crew:
    emoji: 🎬
    requires:
      bins: [python3]
      env: [VLM_VIDEO_UPLOAD_URL, VLM_VIDEO_ANALYZE_URL, VLM_VIDEO_MODEL]
    primaryEnv: VLM_API_KEY
  zh_name: 视频理解
  zh_description: 使用用户配置的外部视频分析服务理解本地视频，并支持 Crew LLM Wiki 视频材料解析。
  query_examples:
    - 这个视频里有什么内容？
    - 帮我分析一下这段视频
    - 描述一下这个视频
python: ">=3.8"
---

# 视频理解

本 Skill 会把本地视频上传到用户配置的外部服务，再调用视频分析接口。Crew 不预置服务地址或 API Key，配置不完整时不会上传文件。

## 配置

```dotenv
VLM_VIDEO_UPLOAD_URL=https://your-media-service.example/upload
VLM_VIDEO_ANALYZE_URL=https://your-media-service.example/analyze
VLM_VIDEO_MODEL=your-video-model
VLM_API_KEY=your-api-key
```

上传接口接收 multipart 字段 `file`，响应通过 `fileUrl`、`filePath` 或 `url` 返回媒体地址；分析接口接收 `model`、`prompt`、`video` 和 `stream` 字段。分析结果可使用 OpenAI 兼容的 `choices[0].message.content`，或 `result.text` / `data.text`。

## 使用

视频会离开本机，必须先向用户说明目标服务及数据外传风险，并取得明确确认：

```bash
python scripts/video_understand.py /path/to/video.mp4 --confirm-upload
python scripts/video_understand.py /path/to/video.mp4 --prompt "视频中的关键步骤是什么？" --confirm-upload
```

支持 `mp4`、`mov`、`webm`、`avi`、`mkv`、`flv`、`m4v`，单个文件上限 100 MB。不得在上层代码中默认添加 `--confirm-upload`。
