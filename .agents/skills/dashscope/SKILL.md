---
name: dashscope
description: DashScope (Alibaba Cloud Bailian / 阿里云百炼) integration — Wan video, image generation, text-to-speech, and ASR with word-level timestamps.
---

# DashScope

Requires `DASHSCOPE_API_KEY` in `.env`. Get one at https://dashscope.aliyun.com/.

## Current API

**CRITICAL:** DashScope's `/compatible-mode/v1/` only supports `/chat/completions` and `/embeddings`. Video, image generation, TTS, and ASR all use **DashScope-native endpoints** — not OpenAI-compatible paths.

All tools use `Authorization: Bearer $DASHSCOPE_API_KEY`.

### Wan Video Generation

```text
POST https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis
Header: X-DashScope-Async: enable
GET  https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}
```

- Text model: `wan2.6-t2v` by default.
- Image model: `wan2.6-i2v-flash` by default.
- Wan 2.6 accepts integer durations from 2 through 15 seconds.
- 720P text-to-video sizes: `1280*720`, `720*1280`, `960*960`.
- Local first-frame images are encoded as `data:image/...;base64,...`.
- A previously created remote task is resumed with `provider_task_id`; never create a replacement task merely because local polling timed out.
- Result URLs are temporary and signed. Download them immediately and never persist or log them.

### Image Generation

```text
POST https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
```

- Model: `qwen-image-2.0-pro` (default), `qwen-image-max`, `wan2.7-image`, `z-image-turbo`
- Body: `{model, input: {messages: [{role: "user", content: [{text: "prompt"}]}]}, parameters: {size: "W*H", n, prompt_extend, watermark}}`
- **Size format uses asterisk:** `"1024*1024"` not `"1024x1024"`
- Response: `output.choices[0].message.content[0].image` (URL, valid ~24h) — must download separately

### Text-to-Speech

```text
POST https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
```

Same endpoint as image gen, different body.

- Model: `qwen3-tts-flash` (default), `qwen3-tts-instruct-flash`, `qwen-tts-2025-05-22`
- Body: `{model, input: {text, voice: "Cherry", language_type: "Auto"}}`
- Response: `output.audio.url` (WAV, valid ~24h) — must download separately

### ASR with Word-Level Timestamps

```text
POST https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription
Header: X-DashScope-Async: enable
```

- Model: `qwen3-asr-flash-filetrans` (NOT `qwen3-asr-flash` — the sync version has no word timestamps)
- Body: `{model, input: {file_url: "https://public-url/audio.mp3"}, parameters: {enable_words: true, language_hints: ["zh","en"]}}`
- Returns `task_id` → poll `GET /api/v1/tasks/{task_id}` until `SUCCEEDED` → download `output.result.transcription_url` → JSON with `transcripts[].sentences[].words[]`
- Timestamps in `begin_time`/`end_time` are in **milliseconds** — the tool normalizes to seconds

## OpenMontage Usage

### Image via selector

```python
from tools.graphics.image_selector import ImageSelector

result = ImageSelector().execute({
    "preferred_provider": "dashscope",
    "prompt": "一只猫坐在沙发上",
    "output_path": "projects/my-video/assets/images/cat.png",
})
```

### Video via selector

```python
from tools.video.video_selector import VideoSelector

result = VideoSelector().execute({
    "preferred_provider": "dashscope",
    "allowed_providers": ["dashscope"],
    "operation": "text_to_video",
    "prompt": "单镜头，镜头穿过清晰的数据流，最后聚焦产品界面",
    "duration": 5,
    "aspect_ratio": "16:9",
    "output_path": "projects/my-video/assets/video/hero.mp4",
})
```

### TTS via selector

```python
from tools.audio.tts_selector import TTSSelector

result = TTSSelector().execute({
    "preferred_provider": "dashscope",
    "text": "如果 AI 真的会改变未来，普通人到底该怎么参与？",
    "voice": "Cherry",
    "output_path": "projects/my-video/assets/audio/narration.wav",
})
```

### ASR directly (word timestamps for subtitles)

```python
from tools.analysis.dashscope_asr import DashscopeAsr

result = DashscopeAsr().execute({
    "audio_url": "https://example.com/narration.wav",
    "output_path": "projects/my-video/assets/audio/transcription.json",
})

# result.data["words"] is a flat list of {text, begin_time_seconds, end_time_seconds}
```

## Recommended Workflow

1. **Video:** Generate one 2-5 second sample first. State the provider, model and cost ceiling before the paid call. Prefer one continuous camera action per hero shot.
2. **Image:** Generate a sample first. Check `prompt_extend: true` (default) — DashScope rewrites your prompt for better results. Disable if you need literal prompt adherence.
3. **TTS:** Generate a 10-15 second sample before full narration. Approve voice and pacing before committing to full generation.
4. **ASR:** Audio must be at a **publicly accessible URL**. Upload to any public host (S3, etc.) first. Local paths are rejected with a clear error.
5. **Subtitles:** Build from `result.data["words"]` — each word has `begin_time_seconds` and `end_time_seconds`. Group words into caption phrases by language semantics, not fixed character count.

## Parameters

### Video (`dashscope_video`)
- `prompt` (required): one-shot visual and motion description
- `operation`: `text_to_video` or `image_to_video`
- `duration`: integer from 2 through 15 seconds
- `aspect_ratio`: `16:9`, `9:16`, or `1:1`
- `reference_image_path` / `reference_image_url`: first frame for image-to-video
- `provider_task_id`: resume an existing paid remote task without creating another
- `poll_interval_seconds`: default `5`
- `timeout_seconds`: default `300`
- `output_path`: local MP4 destination

### Image (`dashscope_image`)
- `prompt` (required): text prompt
- `model`: default `qwen-image-2.0-pro`
- `size`: default `"1024*1024"` — **asterisk separator, not "x"**
- `n`: 1-6 images
- `negative_prompt`: things to avoid (max 500 chars)
- `prompt_extend`: default `true` — auto-rewrite prompt for better results
- `watermark`: default `false`
- `seed`: for reproducibility

### TTS (`dashscope_tts`)
- `text` (required): text to synthesize (max 600 chars for qwen3-tts-flash)
- `model`: default `qwen3-tts-flash`
- `voice`: default `"Cherry"` — other voices: `"Ethan"`, `"Chelsie"`, etc.
- `language_type`: default `"Auto"` — `"Chinese"`, `"English"`, `"Japanese"`, `"Korean"`
- `instructions`: natural language delivery instructions (only for `qwen3-tts-instruct-flash`)

### ASR (`dashscope_asr`)
- `audio_url` (required): **must be publicly accessible URL**
- `model`: `qwen3-asr-flash-filetrans` (only model that supports word timestamps)
- `language_hints`: default `["zh", "en"]`
- `enable_words`: default `true` — required for word-level timestamps
- `poll_interval_seconds`: default `5.0`
- `timeout_seconds`: default `300`

## Troubleshooting

- **Video polling timeout:** Keep the returned `task_id` and resume it. Do not submit the same paid shot again.
- **Video 1:1 size error:** Wan 2.6 720P square is `960*960`, not `720*720`.
- **Image-to-video reference error:** Use a public/OSS URL or a supported local image up to 20 MB; local images are Base64 encoded.
- **Image size error:** Use `"W*H"` with asterisk, not `"WxH"`. Example: `"2048*2048"`.
- **TTS no audio URL:** Check `output.audio.url` — if empty, the model name or voice may be wrong.
- **ASR "file not accessible":** `audio_url` must be publicly reachable. DashScope servers fetch the file; local paths and auth-gated URLs don't work.
- **ASR poll timeout:** Increase `timeout_seconds` (default 300). Long audio files take longer to transcribe.
- **ASR no word timestamps:** Ensure `enable_words: true` and model is `qwen3-asr-flash-filetrans` (not the sync `qwen3-asr-flash`).
- **Auth error (401):** Verify `DASHSCOPE_API_KEY` is set. Use `Authorization: Bearer $KEY` header.

## Safety

Never print or write the API key to logs, metadata, patches, or project artifacts. `.env.example` should contain only empty variable names. The tool's `_safe_error()` method redacts the key from error messages.
