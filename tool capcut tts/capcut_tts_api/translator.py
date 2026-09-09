"""
AI Subtitle & Novel Translation Service using Gemini API.
Ports and enhances the complete translation engine from truyen-ngan:
- High-precision SRT parsing and serialization (SRT Vietnamese, Bilingual SRT, BlackBox ASS)
- Smart chunking: Gộp chunk lớn cho Gemini (tối thiểu request) & Băm nhỏ an toàn 16k TPM cho Gemma
- Context Overlap to prevent AI summarization and dropped lines
- Worker Pool with Round-Robin API Key distribution & Automatic Key Failover on Rate Limit / Invalid Key
"""

import concurrent.futures
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:
    requests = None
    HTTPAdapter = None


# Model display labels → actual Google API model IDs (identity — these ARE the real API names)
# Source: D:\truyen-ngan\index.html transModelSelect <option value="...">
MODEL_MAP = {
    "gemini-3.5-flash-lite (Hạn mức 500 RPD - Gộp 1 Request)": "gemini-3.5-flash-lite",
    "gemini-3.6-flash (Zhihu kịch tính - Gộp 1 Request)": "gemini-3.6-flash",
    "gemma-4-31b-it (14.400 RPD - Băm nhỏ an toàn 16k TPM)": "gemma-4-31b-it",
    "gemma-4-26b-a4b-it (14.400 RPD MoE - Tốc độ cao)": "gemma-4-26b-a4b-it",
    "gemini-3.7-flash (Thế hệ 3.7 - 20 RPD)": "gemini-3.7-flash",
    "gemini-3.1-flash-lite (15 RPM / 500 RPD)": "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite (Hạn mức cao: 15 RPM / 500 RPD / 250k TPM)": "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite": "gemini-3.1-flash-lite",
}

STYLE_PRESETS = {
    "✨ Tự Động AI (Auto)": "",
    "🎬 Phim Ngắn Zhihu": "Phim ngắn Zhihu vả mặt kịch tính, nhịp điệu dồn dập, sắc bén",
    "📖 Thuần Việt Văn Học": "Thuần Việt văn học trau chuốt, mượt mà, giàu cảm xúc, thoát ý",
    "⚔️ Cổ Trang Tiên Hiệp": "Cổ trang tiên hiệp, bảo lưu danh xưng và đại từ xưng hô Hán Việt",
    "😂 Hài Hước Bắt Trend": "Hài hước, dí dỏm, châm biếm, bắt trend giới trẻ",
}

CONCURRENCY_OPTIONS = [
    "🚀 Tự Động (Theo số lượng API Key)",
    "🚶 1 Luồng (Tuần tự - An toàn)",
    "⚡ 2 Luồng Song Song (x2 Tốc độ)",
    "⚡ 3 Luồng Song Song (x3 Tốc độ)",
    "⚡ 4 Luồng Song Song (x4 Tốc độ)",
    "⚡ 5 Luồng Song Song (Khuyên dùng)",
    "⚡ 6 Luồng Song Song",
    "⚡ 8 Luồng Song Song",
    "⚡ 10 Luồng Song Song (Siêu tốc)",
    "⚡ 12 Luồng Song Song",
    "⚡ 15 Luồng Song Song",
    "🔥 20 Luồng Song Song (Cực đại)",
]


def resolve_model_id(label_or_id: str) -> str:
    """Resolve user-selected label or raw model ID to an executable model name."""
    if not label_or_id:
        return "gemini-3.5-flash-lite"
    if label_or_id in MODEL_MAP:
        return MODEL_MAP[label_or_id]
    for k, v in MODEL_MAP.items():
        if label_or_id.lower() in k.lower():
            return v
    # Extract clean model identifier before space or parenthesis
    clean = re.split(r"[\s\(]", label_or_id.strip())[0].strip()
    if clean == "gemini-2.5-flash":
        return "gemini-3.6-flash"
    if clean == "gemini-2.5-flash-lite":
        return "gemini-3.1-flash-lite"
    return clean or "gemini-3.5-flash-lite"


def parse_concurrency_val(val: Union[str, int], key_count: int = 1) -> int:
    """Parse concurrency selection string into integer worker count."""
    if isinstance(val, int):
        return max(1, min(val, 20))
    s = str(val).lower().strip()
    if "tự động" in s or "auto" in s:
        return max(1, min(key_count, 5))
    nums = re.findall(r"\d+", s)
    if nums:
        return max(1, min(int(nums[0]), 20))
    return 3


@dataclass
class SrtItem:
    """Represents a single subtitle block in an SRT file."""
    id: int
    timecode: str
    original_text: str
    translated_text: str = ""


def normalize_single_time(t: str) -> str:
    """Normalize timestamp to standard HH:MM:SS,mmm format."""
    if not t or not isinstance(t, str):
        return "00:00:00,000"
    cleaned = t.strip().replace(".", ",")
    parts = cleaned.split(",")
    hms = parts[0] if len(parts) > 0 else ""
    ms_part = parts[1] if len(parts) > 1 else "000"
    ms = ms_part.ljust(3, "0")[:3]
    hms_parts = [int(p) if p.isdigit() else 0 for p in hms.split(":")]
    h, m, s = 0, 0, 0
    if len(hms_parts) >= 3:
        h, m, s = hms_parts[0], hms_parts[1], hms_parts[2]
    elif len(hms_parts) == 2:
        m, s = hms_parts[0], hms_parts[1]
    elif len(hms_parts) == 1:
        s = hms_parts[0]
    return f"{h:02d}:{m:02d}:{s:02d},{ms}"


def normalize_timecode(timecode: str) -> str:
    """Normalize full timecode string: 00:00:01,000 --> 00:00:04,000"""
    if not timecode or not isinstance(timecode, str):
        return ""
    parts = re.split(r"\s*-->\s*", timecode.strip())
    if len(parts) == 2:
        return f"{normalize_single_time(parts[0])} --> {normalize_single_time(parts[1])}"
    return timecode.strip()


def parse_srt(srt_content: str) -> List[SrtItem]:
    """
    Robust SRT parser:
    - Strips code blocks and AI preambles
    - Matches timecode lines with flexible regex anchors
    - Handles various ID formats (e.g. 1, 1., #1, [1], 1:)
    - Accurately captures multi-line subtitle text
    """
    if not srt_content or not isinstance(srt_content, str):
        return []

    cleaned = re.sub(r"```(?:srt)?", "", srt_content, flags=re.IGNORECASE)
    cleaned = cleaned.replace("```", "")
    normalized = cleaned.replace("\r\n", "\n").replace("\r", "\n").strip()

    tc_regex = re.compile(
        r"^[ \t]*(?:(?P<id_line>(?:\[|\#)?\d+(?:\]|\.|\:)?)[ \t]*\n[ \t]*)?"
        r"(?P<timecode>\d{1,2}:\d{1,2}:\d{1,2}(?:[,\.]\d{1,3})?\s*-->\s*\d{1,2}:\d{1,2}:\d{1,2}(?:[,\.]\d{1,3})?)",
        re.MULTILINE
    )

    matches = list(tc_regex.finditer(normalized))
    if not matches:
        blocks = re.split(r"\n\s*\n+", normalized)
        items = []
        for idx, block in enumerate(blocks):
            lines = [line.strip() for line in block.strip().split("\n") if line.strip()]
            if len(lines) >= 2:
                item_id = idx + 1
                tc_idx = 0
                clean_first = re.sub(r"[^\d]", "", lines[0])
                if clean_first:
                    item_id = int(clean_first)
                    tc_idx = 1
                if tc_idx < len(lines) and "-->" in lines[tc_idx]:
                    tc = normalize_timecode(lines[tc_idx])
                    txt = "\n".join(lines[tc_idx + 1:]).strip()
                    items.append(SrtItem(id=item_id, timecode=tc, original_text=txt, translated_text=""))
        return items

    items = []
    for i, m in enumerate(matches):
        start_pos = m.end()
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(normalized)

        raw_tc = m.group("timecode")
        timecode = normalize_timecode(raw_tc)

        id_line = m.group("id_line")
        if id_line:
            id_digits = re.sub(r"[^\d]", "", id_line)
            item_id = int(id_digits) if id_digits else i + 1
        else:
            pre_text = normalized[:m.start()].rstrip()
            pre_lines = pre_text.split("\n")
            if pre_lines and re.match(r"^[ \t]*(?:\[|\#)?\d+(?:\]|\.|\:)?[ \t]*$", pre_lines[-1]):
                id_digits = re.sub(r"[^\d]", "", pre_lines[-1])
                item_id = int(id_digits) if id_digits else i + 1
            else:
                item_id = i + 1

        text_block = normalized[start_pos:end_pos].strip()
        lines = [line.rstrip() for line in text_block.split("\n")]
        if i + 1 < len(matches) and lines and re.match(r"^[ \t]*(?:\[|\#)?\d+(?:\]|\.|\:)?[ \t]*$", lines[-1]):
            lines.pop()
        subtitle_text = "\n".join(lines).strip()

        items.append(
            SrtItem(
                id=item_id,
                timecode=timecode,
                original_text=subtitle_text,
                translated_text="",
            )
        )

    return items


def build_srt(items: List[SrtItem], mode: str = "translated") -> str:
    """
    Build valid SRT string from SrtItem list.
    :param mode: 'translated' (Vietnamese only), 'bilingual' (Vietnamese on top, original below), 'source' (original)
    """
    if not items:
        return ""

    blocks = []
    for idx, item in enumerate(items, 1):
        item_id = item.id if item.id else idx
        timecode = normalize_timecode(item.timecode)
        text = item.translated_text or item.original_text or ""

        if mode == "bilingual":
            if item.original_text and item.translated_text and item.original_text.strip() != item.translated_text.strip():
                text = f"{item.translated_text.strip()}\n{item.original_text.strip()}"
            else:
                text = item.translated_text or item.original_text
        elif mode == "source":
            text = item.original_text

        blocks.append(f"{item_id}\n{timecode}\n{text}")

    return "\n\n".join(blocks) + "\n"


def format_srt_time_to_ass(srt_time: str) -> str:
    """Convert SRT timestamp to ASS format: 00:01:23,456 -> 0:01:23.45"""
    if not srt_time or not isinstance(srt_time, str):
        return "0:00:00.00"
    cleaned = srt_time.strip().replace(".", ",")
    parts = cleaned.split(",")
    hms = parts[0] if len(parts) > 0 else ""
    ms_part = parts[1] if len(parts) > 1 else "000"
    ms_num = int(ms_part.ljust(3, "0")[:3]) if ms_part.isdigit() else 0
    centi = ms_num // 10
    centi_str = f"{centi:02d}"

    hms_parts = [int(p) if p.isdigit() else 0 for p in hms.split(":")]
    h, m, s = 0, 0, 0
    if len(hms_parts) >= 3:
        h, m, s = hms_parts[0], hms_parts[1], hms_parts[2]
    elif len(hms_parts) == 2:
        m, s = hms_parts[0], hms_parts[1]
    elif len(hms_parts) == 1:
        s = hms_parts[0]
    return f"{h}:{m:02d}:{s:02d}.{centi_str}"


def build_ass(items: List[SrtItem], mode: str = "translated") -> str:
    """
    Build ASS subtitle format with a solid black box style (Opaque Box)
    to cover up hardcoded original Chinese subtitles on videos.
    """
    if not items:
        return ""

    header = """[Script Info]
; Script generated by CapCut Translation Studio
Title: Vietnamese Subtitles with Black Box
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: BlackBox,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,3,14,0,2,30,30,95,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogues = []
    for item in items:
        text = item.translated_text or item.original_text or ""
        if mode == "bilingual":
            if item.original_text and item.translated_text and item.original_text.strip() != item.translated_text.strip():
                text = f"{item.translated_text.strip()}\\N{item.original_text.strip()}"
        ass_text = text.replace("\r\n", "\\N").replace("\n", "\\N").replace("\r", "\\N")

        timecode = normalize_timecode(item.timecode or "")
        time_parts = re.split(r"\s*-->\s*", timecode)
        start_ass = format_srt_time_to_ass(time_parts[0] if len(time_parts) > 0 else "")
        end_ass = format_srt_time_to_ass(time_parts[1] if len(time_parts) > 1 else "")
        dialogues.append(f"Dialogue: 0,{start_ass},{end_ass},BlackBox,,0,0,0,,{ass_text}")

    return header + "\n".join(dialogues) + "\n"


def is_srt_content(text: str) -> bool:
    """Check if string has SRT subtitle format (supports flexible formats like 0:0:1,000 or 00:00:01,000)."""
    if not text or not isinstance(text, str):
        return False
    return bool(re.search(r"\d{1,2}:\d{1,2}:\d{1,2}(?:[,\.]\d{1,3})?\s*-->\s*\d{1,2}:\d{1,2}:\d{1,2}(?:[,\.]\d{1,3})?", text))


def count_units(text: str) -> int:
    """Count text units (CJK characters + Latin words)."""
    if not text or not isinstance(text, str):
        return 0
    cjk_matches = re.findall(r"[\u4e00-\u9fa5\u3040-\u30ff\uac00-\ud7af]", text)
    cjk_count = len(cjk_matches)
    non_cjk = re.sub(r"[\u4e00-\u9fa5\u3040-\u30ff\uac00-\ud7af]", " ", text)
    words = len(non_cjk.strip().split())
    return cjk_count + words


def get_chunk_config(model_id: str, trans_type: str = "srt") -> Dict[str, Any]:
    """
    Get chunk size and rate-limit delay based on model architecture:
    - Gemini (Flash 3.5 / 3.6 / 3.1 / 3.7): Gộp chunk lớn (80 dòng SRT / 1.800 từ) để tối thiểu request.
    - Gemma (4 31B / 26B): Băm nhỏ an toàn (40 dòng SRT / 700 từ) để an toàn 16k TPM.
    """
    is_gemma = bool(model_id and "gemma" in model_id.lower())
    if trans_type == "srt":
        return {
            "chunk_size": 40 if is_gemma else 80,
            "delay_ms": 1200 if is_gemma else 1800,
            "strategy": "🛡️ Smart Chunking: Băm Nhỏ An Toàn 16k TPM (Gemma)" if is_gemma else "⚡ Smart Chunking: Gộp Chunk Lớn (Gemini)"
        }
    else:
        return {
            "chunk_size": 700 if is_gemma else 1800,
            "delay_ms": 1200 if is_gemma else 1800,
            "strategy": "🛡️ Smart Chunking: Băm Nhỏ An Toàn 16k TPM (Gemma)" if is_gemma else "⚡ Smart Chunking: Gộp Chunk Lớn (Gemini)"
        }


def chunk_srt_items(items: List[SrtItem], model_id: str = "gemini-3.5-flash-lite") -> Tuple[List[List[SrtItem]], Dict[str, Any]]:
    """Slice list of SRT items into batches according to model capability."""
    config = get_chunk_config(model_id, "srt")
    size = config["chunk_size"]
    chunks = []
    for i in range(0, len(items), size):
        chunks.append(items[i : i + size])
    return chunks, config


def chunk_raw_text(raw_text: str, model_id: str = "gemini-3.5-flash-lite") -> Tuple[List[str], Dict[str, Any]]:
    """Smart chunking for novel / raw text by paragraphs and sentences."""
    config = get_chunk_config(model_id, "novel")
    limit = config["chunk_size"]
    if not raw_text or not raw_text.strip():
        return [], config

    paragraphs = [p.strip() for p in raw_text.split("\n") if p.strip()]
    chunks = []
    current_chunk = []
    current_count = 0

    for para in paragraphs:
        para_units = count_units(para)
        if para_units > limit:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_count = 0
            # Split long paragraph by sentence boundaries
            sentences = re.split(r"([。！？\.\!\?\n]+)", para)
            temp_chunk = ""
            for s in range(0, len(sentences), 2):
                sent = (sentences[s] if s < len(sentences) else "") + (sentences[s + 1] if s + 1 < len(sentences) else "")
                sent_units = count_units(sent)
                if count_units(temp_chunk) + sent_units > limit and temp_chunk:
                    chunks.append(temp_chunk.strip())
                    temp_chunk = sent
                else:
                    temp_chunk += sent
            if temp_chunk.strip():
                chunks.append(temp_chunk.strip())
        elif current_count + para_units > limit and current_chunk:
            chunks.append("\n\n".join(current_chunk))
            current_chunk = [para]
            current_count = para_units
        else:
            current_chunk.append(para)
            current_count += para_units

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return [c for c in chunks if c.strip()], config


def get_translation_system_prompt(user_custom_style: str = "", trans_type: str = "srt") -> str:
    """Generate professional translation prompt based on style and format."""
    # Resolve preset style if user selected standard name
    style_text = STYLE_PRESETS.get(user_custom_style, user_custom_style).strip()

    name_and_language_rules = """QUY TẮC BẮT BUỘC VỀ TÊN NHÂN VẬT & TỪ NGỮ (CHỐNG LỖI NỬA VIỆT NỬA HÁN):
1. PHIÊN ÂM 100% SANG HÁN VIỆT: Toàn bộ họ tên nhân vật, tên riêng, biệt danh, địa danh, chức vụ, danh xưng BẮT BUỘC phải chuyển sang âm Hán Việt chuẩn mực hoàn chỉnh (Ví dụ: 余昭昭 -> Dư Chiêu Chiêu, 祝清梨 -> Chúc Thanh Lê, 沈砚白 -> Thẩm Nghiễn Bạch, 顾总 -> Cố tổng, 陆爷 -> Lục gia, 林浅 -> Lâm Thiển, 苏小姐 -> Tô tiểu thư, 李特助 -> trợ lý Lý/đặc trợ Lý...).
2. TUYỆT ĐỐI KHÔNG DỊCH NỬA VỜI: Nghiêm cấm tuyệt đối việc dịch một nửa tiếng Việt một nửa để lại chữ Hán (CẤM các dạng như 'Dư 昭昭', 'Chúc 清梨', 'Thẩm 砚白', 'Cố 总', 'Lục 爷'...).
3. 100% TIẾNG VIỆT THUẦN TÚY: Tuyệt đối không để sót bất kỳ ký tự chữ Hán (Chinese Hanzi) nào trong kết quả dịch. Toàn bộ chữ Hán đều phải được dịch nghĩa hoặc phiên âm Hán Việt tương ứng."""

    if style_text:
        style_guide = f"""PHONG CÁCH DỊCH THEO YÊU CẦU CỦA NGƯỜI DÙNG:
"{style_text}"
- Hãy bám sát và tuân thủ tuyệt đối phong cách dịch, giọng văn và yêu cầu trên.
- Dịch thoát nghĩa, trôi chảy, đúng sắc thái nhân vật và ngữ cảnh của câu chuyện."""
    else:
        style_guide = """TỰ ĐỘNG SUY LUẬN NGỮ CẢNH & THỂ LOẠI (AUTO-INFERENCE):
- Hãy đọc kỹ văn bản gốc để tự động nhận diện thể loại (phim ngắn Zhihu vả mặt, hiện đại đô thị, cổ trang tiên hiệp, hào môn thế gia, hài hước, ngôn tình...).
- Tự động điều chỉnh giọng văn cho phù hợp nhất: kịch tính dồn dập cho phim ngắn, mềm mại giàu cảm xúc cho ngôn tình, trang trọng khí thế cho tiên hiệp/cổ trang.
- Dịch thoát nghĩa, tự nhiên, thuần Việt, tuyệt đối không dịch thô kiểu "word-by-word" máy móc."""

    if trans_type == "srt":
        return f"""Bạn là chuyên gia dịch phụ đề video và phim ngắn Trung - Việt hàng đầu thế giới.
{style_guide}

{name_and_language_rules}

QUY TẮC BẮT BUỘC ĐỂ KHÔNG BỊ DỊCH THIẾU HOẶC MẤT DÒNG PHỤ ĐỀ:
1. TUYỆT ĐỐI BẢO TOÀN 100% CẤU TRÚC SRT: Đầu vào có bao nhiêu khối phụ đề (ID từ 1 đến N) thì đầu ra BẮT BUỘC PHẢI CÓ ĐỦ CHÍNH XÁC bấy nhiêu khối phụ đề.
2. Giữ nguyên số thứ tự ID và dòng Timecode. Dưới mỗi timecode là đúng 1 bản dịch tiếng Việt tương ứng.
3. KHÔNG gộp 2 khối phụ đề thành 1, KHÔNG bỏ qua bất kỳ khối phụ đề nào.
4. KHÔNG thêm lời chào, lời giải thích hay code block ngoài định dạng SRT chuẩn."""

    return f"""Bạn là chuyên gia dịch thuật văn học và tiểu thuyết Trung - Việt hàng đầu thế giới.
{style_guide}

{name_and_language_rules}

QUY TẮC BẮT BUỘC ĐỂ BẢN DỊCH KHÔNG BỊ THIẾU (CHỐNG TÓM TẮT):
1. DỊCH ĐẦY ĐỦ 100% TOÀN BỘ VĂN BẢN: Bắt buộc dịch trọn vẹn từng câu, từng đoạn từ đầu đến cuối. Tuyệt đối KHÔNG ĐƯỢC TÓM TẮT, KHÔNG ĐƯỢC CẮT BỚT, KHÔNG ĐƯỢC BỎ SÓT bất kỳ câu văn, lời thoại hay đoạn miêu tả nào dù là nhỏ nhất.
2. Giữ nguyên toàn bộ cấu trúc đoạn văn của bản gốc (đoạn nào dịch ra đoạn đó).
3. Dịch thoát nghĩa, câu từ mượt mà, thuần Việt, chuẩn ngữ pháp tiếng Việt.
4. KHÔNG thêm bất kỳ lời dẫn, ghi chú hay giải thích nào ngoài nội dung đã dịch."""


class GeminiTranslator:
    """
    High-performance AI Translation Engine ported directly from truyen-ngan:
    - Persistent HTTP connection pooling with requests.Session() (fast Keep-Alive)
    - Worker Pool architecture with dedicated worker IDs
    - Multi-Key Round-Robin & Instant Key Failover upon 429/403/401/400 errors
    - Smart Chunking & Context Overlap
    - Live SRT accumulation without un-translated source text flashing
    """

    def __init__(self, api_keys: Union[str, List[str]], model: str = "gemini-3.5-flash-lite"):
        if isinstance(api_keys, str):
            keys = [k.strip() for k in re.split(r"[,;\n\r]+", api_keys) if k.strip()]
        else:
            keys = [k.strip() for k in api_keys if k and k.strip()]
        self.api_keys = keys
        self.model = resolve_model_id(model)
        self.raw_model_name = model
        self.is_translating = False
        self.is_cancelled = False

        # Connection pooling for high performance (equivalent to browser HTTP/2 multiplexing)
        self.session = None
        if requests is not None:
            self.session = requests.Session()
            if HTTPAdapter is not None:
                adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25, max_retries=0)
                self.session.mount("https://", adapter)
                self.session.mount("http://", adapter)

    def close(self):
        """Close connection pool session."""
        if self.session:
            try:
                self.session.close()
            except Exception:
                pass

    def call_gemini_with_key(
        self,
        prompt: str,
        system_instruction: str,
        api_key: str,
        model: Optional[str] = None,
        timeout: int = 60,
    ) -> str:
        """
        Call Gemini REST API with HTTP Keep-Alive connection reuse.
        Fails fast on 429/403/400/401 to trigger instant key rotation in the worker.
        """
        if requests is None:
            raise RuntimeError("Thư viện 'requests' chưa được cài đặt. Vui lòng chạy 'pip install requests'.")

        effective_model = resolve_model_id(model or self.model)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{effective_model}:generateContent?key={api_key}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 8192,
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"},
            ],
        }
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        http_client = self.session if self.session else requests
        resp = http_client.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=timeout)

        if resp.status_code != 200:
            err_text = resp.text[:300]
            err = RuntimeError(f"Gemini API Error {resp.status_code}: {err_text}")
            err.status_code = resp.status_code
            raise err

        data = resp.json()
        prompt_feedback = data.get("promptFeedback", {})
        if prompt_feedback.get("blockReason"):
            raise RuntimeError(f"Gemini API chặn nội dung (blockReason: {prompt_feedback.get('blockReason')}).")

        candidate = data.get("candidates", [{}])[0] if data.get("candidates") else {}
        finish_reason = candidate.get("finishReason", "")
        if finish_reason == "SAFETY":
            raise RuntimeError("Gemini API chặn nội dung do bộ lọc an toàn (finishReason: SAFETY).")

        text = candidate.get("content", {}).get("parts", [{}])[0].get("text", "") if candidate.get("content") else ""
        if not text:
            raise ValueError(f"API không trả về nội dung dịch hợp lệ (finishReason: {finish_reason or 'UNKNOWN'}).")
        return text.strip()

    def translate_srt(
        self,
        srt_content_or_items: Union[str, List[SrtItem]],
        style: str = "",
        concurrency: Union[str, int] = "auto",
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> List[SrtItem]:
        """
        Translate SRT subtitles with Worker Pool & Multi-Key Instant Failover.
        """
        if not self.api_keys:
            raise ValueError("Chưa có Gemini API Key. Vui lòng nhập ít nhất 1 Key!")

        if isinstance(srt_content_or_items, str):
            items = parse_srt(srt_content_or_items)
        else:
            items = list(srt_content_or_items)

        if not items:
            return []

        self.is_translating = True
        self.is_cancelled = False

        chunks, config = chunk_srt_items(items, self.model)
        total_chunks = len(chunks)
        requested_concurrency = parse_concurrency_val(concurrency, len(self.api_keys))
        num_workers = min(requested_concurrency, total_chunks)

        system_prompt = get_translation_system_prompt(style, "srt")

        completed_chunks = 0
        next_chunk_index = 0
        lock = threading.Lock()

        tasks = []
        for idx, ch in enumerate(chunks):
            tasks.append({
                "chunk_index": idx,
                "chunk_items": ch,
                "prev_chunk_items": chunks[idx - 1] if idx > 0 else None,
            })

        def run_worker(worker_id: int):
            nonlocal next_chunk_index, completed_chunks
            while not self.is_cancelled and (not cancel_check or not cancel_check()):
                with lock:
                    if next_chunk_index >= len(tasks):
                        break
                    current_task_idx = next_chunk_index
                    next_chunk_index += 1

                task = tasks[current_task_idx]
                chunk_index = task["chunk_index"]
                chunk_items = task["chunk_items"]
                prev_chunk_items = task["prev_chunk_items"]

                # Key selection: round-robin across worker & task
                key_index = (worker_id + current_task_idx) % len(self.api_keys)
                current_key = self.api_keys[key_index]

                chunk_srt_text = build_srt(chunk_items, mode="source")

                # Context Overlap aligned 100% with truyen-ngan
                context_prefix = ""
                overlap_tcs = set()
                if prev_chunk_items and len(prev_chunk_items) > 0:
                    overlap_items = prev_chunk_items[-3:]
                    overlap_lines = "\n".join([f"{it.timecode}: {it.original_text}" for it in overlap_items if it.original_text])
                    overlap_tcs = {normalize_timecode(it.timecode) for it in overlap_items}
                    if overlap_lines:
                        context_prefix = f"[BỐI CẢNH 3 CÂU LIỀN TRƯỚC ĐỂ BẠN NẮM VỮNG ĐẠI TỪ XƯNG HÔ VÀ MẠCH CẢM XÚC - TUYỆT ĐỐI KHÔNG DỊCH LẠI CÁC CÂU NÀY]:\n{overlap_lines}\n\n"

                prompt = f"{context_prefix}[NỘI DUNG BẮT BUỘC DỊCH SANG TIẾNG VIỆT 100% CÁC KHỐI PHỤ ĐỀ DƯỚI ĐÂY (PHIÊN ÂM TẤT CẢ HỌ TÊN NHÂN VẬT SANG HÁN VIỆT HOÀN TOÀN, TUYỆT ĐỐI KHÔNG ĐỂ SÓT CHỮ HÁN)]:\n\n{chunk_srt_text}"

                if progress_callback:
                    with lock:
                        pct = completed_chunks / total_chunks
                        msg = f"[Luồng #{worker_id + 1}] Đang dịch phần {chunk_index + 1}/{total_chunks} ({len(chunk_items)} dòng)..." if num_workers > 1 else f"Đang dịch phần {chunk_index + 1}/{total_chunks}..."
                        progress_callback({
                            "status": "translating",
                            "worker_id": worker_id + 1,
                            "completed": completed_chunks,
                            "total": total_chunks,
                            "progress": pct,
                            "message": msg,
                        })

                success = False
                retry_attempts = 0
                max_retries = max(5, len(self.api_keys) + 1)

                while not success and retry_attempts < max_retries and not self.is_cancelled and (not cancel_check or not cancel_check()):
                    try:
                        trans_text = self.call_gemini_with_key(
                            prompt=prompt,
                            system_instruction=system_prompt,
                            api_key=current_key,
                            model=self.model,
                        )
                        parsed_trans = parse_srt(trans_text)

                        # Filter out translated overlap lines if model translated them anyway
                        chunk_tcs = {normalize_timecode(it.timecode) for it in chunk_items}
                        if overlap_tcs:
                            filtered_trans = [
                                pt for pt in parsed_trans
                                if not (normalize_timecode(pt.timecode) in overlap_tcs and normalize_timecode(pt.timecode) not in chunk_tcs)
                            ]
                            if filtered_trans:
                                parsed_trans = filtered_trans

                        # Reset translated_text on chunk_items before matching
                        for it in chunk_items:
                            it.translated_text = ""

                        # 4-tier subtitle matching:
                        # 1. Exact ID match (pt.id == item.id)
                        # 2. Local 1-based index match (pt.id == local_idx + 1)
                        # 3. Normalized timecode match
                        # 4. Sequential index fallback
                        used_pt_indices = set()

                        # Tier 1: exact ID match
                        for local_idx, item in enumerate(chunk_items):
                            for p_idx, pt in enumerate(parsed_trans):
                                if p_idx not in used_pt_indices and pt.id == item.id:
                                    used_pt_indices.add(p_idx)
                                    item.translated_text = pt.original_text or pt.translated_text
                                    break

                        # Tier 2: timecode match (normalize_timecode(item.timecode) == normalize_timecode(pt.timecode))
                        for local_idx, item in enumerate(chunk_items):
                            if item.translated_text:
                                continue
                            item_tc = normalize_timecode(item.timecode)
                            for p_idx, pt in enumerate(parsed_trans):
                                if p_idx not in used_pt_indices and normalize_timecode(pt.timecode) == item_tc:
                                    used_pt_indices.add(p_idx)
                                    item.translated_text = pt.original_text or pt.translated_text
                                    break

                        # Tier 3: local index match (pt.id == local_idx + 1)
                        for local_idx, item in enumerate(chunk_items):
                            if item.translated_text:
                                continue
                            for p_idx, pt in enumerate(parsed_trans):
                                if p_idx not in used_pt_indices and pt.id == local_idx + 1:
                                    used_pt_indices.add(p_idx)
                                    item.translated_text = pt.original_text or pt.translated_text
                                    break

                        # Tier 4: sequential fallback
                        unused_pt = [p_idx for p_idx in range(len(parsed_trans)) if p_idx not in used_pt_indices]
                        u_idx = 0
                        for local_idx, item in enumerate(chunk_items):
                            if item.translated_text:
                                continue
                            if u_idx < len(unused_pt):
                                pt = parsed_trans[unused_pt[u_idx]]
                                u_idx += 1
                                item.translated_text = pt.original_text or pt.translated_text

                        # Validation: check translated line ratio
                        translated_count = sum(1 for it in chunk_items if it.translated_text and it.translated_text.strip())
                        min_required = 1 if len(chunk_items) == 1 else math.ceil(len(chunk_items) * 0.5)
                        if translated_count < min_required:
                            for it in chunk_items:
                                it.translated_text = ""
                            raise RuntimeError(
                                f"Phần {chunk_index + 1} chỉ nhận diện được {translated_count}/{len(chunk_items)} dòng dịch. "
                                f"Tự động chuyển API key và dịch lại..."
                            )

                        with lock:
                            completed_chunks += 1
                            success = True
                            if progress_callback:
                                pct = completed_chunks / total_chunks
                                # Only output items that are translated so far (matches truyen-ngan, prevents Chinese flash)
                                translated_so_far = [it for it in items if it.translated_text]
                                cur_full_srt = build_srt(translated_so_far, mode="translated")
                                progress_callback({
                                    "status": "chunk_completed",
                                    "worker_id": worker_id + 1,
                                    "completed": completed_chunks,
                                    "total": total_chunks,
                                    "progress": pct,
                                    "accumulated_text": cur_full_srt,
                                    "message": f"[Đa luồng x{num_workers}] Đã xong {completed_chunks}/{total_chunks} phần ({len(translated_so_far)}/{len(items)} dòng)!",
                                })

                        if config["delay_ms"] > 0 and next_chunk_index < len(tasks):
                            time.sleep(max(0.1, config["delay_ms"] / 1000.0 / (num_workers if num_workers > 1 else 1.0)))

                    except Exception as err:
                        retry_attempts += 1
                        # Instant Key Failover: switch to next key immediately without long sleep
                        if len(self.api_keys) > 1:
                            key_index = (key_index + 1) % len(self.api_keys)
                            current_key = self.api_keys[key_index]

                        if retry_attempts >= max_retries:
                            raise RuntimeError(f"Luồng #{worker_id + 1} không thể dịch phần {chunk_index + 1}: {err}")

                        time.sleep(min(2.0, 0.5 * retry_attempts))

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(run_worker, w) for w in range(num_workers)]
            for fut in concurrent.futures.as_completed(futures):
                if cancel_check and cancel_check():
                    self.is_cancelled = True
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise RuntimeError("Đã huỷ bởi người dùng.")
                fut.result()

        self.is_translating = False
        return items

    def translate_text(
        self,
        raw_text: str,
        style: str = "",
        concurrency: Union[str, int] = "auto",
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> str:
        """
        Translate long novel / raw text with Worker Pool & Multi-Key Instant Failover.
        """
        if not self.api_keys:
            raise ValueError("Chưa có Gemini API Key. Vui lòng nhập ít nhất 1 Key!")

        if not raw_text or not raw_text.strip():
            return ""

        self.is_translating = True
        self.is_cancelled = False

        chunks, config = chunk_raw_text(raw_text, self.model)
        total_chunks = len(chunks)
        if total_chunks == 0:
            return ""

        requested_concurrency = parse_concurrency_val(concurrency, len(self.api_keys))
        num_workers = min(requested_concurrency, total_chunks)

        system_prompt = get_translation_system_prompt(style, "novel")

        translated_chunks = [None] * total_chunks
        completed_chunks = 0
        next_chunk_index = 0
        lock = threading.Lock()

        tasks = []
        for idx, ch in enumerate(chunks):
            tasks.append({
                "chunk_index": idx,
                "chunk_text": ch,
                "prev_chunk_text": chunks[idx - 1] if idx > 0 else None,
            })

        def run_worker(worker_id: int):
            nonlocal next_chunk_index, completed_chunks
            while not self.is_cancelled and (not cancel_check or not cancel_check()):
                with lock:
                    if next_chunk_index >= len(tasks):
                        break
                    current_task_idx = next_chunk_index
                    next_chunk_index += 1

                task = tasks[current_task_idx]
                chunk_index = task["chunk_index"]
                chunk_text = task["chunk_text"]
                prev_chunk_text = task["prev_chunk_text"]

                key_index = (worker_id + current_task_idx) % len(self.api_keys)
                current_key = self.api_keys[key_index]

                context_prefix = ""
                if prev_chunk_text:
                    sentences = re.split(r"([。！？\.\!\?\n]+)", prev_chunk_text)
                    last_sentences = "".join(sentences[-6:]).strip()
                    if last_sentences:
                        context_prefix = f'[BỐI CẢNH ĐOẠN LIỀN TRƯỚC ĐỂ THAM KHẢO MẠCH TRUYỆN - TUYỆT ĐỐI KHÔNG DỊCH LẠI]:\n"{last_sentences}"\n\n'

                prompt = f"{context_prefix}Dịch ĐẦY ĐỦ 100% toàn bộ văn bản tiểu thuyết sau đây sang tiếng Việt (TUYỆT ĐỐI KHÔNG TÓM TẮT, KHÔNG CẮT BỚT BẤT KỲ CÂU NÀO, PHIÊN ÂM TẤT CẢ HỌ TÊN NHÂN VẬT SANG HÁN VIỆT HOÀN TOÀN, TUYỆT ĐỐI KHÔNG ĐỂ SÓT CHỮ HÁN):\n\n{chunk_text}"

                if progress_callback:
                    with lock:
                        pct = completed_chunks / total_chunks
                        msg = f"[Luồng #{worker_id + 1}] Đang dịch đoạn {chunk_index + 1}/{total_chunks}..." if num_workers > 1 else f"Đang dịch đoạn {chunk_index + 1}/{total_chunks}..."
                        progress_callback({
                            "status": "translating",
                            "worker_id": worker_id + 1,
                            "completed": completed_chunks,
                            "total": total_chunks,
                            "progress": pct,
                            "message": msg,
                        })

                success = False
                retry_attempts = 0
                max_retries = max(5, len(self.api_keys) + 1)

                while not success and retry_attempts < max_retries and not self.is_cancelled and (not cancel_check or not cancel_check()):
                    try:
                        res_text = self.call_gemini_with_key(
                            prompt=prompt,
                            system_instruction=system_prompt,
                            api_key=current_key,
                            model=self.model,
                        )
                        res_clean = res_text.strip()
                        if not res_clean:
                            raise RuntimeError(f"API trả về đoạn dịch trống cho đoạn {chunk_index + 1}.")

                        with lock:
                            translated_chunks[chunk_index] = res_clean
                            completed_chunks += 1
                            success = True
                            if progress_callback:
                                cur_accumulated = "\n\n".join([c for c in translated_chunks if c])
                                pct = completed_chunks / total_chunks
                                progress_callback({
                                    "status": "chunk_completed",
                                    "worker_id": worker_id + 1,
                                    "completed": completed_chunks,
                                    "total": total_chunks,
                                    "progress": pct,
                                    "accumulated_text": cur_accumulated,
                                    "message": f"[Đa luồng x{num_workers}] Đã xong {completed_chunks}/{total_chunks} đoạn ({int(pct * 100)}%)!",
                                })

                        if config["delay_ms"] > 0 and next_chunk_index < len(tasks):
                            time.sleep(max(0.1, config["delay_ms"] / 1000.0 / (num_workers if num_workers > 1 else 1.0)))

                    except Exception as err:
                        retry_attempts += 1
                        if len(self.api_keys) > 1:
                            key_index = (key_index + 1) % len(self.api_keys)
                            current_key = self.api_keys[key_index]

                        if retry_attempts >= max_retries:
                            raise RuntimeError(f"Luồng #{worker_id + 1} không thể dịch đoạn {chunk_index + 1}: {err}")

                        time.sleep(min(2.0, 0.5 * retry_attempts))

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(run_worker, w) for w in range(num_workers)]
            for fut in concurrent.futures.as_completed(futures):
                if cancel_check and cancel_check():
                    self.is_cancelled = True
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise RuntimeError("Đã huỷ bởi người dùng.")
                fut.result()

        self.is_translating = False

        missing_indices = [i + 1 for i, c in enumerate(translated_chunks) if not c or not c.strip()]
        if missing_indices:
            raise RuntimeError(f"Dịch chưa hoàn tất: Thiếu các đoạn {missing_indices} trong văn bản!")

        return "\n\n".join([c for c in translated_chunks if c])
