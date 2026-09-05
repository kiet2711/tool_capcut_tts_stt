"""
High-level Python API Client for CapCut Text-to-Speech (TTS) and Speech-to-Text (STT) tasks.
"""

import concurrent.futures
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    requests = None

from capcut_tts_api.config import BASE_URL
from capcut_tts_api.exceptions import CapCutAPIError, CapCutError, CapCutTaskError
from capcut_tts_api.models import DeviceConfig, SubtitleResult, UploadResult, Utterance, VoiceInfo, Word
from capcut_tts_api.signer import (
    base_headers,
    compact_json,
    common_query,
    escape_xml,
    make_sign_header,
    make_tts_payload_sign,
)
from capcut_tts_api.uploader import VODUploader


def get_media_duration(file_path: Union[str, Path]) -> float:
    """Get media duration in seconds via ffprobe or ffmpeg."""
    path_str = str(file_path)
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    # 1. Try ffprobe
    try:
        res = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path_str,
            ],
            capture_output=True,
            text=True,
            startupinfo=startupinfo,
            timeout=15,
        )
        if res.returncode == 0 and res.stdout.strip():
            val = float(res.stdout.strip())
            if val > 0:
                return val
    except Exception:
        pass

    # 2. Fallback to ffmpeg -i
    try:
        res = subprocess.run(
            ["ffmpeg", "-i", path_str],
            capture_output=True,
            text=True,
            startupinfo=startupinfo,
            timeout=15,
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", res.stderr)
        if match:
            h = int(match.group(1))
            m = int(match.group(2))
            s = float(match.group(3))
            return h * 3600 + m * 60 + s
    except Exception:
        pass

    return 0.0


def extract_audio_chunk(
    source_path: Union[str, Path],
    start_sec: float,
    duration_sec: float,
    output_path: Union[str, Path],
) -> str:
    """
    Extract a slice of media and compress to lightweight 64kbps mono MP3 (24000Hz).
    """
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(start_sec),
        "-t",
        str(duration_sec),
        "-i",
        str(source_path),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-b:a",
        "64k",
        "-ac",
        "1",
        "-ar",
        "24000",
        str(output_path),
    ]
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        startupinfo=startupinfo,
        timeout=300,
    )
    if res.returncode != 0 or not os.path.exists(output_path):
        raise CapCutError(f"Lỗi trích xuất phân đoạn âm thanh (FFmpeg): {res.stderr}")
    return str(output_path)


def compress_to_lightweight_audio(
    source_path: Union[str, Path],
    output_path: Union[str, Path],
) -> str:
    """
    Compress media to lightweight 64kbps mono MP3 (24000Hz) for fast STT upload.
    """
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-b:a",
        "64k",
        "-ac",
        "1",
        "-ar",
        "24000",
        str(output_path),
    ]
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        startupinfo=startupinfo,
        timeout=600,
    )
    if res.returncode != 0 or not os.path.exists(output_path):
        raise CapCutError(f"Lỗi tối ưu âm thanh STT (FFmpeg): {res.stderr}")
    return str(output_path)


def _checked_json_response(resp: Any, label: str) -> Dict[str, Any]:
    try:
        data = resp.json()
    except Exception as exc:
        raise CapCutAPIError(
            f"{label} returned non-JSON HTTP {resp.status_code}: {resp.text[:500]}",
            status_code=resp.status_code,
        ) from exc
    if resp.status_code >= 400:
        raise CapCutAPIError(
            f"{label} HTTP {resp.status_code}: {data}",
            status_code=resp.status_code,
            response_data=data,
        )
    return data


class CapCutClient:
    """
    Main CapCut API client for generating TTS and transcribing STT audio/video files.
    """

    def __init__(
        self,
        device: Optional[Union[DeviceConfig, Dict[str, Any], str, Path]] = None,
        session: Optional[Any] = None,
    ):
        """
        Initialize CapCutClient.

        :param device: Device configuration instance, dictionary, or path to device.json file.
        :param session: Optional requests.Session instance for connection pooling.
        """
        if isinstance(device, DeviceConfig):
            self.device = device
        elif isinstance(device, (str, Path)):
            self.device = DeviceConfig.from_json_file(device)
        elif isinstance(device, dict):
            self.device = DeviceConfig.from_dict(device)
        else:
            self.device = DeviceConfig()

        if session:
            self.session = session
        elif requests:
            self.session = requests.Session()
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            
            # Add automatic retry for network errors and 5xx responses
            retry_strategy = Retry(
                total=5,  # Maximum number of retries
                backoff_factor=2,  # Wait 2s, 4s, 8s... between retries
                status_forcelist=[500, 502, 503, 504],
                allowed_methods=["HEAD", "GET", "OPTIONS", "POST"] # Crucial: Must allow POST
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)
        else:
            self.session = None
    # -------------------------------------------------------------------------
    # Voice Resolution Helper
    # -------------------------------------------------------------------------

    def resolve_voice(
        self,
        voice: Optional[str] = None,
        resource_id: Optional[str] = None,
        catalog_path: Optional[Union[str, Path]] = None,
    ) -> Tuple[str, str]:
        """
        Resolve voice_type and resource_id from Voice.json catalog or explicit inputs.

        :param voice: Voice type string (e.g. 'BV421_vivn_streaming', 'BV074_streaming') or display name
        :param resource_id: Optional explicit voice resource ID string
        :return: Tuple of (voice_type, resource_id)
        """
        default_voice = "BV074_streaming"
        default_resource_id = "7102355709945188865"

        target_voice = voice or default_voice
        target_res = resource_id

        # Try lookup in Voice.json catalog
        all_voices = self.list_voices(catalog_path=catalog_path)
        target_lower = target_voice.lower().strip() if target_voice else ""

        # 1. Primary match: voice_type
        for v in all_voices:
            if v.voice_type.lower() == target_lower:
                return v.voice_type, target_res or v.resource_id

        # 2. Secondary match: display_name or resource_id
        for v in all_voices:
            if v.display_name.lower() == target_lower or (target_res and v.resource_id == target_res):
                return v.voice_type, target_res or v.resource_id

        # Fallback to provided values or defaults
        resolved_voice = target_voice
        resolved_res = target_res or default_resource_id
        return resolved_voice, resolved_res

    # -------------------------------------------------------------------------
    # Request Payload Builders (Dry-run capable)
    # -------------------------------------------------------------------------

    def build_tts_new_request(
        self,
        texts: Union[str, List[str]],
        voice: Optional[str] = "BV074_streaming",
        resource_id: Optional[str] = None,
        rate: str = "1.0",
    ) -> Tuple[str, Dict[str, str], str]:
        """
        Build URL, headers, and body string for creating a new TTS task.
        Automatically resolves resource_id for voice character if omitted.
        """
        if isinstance(texts, str):
            text_list = [texts]
        else:
            text_list = list(texts)

        if not text_list:
            raise ValueError("texts must contain at least one string")

        voice_type, final_resource_id = self.resolve_voice(voice=voice, resource_id=resource_id)

        device_dict = self.device.to_dict()
        babi = {
            "feature_entrance": "editor",
            "feature_entrance_detail": "editor-feature-text_to_speech",
            "feature_key": "text_to_speech",
            "scenario": "video_editor",
        }
        voice_blocks = []
        for text in text_list:
            voice_blocks.append(
                f'    <voice name="{voice_type}" mock_tone_info="" platform="sami" '
                f'resource_id="{final_resource_id}" emotion="" emotion_scale="0" style="" role="" '
                f'moyin_emotion="" is_clone_tone="false" need_subtitle_timestamp="false">\n'
                f'        <prosody rate="{rate}">{escape_xml(text)}</prosody>\n'
                f'    </voice>'
            )
        ssml = (
            '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">\n'
            + "\n".join(voice_blocks)
            + "\n</speak>"
        )
        extra_info = compact_json({"benefit_info": {}})
        payload = {
            "audio_format": "mp3",
            "babi_param": compact_json(babi),
            "credit_disable": False,
            "extra_info": extra_info,
            "need_merge_voice": False,
            "need_subtitle_timestamp": False,
            "scene": "text_to_speech",
            "ssml": ssml,
        }
        payload["sign"] = make_tts_payload_sign(
            ssml, extra_info, device_dict["device_id"], device_dict["aid"]
        )
        body = {
            "bind_id": str(uuid.uuid4()),
            "can_queue": True,
            "enter_from": "text_to_speech",
            "tasks": [
                {
                    "context": str(uuid.uuid4()),
                    "payload": compact_json(payload),
                    "req_key": "sami_text_to_speech",
                    "task_version": "v3",
                }
            ],
        }

        body_text = compact_json(body)
        path = "/lv/v1/common_task/new"
        query = common_query(device_dict, babi, include_region=True)
        url = BASE_URL + path + "?" + urlencode(query)
        headers = base_headers(device_dict, body_text, appid=True)
        lower_headers = {k.lower(): v for k, v in headers.items()}
        if "sign" not in lower_headers:
            headers["sign"] = make_sign_header(
                url, device_dict["appvr"], lower_headers["device-time"], device_dict["tdid"]
            )
        return url, headers, body_text

    def build_stt_new_request(
        self,
        audio_vid: str,
        audio_md5: str,
        duration_ms: int = 10000,
        language: str = "zh-CN",
        translation_language: str = "vi-VN",
        use_translation: bool = False,
    ) -> Tuple[str, Dict[str, str], str]:
        """
        Build URL, headers, and body string for creating a new STT task.
        """
        device_dict = self.device.to_dict()
        babi = {
            "feature_entrance": "editor",
            "feature_entrance_detail": "editor-elements-captions-subtitle_recognition",
            "feature_key": "subtitle_recognition",
            "scenario": "video_editor",
        }
        cap_json = {
            "adjust_endtime": 200,
            "audio": audio_vid,
            "audio_type": "vid",
            "caption_type": 0,
            "client_request_id": str(uuid.uuid4()),
            "duration": int(duration_ms),
            "enable_cache": True,
            "enter_from": "asr",
            "language": language,
            "max_lines": 1,
            "md5": audio_md5,
            "pack_options": {"need_attribute": True},
            "songs_info": [
                {"end_time": float(duration_ms) - 10.334, "id": "", "start_time": 0}
            ],
            "translation_language": translation_language,
            "use_translation": bool(use_translation),
            "words_per_line": 15,
        }
        body = {
            "bind_id": str(uuid.uuid4()).upper(),
            "can_queue": True,
            "enter_from": "asr",
            "tasks": [
                {
                    "context": str(uuid.uuid4()),
                    "payload": compact_json({"cap_json": cap_json}),
                    "req_key": "cc_audio_subtitle_asr",
                    "task_version": "v3",
                }
            ],
        }

        body_text = compact_json(body)
        path = "/lv/v1/common_task/new"
        query = common_query(device_dict, babi, include_region=True)
        url = BASE_URL + path + "?" + urlencode(query)
        headers = base_headers(device_dict, body_text, appid=False)
        lower_headers = {k.lower(): v for k, v in headers.items()}
        if "sign" not in lower_headers:
            headers["sign"] = make_sign_header(
                url, device_dict["appvr"], lower_headers["device-time"], device_dict["tdid"]
            )
        return url, headers, body_text

    def build_query_request(
        self,
        task_id: str,
        token: str,
        mode: str = "tts",
        bind_id: str = "",
    ) -> Tuple[str, Dict[str, str], str]:
        """
        Build URL, headers, and body string for querying a task.
        :param mode: "tts" or "stt"
        """
        req_key = (
            "sami_text_to_speech"
            if mode in ("tts", "tts-query")
            else "cc_audio_subtitle_asr"
        )
        device_dict = self.device.to_dict()
        body = {
            "tasks": [
                {
                    "bind_id": bind_id,
                    "id": task_id,
                    "req_key": req_key,
                    "task_version": "v3",
                    "token": token,
                }
            ]
        }
        body_text = compact_json(body)
        path = "/lv/v1/common_task/query"
        query = common_query(device_dict, None, include_region=False)
        url = BASE_URL + path + "?" + urlencode(query)
        headers = base_headers(
            device_dict, body_text, appid=(mode in ("tts", "tts-query"))
        )
        lower_headers = {k.lower(): v for k, v in headers.items()}
        if "sign" not in lower_headers:
            headers["sign"] = make_sign_header(
                url, device_dict["appvr"], lower_headers["device-time"], device_dict["tdid"]
            )
        return url, headers, body_text

    # -------------------------------------------------------------------------
    # Core API Execution Methods
    # -------------------------------------------------------------------------

    def create_tts_task(
        self,
        texts: Union[str, List[str]],
        voice: Optional[str] = "BV074_streaming",
        resource_id: Optional[str] = None,
        rate: str = "1.0",
    ) -> Dict[str, Any]:
        """
        Submit a new Text-to-Speech (TTS) synthesis task to CapCut.
        """
        if self.session is None:
            raise CapCutError("The 'requests' package is required. Run 'pip install requests'.")
        url, headers, body_text = self.build_tts_new_request(texts, voice, resource_id, rate)
        resp = self.session.post(url, headers=headers, data=body_text.encode("utf-8"), timeout=15)
        return _checked_json_response(resp, "create_tts_task")

    def query_tts_task(
        self, task_id: str, token: str, bind_id: str = ""
    ) -> Dict[str, Any]:
        """
        Query TTS task status by task_id and token.
        """
        if self.session is None:
            raise CapCutError("The 'requests' package is required. Run 'pip install requests'.")
        url, headers, body_text = self.build_query_request(
            task_id, token, mode="tts", bind_id=bind_id
        )
        resp = self.session.post(url, headers=headers, data=body_text.encode("utf-8"), timeout=15)
        return _checked_json_response(resp, "query_tts_task")

    def generate_speech(
        self,
        texts: Union[str, List[str]],
        voice: Optional[str] = "BV074_streaming",
        resource_id: Optional[str] = None,
        rate: str = "1.0",
        wait: bool = True,
        poll_interval: float = 0.5,
        timeout: float = 15.0,
        max_retries: int = 3,
        status_callback: Optional[callable] = None,
    ) -> Dict[str, Any]:
        """
        Convenience method: Submits TTS task and polls until completed.
        Includes fast timeout, automatic device rotation and retries.
        """
        last_exc = None
        for attempt in range(max_retries):
            try:
                create_res = self.create_tts_task(texts, voice, resource_id, rate)
                if not wait:
                    return create_res

                tasks = (create_res.get("data") or {}).get("tasks") or []
                if not tasks:
                    raise CapCutTaskError(f"No task returned from API: {create_res}")

                task_id = tasks[0]["id"]
                token = tasks[0]["token"]

                start_time = time.time()
                while time.time() - start_time < timeout:
                    query_res = self.query_tts_task(task_id, token)
                    query_tasks = (query_res.get("data") or {}).get("tasks") or []
                    if query_tasks:
                        task_item = query_tasks[0]
                        status = task_item.get("status")
                        if status_callback:
                            if status_callback(status) is False:
                                raise CapCutTaskError("Đã huỷ bởi người dùng.")
                        if status in ("success", "succeed"):
                            return query_res
                        elif status == "failed":
                            err_msg = query_res.get("message") or task_item.get("message") or "CapCut rejected text"
                            raise CapCutTaskError(f"TTS Task failed: {err_msg}")
                    else:
                        msg = query_res.get("message") or str(query_res)
                        if status_callback:
                            status_callback(f"Lỗi truy vấn: {msg}")
                    time.sleep(poll_interval)

                raise CapCutTaskError(f"TTS Task timed out after {timeout} seconds")
            except Exception as exc:
                last_exc = exc
                if "Đã huỷ bởi người dùng" in str(exc):
                    raise exc
                # Randomize device and retry
                self.device.randomize()
                time.sleep(0.8 * (attempt + 1))

        raise CapCutError(f"Failed to generate speech after {max_retries} attempts: {last_exc}")

    def upload_audio(self, file_path: Union[str, Path]) -> UploadResult:
        """
        Upload audio or video file to VOD space.
        """
        uploader = VODUploader(self.device, session=self.session)
        return uploader.upload_file(file_path)

    def create_stt_task(
        self,
        audio_vid: str,
        audio_md5: str,
        duration_ms: int = 10000,
        language: str = "zh-CN",
        translation_language: str = "vi-VN",
        use_translation: bool = False,
    ) -> Dict[str, Any]:
        """
        Submit Speech-to-Text task using pre-uploaded media vid and md5.
        """
        if self.session is None:
            raise CapCutError("The 'requests' package is required. Run 'pip install requests'.")
        url, headers, body_text = self.build_stt_new_request(
            audio_vid, audio_md5, duration_ms, language, translation_language, use_translation
        )
        resp = self.session.post(url, headers=headers, data=body_text.encode("utf-8"), timeout=60)
        return _checked_json_response(resp, "create_stt_task")

    def query_stt_task(
        self, task_id: str, token: str, bind_id: str = ""
    ) -> Dict[str, Any]:
        """
        Query STT task status by task_id and token.
        """
        if self.session is None:
            raise CapCutError("The 'requests' package is required. Run 'pip install requests'.")
        url, headers, body_text = self.build_query_request(
            task_id, token, mode="stt", bind_id=bind_id
        )
        resp = self.session.post(url, headers=headers, data=body_text.encode("utf-8"), timeout=60)
        return _checked_json_response(resp, "query_stt_task")

    def transcribe_file(
        self,
        file_path: Union[str, Path],
        language: str = "zh-CN",
        translation_language: str = "vi-VN",
        use_translation: bool = False,
        wait: bool = True,
        poll_interval: float = 2.0,
        timeout: float = 900.0,
    ) -> Dict[str, Any]:
        """
        Upload media file, create STT task, and optionally poll for completion.
        """
        upload_res = self.upload_audio(file_path)
        duration_ms = upload_res.duration_ms or 10000

        stt_res = self.create_stt_task(
            audio_vid=upload_res.vid,
            audio_md5=upload_res.md5,
            duration_ms=duration_ms,
            language=language,
            translation_language=translation_language,
            use_translation=use_translation,
        )

        if not wait:
            return stt_res

        tasks = (stt_res.get("data") or {}).get("tasks") or []
        if not tasks:
            raise CapCutTaskError(f"No STT task returned from API: {stt_res}")

        task_id = tasks[0]["id"]
        token = tasks[0]["token"]

        start_time = time.time()
        while time.time() - start_time < timeout:
            query_res = self.query_stt_task(task_id, token)
            query_tasks = (query_res.get("data") or {}).get("tasks") or []
            if query_tasks:
                status = query_tasks[0].get("status")
                if status in ("success", "succeed"):
                    return query_res
                elif status == "failed":
                    raise CapCutTaskError(f"STT Task failed: {query_res}")
            time.sleep(poll_interval)

        raise CapCutTaskError(f"STT Task timed out after {timeout} seconds")

    def extract_subtitles(self, query_response: Dict[str, Any]) -> SubtitleResult:
        """
        Extract and parse subtitles from an STT query response payload.
        """
        try:
            tasks = (query_response.get("data") or {}).get("tasks") or []
            if not tasks:
                return SubtitleResult()
            raw_payload = tasks[0].get("payload", "{}")
            if isinstance(raw_payload, str):
                payload_dict = json.loads(raw_payload)
            else:
                payload_dict = raw_payload
            return SubtitleResult.from_payload(payload_dict)
        except Exception as exc:
            raise CapCutError(f"Failed to parse subtitle payload: {exc}") from exc

    def transcribe_large_media(
        self,
        media_path: Union[str, Path],
        language: str = "vi-VN",
        translation_language: str = "vi-VN",
        use_translation: bool = False,
        chunk_duration_sec: int = 600,
        concurrency: int = 3,
        progress_callback: Optional[callable] = None,
        cancel_check: Optional[callable] = None,
    ) -> SubtitleResult:
        """
        High-performance Large Media Transcriber with Auto-Chunking, Multi-threading & Timeline Stitching.
        Supports seamless large file STT recognition (1-3 hours) without timeouts or VOD size errors.
        """
        if cancel_check and cancel_check():
            raise CapCutError("Đã huỷ bởi người dùng.")

        media_path = Path(media_path)
        if not media_path.exists():
            raise CapCutError(f"Không tìm thấy file: {media_path}")

        if progress_callback:
            progress_callback({
                "phase": "probing",
                "progress": 0.05,
                "message": "Đang kiểm tra thông tin file và thời lượng âm thanh..."
            })

        total_duration = get_media_duration(media_path)
        file_size = os.path.getsize(media_path)
        is_small_file = (total_duration > 0 and total_duration <= chunk_duration_sec and file_size <= 25 * 1024 * 1024)

        # Case 1: Small file (<= 10 mins and <= 25MB) -> compress to 64k mono MP3 and transcribe directly
        if is_small_file:
            if progress_callback:
                progress_callback({
                    "phase": "compressing",
                    "progress": 0.15,
                    "message": "File nhỏ (<10 phút), đang tối ưu âm thanh (64k mono)..."
                })

            temp_mp3 = media_path.parent / f"stt_opt_{int(time.time())}_{uuid.uuid4().hex[:6]}.mp3"
            try:
                compress_to_lightweight_audio(media_path, temp_mp3)
                target_file = temp_mp3 if temp_mp3.exists() else media_path

                if progress_callback:
                    progress_callback({
                        "phase": "uploading",
                        "progress": 0.30,
                        "message": "Đang tải file âm thanh lên CapCut Cloud..."
                    })

                result = None
                last_err = None
                for attempt in range(3):
                    if cancel_check and cancel_check():
                        raise CapCutError("Đã huỷ bởi người dùng.")
                    try:
                        worker_client = CapCutClient()
                        upload_res = worker_client.upload_audio(target_file)

                        if progress_callback:
                            progress_callback({
                                "phase": "creating_task",
                                "progress": 0.50,
                                "message": "Đang khởi tạo tác vụ nhận diện giọng nói (STT)..."
                            })

                        stt_task = worker_client.create_stt_task(
                            audio_vid=upload_res.vid,
                            audio_md5=upload_res.md5,
                            duration_ms=upload_res.duration_ms or int(total_duration * 1000) or 10000,
                            language=language,
                            translation_language=translation_language,
                            use_translation=use_translation,
                        )
                        tasks = (stt_task.get("data") or {}).get("tasks") or []
                        if not tasks:
                            raise CapCutError(f"Không nhận được task từ API: {stt_task}")
                        task_id = tasks[0]["id"]
                        token = tasks[0]["token"]

                        start_poll = time.time()
                        while time.time() - start_poll < 300:
                            if cancel_check and cancel_check():
                                raise CapCutError("Đã huỷ bởi người dùng.")
                            elapsed = int(time.time() - start_poll)
                            pct = min(0.95, 0.50 + (elapsed / 60) * 0.40)
                            if progress_callback:
                                progress_callback({
                                    "phase": "polling",
                                    "progress": pct,
                                    "message": f"Máy chủ đang nhận diện giọng nói... ({elapsed}s)"
                                })
                            q = worker_client.query_stt_task(task_id, token)
                            q_tasks = (q.get("data") or {}).get("tasks") or []
                            if q_tasks:
                                status = q_tasks[0].get("status")
                                if status in ("success", "succeed"):
                                    result = worker_client.extract_subtitles(q)
                                    break
                                elif status == "failed":
                                    raise CapCutError("CapCut báo lỗi xử lý thất bại (file không có giọng nói hoặc định dạng lỗi).")
                            time.sleep(2.0)

                        if result:
                            break
                        raise CapCutError("Quá thời gian chờ phản hồi STT (5 phút).")
                    except Exception as exc:
                        last_err = exc
                        if cancel_check and cancel_check():
                            raise CapCutError("Đã huỷ bởi người dùng.")
                        time.sleep(1.0)

                if result is None:
                    raise CapCutError(f"Nhận diện STT thất bại sau 3 lần thử: {last_err}")
                return result

            finally:
                if temp_mp3.exists():
                    try:
                        temp_mp3.unlink()
                    except Exception:
                        pass

        # Case 2: Large file -> Auto-chunking + Multi-threaded Concurrency Pool
        session_dir = media_path.parent / f"stt_chunks_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        session_dir.mkdir(parents=True, exist_ok=True)

        try:
            effective_duration = total_duration if total_duration > 0 else 3600.0
            num_chunks = max(1, math.ceil(effective_duration / chunk_duration_sec))

            if progress_callback:
                progress_callback({
                    "phase": "chunking",
                    "progress": 0.10,
                    "message": f"Tệp dài {int(effective_duration // 60)} phút. Đang cắt thành {num_chunks} phân đoạn (10 phút/đoạn)..."
                })

            # Step 1: Slice chunks
            chunk_info_list = []
            for i in range(num_chunks):
                if cancel_check and cancel_check():
                    raise CapCutError("Đã huỷ bởi người dùng.")
                start_sec = i * chunk_duration_sec
                dur_sec = min(chunk_duration_sec, effective_duration - start_sec)
                out_path = session_dir / f"chunk_{i:03d}.mp3"
                extract_audio_chunk(media_path, start_sec, dur_sec, out_path)
                chunk_info_list.append({
                    "index": i,
                    "start_sec": start_sec,
                    "dur_sec": dur_sec,
                    "file_path": out_path
                })

            if progress_callback:
                progress_callback({
                    "phase": "transcribing_pool",
                    "progress": 0.15,
                    "message": f"Đã cắt xong {num_chunks} đoạn. Đang khởi chạy {min(concurrency, num_chunks)} luồng nhận diện song song..."
                })

            # Step 2: Multi-threaded pool
            chunk_results = [None] * num_chunks
            completed_count = 0
            lock = threading.Lock()

            def process_single_chunk(chunk_item):
                nonlocal completed_count
                idx = chunk_item["index"]
                c_path = chunk_item["file_path"]
                dur_ms = int(chunk_item["dur_sec"] * 1000)

                for attempt in range(3):
                    if cancel_check and cancel_check():
                        raise CapCutError("Đã huỷ bởi người dùng.")
                    try:
                        worker_dev = DeviceConfig()
                        worker_dev.randomize()
                        worker_client = CapCutClient(device=worker_dev)
                        upload_res = worker_client.upload_audio(c_path)

                        stt_task = worker_client.create_stt_task(
                            audio_vid=upload_res.vid,
                            audio_md5=upload_res.md5,
                            duration_ms=upload_res.duration_ms or dur_ms,
                            language=language,
                            translation_language=translation_language,
                            use_translation=use_translation,
                        )
                        tasks = (stt_task.get("data") or {}).get("tasks") or []
                        if not tasks:
                            raise CapCutError(f"Không nhận được task từ API: {stt_task}")
                        task_id = tasks[0]["id"]
                        token = tasks[0]["token"]

                        start_poll = time.time()
                        while time.time() - start_poll < 300:
                            if cancel_check and cancel_check():
                                raise CapCutError("Đã huỷ bởi người dùng.")
                            q = worker_client.query_stt_task(task_id, token)
                            q_tasks = (q.get("data") or {}).get("tasks") or []
                            if q_tasks:
                                status = q_tasks[0].get("status")
                                if status in ("success", "succeed"):
                                    sub_res = worker_client.extract_subtitles(q)
                                    chunk_results[idx] = sub_res
                                    break
                                elif status == "failed":
                                    raise CapCutError("CapCut báo lỗi nhận diện thất bại.")
                            time.sleep(2.0)

                        if chunk_results[idx] is not None:
                            break
                    except Exception as e:
                        if cancel_check and cancel_check():
                            raise CapCutError("Đã huỷ bởi người dùng.")
                        time.sleep(1.0)

                with lock:
                    completed_count += 1
                    pct = min(0.95, 0.15 + (completed_count / num_chunks) * 0.80)
                    if progress_callback:
                        progress_callback({
                            "phase": "processing",
                            "completed": completed_count,
                            "total": num_chunks,
                            "progress": pct,
                            "message": f"Đang nhận diện đa luồng: Đã xong {completed_count}/{num_chunks} đoạn ({int(completed_count / num_chunks * 100)}%)..."
                        })

            active_workers = min(concurrency, num_chunks)
            with concurrent.futures.ThreadPoolExecutor(max_workers=active_workers) as executor:
                futures = [executor.submit(process_single_chunk, chunk) for chunk in chunk_info_list]
                for fut in concurrent.futures.as_completed(futures):
                    if cancel_check and cancel_check():
                        executor.shutdown(wait=False, cancel_futures=True)
                        raise CapCutError("Đã huỷ bởi người dùng.")
                    fut.result()

            # Step 3: Timeline Offset Stitching
            if progress_callback:
                progress_callback({
                    "phase": "stitching",
                    "progress": 0.96,
                    "message": "Đang tổng hợp và đồng bộ mốc thời gian phụ đề..."
                })

            all_utterances = []
            full_text_parts = []

            for i, chunk in enumerate(chunk_info_list):
                res = chunk_results[i]
                if not res or not res.utterances:
                    continue
                offset_ms = int(chunk["start_sec"] * 1000)

                for ut in res.utterances:
                    start_ms = ut.start_time + offset_ms
                    end_ms = ut.end_time + offset_ms
                    words = [
                        Word(
                            text=w.text,
                            start_time=w.start_time + offset_ms,
                            end_time=w.end_time + offset_ms,
                            blank_duration=w.blank_duration,
                        )
                        for w in ut.words
                    ]
                    all_utterances.append(
                        Utterance(
                            text=ut.text,
                            start_time=start_ms,
                            end_time=end_ms,
                            words=words,
                        )
                    )
                    if ut.text and ut.text.strip():
                        full_text_parts.append(ut.text.strip())

            return SubtitleResult(
                utterances=all_utterances,
                full_text=" ".join(full_text_parts),
            )

        finally:
            if session_dir.exists():
                try:
                    shutil.rmtree(session_dir, ignore_errors=True)
                except Exception:
                    pass

    def list_voices(
        self, lang: Optional[str] = None, catalog_path: Optional[Union[str, Path]] = None
    ) -> List[VoiceInfo]:
        """
        List available CapCut TTS voices from catalog file.
        """
        path = catalog_path or Path(__file__).parent.parent / "Voice.json"
        voices = VoiceInfo.load_catalog(path)
        if lang:
            return [v for v in voices if v.lang.lower() == lang.lower() or v.lan.lower() == lang.lower()]
        return voices
