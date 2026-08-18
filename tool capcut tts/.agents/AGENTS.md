# CapCut TTS API - AI Rules and Customizations

## 1. Troubleshooting & Known API Quirks
Whenever you encounter network errors, parsing errors, or hanging execution while working on this repository, you **MUST** read the [TROUBLESHOOTING.md](file:///d:/tong-hop-tool-video/tool%20capcut%20tts/TROUBLESHOOTING.md) file located in the root of the project BEFORE attempting to debug or modify code. 

The `TROUBLESHOOTING.md` file contains documented solutions for:
- API status string changes (`"succeed"` vs `"success"`).
- VOD chunked uploading requirements (to avoid `ConnectionResetError`).
- ffmpeg audio extraction (to avoid slow 50MB video uploads).
- Proper payload decoding for `generate_speech`.

Always strictly adhere to these established solutions rather than trying to reinvent the wheel.
