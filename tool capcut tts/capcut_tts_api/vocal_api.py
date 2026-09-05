"""
CapCut Cloud API Vocal and Instrumental Separation Module (for CapCut PRO accounts).
Uses CapCut's official backend endpoints (/lv/v1/common_task/new, vc_sound_separate)
with PRO account authentication (Cookie / sessionid) and smart chunking to handle media > 15 minutes.
"""

import concurrent.futures
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import urlencode

import requests

from capcut_tts_api.client import CapCutClient
from capcut_tts_api.config import BASE_URL
from capcut_tts_api.exceptions import CapCutAPIError, CapCutError, CapCutTaskError
from capcut_tts_api.models import DeviceConfig, UploadResult
from capcut_tts_api.signer import base_headers, compact_json, common_query, make_sign_header
from capcut_tts_api.uploader import VODUploader


COOKIE_CONFIG_FILE = Path(__file__).resolve().parent.parent / "capcut_pro_cookie.json"


def save_pro_cookie(cookie_str: str) -> None:
    """Save user PRO cookie to persistent configuration file."""
    data = {"cookie": cookie_str.strip(), "updated_at": int(time.time())}
    with open(COOKIE_CONFIG_FILE, "w", encoding="utf-8") as fp:
        json.dump(data, fp, indent=2, ensure_ascii=False)


def load_pro_cookie() -> str:
    """Load saved PRO cookie from persistent configuration file."""
    if COOKIE_CONFIG_FILE.exists():
        try:
            with open(COOKIE_CONFIG_FILE, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                return data.get("cookie", "").strip()
        except Exception:
            return ""
    return ""


def format_cookie_header(raw_cookie: str) -> str:
    """Format raw cookie string or sessionid into a standard HTTP Cookie header string."""
    raw = raw_cookie.strip()
    if not raw:
        return ""
    if "=" not in raw:
        # User entered just the sessionid value
        return f"sessionid={raw}; sessionid_ss={raw}"
    return raw


def verify_pro_cookie(cookie_str: str) -> Dict[str, Any]:
    """
    Verify CapCut account login status and validity using user's Cookie.
    Returns dict: {'valid': bool, 'user_id': int, 'username': str, 'message': str, 'raw': dict}
    """
    cookie = format_cookie_header(cookie_str)
    if not cookie:
        return {
            "valid": False,
            "user_id": 0,
            "username": "",
            "message": "Cookie trống. Vui lòng nhập Cookie hoặc sessionid của bạn.",
            "raw": {},
        }

    url = "https://passport-api-sg.capcut.com/passport/account/info/v2/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Cookie": cookie,
        "Accept": "application/json",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        account_data = data.get("data", {})
        user_id = account_data.get("user_id", 0)
        error_code = account_data.get("error_code")

        if resp.status_code == 200 and user_id != 0 and (error_code is None or error_code == 0):
            username = account_data.get("name") or account_data.get("screen_name") or f"User_{user_id}"
            return {
                "valid": True,
                "user_id": user_id,
                "username": username,
                "message": f"Tài khoản hợp lệ: {username} (ID: {user_id})",
                "raw": account_data,
            }
        else:
            desc = account_data.get("description") or data.get("message") or "Session không hợp lệ hoặc đã hết hạn"
            return {
                "valid": False,
                "user_id": 0,
                "username": "",
                "message": f"Không thể xác thực tài khoản: {desc}",
                "raw": data,
            }
    except Exception as exc:
        return {
            "valid": False,
            "user_id": 0,
            "username": "",
            "message": f"Lỗi kết nối máy chủ xác thực: {exc}",
            "raw": {},
        }


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
            encoding="utf-8",
            errors="replace",
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
            encoding="utf-8",
            errors="replace",
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


def extract_audio_slice(
    source_path: Union[str, Path],
    start_sec: float,
    duration_sec: float,
    output_path: Union[str, Path],
    sample_rate: int = 44100,
) -> str:
    """Extract a slice of media to 16-bit stereo WAV with high quality."""
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
        "pcm_s16le",
        "-ac",
        "2",
        "-ar",
        str(sample_rate),
        str(output_path),
    ]
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        startupinfo=startupinfo,
        timeout=600,
    )
    if res.returncode != 0 or not os.path.exists(output_path):
        raise CapCutError(f"Lỗi trích xuất phân đoạn âm thanh (FFmpeg): {res.stderr}")
    return str(output_path)


def stitch_audio_chunks(
    chunk_files: List[Union[str, Path]],
    output_path: Union[str, Path],
    out_format: str = "mp3",
) -> str:
    """
    Stitch multiple audio chunks into a single audio file using FFmpeg concat filter.
    """
    if not chunk_files:
        raise ValueError("Danh sách file chunk trống, không thể ghép nối.")

    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    if len(chunk_files) == 1:
        if out_format.lower() == "mp3":
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(chunk_files[0]),
                "-acodec",
                "libmp3lame",
                "-b:a",
                "320k",
                str(output_path),
            ]
        else:
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(chunk_files[0]),
                "-acodec",
                "pcm_s16le",
                str(output_path),
            ]
        subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            startupinfo=startupinfo,
            check=True,
        )
        return str(output_path)

    # Multi-chunk concat
    list_file = None
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8") as tf:
            list_file = tf.name
            for cf in chunk_files:
                escaped = Path(cf).as_posix()
                tf.write(f"file '{escaped}'\n")

        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_file,
        ]
        if out_format.lower() == "mp3":
            cmd.extend(["-c:a", "libmp3lame", "-b:a", "320k"])
        else:
            cmd.extend(["-c:a", "pcm_s16le"])
        cmd.append(str(output_path))

        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            startupinfo=startupinfo,
            timeout=900,
        )
        if res.returncode != 0 or not os.path.exists(output_path):
            raise CapCutError(f"Lỗi ghép nối các phân đoạn âm thanh (FFmpeg): {res.stderr}")
        return str(output_path)
    finally:
        if list_file and os.path.exists(list_file):
            try:
                os.remove(list_file)
            except Exception:
                pass


def download_remote_file(
    url: str,
    dest_path: Union[str, Path],
    timeout: int = 60,
    progress_callback: Optional[Callable[[float], None]] = None,
) -> str:
    """Download a file from URL with chunked streaming and optional progress callback."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        total_size = int(resp.headers.get("content-length", 0))
        downloaded = 0
        dest_p = Path(dest_path)
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        with open(dest_p, "wb") as fp:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    fp.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total_size > 0:
                        progress_callback(downloaded / total_size)
    return str(dest_path)


class CapCutVocalSeparator:
    """
    Client for interacting with CapCut Cloud Vocal Separation API.
    Requires CapCut PRO account Cookie/SessionID.
    """

    def __init__(
        self,
        cookie: str = "",
        client: Optional[CapCutClient] = None,
    ):
        self.cookie = format_cookie_header(cookie or load_pro_cookie())
        self.client = client or CapCutClient()
        self.device = self.client.device

    def create_vocal_task(
        self,
        audio_vid: str,
        duration_ms: int,
        separate_type: int = 2,
    ) -> Tuple[str, str, str]:
        """
        Submit a new vocal separation task to CapCut API.
        :param audio_vid: VOD vid from pre-uploaded audio.
        :param duration_ms: Audio duration in milliseconds.
        :param separate_type: 2 for Keep Vocals, 1 for Keep Instrumental/Beat.
        :return: Tuple of (task_id, token, bind_id)
        """
        device_dict = self.device.to_dict()
        context_id = str(uuid.uuid4())
        bind_id = str(uuid.uuid4()).upper()

        babi = {
            "feature_entrance": "editor",
            "feature_entrance_detail": "editor-audio-vocal_separation",
            "feature_key": "audio_vocal_separation",
            "scenario": "video_editor",
        }

        # Normalize separate_type to CapCut API strings ("human" or "others")
        sep_str = "human"
        if separate_type in (1, "1", "others", "instrumental", "beat"):
            sep_str = "others"
        elif separate_type in (2, "2", "human", "vocal"):
            sep_str = "human"

        inner_payload = {
            "material_list": [
                {
                    "id": context_id,
                    "type": "audio",
                    "source": audio_vid,
                    "source_type": "vid_origin",
                    "format": {
                        "input": "wav",
                        "output": "wav",
                    },
                }
            ],
            "separate_type": sep_str,
            "audio_duration": int(duration_ms),
            "enable_trim": False,
        }

        body = {
            "bind_id": bind_id,
            "can_queue": True,
            "enter_from": "vc_sound_separate",
            "tasks": [
                {
                    "context": context_id,
                    "payload": compact_json(inner_payload),
                    "req_key": "vc_sound_separate",
                    "type": 6,
                }
            ],
        }

        body_text = compact_json(body)
        path = "/lv/v1/common_task/new"
        query = common_query(device_dict, babi, include_region=True)
        url = BASE_URL + path + "?" + urlencode(query)

        headers = base_headers(device_dict, body_text, appid=False)
        if self.cookie:
            headers["cookie"] = self.cookie

        lower_headers = {k.lower(): v for k, v in headers.items()}
        if "sign" not in lower_headers:
            headers["sign"] = make_sign_header(
                url, device_dict["appvr"], lower_headers["device-time"], device_dict["tdid"]
            )

        resp = self.client.session.post(
            url, headers=headers, data=body_text.encode("utf-8"), timeout=30
        )
        res_json = resp.json()

        ret = str(res_json.get("ret", ""))
        errmsg = res_json.get("errmsg", "")

        if ret != "0":
            if "param err" in errmsg or ret == "1000":
                raise CapCutAPIError(
                    f"CapCut từ chối tạo tác vụ tách giọng (Mã: {ret} - {errmsg}). "
                    "Tính năng tách giọng đòi hỏi tài khoản CapCut PRO. "
                    "Vui lòng kiểm tra lại Cookie/SessionID của tài khoản PRO đã nhập!",
                    status_code=resp.status_code,
                    response_data=res_json,
                )
            raise CapCutAPIError(
                f"Lỗi tạo tác vụ tách giọng CapCut: {errmsg} (ret: {ret})",
                status_code=resp.status_code,
                response_data=res_json,
            )

        tasks = (res_json.get("data") or {}).get("tasks") or []
        if not tasks:
            raise CapCutTaskError(f"Máy chủ không trả về task_id: {res_json}")

        task_id = tasks[0]["id"]
        token = tasks[0]["token"]
        return task_id, token, bind_id

    def query_vocal_task(
        self,
        task_id: str,
        token: str,
        bind_id: str,
    ) -> Dict[str, Any]:
        """Query task status from CapCut API."""
        device_dict = self.device.to_dict()
        body = {
            "tasks": [
                {
                    "bind_id": bind_id,
                    "id": task_id,
                    "req_key": "vc_sound_separate",
                    "token": token,
                    "type": 6,
                }
            ]
        }
        body_text = compact_json(body)
        path = "/lv/v1/common_task/query"
        query = common_query(device_dict, None, include_region=False)
        url = BASE_URL + path + "?" + urlencode(query)

        headers = base_headers(device_dict, body_text, appid=False)
        if self.cookie:
            headers["cookie"] = self.cookie

        lower_headers = {k.lower(): v for k, v in headers.items()}
        if "sign" not in lower_headers:
            headers["sign"] = make_sign_header(
                url, device_dict["appvr"], lower_headers["device-time"], device_dict["tdid"]
            )

        resp = self.client.session.post(
            url, headers=headers, data=body_text.encode("utf-8"), timeout=30
        )
        return resp.json()

    def process_single_chunk(
        self,
        chunk_wav_path: str,
        duration_sec: float,
        mode: str = "both",
        temp_dir: Optional[str] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, str]:
        """
        Process separation of a single WAV chunk (< 15 mins) using CapCut API.
        Returns dict: {'vocal': path, 'instrumental': path}
        """
        if cancel_check and cancel_check():
            raise CapCutError("Đã huỷ bởi người dùng.")

        duration_ms = max(int(duration_sec * 1000), 1000)

        # 1. Upload chunk WAV to CapCut VOD
        if progress_callback:
            progress_callback("Đang tải âm thanh lên máy chủ CapCut...")
        upload_res = self.client.upload_audio(chunk_wav_path)
        vid = upload_res.vid

        if cancel_check and cancel_check():
            raise CapCutError("Đã huỷ bởi người dùng.")

        tmp_d = Path(temp_dir or tempfile.mkdtemp())
        tmp_d.mkdir(parents=True, exist_ok=True)
        base_name = Path(chunk_wav_path).stem

        result_files: Dict[str, str] = {}

        # Mode definitions:
        # separate_type = 2: Keep Vocals
        # separate_type = 1: Keep Instrumental
        targets = []
        if mode in ("both", "vocal"):
            targets.append(("vocal", "human"))
        if mode in ("both", "instrumental"):
            targets.append(("instrumental", "others"))

        for stem_name, sep_type in targets:
            if cancel_check and cancel_check():
                raise CapCutError("Đã huỷ bởi người dùng.")

            stem_label = "Giọng nói (Vocal)" if stem_name == "vocal" else "Nhạc nền (Beat)"
            if progress_callback:
                progress_callback(f"Đang gửi yêu cầu tách {stem_label}...")

            task_id, token, bind_id = self.create_vocal_task(
                audio_vid=vid,
                duration_ms=duration_ms,
                separate_type=sep_type,
            )

            # Polling task result
            timeout = 300.0
            start_t = time.time()
            download_url = None

            while time.time() - start_t < timeout:
                if cancel_check and cancel_check():
                    raise CapCutError("Đã huỷ bởi người dùng.")

                q_res = self.query_vocal_task(task_id, token, bind_id)
                tasks = (q_res.get("data") or {}).get("tasks") or []
                if tasks:
                    t_item = tasks[0]
                    status = t_item.get("status")
                    if progress_callback:
                        progress_callback(f"Đang xử lý {stem_label} trên Cloud ({status})...")

                    if status in ("success", "succeed"):
                        payload_str = t_item.get("payload", "")
                        try:
                            pl = json.loads(payload_str) if isinstance(payload_str, str) else payload_str
                            # Extract download_url from payload
                            if isinstance(pl, dict):
                                mat_list = pl.get("material_list") or []
                                if mat_list and isinstance(mat_list, list) and len(mat_list) > 0:
                                    download_url = mat_list[0].get("source") or mat_list[0].get("url")
                                if not download_url:
                                    download_url = pl.get("download_url") or pl.get("url")
                                if not download_url:
                                    sep_audios = pl.get("separated_audios") or []
                                    if sep_audios and isinstance(sep_audios, list) and len(sep_audios) > 0:
                                        download_url = sep_audios[0].get("url")
                        except Exception:
                            download_url = t_item.get("download_url")

                        if not download_url:
                            # Try finding URL in raw response
                            urls = re.findall(r"https?://[^\s\"\'<>]+", json.dumps(q_res))
                            for u in urls:
                                if "download" in u or "tos" in u or ".wav" in u or ".mp3" in u or "sami-data-platform" in u:
                                    download_url = u
                                    break

                        if download_url:
                            break
                        else:
                            raise CapCutTaskError(f"Hoàn thành nhưng không tìm thấy link tải về: {q_res}")
                    elif status == "failed":
                        err_code = t_item.get("err_code")
                        err_msg = (
                            t_item.get("err_msg")
                            or t_item.get("detail_info")
                            or t_item.get("message")
                            or q_res.get("errmsg")
                            or "Lỗi tách giọng trên máy chủ"
                        )
                        code_str = f" (Mã {err_code})" if err_code is not None else ""
                        raise CapCutTaskError(f"Tác vụ tách {stem_label} thất bại{code_str}: {err_msg}")

                time.sleep(2.0)

            if not download_url:
                raise CapCutTaskError(f"Quá thời gian chờ phản hồi tách {stem_label} từ máy chủ CapCut.")

            # Download separated stem file
            if progress_callback:
                progress_callback(f"Đang tải về {stem_label}...")

            out_chunk_wav = str(tmp_d / f"{base_name}_{stem_name}.wav")
            download_remote_file(download_url, out_chunk_wav)
            result_files[stem_name] = out_chunk_wav

        return result_files

    def separate_media(
        self,
        input_path: Union[str, Path],
        output_dir: Union[str, Path],
        mode: str = "both",
        out_format: str = "mp3",
        chunk_duration_sec: int = 600,
        concurrency: int = 3,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, str]:
        """
        Complete end-to-end multi-threaded pipeline:
        1. Probes file duration and determines chunking (CapCut 15-min limit safe).
        2. Slices media into WAV chunks.
        3. Processes chunks concurrently using ThreadPoolExecutor across CapCut Cloud API workers.
        4. Downloads resulting stems for each chunk.
        5. Stitches multi-chunk stems chronologically into seamless final media.
        6. Returns output file paths: {'vocal': ..., 'instrumental': ...}.
        """
        if cancel_check and cancel_check():
            raise CapCutError("Đã huỷ bởi người dùng.")

        input_p = Path(input_path)
        if not input_p.exists():
            raise FileNotFoundError(f"Không tìm thấy file nguồn: {input_p}")

        # Cookie is optional: CapCut Cloud API supports free/guest separation without cookie
        # If cookie is provided, it will be attached to requests for authenticated/PRO access

        out_d = Path(output_dir)
        out_d.mkdir(parents=True, exist_ok=True)

        def report(pct: float, msg: str):
            if progress_callback:
                progress_callback({"percent": pct, "status": msg})

        report(2.0, "Đang kiểm tra thời lượng âm thanh/video...")
        total_duration = get_media_duration(input_p)
        if total_duration <= 0:
            raise CapCutError("Không xác định được thời lượng tệp tin hoặc tệp không chứa âm thanh hợp lệ.")

        # CapCut hard ceiling is 15 minutes (900 seconds). Default chunk duration: 600s (10 mins).
        safe_chunk_sec = min(max(int(chunk_duration_sec), 60), 840)

        num_chunks = max(1, int((total_duration + safe_chunk_sec - 1) // safe_chunk_sec))
        work_dir = tempfile.mkdtemp(prefix="capcut_vocal_")

        vocal_chunks: List[Optional[str]] = [None] * num_chunks
        inst_chunks: List[Optional[str]] = [None] * num_chunks

        try:
            chunks_info = []
            for idx in range(num_chunks):
                start_sec = idx * safe_chunk_sec
                dur_sec = min(safe_chunk_sec, total_duration - start_sec)
                chunk_wav = str(Path(work_dir) / f"slice_{idx:03d}.wav")
                chunks_info.append({
                    "idx": idx,
                    "start_sec": start_sec,
                    "dur_sec": dur_sec,
                    "chunk_wav": chunk_wav,
                })

            active_workers = min(max(int(concurrency), 1), num_chunks)
            completed_count = 0
            lock = threading.Lock()

            if num_chunks > 1:
                report(4.0, f"Đang khởi tạo {active_workers} luồng xử lý song song cho {num_chunks} phân đoạn...")

            def process_chunk_task(c_info):
                nonlocal completed_count
                idx = c_info["idx"]
                start_sec = c_info["start_sec"]
                dur_sec = c_info["dur_sec"]
                chunk_wav = c_info["chunk_wav"]

                if cancel_check and cancel_check():
                    raise CapCutError("Đã huỷ bởi người dùng.")

                # 1. Trích xuất lát cắt âm thanh bằng FFmpeg
                extract_audio_slice(input_p, start_sec, dur_sec, chunk_wav)

                if cancel_check and cancel_check():
                    raise CapCutError("Đã huỷ bởi người dùng.")

                # 2. Xử lý qua CapCut Cloud API bằng worker separator độc lập
                worker_separator = CapCutVocalSeparator(cookie=self.cookie, client=CapCutClient())

                last_err = None
                for attempt in range(2):
                    if cancel_check and cancel_check():
                        raise CapCutError("Đã huỷ bởi người dùng.")
                    try:
                        res_stems = worker_separator.process_single_chunk(
                            chunk_wav_path=chunk_wav,
                            duration_sec=dur_sec,
                            mode=mode,
                            temp_dir=work_dir,
                            cancel_check=cancel_check,
                        )
                        if "vocal" in res_stems:
                            vocal_chunks[idx] = res_stems["vocal"]
                        if "instrumental" in res_stems:
                            inst_chunks[idx] = res_stems["instrumental"]
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        if cancel_check and cancel_check():
                            raise CapCutError("Đã huỷ bởi người dùng.")
                        time.sleep(1.5)

                if last_err:
                    raise CapCutError(f"Lỗi xử lý phân đoạn {idx+1}/{num_chunks}: {last_err}")

                # Dọn file slice trung gian
                if os.path.exists(chunk_wav):
                    try:
                        os.remove(chunk_wav)
                    except Exception:
                        pass

                with lock:
                    completed_count += 1
                    pct = 5.0 + (completed_count / num_chunks) * 82.0
                    msg = (
                        f"Đa luồng ({active_workers} luồng): Đã tách xong {completed_count}/{num_chunks} đoạn ({int(completed_count / num_chunks * 100)}%)..."
                        if num_chunks > 1
                        else "Đã hoàn thành tách giọng từ máy chủ..."
                    )
                    report(pct, msg)

            if num_chunks == 1:
                process_chunk_task(chunks_info[0])
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=active_workers) as executor:
                    futures = [executor.submit(process_chunk_task, c) for c in chunks_info]
                    for fut in concurrent.futures.as_completed(futures):
                        if cancel_check and cancel_check():
                            executor.shutdown(wait=False, cancel_futures=True)
                            raise CapCutError("Đã huỷ bởi người dùng.")
                        fut.result()

            if cancel_check and cancel_check():
                raise CapCutError("Đã huỷ bởi người dùng.")

            valid_vocals = [vocal_chunks[i] for i in range(num_chunks) if vocal_chunks[i]]
            valid_insts = [inst_chunks[i] for i in range(num_chunks) if inst_chunks[i]]

            if mode in ("both", "vocal") and len(valid_vocals) != num_chunks:
                raise CapCutError(f"Tách giọng nói bị thiếu: chỉ hoàn thành {len(valid_vocals)}/{num_chunks} đoạn.")
            if mode in ("both", "instrumental") and len(valid_insts) != num_chunks:
                raise CapCutError(f"Tách nhạc nền bị thiếu: chỉ hoàn thành {len(valid_insts)}/{num_chunks} đoạn.")

            report(88.0, "Đang ghép nối và hoàn thiện file âm thanh đầu ra...")

            base_stem = input_p.stem
            final_outputs: Dict[str, str] = {}
            ext = out_format.lower()

            if valid_vocals:
                report(92.0, "Đang xuất bản file Giọng Nói (Vocals)...")
                vocal_out = str(out_d / f"{base_stem}_vocals.{ext}")
                stitch_audio_chunks(valid_vocals, vocal_out, out_format=ext)
                final_outputs["vocal"] = vocal_out

            if valid_insts:
                report(96.0, "Đang xuất bản file Nhạc Nền (Beat)...")
                inst_out = str(out_d / f"{base_stem}_instrumental.{ext}")
                stitch_audio_chunks(valid_insts, inst_out, out_format=ext)
                final_outputs["instrumental"] = inst_out

            report(100.0, "Tách giọng bằng CapCut Cloud API thành công 100%!")
            return final_outputs

        finally:
            # Clean up temp folder
            try:
                import shutil
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                pass
