import customtkinter as ctk
from tkinter import messagebox
import threading
import time
import os
import re
from mutagen.mp3 import MP3

class TTSErrorReviewDialog(ctk.CTkToplevel):
    """
    Cửa sổ Modal xem xét và xử lý các câu TTS bị lỗi / từ chối bởi CapCut.
    Cho phép sửa trực tiếp văn bản, thử lại từng câu hoặc thử lại tất cả,
    và tiếp tục quy trình ghép timeline mà không bị gián đoạn.
    """
    def __init__(self, parent, missing_items, generate_fn, on_proceed_callback, on_cancel_callback=None, initial_logs=None):
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
        """
        super().__init__(parent)

        self.parent = parent
        self.missing_items = missing_items
        self.generate_fn = generate_fn
        self.on_proceed_callback = on_proceed_callback
        self.on_cancel_callback = on_cancel_callback
        
        self.is_retrying_all = False
        self.is_closed = False
        self.item_widgets = {} # index -> widget dict

        self.title("Cần xem xét câu lỗi CapCut")
        self.geometry("780x740")
        self.minsize(700, 650)

        # Modal behavior
        self.transient(parent)
        self.grab_set()
        self.focus_force()
        self.protocol("WM_DELETE_WINDOW", self.on_close_clicked)

        # Center on screen
        self.update_idletasks()
        try:
            px = parent.winfo_rootx() + (parent.winfo_width() // 2) - 390
            py = parent.winfo_rooty() + (parent.winfo_height() // 2) - 370
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
            height=100,
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

        # Error Card Header
        self.label_error_card_title = ctk.CTkLabel(
            self.frame_error_container,
            text=f"⚠️ Cần xử lý  {len(self.missing_items)} câu bị lỗi",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#f87171",
            anchor="w"
        )
        self.label_error_card_title.grid(row=0, column=0, padx=15, pady=(12, 2), sticky="w")

        self.label_error_card_desc = ctk.CTkLabel(
            self.frame_error_container,
            text="Các câu dưới đây bị CapCut từ chối (do từ nhạy cảm hoặc nghẽn mạng). Bạn có thể sửa trực tiếp câu từ và bấm tạo lại.",
            font=ctk.CTkFont(size=12),
            text_color="#9ca3af",
            anchor="w",
            justify="left"
        )
        self.label_error_card_desc.grid(row=1, column=0, padx=15, pady=(0, 10), sticky="w")

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
        self.frame_bottom.grid(row=4, column=0, padx=20, pady=(10, 15), sticky="ew")
        self.frame_bottom.grid_columnconfigure(1, weight=1)

        # Button 1: Thử tạo lại tất cả câu lỗi (Cyan prominent button)
        self.btn_retry_all = ctk.CTkButton(
            self.frame_bottom,
            text="🔄  Thử tạo lại tất cả câu lỗi",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#14b8a6",
            hover_color="#0d9488",
            text_color="#0f172a",
            height=42,
            corner_radius=8,
            command=self.on_retry_all_clicked
        )
        self.btn_retry_all.grid(row=0, column=0, padx=(0, 10), pady=5, sticky="w")

        # Button 2: Bỏ qua & Tiếp tục Render Video (Dark outline button)
        self.btn_proceed = ctk.CTkButton(
            self.frame_bottom,
            text="⏭️  Bỏ qua & Tiếp tục Render Video",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#242638",
            hover_color="#31354c",
            border_width=1,
            border_color="#4b5563",
            text_color="#ffffff",
            height=42,
            corner_radius=8,
            command=self.on_proceed_clicked
        )
        self.btn_proceed.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        # Button 3: Đóng Cửa Sổ (Dark subtle button in bottom right)
        self.btn_close = ctk.CTkButton(
            self.frame_bottom,
            text="Đóng Cửa Sổ",
            font=ctk.CTkFont(size=12),
            fg_color="#1c1e2b",
            hover_color="#2c2f42",
            border_width=1,
            border_color="#374151",
            text_color="#cbd5e1",
            height=36,
            width=110,
            corner_radius=6,
            command=self.on_close_clicked
        )
        self.btn_close.grid(row=0, column=2, padx=(10, 0), pady=5, sticky="e")

    def _get_initial_hint(self, text):
        """Phát hiện lỗi gợi ý dựa trên nội dung câu."""
        if not text or not text.strip():
            return "⚠️ Câu này rỗng. Vui lòng nhập nội dung văn bản rồi bấm \"Thử lại\"."
        if re.search(r'[\u4e00-\u9fff]', text):
            return "⚠️ Câu này đang là chữ tiếng Trung nên giọng Việt không đọc được. Vui lòng nhập bản dịch tiếng Việt vào ô trên rồi bấm \"Thử lại\"."
        return "⚠️ Câu này bị CapCut từ chối (do từ nhạy cảm hoặc nghẽn mạng). Bạn có thể sửa trực tiếp câu từ và bấm tạo lại."

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

            # Top Row: Badge + Entry + Retry Button
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

            btn_retry = ctk.CTkButton(
                card,
                text="🔄 Thử lại",
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color="#0891b2",
                hover_color="#0e7490",
                text_color="#ffffff",
                height=32,
                width=90,
                corner_radius=6,
                command=lambda it=item: self.retry_single_item(it)
            )
            btn_retry.grid(row=0, column=2, padx=(5, 12), pady=(10, 4), sticky="e")

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
            lbl_status.grid(row=1, column=0, columnspan=3, padx=12, pady=(2, 10), sticky="w")

            # Store widget references
            self.item_widgets[item_idx] = {
                "card": card,
                "badge": lbl_badge,
                "entry": entry_text,
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

    def set_item_success(self, item_idx):
        """Chuyển giao diện thẻ sang trạng thái Thành công (Xanh lá)."""
        widgets = self.item_widgets.get(item_idx)
        if not widgets:
            return

        widgets["card"].configure(border_color="#059669")
        widgets["badge"].configure(text_color="#10b981")
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

    def retry_single_item(self, item):
        """Xử lý thử lại một câu riêng lẻ."""
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
                text="⚠️ Câu này vẫn còn chữ tiếng Trung. Vui lòng dịch sang tiếng Việt để giọng đọc hiểu được!",
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
        """Thử tạo lại tất cả các câu chưa thành công."""
        if self.is_retrying_all:
            return

        unresolved_items = [it for it in self.missing_items if it.get("status") != "success"]
        if not unresolved_items:
            messagebox.showinfo("Thông báo", "Tất cả các câu đã được tạo thành công!", parent=self)
            return

        self.is_retrying_all = True
        self.btn_retry_all.configure(state="disabled", text="⏳ Đang thử tạo lại tất cả...")
        self.append_log("HỆ THỐNG", f"Bắt đầu thử tạo lại hàng loạt {len(unresolved_items)} câu lỗi...")

        def _worker_all():
            for item in unresolved_items:
                if self.is_closed:
                    break
                item_idx = item["index"]
                widgets = self.item_widgets.get(item_idx)
                if not widgets:
                    continue

                new_text = widgets["entry"].get().strip()
                sub_id = item.get("sub_index", item_idx + 1)

                if not new_text or re.search(r'[\u4e00-\u9fff]', new_text):
                    # Skip invalid text
                    self.append_log("BỎ QUA", f"Câu #{sub_id} chưa được dịch hợp lệ, bỏ qua thử lại tự động.")
                    continue

                self.after(0, lambda w=widgets: w["btn_retry"].configure(text="⏳ Đang tạo...", state="disabled", fg_color="#374151"))
                self.after(0, lambda w=widgets: w["status"].configure(text="⏳ Đang gửi yêu cầu đến API CapCut...", text_color="#94a3b8"))
                self.append_log("ĐANG XỬ LÝ", f"Đang tạo lại câu #{sub_id}: \"{new_text[:35]}...\"")

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

                time.sleep(0.3)

            self.is_retrying_all = False
            self.after(0, lambda: self.btn_retry_all.configure(state="normal", text="🔄  Thử tạo lại tất cả câu lỗi"))
            self.append_log("HỆ THỐNG", "Hoàn tất đợt thử lại tự động!")

        threading.Thread(target=_worker_all, daemon=True).start()

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
            "Bạn có chắc muốn đóng cửa sổ và DỪNG tiến trình xử lý không?\n"
            "(Danh sách các câu chưa tạo sẽ được lưu vào file missing_subs.srt)",
            parent=self
        )
        if not ans:
            return

        self.is_closed = True
        self.grab_release()
        self.destroy()

        if self.on_cancel_callback:
            self.on_cancel_callback()
