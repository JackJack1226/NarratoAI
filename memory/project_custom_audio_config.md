# Pipeline Configuration: Custom Audio Integration

This file stores the configuration for custom audio assets (original video sounds) and voice mapping for the automation pipeline.

## Audio Assets
- **Custom Sound Path**: `C:\Users\1\Desktop\undefined.mp3`
  - **Usage**: To be inserted into the video-audio track according to script metadata.

## Workflow Plan
1.  **Parser Update**: The script parser will be updated to accept `file_path` for `type: "original_audio"` entries, pointing to absolute paths like the one above.
2.  **Audio Mixer Integration**: The `ffmpeg` mixer will handle absolute paths to custom files, ensuring they are correctly mixed with TTS-generated segments.

---
**Why**: Linking the custom audio path to the project's memory ensures the pipeline can reliably locate and inject this specific file during the mixing phase without hardcoding paths in the orchestrator script.
**How to apply**: The `video_automation_pipeline.py` will read this path from the metadata whenever a segment of type `original_audio` is encountered.
