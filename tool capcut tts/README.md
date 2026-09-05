# CapCut TTS & STT Automation Tool

Công cụ tự động hóa mạnh mẽ dựa trên API của CapCut, hỗ trợ xử lý giọng nói, phụ đề và quản lý dự án CapCut trên máy tính (PC). 

## Mục Lục
- [1. Chức năng chính](#1-chức-năng-chính)
- [2. Cách chạy chương trình](#2-cách-chạy-chương-trình)
  - [Giao diện đồ họa (GUI)](#giao-diện-đồ-họa-gui)
  - [Giao diện dòng lệnh (CLI)](#giao-diện-dòng-lệnh-cli)
- [3. Cơ chế hoạt động của các chức năng (Dành cho Developer / AI)](#3-cơ-chế-hoạt-động-của-các-chức-năng-dành-cho-developer--ai)
  - [Tab 1: Chuyển đổi Văn bản thành Giọng nói (TTS Basic)](#tab-1-chuyển-đổi-văn-bản-thành-giọng-nói-tts-basic)
  - [Tab 2: Lồng tiếng SRT & Đồng bộ Video (Voiceover & Sync)](#tab-2-lồng-tiếng-srt--đồng-bộ-video-voiceover--sync)
  - [Tab 3: Cắt nhỏ Project CapCut (Split Project)](#tab-3-cắt-nhỏ-project-capcut-split-project)
  - [Tab 4: Nhận diện giọng nói (STT - Speech to Text)](#tab-4-nhận-diện-giọng-nói-stt---speech-to-text)
  - [Tab 5: Dịch thuật phụ đề AI](#tab-5-dịch-thuật-phụ-đề-ai)
  - [Tab 6: Tách Giọng Nói (CapCut Cloud API PRO)](#tab-6-tách-giọng-nói-capcut-cloud-api-pro)

---

## 1. Chức năng chính
Công cụ cung cấp 6 tính năng chủ lực phục vụ cho dân Editor / Creator:
- **Tạo giọng nói AI (TTS):** Chuyển đổi đoạn văn bản bất kỳ thành file âm thanh với kho giọng đọc tự nhiên của CapCut.
- **Lồng tiếng tự động từ file SRT & Khớp hình ảnh:** Tự động tạo giọng đọc cho từng dòng phụ đề SRT và tự động cắt/tăng giảm tốc độ (speed) của video gốc để khớp với độ dài âm thanh AI vừa tạo, sau đó chèn tất cả vào Project CapCut PC.
- **Cắt nhỏ Project CapCut:** Hỗ trợ tách một dự án (Project) lớn chứa nhiều đoạn video thành các dự án nhỏ hơn để tiện cho việc render hàng loạt.
- **Nhận diện giọng nói (Speech-to-Text) & Dịch thuật:** Trích xuất lời nói từ video/audio thành file phụ đề chuẩn SRT, hỗ trợ dịch tự động sang các ngôn ngữ khác (ví dụ: Anh sang Việt).
- **Dịch phụ đề AI:** Dịch file phụ đề SRT tự động giữ nguyên mốc thời gian và định dạng chuẩn.
- **Tách Giọng Nói (CapCut Cloud API PRO):** Tách giọng đọc/hát (Vocals) và nhạc nền (Instrumental/Beat) trực tiếp bằng **100% API đám mây chính thức của CapCut** (`vc_sound_separate`). Hỗ trợ cấu hình Cookie tài khoản CapCut PRO và cơ chế phân đoạn thông minh (Smart Chunking) bằng FFmpeg giúp xử lý mọi file dài bất kỳ vượt qua giới hạn 15 phút của CapCut.

---

## 2. Cách chạy chương trình

### Yêu cầu tiên quyết (Prerequisites)
- Cài đặt `Python 3.8+`
- Cài đặt `ffmpeg` (để công cụ có thể tối ưu trích xuất âm thanh).
- Cài đặt thư viện: `pip install -r requirements.txt`

### Giao diện đồ họa (GUI)
Đây là cách thân thiện nhất cho người dùng phổ thông.
1. Mở Terminal / Command Prompt tại thư mục dự án.
2. Chạy lệnh:
   ```bash
   python gui.py
   ```
3. Cửa sổ ứng dụng sẽ hiện lên với 4 Tab chức năng riêng biệt. Bạn chỉ cần chọn file, cấu hình thông số và bấm "Bắt đầu".

### Giao diện dòng lệnh (CLI)
Dành cho người dùng chuyên nghiệp muốn viết script hoặc lập lịch tự động.
Sử dụng file `cli.py` để gọi trực tiếp các hàm API:
```bash
# Xem danh sách giọng đọc
python -m capcut_tts_api.cli list-voices --language vi-VN

# Tạo âm thanh từ chữ (TTS)
python -m capcut_tts_api.cli tts-new --text "Xin chào thế giới" --out audio.mp3

# Trích xuất phụ đề từ file (STT)
python -m capcut_tts_api.cli stt-file --audio-file video.mp4 --language vi-VN --out subtitle.srt
```

---

## 3. Cơ chế hoạt động của các chức năng (Dành cho Developer / AI)

Phần này ghi chú lại luồng xử lý (Logic Flow) đằng sau mỗi tính năng, giúp Developer hoặc AI (như Claude, GPT, Gemini) có thể dễ dàng hiểu kiến trúc mã nguồn để bảo trì, sửa lỗi (debug) và phát triển thêm.

### Tab 1: Chuyển đổi Văn bản thành Giọng nói (TTS Basic)
- **Tệp liên quan:** `gui.py` (Tab 1), `capcut_tts_api/client.py`
- **Cơ chế:**
  1. Lấy dữ liệu Text và ID của giọng đọc (Voice Resource ID) từ giao diện.
  2. Gọi phương thức `client.generate_speech(text, voice)`.
  3. Client gửi request POST `CreateTtsTask` lên máy chủ ByteDance/CapCut.
  4. Client tạo vòng lặp (Polling) liên tục gọi `QueryTtsTask` để kiểm tra tiến độ. Chấp nhận trạng thái hoàn thành là `"success"` hoặc `"succeed"`.
  5. Nếu thành công, giải mã (decode) chuỗi `payload` Base64 trả về để lấy ra dữ liệu nhị phân của file MP3 và lưu xuống đĩa.

### Tab 2: Lồng tiếng SRT & Đồng bộ Video (Voiceover & Sync)
- **Tệp liên quan:** `gui.py` (Tab 2), `capcut_tts_api/client.py`, thư mục `mod_project/` (nếu có logic sửa json).
- **Cơ chế:**
  1. Phân tích (Parse) file SRT để lấy ra mốc thời gian `start`, `end` và `text` của từng dòng phụ đề.
  2. Tạo thư mục tạm (temp) và lặp qua từng dòng phụ đề, gọi API TTS để tạo ra file `.mp3` cho mỗi câu.
  3. Lấy ra file Project của CapCut (thường là `draft_content.json`).
  4. Đọc dữ liệu JSON, chèn các track âm thanh `.mp3` vừa tạo vào Timeline của project.
  5. **Logic Đồng bộ (Sync):** Tính toán độ dài âm thanh AI vừa tạo ($L_{audio}$) và so sánh với độ dài gốc của đoạn video tương ứng trong khoảng SRT ($L_{video}$).
  6. Tính tỷ lệ $Speed = L_{video} / L_{audio}$. Cập nhật thông số `speed` của block video trong file `draft_content.json` để video bị kéo giãn/nén lại khớp hoàn toàn với âm thanh. (Các đoạn rỗng không có chữ cũng sẽ được cắt mảng và giữ nguyên thời lượng).
  7. **Hỗ trợ Video đã cắt / Nhiều Clip (Multi-clip):** Có tùy chọn checkbox *"✂️ Hỗ trợ video đã bị cắt / nhiều clip (Multi-clip)"* (mặc định: TẮT). Khi BẬT, thuật toán *Timeline Slicing* sẽ duyệt toàn bộ timeline để co giãn từng clip mà vẫn bảo tồn 100% các đoạn cắt, góc quay, hiệu ứng và clip khác nhau.
  8. Cập nhật và lưu lại file `draft_content.json`.

### Tab 3: Cắt nhỏ Project CapCut (Split Project)
- **Tệp liên quan:** `gui.py` (Tab 3)
- **Cơ chế:**
  1. Đọc file `draft_content.json` của một Project lớn.
  2. Duyệt qua cây cấu trúc `tracks` chứa các `video_segments`.
  3. Áp dụng thuật toán gom nhóm (Grouping) các phân đoạn video dựa trên số lượng mảnh (split count) hoặc độ dài quy định.
  4. Sinh ra nhiều thư mục Project CapCut mới (nhân bản từ project gốc), sau đó sửa file `draft_content.json` của mỗi project mới: xóa bỏ đi các track/segment không thuộc nhóm đó.
  5. Kết quả là 1 Project dài được tự động "xẻ thịt" thành N Project nhỏ với cấu trúc thư mục hoàn hảo để CapCut PC có thể mở trực tiếp.

### Tab 4: Nhận diện giọng nói (STT - Speech to Text)
- **Tệp liên quan:** `gui.py` (Tab 4), `capcut_tts_api/client.py`, `capcut_tts_api/uploader.py`
- **Cơ chế:**
  1. **Tiền xử lý:** Nếu file đầu vào là Video (`.mp4`, `.mov`,...), dùng `ffmpeg` trích xuất riêng âm thanh (`-map a`) ra một file mp3 tạm (dung lượng < 5MB). Nếu là audio thì dùng trực tiếp.
  2. **Upload (VOD):** Gọi `uploader.upload_audio`. Do CapCut VOD có cơ chế từ chối payload lớn, file sẽ được tự động chia nhỏ (Chunked Upload 5MB/phần) và gửi đi bằng HTTP POST kèm `urllib3.Retry` để chống lỗi `ConnectionResetError`.
  3. Khởi tạo Task STT thông qua `client.create_stt_task()`, gửi kèm ngôn ngữ gốc và ngôn ngữ đích (nếu chọn dịch thuật).
  4. **Polling:** Lặp liên tục `query_stt_task` 3 giây/lần. Trạng thái chấp nhận là `"success"` hoặc `"succeed"`. Thời gian timeout tối đa 15 phút.
  5. **Bóc tách & Dịch thuật:** Khi thành công, dùng `extract_subtitles` để build cấu trúc phụ đề. **Lưu ý quan trọng về Dịch thuật:** API CapCut trả về văn bản gốc trong trường `"text"`, và bản dịch (nếu có yêu cầu) trong trường `"translation_text"`. Logic trích xuất sẽ ưu tiên lấy `"translation_text"` nếu tồn tại để tránh việc bị trả về ngôn ngữ gốc. Sau đó hỏi người dùng vị trí `Save As` và ghi ra đĩa định dạng `.srt` chuẩn. Đảm bảo dọn rác (file audio tạm) sau khi hoàn tất.

### Tab 5: Dịch thuật phụ đề AI
- **Tệp liên quan:** `gui.py` (Tab 5), `capcut_tts_api/translator.py`
- **Cơ chế:** Dịch theo ngữ cảnh các khối phụ đề SRT thông qua Gemini AI, bảo toàn 100% timecode và thứ tự câu phụ đề.

### Tab 6: Tách Giọng Nói (AI / Cloud)
- **Tệp liên quan:** `gui.py` (Tab 6), `capcut_tts_api/vocal_api.py`
- **Bản chất kỹ thuật:** Sử dụng trực tiếp API tách giọng chính thức của CapCut PC (`/lv/v1/common_task/new`, `req_key: vc_sound_separate`).
- **Không bắt buộc đăng nhập:** Hỗ trợ tách giọng hoàn toàn miễn phí mà không bắt buộc có Cookie/tài khoản PRO. Nếu có tài khoản PRO, người dùng vẫn có thể dán `sessionid` để được server ưu tiên băng thông.
- **Cơ chế Đa luồng (Multi-threading) & Smart Chunking cho video dài (1-2 tiếng):**
  - Khi file đầu vào dài (kể cả video 30 phút, 1 giờ, 2 giờ...), công cụ sẽ:
    1. Tự động dùng FFmpeg phân đoạn file nguồn thành các lát cắt ngắn (mặc định 10 phút/đoạn).
    2. Sử dụng ThreadPoolExecutor xử lý **đa luồng song song** (1 đến 8 luồng tùy cấu hình, mặc định 3-5 luồng).
    3. Mỗi luồng độc lập upload phân đoạn lên ByteDance VOD và gửi request tạo tác vụ `vc_sound_separate` song song trên CapCut Cloud.
    4. Polling và tải về đồng thời các lát cắt âm thanh đã tách Giọng nói (Vocals) và Nhạc nền (Beat).
    5. Tự động dùng FFmpeg ghép nối (concat) toàn bộ các phân đoạn lại theo đúng thứ tự thời gian, tạo ra file hoàn chỉnh liền mạch không lệch 1 mili-giây.


