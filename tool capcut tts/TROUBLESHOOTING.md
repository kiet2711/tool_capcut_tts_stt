# Cẩm nang xử lý sự cố (Troubleshooting) & Những điều cần lưu ý

File này ghi lại các kinh nghiệm, lỗi phổ biến và cách giải quyết trong quá trình phát triển tool CapCut TTS/STT API. **Bất kỳ AI nào (hoặc Developer) khi làm việc với project này hãy đọc qua tài liệu này trước khi debug lỗi mạng hoặc API.**

## 1. Trạng thái phản hồi của CapCut API (Task Status)
- **Vấn đề:** Đôi khi code bị treo ở vòng lặp polling (query task status) dù server đã làm xong.
- **Nguyên nhân:** CapCut đã âm thầm thay đổi trạng thái hoàn thành từ `"success"` thành `"succeed"` đối với một số API (cả TTS và STT).
- **Cách xử lý:** 
  - **TUYỆT ĐỐI KHÔNG** dùng điều kiện `if status == "success":`.
  - **LUÔN LUÔN** dùng: `if status in ("success", "succeed"):`.

## 2. Lỗi ngắt kết nối khi tải file lên (ConnectionResetError - 10054)
- **Vấn đề:** Trong quá trình tải file lên CapCut VOD, báo lỗi `ConnectionResetError(10054, 'An existing connection was forcibly closed by the remote host')`.
- **Nguyên nhân:**
  1. Máy chủ VOD từ chối kết nối nếu bạn gửi một Payload quá lớn (ví dụ: >5MB) trong một block duy nhất.
  2. Đường truyền mạng không ổn định.
- **Cách xử lý:**
  - Trong `uploader.py`, **bắt buộc** phải chia nhỏ file thành các chunk (5MB/chunk) và upload thông qua phương thức Multipart/Chunked Upload.
  - Khởi tạo `requests.Session()` với `urllib3.util.retry.Retry`. Đảm bảo `allowed_methods` bao gồm `"POST"` và `status_forcelist=[500, 502, 503, 504]`.

## 3. Tải lên video dung lượng lớn cho STT bị treo hoặc rất chậm
- **Vấn đề:** Khi trích xuất SRT (STT) từ video 50MB, thanh tiến trình kẹt ở "Đang tải file lên...".
- **Nguyên nhân:** Gửi nguyên file `.mp4` 50MB lên server tốn rất nhiều băng thông và thời gian, trong khi API STT chỉ cần dữ liệu âm thanh.
- **Cách xử lý:**
  - Ở tầng GUI (`gui.py`), nếu phát hiện đầu vào là Video (`.mp4`, `.mov`, v.v.), hãy dùng `subprocess` gọi lệnh `ffmpeg -y -i <video> -q:a 0 -map a temp_audio.mp3` để tách âm thanh cục bộ trước.
  - Upload file `temp_audio.mp3` đó lên (chỉ tốn vài giây) thay vì upload video gốc. Sau khi upload, phải dọn dẹp (xóa) file `temp_audio.mp3` rác.

## 4. API `generate_speech` trả về dữ liệu kiểu `dict`
- **Vấn đề:** Truy cập `res.audio_data` từ `generate_speech` bị lỗi `AttributeError`.
- **Nguyên nhân:** `generate_speech` không trả về class object tiện ích mà trả về `Dict[str, Any]` (JSON payload nguyên gốc từ server).
- **Cách xử lý:** 
  - Nếu muốn lấy file âm thanh `mp3` gốc, cần trích xuất chuỗi Base64 từ đường dẫn JSON: `res['data']['tasks'][0]['payload']` -> `json.loads` -> `['cap_json']['audio']`. Sau đó decode base64.

## 5. STT Dịch thuật (Translation) trả về ngôn ngữ gốc
- **Vấn đề:** Khi gửi request STT kèm `use_translation=True`, file phụ đề SRT trả ra vẫn chứa nguyên ngôn ngữ gốc, không được dịch.
- **Nguyên nhân:** Máy chủ API trả kết quả dịch ở trường `"translation_text"` chứ không ghi đè lên trường `"text"` của mảng `utterances`. 
- **Cách xử lý:**
  - Ở bước parse payload (`models.py`), phải lấy nội dung bằng lệnh: `text = item.get("translation_text") or item.get("text", "")`. Điều này giúp tự động ưu tiên lấy bản dịch nếu có, ngược lại lấy bản gốc.
