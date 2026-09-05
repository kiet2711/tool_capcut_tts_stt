import customtkinter as ctk
from tkinter import messagebox, filedialog, colorchooser
import threading
import sys
import os
import json
import uuid
import shutil
import base64
import re
import math
import requests
import time
import pysrt
from mutagen.mp3 import MP3
import asyncio
import edge_tts
from PIL import Image, ImageTk
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from capcut_tts_api import CapCutClient, CapCutError
from capcut_tts_api.vocal_api import (
    CapCutVocalSeparator,
    save_pro_cookie,
    load_pro_cookie,
    verify_pro_cookie,
)
from capcut_tts_api.translator import (
    GeminiTranslator, SrtItem, parse_srt, build_srt, build_ass, is_srt_content,
    MODEL_MAP, STYLE_PRESETS, CONCURRENCY_OPTIONS, parse_concurrency_val,
    count_units, get_chunk_config, chunk_srt_items, chunk_raw_text
)
from error_review_dialog import TTSErrorReviewDialog
from api_key_manager_dialog import ApiKeyManagerDialog

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

def modify_capcut_project(draft_json_path, audio_info_list, sync_mode="Khớp từng câu (Anti-Overlap)", adv_settings=None, fixed_vid_speed=1.0, fixed_aud_min_speed=0.8, fixed_aud_max_speed=1.5, enable_multi_segment=False):
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
    
    fixed_speed_modes = ["Đổi tốc độ toàn bộ (Fixed Speed)", "Đổi tốc độ toàn bộ (dùng cấu hình Tab 2)"]
    anti_overlap_modes = ["Khớp từng câu (Anti-Overlap)", "Khớp từng câu (dùng cấu hình Tab 2)"]

    # Both sync modes split source video according to SRT time.
    # A negative gap means that two SRT blocks overlap (or are out of order).
    if sync_mode in anti_overlap_modes + fixed_speed_modes:
        timeline_issues = []
        previous = None
        for info in sorted(audio_info_list, key=lambda item: item.get("index", 0)):
            start = int(info.get("start", 0))
            end = int(info.get("end", 0))
            index = int(info.get("index", 0)) + 1
            if end <= start:
                timeline_issues.append(f"Câu {index}: timecode kết thúc không lớn hơn bắt đầu")
            if previous and start < previous["end"]:
                overlap_ms = (previous["end"] - start) / 1000.0
                timeline_issues.append(
                    f"Câu {index} chồng câu {previous['index']} {overlap_ms:.0f} ms"
                )
            previous = {"index": index, "end": end}

        if timeline_issues:
            preview = "\n".join(timeline_issues[:12])
            more = len(timeline_issues) - min(len(timeline_issues), 12)
            if more:
                preview += f"\n... và {more} lỗi timecode khác"
            raise Exception(
                "Không thể đồng bộ an toàn vì SRT có timecode chồng nhau/không hợp lệ.\n"
                "Nếu tiếp tục, hình có thể bị cắt lệch so với giọng. Hãy sửa SRT rồi chạy lại:\n"
                f"{preview}"
            )

    text_track = None
    max_segments = 0
    for track in tracks:
        if track.get("type") == "text":
            seg_len = len(track.get("segments", []))
            if seg_len > max_segments:
                text_track = track
                max_segments = seg_len
                
    if text_track:
        text_track["segments"].sort(key=lambda s: s.get("target_timerange", {}).get("start", 0))
        for t_seg in text_track["segments"]:
            t_seg["_orig_start"] = t_seg.get("target_timerange", {}).get("start", 0)
        used_text_segment_ids = set()

    import copy

    if sync_mode in anti_overlap_modes + fixed_speed_modes:
        if enable_multi_segment:
            video_track = None
            for track in tracks:
                if track.get("type") == "video" and len(track.get("segments", [])) > 0:
                    video_track = track
                    break
            if not video_track or len(video_track.get("segments", [])) == 0:
                raise Exception("Không tìm thấy Video track hợp lệ trong project.")

            orig_segments = sorted(
                video_track["segments"],
                key=lambda s: s.get("target_timerange", {}).get("start", 0)
            )
            timeline_video_end = max(
                s.get("target_timerange", {}).get("start", 0) + s.get("target_timerange", {}).get("duration", 0)
                for s in orig_segments
            ) if orig_segments else 0

            new_video_segments = []
            current_target_time = 0
            current_orig_time = 0
            existing_speed_ids = {
                item.get("id") for item in materials["speeds"] if item.get("id")
            }

            def slice_timeline_range(range_start, range_end, target_duration):
                nonlocal current_target_time
                if range_end <= range_start or target_duration <= 0:
                    return

                range_dur = range_end - range_start
                curr_pos = range_start
                slices_to_add = []

                for seg in orig_segments:
                    t_start = seg.get("target_timerange", {}).get("start", 0)
                    t_dur = seg.get("target_timerange", {}).get("duration", 0)
                    t_end = t_start + t_dur
                    if t_end <= range_start or t_start >= range_end:
                        continue

                    if t_start > curr_pos:
                        gap_len = min(t_start, range_end) - curr_pos
                        if gap_len > 0:
                            slices_to_add.append({"type": "gap", "orig_dur": gap_len})
                        curr_pos = min(t_start, range_end)
                        if curr_pos >= range_end:
                            break

                    overlap_start = max(curr_pos, t_start)
                    overlap_end = min(range_end, t_end)
                    overlap_dur = overlap_end - overlap_start
                    if overlap_dur > 0:
                        slices_to_add.append({
                            "type": "seg",
                            "seg": seg,
                            "overlap_start": overlap_start,
                            "overlap_dur": overlap_dur,
                            "t_start": t_start,
                            "t_dur": t_dur,
                            "orig_dur": overlap_dur
                        })
                        curr_pos = overlap_end

                if curr_pos < range_end:
                    gap_len = range_end - curr_pos
                    slices_to_add.append({"type": "gap", "orig_dur": gap_len})

                accumulated_target_dur = 0
                num_slices = len(slices_to_add)

                for idx, item in enumerate(slices_to_add):
                    if idx == num_slices - 1:
                        item_target_dur = target_duration - accumulated_target_dur
                    else:
                        item_target_dur = int(round(target_duration * (item["orig_dur"] / range_dur)))
                    accumulated_target_dur += item_target_dur

                    if item_target_dur <= 0:
                        item_target_dur = 1

                    if item["type"] == "gap":
                        current_target_time += item_target_dur
                        continue

                    seg = item["seg"]
                    t_start = item["t_start"]
                    t_dur = item["t_dur"]
                    overlap_start = item["overlap_start"]
                    overlap_dur = item["overlap_dur"]

                    seg_speed = seg.get("speed", 1.0)
                    if seg_speed is None or seg_speed <= 0:
                        seg_speed = seg["source_timerange"]["duration"] / t_dur if t_dur > 0 else 1.0

                    is_reverse = seg.get("reverse", False)
                    offset_from_seg_start = overlap_start - t_start
                    src_start_orig = seg["source_timerange"]["start"]

                    if not is_reverse:
                        new_src_start = src_start_orig + int(offset_from_seg_start * seg_speed)
                        new_src_dur = int(overlap_dur * seg_speed)
                    else:
                        orig_src_dur = seg["source_timerange"]["duration"]
                        new_src_start = src_start_orig + orig_src_dur - int((offset_from_seg_start + overlap_dur) * seg_speed)
                        new_src_dur = int(overlap_dur * seg_speed)

                    if new_src_dur <= 0:
                        new_src_dur = 1

                    new_seg_speed = new_src_dur / item_target_dur if item_target_dur > 0 else seg_speed

                    seg_clone = copy.deepcopy(seg)
                    seg_clone["id"] = str(uuid.uuid4()).upper()
                    seg_clone["source_timerange"] = {"start": new_src_start, "duration": new_src_dur}
                    seg_clone["target_timerange"] = {"start": current_target_time, "duration": item_target_dur}

                    preserved_refs = [
                        ref for ref in seg.get("extra_material_refs", [])
                        if ref not in existing_speed_ids
                    ]
                    speed_id = str(uuid.uuid4()).upper()
                    materials["speeds"].append({
                        "id": speed_id, "type": "speed", "mode": 0, "speed": new_seg_speed, "curve_speed": None
                    })
                    existing_speed_ids.add(speed_id)

                    seg_clone["extra_material_refs"] = preserved_refs + [speed_id]
                    seg_clone["speed"] = new_seg_speed

                    new_video_segments.append(seg_clone)
                    current_target_time += item_target_dur

        else:
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
            existing_speed_ids = {
                item.get("id") for item in materials["speeds"] if item.get("id")
            }
            preserved_video_material_refs = [
                ref for ref in video_segment.get("extra_material_refs", [])
                if ref not in existing_speed_ids
            ]
            
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
                
                seg_clone["extra_material_refs"] = preserved_video_material_refs + [speed_id]
                seg_clone["speed"] = speed
                
                current_target_time += target_duration
                current_source_time += duration
                return seg_clone

            fixed_pending_start = None
            fixed_pending_duration = 0

            def add_fixed_normal(source_start, duration):
                nonlocal fixed_pending_start, fixed_pending_duration
                if duration <= 0:
                    return
                if fixed_pending_start is None:
                    fixed_pending_start = source_start
                fixed_pending_duration += duration

            def flush_fixed_normal():
                nonlocal fixed_pending_start, fixed_pending_duration
                if fixed_pending_duration > 0:
                    chunk = create_video_chunk(fixed_pending_start, fixed_pending_duration, 1.0)
                    if chunk:
                        new_video_segments.append(chunk)
                fixed_pending_start = None
                fixed_pending_duration = 0

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
        sync_target_duration = srt_end - srt_start

        if enable_multi_segment and sync_mode in anti_overlap_modes + fixed_speed_modes:
            block_source_duration = srt_end - srt_start
            gap_duration = srt_start - current_orig_time
            if gap_duration > 0:
                slice_timeline_range(current_orig_time, srt_start, gap_duration)
                current_orig_time = srt_start

            block_target_start = current_target_time

            if sync_mode in fixed_speed_modes:
                requested_audio_speed = audio_duration_micros / block_source_duration if block_source_duration > 0 else 1.0
                if requested_audio_speed > fixed_aud_max_speed:
                    audio_speed_val = fixed_aud_max_speed
                    audio_target_duration = int(audio_duration_micros / audio_speed_val)
                    slice_timeline_range(srt_start, srt_end, audio_target_duration)
                    sync_target_duration = audio_target_duration
                else:
                    if requested_audio_speed > 1.0:
                        audio_speed_val = max(fixed_aud_min_speed, requested_audio_speed)
                    audio_target_duration = int(audio_duration_micros / audio_speed_val)
                    slice_timeline_range(srt_start, srt_end, block_source_duration)
                    sync_target_duration = block_source_duration
            else:
                target_sentence_dur = int(block_source_duration / video_speed) if video_speed > 0 else block_source_duration
                slice_timeline_range(srt_start, srt_end, target_sentence_dur)
                audio_target_duration = audio_duration_micros
                sync_target_duration = audio_duration_micros

            current_orig_time = srt_end

        else:
            if sync_mode in fixed_speed_modes:
                block_source_duration = srt_end - srt_start
                source_at_block = current_source_time + fixed_pending_duration
                gap_duration = srt_start - source_at_block
                if gap_duration < 0:
                    raise Exception(
                        f"Timeline SRT không thể map an toàn ở câu {info.get('index', i) + 1}: "
                        f"con trỏ video vượt mốc SRT {-gap_duration / 1000.0:.0f} ms."
                    )
                if gap_duration > 0:
                    add_fixed_normal(source_at_block, gap_duration)
                    source_at_block += gap_duration

                requested_audio_speed = audio_duration_micros / block_source_duration if block_source_duration > 0 else 1.0
                if requested_audio_speed > fixed_aud_max_speed:
                    audio_speed_val = fixed_aud_max_speed
                    audio_target_duration = int(audio_duration_micros / audio_speed_val)
                    flush_fixed_normal()
                    block_target_start = current_target_time
                    video_speed = block_source_duration / audio_target_duration if audio_target_duration > 0 else 1.0
                    chunk = create_video_chunk(current_source_time, block_source_duration, video_speed)
                    if chunk:
                        new_video_segments.append(chunk)
                    sync_target_duration = audio_target_duration
                else:
                    if requested_audio_speed > 1.0:
                        audio_speed_val = max(fixed_aud_min_speed, requested_audio_speed)
                    audio_target_duration = int(audio_duration_micros / audio_speed_val)
                    block_target_start = current_target_time + fixed_pending_duration
                    add_fixed_normal(source_at_block, block_source_duration)
            else:
                block_target_start = srt_start
                audio_target_duration = audio_duration_micros
                
            if sync_mode in anti_overlap_modes:
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
            
        if text_track and sync_mode in anti_overlap_modes + fixed_speed_modes:
            srt_start = info["start"]
            text_seg = None
            min_diff = float('inf')
            
            for t_seg in text_track["segments"]:
                if t_seg.get("id") in used_text_segment_ids:
                    continue
                diff = abs(t_seg.get("_orig_start", 0) - srt_start)
                if diff < min_diff:
                    min_diff = diff
                    text_seg = t_seg
                    
            if min_diff > 100000: # 100ms tolerance for frame snapping
                text_seg = None
                
            if text_seg:
                used_text_segment_ids.add(text_seg.get("id"))
                if text_seg.get("target_timerange") is not None:
                    text_seg["target_timerange"]["start"] = block_target_start
                    text_seg["target_timerange"]["duration"] = audio_duration_micros if sync_mode in anti_overlap_modes else sync_target_duration
                if text_seg.get("source_timerange") is not None:
                    text_seg["source_timerange"]["duration"] = audio_duration_micros if sync_mode in anti_overlap_modes else sync_target_duration
                
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
        
    if sync_mode in anti_overlap_modes + fixed_speed_modes:
        if enable_multi_segment:
            if current_orig_time < timeline_video_end:
                remainder_dur = timeline_video_end - current_orig_time
                slice_timeline_range(current_orig_time, timeline_video_end, remainder_dur)
            video_track["segments"] = new_video_segments
        else:
            if sync_mode in fixed_speed_modes:
                flush_fixed_normal()
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
        
        self.combo_voice = ctk.CTkComboBox(self.frame_top, values=["Đang tải..."], width=220, command=self.on_voice_changed)
        self.combo_voice.grid(row=0, column=1, padx=5, pady=10, sticky="ew")
        
        self.label_rate = ctk.CTkLabel(self.frame_top, text="Tốc độ:", font=ctk.CTkFont(weight="bold"))
        self.label_rate.grid(row=0, column=2, padx=10, pady=10, sticky="w")
        
        self.slider_rate = ctk.CTkSlider(self.frame_top, from_=0.5, to=2.0, number_of_steps=15, command=self.update_rate_label, width=120)
        self.slider_rate.set(1.0)
        self.slider_rate.grid(row=0, column=3, padx=5, pady=10, sticky="ew")

        self.label_rate_val = ctk.CTkLabel(self.frame_top, text="1.0")
        self.label_rate_val.grid(row=0, column=4, padx=5, pady=10, sticky="w")

        # 2. Tabs for Modes
        self.tabview = ctk.CTkTabview(self)
        self.tabview.grid(row=1, column=0, padx=20, pady=10, sticky="nsew")
        self.tab_basic = self.tabview.add("Tạo TTS Cơ Bản")
        self.tab_srt = self.tabview.add("Chèn SRT vào CapCut")
        self.tab_split = self.tabview.add("Chia Nhỏ Project")
        self.tab_stt = self.tabview.add("Nhận diện (STT)")
        self.tab_trans = self.tabview.add("Dịch Thuật (AI)")
        self.tab_vocal = self.tabview.add("Tách Giọng Nói (AI)")
        
        # -- Tab 1: Basic TTS --
        self.tab_basic.grid_columnconfigure(0, weight=1)
        self.tab_basic.grid_rowconfigure(0, weight=1)
        self.text_input = ctk.CTkTextbox(self.tab_basic, font=ctk.CTkFont(size=14))
        self.text_input.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        self.text_input.insert("1.0", "Xin chào! Bạn có thể nhập nội dung văn bản vào đây để tôi đọc cho bạn nghe nhé.")
        
        self.frame_basic_bottom = ctk.CTkFrame(self.tab_basic, fg_color="transparent")
        self.frame_basic_bottom.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="ew")
        self.frame_basic_bottom.grid_columnconfigure(0, weight=1)
        
        self.frame_threads_basic = ctk.CTkFrame(self.frame_basic_bottom, fg_color="transparent")
        self.frame_threads_basic.grid(row=0, column=0, sticky="w", pady=(0, 5))
        ctk.CTkLabel(self.frame_threads_basic, text="Số luồng tạo TTS (1-100):", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, padx=(0, 5), sticky="w")
        self.slider_threads_basic = ctk.CTkSlider(self.frame_threads_basic, from_=1, to=100, number_of_steps=99, command=self.update_threads_basic_label, width=130)
        self.slider_threads_basic.set(20)
        self.slider_threads_basic.grid(row=0, column=1, padx=5, sticky="w")
        self.label_threads_basic_val = ctk.CTkLabel(self.frame_threads_basic, text="20", font=ctk.CTkFont(weight="bold"))
        self.label_threads_basic_val.grid(row=0, column=2, padx=5, sticky="w")
        
        self.btn_generate_basic = ctk.CTkButton(self.frame_basic_bottom, text="Tạo Giọng Nói (TTS) và Lưu...", font=ctk.CTkFont(size=16, weight="bold"), height=45, command=self.on_generate_basic)
        self.btn_generate_basic.grid(row=1, column=0, sticky="ew")
        
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
        
        # Sync Video Options & Threads
        self.frame_srt_row4 = ctk.CTkFrame(self.tab_srt, fg_color="transparent")
        self.frame_srt_row4.grid(row=4, column=0, columnspan=3, padx=10, pady=5, sticky="ew")
        
        ctk.CTkLabel(self.frame_srt_row4, text="Chế độ đồng bộ:").grid(row=0, column=0, padx=(0, 5), sticky="w")
        self.combo_sync_mode_var = ctk.StringVar(value="Khớp từng câu (Anti-Overlap)")
        self.combo_sync_mode_var.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        self.combo_sync_mode = ctk.CTkComboBox(self.frame_srt_row4, values=["Không đồng bộ", "Khớp từng câu (Anti-Overlap)", "Đổi tốc độ toàn bộ (Fixed Speed)"], variable=self.combo_sync_mode_var, command=self.toggle_sync_opts, width=240)
        self.combo_sync_mode.grid(row=0, column=1, padx=(0, 6), sticky="w")
        ctk.CTkButton(self.frame_srt_row4, text="? Hướng dẫn", width=95, command=self.show_sync_mode_guide).grid(row=0, column=2, padx=(0, 14), sticky="w")
        
        ctk.CTkLabel(self.frame_srt_row4, text="Số luồng (1-100):", font=ctk.CTkFont(weight="bold")).grid(row=0, column=3, padx=(0, 5), sticky="w")
        self.slider_threads_srt = ctk.CTkSlider(self.frame_srt_row4, from_=1, to=100, number_of_steps=99, command=self.update_threads_srt_label, width=120)
        self.slider_threads_srt.set(50)
        self.slider_threads_srt.grid(row=0, column=4, padx=5, sticky="w")
        self.label_threads_srt_val = ctk.CTkLabel(self.frame_srt_row4, text="50", font=ctk.CTkFont(weight="bold"))
        self.label_threads_srt_val.grid(row=0, column=5, padx=5, sticky="w")
        
        self.frame_sync_opts = ctk.CTkFrame(self.tab_srt)
        self.frame_sync_opts.grid(row=5, column=0, columnspan=3, padx=10, pady=5, sticky="ew")
        
        self.frame_fixed_speed_opts = ctk.CTkFrame(self.tab_srt)
        # Not gridded by default
        
        # Kept only for loading older app_config.json files.  Fixed Speed no
        # longer applies one speed to the complete video timeline.
        self.val_fixed_vid_speed = ctk.StringVar(value="1.0")

        self.val_enable_multi_segment = ctk.BooleanVar(value=False)
        self.val_enable_multi_segment.trace_add("write", lambda *args: self.save_sync_config(silent=True))

        ctk.CTkLabel(self.frame_fixed_speed_opts, text="Audio chậm nhất (x):").grid(row=0, column=0, padx=5, pady=5)
        self.val_fixed_aud_min_speed = ctk.StringVar(value="0.8")
        self.val_fixed_aud_min_speed.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_fixed_speed_opts, textvariable=self.val_fixed_aud_min_speed, width=50).grid(row=0, column=1, padx=5, pady=5)

        ctk.CTkLabel(self.frame_fixed_speed_opts, text="Audio nhanh nhất (x):").grid(row=0, column=2, padx=5, pady=5)
        self.val_fixed_aud_max_speed = ctk.StringVar(value="1.5")
        self.val_fixed_aud_max_speed.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_fixed_speed_opts, textvariable=self.val_fixed_aud_max_speed, width=50).grid(row=0, column=3, padx=5, pady=5)

        self.chk_multi_segment_fixed = ctk.CTkCheckBox(
            self.frame_fixed_speed_opts,
            text="✂️ Hỗ trợ video đã bị cắt / nhiều clip (Multi-clip)",
            variable=self.val_enable_multi_segment,
            font=ctk.CTkFont(size=12)
        )
        self.chk_multi_segment_fixed.grid(row=1, column=0, columnspan=4, padx=5, pady=(4, 5), sticky="w")
        
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

        self.chk_multi_segment_anti = ctk.CTkCheckBox(
            self.frame_sync_opts,
            text="✂️ Hỗ trợ video đã bị cắt / nhiều clip (Multi-clip)",
            variable=self.val_enable_multi_segment,
            font=ctk.CTkFont(size=12)
        )
        self.chk_multi_segment_anti.grid(row=1, column=0, columnspan=5, padx=5, pady=(4, 5), sticky="w")
        
        self.frame_tab_srt_btns = ctk.CTkFrame(self.tab_srt, fg_color="transparent")
        self.frame_tab_srt_btns.grid(row=6, column=0, columnspan=3, padx=10, pady=20, sticky="ew")
        self.frame_tab_srt_btns.grid_columnconfigure(0, weight=3)
        self.frame_tab_srt_btns.grid_columnconfigure(1, weight=2)

        self.btn_generate_srt = ctk.CTkButton(self.frame_tab_srt_btns, text="Bắt đầu xử lý SRT", font=ctk.CTkFont(size=16, weight="bold"), height=45, command=self.on_generate_srt)
        self.btn_generate_srt.grid(row=0, column=0, padx=(0, 10), sticky="ew")

        self.btn_open_review_tab2 = ctk.CTkButton(self.frame_tab_srt_btns, text="⚠️ Mở Bảng Xử Lý Câu Lỗi", font=ctk.CTkFont(size=14, weight="bold"), fg_color="#b45309", hover_color="#92400e", text_color="#ffffff", height=45, command=self.open_review_dialog_tab2)
        self.btn_open_review_tab2.grid(row=0, column=1, sticky="ew")

        # -- Tab 3: Split Project --
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

        # -- Tab 4: STT --
        self.tab_stt.grid_columnconfigure(1, weight=1)
        
        self.stt_media_path = ctk.StringVar()
        ctk.CTkLabel(self.tab_stt, text="File Media (Video/Audio):").grid(row=0, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self.tab_stt, textvariable=self.stt_media_path, state="disabled").grid(row=0, column=1, padx=10, pady=10, sticky="ew")
        ctk.CTkButton(self.tab_stt, text="Chọn", width=60, command=self.select_stt_media).grid(row=0, column=2, padx=10, pady=10)
        
        ctk.CTkLabel(self.tab_stt, text="Ngôn ngữ gốc:").grid(row=1, column=0, padx=10, pady=10, sticky="w")
        
        self.frame_stt_row1 = ctk.CTkFrame(self.tab_stt, fg_color="transparent")
        self.frame_stt_row1.grid(row=1, column=1, columnspan=2, padx=10, pady=10, sticky="ew")
        
        self.stt_lang_combo = ctk.CTkComboBox(self.frame_stt_row1, values=["vi-VN", "zh-CN", "en-US", "ja-JP", "ko-KR", "th-TH", "id-ID", "ms-MY"], width=130)
        self.stt_lang_combo.grid(row=0, column=0, padx=(0, 20), sticky="w")
        self.stt_lang_combo.set("zh-CN")
        
        ctk.CTkLabel(self.frame_stt_row1, text="Số luồng STT (1-10):", font=ctk.CTkFont(weight="bold")).grid(row=0, column=1, padx=(0, 5), sticky="w")
        self.slider_threads_stt = ctk.CTkSlider(self.frame_stt_row1, from_=1, to=10, number_of_steps=9, command=self.update_threads_stt_label, width=120)
        self.slider_threads_stt.set(3)
        self.slider_threads_stt.grid(row=0, column=2, padx=5, sticky="w")
        self.label_threads_stt_val = ctk.CTkLabel(self.frame_stt_row1, text="3", font=ctk.CTkFont(weight="bold"))
        self.label_threads_stt_val.grid(row=0, column=3, padx=5, sticky="w")
        
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

        # -- Tab 5: Dịch Thuật (AI) --
        self.tab_trans.grid_columnconfigure(0, weight=1)
        self.tab_trans.grid_rowconfigure(3, weight=1)

        # 1. Config Bar Top (Row 0)
        self.frame_trans_top = ctk.CTkFrame(self.tab_trans)
        self.frame_trans_top.grid(row=0, column=0, padx=10, pady=(5, 3), sticky="ew")

        # Row 0 of top: API Keys + Model + Threads
        ctk.CTkLabel(self.frame_trans_top, text="🔑 Gemini API:", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, padx=(10, 2), pady=5, sticky="w")
        self.trans_api_key_var = ctk.StringVar()
        self.trans_api_key_var.trace_add("write", lambda *args: (self.save_sync_config(silent=True), self.update_key_badge(), self.update_trans_estimate()))
        
        self.btn_open_key_mgr = ctk.CTkButton(
            self.frame_trans_top,
            text="🔑 Quản Lý & Test Keys",
            width=180,
            height=30,
            fg_color="#4338ca",
            hover_color="#3730a3",
            font=ctk.CTkFont(weight="bold"),
            command=self.open_api_key_manager
        )
        self.btn_open_key_mgr.grid(row=0, column=1, padx=2, pady=5, sticky="w")

        self.lbl_key_badge = ctk.CTkLabel(self.frame_trans_top, text="(0 Key)", font=ctk.CTkFont(size=12, weight="bold"), text_color="#38bdf8")
        self.lbl_key_badge.grid(row=0, column=2, padx=(2, 10), pady=5, sticky="w")

        ctk.CTkLabel(self.frame_trans_top, text="🤖 Model AI:", font=ctk.CTkFont(weight="bold")).grid(row=0, column=3, padx=(5, 2), pady=5, sticky="w")
        self.trans_model_var = ctk.StringVar(value="gemini-3.5-flash-lite (Hạn mức 500 RPD - Gộp 1 Request)")
        self.trans_model_var.trace_add("write", lambda *args: (self.save_sync_config(silent=True), self.update_trans_estimate()))
        self.trans_model_combo = ctk.CTkComboBox(
            self.frame_trans_top,
            values=[
                "gemini-3.5-flash-lite (Hạn mức 500 RPD - Gộp 1 Request)",
                "gemini-3.6-flash (Zhihu kịch tính - Gộp 1 Request)",
                "gemma-4-31b-it (14.400 RPD - Băm nhỏ an toàn 16k TPM)",
                "gemma-4-26b-a4b-it (14.400 RPD MoE - Tốc độ cao)",
                "gemini-3.7-flash (Thế hệ 3.7 - 20 RPD)",
                "gemini-2.5-flash-lite (10 RPM / 20 RPD)"
            ],
            variable=self.trans_model_var,
            width=330
        )
        self.trans_model_combo.grid(row=0, column=4, padx=2, pady=5, sticky="w")

        ctk.CTkLabel(self.frame_trans_top, text="⚡ Số Luồng Dịch (1 - 20):", font=ctk.CTkFont(weight="bold")).grid(row=0, column=5, padx=(10, 2), pady=5, sticky="w")
        self.trans_concurrency_var = ctk.StringVar(value="🚀 Tự Động (Theo số lượng API Key)")
        self.trans_concurrency_var.trace_add("write", lambda *args: (self.save_sync_config(silent=True), self.update_trans_estimate()))
        self.trans_concurrency_combo = ctk.CTkComboBox(
            self.frame_trans_top,
            values=CONCURRENCY_OPTIONS,
            variable=self.trans_concurrency_var,
            width=220
        )
        self.trans_concurrency_combo.grid(row=0, column=6, padx=(2, 10), pady=5, sticky="w")

        # Row 1 of top: Phong cách dịch + Quick style chips
        self.frame_trans_style_row = ctk.CTkFrame(self.tab_trans, fg_color="transparent")
        self.frame_trans_style_row.grid(row=1, column=0, padx=10, pady=(2, 4), sticky="ew")

        ctk.CTkLabel(self.frame_trans_style_row, text="🎭 Phong Cách Dịch:", font=ctk.CTkFont(weight="bold")).pack(side="left", padx=(5, 5))
        self.trans_style_var = ctk.StringVar(value="")
        self.trans_style_var.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        self.trans_style_entry = ctk.CTkEntry(self.frame_trans_style_row, textvariable=self.trans_style_var, placeholder_text="Để trống để AI tự đọc và suy luận (hoặc tự nhập: Vả mặt Zhihu, Cổ trang tiên hiệp, Hài hước...)")
        self.trans_style_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.btn_style_auto = ctk.CTkButton(self.frame_trans_style_row, text="✨ Tự Động AI", width=95, height=28, command=lambda: self.set_style_preset("✨ Tự Động AI (Auto)"))
        self.btn_style_auto.pack(side="left", padx=2)
        self.btn_style_zhihu = ctk.CTkButton(self.frame_trans_style_row, text="🎬 Phim Ngắn Zhihu", width=125, height=28, command=lambda: self.set_style_preset("🎬 Phim Ngắn Zhihu"))
        self.btn_style_zhihu.pack(side="left", padx=2)
        self.btn_style_lit = ctk.CTkButton(self.frame_trans_style_row, text="📖 Thuần Việt Văn Học", width=135, height=28, command=lambda: self.set_style_preset("📖 Thuần Việt Văn Học"))
        self.btn_style_lit.pack(side="left", padx=2)
        self.btn_style_ancient = ctk.CTkButton(self.frame_trans_style_row, text="⚔️ Cổ Trang Tiên Hiệp", width=135, height=28, command=lambda: self.set_style_preset("⚔️ Cổ Trang Tiên Hiệp"))
        self.btn_style_ancient.pack(side="left", padx=2)
        self.btn_style_funny = ctk.CTkButton(self.frame_trans_style_row, text="😂 Hài Hước", width=90, height=28, command=lambda: self.set_style_preset("😂 Hài Hước Bắt Trend"))
        self.btn_style_funny.pack(side="left", padx=2)

        # Row 2: Estimate & Strategy Banner
        self.frame_trans_banner = ctk.CTkFrame(self.tab_trans, fg_color="#1e1e2d", corner_radius=6)
        self.frame_trans_banner.grid(row=2, column=0, padx=10, pady=(0, 4), sticky="ew")

        self.lbl_trans_badge = ctk.CTkLabel(self.frame_trans_banner, text="⚡ Smart Chunking: Gộp Chunk Lớn", font=ctk.CTkFont(size=12, weight="bold"), text_color="#38bdf8")
        self.lbl_trans_badge.pack(side="left", padx=(10, 5), pady=4)

        self.lbl_trans_estimate = ctk.CTkLabel(self.frame_trans_banner, text="Dùng Gemini: Tối thiểu hóa request • 0 dòng phụ đề • 0 từ", font=ctk.CTkFont(size=12), text_color="#94a3b8")
        self.lbl_trans_estimate.pack(side="left", padx=5, pady=4)

        # 3. Main 2-Column Area (Row 3)
        self.frame_trans_main = ctk.CTkFrame(self.tab_trans, fg_color="transparent")
        self.frame_trans_main.grid(row=3, column=0, padx=10, pady=4, sticky="nsew")
        self.frame_trans_main.grid_columnconfigure(0, weight=1)
        self.frame_trans_main.grid_columnconfigure(1, weight=1)
        self.frame_trans_main.grid_rowconfigure(1, weight=1)

        # Left: Source
        self.frame_source_header = ctk.CTkFrame(self.frame_trans_main, fg_color="transparent")
        self.frame_source_header.grid(row=0, column=0, padx=(0, 5), pady=(0, 5), sticky="ew")
        ctk.CTkLabel(self.frame_source_header, text="CN / GB  Văn Bản Gốc (Raw / SRT):", font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(self.frame_source_header, text="Xóa", width=45, fg_color="#b23b3b", hover_color="#8f2b2b", command=self.clear_trans_source).pack(side="right", padx=2)
        ctk.CTkButton(self.frame_source_header, text="📋 Dán Clipboard", width=110, command=self.paste_trans_source).pack(side="right", padx=2)
        ctk.CTkButton(self.frame_source_header, text="📂 Chọn File (.SRT / .TXT)", width=150, command=self.select_trans_file).pack(side="right", padx=2)

        self.trans_source_input = ctk.CTkTextbox(self.frame_trans_main, font=ctk.CTkFont(size=13))
        self.trans_source_input.grid(row=1, column=0, padx=(0, 5), pady=0, sticky="nsew")
        self.trans_source_input.bind("<KeyRelease>", lambda event: self.update_trans_estimate())

        # Right: Result
        self.frame_result_header = ctk.CTkFrame(self.frame_trans_main, fg_color="transparent")
        self.frame_result_header.grid(row=0, column=1, padx=(5, 0), pady=(0, 5), sticky="ew")
        ctk.CTkLabel(self.frame_result_header, text="VN  Bản Dịch Tiếng Việt:", font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(self.frame_result_header, text="Xóa", width=45, fg_color="#b23b3b", hover_color="#8f2b2b", command=self.clear_trans_result).pack(side="right", padx=2)
        ctk.CTkButton(self.frame_result_header, text="📋 Copy", width=55, command=self.copy_trans_result).pack(side="right", padx=2)

        self.trans_result_output = ctk.CTkTextbox(self.frame_trans_main, font=ctk.CTkFont(size=13))
        self.trans_result_output.grid(row=1, column=1, padx=(5, 0), pady=0, sticky="nsew")

        # 4. Actions and Export Bar (Row 4)
        self.frame_trans_bottom = ctk.CTkFrame(self.tab_trans, fg_color="transparent")
        self.frame_trans_bottom.grid(row=4, column=0, padx=10, pady=(4, 10), sticky="ew")
        self.frame_trans_bottom.grid_columnconfigure(0, weight=1)

        self.trans_progressbar = ctk.CTkProgressBar(self.frame_trans_bottom)
        self.trans_progressbar.grid(row=0, column=0, columnspan=7, sticky="ew", pady=(0, 8))
        self.trans_progressbar.set(0)

        # Buttons row
        self.frame_trans_btns = ctk.CTkFrame(self.frame_trans_bottom, fg_color="transparent")
        self.frame_trans_btns.grid(row=1, column=0, columnspan=7, sticky="ew")

        self.btn_start_trans = ctk.CTkButton(self.frame_trans_btns, text="⚡ Bắt đầu Dịch", font=ctk.CTkFont(size=15, weight="bold"), height=38, width=140, command=self.on_start_translate)
        self.btn_start_trans.pack(side="left", padx=(0, 5))

        self.btn_stop_trans = ctk.CTkButton(self.frame_trans_btns, text="⏹ Dừng", font=ctk.CTkFont(size=14, weight="bold"), height=38, width=70, fg_color="#b23b3b", hover_color="#8f2b2b", state="disabled", command=self.on_stop_translate)
        self.btn_stop_trans.pack(side="left", padx=(0, 15))

        ctk.CTkButton(self.frame_trans_btns, text="💾 Lưu SRT Việt", height=38, command=lambda: self.download_trans_srt("translated")).pack(side="left", padx=2)
        ctk.CTkButton(self.frame_trans_btns, text="💾 Lưu SRT Song Ngữ", height=38, command=lambda: self.download_trans_srt("bilingual")).pack(side="left", padx=2)
        ctk.CTkButton(self.frame_trans_btns, text="⬛ Lưu ASS Nền Đen", height=38, command=lambda: self.download_trans_ass("translated")).pack(side="left", padx=2)
        ctk.CTkButton(self.frame_trans_btns, text="📝 Lưu TXT", height=38, command=self.download_trans_txt).pack(side="left", padx=2)

        ctk.CTkButton(self.frame_trans_btns, text="➡️ Nạp vào Tab SRT", height=38, fg_color="#4f46e5", hover_color="#4338ca", command=self.send_trans_to_srt).pack(side="right", padx=2)
        ctk.CTkButton(self.frame_trans_btns, text="➡️ Nạp vào Tab TTS", height=38, fg_color="#059669", hover_color="#047857", command=self.send_trans_to_tts).pack(side="right", padx=2)

        # -- Tab 6: Vocal Separation (CapCut Cloud API PRO) --
        self.tab_vocal.grid_columnconfigure(0, weight=1)

        # 1. Khung cấu hình tài khoản CapCut PRO
        self.frame_vocal_auth = ctk.CTkFrame(self.tab_vocal)
        self.frame_vocal_auth.grid(row=0, column=0, padx=10, pady=(10, 5), sticky="ew")
        self.frame_vocal_auth.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            self.frame_vocal_auth,
            text="👑 Tài Khoản CapCut (Tùy chọn):",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=0, padx=10, pady=8, sticky="w")

        saved_ck = load_pro_cookie()
        self.vocal_cookie_var = ctk.StringVar(value=saved_ck)
        self.vocal_cookie_var.trace_add("write", self.on_vocal_cookie_changed)

        self.vocal_cookie_input = ctk.CTkEntry(
            self.frame_vocal_auth,
            textvariable=self.vocal_cookie_var,
            placeholder_text="Không bắt buộc — Tự động lưu khi nhập/dán, xóa đi sẽ tự hủy lưu...",
            font=ctk.CTkFont(size=12),
        )
        self.vocal_cookie_input.grid(row=0, column=1, padx=5, pady=8, sticky="ew")

        self.btn_clear_cookie = ctk.CTkButton(
            self.frame_vocal_auth,
            text="❌ Xóa",
            width=70,
            fg_color="#ef4444",
            hover_color="#dc2626",
            command=self.clear_vocal_cookie_gui,
        )
        self.btn_clear_cookie.grid(row=0, column=2, padx=5, pady=8)

        self.btn_verify_cookie = ctk.CTkButton(
            self.frame_vocal_auth,
            text="🔍 Kiểm tra",
            width=110,
            fg_color="#0284c7",
            hover_color="#0369a1",
            command=self.verify_vocal_cookie_gui,
        )
        self.btn_verify_cookie.grid(row=0, column=3, padx=(5, 10), pady=8)

        self.frame_vocal_tip = ctk.CTkFrame(self.frame_vocal_auth, fg_color="transparent")
        self.frame_vocal_tip.grid(row=1, column=0, columnspan=4, padx=10, pady=(0, 8), sticky="ew")
        self.frame_vocal_tip.grid_columnconfigure(0, weight=1)

        initial_status = (
            "💾 Đã nạp Cookie đã lưu từ trước (Tự động lưu khi sửa đổi)"
            if saved_ck
            else "⚪ Chưa cấu hình tài khoản (Đang dùng chế độ Miễn phí, nhập Cookie sẽ tự lưu)"
        )
        initial_color = "#38bdf8" if saved_ck else "gray"

        self.vocal_account_status = ctk.CTkLabel(
            self.frame_vocal_tip,
            text=initial_status,
            font=ctk.CTkFont(size=11),
            text_color=initial_color,
        )
        self.vocal_account_status.grid(row=0, column=0, sticky="w")

        # 2. Khung chọn file media & thư mục đầu ra
        self.frame_vocal_files = ctk.CTkFrame(self.tab_vocal)
        self.frame_vocal_files.grid(row=1, column=0, padx=10, pady=5, sticky="ew")
        self.frame_vocal_files.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            self.frame_vocal_files,
            text="Tệp Video/Audio:",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=0, padx=10, pady=8, sticky="w")

        self.vocal_media_path = ctk.StringVar(value="")
        self.entry_vocal_media = ctk.CTkEntry(
            self.frame_vocal_files,
            textvariable=self.vocal_media_path,
            placeholder_text="Chọn tệp video hoặc âm thanh (MP4, MKV, MP3, WAV, M4A, FLAC...)",
        )
        self.entry_vocal_media.grid(row=0, column=1, padx=5, pady=8, sticky="ew")

        ctk.CTkButton(
            self.frame_vocal_files,
            text="Chọn file...",
            width=90,
            command=self.select_vocal_media,
        ).grid(row=0, column=2, padx=5, pady=8)

        ctk.CTkLabel(
            self.frame_vocal_files,
            text="Thư mục xuất:",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=1, column=0, padx=10, pady=8, sticky="w")

        self.vocal_out_dir = ctk.StringVar(value="")
        self.entry_vocal_out_dir = ctk.CTkEntry(
            self.frame_vocal_files,
            textvariable=self.vocal_out_dir,
            placeholder_text="Để trống sẽ tự động lưu cùng thư mục với file gốc",
        )
        self.entry_vocal_out_dir.grid(row=1, column=1, padx=5, pady=8, sticky="ew")

        ctk.CTkButton(
            self.frame_vocal_files,
            text="Chọn thư mục...",
            width=90,
            command=self.select_vocal_out_dir,
        ).grid(row=1, column=2, padx=5, pady=8)

        ctk.CTkButton(
            self.frame_vocal_files,
            text="📂 Mở",
            width=50,
            fg_color="#374151",
            hover_color="#4b5563",
            command=self.open_vocal_output_dir,
        ).grid(row=1, column=3, padx=(2, 10), pady=8)

        # 3. Khung cài đặt chế độ tách & phân đoạn thông minh
        self.frame_vocal_options = ctk.CTkFrame(self.tab_vocal)
        self.frame_vocal_options.grid(row=2, column=0, padx=10, pady=5, sticky="ew")
        self.frame_vocal_options.grid_columnconfigure(5, weight=1)

        ctk.CTkLabel(
            self.frame_vocal_options,
            text="Chế độ tách:",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=0, padx=(10, 5), pady=8, sticky="w")

        self.combo_vocal_mode = ctk.CTkComboBox(
            self.frame_vocal_options,
            values=[
                "Chỉ lấy Giọng nói (Loại bỏ nhạc nền)",
                "Tách cả 2 (Giọng nói + Nhạc nền)",
                "Chỉ lấy Nhạc nền (Tách beat karaoke)",
            ],
            width=240,
        )
        self.combo_vocal_mode.set("Chỉ lấy Giọng nói (Loại bỏ nhạc nền)")
        self.combo_vocal_mode.grid(row=0, column=1, padx=5, pady=8, sticky="w")

        ctk.CTkLabel(
            self.frame_vocal_options,
            text="Định dạng xuất:",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=2, padx=(15, 5), pady=8, sticky="w")

        self.combo_vocal_format = ctk.CTkComboBox(
            self.frame_vocal_options,
            values=["MP3 (320kbps siêu nét)", "WAV (Lossless chuyên nghiệp)"],
            width=180,
        )
        self.combo_vocal_format.set("MP3 (320kbps siêu nét)")
        self.combo_vocal_format.grid(row=0, column=3, padx=5, pady=8, sticky="w")

        self.vocal_smart_chunk = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            self.frame_vocal_options,
            text="✂️ Phân đoạn thông minh nếu file > 15 phút (Khắc phục giới hạn CapCut)",
            variable=self.vocal_smart_chunk,
            font=ctk.CTkFont(size=12),
        ).grid(row=1, column=0, columnspan=2, padx=10, pady=(0, 8), sticky="w")

        ctk.CTkLabel(
            self.frame_vocal_options,
            text="Độ dài phân đoạn:",
            font=ctk.CTkFont(size=12),
        ).grid(row=1, column=2, padx=(15, 5), pady=(0, 8), sticky="w")

        vocal_dur_values = [
            f"{i} phút (Khuyên dùng)" if i == 10
            else (f"{i} phút (Tối đa)" if i == 15
            else f"{i} phút")
            for i in range(1, 16)
        ]
        self.combo_vocal_chunk_dur = ctk.CTkComboBox(
            self.frame_vocal_options,
            values=vocal_dur_values,
            width=170,
            command=self.on_vocal_chunk_dur_changed,
        )
        self.combo_vocal_chunk_dur.set("10 phút (Khuyên dùng)")
        self.combo_vocal_chunk_dur.grid(row=1, column=3, padx=5, pady=(0, 8), sticky="w")

        # Row 2: Concurrency / Đa luồng
        ctk.CTkLabel(
            self.frame_vocal_options,
            text="⚡ Đa luồng xử lý (1-100):",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=2, column=0, padx=(10, 5), pady=(0, 8), sticky="w")

        self.frame_vocal_threads = ctk.CTkFrame(self.frame_vocal_options, fg_color="transparent")
        self.frame_vocal_threads.grid(row=2, column=1, padx=5, pady=(0, 8), sticky="w")

        self.slider_threads_vocal = ctk.CTkSlider(
            self.frame_vocal_threads,
            from_=1,
            to=100,
            number_of_steps=99,
            command=self.update_threads_vocal_label,
            width=140,
        )
        self.slider_threads_vocal.set(5)
        self.slider_threads_vocal.grid(row=0, column=0, padx=(0, 5), sticky="w")

        self.label_threads_vocal_val = ctk.CTkLabel(
            self.frame_vocal_threads,
            text="5 luồng",
            font=ctk.CTkFont(weight="bold"),
            width=70,
        )
        self.label_threads_vocal_val.grid(row=0, column=1, padx=2, sticky="w")

        ctk.CTkLabel(
            self.frame_vocal_options,
            text="💡 Tối ưu video dài (1-2 tiếng): Tách nhiều đoạn cùng lúc, tốc độ tăng gấp nhiều lần.",
            font=ctk.CTkFont(size=11),
            text_color="gray",
        ).grid(row=2, column=2, columnspan=2, padx=(15, 5), pady=(0, 8), sticky="w")

        # Row 3: Tùy chọn tự động ghép vào video gốc
        self.vocal_remux_video = ctk.BooleanVar(value=False)
        self.chk_vocal_remux = ctk.CTkCheckBox(
            self.frame_vocal_options,
            text="🎬 Tự động ghép âm thanh đã tách vào video gốc (bỏ âm thanh cũ, Stream Copy siêu tốc)",
            variable=self.vocal_remux_video,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8",
        )
        self.chk_vocal_remux.grid(row=3, column=0, columnspan=4, padx=10, pady=(2, 8), sticky="w")

        # 4. Khung tiến trình & nút thao tác
        self.frame_vocal_action = ctk.CTkFrame(self.tab_vocal)
        self.frame_vocal_action.grid(row=3, column=0, padx=10, pady=(5, 10), sticky="ew")
        self.frame_vocal_action.grid_columnconfigure(0, weight=1)

        self.vocal_progressbar = ctk.CTkProgressBar(self.frame_vocal_action)
        self.vocal_progressbar.grid(row=0, column=0, columnspan=4, padx=10, pady=(10, 5), sticky="ew")
        self.vocal_progressbar.set(0)

        self.vocal_status_label = ctk.CTkLabel(
            self.frame_vocal_action,
            text="Sẵn sàng tách giọng với CapCut Cloud API.",
            font=ctk.CTkFont(size=12),
            text_color="gray",
        )
        self.vocal_status_label.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="w")

        self.frame_vocal_btns = ctk.CTkFrame(self.frame_vocal_action, fg_color="transparent")
        self.frame_vocal_btns.grid(row=2, column=0, columnspan=4, padx=10, pady=(0, 10), sticky="ew")

        self.btn_start_vocal = ctk.CTkButton(
            self.frame_vocal_btns,
            text="🚀 Bắt đầu tách giọng (CapCut API)",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=38,
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            command=self.on_start_vocal_separation,
        )
        self.btn_start_vocal.pack(side="left", padx=(0, 10))

        self.btn_cancel_vocal = ctk.CTkButton(
            self.frame_vocal_btns,
            text="⏹ Huỷ",
            font=ctk.CTkFont(size=13, weight="bold"),
            height=38,
            width=80,
            fg_color="#b23b3b",
            hover_color="#8f2b2b",
            state="disabled",
            command=self.on_cancel_vocal_separation,
        )
        self.btn_cancel_vocal.pack(side="left", padx=(0, 10))

        self.btn_open_vocal_dir = ctk.CTkButton(
            self.frame_vocal_btns,
            text="📂 Mở thư mục kết quả",
            height=38,
            command=self.open_vocal_output_dir,
        )
        self.btn_open_vocal_dir.pack(side="left", padx=2)

        self.vocal_is_running = False
        self.vocal_cancelled = False

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
        
        # Device ID Rotation
        ctk.CTkLabel(self.frame_adv, text="Đổi ID sau (block):").grid(row=1, column=0, padx=5, pady=5, sticky="e")
        self.val_id_blocks = ctk.StringVar(value="300")
        self.val_id_blocks.trace_add("write", lambda *args: self.save_sync_config(silent=True))
        ctk.CTkEntry(self.frame_adv, textvariable=self.val_id_blocks, width=60).grid(row=1, column=1, padx=5, pady=5, sticky="w")
        
        ctk.CTkButton(self.frame_adv, text="Reset", width=60, fg_color="#b23b3b", hover_color="#8f2b2b", command=self.reset_adv_config).grid(row=1, column=2, padx=5, pady=5, sticky="w")
        
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

    def show_sync_mode_guide(self):
        messagebox.showinfo(
            "Hướng dẫn chế độ đồng bộ",
            "Không đồng bộ\n"
            "• Chèn audio tại đúng mốc SRT, không đổi tốc độ video hoặc audio.\n\n"
            "Khớp từng câu (Anti-Overlap)\n"
            "• Cắt video theo từng câu SRT, rồi đổi tốc độ từng đoạn để khớp giọng.\n"
            "• Khớp sát nhất, nhưng tạo nhiều đoạn video nên dự án nặng và render lâu hơn.\n\n"
            "Đổi tốc độ toàn bộ (Fixed Speed)\n"
            "• Video giữ nguyên ở các câu bình thường; audio được tăng tốc trong khoảng Audio chậm nhất–nhanh nhất.\n"
            "• Chỉ khi audio vẫn quá dài ở tốc độ tối đa, tool mới cắt đúng đoạn video của câu đó và làm chậm đoạn ấy.\n"
            "• Audio ngắn hơn không bị chỉnh; phần trống được giữ nguyên. Đây là mode nhẹ hơn Anti-Overlap.\n\n"
            "✂️ Tùy chọn: Hỗ trợ video đã bị cắt / nhiều clip (Multi-clip)\n"
            "• Mặc định: TẮT (dùng thuật toán gốc cho dự án có 1 video liền mạch).\n"
            "• BẬT: Khi dự án CapCut của bạn đã bị cắt tỉa nhiều đoạn hoặc ghép nối từ nhiều clip khác nhau. Tool sẽ tự động quét toàn bộ timeline và co giãn từng clip mà vẫn giữ nguyên vị trí cắt, hiệu ứng, góc quay."
        )

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
                    if "selected_voice" in config: self.saved_voice = config["selected_voice"]
                    if "val_min_video" in config: self.val_min_video.set(config["val_min_video"])
                    if "val_max_video" in config: self.val_max_video.set(config["val_max_video"])
                    if "val_max_audio" in config: self.val_max_audio.set(config["val_max_audio"])
                    if "val_id_blocks" in config: self.val_id_blocks.set(config["val_id_blocks"])
                    if "sync_mode" in config: self.combo_sync_mode_var.set(config["sync_mode"])
                    if "fixed_vid_speed" in config: self.val_fixed_vid_speed.set(config["fixed_vid_speed"])
                    if "fixed_aud_min_speed" in config:
                        self.val_fixed_aud_min_speed.set(config["fixed_aud_min_speed"])
                    elif "fixed_aud_speed" in config:
                        self.val_fixed_aud_min_speed.set(config["fixed_aud_speed"])
                    if "fixed_aud_max_speed" in config:
                        self.val_fixed_aud_max_speed.set(config["fixed_aud_max_speed"])
                    if "enable_multi_segment_video" in config and hasattr(self, "val_enable_multi_segment"):
                        self.val_enable_multi_segment.set(bool(config["enable_multi_segment_video"]))
                    
                    self.toggle_sync_opts()
                    
                    if "adv_vid_vol" in config: self.val_vid_vol.set(config["adv_vid_vol"])
                    if "adv_aud_vol" in config: self.val_aud_vol.set(config["adv_aud_vol"])
                    if "wm_enabled" in config: self.val_wm_enabled.set(config["wm_enabled"])
                    if "wm_path" in config: self.val_wm_path.set(config["wm_path"])
                    if "wm_x" in config: self.val_wm_x.set(config["wm_x"])
                    if "wm_y" in config: self.val_wm_y.set(config["wm_y"])
                    if "wm_scale" in config: self.val_wm_scale.set(config["wm_scale"])
                    
                    if "threads_basic" in config:
                        self.slider_threads_basic.set(config["threads_basic"])
                        self.label_threads_basic_val.configure(text=f"{config['threads_basic']}")
                    elif "val_threads" in config:
                        self.slider_threads_basic.set(min(100, config["val_threads"]))
                        self.label_threads_basic_val.configure(text=f"{min(100, config['val_threads'])}")
                        
                    if "threads_srt" in config:
                        self.slider_threads_srt.set(config["threads_srt"])
                        self.label_threads_srt_val.configure(text=f"{config['threads_srt']}")
                    elif "val_threads" in config:
                        self.slider_threads_srt.set(min(100, config["val_threads"]))
                        self.label_threads_srt_val.configure(text=f"{min(100, config['val_threads'])}")
                        
                    if "threads_stt" in config:
                        self.slider_threads_stt.set(config["threads_stt"])
                        self.label_threads_stt_val.configure(text=f"{config['threads_stt']}")

                    if "threads_vocal" in config and hasattr(self, "slider_threads_vocal"):
                        val_vocal = min(100, max(1, int(config["threads_vocal"])))
                        self.slider_threads_vocal.set(val_vocal)
                        self.label_threads_vocal_val.configure(text=f"{val_vocal} luồng")

                    if "vocal_chunk_dur" in config and hasattr(self, "combo_vocal_chunk_dur"):
                        self.combo_vocal_chunk_dur.set(config["vocal_chunk_dur"])
                        
                    if "trans_api_keys" in config and hasattr(self, "trans_api_key_var"):
                        self.trans_api_key_var.set(config["trans_api_keys"])
                    if "trans_model" in config and hasattr(self, "trans_model_var"):
                        self.trans_model_var.set(config["trans_model"])
                    if "trans_style" in config and hasattr(self, "trans_style_var"):
                        self.trans_style_var.set(config["trans_style"])
                    if "trans_concurrency" in config and hasattr(self, "trans_concurrency_var"):
                        self.trans_concurrency_var.set(config["trans_concurrency"])
                        
                    self.update_key_badge()
                    self.update_trans_estimate()
        except Exception as e:
            print(f"Error loading config: {e}")

    def save_sync_config(self, silent=False):
        try:
            current_voice = self.combo_voice.get() if hasattr(self, "combo_voice") else getattr(self, "saved_voice", "Cô Gái Hoạt Ngôn (BV074_streaming)")
            if current_voice == "Đang tải..." or current_voice.startswith("Lỗi:"):
                current_voice = getattr(self, "saved_voice", "Cô Gái Hoạt Ngôn (BV074_streaming)")
                
            config = {
                "selected_voice": current_voice,
                "val_min_video": self.val_min_video.get(),
                "val_max_video": self.val_max_video.get(),
                "val_max_audio": self.val_max_audio.get(),
                "val_id_blocks": self.val_id_blocks.get(),
                "sync_mode": self.combo_sync_mode_var.get(),
                "fixed_vid_speed": self.val_fixed_vid_speed.get(),
                "fixed_aud_min_speed": self.val_fixed_aud_min_speed.get(),
                "fixed_aud_max_speed": self.val_fixed_aud_max_speed.get(),
                "enable_multi_segment_video": self.val_enable_multi_segment.get() if hasattr(self, "val_enable_multi_segment") else False,
                "adv_vid_vol": self.val_vid_vol.get(),
                "adv_aud_vol": self.val_aud_vol.get(),
                "wm_enabled": self.val_wm_enabled.get(),
                "wm_path": self.val_wm_path.get(),
                "wm_x": self.val_wm_x.get(),
                "wm_y": self.val_wm_y.get(),
                "wm_scale": self.val_wm_scale.get(),
                "threads_basic": int(self.slider_threads_basic.get()),
                "threads_srt": int(self.slider_threads_srt.get()),
                "threads_stt": int(self.slider_threads_stt.get()),
                "threads_vocal": int(self.slider_threads_vocal.get()) if hasattr(self, "slider_threads_vocal") else 5,
                "vocal_chunk_dur": self.combo_vocal_chunk_dur.get() if hasattr(self, "combo_vocal_chunk_dur") else "10 phút (Khuyên dùng)",
                "trans_api_keys": self.trans_api_key_var.get() if hasattr(self, "trans_api_key_var") else "",
                "trans_model": self.trans_model_var.get() if hasattr(self, "trans_model_var") else "gemini-3.5-flash-lite (Hạn mức 500 RPD - Gộp 1 Request)",
                "trans_style": self.trans_style_var.get() if hasattr(self, "trans_style_var") else "",
                "trans_concurrency": self.trans_concurrency_var.get() if hasattr(self, "trans_concurrency_var") else "🚀 Tự Động (Theo số lượng API Key)",
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
        self.val_fixed_vid_speed.set("1.0")
        self.val_fixed_aud_min_speed.set("0.8")
        self.val_fixed_aud_max_speed.set("1.5")
        if hasattr(self, "val_enable_multi_segment"):
            self.val_enable_multi_segment.set(False)
        self.toggle_sync_opts()
        
    def reset_adv_config(self):
        self.val_id_blocks.set("300")
        self.val_vid_vol.set("0")
        self.val_aud_vol.set("0")

    def reset_device_id(self):
        self.client.device.randomize()
        new_id = self.client.device.device_id
        messagebox.showinfo("Thành công", f"Đã tự động đổi sang thiết bị mới!\nDevice ID: {new_id}\nBạn đã có thể tiếp tục tạo giọng nói.")

    def update_rate_label(self, value):
        self.label_rate_val.configure(text=f"{value:.1f}")
        
    def update_threads_basic_label(self, value):
        self.label_threads_basic_val.configure(text=f"{int(value)}")
        self.save_sync_config(silent=True)

    def update_threads_srt_label(self, value):
        self.label_threads_srt_val.configure(text=f"{int(value)}")
        self.save_sync_config(silent=True)

    def update_threads_stt_label(self, value):
        self.label_threads_stt_val.configure(text=f"{int(value)}")
        self.save_sync_config(silent=True)

    def update_threads_vocal_label(self, value):
        self.label_threads_vocal_val.configure(text=f"{int(value)} luồng")
        self.save_sync_config(silent=True)

    def on_vocal_chunk_dur_changed(self, choice=None):
        self.save_sync_config(silent=True)

    def on_voice_changed(self, choice=None):
        val = self.combo_voice.get()
        if val and val != "Đang tải..." and not val.startswith("Lỗi:"):
            self.saved_voice = val
            self.save_sync_config(silent=True)

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
                default_voice = None
                saved = getattr(self, "saved_voice", None)
                if saved and saved in voice_names:
                    default_voice = saved
                else:
                    for vname in voice_names:
                        # Prioritize exact "Cô Gái Hoạt Ngôn (BV074_streaming)" (not "BV074_streaming_dsp")
                        if "(bv074_streaming)" in vname.lower() or ("cô gái hoạt ngôn" in vname.lower() and "dsp" not in vname.lower()):
                            default_voice = vname
                            break
                    if not default_voice:
                        default_voice = voice_names[0]
                self.saved_voice = default_voice
                self.after(0, lambda: self.combo_voice.set(default_voice))
        except Exception as e:
            err_msg = [f"Lỗi: {str(e)[:50]}..."]
            self.after(0, lambda: self.combo_voice.configure(values=err_msg))
            self.after(0, lambda: self.combo_voice.set(err_msg[0]))

    def get_selected_voice(self):
        selected_voice_str = self.combo_voice.get()
        try:
            return selected_voice_str.split("(")[-1].strip(")")
        except IndexError:
            return "BV074_streaming"
            
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
                
                num_threads = min(len(chunks), int(self.slider_threads_basic.get()) if hasattr(self, "slider_threads_basic") else 20)
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
        num_threads = int(self.slider_threads_srt.get()) if hasattr(self, "slider_threads_srt") else 50
        
        min_vid = 0.85
        max_vid = 1.15
        max_aud = 1.15
        fixed_vid_speed = 1.0
        fixed_aud_min_speed = 0.8
        fixed_aud_max_speed = 1.5
        
        try:
            val_id_blocks = int(self.val_id_blocks.get())
        except ValueError:
            messagebox.showwarning("Lỗi", "Vui lòng nhập số nguyên hợp lệ cho cấu hình đổi ID.")
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
                fixed_aud_min_speed = float(self.val_fixed_aud_min_speed.get())
                fixed_aud_max_speed = float(self.val_fixed_aud_max_speed.get())
            except ValueError:
                messagebox.showwarning("Lỗi", "Vui lòng nhập số hợp lệ cho Audio min/max (ví dụ: 0.8, 1.5).")
                return
            if fixed_aud_min_speed <= 0 or fixed_aud_max_speed <= 0 or fixed_aud_min_speed > fixed_aud_max_speed:
                messagebox.showwarning("Lỗi", "Tốc độ audio phải lớn hơn 0 và Audio chậm nhất không được lớn hơn Audio nhanh nhất.")
                return

        adv_settings = self.get_adv_settings()
        self.btn_generate_srt.configure(state="disabled", text="Đang xử lý...")
        self.is_cancelled = False
        self.btn_stop.configure(state="normal")
        threading.Thread(target=self.generate_srt_thread, args=(srt_file, json_file, voice_type, rate, sync_mode, min_vid, max_vid, max_aud, num_threads, val_id_blocks, adv_settings, fixed_vid_speed, fixed_aud_min_speed, fixed_aud_max_speed), daemon=True).start()
        
    def generate_srt_thread(self, srt_file, json_file, voice_type, rate, sync_mode, min_vid_spd, max_vid_spd, max_aud_spd, num_threads, val_id_blocks, adv_settings, fixed_vid_speed, fixed_aud_min_speed, fixed_aud_max_speed):
        try:
            subs = pysrt.open(srt_file)
            total = len(subs)
            if total == 0:
                raise Exception("File SRT rỗng hoặc không hợp lệ.")

            # Syncing a single original video needs a monotonic, non-overlapping
            # source timeline.  Chunked STT occasionally returns two adjacent
            # rows that overlap at a chunk boundary.  Preserve subtitle order:
            # move the later row to the previous row's end while preserving its
            # own duration.  A following row is adjusted only if this local
            # move reaches it.  This prevents the video source cursor from
            # moving past the next SRT start without shifting the rest of a
            # long video.
            srt_timeline_issues = []
            srt_timeline_adjustments = []
            previous_nonempty = None
            for i, sub in enumerate(subs):
                text = sub.text.replace("\n", " ").strip()
                if not text:
                    continue
                start_ms = sub.start.ordinal
                end_ms = sub.end.ordinal
                if end_ms <= start_ms:
                    srt_timeline_issues.append(
                        f"Câu {i + 1}: timecode kết thúc không lớn hơn bắt đầu"
                    )
                if previous_nonempty and start_ms < previous_nonempty["end_ms"]:
                    old_start_ms = start_ms
                    old_end_ms = end_ms
                    shift_ms = previous_nonempty["end_ms"] - start_ms
                    start_ms += shift_ms
                    end_ms += shift_ms
                    sub.start.ordinal = start_ms
                    sub.end.ordinal = end_ms
                    srt_timeline_adjustments.append({
                        "shifted_index": i + 1,
                        "previous_index": previous_nonempty["index"],
                        "old_start_ms": old_start_ms,
                        "new_start_ms": start_ms,
                        "old_end_ms": old_end_ms,
                        "new_end_ms": end_ms,
                        "shifted_ms": shift_ms,
                    })
                previous_nonempty = {"index": i + 1, "end_ms": end_ms}

            if srt_timeline_issues and sync_mode != "Không đồng bộ":
                preview = "\n".join(srt_timeline_issues[:12])
                more = len(srt_timeline_issues) - min(len(srt_timeline_issues), 12)
                if more:
                    preview += f"\n... và {more} lỗi timecode khác"
                raise Exception(
                    "SRT có timecode chồng nhau/không hợp lệ nên không thể khớp hình an toàn.\n"
                    "Hãy sửa các mốc sau hoặc chọn 'Không đồng bộ':\n"
                    f"{preview}"
                )

            if srt_timeline_adjustments and sync_mode != "Không đồng bộ":
                self.after(0, lambda n=len(srt_timeline_adjustments): self.label_status.configure(
                    text=f"Đã tự xử lý {n} chỗ SRT chồng nhau ở ranh giới STT; đang tạo giọng...",
                    text_color="orange"
                ))
                
            self.after(0, lambda: self.progressbar.set(0))
            self.after(0, lambda: self.label_progress.configure(text=f"Tiến độ: 0 / {total} câu"))
            self.after(0, lambda: self.label_status.configure(text="Bắt đầu tạo âm thanh từ SRT (chạy đa luồng)..."))

            # Create an output dir for this project's audio
            proj_dir = os.path.dirname(json_file)
            audio_dir = os.path.join(proj_dir, "tts_audios")
            os.makedirs(audio_dir, exist_ok=True)
            if srt_timeline_adjustments and sync_mode != "Không đồng bộ":
                adjustment_path = os.path.join(proj_dir, "srt_timeline_adjustments.json")
                with open(adjustment_path, "w", encoding="utf-8") as f:
                    json.dump(srt_timeline_adjustments, f, ensure_ascii=False, indent=2)
            
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
                    
                    if val_id_blocks > 0 and completed % val_id_blocks == 0:
                        self.client.device.randomize()
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
                        initial_logs=[("TTS_NEEDS_REVIEW", f"Có {len(missing_items)} câu bị lỗi CapCut cần xử lý.")],
                        threads_count=int(self.slider_threads_srt.get()) if hasattr(self, 'slider_threads_srt') else 10
                    )

                self.after(0, show_dialog)
                dialog_event.wait()

                if not user_choice[0] or user_choice[0][0] == "cancel":
                    self.after(0, lambda: self.label_status.configure(text="Đã dừng. Bạn có thể bấm '⚠️ Mở Bảng Xử Lý Câu Lỗi' để mở lại bất kỳ lúc nào.", text_color="orange"))
                    return

                # Proceed
                _, resolved_items, unresolved_items = user_choice[0]
                for res_it in resolved_items:
                    if "result_info" in res_it and res_it["result_info"]:
                        audio_info_list.append(res_it["result_info"])

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
                self.after(0, lambda: self.label_status.configure(text="Đang khớp audio và chỉ cắt video ở các câu vượt giới hạn..."))
            else:
                self.after(0, lambda: self.label_status.configure(text="Đang chèn âm thanh vào dự án CapCut..."))
                
            modify_capcut_project(json_file, final_audio_info, sync_mode, adv_settings, fixed_vid_speed, fixed_aud_min_speed, fixed_aud_max_speed, enable_multi_segment=self.val_enable_multi_segment.get() if hasattr(self, 'val_enable_multi_segment') else False)
            
            self.after(0, lambda: self.label_status.configure(text="Hoàn tất!", text_color="green"))
            
            actual_skipped = [item for item in final_audio_info if item.get("is_dummy") or not item.get("path")]
            msg = f"Đã chèn âm thanh thành công!\nVui lòng tải lại dự án trên CapCut."
            if actual_skipped:
                msg += f"\n\n(Đã bỏ qua {len(actual_skipped)} câu lỗi không có âm thanh)"
            if reports:
                msg += f"\n\nLưu ý: Có {len(reports)} câu dịch quá dài không thể ép vừa khớp tốc độ. Đã lưu báo cáo tại overlap_report.json"
            self.after(0, lambda m=msg: messagebox.showinfo("Thành công", m))
            
        except Exception as e:
            self.after(0, lambda e=e: self.label_status.configure(text=f"Lỗi: {e}", text_color="red"))
            self.after(0, lambda e=e: messagebox.showerror("Lỗi", f"Có lỗi xảy ra:\n{e}"))
        finally:
            self.after(0, lambda: self.btn_generate_srt.configure(state="normal", text="Bắt đầu xử lý SRT"))
            self.after(0, lambda: self.btn_stop.configure(state="disabled"))

    def open_review_dialog_tab2(self):
        srt_file = self.srt_path.get()
        json_file = self.json_path.get()
        
        if not srt_file or not os.path.exists(srt_file):
            messagebox.showwarning("Cảnh báo", "Vui lòng chọn file SRT hợp lệ trước.")
            return
        if not json_file or not os.path.exists(json_file):
            messagebox.showwarning("Cảnh báo", "Vui lòng chọn file Dự án CapCut (JSON) trước.")
            return
            
        voice_type = self.get_selected_voice()
        rate = self.get_selected_rate()
        sync_mode = self.combo_sync_mode_var.get()
        
        try:
            min_vid = float(self.val_min_video.get())
            max_vid = float(self.val_max_video.get())
            max_aud = float(self.val_max_audio.get())
            fixed_vid_speed = float(self.val_fixed_vid_speed.get())
            fixed_aud_min_speed = float(self.val_fixed_aud_min_speed.get())
            fixed_aud_max_speed = float(self.val_fixed_aud_max_speed.get())
        except:
            min_vid, max_vid, max_aud = 0.85, 1.15, 1.15
            fixed_vid_speed, fixed_aud_min_speed, fixed_aud_max_speed = 1.0, 0.8, 1.5

        if sync_mode == "Đổi tốc độ toàn bộ (Fixed Speed)" and (
            fixed_aud_min_speed <= 0 or fixed_aud_max_speed <= 0 or
            fixed_aud_min_speed > fixed_aud_max_speed
        ):
            messagebox.showwarning("Lỗi", "Tốc độ audio phải lớn hơn 0 và Audio chậm nhất không được lớn hơn Audio nhanh nhất.")
            return
            
        adv_settings = self.get_adv_settings()
        
        try:
            subs = pysrt.open(srt_file)
            if len(subs) == 0:
                messagebox.showwarning("Cảnh báo", "File SRT rỗng.")
                return
        except Exception as e:
            messagebox.showerror("Lỗi", f"Không thể đọc file SRT: {e}")
            return
            
        proj_dir = os.path.dirname(json_file)
        audio_dir = os.path.join(proj_dir, "tts_audios")
        os.makedirs(audio_dir, exist_ok=True)
        
        missing_items = []
        for i, sub in enumerate(subs):
            audio_path = os.path.join(audio_dir, f"audio_{i+1:04d}.mp3")
            start_micros = sub.start.ordinal * 1000
            end_micros = sub.end.ordinal * 1000
            orig_dur = end_micros - start_micros
            sub_id = getattr(sub, 'index', i + 1) or (i + 1)
            
            is_missing = True
            if os.path.exists(audio_path):
                try:
                    audio = MP3(audio_path)
                    if audio.info.length > 0:
                        is_missing = False
                except Exception:
                    pass
                    
            if is_missing:
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
                
        if not missing_items:
            messagebox.showinfo("Thông báo", "🎉 Tuyệt vời! Tất cả các câu đều đã có âm thanh hợp lệ trong thư mục tts_audios.\nKhông có câu nào bị lỗi.")
            return

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
                    if video_speed < min_vid:
                        req_spd = dur / (orig_dur / min_vid) if orig_dur > 0 else 2.0
                        if req_spd > max_aud:
                            app_spd = max_aud
                            overlap_sec = (dur / max_aud - (orig_dur / min_vid)) / 1000000.0
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
                            if video_speed < min_vid:
                                video_speed = min_vid
                        except Exception:
                            pass
                    elif video_speed > max_vid:
                        video_speed = max_vid

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

        def on_proceed_tab2(resolved, unresolved):
            def _apply_worker():
                try:
                    self.after(0, lambda: self.label_status.configure(text="Đang chèn âm thanh vào dự án CapCut...", text_color="orange"))
                    final_audio_info = []
                    for i, sub in enumerate(subs):
                        text = sub.text.replace("\n", " ").strip()
                        if not text:
                            continue
                        start_micros = sub.start.ordinal * 1000
                        end_micros = sub.end.ordinal * 1000
                        orig_dur = end_micros - start_micros
                        audio_path = os.path.join(audio_dir, f"audio_{i+1:04d}.mp3")
                        if os.path.exists(audio_path):
                            try:
                                audio = MP3(audio_path)
                                dur = int(audio.info.length * 1000000)
                                v_spd = 1.0
                                if sync_mode == "Khớp từng câu (Anti-Overlap)":
                                    v_spd = orig_dur / dur if dur > 0 else 1.0
                                    if v_spd < min_vid: v_spd = min_vid
                                    elif v_spd > max_vid: v_spd = max_vid
                                final_audio_info.append({
                                    "index": i, "path": audio_path, "start": start_micros, "end": end_micros,
                                    "duration": dur, "video_speed": v_spd, "report_item": None
                                })
                            except Exception:
                                final_audio_info.append({
                                    "index": i, "path": "", "start": start_micros, "end": end_micros,
                                    "duration": orig_dur, "video_speed": 1.0, "is_dummy": True, "report_item": None
                                })
                        else:
                            final_audio_info.append({
                                "index": i, "path": "", "start": start_micros, "end": end_micros,
                                "duration": orig_dur, "video_speed": 1.0, "is_dummy": True, "report_item": None
                            })
                    final_audio_info.sort(key=lambda x: x["index"])
                    modify_capcut_project(json_file, final_audio_info, sync_mode, adv_settings, fixed_vid_speed, fixed_aud_min_speed, fixed_aud_max_speed, enable_multi_segment=self.val_enable_multi_segment.get() if hasattr(self, 'val_enable_multi_segment') else False)
                    self.after(0, lambda: self.label_status.configure(text="Hoàn tất! Đã cập nhật âm thanh vào dự án CapCut.", text_color="green"))
                    self.after(0, lambda: messagebox.showinfo("Thành công", "Đã chèn âm thanh vào dự án CapCut thành công!\nVui lòng tải lại dự án trên CapCut."))
                except Exception as ex:
                    self.after(0, lambda ex=ex: self.label_status.configure(text=f"Lỗi: {ex}", text_color="red"))
                    self.after(0, lambda ex=ex: messagebox.showerror("Lỗi", f"Có lỗi xảy ra khi chèn vào CapCut:\n{ex}"))

            threading.Thread(target=_apply_worker, daemon=True).start()

        TTSErrorReviewDialog(
            parent=self,
            missing_items=missing_items,
            generate_fn=generate_single_item,
            on_proceed_callback=on_proceed_tab2,
            on_cancel_callback=None,
            initial_logs=[("TTS_NEEDS_REVIEW", f"Có {len(missing_items)} câu bị thiếu / lỗi cần xử lý.")],
            threads_count=int(self.slider_threads_srt.get()) if hasattr(self, 'slider_threads_srt') else 10
        )


    # --- SPLIT PROJECT TAB ---
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
        self.stt_progressbar.configure(mode="determinate")
        self.stt_progressbar.set(0)
        
        threading.Thread(target=self.generate_stt_thread, args=(media_file, lang, use_trans, target_lang, out_srt), daemon=True).start()
        
    def generate_stt_thread(self, media_file, lang, use_trans, target_lang, out_srt):
        try:
            num_threads = int(self.slider_threads_stt.get()) if hasattr(self, "slider_threads_stt") else 3
            
            def on_progress(p):
                prog = p.get("progress", 0.0)
                msg = p.get("message", "")
                self.after(0, lambda pv=prog: self.stt_progressbar.set(pv))
                self.after(0, lambda m=msg: self.label_status.configure(text=m, text_color="white"))
                
            def check_cancelled():
                return self.is_cancelled

            self.after(0, lambda: self.stt_progressbar.set(0.02))
            self.after(0, lambda: self.label_status.configure(text="Đang khởi tạo bộ nhận diện STT đa luồng...", text_color="white"))

            # Chạy pipeline STT chia đoạn và đa luồng tối ưu từ truyen-ngan
            subtitles = self.client.transcribe_large_media(
                media_path=media_file,
                language=lang,
                translation_language=target_lang,
                use_translation=use_trans,
                chunk_duration_sec=600, # Phân đoạn 10 phút/đoạn
                concurrency=num_threads,
                progress_callback=on_progress,
                cancel_check=check_cancelled
            )

            if not subtitles or not subtitles.utterances:
                raise Exception("Không tìm thấy bất kỳ giọng nói nào trong file này (hoặc API trả về rỗng).")

            self.after(0, lambda: self.label_status.configure(text="Đang lưu file phụ đề SRT..."))
            
            # Lưu file SRT chuẩn
            subtitles.save_srt(out_srt)
            
            self.after(0, lambda: self.stt_progressbar.set(1.0))
            self.after(0, lambda: self.label_status.configure(text="Hoàn tất trích xuất phụ đề SRT!", text_color="green"))
            msg = f"Đã trích xuất {len(subtitles.utterances)} câu phụ đề thành công!\nĐã lưu tại:\n{out_srt}"
            self.after(0, lambda m=msg: messagebox.showinfo("Thành công", m))
            
        except Exception as e:
            self.after(0, lambda e=e: self.label_status.configure(text=f"Lỗi: {e}", text_color="red"))
            self.after(0, lambda e=e: messagebox.showerror("Lỗi", f"Có lỗi xảy ra:\n{e}"))
        finally:
            self.after(0, lambda: self.stt_progressbar.set(0))
            self.after(0, lambda: self.btn_generate_stt.configure(state="normal", text="Bắt đầu trích xuất SRT"))
            self.after(0, lambda: self.btn_stop.configure(state="disabled"))

    # =========================================================================
    # TAB 5: DỊCH THUẬT (AI) METHODS
    # =========================================================================

    def set_style_preset(self, preset_name):
        preset_val = STYLE_PRESETS.get(preset_name, "")
        self.trans_style_var.set(preset_val)
        self.save_sync_config(silent=True)

    def update_trans_estimate(self):
        if not hasattr(self, "trans_source_input") or not hasattr(self, "lbl_trans_estimate"):
            return
        text = self.trans_source_input.get("1.0", "end-1c").strip()
        model = self.trans_model_var.get() if hasattr(self, "trans_model_var") else "gemini-3.5-flash-lite (Hạn mức 500 RPD - Gộp 1 Request)"
        concurrency_str = self.trans_concurrency_var.get() if hasattr(self, "trans_concurrency_var") else "auto"
        api_keys_str = self.trans_api_key_var.get() if hasattr(self, "trans_api_key_var") else ""
        keys = [k.strip() for k in re.split(r"[,;\n\r]+", api_keys_str) if k.strip()]
        num_threads = parse_concurrency_val(concurrency_str, len(keys))

        is_srt = is_srt_content(text)
        is_gemma = "gemma" in model.lower()

        if is_gemma:
            self.lbl_trans_badge.configure(text="🛡️ Smart Chunking: Băm Nhỏ An Toàn 16k TPM (Gemma)", text_color="#f59e0b")
        else:
            self.lbl_trans_badge.configure(text="⚡ Smart Chunking: Gộp Chunk Lớn (Gemini)", text_color="#38bdf8")

        if not text:
            self.lbl_trans_estimate.configure(text="Dùng Gemini: Tối thiểu hóa request (Ước tính tốn 1 Request duy nhất) • 0 dòng phụ đề • 0 từ")
            return

        if is_srt:
            items = parse_srt(text)
            total_lines = len(items)
            total_words = sum(count_units(it.original_text) for it in items)
            chunks, config = chunk_srt_items(items, model)
            req_count = len(chunks)
            est_sec = max(2, math.ceil((req_count * 3.8) / num_threads))
            self.lbl_trans_estimate.configure(
                text=f"{req_count} Request (~{est_sec}s hoàn thành • {num_threads} Luồng Song Song • {config['chunk_size']} dòng/đoạn) • {total_lines} dòng phụ đề • ~{total_words} từ"
            )
        else:
            total_words = count_units(text)
            total_chars = len(text)
            chunks, config = chunk_raw_text(text, model)
            req_count = len(chunks)
            est_sec = max(3, math.ceil((req_count * 4.0) / num_threads))
            self.lbl_trans_estimate.configure(
                text=f"{req_count} Request (~{est_sec}s hoàn thành • {num_threads} Luồng Song Song • {config['chunk_size']} chữ/đoạn) • ~{total_words} chữ • {total_chars} ký tự"
            )

    def update_key_badge(self):
        if hasattr(self, "lbl_key_badge") and hasattr(self, "trans_api_key_var"):
            raw = self.trans_api_key_var.get().strip()
            keys = [k.strip() for k in re.split(r"[,;\n\r\t]+", raw) if k.strip()]
            self.lbl_key_badge.configure(text=f"({len(keys)} Key)")

    def open_api_key_manager(self):
        def on_keys_saved(new_keys_str):
            self.trans_api_key_var.set(new_keys_str)
            self.save_sync_config(silent=True)
            self.update_key_badge()
            self.update_trans_estimate()
            messagebox.showinfo("Thành công", "Đã lưu danh sách API Keys thành công!")

        current_keys = self.trans_api_key_var.get()
        ApiKeyManagerDialog(self, initial_keys_str=current_keys, on_save_callback=on_keys_saved)

    def select_trans_file(self):
        file_path = filedialog.askopenfilename(
            title="Chọn file SRT hoặc file văn bản TXT",
            filetypes=[("Phụ đề & Văn bản", "*.srt *.txt *.vtt"), ("Tất cả files", "*.*")]
        )
        if not file_path:
            return
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            self.trans_source_input.delete("1.0", "end")
            self.trans_source_input.insert("1.0", content)
            self.update_trans_estimate()
            self.label_status.configure(text=f"Đã mở file: {os.path.basename(file_path)}", text_color="white")
        except Exception as e:
            messagebox.showerror("Lỗi", f"Không thể đọc file: {e}")

    def paste_trans_source(self):
        try:
            text = self.clipboard_get()
            if text:
                self.trans_source_input.insert("end", text)
                self.update_trans_estimate()
        except Exception:
            pass

    def clear_trans_source(self):
        self.trans_source_input.delete("1.0", "end")
        self.update_trans_estimate()

    def copy_trans_result(self):
        text = self.trans_result_output.get("1.0", "end-1c")
        if text.strip():
            self.clipboard_clear()
            self.clipboard_append(text)
            messagebox.showinfo("Thông báo", "Đã sao chép toàn bộ bản dịch vào bộ nhớ tạm!")

    def clear_trans_result(self):
        self.trans_result_output.delete("1.0", "end")

    def on_stop_translate(self):
        self.is_cancelled = True
        self.label_status.configure(text="Đang dừng quá trình dịch...", text_color="orange")

    def on_start_translate(self):
        source_text = self.trans_source_input.get("1.0", "end-1c").strip()
        if not source_text:
            messagebox.showwarning("Cảnh báo", "Vui lòng nhập văn bản hoặc chọn file SRT cần dịch!")
            return

        api_keys_str = self.trans_api_key_var.get().strip()
        if not api_keys_str:
            messagebox.showwarning("Cảnh báo", "Vui lòng nhập Gemini API Key để dịch thuật!\n(Có thể lấy miễn phí tại aistudio.google.com)")
            return

        model = self.trans_model_var.get()
        style = self.trans_style_var.get()
        concurrency = self.trans_concurrency_var.get() if hasattr(self, "trans_concurrency_var") else "auto"

        self.btn_start_trans.configure(state="disabled", text="Đang dịch...")
        self.btn_stop_trans.configure(state="normal")
        self.is_cancelled = False
        self.trans_progressbar.set(0)
        self.trans_result_output.delete("1.0", "end")

        threading.Thread(
            target=self.translate_worker_thread,
            args=(source_text, api_keys_str, model, style, concurrency),
            daemon=True
        ).start()

    def translate_worker_thread(self, source_text, api_keys_str, model, style, concurrency):
        try:
            translator = GeminiTranslator(api_keys=api_keys_str, model=model)
            is_srt = is_srt_content(source_text)

            def on_progress(p):
                prog = p.get("progress", 0.0)
                msg = p.get("message", "")
                acc = p.get("accumulated_text", "")
                self.after(0, lambda pv=prog: self.trans_progressbar.set(pv))
                self.after(0, lambda m=msg: self.label_status.configure(text=m, text_color="white"))
                if acc:
                    self.after(0, lambda t=acc: self._update_trans_result(t))

            def check_cancelled():
                return self.is_cancelled

            self.after(0, lambda: self.label_status.configure(text="Đang phân tích và chia đoạn nội dung...", text_color="white"))

            if is_srt:
                parsed_items = parse_srt(source_text)
                if not parsed_items:
                    raise Exception("Không thể nhận diện các khối phụ đề SRT hợp lệ.")
                
                self.after(0, lambda n=len(parsed_items): self.label_status.configure(
                    text=f"Đã nhận diện {n} câu phụ đề SRT. Bắt đầu dịch đa luồng...",
                    text_color="white"
                ))

                translated_items = translator.translate_srt(
                    srt_content_or_items=parsed_items,
                    style=style,
                    concurrency=concurrency,
                    progress_callback=on_progress,
                    cancel_check=check_cancelled
                )
                self.last_translated_srt_items = translated_items
                final_text = build_srt(translated_items, mode="translated")
            else:
                self.last_translated_srt_items = None
                final_text = translator.translate_text(
                    raw_text=source_text,
                    style=style,
                    concurrency=concurrency,
                    progress_callback=on_progress,
                    cancel_check=check_cancelled
                )

            self.after(0, lambda t=final_text: self._update_trans_result(t))
            self.after(0, lambda: self.trans_progressbar.set(1.0))
            self.after(0, lambda: self.label_status.configure(text="🎉 Đã dịch hoàn tất 100%!", text_color="green"))
            self.after(0, lambda: messagebox.showinfo("Thành công", "Đã dịch hoàn tất toàn bộ nội dung!"))

        except Exception as e:
            self.after(0, lambda e=e: self.label_status.configure(text=f"Lỗi: {e}", text_color="red"))
            self.after(0, lambda e=e: messagebox.showerror("Lỗi dịch thuật", f"Có lỗi xảy ra trong quá trình dịch:\n{e}"))
        finally:
            self.after(0, lambda: self.btn_start_trans.configure(state="normal", text="⚡ Bắt đầu Dịch"))
            self.after(0, lambda: self.btn_stop_trans.configure(state="disabled"))

    def _update_trans_result(self, text):
        self.trans_result_output.delete("1.0", "end")
        self.trans_result_output.insert("1.0", text)

    def download_trans_srt(self, mode="translated"):
        text = self.trans_result_output.get("1.0", "end-1c").strip()
        if not text:
            messagebox.showwarning("Cảnh báo", "Chưa có nội dung bản dịch để lưu!")
            return

        if hasattr(self, "last_translated_srt_items") and self.last_translated_srt_items:
            content = build_srt(self.last_translated_srt_items, mode=mode)
        else:
            if not is_srt_content(text):
                messagebox.showwarning("Cảnh báo", "Bản dịch hiện tại không phải định dạng SRT. Vui lòng chọn 'Lưu TXT'!")
                return
            content = text

        suffix = "_bilingual.srt" if mode == "bilingual" else "_vi.srt"
        out_file = filedialog.asksaveasfilename(
            title="Lưu file phụ đề SRT",
            defaultextension=".srt",
            initialfile=f"subtitles{suffix}",
            filetypes=[("SRT Subtitles", "*.srt")]
        )
        if out_file:
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(content)
            messagebox.showinfo("Thành công", f"Đã lưu file SRT tại:\n{out_file}")

    def download_trans_ass(self, mode="translated"):
        text = self.trans_result_output.get("1.0", "end-1c").strip()
        if not text:
            messagebox.showwarning("Cảnh báo", "Chưa có nội dung bản dịch để lưu!")
            return

        if hasattr(self, "last_translated_srt_items") and self.last_translated_srt_items:
            content = build_ass(self.last_translated_srt_items, mode=mode)
        else:
            items = parse_srt(text)
            if not items:
                messagebox.showwarning("Cảnh báo", "Không thể tạo file ASS từ văn bản này (cần định dạng phụ đề SRT có timestamp)!")
                return
            content = build_ass(items, mode=mode)

        out_file = filedialog.asksaveasfilename(
            title="Lưu file phụ đề .ASS nền đen che sub gốc",
            defaultextension=".ass",
            initialfile="subtitles_blackbox.ass",
            filetypes=[("Advanced SubStation Alpha", "*.ass")]
        )
        if out_file:
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(content)
            messagebox.showinfo("Thành công", f"Đã lưu file ASS nền đen che sub gốc tại:\n{out_file}")

    def download_trans_txt(self):
        text = self.trans_result_output.get("1.0", "end-1c").strip()
        if not text:
            messagebox.showwarning("Cảnh báo", "Chưa có nội dung bản dịch để lưu!")
            return

        out_file = filedialog.asksaveasfilename(
            title="Lưu file văn bản dịch (.txt)",
            defaultextension=".txt",
            initialfile="ban_dich.txt",
            filetypes=[("Text Files", "*.txt")]
        )
        if out_file:
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(text)
            messagebox.showinfo("Thành công", f"Đã lưu file TXT tại:\n{out_file}")

    def send_trans_to_tts(self):
        text = self.trans_result_output.get("1.0", "end-1c").strip()
        if not text:
            messagebox.showwarning("Cảnh báo", "Chưa có bản dịch nào để nạp sang Tab TTS!")
            return

        # Clean SRT formatting if user translates SRT but wants TTS
        if is_srt_content(text):
            items = parse_srt(text)
            plain_text = "\n\n".join([it.original_text for it in items if it.original_text])
        else:
            plain_text = text

        self.text_input.delete("1.0", "end")
        self.text_input.insert("1.0", plain_text)
        self.tabview.set("Tạo TTS Cơ Bản")
        messagebox.showinfo("Thành công", "Đã nạp toàn bộ văn bản dịch sang Tab 'Tạo TTS Cơ Bản' để tạo giọng đọc!")

    def send_trans_to_srt(self):
        import tempfile
        text = self.trans_result_output.get("1.0", "end-1c").strip()
        if not text:
            messagebox.showwarning("Cảnh báo", "Chưa có bản dịch nào để nạp sang Tab SRT!")
            return

        if not is_srt_content(text) and not (hasattr(self, "last_translated_srt_items") and self.last_translated_srt_items):
            messagebox.showwarning("Cảnh báo", "Bản dịch hiện tại là văn bản thông thường, không phải file SRT có timecode!\nVui lòng chọn 'Nạp vào Tab TTS'.")
            return

        if hasattr(self, "last_translated_srt_items") and self.last_translated_srt_items:
            srt_str = build_srt(self.last_translated_srt_items, mode="translated")
        else:
            srt_str = text

        temp_srt_path = os.path.join(tempfile.gettempdir(), f"translated_srt_{int(time.time())}.srt")
        with open(temp_srt_path, "w", encoding="utf-8") as f:
            f.write(srt_str)

        self.srt_path.set(temp_srt_path)
        self.tabview.set("Chèn SRT vào CapCut")
        messagebox.showinfo("Thành công", f"Đã nạp file phụ đề dịch sang Tab 'Chèn SRT vào CapCut'!\nĐường dẫn tạm:\n{temp_srt_path}")

    # =========================================================================
    # Tab 6: Vocal Separation (CapCut Cloud API PRO) Event Handlers
    # =========================================================================

    def on_vocal_cookie_changed(self, *args):
        cookie = self.vocal_cookie_var.get().strip() if hasattr(self, "vocal_cookie_var") else ""
        save_pro_cookie(cookie)
        if hasattr(self, "vocal_account_status"):
            if not cookie:
                self.vocal_account_status.configure(
                    text="⚪ Đã xóa Cookie (Đang dùng chế độ Miễn phí)",
                    text_color="gray",
                )
            else:
                self.vocal_account_status.configure(
                    text="💾 Đã tự động lưu Cookie vào tệp cấu hình",
                    text_color="#38bdf8",
                )

    def clear_vocal_cookie_gui(self):
        if hasattr(self, "vocal_cookie_var"):
            self.vocal_cookie_var.set("")
        save_pro_cookie("")
        if hasattr(self, "vocal_account_status"):
            self.vocal_account_status.configure(
                text="⚪ Đã xóa Cookie (Đang dùng chế độ Miễn phí)",
                text_color="gray",
            )

    def verify_vocal_cookie_gui(self):
        cookie = self.vocal_cookie_input.get().strip()
        if not cookie:
            messagebox.showwarning("Cảnh báo", "Vui lòng nhập Cookie hoặc sessionid trước khi kiểm tra!")
            return

        self.btn_verify_cookie.configure(state="disabled", text="Đang kiểm tra...")
        self.vocal_account_status.configure(text="⏳ Đang kết nối máy chủ CapCut để xác thực...", text_color="gray")

        def _worker():
            res = verify_pro_cookie(cookie)

            def _update_ui():
                self.btn_verify_cookie.configure(state="normal", text="🔍 Kiểm tra tài khoản")
                if res.get("valid"):
                    save_pro_cookie(cookie)
                    msg = f"✅ {res.get('message')}"
                    self.vocal_account_status.configure(text=msg, text_color="#10b981")
                    messagebox.showinfo("Tài khoản hợp lệ", f"Đã xác thực thành công tài khoản CapCut PRO:\n\n{res.get('message')}")
                else:
                    msg = f"❌ {res.get('message')}"
                    self.vocal_account_status.configure(text=msg, text_color="#ef4444")
                    messagebox.showerror("Xác thực thất bại", f"{res.get('message')}\n\nVui lòng kiểm tra lại Cookie/SessionID từ capcut.com!")

            self.after(0, _update_ui)

        threading.Thread(target=_worker, daemon=True).start()

    def select_vocal_media(self):
        file_path = filedialog.askopenfilename(
            title="Chọn tệp Video hoặc Âm thanh để tách giọng",
            filetypes=[
                (
                    "Tất cả tệp media",
                    "*.mp4 *.mov *.mkv *.avi *.flv *.webm *.mp3 *.wav *.m4a *.aac *.flac *.ogg",
                ),
                ("Video", "*.mp4 *.mov *.mkv *.avi *.flv *.webm"),
                ("Audio", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"),
                ("Tất cả files", "*.*"),
            ],
        )
        if file_path:
            self.vocal_media_path.set(file_path)
            if not self.vocal_out_dir.get().strip():
                self.vocal_out_dir.set(str(Path(file_path).parent))
            self.vocal_status_label.configure(
                text=f"Đã chọn: {Path(file_path).name}", text_color="white"
            )

    def select_vocal_out_dir(self):
        dir_path = filedialog.askdirectory(title="Chọn thư mục xuất kết quả tách giọng")
        if dir_path:
            self.vocal_out_dir.set(dir_path)

    def open_vocal_output_dir(self):
        target = self.vocal_out_dir.get().strip()
        if not target and self.vocal_media_path.get().strip():
            target = str(Path(self.vocal_media_path.get().strip()).parent)
        if target and os.path.exists(target):
            os.startfile(target)
        else:
            messagebox.showwarning("Cảnh báo", "Thư mục đầu ra chưa được tạo hoặc không tồn tại!")

    def on_start_vocal_separation(self):
        media_p = self.vocal_media_path.get().strip()
        if not media_p or not os.path.exists(media_p):
            messagebox.showwarning("Cảnh báo", "Vui lòng chọn tệp video hoặc âm thanh hợp lệ!")
            return

        cookie = self.vocal_cookie_input.get().strip()
        if cookie:
            save_pro_cookie(cookie)

        out_d = self.vocal_out_dir.get().strip()
        if not out_d:
            out_d = str(Path(media_p).parent)
            self.vocal_out_dir.set(out_d)

        # Mode
        mode_val = self.combo_vocal_mode.get()
        if "Chỉ lấy Giọng nói" in mode_val:
            mode = "vocal"
        elif "Chỉ lấy Nhạc nền" in mode_val:
            mode = "instrumental"
        else:
            mode = "both"

        # Format
        fmt_val = self.combo_vocal_format.get()
        out_fmt = "wav" if "WAV" in fmt_val else "mp3"

        # Chunk duration (1 - 15 minutes)
        chunk_val = self.combo_vocal_chunk_dur.get().strip()
        try:
            min_val = int(chunk_val.split(" ")[0])
            min_val = min(max(min_val, 1), 15)
            chunk_sec = min_val * 60
        except Exception:
            chunk_sec = 600

        # Threads
        num_threads = int(self.slider_threads_vocal.get()) if hasattr(self, "slider_threads_vocal") else 3

        # Remux video flag
        remux_video = self.vocal_remux_video.get() if hasattr(self, "vocal_remux_video") else False

        self.vocal_is_running = True
        self.vocal_cancelled = False

        self.btn_start_vocal.configure(state="disabled")
        self.btn_cancel_vocal.configure(state="normal")
        self.vocal_progressbar.set(0.0)
        self.vocal_status_label.configure(
            text="Đang khởi động tác vụ tách giọng trên CapCut Cloud...", text_color="white"
        )

        threading.Thread(
            target=self.vocal_separation_thread,
            args=(media_p, out_d, mode, out_fmt, chunk_sec, num_threads, cookie, remux_video),
            daemon=True,
        ).start()

    def on_cancel_vocal_separation(self):
        if self.vocal_is_running:
            self.vocal_cancelled = True
            self.vocal_status_label.configure(
                text="Đang huỷ tác vụ, vui lòng chờ...", text_color="#f59e0b"
            )

    def vocal_separation_thread(
        self,
        media_path: str,
        output_dir: str,
        mode: str,
        out_format: str,
        chunk_sec: int,
        num_threads: int,
        cookie: str,
        remux_video: bool = False,
    ):
        try:
            separator = CapCutVocalSeparator(cookie=cookie)

            def progress_cb(info):
                pct = info.get("percent", 0.0) / 100.0
                msg = info.get("status", "")

                def _ui():
                    self.vocal_progressbar.set(pct)
                    self.vocal_status_label.configure(text=msg, text_color="white")

                self.after(0, _ui)

            def cancel_check():
                return self.vocal_cancelled

            results = separator.separate_media(
                input_path=media_path,
                output_dir=output_dir,
                mode=mode,
                out_format=out_format,
                chunk_duration_sec=chunk_sec,
                concurrency=num_threads,
                remux_video=remux_video,
                progress_callback=progress_cb,
                cancel_check=cancel_check,
            )

            def _success_ui():
                self.vocal_progressbar.set(1.0)
                status_text = (
                    "✅ Tách giọng và ghép video hoàn tất thành công 100%!"
                    if "video" in results
                    else "✅ Tách giọng hoàn tất thành công 100%!"
                )
                self.vocal_status_label.configure(
                    text=status_text, text_color="#10b981"
                )
                res_lines = [f"- {k.capitalize()}: {Path(v).name}" for k, v in results.items()]
                res_msg = "\n".join(res_lines)
                title = "Tách Giọng & Ghép Video Hoàn Tất" if "video" in results else "Tách Giọng Hoàn Tất"
                messagebox.showinfo(
                    title,
                    f"Đã hoàn thành tác vụ với CapCut Cloud API!\n\nThư mục lưu:\n{output_dir}\n\nTệp đã tạo:\n{res_msg}",
                )

            self.after(0, _success_ui)

        except Exception as exc:
            err_text = str(exc)

            def _err_ui():
                if self.vocal_cancelled:
                    self.vocal_status_label.configure(
                        text="⏹ Đã huỷ tác vụ bởi người dùng.", text_color="gray"
                    )
                else:
                    self.vocal_status_label.configure(
                        text=f"❌ Lỗi: {err_text}", text_color="#ef4444"
                    )
                    messagebox.showerror("Lỗi Tách Giọng CapCut", f"Đã xảy ra lỗi:\n{err_text}")

            self.after(0, _err_ui)

        finally:
            def _reset_ui():
                self.vocal_is_running = False
                self.vocal_cancelled = False
                self.btn_start_vocal.configure(state="normal")
                self.btn_cancel_vocal.configure(state="disabled")

            self.after(0, _reset_ui)


if __name__ == "__main__":
    app = CapCutTTSApp()
    app.mainloop()

