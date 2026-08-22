import os
import json
import base64
import requests
import asyncio
import edge_tts
from capcut_tts_api import CapCutClient

def main():
    client = CapCutClient()
    voices = client.list_voices(lang="vi-VN")
    
    os.makedirs("voice_samples", exist_ok=True)
    
    html_content = """
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>CapCut TTS - Review Giọng Đọc</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; background-color: #f5f5f5; }
            .voice-card { background: white; border-radius: 8px; padding: 15px; margin-bottom: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            h3 { margin-top: 0; color: #333; }
            .voice-type { color: #666; font-size: 0.9em; margin-bottom: 10px; }
            audio { width: 100%; margin-top: 10px; }
            .loading { color: orange; font-style: italic; }
        </style>
    </head>
    <body>
        <h1>Review Các Giọng Đọc (CapCut TTS & Edge TTS)</h1>
        <p>Đây là file tổng hợp nghe thử các giọng đọc có sẵn trên CapCut và Edge TTS.</p>
    """
    
    test_text = "Xin chào, đây là giọng đọc thử nghiệm để bạn nghe trước."
    
    # 1. Edge TTS
    edge_voices = [
        {"name": "[Miễn Phí] Edge TTS - Nữ (Hoài My)", "voice": "vi-VN-HoaiMyNeural"},
        {"name": "[Miễn Phí] Edge TTS - Nam (Nam Minh)", "voice": "vi-VN-NamMinhNeural"}
    ]
    
    async def generate_edge_tts(text, voice, save_path):
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(save_path)
    
    for v in edge_voices:
        print(f"Generating Edge TTS: {v['name']}...")
        save_path = f"voice_samples/{v['voice']}.mp3"
        if not os.path.exists(save_path):
            asyncio.run(generate_edge_tts(test_text, v["voice"], save_path))
        
        html_content += f"""
        <div class="voice-card">
            <h3>{v['name']}</h3>
            <div class="voice-type">{v['voice']}</div>
            <audio controls preload="none">
                <source src="voice_samples/{v['voice']}.mp3" type="audio/mpeg">
                Trình duyệt của bạn không hỗ trợ thẻ audio.
            </audio>
        </div>
        """
        
    # 2. CapCut Voices
    for v in voices:
        print(f"Generating CapCut Voice: {v.display_name}...")
        safe_name = v.display_name.replace("/", "_").replace("\\", "_").replace(" ", "_")
        safe_voice_type = v.voice_type.replace("/", "_").replace("\\", "_")
        save_path = f"voice_samples/capcut_{safe_name}_{safe_voice_type}.mp3"
        
        if not os.path.exists(save_path):
            try:
                # Chú ý: Cần sleep 1 tí để tránh bị ban / rate limit
                res = client.generate_speech(test_text, voice=v.voice_type)
                
                tasks = (res.get("data") or {}).get("tasks") or []
                if not tasks:
                    print(f"Không có task data cho {v.display_name}")
                    continue
                    
                task_data = tasks[0]
                video_url = None
                audio_base64 = None
                
                payload_str = task_data.get("payload", "")
                if payload_str:
                    try:
                        payload_json = json.loads(payload_str)
                        audio_subtitles = payload_json.get("audio_subtitles", [])
                        if audio_subtitles and len(audio_subtitles) > 0:
                            video_url = audio_subtitles[0].get("speech_url")
                    except Exception:
                        pass
                
                if not video_url:
                    if "video_url" in task_data:
                         video_url = task_data["video_url"]
                    elif "audio" in task_data:
                         audio_base64 = task_data["audio"]

                if video_url:
                    resp = requests.get(video_url, timeout=60)
                    resp.raise_for_status()
                    with open(save_path, "wb") as f:
                        f.write(resp.content)
                elif audio_base64:
                    with open(save_path, "wb") as f:
                        f.write(base64.b64decode(audio_base64))
                        
            except Exception as e:
                print(f"Error generating {v.display_name}: {e}")
                continue
                
        html_content += f"""
        <div class="voice-card">
            <h3>{v.display_name}</h3>
            <div class="voice-type">{v.voice_type} (ID: {v.resource_id})</div>
            <audio controls preload="none">
                <source src="voice_samples/capcut_{safe_name}_{safe_voice_type}.mp3" type="audio/mpeg">
                Trình duyệt của bạn không hỗ trợ thẻ audio.
            </audio>
        </div>
        """
        
    html_content += """
    </body>
    </html>
    """
    
    with open("review_giong_doc.html", "w", encoding="utf-8") as f:
        f.write(html_content)
        
    print("Done! Open review_giong_doc.html to see the review.")

if __name__ == "__main__":
    main()
