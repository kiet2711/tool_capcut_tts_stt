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

from capcut_tts_api import CapCutClient, CapCutError

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

def modify_capcut_project(draft_json_path, audio_info_list, sync_video=True, adv_settings=None):
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
    
    text_track = None
    max_segments = 0
    for track in tracks:
        if track.get("type") == "text":
            seg_len = len(track.get("segments", []))
            if seg_len >= len(audio_info_list) and seg_len > max_segments:
                text_track = track
                max_segments = seg_len
                
    if text_track:
        text_track["segments"].sort(key=lambda s: s.get("target_timerange", {}).get("start", 0))

    if sync_video:
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
        
        block_target_start = srt_start
        is_dummy = info.get("is_dummy", False)
        
        if sync_video:
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
            
        if text_track:
            # Ưu tiên lấy theo original index (vì nếu SRT có dòng trống bị skip, index sẽ bị lệch so với i)
            orig_idx = info.get("index", i)
            if orig_idx < len(text_track["segments"]):
                text_seg = text_track["segments"][orig_idx]
            elif i < len(text_track["segments"]):
                text_seg = text_track["segments"][i]
            else:
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
        
        materials["speeds"].append({"id": speed_id, "type": "speed", "mode": 0, "speed": 1.0, "curve_speed": None})
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
            "target_timerange": {"duration": audio_duration_micros, "start": block_target_start},
            "extra_material_refs": [speed_id, fade_id], "speed": 1.0, "volume": 1.0,
            "is_loop": False, "is_tone_modify": False, "reverse": False,
            "intensifies_audio": False, "cartoon": False, "last_nonzero_volume": 1.0,
            "render_index": 0, "state": 0, "clip": None, "enable_adjust": False,
            "enable_color_curves": True, "enable_color_wheels": True, "enable_lut": False,
            "enable_smart_color_adjust": False, "group_id": "", "hdr_settings": None,
            "is_placeholder": False, "keyframe_refs": [], "template_id": "",
            "template_scene": "default", "track_attribute": 0, "track_render_index": 0,
            "visible": True, "render_timerange": {"duration": 0, "start": 0}
        })
        
    if sync_video:
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
            
        font_size = adv_settings.get("font_size", 5.0)
        tc_hex = adv_settings.get("text_color", "#FFFFFF").lstrip('#')
        sc_hex = adv_settings.get("stroke_color", "#000000").lstrip('#')
        stroke_width = adv_settings.get("stroke_width", 0.08)
        
        tc_rgba = [int(tc_hex[i:i+2], 16) / 255.0 for i in (0, 2, 4)] + [1.0] if len(tc_hex) == 6 else [1.0, 1.0, 1.0, 1.0]
        sc_rgba = [int(sc_hex[i:i+2], 16) / 255.0 for i in (0, 2, 4)] + [1.0] if len(sc_hex) == 6 else [0.0, 0.0, 0.0, 1.0]
        tc_hex_full = adv_settings.get("text_color", "#FFFFFF")
        sc_hex_full = adv_settings.get("stroke_color", "#000000")
        use_preset = adv_settings.get("use_preset_cyan", False)
        
        for text_mat in materials.get("texts", []):
            if use_preset:
                text_mat["text_color"] = "#ffffff"
                text_mat["background_color"] = "#000000"
                text_mat["background_alpha"] = 1.0
                text_mat["background_style"] = 1
                text_mat["background_round_radius"] = 0.2
                text_mat["border_color"] = "#00ffff"
                text_mat["border_width"] = 0.08
                text_mat["border_alpha"] = 1.0
                text_mat["has_shadow"] = False
                font_size = adv_settings.get("font_size", 5.0)
                text_mat["font_size"] = font_size
                tc_rgba_content = [1.0, 1.0, 1.0, 1.0]
            else:
                text_mat["text_color"] = tc_hex_full.lower()
                text_mat["border_color"] = sc_hex_full.lower()
                text_mat["border_width"] = stroke_width
                text_mat["font_size"] = font_size
                tc_rgba_content = tc_rgba
            
            try:
                content_obj = json.loads(text_mat["content"])
                for style in content_obj.get("styles", []):
                    style["size"] = font_size
                    if "fill" in style and "content" in style["fill"] and "solid" in style["fill"]["content"]:
                        style["fill"]["content"]["solid"]["color"] = tc_rgba_content[:3]
                # CapCut parser is strict, dump with no spaces to mimic compact JSON
                text_mat["content"] = json.dumps(content_obj, ensure_ascii=False, separators=(',', ':'))
            except Exception:
                pass
        
    tracks.append(new_audio_track)
    
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
    
    total_duration = 0
    for track in draft.get("tracks", []):
        for seg in track.get("segments", []):
            end_time = seg.get("target_timerange", {}).get("start", 0) + seg.get("target_timerange", {}).get("duration", 0)
            if end_time > total_duration:
                total_duration = end_time
                
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
                seg_start = seg.get("target_timerange", {}).get("start", 0)
                if chunk_start <= seg_start < chunk_end:
                    seg["target_timerange"]["start"] = max(0, seg_start - chunk_start)
                    new_segments.append(seg)
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
        
        # Sync Video Checkbox
        self.chk_sync_video_var = ctk.BooleanVar(value=True)
        self.chk_sync_video = ctk.CTkCheckBox(self.tab_srt, text="Đồng bộ tốc độ video khớp với audio (Anti-Overlap)", variable=self.chk_sync_video_var, command=self.toggle_sync_opts)
        self.chk_sync_video.grid(row=4, column=0, columnspan=3, padx=10, pady=5, sticky="w")
        
        self.frame_sync_opts = ctk.CTkFrame(self.tab_srt)
        self.frame_sync_opts.grid(row=5, column=0, columnspan=3, padx=10, pady=5, sticky="ew")
        
        ctk.CTkLabel(self.frame_sync_opts, text="Video chậm tối đa:").grid(row=0, column=0, padx=5, pady=5)
        self.val_min_video = ctk.StringVar(value="0.85")
        ctk.CTkEntry(self.frame_sync_opts, textvariable=self.val_min_video, width=50).grid(row=0, column=1, padx=5, pady=5)
        
        ctk.CTkLabel(self.frame_sync_opts, text="Video nhanh tối đa:").grid(row=0, column=2, padx=5, pady=5)
        self.val_max_video = ctk.StringVar(value="1.15")
        ctk.CTkEntry(self.frame_sync_opts, textvariable=self.val_max_video, width=50).grid(row=0, column=3, padx=5, pady=5)
        
        ctk.CTkLabel(self.frame_sync_opts, text="Audio nhanh tối đa:").grid(row=0, column=4, padx=5, pady=5)
        self.val_max_audio = ctk.StringVar(value="1.15")
        ctk.CTkEntry(self.frame_sync_opts, textvariable=self.val_max_audio, width=50).grid(row=0, column=5, padx=5, pady=5)
        
        ctk.CTkLabel(self.frame_sync_opts, text="Nghỉ (s):").grid(row=1, column=0, padx=5, pady=5)
        self.val_rest_time = ctk.StringVar(value="10")
        ctk.CTkEntry(self.frame_sync_opts, textvariable=self.val_rest_time, width=50).grid(row=1, column=1, padx=5, pady=5)
        
        ctk.CTkLabel(self.frame_sync_opts, text="sau (block):").grid(row=1, column=2, padx=5, pady=5)
        self.val_rest_blocks = ctk.StringVar(value="300")
        ctk.CTkEntry(self.frame_sync_opts, textvariable=self.val_rest_blocks, width=50).grid(row=1, column=3, padx=5, pady=5)
        
        ctk.CTkLabel(self.frame_sync_opts, text="Đổi ID sau (block):").grid(row=1, column=4, padx=5, pady=5)
        self.val_id_blocks = ctk.StringVar(value="300")
        ctk.CTkEntry(self.frame_sync_opts, textvariable=self.val_id_blocks, width=50).grid(row=1, column=5, padx=5, pady=5)

        ctk.CTkButton(self.frame_sync_opts, text="Lưu cấu hình", width=80, command=self.save_sync_config).grid(row=0, column=6, rowspan=2, padx=5, pady=5, sticky="ns")
        ctk.CTkButton(self.frame_sync_opts, text="Reset về mặc định", width=80, fg_color="#b23b3b", hover_color="#8f2b2b", command=self.reset_sync_config).grid(row=0, column=7, rowspan=2, padx=5, pady=5, sticky="ns")
        
        self.load_sync_config()
        
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
        
        self.chk_merge_sync_video_var = ctk.BooleanVar(value=True)
        self.chk_merge_sync_video = ctk.CTkCheckBox(self.tab_merge, text="Đồng bộ tốc độ video khớp với audio (dùng cấu hình Tab 2)", variable=self.chk_merge_sync_video_var)
        self.chk_merge_sync_video.grid(row=5, column=0, columnspan=3, padx=10, pady=5, sticky="w")
        
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
        ctk.CTkEntry(self.frame_adv, textvariable=self.val_vid_vol, width=60).grid(row=0, column=1, padx=5, pady=5, sticky="w")
        
        ctk.CTkLabel(self.frame_adv, text="Âm lượng Audio (dB):").grid(row=0, column=2, padx=5, pady=5, sticky="e")
        self.val_aud_vol = ctk.StringVar(value="0")
        ctk.CTkEntry(self.frame_adv, textvariable=self.val_aud_vol, width=60).grid(row=0, column=3, padx=5, pady=5, sticky="w")
        
        # Subtitle Style group
        ctk.CTkLabel(self.frame_adv, text="Font size:").grid(row=1, column=0, padx=5, pady=5, sticky="e")
        self.val_font_size = ctk.StringVar(value="5.0")
        ctk.CTkEntry(self.frame_adv, textvariable=self.val_font_size, width=60).grid(row=1, column=1, padx=5, pady=5, sticky="w")
        
        ctk.CTkLabel(self.frame_adv, text="Màu chữ:").grid(row=1, column=2, padx=5, pady=5, sticky="e")
        self.val_text_color = ctk.StringVar(value="#FFFFFF")
        self.btn_text_color = ctk.CTkButton(self.frame_adv, textvariable=self.val_text_color, width=80, fg_color="#FFFFFF", text_color="black", command=lambda: self.pick_color(self.val_text_color, self.btn_text_color))
        self.btn_text_color.grid(row=1, column=3, padx=5, pady=5, sticky="w")
        
        ctk.CTkLabel(self.frame_adv, text="Màu viền:").grid(row=1, column=4, padx=5, pady=5, sticky="e")
        self.val_stroke_color = ctk.StringVar(value="#000000")
        self.btn_stroke_color = ctk.CTkButton(self.frame_adv, textvariable=self.val_stroke_color, width=80, fg_color="#000000", command=lambda: self.pick_color(self.val_stroke_color, self.btn_stroke_color))
        self.btn_stroke_color.grid(row=1, column=5, padx=5, pady=5, sticky="w")
        
        ctk.CTkLabel(self.frame_adv, text="Độ dày viền:").grid(row=1, column=6, padx=5, pady=5, sticky="e")
        self.val_stroke_width = ctk.StringVar(value="0.08")
        ctk.CTkEntry(self.frame_adv, textvariable=self.val_stroke_width, width=50).grid(row=1, column=7, padx=5, pady=5, sticky="w")
        
        # Preset checkbox and Save button
        self.chk_preset_cyan_var = ctk.BooleanVar(value=False)
        self.chk_preset_cyan = ctk.CTkCheckBox(self.frame_adv, text="Dùng Preset Nền Đen Viền Xanh (CapCut)", variable=self.chk_preset_cyan_var)
        self.chk_preset_cyan.grid(row=2, column=0, columnspan=5, padx=10, pady=(5, 10), sticky="w")
        
        ctk.CTkButton(self.frame_adv, text="Lưu cấu hình", width=120, command=self.save_sync_config).grid(row=2, column=6, columnspan=2, padx=10, pady=(5, 10), sticky="e")

        # 4. Bottom Status
        self.frame_bottom = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_bottom.grid(row=3, column=0, pady=(0, 10), sticky="ew")
        self.frame_bottom.grid_columnconfigure(0, weight=1)
        self.frame_bottom.grid_columnconfigure(1, weight=0)

        self.label_status = ctk.CTkLabel(self.frame_bottom, text="Sẵn sàng.", text_color="gray")
        self.label_status.grid(row=0, column=0, padx=20, sticky="w")
        
        self.btn_reset_id = ctk.CTkButton(self.frame_bottom, text="Đổi Device ID (Gỡ Ban)", width=150, fg_color="#b23b3b", hover_color="#8f2b2b", command=self.reset_device_id)
        self.btn_reset_id.grid(row=0, column=1, padx=20, sticky="e")

        # Load voices
        threading.Thread(target=self.load_voices, daemon=True).start()

    def toggle_sync_opts(self):
        if self.chk_sync_video_var.get():
            self.frame_sync_opts.grid()
        else:
            self.frame_sync_opts.grid_remove()

    def toggle_adv_settings(self):
        if self.frame_adv.winfo_ismapped():
            self.frame_adv.grid_remove()
            self.btn_toggle_adv.configure(text="[+] Hiển thị tuỳ chỉnh nâng cao")
        else:
            self.frame_adv.grid(row=3, column=0, pady=(0, 10), sticky="ew")
            self.frame_bottom.grid(row=4, column=0, pady=(0, 10), sticky="ew")
            self.btn_toggle_adv.configure(text="[-] Ẩn tuỳ chỉnh nâng cao")
            
    def pick_color(self, string_var, button):
        color_code = colorchooser.askcolor(title="Chọn màu")[1]
        if color_code:
            string_var.set(color_code)
            button.configure(fg_color=color_code)
            # If color is dark, set text to white, else black
            r = int(color_code[1:3], 16)
            g = int(color_code[3:5], 16)
            b = int(color_code[5:7], 16)
            brightness = (r * 299 + g * 587 + b * 114) / 1000
            button.configure(text_color="black" if brightness > 128 else "white")

    def get_adv_settings(self):
        try: vid_vol = float(self.val_vid_vol.get()) if self.val_vid_vol.get() else 0.0
        except: vid_vol = 0.0
        try: aud_vol = float(self.val_aud_vol.get()) if self.val_aud_vol.get() else 0.0
        except: aud_vol = 0.0
        try: font_size = float(self.val_font_size.get()) if self.val_font_size.get() else 5.0
        except: font_size = 5.0
        try: stroke_width = float(self.val_stroke_width.get()) if self.val_stroke_width.get() else 0.08
        except: stroke_width = 0.08
        
        return {
            "vid_vol": vid_vol,
            "aud_vol": aud_vol,
            "font_size": font_size,
            "text_color": self.val_text_color.get(),
            "stroke_color": self.val_stroke_color.get(),
            "stroke_width": stroke_width,
            "use_preset_cyan": self.chk_preset_cyan_var.get()
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
                    
                    if "adv_vid_vol" in config: self.val_vid_vol.set(config["adv_vid_vol"])
                    if "adv_aud_vol" in config: self.val_aud_vol.set(config["adv_aud_vol"])
                    if "adv_font_size" in config: self.val_font_size.set(config["adv_font_size"])
                    if "adv_text_color" in config: 
                        self.val_text_color.set(config["adv_text_color"])
                        self.btn_text_color.configure(fg_color=config["adv_text_color"])
                    if "adv_stroke_color" in config: 
                        self.val_stroke_color.set(config["adv_stroke_color"])
                        self.btn_stroke_color.configure(fg_color=config["adv_stroke_color"])
                    if "adv_stroke_width" in config: self.val_stroke_width.set(config["adv_stroke_width"])
                    if "adv_use_preset_cyan" in config: self.chk_preset_cyan_var.set(config["adv_use_preset_cyan"])
                    
                    if "val_threads" in config:
                        self.slider_threads.set(config["val_threads"])
                        self.label_threads_val.configure(text=f"{config['val_threads']}")
        except Exception as e:
            print(f"Error loading config: {e}")

    def save_sync_config(self):
        try:
            config = {
                "val_min_video": self.val_min_video.get(),
                "val_max_video": self.val_max_video.get(),
                "val_max_audio": self.val_max_audio.get(),
                "val_rest_time": self.val_rest_time.get(),
                "val_rest_blocks": self.val_rest_blocks.get(),
                "val_id_blocks": self.val_id_blocks.get(),
                "adv_vid_vol": self.val_vid_vol.get(),
                "adv_aud_vol": self.val_aud_vol.get(),
                "adv_font_size": self.val_font_size.get(),
                "adv_text_color": self.val_text_color.get(),
                "adv_stroke_color": self.val_stroke_color.get(),
                "adv_stroke_width": self.val_stroke_width.get(),
                "adv_use_preset_cyan": self.chk_preset_cyan_var.get()
            }
            with open("app_config.json", "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
            messagebox.showinfo("Thành công", "Đã lưu cấu hình!")
        except Exception as e:
            messagebox.showerror("Lỗi", f"Không thể lưu cấu hình: {e}")

    def reset_sync_config(self):
        self.val_min_video.set("0.85")
        self.val_max_video.set("1.15")
        self.val_max_audio.set("1.15")
        self.val_rest_time.set("10")
        self.val_rest_blocks.set("300")
        self.val_id_blocks.set("300")

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
            voice_names = [f"{v.display_name} ({v.voice_type})" for v in voices]
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
            result = self.client.generate_speech(texts=text, voice=voice_type, rate=rate, wait=True)
            self.download_audio_from_api(result, save_path)
            self.after(0, lambda: self.label_status.configure(text=f"Hoàn tất! Đã lưu tại: {save_path}", text_color="green"))
            self.after(0, lambda: messagebox.showinfo("Thành công", f"Đã lưu file âm thanh thành công tại:\n{save_path}"))
        except Exception as e:
            self.after(0, lambda e=e: self.label_status.configure(text=f"Lỗi: {e}", text_color="red"))
            self.after(0, lambda e=e: messagebox.showerror("Lỗi", f"Có lỗi xảy ra:\n{e}"))
        finally:
            self.after(0, lambda: self.btn_generate_basic.configure(state="normal", text="Tạo Giọng Nói (TTS) và Lưu..."))

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
        sync_video = self.chk_sync_video_var.get()
        num_threads = int(self.slider_threads.get())
        
        min_vid = 0.85
        max_vid = 1.15
        max_aud = 1.15
        try:
            val_rest_time = int(self.val_rest_time.get())
            val_rest_blocks = int(self.val_rest_blocks.get())
            val_id_blocks = int(self.val_id_blocks.get())
        except ValueError:
            messagebox.showwarning("Lỗi", "Vui lòng nhập số nguyên hợp lệ cho cấu hình chống ban.")
            return

        if sync_video:
            try:
                min_vid = float(self.val_min_video.get())
                max_vid = float(self.val_max_video.get())
                max_aud = float(self.val_max_audio.get())
            except ValueError:
                messagebox.showwarning("Lỗi", "Vui lòng nhập số hợp lệ cho các ô cấu hình Anti-Overlap (ví dụ: 0.85, 1.15).")
                return

        adv_settings = self.get_adv_settings()
        self.btn_generate_srt.configure(state="disabled", text="Đang xử lý...")
        threading.Thread(target=self.generate_srt_thread, args=(srt_file, json_file, voice_type, rate, sync_video, min_vid, max_vid, max_aud, num_threads, val_rest_time, val_rest_blocks, val_id_blocks, adv_settings), daemon=True).start()
        
    def generate_srt_thread(self, srt_file, json_file, voice_type, rate, sync_video, min_vid_spd, max_vid_spd, max_aud_spd, num_threads, val_rest_time, val_rest_blocks, val_id_blocks, adv_settings):
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
                text = sub.text.replace("\n", " ").strip()
                if not text:
                    return None
                    
                start_micros = sub.start.ordinal * 1000
                end_micros = sub.end.ordinal * 1000
                original_duration_micros = end_micros - start_micros
                
                save_path = os.path.join(audio_dir, f"audio_{i+1:04d}.mp3")
                
                local_client = CapCutClient(device=self.client.device)
                
                def safe_generate_speech(t_text, t_voice, t_rate):
                    for attempt in range(6):
                        try:
                            return local_client.generate_speech(texts=t_text, voice=t_voice, rate=t_rate, wait=True)
                        except Exception as e:
                            if attempt == 5:
                                raise e
                            self.after(0, lambda a=attempt: self.label_status.configure(text=f"API chặn. Nghỉ {5*(a+1)}s và thử lại ({a+1}/5)...", text_color="orange"))
                            time.sleep(5 * (attempt + 1))
                            with lock:
                                self.client.device.randomize()

                result = safe_generate_speech(text, voice_type, rate)
                
                self.download_audio_from_api(result, save_path)
                
                audio = MP3(save_path)
                audio_duration_micros = int(audio.info.length * 1000000)
                
                video_speed = 1.0
                report_item = None
                
                if sync_video:
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
                        result = safe_generate_speech(text, voice_type, new_rate)
                        self.download_audio_from_api(result, save_path)
                        
                        audio = MP3(save_path)
                        audio_duration_micros = int(audio.info.length * 1000000)
                        video_speed = original_duration_micros / audio_duration_micros if audio_duration_micros > 0 else 1.0
                        if video_speed < min_vid_spd:
                            video_speed = min_vid_spd
                            
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
                        time.sleep(val_rest_time)
                    elif is_rest:
                        self.after(0, lambda c=completed: self.label_status.configure(text=f"Đã tạo {c} câu. Nghỉ {val_rest_time}s chống block..."))
                        time.sleep(val_rest_time)
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
                    res = future.result() # Will raise exception if thread failed
                    if res is not None:
                        audio_info_list.append(res)
                        
            # Sort by original index to keep chronological order
            audio_info_list.sort(key=lambda x: x["index"])

            reports = [info["report_item"] for info in audio_info_list if info.get("report_item")]
            if reports:
                report_path = os.path.join(proj_dir, "overlap_report.json")
                with open(report_path, "w", encoding="utf-8") as f:
                    json.dump(reports, f, ensure_ascii=False, indent=2)

            # All audios generated, modify CapCut JSON
            if sync_video:
                self.after(0, lambda: self.label_status.configure(text="Đang phân tích timeline và chia nhỏ video..."))
            else:
                self.after(0, lambda: self.label_status.configure(text="Đang chèn âm thanh vào dự án CapCut..."))
                
            modify_capcut_project(json_file, audio_info_list, sync_video, adv_settings)
            
            self.after(0, lambda: self.label_status.configure(text="Hoàn tất!", text_color="green"))
            
            if sync_video:
                msg = f"Đã chia nhỏ video và chèn {len(audio_info_list)} âm thanh thành công!\nVui lòng tải lại dự án trên CapCut."
            else:
                msg = f"Đã chèn {len(audio_info_list)} âm thanh thành công!\nVui lòng tải lại dự án trên CapCut."
                
            if reports:
                msg += f"\n\nLưu ý: Có {len(reports)} câu dịch quá dài không thể ép vừa khớp tốc độ. Đã lưu báo cáo tại overlap_report.json"
            self.after(0, lambda m=msg: messagebox.showinfo("Thành công", m))
            
        except Exception as e:
            self.after(0, lambda e=e: self.label_status.configure(text=f"Lỗi: {e}", text_color="red"))
            self.after(0, lambda e=e: messagebox.showerror("Lỗi", f"Có lỗi xảy ra:\n{e}"))
        finally:
            self.after(0, lambda: self.btn_generate_srt.configure(state="normal", text="Bắt đầu xử lý SRT"))

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
            
        sync_video = self.chk_merge_sync_video_var.get()
        min_vid = 0.85
        max_vid = 1.15
        
        if sync_video:
            try:
                min_vid = float(self.val_min_video.get())
                max_vid = float(self.val_max_video.get())
            except ValueError:
                messagebox.showwarning("Lỗi", "Vui lòng nhập số hợp lệ cho cấu hình Anti-Overlap ở Tab 2.")
                return

        adv_settings = self.get_adv_settings()
        self.btn_generate_merge.configure(state="disabled", text="Đang ghép...")
        threading.Thread(target=self.generate_merge_thread, args=(srt_file, json_file, audio_dir, sync_video, min_vid, max_vid, adv_settings), daemon=True).start()
    def generate_merge_thread(self, srt_file, json_file, audio_dir, sync_video, min_vid_spd, max_vid_spd, adv_settings):
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
                    sub = subs[idx]
                    text = sub.text.replace("\n", " ").strip()
                    if not text:
                        return True
                    audio_path = os.path.join(audio_dir, f"audio_{idx+1:04d}.mp3")
                    
                    def safe_generate_speech(t_text, t_voice, t_rate):
                        for attempt in range(6):
                            try:
                                return self.client.generate_speech(texts=t_text, voice=t_voice, rate=t_rate, wait=True)
                            except Exception as e:
                                if attempt == 5:
                                    return None
                                self.after(0, lambda a=attempt: self.label_status.configure(text=f"API chặn khi tải bù. Nghỉ {5*(a+1)}s và thử lại...", text_color="orange"))
                                time.sleep(5 * (attempt + 1))
                                with lock:
                                    self.client.device.randomize()
                                
                    result = safe_generate_speech(text, voice_type, rate)
                    if result:
                        try:
                            self.download_audio_from_api(result, audio_path)
                            return True
                        except Exception:
                            return False
                    return False

                with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
                    futures = {executor.submit(download_missing, idx): idx for idx in missing_indices}
                    for future in concurrent.futures.as_completed(futures):
                        idx = futures[future]
                        success = future.result()
                        if success:
                            with lock:
                                for j in range(len(missing_subs)):
                                    if missing_subs[j].index == subs[idx].index:
                                        del missing_subs[j]
                                        break
                                missing_subs.save(missing_srt_path, encoding='utf-8')
                
                # Phase 3: Check again
                missing_indices = []
                for i, sub in enumerate(subs):
                    audio_path = os.path.join(audio_dir, f"audio_{i+1:04d}.mp3")
                    if not os.path.exists(audio_path):
                        missing_indices.append(i)
                
                if not missing_indices:
                    if os.path.exists(missing_srt_path):
                        os.remove(missing_srt_path)
                else:
                    ans = [None]
                    event = threading.Event()
                    def ask():
                        ans[0] = messagebox.askyesno(
                            "Tải bù thất bại",
                            f"Có {len(missing_indices)} câu không thể tạo giọng đọc (API lỗi/từ chối). Danh sách lưu tại:\n{missing_srt_path}\n\nBạn có muốn BỎ QUA các câu này (không ghép tiếng đoạn đó) và tiếp tục ghép vào CapCut không?\n\nChọn Yes để bỏ qua và tiếp tục.\nChọn No để DỪNG LẠI và tự xử lý."
                        )
                        event.set()
                    self.after(0, ask)
                    event.wait()
                    if not ans[0]:
                        self.after(0, lambda: self.label_status.configure(text="Đã dừng ghép theo yêu cầu người dùng.", text_color="red"))
                        return

            audio_info_list = []
            self.after(0, lambda: self.label_status.configure(text="Đang phân tích và xử lý logic chèn..."))
            
            for i, sub in enumerate(subs):
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
                    audio = MP3(audio_path)
                    audio_duration_micros = int(audio.info.length * 1000000)
                    if sync_video:
                        video_speed = original_duration_micros / audio_duration_micros if audio_duration_micros > 0 else 1.0
                        if video_speed < min_vid_spd:
                            video_speed = min_vid_spd
                        elif video_speed > max_vid_spd:
                            video_speed = max_vid_spd
                        
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

            if sync_video:
                self.after(0, lambda: self.label_status.configure(text="Đang phân tích timeline và chia nhỏ video..."))
            else:
                self.after(0, lambda: self.label_status.configure(text="Đang chèn âm thanh vào dự án CapCut..."))
                
            modify_capcut_project(json_file, audio_info_list, sync_video, adv_settings)
            
            self.after(0, lambda: self.label_status.configure(text="Hoàn tất!", text_color="green"))
            msg = f"Đã chèn âm thanh thành công!\nVui lòng tải lại dự án trên CapCut."
            self.after(0, lambda m=msg: messagebox.showinfo("Thành công", m))
            
        except Exception as e:
            self.after(0, lambda e=e: self.label_status.configure(text=f"Lỗi: {e}", text_color="red"))
            self.after(0, lambda e=e: messagebox.showerror("Lỗi", f"Có lỗi xảy ra:\n{e}"))
        finally:
            self.after(0, lambda: self.btn_generate_merge.configure(state="normal", text="Bắt đầu ghép vào CapCut"))

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

if __name__ == "__main__":
    app = CapCutTTSApp()
    app.mainloop()
