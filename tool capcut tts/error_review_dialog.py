import customtkinter as ctk
from tkinter import messagebox
import threading
import time
import os
import re
import json
import concurrent.futures
from mutagen.mp3 import MP3

class TTSErrorReviewDialog(ctk.CTkToplevel):
    """
    Cửa sổ Modal xem xét và xử lý các câu TTS bị lỗi / từ chối bởi CapCut.
    Hỗ trợ:
    - Dịch tự động bằng AI (Gemini) từ tiếng Trung/ngoại ngữ sang tiếng Việt và tự điền vào ô.
    - Tạo lại đa luồng (multi-threading) thay vì tuần tự từng câu một.
    - Sửa trực tiếp văn bản, thử lại từng câu hoặc thử lại tất cả.
    - Tiếp tục quy trình ghép timeline CapCut mà không bị gián đoạn.
    """
    def __init__(self, parent, missing_items, generate_fn, on_proceed_callback, on_cancel_callback=None, initial_logs=None, threads_count=10):
        """
        :param parent: Window cha
        :param missing_items: Danh sách dict các câu bị lỗi:
            [
                {
                    "index": int,            # 0-indexed trong file subs
                    "sub_index": int|str,    # Số thứ tự trong SRT (vd: 1762)
                    "text": str,             # Nội dung văn bản hiện tại
                    "sub": pysrt_item,       # Đối tượng sub gốc
                    "save_path": str,        # Đường dẫn file mp3 đích
                    "start_micros": int,
                    "end_micros": int,
                    "original_duration_micros": int,
                    "status": str,           # 'pending' | 'success' | 'failed'
                    "error_msg": str
                }, ...
            ]
        :param generate_fn: Hàm callback xử lý tạo âm thanh cho 1 câu:
            generate_fn(item, new_text) -> (success: bool, result_info_or_error: dict|str)
        :param on_proceed_callback: Callback khi người dùng bấm "Bỏ qua & Tiếp tục" hoặc xong hết:
            on_proceed_callback(resolved_items, skipped_items)
        :param on_cancel_callback: Callback khi người dùng bấm "Đóng Cửa Sổ" / Hủy:
            on_cancel_callback()
        :param initial_logs: Danh sách các dòng log ban đầu (nếu có)
        :param threads_count: Số luồng tối đa khi thử tạo lại đa luồng
        """
        super().__init__(parent)

        self.parent = parent
        self.missing_items = missing_items
        self.generate_fn = generate_fn
        self.on_proceed_callback = on_proceed_callback
        self.on_cancel_callback = on_cancel_callback
        self.threads_count = max(1, min(int(threads_count or 10), 20))
        
        self.is_retrying_all = False
        self.is_translating = False
        self.is_closed = False
        self.item_widgets = {} # index -> widget dict

        self.title("Cần xem xét câu lỗi CapCut")
        self.geometry("860x760")
        self.minsize(760, 650)

        # Modal behavior
        self.transient(parent)
        self.grab_set()
        self.focus_force()
        self.protocol("WM_DELETE_WINDOW", self.on_close_clicked)

        # Center on screen
        self.update_idletasks()
        try:
            px = parent.winfo_rootx() + (parent.winfo_width() // 2) - 430
            py = parent.winfo_rooty() + (parent.winfo_height() // 2) - 380
            self.geometry(f"+{max(10, px)}+{max(10, py)}")
        except Exception:
            pass

        self._build_ui()

        # Load initial logs
        if initial_logs:
            for tag, msg in initial_logs:
                self.append_log(tag, msg)
        else:
            self.append_log("TTS_NEEDS_REVIEW", f"Có {len(self.missing_items)} câu bị lỗi CapCut cần xử lý.")

        self.update_progress()

    def _build_ui(self):
        # Configure root grid
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # -------------------------------------------------------------
        # 1. Header Section
        # -------------------------------------------------------------
        self.frame_header = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_header.grid(row=0, column=0, padx=20, pady=(15, 5), sticky="ew")
        self.frame_header.grid_columnconfigure(0, weight=1)

        # Title with Amber Warning Icon
        self.label_title = ctk.CTkLabel(
            self.frame_header,
            text="⚠️  Cần xem xét câu lỗi CapCut",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#ffffff",
            anchor="w"
        )
        self.label_title.grid(row=0, column=0, sticky="w", pady=(0, 8))

        # Progress Bar
        self.progressbar = ctk.CTkProgressBar(
            self.frame_header,
            height=6,
            corner_radius=3,
            progress_color="#14b8a6",
            fg_color="#2b2d42"
        )
        self.progressbar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.progressbar.set(0)

        # Subtitle
        self.label_subtitle = ctk.CTkLabel(
            self.frame_header,
            text=f"Có {len(self.missing_items)} câu bị lỗi CapCut cần xử lý.",
            font=ctk.CTkFont(size=13),
            text_color="#cbd5e1",
            anchor="w"
        )
        self.label_subtitle.grid(row=2, column=0, sticky="w")

        # -------------------------------------------------------------
        # 2. Console Log Box Section
        # -------------------------------------------------------------
        self.frame_console = ctk.CTkFrame(self, fg_color="#11131a", border_width=1, border_color="#272b3b", corner_radius=8)
        self.frame_console.grid(row=1, column=0, padx=20, pady=8, sticky="ew")
        self.frame_console.grid_columnconfigure(0, weight=1)

        self.text_console = ctk.CTkTextbox(
            self.frame_console,
            height=95,
            fg_color="transparent",
            text_color="#94a3b8",
            font=ctk.CTkFont(family="Consolas", size=11),
            wrap="word",
            activate_scrollbars=True
        )
        self.text_console.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")

        # -------------------------------------------------------------
        # 3. Error Items Container Section (Red Alert Card)
        # -------------------------------------------------------------
        self.frame_error_container = ctk.CTkFrame(
            self,
            fg_color="#161824",
            border_width=1,
            border_color="#7f1d1d",
            corner_radius=10
        )
        self.frame_error_container.grid(row=3, column=0, padx=20, pady=8, sticky="nsew")
        self.frame_error_container.grid_columnconfigure(0, weight=1)
        self.frame_error_container.grid_rowconfigure(2, weight=1)

        # Error Card Header with Title and Quick Translate Button
        self.frame_error_header = ctk.CTkFrame(self.frame_error_container, fg_color="transparent")
        self.frame_error_header.grid(row=0, column=0, padx=15, pady=(10, 2), sticky="ew")
        self.frame_error_header.grid_columnconfigure(0, weight=1)

        self.label_error_card_title = ctk.CTkLabel(
            self.frame_error_header,
            text=f"⚠️ Cần xử lý  {len(self.missing_items)} câu bị lỗi",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#f87171",
            anchor="w"
        )
        self.label_error_card_title.grid(row=0, column=0, sticky="w")

        self.btn_quick_trans = ctk.CTkButton(
            self.frame_error_header,
            text="🌐 Dịch tất cả câu lỗi (AI)",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#4f46e5",
            hover_color="#4338ca",
            text_color="#ffffff",
            height=28,
            width=180,
            corner_radius=6,
            command=self.translate_all_unresolved
        )
        self.btn_quick_trans.grid(row=0, column=1, sticky="e")

        self.label_error_card_desc = ctk.CTkLabel(
            self.frame_error_container,
            text="Các câu dưới đây bị lỗi hoặc chưa có bản dịch tiếng Việt. Bạn có thể bấm '🌐 Dịch' để AI tự động dịch hoặc sửa trực tiếp rồi bấm 'Thử lại'.",
            font=ctk.CTkFont(size=12),
            text_color="#9ca3af",
            anchor="w",
            justify="left"
        )
        self.label_error_card_desc.grid(row=1, column=0, padx=15, pady=(0, 8), sticky="w")

        # Scrollable List for Failed Items
        self.scroll_items = ctk.CTkScrollableFrame(
            self.frame_error_container,
            fg_color="transparent",
            corner_radius=6
        )
        self.scroll_items.grid(row=2, column=0, padx=10, pady=(0, 10), sticky="nsew")
        self.scroll_items.grid_columnconfigure(0, weight=1)

        # Render list of cards
        self._populate_items()

        # -------------------------------------------------------------
        # 4. Bottom Action Bar Section
        # -------------------------------------------------------------
        self.frame_bottom = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_bottom.grid(row=4, column=0, padx=20, pady=(6, 15), sticky="ew")
        self.frame_bottom.grid_columnconfigure(0, weight=1)

        # Row 0: Option Checkbox
        self.chk_auto_tts_var = ctk.BooleanVar(value=True)
        self.chk_auto_tts = ctk.CTkCheckBox(
            self.frame_bottom,
            text="⚡ Tự động tạo giọng đọc ngay sau khi dịch xong",
            variable=self.chk_auto_tts_var,
            font=ctk.CTkFont(size=12),
            text_color="#94a3b8"
        )
        self.chk_auto_tts.grid(row=0, column=0, sticky="w", pady=(0, 8))

        # Row 1: Action Buttons
        self.frame_action_buttons = ctk.CTkFrame(self.frame_bottom, fg_color="transparent")
        self.frame_action_buttons.grid(row=1, column=0, sticky="ew")

        # Button 1: Dịch tất cả câu lỗi (AI)
        self.btn_translate_all = ctk.CTkButton(
            self.frame_action_buttons,
            text="🌐  Dịch tất cả câu lỗi (AI)",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#4f46e5",
            hover_color="#4338ca",
            text_color="#ffffff",
            height=40,
            corner_radius=8,
            command=self.translate_all_unresolved
        )
        self.btn_translate_all.pack(side="left", padx=(0, 8))

        # Button 2: Thử tạo lại tất cả câu lỗi (Đa luồng)
        self.btn_retry_all = ctk.CTkButton(
            self.frame_action_buttons,
            text=f"🔄  Thử tạo lại tất cả (Đa luồng x{self.threads_count})",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#14b8a6",
            hover_color="#0d9488",
            text_color="#0f172a",
            height=40,
            corner_radius=8,
            command=self.on_retry_all_clicked
        )
        self.btn_retry_all.pack(side="left", padx=(0, 8))

        # Button 3: Bỏ qua & Tiếp tục Render Video
        self.btn_proceed = ctk.CTkButton(
            self.frame_action_buttons,
            text="⏭️  Bỏ qua & Tiếp tục Render Video",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#242638",
            hover_color="#31354c",
            border_width=1,
            border_color="#4b5563",
            text_color="#ffffff",
            height=40,
            corner_radius=8,
            command=self.on_proceed_clicked
        )
        self.btn_proceed.pack(side="left", padx=(0, 8))

        # Button 4: Đóng Cửa Sổ
        self.btn_close = ctk.CTkButton(
            self.frame_action_buttons,
            text="Đóng Cửa Sổ",
            font=ctk.CTkFont(size=12),
            fg_color="#1c1e2b",
            hover_color="#2c2f42",
            border_width=1,
            border_color="#374151",
            text_color="#cbd5e1",
            height=40,
            width=100,
            corner_radius=6,
            command=self.on_close_clicked
        )
        self.btn_close.pack(side="right", padx=0)

    def _get_initial_hint(self, text):
        """Phát hiện lỗi gợi ý dựa trên nội dung câu."""
        if not text or not text.strip():
            return "⚠️ Câu này rỗng. Vui lòng nhập nội dung văn bản rồi bấm \"Thử lại\"."
        if re.search(r'[\u4e00-\u9fff]', text):
            return "⚠️ Câu này đang là chữ tiếng Trung. Bấm nút '🌐 Dịch' bên cạnh để AI dịch sang tiếng Việt!"
        return "⚠️ Câu này bị CapCut từ chối (do từ nhạy cảm hoặc nghẽn mạng). Bạn có thể sửa câu từ rồi bấm tạo lại."

    def _populate_items(self):
        """Tạo giao diện thẻ cho từng câu lỗi."""
        for row_idx, item in enumerate(self.missing_items):
            item_idx = item["index"]
            sub_id = item.get("sub_index", item_idx + 1)
            raw_text = item.get("text", "")

            # Item Frame Card
            card = ctk.CTkFrame(
                self.scroll_items,
                fg_color="#12131c",
                border_width=1,
                border_color="#7f1d1d",
                corner_radius=8
            )
            card.grid(row=row_idx, column=0, padx=5, pady=6, sticky="ew")
            card.grid_columnconfigure(1, weight=1)

            # Top Row: Badge + Entry + Translate Button + Retry Button
            lbl_badge = ctk.CTkLabel(
                card,
                text=f"#{sub_id}",
                font=ctk.CTkFont(size=13, weight="bold"),
                text_color="#ef4444",
                width=55,
                anchor="w"
            )
            lbl_badge.grid(row=0, column=0, padx=(12, 5), pady=(10, 4), sticky="w")

            entry_text = ctk.CTkEntry(
                card,
                fg_color="#1a1c29",
                border_color="#374151",
                text_color="#ffffff",
                corner_radius=6,
                font=ctk.CTkFont(size=13),
                height=34
            )
            entry_text.insert(0, raw_text)
            entry_text.grid(row=0, column=1, padx=5, pady=(10, 4), sticky="ew")

            btn_trans = ctk.CTkButton(
                card,
                text="🌐 Dịch",
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color="#4f46e5",
                hover_color="#4338ca",
                text_color="#ffffff",
                height=32,
                width=65,
                corner_radius=6,
                command=lambda it=item: self.translate_single_item(it)
            )
            btn_trans.grid(row=0, column=2, padx=(5, 2), pady=(10, 4), sticky="e")

            btn_retry = ctk.CTkButton(
                card,
                text="🔄 Thử lại",
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color="#0891b2",
                hover_color="#0e7490",
                text_color="#ffffff",
                height=32,
                width=85,
                corner_radius=6,
                command=lambda it=item: self.retry_single_item(it)
            )
            btn_retry.grid(row=0, column=3, padx=(2, 12), pady=(10, 4), sticky="e")

            # Bottom Row: Hint / Error / Success Message
            hint_msg = self._get_initial_hint(raw_text)
            lbl_status = ctk.CTkLabel(
                card,
                text=hint_msg,
                font=ctk.CTkFont(size=11),
                text_color="#f59e0b" if "tiếng Trung" in hint_msg else "#ef4444",
                anchor="w",
                justify="left",
                wraplength=640
            )
            lbl_status.grid(row=1, column=0, columnspan=4, padx=12, pady=(2, 10), sticky="w")

            # Store widget references
            self.item_widgets[item_idx] = {
                "card": card,
                "badge": lbl_badge,
                "entry": entry_text,
                "btn_trans": btn_trans,
                "btn_retry": btn_retry,
                "status": lbl_status,
                "item": item
            }

    def append_log(self, tag, message):
        """Thêm dòng log thời gian thực vào Console."""
        if self.is_closed:
            return
        timestamp = time.strftime("%I:%M:%S %p")
        log_line = f"[{timestamp}] [{tag}] {message}\n"
        
        def _insert():
            try:
                self.text_console.configure(state="normal")
                self.text_console.insert("end", log_line)
                self.text_console.see("end")
                self.text_console.configure(state="disabled")
            except Exception:
                pass
        self.after(0, _insert)

    def update_progress(self):
        """Cập nhật tiến độ giải quyết câu lỗi."""
        if self.is_closed:
            return
        total = len(self.missing_items)
        resolved_count = sum(1 for it in self.missing_items if it.get("status") == "success")
        unresolved_count = total - resolved_count

        progress_val = resolved_count / total if total > 0 else 1.0
        self.progressbar.set(progress_val)

        if unresolved_count == 0:
            self.label_subtitle.configure(
                text="🎉 Đã xử lý thành công tất cả câu lỗi!",
                text_color="#10b981"
            )
            self.label_error_card_title.configure(
                text="✅ Tất cả câu lỗi đã được tạo thành công!",
                text_color="#10b981"
            )
            self.frame_error_container.configure(border_color="#059669")
            self.btn_proceed.configure(
                text="✅ Hoàn tất & Tiếp tục Render Video",
                fg_color="#059669",
                hover_color="#047857"
            )
        else:
            self.label_subtitle.configure(
                text=f"Có {unresolved_count} câu bị lỗi CapCut cần xử lý (Đã sửa: {resolved_count}/{total}).",
                text_color="#cbd5e1"
            )
            self.label_error_card_title.configure(
                text=f"⚠️ Cần xử lý  {unresolved_count} câu bị lỗi",
                text_color="#f87171"
            )
            self.frame_error_container.configure(border_color="#7f1d1d")
            self.btn_proceed.configure(
                text="⏭️  Bỏ qua & Tiếp tục Render Video",
                fg_color="#242638",
                hover_color="#31354c"
            )

    def set_item_success(self, item_idx):
        """Chuyển giao diện thẻ sang trạng thái Thành công (Xanh lá)."""
        widgets = self.item_widgets.get(item_idx)
        if not widgets:
            return

        widgets["card"].configure(border_color="#059669")
        widgets["badge"].configure(text_color="#10b981")
        if "btn_trans" in widgets:
            widgets["btn_trans"].configure(state="disabled")
        widgets["btn_retry"].configure(
            text="✅ Đã tạo",
            fg_color="#064e3b",
            hover_color="#064e3b",
            border_width=1,
            border_color="#10b981",
            text_color="#34d399",
            state="disabled"
        )
        widgets["status"].configure(
            text="✅ Tạo âm thanh AI thành công!",
            text_color="#10b981"
        )
        self.update_progress()

    def set_item_error(self, item_idx, error_msg):
        """Chuyển giao diện thẻ sang trạng thái Thất bại."""
        widgets = self.item_widgets.get(item_idx)
        if not widgets:
            return

        widgets["card"].configure(border_color="#7f1d1d")
        widgets["badge"].configure(text_color="#ef4444")
        if "btn_trans" in widgets:
            widgets["btn_trans"].configure(state="normal")
        widgets["btn_retry"].configure(
            text="🔄 Thử lại",
            fg_color="#0891b2",
            hover_color="#0e7490",
            border_width=0,
            text_color="#ffffff",
            state="normal"
        )
        widgets["status"].configure(
            text=f"⚠️ Lỗi: {error_msg}. Vui lòng sửa lại câu chữ rồi bấm \"Thử lại\".",
            text_color="#ef4444"
        )

    # -------------------------------------------------------------
    # AI Translation Integration
    # -------------------------------------------------------------
    def translate_single_item(self, item):
        """Dịch 1 câu đơn lẻ bằng AI."""
        if self.is_translating or self.is_retrying_all:
            return
        self.translate_items([item], auto_retry_after=False)

    def translate_all_unresolved(self, auto_retry=False):
        """Dịch tất cả các câu chưa thành công (hoặc tất cả các câu trong danh sách)."""
        if self.is_translating or self.is_retrying_all:
            return
        unresolved = [it for it in self.missing_items if it.get("status") != "success"]
        if not unresolved:
            messagebox.showinfo("Thông báo", "Tất cả các câu đã được xử lý xong!", parent=self)
            return
        self.translate_items(unresolved, auto_retry_after=auto_retry)

    def translate_items(self, target_items, auto_retry_after=False):
        """Xử lý dịch thuật AI cho danh sách câu lỗi."""
        if self.is_translating:
            return

        if not target_items:
            return

        # 1. Lấy thông tin cấu hình dịch từ parent / app_config
        api_keys_str = ""
        model = "gemini-3.5-flash-lite"
        style = ""
        concurrency = "auto"

        if hasattr(self.parent, "trans_api_key_var"):
            api_keys_str = self.parent.trans_api_key_var.get().strip()
        if hasattr(self.parent, "trans_model_var"):
            model = self.parent.trans_model_var.get()
        if hasattr(self.parent, "trans_style_var"):
            style = self.parent.trans_style_var.get()
        if hasattr(self.parent, "trans_concurrency_var"):
            concurrency = self.parent.trans_concurrency_var.get()

        if not api_keys_str:
            # Try reading app_config.json
            try:
                cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_config.json")
                if os.path.exists(cfg_path):
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                        api_keys_str = cfg.get("trans_api_keys", "").strip()
                        if "trans_model" in cfg and not model:
                            model = cfg["trans_model"]
                        if "trans_style" in cfg and not style:
                            style = cfg["trans_style"]
            except Exception:
                pass

        if not api_keys_str:
            messagebox.showwarning(
                "Chưa có Gemini API Key",
                "Vui lòng nhập Gemini API Key trong Tab 'Dịch thuật' để sử dụng tính năng dịch tự động!",
                parent=self
            )
            return

        self.is_translating = True
        self.btn_translate_all.configure(state="disabled", text="⏳ Đang dịch AI...")
        if hasattr(self, "btn_quick_trans"):
            self.btn_quick_trans.configure(state="disabled", text="⏳ Đang dịch...")
        self.append_log("DỊCH THUẬT", f"Bắt đầu dịch AI cho {len(target_items)} câu lỗi...")

        # Set UI loading for items
        for it in target_items:
            w = self.item_widgets.get(it["index"])
            if w:
                w["status"].configure(text="⏳ Đang gửi yêu cầu dịch đến Gemini AI...", text_color="#38bdf8")
                if "btn_trans" in w:
                    w["btn_trans"].configure(state="disabled")

        def _worker_trans():
            try:
                from capcut_tts_api.translator import GeminiTranslator, SrtItem
                translator = GeminiTranslator(api_keys=api_keys_str, model=model)

                # Prepare SrtItem list
                srt_items = []
                for it in target_items:
                    idx = it["index"]
                    w = self.item_widgets.get(idx)
                    cur_text = w["entry"].get().strip() if w else it.get("text", "")
                    sub_id = it.get("sub_index", idx + 1)
                    try:
                        parsed_id = int(sub_id)
                    except Exception:
                        parsed_id = idx + 1

                    srt_items.append(SrtItem(
                        id=parsed_id,
                        timecode="00:00:00,000 --> 00:00:01,000",
                        original_text=cur_text or it.get("text", "")
                    ))

                # Translate using translate_srt
                translated_res = translator.translate_srt(
                    srt_content_or_items=srt_items,
                    style=style,
                    concurrency=concurrency
                )

                trans_map = {}
                for s_it in translated_res:
                    if s_it.translated_text:
                        trans_map[s_it.id] = s_it.translated_text.strip()

                success_count = 0
                for it in target_items:
                    idx = it["index"]
                    sub_id = it.get("sub_index", idx + 1)
                    try:
                        parsed_id = int(sub_id)
                    except Exception:
                        parsed_id = idx + 1

                    new_trans = trans_map.get(parsed_id, "")
                    if not new_trans and len(translated_res) == len(target_items):
                        # Fallback by index position
                        pos = target_items.index(it)
                        new_trans = translated_res[pos].translated_text.strip()

                    if new_trans:
                        it["text"] = new_trans
                        if "sub" in it and it["sub"]:
                            it["sub"].text = new_trans
                        success_count += 1

                        def _update_card(item_idx=idx, trans_text=new_trans, sid=sub_id):
                            w = self.item_widgets.get(item_idx)
                            if w:
                                w["entry"].delete(0, "end")
                                w["entry"].insert(0, trans_text)
                                w["status"].configure(
                                    text="✨ Đã dịch sang Tiếng Việt. Sẵn sàng tạo âm thanh!",
                                    text_color="#38bdf8"
                                )
                                w["card"].configure(border_color="#3b82f6")
                                if "btn_trans" in w:
                                    w["btn_trans"].configure(state="normal")
                        self.after(0, _update_card)
                        self.append_log("DỊCH THUẬT", f"Câu #{sub_id}: Đã dịch ➔ \"{new_trans[:35]}\"")

                self.append_log("DỊCH THUẬT", f"Hoàn tất dịch {success_count}/{len(target_items)} câu sang Tiếng Việt!")

            except Exception as ex:
                self.append_log("LỖI", f"Lỗi trong quá trình dịch thuật AI: {ex}")
                self.after(0, lambda e=str(ex): messagebox.showerror("Lỗi dịch thuật", f"Không thể dịch tự động:\n{e}", parent=self))
            finally:
                self.is_translating = False
                def _restore_btns():
                    self.btn_translate_all.configure(state="normal", text="🌐  Dịch tất cả câu lỗi (AI)")
                    if hasattr(self, "btn_quick_trans"):
                        self.btn_quick_trans.configure(state="normal", text="🌐 Dịch tất cả câu lỗi (AI)")
                    for it in target_items:
                        w = self.item_widgets.get(it["index"])
                        if w and "btn_trans" in w:
                            w["btn_trans"].configure(state="normal")
                self.after(0, _restore_btns)

                # Check if auto retry should run
                should_retry = auto_retry_after or (hasattr(self, "chk_auto_tts_var") and self.chk_auto_tts_var.get())
                if should_retry and not self.is_closed:
                    self.after(300, self.on_retry_all_clicked)

        threading.Thread(target=_worker_trans, daemon=True).start()

    # -------------------------------------------------------------
    # Multi-threaded Speech Generation (Retry)
    # -------------------------------------------------------------
    def retry_single_item(self, item):
        """Xử lý thử lại một câu riêng lẻ."""
        if self.is_translating:
            return
        item_idx = item["index"]
        widgets = self.item_widgets.get(item_idx)
        if not widgets:
            return

        new_text = widgets["entry"].get().strip()
        sub_id = item.get("sub_index", item_idx + 1)

        if not new_text:
            widgets["status"].configure(
                text="⚠️ Vui lòng nhập nội dung văn bản trước khi bấm thử lại!",
                text_color="#ef4444"
            )
            return

        # Check Chinese
        if re.search(r'[\u4e00-\u9fff]', new_text):
            widgets["status"].configure(
                text="⚠️ Câu này vẫn còn chữ tiếng Trung. Vui lòng bấm '🌐 Dịch' hoặc nhập tiếng Việt!",
                text_color="#f59e0b"
            )
            return

        # Set UI loading
        widgets["btn_retry"].configure(
            text="⏳ Đang tạo...",
            state="disabled",
            fg_color="#374151"
        )
        widgets["status"].configure(
            text="⏳ Đang gửi yêu cầu đến API CapCut...",
            text_color="#94a3b8"
        )
        self.append_log("ĐANG XỬ LÝ", f"Đang thử tạo âm thanh cho câu #{sub_id}: \"{new_text[:35]}...\"")

        def _worker():
            try:
                success, res = self.generate_fn(item, new_text)
                if success:
                    item["status"] = "success"
                    item["text"] = new_text
                    if "sub" in item and item["sub"]:
                        item["sub"].text = new_text
                    item["result_info"] = res
                    self.after(0, lambda: self.set_item_success(item_idx))
                    self.append_log("THÀNH CÔNG", f"Đã tạo âm thanh AI thành công cho câu #{sub_id}!")
                else:
                    err_msg = str(res)
                    item["status"] = "failed"
                    self.after(0, lambda: self.set_item_error(item_idx, err_msg))
                    self.append_log("LỖI", f"Tạo câu #{sub_id} thất bại: {err_msg}")
            except Exception as ex:
                item["status"] = "failed"
                self.after(0, lambda: self.set_item_error(item_idx, str(ex)))
                self.append_log("LỖI", f"Ngoại lệ khi tạo câu #{sub_id}: {ex}")

        threading.Thread(target=_worker, daemon=True).start()

    def on_retry_all_clicked(self):
        """Thử tạo lại tất cả các câu chưa thành công bằng ĐA LUỒNG."""
        if self.is_retrying_all or self.is_translating:
            return

        unresolved_items = [it for it in self.missing_items if it.get("status") != "success"]
        if not unresolved_items:
            messagebox.showinfo("Thông báo", "Tất cả các câu đã được tạo thành công!", parent=self)
            return

        self.is_retrying_all = True
        self.btn_retry_all.configure(state="disabled", text="⏳ Đang tạo đa luồng...")
        self.append_log("HỆ THỐNG", f"Bắt đầu thử tạo lại ĐA LUỒNG cho {len(unresolved_items)} câu lỗi...")

        def _task_single(item):
            if self.is_closed:
                return
            item_idx = item["index"]
            widgets = self.item_widgets.get(item_idx)
            if not widgets:
                return

            new_text = widgets["entry"].get().strip()
            sub_id = item.get("sub_index", item_idx + 1)

            if not new_text:
                self.append_log("BỎ QUA", f"Câu #{sub_id} bị rỗng, bỏ qua thử lại.")
                self.after(0, lambda w=widgets: w["status"].configure(
                    text="⚠️ Câu này đang rỗng. Vui lòng nhập nội dung!", text_color="#ef4444"
                ))
                return

            if re.search(r'[\u4e00-\u9fff]', new_text):
                self.append_log("BỎ QUA", f"Câu #{sub_id} vẫn còn chữ Trung, bỏ qua.")
                self.after(0, lambda w=widgets: w["status"].configure(
                    text="⚠️ Câu này vẫn còn chữ tiếng Trung. Vui lòng bấm '🌐 Dịch' hoặc nhập tiếng Việt!",
                    text_color="#f59e0b"
                ))
                return

            self.after(0, lambda w=widgets: w["btn_retry"].configure(text="⏳ Đang tạo...", state="disabled", fg_color="#374151"))
            self.after(0, lambda w=widgets: w["status"].configure(text="⏳ Đang gửi yêu cầu đến CapCut API...", text_color="#94a3b8"))
            self.append_log("ĐANG XỬ LÝ", f"Đang tạo câu #{sub_id}: \"{new_text[:35]}...\"")

            try:
                success, res = self.generate_fn(item, new_text)
                if success:
                    item["status"] = "success"
                    item["text"] = new_text
                    if "sub" in item and item["sub"]:
                        item["sub"].text = new_text
                    item["result_info"] = res
                    self.after(0, lambda idx=item_idx: self.set_item_success(idx))
                    self.append_log("THÀNH CÔNG", f"Đã tạo âm thanh AI thành công cho câu #{sub_id}!")
                else:
                    err_msg = str(res)
                    item["status"] = "failed"
                    self.after(0, lambda idx=item_idx, m=err_msg: self.set_item_error(idx, m))
                    self.append_log("LỖI", f"Tạo câu #{sub_id} thất bại: {err_msg}")
            except Exception as ex:
                item["status"] = "failed"
                self.after(0, lambda idx=item_idx, e=str(ex): self.set_item_error(idx, e))
                self.append_log("LỖI", f"Ngoại lệ khi tạo câu #{sub_id}: {ex}")

        def _pool_runner():
            threads = getattr(self, "threads_count", 10) or 10
            max_workers = max(1, min(len(unresolved_items), threads, 20))
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_task_single, it) for it in unresolved_items]
                concurrent.futures.wait(futures)

            self.is_retrying_all = False
            self.after(0, lambda: self.btn_retry_all.configure(state="normal", text=f"🔄  Thử tạo lại tất cả (Đa luồng x{self.threads_count})"))
            self.append_log("HỆ THỐNG", "Hoàn tất đợt thử tạo lại đa luồng!")

        threading.Thread(target=_pool_runner, daemon=True).start()

    def on_proceed_clicked(self):
        """Người dùng bấm 'Bỏ qua & Tiếp tục Render Video' / Hoàn tất."""
        resolved = [it for it in self.missing_items if it.get("status") == "success"]
        unresolved = [it for it in self.missing_items if it.get("status") != "success"]

        if unresolved:
            ans = messagebox.askyesno(
                "Xác nhận tiếp tục",
                f"Vẫn còn {len(unresolved)} câu chưa được tạo âm thanh.\n\n"
                f"Nếu tiếp tục, các câu này sẽ im lặng (không có tiếng) trong CapCut.\n"
                f"Bạn có chắc chắn muốn BỎ QUA và tiếp tục không?",
                parent=self
            )
            if not ans:
                return

        self.is_closed = True
        self.grab_release()
        self.destroy()

        if self.on_proceed_callback:
            self.on_proceed_callback(resolved, unresolved)

    def on_close_clicked(self):
        """Người dùng đóng cửa sổ hoặc bấm Dừng lại."""
        ans = messagebox.askyesno(
            "Đóng cửa sổ",
            "Bạn có chắc muốn đóng cửa sổ không?\n\n(Bạn có thể bấm nút '⚠️ Mở Bảng Xử Lý Câu Lỗi' trên giao diện chính bất kỳ lúc nào để mở lại và sửa tiếp)",
            parent=self
        )
        if not ans:
            return

        self.is_closed = True
        self.grab_release()
        self.destroy()

        if self.on_cancel_callback:
            self.on_cancel_callback()
