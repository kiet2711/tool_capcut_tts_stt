import customtkinter as ctk
from tkinter import messagebox, filedialog, colorchooser
import threading
import sys
import os
import json
import uuid
import shutil
import base64
import requests
import time
import pysrt
from mutagen.mp3 import MP3
import asyncio
import edge_tts
from PIL import Image, ImageTk

from capcut_tts_api import CapCutClient, CapCutError
from error_review_dialog import TTSErrorReviewDialog

def generate_edge_tts_sync(text, voice, rate_str, save_path, cancel_check=None):
    async def _amain():
        for attempt in range(6):
            if cancel_check and cancel_check():
                raise Exception("Đã huỷ bởi người dùng.")
            try:
                communicate = edge_tts.Communicate(text, voice, rate=rate_str)
                await communicate.save(save_path)
                return
            except Exception as e:
                if cancel_check and cancel_check():
                    raise Exception("Đã huỷ bởi người dùng.")
                if attempt == 5:
                    raise e
                await asyncio.sleep(3 * (attempt + 1))
    asyncio.run(_amain())

def format_edge_tts_rate(rate_float):
    pct = int((rate_float - 1.0) * 100)
    if pct >= 0:
        return f"+{pct}%"
    return f"{pct}%"

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

def modify_capcut_project(draft_json_path, audio_info_list, sync_mode="Khớp từng câu (Anti-Overlap)", adv_settings=None, fixed_vid_speed=1.0, fixed_aud_speed=1.0):
    """
    audio_info_list: list of dicts like:
    {
        "path": "C:/path/to/file.mp3",
        "start": 1000000, # microseconds (SRT start)
        "end": 2000000, # microseconds (SRT end)
        "duration": 5000000, # microseconds (Audio duration)
        "video_speed": 0.85 # Only used if sync_video is True
    }
    """
    with open(draft_json_path, 'r', encoding='utf-8') as f:
        draft = json.load(f)
        
    materials = draft.get("materials", {})
    if "audios" not in materials:
        materials["audios"] = []
    if "speeds" not in materials:
        materials["speeds"] = []
    if "audio_fades" not in materials:
        materials["audio_fades"] = []
        
    tracks = draft.get("tracks", [])
    
    if sync_mode in ["Đổi tốc độ toàn bộ (Fixed Speed)", "Đổi tốc độ toàn bộ (dùng cấu hình Tab 2)"]:
        for track in tracks:
            for seg in track.get("segments", []):
                if "target_timerange" in seg:
                    seg["target_timerange"]["start"] = int(seg["target_timerange"]["start"] / fixed_vid_speed)
                    seg["target_timerange"]["duration"] = int(seg["target_timerange"]["duration"] / fixed_vid_speed)
                
                if track.get("type") in ["video", "audio"]:
                    speed_id = str(uuid.uuid4()).upper()
                    materials["speeds"].append({
                        "id": speed_id, "type": "speed", "mode": 0, "speed": fixed_vid_speed, "curve_speed": None
                    })
                    if "extra_material_refs" not in seg:
                        seg["extra_material_refs"] = []
                    seg["extra_material_refs"].append(speed_id)
                    seg["speed"] = fixed_vid_speed

    
    text_track = None
    max_segments = 0
    for track in tracks:
        if track.get("type") == "text":
            seg_len = len(track.get("segments", []))
            # Removing strict len check because CapCut might drop a few invalid/overlapping subtitle blocks during import.
            if seg_len > max_segments:
                text_track = track
                max_segments = seg_len
                
    if text_track:
        text_track["segments"].sort(key=lambda s: s.get("target_timerange", {}).get("start", 0))
        for t_seg in text_track["segments"]:
            t_seg["_orig_start"] = t_seg.get("target_timerange", {}).get("start", 0)
        for t_seg in text_track["segments"]:
            t_seg["_orig_start"] = t_seg.get("target_timerange", {}).get("start", 0)

    if sync_mode in ["Khớp từng câu (Anti-Overlap)", "Khớp từng câu (dùng cấu hình Tab 2)"]:
        video_track = None
        video_segment = None
        for track in tracks:
            if track.get("type") == "video":
                if len(track.get("segments", [])) > 0:
                    video_track = track
                    video_segment = track["segments"][0]
                    break
                    
        if not video_track or not video_segment:
            raise Exception("Không tìm thấy Video track hợp lệ trong project. Vui lòng đảm bảo project có 1 video.")
                    
        new_video_segments = []
        current_target_time = 0
        current_source_time = video_segment["source_timerange"]["start"]
        
        import copy
        
        def create_video_chunk(source_start, duration, speed):
            nonlocal current_target_time, current_source_time
            if duration <= 0:
                return None
                
            seg_clone = copy.deepcopy(video_segment)
            seg_clone["id"] = str(uuid.uuid4()).upper()
            
            target_duration = int(duration / speed)
            
            seg_clone["source_timerange"]["start"] = source_start
            seg_clone["source_timerange"]["duration"] = duration
            seg_clone["target_timerange"]["start"] = current_target_time
            seg_clone["target_timerange"]["duration"] = target_duration
            
            speed_id = str(uuid.uuid4()).upper()
            materials["speeds"].append({
                "id": speed_id, "type": "speed", "mode": 0, "speed": speed, "curve_speed": None
            })
            
            seg_clone["extra_material_refs"] = [speed_id]
            seg_clone["speed"] = speed
            
            current_target_time += target_duration
            current_source_time += duration
            return seg_clone

    new_audio_track_id = str(uuid.uuid4()).upper()
    new_audio_track = {
        "attribute": 0, "flag": 0, "id": new_audio_track_id, "is_default_name": True,
        "name": "", "segments": [], "type": "audio"
    }
    
    for i, info in enumerate(audio_info_list):
        srt_start = info["start"]
        srt_end = info["end"]
        video_speed = info.get("video_speed", 1.0)
        audio_path = info["path"].replace("\\", "/")
        audio_duration_micros = info["duration"]
        
        is_dummy = info.get("is_dummy", False)
        
        audio_speed_val = 1.0
        if sync_mode in ["Đổi tốc độ toàn bộ (Fixed Speed)", "Đổi tốc độ toàn bộ (dùng cấu hình Tab 2)"]:
            block_target_start = int(srt_start / fixed_vid_speed)
            audio_speed_val = fixed_aud_speed
            audio_target_duration = int(audio_duration_micros / fixed_aud_speed)
        else:
            block_target_start = srt_start
            audio_target_duration = audio_duration_micros
            
        if sync_mode in ["Khớp từng câu (Anti-Overlap)", "Khớp từng câu (dùng cấu hình Tab 2)"]:
            gap_duration = srt_start - current_source_time
            if gap_duration > 0:
                chunk = create_video_chunk(current_source_time, gap_duration, 1.0)
                if chunk:
                    new_video_segments.append(chunk)
                    
            block_target_start = current_target_time
            block_source_duration = srt_end - srt_start
            
            chunk = create_video_chunk(current_source_time, block_source_duration, video_speed)
            if chunk:
                new_video_segments.append(chunk)
            
        if text_track and sync_mode in ["Khớp từng câu (Anti-Overlap)", "Khớp từng câu (dùng cấu hình Tab 2)"]:
            srt_start = info["start"]
            text_seg = None
            min_diff = float('inf')
            
            for t_seg in text_track["segments"]:
                diff = abs(t_seg.get("_orig_start", 0) - srt_start)
                if diff < min_diff:
                    min_diff = diff
                    text_seg = t_seg
                    
            if min_diff > 100000: # 100ms tolerance for frame snapping
                text_seg = None
                
            if text_seg:
                if text_seg.get("target_timerange") is not None:
                    text_seg["target_timerange"]["start"] = block_target_start
                    text_seg["target_timerange"]["duration"] = audio_duration_micros
                if text_seg.get("source_timerange") is not None:
                    text_seg["source_timerange"]["duration"] = audio_duration_micros
                
        if is_dummy:
            continue
            
        mat_id = str(uuid.uuid4()).upper()
        seg_id = str(uuid.uuid4()).upper()
        speed_id = str(uuid.uuid4()).upper()
        fade_id = str(uuid.uuid4()).upper()
        
        materials["speeds"].append({"id": speed_id, "type": "speed", "mode": 0, "speed": audio_speed_val, "curve_speed": None})
        materials["audio_fades"].append({"fade_in_duration": 0, "fade_out_duration": 0, "fade_type": 0, "id": fade_id, "type": "audio_fade"})
        materials["audios"].append({
            "app_id": 0, "category_id": "", "category_name": "local", "check_flag": 1,
            "duration": audio_duration_micros, "file_Path": audio_path, "id": mat_id,
            "int_id": str(uuid.uuid4().int)[:10], "local_material_id": str(uuid.uuid4()),
            "music_id": mat_id, "name": os.path.basename(audio_path), "path": audio_path,
            "source_platform": 0, "team_id": "", "text_id": "", "tone_category_id": "",
            "tone_category_name": "", "tone_effect_id": "", "tone_effect_name": "",
            "tone_speaker": "", "tone_type": "", "type": "extract_music", "video_id": ""
        })
        
        new_audio_track["segments"].append({
            "id": seg_id, "material_id": mat_id,
            "source_timerange": {"duration": audio_duration_micros, "start": 0},
            "target_timerange": {"duration": audio_target_duration, "start": block_target_start},
            "extra_material_refs": [speed_id, fade_id], "speed": audio_speed_val, "volume": 1.0,
            "is_loop": False, "is_tone_modify": False, "reverse": False,
            "intensifies_audio": False, "cartoon": False, "last_nonzero_volume": 1.0,
            "render_index": 0, "state": 0, "clip": None, "enable_adjust": False,
            "enable_color_curves": True, "enable_color_wheels": True, "enable_lut": False,
            "enable_smart_color_adjust": False, "group_id": "", "hdr_settings": None,
            "is_placeholder": False, "keyframe_refs": [], "template_id": "",
            "template_scene": "default", "track_attribute": 0, "track_render_index": 0,
            "visible": True, "render_timerange": {"duration": 0, "start": 0}
        })
        
    if sync_mode in ["Khớp từng câu (Anti-Overlap)", "Khớp từng câu (dùng cấu hình Tab 2)"]:
        final_video_duration = video_segment["source_timerange"]["start"] + video_segment["source_timerange"]["duration"]
        if current_source_time < final_video_duration:
            chunk = create_video_chunk(current_source_time, final_video_duration - current_source_time, 1.0)
            if chunk:
                new_video_segments.append(chunk)
                
        video_track["segments"] = new_video_segments
        
    if adv_settings:
        vid_vol_mult = 10 ** (adv_settings.get("vid_vol", 0.0) / 20.0)
        aud_vol_mult = 10 ** (adv_settings.get("aud_vol", 0.0) / 20.0)
        
        for track in tracks:
            if track.get("type") == "video":
                for seg in track.get("segments", []):
                    seg["volume"] = vid_vol_mult
                    
        for seg in new_audio_track.get("segments", []):
            seg["volume"] = aud_vol_mult
            
    tracks.append(new_audio_track)
    
    if adv_settings and adv_settings.get("wm_enabled") and adv_settings.get("wm_path"):
        wm_path = adv_settings.get("wm_path")
        if os.path.exists(wm_path):
            wm_x = float(adv_settings.get("wm_x", 0))
            wm_y = float(adv_settings.get("wm_y", 0))
            wm_scale = float(adv_settings.get("wm_scale", 100)) / 100.0
            
            canvas_width = draft.get("canvas_config", {}).get("width", 1920)
            canvas_height = draft.get("canvas_config", {}).get("height", 1080)
            
            trans_x = wm_x / (canvas_width / 2) if canvas_width else 0
            trans_y = - (wm_y / (canvas_height / 2)) if canvas_height else 0
            
            max_dur = 0
            for t in tracks:
                for s in t.get("segments", []):
                    tt = s.get("target_timerange")
                    if tt:
                        end = tt.get("start", 0) + tt.get("duration", 0)
                        if end > max_dur:
                            max_dur = end
                            
            if "videos" not in materials:
                materials["videos"] = []
                
            wm_mat_id = str(uuid.uuid4()).upper()
            materials["videos"].append({
                "id": wm_mat_id, "type": "photo", "path": wm_path.replace("\\", "/"),
                "duration": 600000000, "material_name": os.path.basename(wm_path),
                "width": canvas_width, "height": canvas_height, "crop_scale": 1.0,
                "category_name": "local", "category_id": ""
            })
            
            wm_track_id = str(uuid.uuid4()).upper()
            wm_seg_id = str(uuid.uuid4()).upper()
            
            wm_track = {
                "attribute": 0, "flag": 0, "id": wm_track_id, "is_default_name": True,
                "name": "", "segments": [{
                    "id": wm_seg_id, "material_id": wm_mat_id,
                    "source_timerange": {"duration": max_dur, "start": 0},
                    "target_timerange": {"duration": max_dur, "start": 0},
                    "extra_material_refs": [], "speed": 1.0, "volume": 1.0,
                    "is_loop": False, "is_tone_modify": False, "reverse": False,
                    "intensifies_audio": False, "cartoon": False, "last_nonzero_volume": 1.0,
                    "render_index": 10000, "state": 0, "clip": {
                        "scale": {"x": wm_scale, "y": wm_scale},
                        "transform": {"x": trans_x, "y": trans_y},
                        "rotation": 0.0,
                        "flip": {"vertical": False, "horizontal": False},
                        "alpha": 1.0
                    },
                    "enable_adjust": False, "enable_color_curves": True, "enable_color_wheels": True,
                    "enable_lut": False, "enable_smart_color_adjust": False, "group_id": "",
                    "hdr_settings": None, "is_placeholder": False, "keyframe_refs": [],
                    "template_id": "", "template_scene": "default", "track_attribute": 0,
                    "track_render_index": 0, "visible": True, "render_timerange": {"duration": 0, "start": 0}
                }], "type": "video"
            }
            tracks.append(wm_track)
    
    backup_path = draft_json_path + f".bak_{int(time.time())}"
    shutil.copy2(draft_json_path, backup_path)
    with open(draft_json_path, 'w', encoding='utf-8') as f:
        json.dump(draft, f, ensure_ascii=False)

def split_capcut_project(draft_dir, chunk_duration_min=10):
    draft_content_path = os.path.join(draft_dir, "draft_content.json")
    draft_meta_path = os.path.join(draft_dir, "draft_meta_info.json")
    
    if not os.path.exists(draft_content_path) or not os.path.exists(draft_meta_path):
        raise Exception("Thư mục không chứa project CapCut hợp lệ (thiếu draft_content.json hoặc draft_meta_info.json).")
        
    with open(draft_content_path, 'r', encoding='utf-8') as f:
        draft = json.load(f)
        
    with open(draft_meta_path, 'r', encoding='utf-8') as f:
        meta = json.load(f)
        
    chunk_duration_micros = int(float(chunk_duration_min) * 60 * 1000000)
    
    total_duration_video = 0
    total_duration_any = 0
    for track in draft.get("tracks", []):
        is_video = track.get("type") == "video"
        segments = track.get("segments")
        if not segments:
            continue
        for seg in segments:
            tt = seg.get("target_timerange")
            if not tt:
                continue
            end_time = tt.get("start", 0) + tt.get("duration", 0)
            if end_time > total_duration_any:
                total_duration_any = end_time
            if is_video and end_time > total_duration_video:
                total_duration_video = end_time
                
    total_duration = total_duration_video if total_duration_video > 0 else total_duration_any
                
    if total_duration == 0:
        raise Exception("Project rỗng, không có thời lượng.")
        
    num_parts = (total_duration + chunk_duration_micros - 1) // chunk_duration_micros
    if num_parts <= 1:
        raise Exception(f"Thời lượng dự án ({total_duration / 1000000:.1f}s) nhỏ hơn hoặc bằng 1 phần ({chunk_duration_min} phút). Không cần chia nhỏ.")
    
    base_dir = os.path.dirname(draft_dir)
    draft_name_orig = meta.get("draft_name", "Project")
    
    parts_created = []
    import copy
    
    for i in range(num_parts):
        part_name = f"{draft_name_orig}_Part{i+1}"
        part_dir = os.path.join(base_dir, part_name)
        
        if os.path.exists(part_dir):
            shutil.rmtree(part_dir)
        os.makedirs(part_dir)
        
        for item in os.listdir(draft_dir):
            s = os.path.join(draft_dir, item)
            d = os.path.join(part_dir, item)
            if os.path.isdir(s):
                shutil.copytree(s, d)
            elif item not in ["draft_content.json", "draft_meta_info.json"] and not item.startswith("draft_content.json.bak"):
                shutil.copy2(s, d)
                
        new_meta = copy.deepcopy(meta)
        new_draft_id = str(uuid.uuid4()).upper()
        new_meta["draft_id"] = new_draft_id
        new_meta["draft_name"] = part_name
        new_meta["draft_fold_path"] = part_dir.replace("\\", "/")
        new_meta["draft_root_path"] = part_dir.replace("\\", "/")
        
        with open(os.path.join(part_dir, "draft_meta_info.json"), 'w', encoding='utf-8') as f:
            json.dump(new_meta, f, ensure_ascii=False)
            
        new_draft = copy.deepcopy(draft)
        chunk_start = i * chunk_duration_micros
        chunk_end = (i + 1) * chunk_duration_micros
        
        for track in new_draft.get("tracks", []):
            new_segments = []
            for seg in track.get("segments", []):
                target_timerange = seg.get("target_timerange") or {}
                seg_start = target_timerange.get("start", 0)
                seg_duration = target_timerange.get("duration", 0)
                seg_end = seg_start + seg_duration
                
                # Check overlap with [chunk_start, chunk_end)
                if seg_end > chunk_start and seg_start < chunk_end:
                    import copy
                    new_seg = copy.deepcopy(seg)
                    
                    trim_start = max(0, chunk_start - seg_start)
                    trim_end = max(0, seg_end - chunk_end)
                    
                    new_target_start = max(0, seg_start - chunk_start)
                    new_target_duration = seg_duration - trim_start - trim_end
                    
                    if "target_timerange" not in new_seg or not new_seg["target_timerange"]:
                        new_seg["target_timerange"] = {}
                    new_seg["target_timerange"]["start"] = new_target_start
                    new_seg["target_timerange"]["duration"] = new_target_duration
                    
                    if "source_timerange" in new_seg and new_seg["source_timerange"]:
                        speed = 1.0
                        if "speed" in new_seg:
                            try:
                                speed = float(new_seg["speed"])
                            except:
                                pass
                        elif "extra_material_refs" in new_seg:
                            for ref in new_seg["extra_material_refs"]:
                                for speed_mat in new_draft.get("materials", {}).get("speeds", []):
                                    if speed_mat.get("id") == ref:
                                        try:
                                            speed = float(speed_mat.get("speed", 1.0))
                                        except:
                                            pass
                                        break
                                        
                        source_start = new_seg["source_timerange"].get("start", 0)
                        trim_start_source = int(trim_start * speed)
                        new_seg["source_timerange"]["start"] = source_start + trim_start_source
                        new_seg["source_timerange"]["duration"] = int(new_target_duration * speed)
                        
                    new_segments.append(new_seg)
            track["segments"] = new_segments
            
        with open(os.path.join(part_dir, "draft_content.json"), 'w', encoding='utf-8') as f:
            json.dump(new_draft, f, ensure_ascii=False)
            
        parts_created.append(part_name)
        
    return parts_created

class CapCutTTSApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("CapCut TTS API - Desktop App")
        self.geometry("750x650")
        self.client = CapCutClient()
        self.voices = []
        self.is_cancelled = False

        # -- Layout --
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # 1. Top Frame (Voice and Rate settings)
        self.frame_top = ctk.CTkFrame(self)
        self.frame_top.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="ew")
        
        self.label_voice = ctk.CTkLabel(self.frame_top, text="Chọn Giọng Đọc:", font=ctk.CTkFont(weight="bold"))
        self.label_voice.grid(row=0, column=0, padx=10, pady=10, sticky="w")
        
        self.combo_voice = ctk.CTkComboBox(self.frame_top, values=["Đang tải..."], width=200)
        self.combo_voice.grid(row=0, column=1, padx=5, pady=10, sticky="ew")
        
        self.label_rate = ctk.CTkLabel(self.frame_top, text="Tốc độ:", font=ctk.CTkFont(weight="bold"))
        self.label_rate.grid(row=0, column=2, padx=5, pady=10, sticky="w")
        
        self.slider_rate = ctk.CTkSlider(self.frame_top, from_=0.5, to=2.0, number_of_steps=15, command=self.update_rate_label, width=100)
        self.slider_rate.set(1.0)
        self.slider_rate.grid(row=0, column=3, padx=5, pady=10, sticky="ew")

        self.label_rate_val = ctk.CTkLabel(self.frame_top, text="1.0")
        self.label_rate_val.grid(row=0, column=4, padx=5, pady=10, sticky="w")
        
        self.label_threads = ctk.CTkLabel(self.frame_top, text="Luồng (Threads):", font=ctk.CTkFont(weight="bold"))
        self.label_threads.grid(row=0, column=5, padx=5, pady=10, sticky="w")
        
        self.slider_threads = ctk.CTkSlider(self.frame_top, from_=1, to=200, number_of_steps=199, command=self.update_threads_label, width=100)
        self.slider_threads.set(50)
        self.slider_threads.grid(row=0, column=6, padx=5, pady=10, sticky="ew")

        self.label_threads_val = ctk.CTkLabel(self.frame_top, text="50")
        self.label_threads_val.grid(row=0, column=7, padx=5, pady=10, sticky="w")

        # 2. Tabs for Modes
        self.tabview = ctk.CTkTabview(self)
        self.tabview.grid(row=1, column=0, padx=20, pady=10, sticky="nsew")
        self.tab_basic = self.tabview.add("Tạo TTS Cơ Bản")
        self.tab_srt = self.tabview.add("Chèn SRT vào CapCut")
        self.tab_merge = self.tabview.add("Ghép Audio Có Sẵn")
        self.tab_split = self.tabview.add("Chia Nhỏ Project")
        self.tab_stt = self.tabview.add("Nhận diện (STT)")
        
        # -- Tab 1: Basic TTS --
        self.tab_basic.grid_columnconfigure(0, weight=1)
        self.tab_basic.grid_rowconfigure(0, weight=1)
        self.text_input = ctk.CTkTextbox(self.tab_basic, font=ctk.CTkFont(size=14))
        self.text_input.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        self.text_input.insert("1.0", "Xin chào! Bạn có thể nhập nội dung văn bản vào đây để tôi đọc cho bạn nghe nhé.")
        
        self.btn_generate_basic = ctk.CTkButton(self.tab_basic, text="Tạo Giọng Nói (TTS) và Lưu...", font=ctk.CTkFont(size=16, weight="bold"), height=45, command=self.on_generate_basic)
        self.btn_generate_basic.grid(row=1, column=0, padx=10, pady=10, sticky="ew")
        
        # -- Tab 2: SRT to CapCut --
        self.tab_srt.grid_columnconfigure(1, weight=1)
        
        # SRT File Selector
        self.srt_path = ctk.StringVar()
        ctk.CTkLabel(self.tab_srt, text="File SRT:").grid(row=0, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self.tab_srt, textvariable=self.srt_path, state="disabled").grid(row=0, column=1, padx=10, pady=10, sticky="ew")
        ctk.CTkButton(self.tab_srt, text="Chọn", width=60, command=self.select_srt).grid(row=0, column=2, padx=10, pady=10)
        
        # JSON File Selector
        self.json_path = ctk.StringVar()
        ctk.CTkLabel(self.tab_srt, text="Dự án CapCut (JSON):").grid(row=1, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self.tab_srt, textvariable=self.json_path, state="disabled").grid(row=1, column=1, padx=10, pady=10, sticky="ew")
        ctk.CTkButton(self.tab_srt, text="Chọn", width=60, command=self.select_json).grid(row=1, column=2, padx=10, pady=10)
        
        # Progress Bar
        self.progressbar = ctk.CTkProgressBar(self.tab_srt)
        self.progressbar.grid(row=2, column=0, columnspan=3, padx=10, pady=(20, 5), sticky="ew")
        self.progressbar.set(0)
        
        self.label_progress = ctk.CTkLabel(self.tab_srt, text="Tiến độ: 0 / 0 câu")
        self.label_progress.grid(row=3, column=0, columnspan=3, padx=10, pady=5)
        
        # Sync Video Options
        ctk.CTkLabel(self.tab_srt, text="Chế độ đồng bộ:").grid(row=4, column=0, padx=10, pady=5, sticky="w")
        self.combo_sync_mode_var = ctk.StringVar(value="Khớp từng câu (Anti-Overlap)")
        self.combo_sync_mode_var.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        self.combo_sync_mode = ctk.CTkComboBox(self.tab_srt, values=["Không đồng bộ", "Khớp từng câu (Anti-Overlap)", "Đổi tốc độ toàn bộ (Fixed Speed)"], variable=self.combo_sync_mode_var, command=self.toggle_sync_opts, width=280)
        self.combo_sync_mode.grid(row=4, column=1, columnspan=2, padx=10, pady=5, sticky="w")
        
        self.frame_sync_opts = ctk.CTkFrame(self.tab_srt)
        self.frame_sync_opts.grid(row=5, column=0, columnspan=3, padx=10, pady=5, sticky="ew")
        
        self.frame_fixed_speed_opts = ctk.CTkFrame(self.tab_srt)
        # Not gridded by default
        
        ctk.CTkLabel(self.frame_fixed_speed_opts, text="Tốc độ Video (x):").grid(row=0, column=0, padx=5, pady=5)
        self.val_fixed_vid_speed = ctk.StringVar(value="1.0")
        self.val_fixed_vid_speed.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_fixed_speed_opts, textvariable=self.val_fixed_vid_speed, width=50).grid(row=0, column=1, padx=5, pady=5)
        
        ctk.CTkLabel(self.frame_fixed_speed_opts, text="Tốc độ Audio (x):").grid(row=0, column=2, padx=5, pady=5)
        self.val_fixed_aud_speed = ctk.StringVar(value="1.0")
        self.val_fixed_aud_speed.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_fixed_speed_opts, textvariable=self.val_fixed_aud_speed, width=50).grid(row=0, column=3, padx=5, pady=5)
        
        ctk.CTkLabel(self.frame_sync_opts, text="Video chậm tối đa:").grid(row=0, column=0, padx=5, pady=5)
        self.val_min_video = ctk.StringVar(value="0.85")
        self.val_min_video.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_sync_opts, textvariable=self.val_min_video, width=50).grid(row=0, column=1, padx=5, pady=5)
        
        ctk.CTkLabel(self.frame_sync_opts, text="Video nhanh tối đa:").grid(row=0, column=2, padx=5, pady=5)
        self.val_max_video = ctk.StringVar(value="1.15")
        self.val_max_video.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_sync_opts, textvariable=self.val_max_video, width=50).grid(row=0, column=3, padx=5, pady=5)
        
        ctk.CTkLabel(self.frame_sync_opts, text="Audio nhanh tối đa:").grid(row=0, column=4, padx=5, pady=5)
        self.val_max_audio = ctk.StringVar(value="1.15")
        self.val_max_audio.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_sync_opts, textvariable=self.val_max_audio, width=50).grid(row=0, column=5, padx=5, pady=5)
        
        ctk.CTkButton(self.frame_sync_opts, text="Reset về mặc định", width=80, fg_color="#b23b3b", hover_color="#8f2b2b", command=self.reset_sync_config).grid(row=0, column=6, padx=5, pady=5, sticky="ns")
        
        self.btn_generate_srt = ctk.CTkButton(self.tab_srt, text="Bắt đầu xử lý SRT", font=ctk.CTkFont(size=16, weight="bold"), height=45, command=self.on_generate_srt)
        self.btn_generate_srt.grid(row=6, column=0, columnspan=3, padx=10, pady=20, sticky="ew")

        # -- Tab 3: Merge Existing Audio --
        self.tab_merge.grid_columnconfigure(1, weight=1)
        
        self.merge_srt_path = ctk.StringVar()
        ctk.CTkLabel(self.tab_merge, text="File SRT:").grid(row=0, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self.tab_merge, textvariable=self.merge_srt_path, state="disabled").grid(row=0, column=1, padx=10, pady=10, sticky="ew")
        ctk.CTkButton(self.tab_merge, text="Chọn", width=60, command=self.select_merge_srt).grid(row=0, column=2, padx=10, pady=10)
        
        self.merge_json_path = ctk.StringVar()
        ctk.CTkLabel(self.tab_merge, text="Dự án CapCut (JSON):").grid(row=1, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self.tab_merge, textvariable=self.merge_json_path, state="disabled").grid(row=1, column=1, padx=10, pady=10, sticky="ew")
        ctk.CTkButton(self.tab_merge, text="Chọn", width=60, command=self.select_merge_json).grid(row=1, column=2, padx=10, pady=10)

        self.merge_audio_path = ctk.StringVar()
        ctk.CTkLabel(self.tab_merge, text="Thư mục Audio (mp3):").grid(row=2, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self.tab_merge, textvariable=self.merge_audio_path, state="disabled").grid(row=2, column=1, padx=10, pady=10, sticky="ew")
        ctk.CTkButton(self.tab_merge, text="Chọn", width=60, command=self.select_merge_audio).grid(row=2, column=2, padx=10, pady=10)
        
        self.merge_progressbar = ctk.CTkProgressBar(self.tab_merge)
        self.merge_progressbar.grid(row=3, column=0, columnspan=3, padx=10, pady=(20, 5), sticky="ew")
        self.merge_progressbar.set(0)
        
        self.merge_label_progress = ctk.CTkLabel(self.tab_merge, text="Tiến độ: 0 / 0 câu")
        self.merge_label_progress.grid(row=4, column=0, columnspan=3, padx=10, pady=5)
        
        ctk.CTkLabel(self.tab_merge, text="Chế độ đồng bộ:").grid(row=5, column=0, padx=10, pady=5, sticky="w")
        self.combo_merge_sync_mode_var = ctk.StringVar(value="Khớp từng câu (dùng cấu hình Tab 2)")
        self.combo_merge_sync_mode_var.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        self.combo_merge_sync_mode = ctk.CTkComboBox(self.tab_merge, values=["Không đồng bộ", "Khớp từng câu (dùng cấu hình Tab 2)", "Đổi tốc độ toàn bộ (dùng cấu hình Tab 2)"], variable=self.combo_merge_sync_mode_var, width=350)
        self.combo_merge_sync_mode.grid(row=5, column=1, columnspan=2, padx=10, pady=5, sticky="w")
        
        self.btn_generate_merge = ctk.CTkButton(self.tab_merge, text="Bắt đầu ghép vào CapCut", font=ctk.CTkFont(size=16, weight="bold"), height=45, command=self.on_generate_merge)
        self.btn_generate_merge.grid(row=6, column=0, columnspan=3, padx=10, pady=20, sticky="ew")

        # -- Tab 4: Split Project --
        self.tab_split.grid_columnconfigure(1, weight=1)
        
        self.split_project_path = ctk.StringVar()
        ctk.CTkLabel(self.tab_split, text="Thư mục Project CapCut:").grid(row=0, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self.tab_split, textvariable=self.split_project_path, state="disabled").grid(row=0, column=1, padx=10, pady=10, sticky="ew")
        ctk.CTkButton(self.tab_split, text="Chọn", width=60, command=self.select_split_project).grid(row=0, column=2, padx=10, pady=10)
        
        ctk.CTkLabel(self.tab_split, text="Độ dài mỗi phần (phút):").grid(row=1, column=0, padx=10, pady=10, sticky="w")
        self.split_duration_val = ctk.StringVar(value="10")
        ctk.CTkEntry(self.tab_split, textvariable=self.split_duration_val, width=100).grid(row=1, column=1, padx=10, pady=10, sticky="w")
        
        self.btn_split_project = ctk.CTkButton(self.tab_split, text="Bắt đầu chia nhỏ", font=ctk.CTkFont(size=16, weight="bold"), height=45, command=self.on_split_project)
        self.btn_split_project.grid(row=2, column=0, columnspan=3, padx=10, pady=20, sticky="ew")

        # -- Tab 5: STT --
        self.tab_stt.grid_columnconfigure(1, weight=1)
        
        self.stt_media_path = ctk.StringVar()
        ctk.CTkLabel(self.tab_stt, text="File Media (Video/Audio):").grid(row=0, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self.tab_stt, textvariable=self.stt_media_path, state="disabled").grid(row=0, column=1, padx=10, pady=10, sticky="ew")
        ctk.CTkButton(self.tab_stt, text="Chọn", width=60, command=self.select_stt_media).grid(row=0, column=2, padx=10, pady=10)
        
        ctk.CTkLabel(self.tab_stt, text="Ngôn ngữ gốc:").grid(row=1, column=0, padx=10, pady=10, sticky="w")
        self.stt_lang_combo = ctk.CTkComboBox(self.tab_stt, values=["vi-VN", "zh-CN", "en-US", "ja-JP", "ko-KR", "th-TH", "id-ID", "ms-MY"])
        self.stt_lang_combo.grid(row=1, column=1, padx=10, pady=10, sticky="ew")
        self.stt_lang_combo.set("zh-CN")
        
        self.chk_stt_translate_var = ctk.BooleanVar(value=False)
        self.chk_stt_translate = ctk.CTkCheckBox(self.tab_stt, text="Bật dịch thuật sang:", variable=self.chk_stt_translate_var, command=self.toggle_stt_translate)
        self.chk_stt_translate.grid(row=2, column=0, padx=10, pady=10, sticky="w")
        
        self.stt_target_lang_combo = ctk.CTkComboBox(self.tab_stt, values=["vi-VN", "zh-CN", "en-US", "ja-JP", "ko-KR", "th-TH", "id-ID", "ms-MY"])
        self.stt_target_lang_combo.grid(row=2, column=1, padx=10, pady=10, sticky="ew")
        self.stt_target_lang_combo.set("vi-VN")
        self.stt_target_lang_combo.configure(state="disabled")
        
        self.stt_progressbar = ctk.CTkProgressBar(self.tab_stt)
        self.stt_progressbar.grid(row=3, column=0, columnspan=3, padx=10, pady=(20, 5), sticky="ew")
        self.stt_progressbar.set(0)
        
        self.btn_generate_stt = ctk.CTkButton(self.tab_stt, text="Bắt đầu trích xuất SRT", font=ctk.CTkFont(size=16, weight="bold"), height=45, command=self.on_generate_stt)
        self.btn_generate_stt.grid(row=4, column=0, columnspan=3, padx=10, pady=20, sticky="ew")

        # -- 3. Advanced Settings (Collapsible) --
        self.btn_toggle_adv = ctk.CTkButton(self, text="[+] Hiển thị tuỳ chỉnh nâng cao", fg_color="transparent", text_color="gray", hover_color="#2a2d2e", command=self.toggle_adv_settings)
        self.btn_toggle_adv.grid(row=2, column=0, padx=20, pady=(0, 5), sticky="w")
        
        self.frame_adv = ctk.CTkFrame(self)
        # Not gridded by default
        
        self.frame_adv.grid_columnconfigure(1, weight=1)
        self.frame_adv.grid_columnconfigure(3, weight=1)
        
        # Volume group
        ctk.CTkLabel(self.frame_adv, text="Âm lượng Video (dB):").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        self.val_vid_vol = ctk.StringVar(value="0")
        self.val_vid_vol.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_adv, textvariable=self.val_vid_vol, width=60).grid(row=0, column=1, padx=5, pady=5, sticky="w")
        
        ctk.CTkLabel(self.frame_adv, text="Âm lượng Audio (dB):").grid(row=0, column=2, padx=5, pady=5, sticky="e")
        self.val_aud_vol = ctk.StringVar(value="0")
        self.val_aud_vol.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_adv, textvariable=self.val_aud_vol, width=60).grid(row=0, column=3, padx=5, pady=5, sticky="w")
        
        # Anti-ban group
        self.frame_adv.grid_columnconfigure(5, weight=1)
        ctk.CTkLabel(self.frame_adv, text="Nghỉ (s):").grid(row=1, column=0, padx=5, pady=5, sticky="e")
        self.val_rest_time = ctk.StringVar(value="10")
        self.val_rest_time.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_adv, textvariable=self.val_rest_time, width=60).grid(row=1, column=1, padx=5, pady=5, sticky="w")
        
        ctk.CTkLabel(self.frame_adv, text="sau (block):").grid(row=1, column=2, padx=5, pady=5, sticky="e")
        self.val_rest_blocks = ctk.StringVar(value="300")
        self.val_rest_blocks.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_adv, textvariable=self.val_rest_blocks, width=60).grid(row=1, column=3, padx=5, pady=5, sticky="w")
        
        ctk.CTkLabel(self.frame_adv, text="Đổi ID sau (block):").grid(row=1, column=4, padx=5, pady=5, sticky="e")
        self.val_id_blocks = ctk.StringVar(value="300")
        self.val_id_blocks.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_adv, textvariable=self.val_id_blocks, width=60).grid(row=1, column=5, padx=5, pady=5, sticky="w")
        
        ctk.CTkButton(self.frame_adv, text="Reset", width=60, fg_color="#b23b3b", hover_color="#8f2b2b", command=self.reset_adv_config).grid(row=1, column=6, padx=5, pady=5, sticky="w")
        
        # Watermark settings
        ctk.CTkFrame(self.frame_adv, height=2, fg_color="gray").grid(row=2, column=0, columnspan=7, sticky="ew", pady=10)
        
        self.val_wm_enabled = ctk.BooleanVar(value=False)
        self.val_wm_enabled.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkCheckBox(self.frame_adv, text="Chèn Watermark", variable=self.val_wm_enabled).grid(row=3, column=0, padx=5, pady=5, sticky="w")
        
        self.val_wm_path = ctk.StringVar(value="")
        self.val_wm_path.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_adv, textvariable=self.val_wm_path, placeholder_text="Đường dẫn ảnh...").grid(row=3, column=1, columnspan=2, padx=5, pady=5, sticky="ew")
        ctk.CTkButton(self.frame_adv, text="Chọn", width=50, command=self.select_watermark).grid(row=3, column=3, padx=5, pady=5, sticky="w")
        
        ctk.CTkLabel(self.frame_adv, text="X:").grid(row=3, column=4, padx=2, pady=5, sticky="e")
        self.val_wm_x = ctk.StringVar(value="0")
        self.val_wm_x.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_adv, textvariable=self.val_wm_x, width=50).grid(row=3, column=5, padx=2, pady=5, sticky="w")
        
        ctk.CTkLabel(self.frame_adv, text="Y:").grid(row=4, column=4, padx=2, pady=5, sticky="e")
        self.val_wm_y = ctk.StringVar(value="0")
        self.val_wm_y.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_adv, textvariable=self.val_wm_y, width=50).grid(row=4, column=5, padx=2, pady=5, sticky="w")
        
        ctk.CTkLabel(self.frame_adv, text="Tỷ lệ (%):").grid(row=4, column=0, padx=5, pady=5, sticky="e")
        self.val_wm_scale = ctk.StringVar(value="100")
        self.val_wm_scale.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_adv, textvariable=self.val_wm_scale, width=60).grid(row=4, column=1, padx=5, pady=5, sticky="w")
        
        ctk.CTkButton(self.frame_adv, text="Preview & Chỉnh", fg_color="#2b8f5a", hover_color="#1d663f", command=self.open_watermark_preview).grid(row=4, column=6, padx=5, pady=5, sticky="w")

        # 4. Bottom Status
        self.frame_bottom = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_bottom.grid(row=3, column=0, pady=(0, 10), sticky="ew")
        self.frame_bottom.grid_columnconfigure(0, weight=1)
        self.frame_bottom.grid_columnconfigure(1, weight=0)
        self.frame_bottom.grid_columnconfigure(2, weight=0)

        self.label_status = ctk.CTkLabel(self.frame_bottom, text="Sẵn sàng.", text_color="gray")
        self.label_status.grid(row=0, column=0, padx=20, sticky="w")
        
        self.btn_stop = ctk.CTkButton(self.frame_bottom, text="Dừng (Cancel)", width=120, fg_color="gray", hover_color="#555555", command=self.cancel_generation)
        self.btn_stop.grid(row=0, column=1, padx=(0, 10), sticky="e")
        self.btn_stop.configure(state="disabled")
        
        self.btn_reset_id = ctk.CTkButton(self.frame_bottom, text="Đổi Device ID (Gỡ Ban)", width=150, fg_color="#b23b3b", hover_color="#8f2b2b", command=self.reset_device_id)
        self.btn_reset_id.grid(row=0, column=2, padx=20, sticky="e")

        # Load config
        self.load_sync_config()

        # Load voices
        threading.Thread(target=self.load_voices, daemon=True).start()

    def cancel_generation(self):
        self.is_cancelled = True
        self.label_status.configure(text="Đang dừng quá trình, vui lòng đợi...", text_color="orange")

    def toggle_sync_opts(self, *args):
        mode = self.combo_sync_mode_var.get()
        if mode == "Khớp từng câu (Anti-Overlap)":
            self.frame_sync_opts.grid(row=5, column=0, columnspan=3, padx=10, pady=5, sticky="ew")
            self.frame_fixed_speed_opts.grid_remove()
        elif mode == "Đổi tốc độ toàn bộ (Fixed Speed)":
            self.frame_fixed_speed_opts.grid(row=5, column=0, columnspan=3, padx=10, pady=5, sticky="ew")
            self.frame_sync_opts.grid_remove()
        else:
            self.frame_sync_opts.grid_remove()
            self.frame_fixed_speed_opts.grid_remove()

    def toggle_adv_settings(self):
        if self.frame_adv.winfo_ismapped():
            self.frame_adv.grid_remove()
            self.btn_toggle_adv.configure(text="[+] Hiển thị tuỳ chỉnh nâng cao")
        else:
            self.frame_adv.grid(row=3, column=0, pady=(0, 10), sticky="ew")
            self.frame_bottom.grid(row=4, column=0, pady=(0, 10), sticky="ew")
            self.btn_toggle_adv.configure(text="[-] Ẩn tuỳ chỉnh nâng cao")
            
    def select_watermark(self):
        file_path = filedialog.askopenfilename(filetypes=[("Image files", "*.png *.jpg *.jpeg")])
        if file_path:
            self.val_wm_path.set(file_path)

    def open_watermark_preview(self):
        wm_path = self.val_wm_path.get()
        if not wm_path or not os.path.exists(wm_path):
            messagebox.showerror("Lỗi", "Vui lòng chọn ảnh Watermark hợp lệ trước khi xem trước!")
            return
            
        preview_scale = 0.4
        canvas_w = int(1920 * preview_scale)
        canvas_h = int(1080 * preview_scale)
        
        try:
            self.orig_pil_img = Image.open(wm_path).convert("RGBA")
        except Exception as e:
            messagebox.showerror("Lỗi", f"Không thể tải ảnh Watermark: {e}")
            return

        preview_win = ctk.CTkToplevel(self)
        preview_win.title("Kéo thả & Chỉnh cỡ Watermark")
        preview_win.geometry(f"{canvas_w + 40}x{canvas_h + 150}")
        preview_win.grab_set() 
        
        lbl_info = ctk.CTkLabel(preview_win, text="Kéo thả ảnh logo để chỉnh vị trí. Kéo thanh trượt để chỉnh kích cỡ.\n(Chọn dự án CapCut ở tab 'Chèn SRT...' để thấy ảnh nền video thật)")
        lbl_info.pack(pady=2)
        
        frame_scale = ctk.CTkFrame(preview_win, fg_color="transparent")
        frame_scale.pack(pady=2)
        ctk.CTkLabel(frame_scale, text="Tỷ lệ:").pack(side="left", padx=5)
        
        try: init_scale = float(self.val_wm_scale.get())
        except: init_scale = 100.0
        lbl_scale_val = ctk.CTkLabel(frame_scale, text=f"{int(init_scale)}%")
        
        def update_wm_image(scale_pct):
            scale_val = float(scale_pct) / 100.0
            orig_w, orig_h = self.orig_pil_img.size
            new_w = int(max(1, orig_w * scale_val * preview_scale))
            new_h = int(max(1, orig_h * scale_val * preview_scale))
            resized = self.orig_pil_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            self.preview_wm_image = ImageTk.PhotoImage(resized)
            try:
                canvas.itemconfig(img_id, image=self.preview_wm_image)
            except: pass
            lbl_scale_val.configure(text=f"{int(scale_pct)}%")
            self.val_wm_scale.set(str(int(scale_pct)))
            
        slider_scale = ctk.CTkSlider(frame_scale, from_=10, to=500, command=update_wm_image)
        slider_scale.set(init_scale)
        slider_scale.pack(side="left", padx=10)
        lbl_scale_val.pack(side="left", padx=5)
        
        import tkinter as tk
        import subprocess
        
        bg_image = None
        draft_path = self.json_path.get()
        if draft_path and os.path.exists(draft_path):
            try:
                with open(draft_path, "r", encoding="utf-8") as f:
                    draft = json.load(f)
                videos = draft.get("materials", {}).get("videos", [])
                video_path = None
                for v in videos:
                    p = v.get("path", "")
                    if p and os.path.exists(p) and p.lower().endswith((".mp4", ".mov", ".avi", ".mkv")):
                        video_path = p
                        break
                
                if video_path:
                    temp_bg = "temp_preview_bg.jpg"
                    subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vframes", "1", "-q:v", "2", temp_bg], 
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if os.path.exists(temp_bg):
                        pil_bg = Image.open(temp_bg).convert("RGB")
                        pil_bg = pil_bg.resize((canvas_w, canvas_h), Image.Resampling.LANCZOS)
                        self.preview_bg_image = ImageTk.PhotoImage(pil_bg)
                        bg_image = self.preview_bg_image
            except Exception as e:
                print("Lỗi trích xuất ảnh nền:", e)

        canvas = tk.Canvas(preview_win, width=canvas_w, height=canvas_h, bg="black", highlightthickness=1, highlightbackground="gray")
        canvas.pack(pady=5)
        
        if bg_image:
            canvas.create_image(canvas_w/2, canvas_h/2, image=bg_image, anchor=tk.CENTER)
            
        try: current_wm_x = float(self.val_wm_x.get())
        except: current_wm_x = 0.0
        try: current_wm_y = float(self.val_wm_y.get())
        except: current_wm_y = 0.0
        
        init_x = (canvas_w / 2) + (current_wm_x * preview_scale)
        init_y = (canvas_h / 2) + (current_wm_y * preview_scale)
        
        # Init watermark image
        update_wm_image(init_scale)
        img_id = canvas.create_image(init_x, init_y, image=self.preview_wm_image, anchor=tk.CENTER)
        
        # Bind mouse wheel for zooming
        def on_mouse_wheel(event):
            # event.delta is typically 120 or -120 on Windows
            current = slider_scale.get()
            if event.delta > 0: current = min(500, current + 5)
            elif event.delta < 0: current = max(10, current - 5)
            slider_scale.set(current)
            update_wm_image(current)
            
        preview_win.bind("<MouseWheel>", on_mouse_wheel)
        
        def on_press(event):
            canvas.start_x = event.x
            canvas.start_y = event.y

        def on_drag(event):
            dx = event.x - canvas.start_x
            dy = event.y - canvas.start_y
            canvas.move(img_id, dx, dy)
            canvas.start_x = event.x
            canvas.start_y = event.y
            
        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        
        def save_pos():
            coords = canvas.coords(img_id)
            if coords:
                cx, cy = coords[0], coords[1]
                final_wm_x = (cx - canvas_w / 2) / preview_scale
                final_wm_y = (cy - canvas_h / 2) / preview_scale
                self.val_wm_x.set(str(int(final_wm_x)))
                self.val_wm_y.set(str(int(final_wm_y)))
                self.save_sync_config(silent=True)
                preview_win.destroy()
                messagebox.showinfo("Thành công", f"Đã lưu tọa độ Watermark: X={int(final_wm_x)}, Y={int(final_wm_y)}")
                
        ctk.CTkButton(preview_win, text="Lưu vị trí & Tỷ lệ", command=save_pos).pack(pady=5)

    def get_adv_settings(self):
        try: vid_vol = float(self.val_vid_vol.get()) if self.val_vid_vol.get() else 0.0
        except: vid_vol = 0.0
        try: aud_vol = float(self.val_aud_vol.get()) if self.val_aud_vol.get() else 0.0
        except: aud_vol = 0.0
        
        try: wm_x = float(self.val_wm_x.get()) if self.val_wm_x.get() else 0.0
        except: wm_x = 0.0
        try: wm_y = float(self.val_wm_y.get()) if self.val_wm_y.get() else 0.0
        except: wm_y = 0.0
        try: wm_scale = float(self.val_wm_scale.get()) if self.val_wm_scale.get() else 100.0
        except: wm_scale = 100.0

        return {
            "vid_vol": vid_vol,
            "aud_vol": aud_vol,
            "wm_enabled": self.val_wm_enabled.get(),
            "wm_path": self.val_wm_path.get(),
            "wm_x": wm_x,
            "wm_y": wm_y,
            "wm_scale": wm_scale
        }

    def load_sync_config(self):
        try:
            if os.path.exists("app_config.json"):
                with open("app_config.json", "r", encoding="utf-8") as f:
                    config = json.load(f)
                    if "val_min_video" in config: self.val_min_video.set(config["val_min_video"])
                    if "val_max_video" in config: self.val_max_video.set(config["val_max_video"])
                    if "val_max_audio" in config: self.val_max_audio.set(config["val_max_audio"])
                    if "val_rest_time" in config: self.val_rest_time.set(config["val_rest_time"])
                    if "val_rest_blocks" in config: self.val_rest_blocks.set(config["val_rest_blocks"])
                    if "val_id_blocks" in config: self.val_id_blocks.set(config["val_id_blocks"])
                    if "sync_mode" in config: self.combo_sync_mode_var.set(config["sync_mode"])
                    if "merge_sync_mode" in config: self.combo_merge_sync_mode_var.set(config["merge_sync_mode"])
                    if "fixed_vid_speed" in config: self.val_fixed_vid_speed.set(config["fixed_vid_speed"])
                    if "fixed_aud_speed" in config: self.val_fixed_aud_speed.set(config["fixed_aud_speed"])
                    
                    self.toggle_sync_opts()
                    
                    if "adv_vid_vol" in config: self.val_vid_vol.set(config["adv_vid_vol"])
                    if "adv_aud_vol" in config: self.val_aud_vol.set(config["adv_aud_vol"])
                    if "wm_enabled" in config: self.val_wm_enabled.set(config["wm_enabled"])
                    if "wm_path" in config: self.val_wm_path.set(config["wm_path"])
                    if "wm_x" in config: self.val_wm_x.set(config["wm_x"])
                    if "wm_y" in config: self.val_wm_y.set(config["wm_y"])
                    if "wm_scale" in config: self.val_wm_scale.set(config["wm_scale"])
                    
                    if "val_threads" in config:
                        self.slider_threads.set(config["val_threads"])
                        self.label_threads_val.configure(text=f"{config['val_threads']}")
        except Exception as e:
            print(f"Error loading config: {e}")

    def save_sync_config(self, silent=False):
        try:
            config = {
                "val_min_video": self.val_min_video.get(),
                "val_max_video": self.val_max_video.get(),
                "val_max_audio": self.val_max_audio.get(),
                "val_rest_time": self.val_rest_time.get(),
                "val_rest_blocks": self.val_rest_blocks.get(),
                "val_id_blocks": self.val_id_blocks.get(),
                "sync_mode": self.combo_sync_mode_var.get(),
                "merge_sync_mode": self.combo_merge_sync_mode_var.get(),
                "fixed_vid_speed": self.val_fixed_vid_speed.get(),
                "fixed_aud_speed": self.val_fixed_aud_speed.get(),
                "adv_vid_vol": self.val_vid_vol.get(),
                "adv_aud_vol": self.val_aud_vol.get(),
                "wm_enabled": self.val_wm_enabled.get(),
                "wm_path": self.val_wm_path.get(),
                "wm_x": self.val_wm_x.get(),
                "wm_y": self.val_wm_y.get(),
                "wm_scale": self.val_wm_scale.get()
            }
            with open("app_config.json", "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
            if not silent:
                messagebox.showinfo("Thành công", "Đã lưu cấu hình!")
        except Exception as e:
            if not silent:
                messagebox.showerror("Lỗi", f"Không thể lưu cấu hình: {e}")

    def reset_sync_config(self):
        self.val_min_video.set("0.85")
        self.val_max_video.set("1.15")
        self.val_max_audio.set("1.15")
        self.combo_sync_mode_var.set("Khớp từng câu (Anti-Overlap)")
        self.combo_merge_sync_mode_var.set("Khớp từng câu (dùng cấu hình Tab 2)")
        self.val_fixed_vid_speed.set("1.0")
        self.val_fixed_aud_speed.set("1.0")
        self.toggle_sync_opts()
        
    def reset_adv_config(self):
        self.val_rest_time.set("10")
        self.val_rest_blocks.set("300")
        self.val_id_blocks.set("300")
        self.val_vid_vol.set("0")
        self.val_aud_vol.set("0")

    def reset_device_id(self):
        self.client.device.randomize()
        new_id = self.client.device.device_id
        messagebox.showinfo("Thành công", f"Đã tự động đổi sang thiết bị mới!\nDevice ID: {new_id}\nBạn đã có thể tiếp tục tạo giọng nói.")

    def update_rate_label(self, value):
        self.label_rate_val.configure(text=f"{value:.1f}")
        
    def update_threads_label(self, value):
        self.label_threads_val.configure(text=f"{int(value)}")
        try:
            config = {}
            if os.path.exists("app_config.json"):
                with open("app_config.json", "r", encoding="utf-8") as f:
                    config = json.load(f)
            config["val_threads"] = int(value)
            with open("app_config.json", "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
        except:
            pass

    def load_voices(self):
        try:
            voices = self.client.list_voices(lang="vi-VN")
            self.voices = voices
            voice_names = [
                "[Miễn Phí] Edge TTS - Nữ (vi-VN-HoaiMyNeural)",
                "[Miễn Phí] Edge TTS - Nam (vi-VN-NamMinhNeural)"
            ]
            voice_names.extend([f"{v.display_name} ({v.voice_type})" for v in voices])
            
            if not voice_names:
                voice_names = ["Không tìm thấy giọng đọc nào"]
                
            self.after(0, lambda: self.combo_voice.configure(values=voice_names))
            if voice_names:
                self.after(0, lambda: self.combo_voice.set(voice_names[0]))
        except Exception as e:
            err_msg = [f"Lỗi: {str(e)[:50]}..."]
            self.after(0, lambda: self.combo_voice.configure(values=err_msg))
            self.after(0, lambda: self.combo_voice.set(err_msg[0]))

    def get_selected_voice(self):
        selected_voice_str = self.combo_voice.get()
        try:
            return selected_voice_str.split("(")[-1].strip(")")
        except IndexError:
            return "BV421_vivn_streaming"
            
    def get_selected_rate(self):
        return str(round(self.slider_rate.get(), 1))

    # --- BASIC TTS TAB ---
    def on_generate_basic(self):
        text = self.text_input.get("1.0", "end-1c").strip()
        if not text:
            messagebox.showwarning("Cảnh báo", "Vui lòng nhập văn bản.")
            return

        voice_type = self.get_selected_voice()
        rate = self.get_selected_rate()

        save_path = filedialog.asksaveasfilename(
            defaultextension=".mp3",
            filetypes=[("MP3 Audio", "*.mp3")],
            title="Lưu file âm thanh",
            initialfile="ket_qua.mp3"
        )
        if not save_path:
            return

        self.btn_generate_basic.configure(state="disabled", text="Đang xử lý...")
        self.is_cancelled = False
        self.btn_stop.configure(state="normal")
        self.label_status.configure(text="Đang gửi yêu cầu TTS đến CapCut API...")
        threading.Thread(target=self.generate_tts_thread_basic, args=(text, voice_type, rate, save_path), daemon=True).start()

    def download_audio_from_api(self, result, save_path):
        tasks = (result.get("data") or {}).get("tasks") or []
        if not tasks:
            raise CapCutError(f"No task data returned from API: {result}")
        
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
        else:
            raise CapCutError("Không tìm thấy URL hoặc dữ liệu âm thanh.")

    def generate_tts_thread_basic(self, text, voice_type, rate, save_path):
        try:
            if voice_type.startswith("vi-VN-"):
                rate_str = format_edge_tts_rate(float(rate))
                self.after(0, lambda: self.label_status.configure(text=f"Đang tổng hợp bằng Edge TTS...", text_color="orange"))
                generate_edge_tts_sync(text, voice_type, rate_str, save_path, cancel_check=lambda: self.is_cancelled)
            else:
                def on_status(status):
                    if self.is_cancelled:
                        return False
                    if status not in ("success", "succeed"):
                        self.after(0, lambda s=status: self.label_status.configure(text=f"Đang chờ CapCut xử lý ({s})...", text_color="orange"))
                
                import re
                import tempfile
                import time
                
                max_length = 250
                chunks = []
                paragraphs = text.split('\n')
                for p in paragraphs:
                    p = p.strip()
                    if not p: continue
                    if len(p) <= max_length:
                        chunks.append(p)
                    else:
                        sentences = re.split(r'(?<=[.!?])\s+', p)
                        current_chunk = ""
                        for s in sentences:
                            if len(current_chunk) + len(s) <= max_length:
                                current_chunk += (" " if current_chunk else "") + s
                            else:
                                if current_chunk:
                                    chunks.append(current_chunk)
                                if len(s) > max_length:
                                    words = s.split(' ')
                                    temp = ""
                                    for w in words:
                                        if len(temp) + len(w) <= max_length:
                                            temp += (" " if temp else "") + w
                                        else:
                                            chunks.append(temp.strip())
                                            temp = w
                                    current_chunk = temp.strip()
                                else:
                                    current_chunk = s.strip()
                        if current_chunk:
                            chunks.append(current_chunk)
                
                import concurrent.futures
                import threading
                
                import hashlib
                from mutagen.mp3 import MP3
                
                temp_files = [None] * len(chunks)
                completed_count = 0
                lock = threading.Lock()
                
                cache_dir = os.path.join(tempfile.gettempdir(), "capcut_tts_cache")
                os.makedirs(cache_dir, exist_ok=True)
                
                def process_chunk(index, chunk_text):
                    nonlocal completed_count
                    if self.is_cancelled:
                        return
                        
                    hash_str = hashlib.md5(f"{voice_type}_{rate}_{chunk_text}".encode('utf-8')).hexdigest()
                    cache_path = os.path.join(cache_dir, f"{hash_str}.mp3")
                    temp_files[index] = cache_path
                    
                    if os.path.exists(cache_path):
                        try:
                            audio = MP3(cache_path)
                            if audio.info.length > 0:
                                with lock:
                                    completed_count += 1
                                    current = completed_count
                                self.after(0, lambda c=current, t=len(chunks): self.label_status.configure(text=f"Đã xử lý xong {c}/{t} đoạn (từ bộ nhớ đệm)...", text_color="orange"))
                                return
                        except Exception:
                            pass
                    
                    result = self.client.generate_speech(texts=chunk_text, voice=voice_type, rate=rate, wait=True, status_callback=on_status)
                    self.download_audio_from_api(result, cache_path)
                    
                    with lock:
                        completed_count += 1
                        current = completed_count
                    self.after(0, lambda c=current, t=len(chunks): self.label_status.configure(text=f"Đã xử lý xong {c}/{t} đoạn âm thanh...", text_color="orange"))
                
                num_threads = min(len(chunks), int(self.slider_threads.get()) if hasattr(self, "slider_threads") else 10)
                self.after(0, lambda: self.label_status.configure(text=f"Đang bắt đầu xử lý {len(chunks)} đoạn với {num_threads} luồng...", text_color="orange"))
                
                failed_chunks_info = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
                    future_to_chunk = {}
                    for i, chunk in enumerate(chunks):
                        future = executor.submit(process_chunk, i, chunk)
                        future_to_chunk[future] = chunk
                        
                    for future in concurrent.futures.as_completed(future_to_chunk):
                        chunk_text = future_to_chunk[future]
                        if self.is_cancelled:
                            executor.shutdown(wait=False, cancel_futures=True)
                            raise Exception("Đã huỷ bởi người dùng.")
                        try:
                            future.result() # raises exception if any
                        except Exception as e:
                            failed_chunks_info.append(f"Lỗi: {str(e)}\nĐoạn văn bản:\n{chunk_text}\n{'-'*50}\n")
                            
                if failed_chunks_info:
                    error_txt_path = save_path + ".doan_loi.txt"
                    with open(error_txt_path, "w", encoding="utf-8") as err_f:
                        err_f.write("\n".join(failed_chunks_info))
                    raise Exception(f"Có {len(failed_chunks_info)} đoạn bị lỗi (có thể do chứa từ cấm hoặc lỗi mạng).\n\nĐã xuất nội dung các đoạn lỗi ra file:\n{error_txt_path}\n\nVui lòng mở file này để kiểm tra, sửa lỗi trên giao diện và bấm Bắt đầu lại.")
                            
                self.after(0, lambda: self.label_status.configure(text=f"Đang ghép {len(chunks)} file âm thanh...", text_color="orange"))
                with open(save_path, "wb") as outfile:
                    for f in temp_files:
                        if f and os.path.exists(f):
                            with open(f, "rb") as infile:
                                outfile.write(infile.read())
                                
                # Xoá cache sau khi ghép thành công
                for f in temp_files:
                    if f and os.path.exists(f):
                        try:
                            os.remove(f)
                        except:
                            pass
                
            self.after(0, lambda: self.label_status.configure(text=f"Hoàn tất! Đã lưu tại: {save_path}", text_color="green"))
            self.after(0, lambda: messagebox.showinfo("Thành công", f"Đã lưu file âm thanh thành công tại:\n{save_path}"))
        except Exception as e:
            self.after(0, lambda e=e: self.label_status.configure(text=f"Lỗi: {e}", text_color="red"))
            self.after(0, lambda e=e: messagebox.showerror("Lỗi", f"Có lỗi xảy ra:\n{e}"))
        finally:
            self.after(0, lambda: self.btn_generate_basic.configure(state="normal", text="Tạo Giọng Nói (TTS) và Lưu..."))
            self.after(0, lambda: self.btn_stop.configure(state="disabled"))

    # --- SRT TO CAPCUT TAB ---
    def select_srt(self):
        path = filedialog.askopenfilename(filetypes=[("SRT Subtitles", "*.srt")])
        if path:
            self.srt_path.set(path)
            
    def select_json(self):
        path = filedialog.askdirectory(title="Chọn Thư mục Dự án CapCut")
        if path:
            json_path = os.path.join(path, "draft_content.json")
            if not os.path.exists(json_path):
                messagebox.showwarning("Cảnh báo", f"Không tìm thấy file draft_content.json trong thư mục này.\nVui lòng chọn đúng thư mục chứa dự án CapCut.")
            else:
                self.json_path.set(json_path)

    def on_generate_srt(self):
        srt_file = self.srt_path.get()
        json_file = self.json_path.get()
        
        if not srt_file or not os.path.exists(srt_file):
            messagebox.showwarning("Lỗi", "Vui lòng chọn file SRT hợp lệ.")
            return
        if not json_file or not os.path.exists(json_file):
            messagebox.showwarning("Lỗi", "Vui lòng chọn file draft_content.json hợp lệ.")
            return
            
        voice_type = self.get_selected_voice()
        rate = self.get_selected_rate()
        sync_mode = self.combo_sync_mode_var.get()
        num_threads = int(self.slider_threads.get())
        
        min_vid = 0.85
        max_vid = 1.15
        max_aud = 1.15
        fixed_vid_speed = 1.0
        fixed_aud_speed = 1.0
        
        try:
            val_rest_time = int(self.val_rest_time.get())
            val_rest_blocks = int(self.val_rest_blocks.get())
            val_id_blocks = int(self.val_id_blocks.get())
        except ValueError:
            messagebox.showwarning("Lỗi", "Vui lòng nhập số nguyên hợp lệ cho cấu hình chống ban.")
            return

        if sync_mode == "Khớp từng câu (Anti-Overlap)":
            try:
                min_vid = float(self.val_min_video.get())
                max_vid = float(self.val_max_video.get())
                max_aud = float(self.val_max_audio.get())
            except ValueError:
                messagebox.showwarning("Lỗi", "Vui lòng nhập số hợp lệ cho các ô cấu hình Anti-Overlap (ví dụ: 0.85, 1.15).")
                return
        elif sync_mode == "Đổi tốc độ toàn bộ (Fixed Speed)":
            try:
                fixed_vid_speed = float(self.val_fixed_vid_speed.get())
                fixed_aud_speed = float(self.val_fixed_aud_speed.get())
            except ValueError:
                messagebox.showwarning("Lỗi", "Vui lòng nhập số hợp lệ cho Tốc độ Video/Audio (ví dụ: 1.0, 1.5).")
                return

        adv_settings = self.get_adv_settings()
        self.btn_generate_srt.configure(state="disabled", text="Đang xử lý...")
        self.is_cancelled = False
        self.btn_stop.configure(state="normal")
        threading.Thread(target=self.generate_srt_thread, args=(srt_file, json_file, voice_type, rate, sync_mode, min_vid, max_vid, max_aud, num_threads, val_rest_time, val_rest_blocks, val_id_blocks, adv_settings, fixed_vid_speed, fixed_aud_speed), daemon=True).start()
        
    def generate_srt_thread(self, srt_file, json_file, voice_type, rate, sync_mode, min_vid_spd, max_vid_spd, max_aud_spd, num_threads, val_rest_time, val_rest_blocks, val_id_blocks, adv_settings, fixed_vid_speed, fixed_aud_speed):
        try:
            subs = pysrt.open(srt_file)
            total = len(subs)
            if total == 0:
                raise Exception("File SRT rỗng hoặc không hợp lệ.")
                
            self.after(0, lambda: self.progressbar.set(0))
            self.after(0, lambda: self.label_progress.configure(text=f"Tiến độ: 0 / {total} câu"))
            self.after(0, lambda: self.label_status.configure(text="Bắt đầu tạo âm thanh từ SRT (chạy đa luồng)..."))

            # Create an output dir for this project's audio
            proj_dir = os.path.dirname(json_file)
            audio_dir = os.path.join(proj_dir, "tts_audios")
            os.makedirs(audio_dir, exist_ok=True)
            
            import concurrent.futures
            
            completed = 0
            lock = threading.Lock()
            
            def process_sub(i, sub):
                nonlocal completed
                if self.is_cancelled:
                    return None
                text = sub.text.replace("\n", " ").strip()
                if not text:
                    return None
                    
                start_micros = sub.start.ordinal * 1000
                end_micros = sub.end.ordinal * 1000
                original_duration_micros = end_micros - start_micros
                
                save_path = os.path.join(audio_dir, f"audio_{i+1:04d}.mp3")
                
                # Check if audio exists and is valid
                if os.path.exists(save_path):
                    try:
                        audio = MP3(save_path)
                        audio_duration_micros = int(audio.info.length * 1000000)
                        if audio_duration_micros > 0:
                            # Calculate speed to return consistent results
                            video_speed = 1.0
                            if sync_mode == "Khớp từng câu (Anti-Overlap)":
                                video_speed = original_duration_micros / audio_duration_micros
                                if video_speed < min_vid_spd:
                                    video_speed = min_vid_spd
                                elif video_speed > max_vid_spd:
                                    video_speed = max_vid_spd
                                    
                            with lock:
                                completed += 1
                                progress_val = completed / total
                                self.after(0, lambda pv=progress_val: self.progressbar.set(pv))
                                self.after(0, lambda c=completed, t=total: self.label_progress.configure(text=f"Tiến độ: {c} / {t} câu"))
                            return {
                                "index": i,
                                "path": save_path,
                                "start": start_micros,
                                "end": end_micros,
                                "duration": audio_duration_micros,
                                "video_speed": video_speed,
                                "report_item": None
                            }
                    except Exception:
                        pass
                
                local_client = CapCutClient(device=self.client.device)
                
                try:
                    if voice_type.startswith("vi-VN-"):
                        rate_str = format_edge_tts_rate(float(rate))
                        self.after(0, lambda: self.label_status.configure(text=f"Câu {i+1} đang tổng hợp bằng Edge TTS...", text_color="orange"))
                        generate_edge_tts_sync(text, voice_type, rate_str, save_path, cancel_check=lambda: self.is_cancelled)
                    else:
                        def on_status(status):
                            if self.is_cancelled:
                                return False
                            if status not in ("success", "succeed"):
                                self.after(0, lambda s=status: self.label_status.configure(text=f"Câu {i+1} đang chờ CapCut ({s})...", text_color="orange"))
                        
                        result = local_client.generate_speech(texts=text, voice=voice_type, rate=rate, wait=True, status_callback=on_status)
                        self.download_audio_from_api(result, save_path)
                except Exception as e:
                    if self.is_cancelled:
                        return None
                    # Fallback or record failed sentence
                    return None
                
                try:
                    audio = MP3(save_path)
                    audio_duration_micros = int(audio.info.length * 1000000)
                except Exception:
                    audio_duration_micros = original_duration_micros
                
                video_speed = 1.0
                report_item = None
                
                if sync_mode == "Khớp từng câu (Anti-Overlap)":
                    video_speed = original_duration_micros / audio_duration_micros if audio_duration_micros > 0 else 1.0
                    
                    if video_speed < min_vid_spd:
                        required_speedup = audio_duration_micros / (original_duration_micros / min_vid_spd) if original_duration_micros > 0 else 2.0
                        
                        if required_speedup > max_aud_spd:
                            applied_speedup = max_aud_spd
                            overlap_sec = (audio_duration_micros / max_aud_spd - (original_duration_micros / min_vid_spd)) / 1000000.0
                            report_item = {"index": i + 1, "text": text, "overlap_sec": round(overlap_sec, 2)}
                        else:
                            applied_speedup = required_speedup
                            
                        new_rate = str(round(float(rate) * applied_speedup, 1))
                        try:
                            if voice_type.startswith("vi-VN-"):
                                new_rate_str = format_edge_tts_rate(float(new_rate))
                                generate_edge_tts_sync(text, voice_type, new_rate_str, save_path, cancel_check=lambda: self.is_cancelled)
                            else:
                                result = local_client.generate_speech(texts=text, voice=voice_type, rate=new_rate, wait=True)
                                self.download_audio_from_api(result, save_path)
                            
                            audio = MP3(save_path)
                            audio_duration_micros = int(audio.info.length * 1000000)
                            video_speed = original_duration_micros / audio_duration_micros if audio_duration_micros > 0 else 1.0
                            if video_speed < min_vid_spd:
                                video_speed = min_vid_spd
                        except Exception:
                            pass
                            
                    elif video_speed > max_vid_spd:
                        video_speed = max_vid_spd
                
                with lock:
                    completed += 1
                    progress_val = completed / total
                    self.after(0, lambda pv=progress_val: self.progressbar.set(pv))
                    self.after(0, lambda c=completed, t=total: self.label_progress.configure(text=f"Tiến độ: {c} / {t} câu"))
                    
                    is_rest = False
                    is_id_rotated = False
                    
                    if val_id_blocks > 0 and completed % val_id_blocks == 0:
                        self.client.device.randomize()
                        is_id_rotated = True
                        
                    if val_rest_blocks > 0 and completed % val_rest_blocks == 0:
                        is_rest = True
                    
                    if is_rest and is_id_rotated:
                        self.after(0, lambda c=completed: self.label_status.configure(text=f"Đã tạo {c} câu. Đổi ID và nghỉ {val_rest_time}s..."))
                        for _ in range(int(val_rest_time * 10)):
                            if self.is_cancelled: return None
                            time.sleep(0.1)
                    elif is_rest:
                        self.after(0, lambda c=completed: self.label_status.configure(text=f"Đã tạo {c} câu. Nghỉ {val_rest_time}s chống block..."))
                        for _ in range(int(val_rest_time * 10)):
                            if self.is_cancelled: return None
                            time.sleep(0.1)
                    elif is_id_rotated:
                        self.after(0, lambda c=completed: self.label_status.configure(text=f"Đã tạo {c} câu. Vừa đổi Device ID mới!"))
                    else:
                        self.after(0, lambda txt=text: self.label_status.configure(text=f"Vừa tạo xong: {txt[:30]}..."))
                
                return {
                    "index": i,
                    "path": save_path,
                    "start": start_micros,
                    "end": end_micros,
                    "duration": audio_duration_micros,
                    "video_speed": video_speed,
                    "report_item": report_item
                }
            
            audio_info_list = []
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
                futures = [executor.submit(process_sub, i, sub) for i, sub in enumerate(subs)]
                for future in concurrent.futures.as_completed(futures):
                    if self.is_cancelled:
                        executor.shutdown(wait=False, cancel_futures=True)
                        raise Exception("Đã dừng xử lý theo yêu cầu.")
                    try:
                        res = future.result()
                        if res is not None:
                            audio_info_list.append(res)
                    except Exception:
                        pass
                        
            if self.is_cancelled:
                return

            # Check for missing/failed audio files
            missing_indices = []
            missing_subs = pysrt.SubRipFile()
            for i, sub in enumerate(subs):
                audio_path = os.path.join(audio_dir, f"audio_{i+1:04d}.mp3")
                if not os.path.exists(audio_path):
                    missing_indices.append(i)
                    missing_subs.append(sub)
                else:
                    try:
                        audio = MP3(audio_path)
                        if audio.info.length <= 0:
                            missing_indices.append(i)
                            missing_subs.append(sub)
                    except Exception:
                        missing_indices.append(i)
                        missing_subs.append(sub)

            missing_srt_path = os.path.join(proj_dir, "missing_subs.srt")
            if missing_indices:
                missing_items = []
                for i in missing_indices:
                    sub = subs[i]
                    start_micros = sub.start.ordinal * 1000
                    end_micros = sub.end.ordinal * 1000
                    orig_dur = end_micros - start_micros
                    audio_path = os.path.join(audio_dir, f"audio_{i+1:04d}.mp3")
                    sub_id = getattr(sub, 'index', i + 1) or (i + 1)
                    missing_items.append({
                        "index": i,
                        "sub_index": sub_id,
                        "text": sub.text.replace("\n", " ").strip(),
                        "sub": sub,
                        "save_path": audio_path,
                        "start_micros": start_micros,
                        "end_micros": end_micros,
                        "original_duration_micros": orig_dur,
                        "status": "pending",
                        "error_msg": ""
                    })

                def generate_single_item(item, new_text):
                    save_path = item["save_path"]
                    local_client = CapCutClient(device=self.client.device)
                    try:
                        if voice_type.startswith("vi-VN-"):
                            rate_str = format_edge_tts_rate(float(rate))
                            generate_edge_tts_sync(new_text, voice_type, rate_str, save_path)
                        else:
                            result = local_client.generate_speech(texts=new_text, voice=voice_type, rate=rate, wait=True)
                            self.download_audio_from_api(result, save_path)

                        audio = MP3(save_path)
                        dur = int(audio.info.length * 1000000)
                        if dur <= 0:
                            return False, "File âm thanh tải về rỗng (duration 0s)."

                        video_speed = 1.0
                        report_item = None
                        orig_dur = item.get("original_duration_micros", item["end_micros"] - item["start_micros"])

                        if sync_mode == "Khớp từng câu (Anti-Overlap)":
                            video_speed = orig_dur / dur if dur > 0 else 1.0
                            if video_speed < min_vid_spd:
                                req_spd = dur / (orig_dur / min_vid_spd) if orig_dur > 0 else 2.0
                                if req_spd > max_aud_spd:
                                    app_spd = max_aud_spd
                                    overlap_sec = (dur / max_aud_spd - (orig_dur / min_vid_spd)) / 1000000.0
                                    report_item = {"index": item["index"] + 1, "text": new_text, "overlap_sec": round(overlap_sec, 2)}
                                else:
                                    app_spd = req_spd
                                new_rate = str(round(float(rate) * app_spd, 1))
                                try:
                                    if voice_type.startswith("vi-VN-"):
                                        new_rate_str = format_edge_tts_rate(float(new_rate))
                                        generate_edge_tts_sync(new_text, voice_type, new_rate_str, save_path)
                                    else:
                                        result = local_client.generate_speech(texts=new_text, voice=voice_type, rate=new_rate, wait=True)
                                        self.download_audio_from_api(result, save_path)
                                    audio = MP3(save_path)
                                    dur = int(audio.info.length * 1000000)
                                    video_speed = orig_dur / dur if dur > 0 else 1.0
                                    if video_speed < min_vid_spd:
                                        video_speed = min_vid_spd
                                except Exception:
                                    pass
                            elif video_speed > max_vid_spd:
                                video_speed = max_vid_spd

                        return True, {
                            "index": item["index"],
                            "path": save_path,
                            "start": item["start_micros"],
                            "end": item["end_micros"],
                            "duration": dur,
                            "video_speed": video_speed,
                            "report_item": report_item
                        }
                    except Exception as ex:
                        return False, str(ex)

                user_choice = [None]
                dialog_event = threading.Event()

                def on_dialog_proceed(resolved, unresolved):
                    user_choice[0] = ("proceed", resolved, unresolved)
                    dialog_event.set()

                def on_dialog_cancel():
                    user_choice[0] = ("cancel",)
                    dialog_event.set()

                def show_dialog():
                    TTSErrorReviewDialog(
                        parent=self,
                        missing_items=missing_items,
                        generate_fn=generate_single_item,
                        on_proceed_callback=on_dialog_proceed,
                        on_cancel_callback=on_dialog_cancel,
                        initial_logs=[("TTS_NEEDS_REVIEW", f"Có {len(missing_items)} câu bị lỗi CapCut cần xử lý.")]
                    )

                self.after(0, show_dialog)
                dialog_event.wait()

                if not user_choice[0] or user_choice[0][0] == "cancel":
                    missing_subs = pysrt.SubRipFile()
                    for it in missing_items:
                        if it.get("status") != "success":
                            missing_subs.append(it["sub"])
                    if missing_subs:
                        missing_subs.save(missing_srt_path, encoding='utf-8')
                    self.after(0, lambda: self.label_status.configure(text=f"Đã dừng. Danh sách câu lỗi lưu tại missing_subs.srt", text_color="orange"))
                    return

                # Proceed
                _, resolved_items, unresolved_items = user_choice[0]
                for res_it in resolved_items:
                    if "result_info" in res_it and res_it["result_info"]:
                        audio_info_list.append(res_it["result_info"])

                if unresolved_items:
                    rem_subs = pysrt.SubRipFile()
                    for it in unresolved_items:
                        rem_subs.append(it["sub"])
                    rem_subs.save(missing_srt_path, encoding='utf-8')
                else:
                    if os.path.exists(missing_srt_path):
                        try:
                            os.remove(missing_srt_path)
                        except Exception:
                            pass
            else:
                if os.path.exists(missing_srt_path):
                    try:
                        os.remove(missing_srt_path)
                    except Exception:
                        pass

            # Build full audio info list ensuring all segments are represented
            final_audio_info = []
            audio_map = {item["index"]: item for item in audio_info_list}
            for i, sub in enumerate(subs):
                text = sub.text.replace("\n", " ").strip()
                if not text:
                    continue
                start_micros = sub.start.ordinal * 1000
                end_micros = sub.end.ordinal * 1000
                original_duration_micros = end_micros - start_micros
                audio_path = os.path.join(audio_dir, f"audio_{i+1:04d}.mp3")
                
                if i in audio_map:
                    final_audio_info.append(audio_map[i])
                elif os.path.exists(audio_path):
                    try:
                        audio = MP3(audio_path)
                        dur = int(audio.info.length * 1000000)
                        final_audio_info.append({
                            "index": i,
                            "path": audio_path,
                            "start": start_micros,
                            "end": end_micros,
                            "duration": dur,
                            "video_speed": 1.0,
                            "report_item": None,
                        })
                    except Exception:
                        final_audio_info.append({
                            "index": i,
                            "path": "",
                            "start": start_micros,
                            "end": end_micros,
                            "duration": original_duration_micros,
                            "video_speed": 1.0,
                            "report_item": None,
                            "is_dummy": True,
                        })
                else:
                    final_audio_info.append({
                        "index": i,
                        "path": "",
                        "start": start_micros,
                        "end": end_micros,
                        "duration": original_duration_micros,
                        "video_speed": 1.0,
                        "report_item": None,
                        "is_dummy": True,
                    })

            final_audio_info.sort(key=lambda x: x["index"])

            reports = [info["report_item"] for info in final_audio_info if info.get("report_item")]
            if reports:
                report_path = os.path.join(proj_dir, "overlap_report.json")
                with open(report_path, "w", encoding="utf-8") as f:
                    json.dump(reports, f, ensure_ascii=False, indent=2)

            # All audios generated, modify CapCut JSON
            if sync_mode == "Khớp từng câu (Anti-Overlap)":
                self.after(0, lambda: self.label_status.configure(text="Đang phân tích timeline và chia nhỏ video..."))
            elif sync_mode == "Đổi tốc độ toàn bộ (Fixed Speed)":
                self.after(0, lambda: self.label_status.configure(text="Đang phân tích timeline và thay đổi tốc độ video/audio..."))
            else:
                self.after(0, lambda: self.label_status.configure(text="Đang chèn âm thanh vào dự án CapCut..."))
                
            modify_capcut_project(json_file, final_audio_info, sync_mode, adv_settings, fixed_vid_speed, fixed_aud_speed)
            
            self.after(0, lambda: self.label_status.configure(text="Hoàn tất!", text_color="green"))
            
            msg = f"Đã chèn âm thanh thành công!\nVui lòng tải lại dự án trên CapCut."
            if missing_indices:
                msg += f"\n\n(Đã bỏ qua {len(missing_indices)} câu lỗi không có âm thanh)"
            if reports:
                msg += f"\n\nLưu ý: Có {len(reports)} câu dịch quá dài không thể ép vừa khớp tốc độ. Đã lưu báo cáo tại overlap_report.json"
            self.after(0, lambda m=msg: messagebox.showinfo("Thành công", m))
            
        except Exception as e:
            self.after(0, lambda e=e: self.label_status.configure(text=f"Lỗi: {e}", text_color="red"))
            self.after(0, lambda e=e: messagebox.showerror("Lỗi", f"Có lỗi xảy ra:\n{e}"))
        finally:
            self.after(0, lambda: self.btn_generate_srt.configure(state="normal", text="Bắt đầu xử lý SRT"))
            self.after(0, lambda: self.btn_stop.configure(state="disabled"))


    # --- MERGE EXISTING AUDIO TAB ---
    def select_merge_srt(self):
        path = filedialog.askopenfilename(filetypes=[("SRT Subtitles", "*.srt")])
        if path:
            self.merge_srt_path.set(path)
            
    def select_merge_json(self):
        path = filedialog.askdirectory(title="Chọn Thư mục Dự án CapCut")
        if path:
            json_path = os.path.join(path, "draft_content.json")
            if not os.path.exists(json_path):
                messagebox.showwarning("Cảnh báo", f"Không tìm thấy file draft_content.json trong thư mục này.\nVui lòng chọn đúng thư mục chứa dự án CapCut.")
            else:
                self.merge_json_path.set(json_path)
                audio_dir = os.path.join(path, "tts_audios")
                if os.path.exists(audio_dir) and not self.merge_audio_path.get():
                    self.merge_audio_path.set(audio_dir)
                    
    def select_merge_audio(self):
        path = filedialog.askdirectory(title="Chọn Thư mục chứa Audio")
        if path:
            self.merge_audio_path.set(path)

    def on_generate_merge(self):
        srt_file = self.merge_srt_path.get()
        json_file = self.merge_json_path.get()
        audio_dir = self.merge_audio_path.get()
        
        if not srt_file or not os.path.exists(srt_file):
            messagebox.showwarning("Lỗi", "Vui lòng chọn file SRT hợp lệ.")
            return
        if not json_file or not os.path.exists(json_file):
            messagebox.showwarning("Lỗi", "Vui lòng chọn file draft_content.json hợp lệ.")
            return
        if not audio_dir or not os.path.exists(audio_dir):
            messagebox.showwarning("Lỗi", "Vui lòng chọn thư mục chứa Audio hợp lệ.")
            return
            
        sync_mode = self.combo_merge_sync_mode_var.get()
        min_vid = 0.85
        max_vid = 1.15
        fixed_vid_speed = 1.0
        fixed_aud_speed = 1.0
        
        if sync_mode == "Khớp từng câu (dùng cấu hình Tab 2)":
            try:
                min_vid = float(self.val_min_video.get())
                max_vid = float(self.val_max_video.get())
            except ValueError:
                messagebox.showwarning("Lỗi", "Vui lòng nhập số hợp lệ cho cấu hình Anti-Overlap ở Tab 2.")
                return
        elif sync_mode == "Đổi tốc độ toàn bộ (dùng cấu hình Tab 2)":
            try:
                fixed_vid_speed = float(self.val_fixed_vid_speed.get())
                fixed_aud_speed = float(self.val_fixed_aud_speed.get())
            except ValueError:
                messagebox.showwarning("Lỗi", "Vui lòng nhập số hợp lệ cho Tốc độ Video/Audio ở Tab 2.")
                return

        adv_settings = self.get_adv_settings()
        self.btn_generate_merge.configure(state="disabled", text="Đang ghép...")
        self.is_cancelled = False
        self.btn_stop.configure(state="normal")
        threading.Thread(target=self.generate_merge_thread, args=(srt_file, json_file, audio_dir, sync_mode, min_vid, max_vid, adv_settings, fixed_vid_speed, fixed_aud_speed), daemon=True).start()

    def generate_merge_thread(self, srt_file, json_file, audio_dir, sync_mode, min_vid_spd, max_vid_spd, adv_settings, fixed_vid_speed, fixed_aud_speed):
        try:
            voice_type = self.get_selected_voice()
            rate = self.get_selected_rate()
            num_threads = int(self.slider_threads.get())

            subs = pysrt.open(srt_file)
            total = len(subs)
            if total == 0:
                raise Exception("File SRT rỗng hoặc không hợp lệ.")
                
            self.after(0, lambda: self.merge_progressbar.set(0))
            self.after(0, lambda: self.merge_label_progress.configure(text=f"Tiến độ: 0 / {total} câu"))
            self.after(0, lambda: self.label_status.configure(text="Đang phân tích thời lượng Audio offline..."))

            # Phase 1: Scan for missing blocks
            missing_indices = []
            for i, sub in enumerate(subs):
                audio_path = os.path.join(audio_dir, f"audio_{i+1:04d}.mp3")
                if not os.path.exists(audio_path):
                    missing_indices.append(i)
                else:
                    try:
                        audio = MP3(audio_path)
                        _ = audio.info.length
                    except Exception:
                        missing_indices.append(i)
            
            if missing_indices:
                missing_srt_path = srt_file.replace(".srt", "_missing.srt")
                missing_subs = pysrt.SubRipFile()
                for idx in missing_indices:
                    missing_subs.append(subs[idx])
                missing_subs.save(missing_srt_path, encoding='utf-8')
                
                self.after(0, lambda: self.label_status.configure(text=f"Phát hiện thiếu {len(missing_indices)} câu. Bắt đầu tải bù đa luồng..."))
                
                import concurrent.futures
                lock = threading.Lock()
                
                def download_missing(idx):
                    if self.is_cancelled: return False
                    sub = subs[idx]
                    text = sub.text.replace("\n", " ").strip()
                    if not text:
                        return True
                    audio_path = os.path.join(audio_dir, f"audio_{idx+1:04d}.mp3")
                    
                    try:
                        if voice_type.startswith("vi-VN-"):
                            rate_str = format_edge_tts_rate(float(rate))
                            self.after(0, lambda: self.label_status.configure(text=f"Câu {idx+1} tải bù đang tổng hợp bằng Edge TTS...", text_color="orange"))
                            generate_edge_tts_sync(text, voice_type, rate_str, audio_path, cancel_check=lambda: self.is_cancelled)
                            return True
                        else:
                            def on_status(status):
                                if self.is_cancelled:
                                    return False
                                if status not in ("success", "succeed"):
                                    self.after(0, lambda s=status: self.label_status.configure(text=f"Câu {idx+1} tải bù đang chờ CapCut ({s})...", text_color="orange"))
                            
                            result = self.client.generate_speech(texts=text, voice=voice_type, rate=rate, wait=True, status_callback=on_status)
                            self.download_audio_from_api(result, audio_path)
                            return True
                    except Exception:
                        return False

                with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
                    futures = {executor.submit(download_missing, idx): idx for idx in missing_indices}
                    for future in concurrent.futures.as_completed(futures):
                        if self.is_cancelled:
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                        idx = futures[future]
                        success = future.result()
                        if success:
                            with lock:
                                for j in range(len(missing_subs)):
                                    if missing_subs[j].index == subs[idx].index:
                                        del missing_subs[j]
                                        break
                                missing_subs.save(missing_srt_path, encoding='utf-8')
                                
                if self.is_cancelled:
                    self.after(0, lambda: self.label_status.configure(text="Đã huỷ quá trình tải bù audio.", text_color="orange"))
                    return
                
                # Phase 3: Check again
                missing_indices = []
                for i, sub in enumerate(subs):
                    audio_path = os.path.join(audio_dir, f"audio_{i+1:04d}.mp3")
                    if not os.path.exists(audio_path):
                        missing_indices.append(i)
                    else:
                        try:
                            audio = MP3(audio_path)
                            _ = audio.info.length
                        except Exception:
                            missing_indices.append(i)
                
                if not missing_indices:
                    if os.path.exists(missing_srt_path):
                        os.remove(missing_srt_path)
                else:
                    missing_items = []
                    for i in missing_indices:
                        sub = subs[i]
                        start_micros = sub.start.ordinal * 1000
                        end_micros = sub.end.ordinal * 1000
                        orig_dur = end_micros - start_micros
                        audio_path = os.path.join(audio_dir, f"audio_{i+1:04d}.mp3")
                        sub_id = getattr(sub, 'index', i + 1) or (i + 1)
                        missing_items.append({
                            "index": i,
                            "sub_index": sub_id,
                            "text": sub.text.replace("\n", " ").strip(),
                            "sub": sub,
                            "save_path": audio_path,
                            "start_micros": start_micros,
                            "end_micros": end_micros,
                            "original_duration_micros": orig_dur,
                            "status": "pending",
                            "error_msg": ""
                        })

                    def generate_single_item_merge(item, new_text):
                        save_path = item["save_path"]
                        local_client = CapCutClient(device=self.client.device)
                        try:
                            if voice_type.startswith("vi-VN-"):
                                rate_str = format_edge_tts_rate(float(rate))
                                generate_edge_tts_sync(new_text, voice_type, rate_str, save_path)
                            else:
                                result = local_client.generate_speech(texts=new_text, voice=voice_type, rate=rate, wait=True)
                                self.download_audio_from_api(result, save_path)

                            audio = MP3(save_path)
                            dur = int(audio.info.length * 1000000)
                            if dur <= 0:
                                return False, "File âm thanh tải về rỗng (duration 0s)."
                            return True, {"duration": dur}
                        except Exception as ex:
                            return False, str(ex)

                    user_choice = [None]
                    dialog_event = threading.Event()

                    def on_dialog_proceed_merge(resolved, unresolved):
                        user_choice[0] = ("proceed", resolved, unresolved)
                        dialog_event.set()

                    def on_dialog_cancel_merge():
                        user_choice[0] = ("cancel",)
                        dialog_event.set()

                    def show_dialog_merge():
                        TTSErrorReviewDialog(
                            parent=self,
                            missing_items=missing_items,
                            generate_fn=generate_single_item_merge,
                            on_proceed_callback=on_dialog_proceed_merge,
                            on_cancel_callback=on_dialog_cancel_merge,
                            initial_logs=[("TTS_NEEDS_REVIEW", f"Có {len(missing_items)} câu bị lỗi cần xử lý.")]
                        )

                    self.after(0, show_dialog_merge)
                    dialog_event.wait()

                    if not user_choice[0] or user_choice[0][0] == "cancel":
                        self.after(0, lambda: self.label_status.configure(text="Đã dừng ghép theo yêu cầu người dùng.", text_color="red"))
                        return

                    _, resolved_items, unresolved_items = user_choice[0]
                    if unresolved_items:
                        rem_subs = pysrt.SubRipFile()
                        for it in unresolved_items:
                            rem_subs.append(it["sub"])
                        rem_subs.save(missing_srt_path, encoding='utf-8')
                    else:
                        if os.path.exists(missing_srt_path):
                            try:
                                os.remove(missing_srt_path)
                            except Exception:
                                pass

            audio_info_list = []
            self.after(0, lambda: self.label_status.configure(text="Đang phân tích và xử lý logic chèn..."))
            
            for i, sub in enumerate(subs):
                if self.is_cancelled:
                    raise Exception("Đã dừng ghép theo yêu cầu.")
                text = sub.text.replace("\n", " ").strip()
                if not text:
                    continue
                    
                start_micros = sub.start.ordinal * 1000
                end_micros = sub.end.ordinal * 1000
                original_duration_micros = end_micros - start_micros
                
                audio_path = os.path.join(audio_dir, f"audio_{i+1:04d}.mp3")
                
                is_dummy = False
                video_speed = 1.0
                audio_duration_micros = original_duration_micros
                
                if not os.path.exists(audio_path):
                    is_dummy = True
                    audio_path = ""
                else:
                    try:
                        audio = MP3(audio_path)
                        audio_duration_micros = int(audio.info.length * 1000000)
                        if sync_mode == "Khớp từng câu (dùng cấu hình Tab 2)":
                            video_speed = original_duration_micros / audio_duration_micros if audio_duration_micros > 0 else 1.0
                            if video_speed < min_vid_spd:
                                video_speed = min_vid_spd
                            elif video_speed > max_vid_spd:
                                video_speed = max_vid_spd
                    except Exception:
                        is_dummy = True
                        audio_path = ""
                        
                audio_info_list.append({
                    "index": i,
                    "path": audio_path,
                    "start": start_micros,
                    "end": end_micros,
                    "duration": audio_duration_micros,
                    "video_speed": video_speed,
                    "is_dummy": is_dummy,
                    "report_item": None
                })
                
                progress_val = (i + 1) / total
                self.after(0, lambda pv=progress_val: self.merge_progressbar.set(pv))
                self.after(0, lambda c=i+1, t=total: self.merge_label_progress.configure(text=f"Đã phân tích: {c} / {t} câu"))

            if sync_mode == "Khớp từng câu (dùng cấu hình Tab 2)":
                self.after(0, lambda: self.label_status.configure(text="Đang phân tích timeline và chia nhỏ video..."))
            elif sync_mode == "Đổi tốc độ toàn bộ (dùng cấu hình Tab 2)":
                self.after(0, lambda: self.label_status.configure(text="Đang phân tích timeline và thay đổi tốc độ video/audio..."))
            else:
                self.after(0, lambda: self.label_status.configure(text="Đang chèn âm thanh vào dự án CapCut..."))
                
            modify_capcut_project(json_file, audio_info_list, sync_mode, adv_settings, fixed_vid_speed, fixed_aud_speed)
            
            self.after(0, lambda: self.label_status.configure(text="Hoàn tất!", text_color="green"))
            msg = f"Đã chèn âm thanh thành công!\nVui lòng tải lại dự án trên CapCut."
            self.after(0, lambda m=msg: messagebox.showinfo("Thành công", m))
            
        except Exception as e:
            self.after(0, lambda e=e: self.label_status.configure(text=f"Lỗi: {e}", text_color="red"))
            self.after(0, lambda e=e: messagebox.showerror("Lỗi", f"Có lỗi xảy ra:\n{e}"))
        finally:
            self.after(0, lambda: self.btn_generate_merge.configure(state="normal", text="Bắt đầu ghép vào CapCut"))
            self.after(0, lambda: self.btn_stop.configure(state="disabled"))

    def select_split_project(self):
        folder = filedialog.askdirectory(title="Chọn thư mục dự án CapCut")
        if folder:
            self.split_project_path.set(folder)

    def on_split_project(self):
        project_dir = self.split_project_path.get()
        if not project_dir:
            messagebox.showwarning("Thiếu thông tin", "Vui lòng chọn thư mục project!")
            return
            
        try:
            duration_min = float(self.split_duration_val.get())
            if duration_min <= 0:
                raise ValueError
        except:
            messagebox.showwarning("Lỗi nhập liệu", "Độ dài mỗi phần phải là số lớn hơn 0!")
            return
            
        self.btn_split_project.configure(state="disabled", text="Đang chia nhỏ...")
        self.label_status.configure(text="Đang xử lý chia nhỏ project...", text_color="blue")
        
        def run():
            try:
                parts = split_capcut_project(project_dir, duration_min)
                self.after(0, lambda: self.label_status.configure(text=f"Hoàn tất! Đã chia thành {len(parts)} phần.", text_color="green"))
                msg = f"Đã chia thành công {len(parts)} phần:\n" + "\n".join(parts) + "\n\nVui lòng khởi động lại CapCut để xem các project này."
                self.after(0, lambda m=msg: messagebox.showinfo("Thành công", m))
            except Exception as e:
                self.after(0, lambda e=e: self.label_status.configure(text=f"Lỗi: {e}", text_color="red"))
                self.after(0, lambda e=e: messagebox.showerror("Lỗi", f"Có lỗi xảy ra:\n{e}"))
            finally:
                self.after(0, lambda: self.btn_split_project.configure(state="normal", text="Bắt đầu chia nhỏ"))
                
        threading.Thread(target=run, daemon=True).start()


    # --- STT TAB ---
    def select_stt_media(self):
        path = filedialog.askopenfilename(filetypes=[("Media Files", "*.mp3 *.m4a *.mp4 *.wav *.aac *.flac")])
        if path:
            self.stt_media_path.set(path)
            
    def toggle_stt_translate(self):
        if self.chk_stt_translate_var.get():
            self.stt_target_lang_combo.configure(state="normal")
        else:
            self.stt_target_lang_combo.configure(state="disabled")

    def on_generate_stt(self):
        media_file = self.stt_media_path.get()
        if not media_file or not os.path.exists(media_file):
            messagebox.showwarning("Lỗi", "Vui lòng chọn file Media hợp lệ.")
            return
            
        lang = self.stt_lang_combo.get()
        use_trans = self.chk_stt_translate_var.get()
        target_lang = self.stt_target_lang_combo.get()
        
        default_name = os.path.splitext(os.path.basename(media_file))[0] + ("_translated.srt" if use_trans else "_stt.srt")
        out_srt = filedialog.asksaveasfilename(
            title="Lưu file SRT", 
            initialfile=default_name, 
            defaultextension=".srt", 
            filetypes=[("SRT Subtitles", "*.srt")]
        )
        if not out_srt:
            return
        
        self.btn_generate_stt.configure(state="disabled", text="Đang xử lý...")
        self.is_cancelled = False
        self.btn_stop.configure(state="normal")
        self.stt_progressbar.configure(mode="indeterminate")
        self.stt_progressbar.start()
        
        threading.Thread(target=self.generate_stt_thread, args=(media_file, lang, use_trans, target_lang, out_srt), daemon=True).start()
        
    def generate_stt_thread(self, media_file, lang, use_trans, target_lang, out_srt):
        temp_audio = None
        try:
            import subprocess
            ext = os.path.splitext(media_file)[1].lower()
            upload_file = media_file
            
            if ext in ['.mp4', '.mov', '.avi', '.mkv']:
                self.after(0, lambda: self.label_status.configure(text="Đang trích xuất âm thanh từ video (nhanh hơn)..."))
                temp_audio = os.path.splitext(media_file)[0] + "_temp_audio.mp3"
                try:
                    subprocess.run(["ffmpeg", "-y", "-i", media_file, "-q:a", "0", "-map", "a", temp_audio], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    upload_file = temp_audio
                except Exception as e:
                    upload_file = media_file

            self.after(0, lambda: self.label_status.configure(text="Đang tải file lên CapCut... (với file lớn có thể mất vài phút)"))
            
            # Khởi tạo client cục bộ nếu cần hoặc dùng client chính
            local_client = CapCutClient(device=self.client.device)
            if self.is_cancelled: raise Exception("Đã huỷ bởi người dùng.")
            
            upload_res = local_client.upload_audio(upload_file)
            
            if temp_audio and os.path.exists(temp_audio):
                try:
                    os.remove(temp_audio)
                except:
                    pass
            
            self.after(0, lambda: self.label_status.configure(text="Tải lên thành công! Đang gửi yêu cầu STT..."))
            
            stt_task = local_client.create_stt_task(
                audio_vid=upload_res.vid,
                audio_md5=upload_res.md5,
                duration_ms=upload_res.duration_ms or 10000,
                language=lang,
                translation_language=target_lang,
                use_translation=use_trans,
            )
            
            tasks = (stt_task.get("data") or {}).get("tasks") or []
            if not tasks:
                raise Exception("Không nhận được task từ API.")
                
            task_id = tasks[0]["id"]
            token = tasks[0]["token"]
            
            timeout = 900.0 # 15 minutes timeout for large files
            start_time = time.time()
            stt_res = None
            
            while time.time() - start_time < timeout:
                if self.is_cancelled:
                    raise Exception("Đã huỷ bởi người dùng.")
                elapsed = int(time.time() - start_time)
                self.after(0, lambda e=elapsed: self.label_status.configure(text=f"Đang chờ server xử lý STT... (đã chờ {e}s)"))
                
                query_res = local_client.query_stt_task(task_id, token)
                query_tasks = (query_res.get("data") or {}).get("tasks") or []
                if query_tasks:
                    status = query_tasks[0].get("status")
                    if status in ("success", "succeed"):
                        stt_res = query_res
                        break
                    elif status == "failed":
                        raise Exception("Server CapCut báo lỗi xử lý thất bại (có thể file không chứa giọng nói hợp lệ).")
                time.sleep(3.0)
                
            if not stt_res:
                raise Exception(f"Quá thời gian chờ ({timeout} giây). Vui lòng cắt nhỏ file hoặc thử lại sau.")
            
            self.after(0, lambda: self.label_status.configure(text="Đang phân tích kết quả trả về..."))
            
            subtitles = local_client.extract_subtitles(stt_res)
            
            if not subtitles.utterances:
                raise Exception("Không tìm thấy bất kỳ giọng nói nào trong file này (hoặc API trả về rỗng).")
                
            self.after(0, lambda: self.label_status.configure(text="Đang lưu file SRT..."))
            
            subs = pysrt.SubRipFile()
            for idx, ut in enumerate(subtitles.utterances, 1):
                item = pysrt.SubRipItem(
                    index=idx,
                    start=pysrt.SubRipTime(milliseconds=ut.start_time),
                    end=pysrt.SubRipTime(milliseconds=ut.end_time),
                    text=ut.text
                )
                subs.append(item)
                
            subs.save(out_srt, encoding='utf-8')
            
            self.after(0, lambda: self.label_status.configure(text="Hoàn tất!", text_color="green"))
            msg = f"Đã trích xuất SRT thành công!\nĐã lưu tại:\n{out_srt}"
            self.after(0, lambda m=msg: messagebox.showinfo("Thành công", m))
            
        except Exception as e:
            self.after(0, lambda e=e: self.label_status.configure(text=f"Lỗi: {e}", text_color="red"))
            self.after(0, lambda e=e: messagebox.showerror("Lỗi", f"Có lỗi xảy ra:\n{e}"))
        finally:
            self.after(0, lambda: self.stt_progressbar.stop())
            self.after(0, lambda: self.stt_progressbar.configure(mode="determinate"))
            self.after(0, lambda: self.stt_progressbar.set(0))
            self.after(0, lambda: self.btn_generate_stt.configure(state="normal", text="Bắt đầu trích xuất SRT"))
            self.after(0, lambda: self.btn_stop.configure(state="disabled"))

if __name__ == "__main__":
    app = CapCutTTSApp()
    app.mainloop()
