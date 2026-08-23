import concurrent.futures
import json
import os
import re
import threading
import time
import customtkinter as ctk
from tkinter import messagebox
from typing import Callable, List, Optional

try:
    import requests
except ImportError:
    requests = None


class ApiKeyManagerDialog(ctk.CTkToplevel):
    """
    Dialog for managing, pasting, validating and live-testing Gemini API Keys in parallel.
    Includes automated key cleaning and failover reporting.
    """

    def __init__(self, parent, initial_keys_str: str = "", on_save_callback: Optional[Callable[[str], None]] = None):
        super().__init__(parent)

        self.parent = parent
        self.on_save_callback = on_save_callback
        self.is_testing = False
        self.test_results = {}  # key -> dict(status, msg, latency, is_valid)

        self.title("🔑 Quản Lý & Kiểm Tra Gemini API Keys")
        self.geometry("750x640")
        self.minsize(680, 560)

        # Modal behavior
        self.transient(parent)
        self.grab_set()
        self.focus_force()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # --- 1. Header Frame ---
        self.frame_header = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_header.grid(row=0, column=0, padx=15, pady=(15, 5), sticky="ew")

        ctk.CTkLabel(
            self.frame_header,
            text="Danh sách Gemini API Keys (Mỗi key trên 1 dòng hoặc cách nhau bằng dấu phẩy):",
            font=ctk.CTkFont(size=14, weight="bold")
        ).pack(side="left")

        self.lbl_key_count = ctk.CTkLabel(
            self.frame_header,
            text="0 Key",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#38bdf8"
        )
        self.lbl_key_count.pack(side="right")

        # --- 2. Keys Input Textbox ---
        self.txt_keys = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Consolas", size=13), height=130)
        self.txt_keys.grid(row=1, column=0, padx=15, pady=5, sticky="nsew")

        # Format initial keys (1 per line for clear viewing)
        cleaned_initial = "\n".join(self._extract_keys(initial_keys_str))
        self.txt_keys.insert("1.0", cleaned_initial)
        self.txt_keys.bind("<KeyRelease>", lambda e: self._update_key_count())

        # --- 3. Toolbar Frame ---
        self.frame_tools = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_tools.grid(row=2, column=0, padx=15, pady=5, sticky="ew")

        self.btn_test = ctk.CTkButton(
            self.frame_tools,
            text="🧪 Test Toàn Bộ Key (Đa luồng)",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#0284c7",
            hover_color="#0369a1",
            height=34,
            command=self.start_test_keys
        )
        self.btn_test.pack(side="left", padx=(0, 5))

        self.btn_clean_failed = ctk.CTkButton(
            self.frame_tools,
            text="🧹 Loại Bỏ Key Lỗi",
            font=ctk.CTkFont(size=13),
            fg_color="#7c3aed",
            hover_color="#6d28d9",
            height=34,
            command=self.clean_failed_keys
        )
        self.btn_clean_failed.pack(side="left", padx=5)

        self.btn_paste = ctk.CTkButton(
            self.frame_tools,
            text="📋 Dán Clipboard",
            width=110,
            height=34,
            command=self.paste_from_clipboard
        )
        self.btn_paste.pack(side="left", padx=5)

        self.btn_clear = ctk.CTkButton(
            self.frame_tools,
            text="🗑️ Xóa Tất Cả",
            width=90,
            height=34,
            fg_color="#b23b3b",
            hover_color="#8f2b2b",
            command=self.clear_all
        )
        self.btn_clear.pack(side="left", padx=5)

        # --- 4. Results / Log Area ---
        self.frame_results_label = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_results_label.grid(row=3, column=0, padx=15, pady=(5, 0), sticky="ew")
        
        ctk.CTkLabel(
            self.frame_results_label,
            text="📊 Kết Quả Kiểm Tra Chi Tiết:",
            font=ctk.CTkFont(size=13, weight="bold")
        ).pack(side="left")

        self.lbl_test_summary = ctk.CTkLabel(
            self.frame_results_label,
            text="",
            font=ctk.CTkFont(size=12),
            text_color="#94a3b8"
        )
        self.lbl_test_summary.pack(side="right")

        self.txt_results = ctk.CTkTextbox(
            self,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color="#1e1e2d"
        )
        self.txt_results.grid(row=4, column=0, padx=15, pady=5, sticky="nsew")

        # --- 5. Bottom Action Frame ---
        self.frame_bottom = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_bottom.grid(row=5, column=0, padx=15, pady=15, sticky="ew")

        self.btn_save = ctk.CTkButton(
            self.frame_bottom,
            text="💾 Lưu & Áp Dụng",
            font=ctk.CTkFont(size=15, weight="bold"),
            height=40,
            fg_color="#059669",
            hover_color="#047857",
            command=self.save_and_close
        )
        self.btn_save.pack(side="right", padx=(5, 0))

        self.btn_cancel = ctk.CTkButton(
            self.frame_bottom,
            text="Đóng",
            font=ctk.CTkFont(size=14),
            height=40,
            width=90,
            fg_color="gray",
            hover_color="#4b5563",
            command=self.destroy
        )
        self.btn_cancel.pack(side="right", padx=5)

        self._update_key_count()

    def _extract_keys(self, raw_text: str) -> List[str]:
        if not raw_text:
            return []
        keys = [k.strip() for k in re.split(r"[,;\n\r\t]+", raw_text) if k.strip()]
        # Remove duplicates while preserving order
        seen = set()
        unique = []
        for k in keys:
            if k not in seen:
                seen.add(k)
                unique.append(k)
        return unique

    def _mask_key(self, key: str) -> str:
        if len(key) <= 10:
            return key
        return f"{key[:6]}...{key[-4:]}"

    def _update_key_count(self):
        content = self.txt_keys.get("1.0", "end-1c")
        keys = self._extract_keys(content)
        self.lbl_key_count.configure(text=f"{len(keys)} Key(s)")

    def paste_from_clipboard(self):
        try:
            text = self.clipboard_get()
            if text:
                current = self.txt_keys.get("1.0", "end-1c").strip()
                if current:
                    new_content = current + "\n" + text.strip()
                else:
                    new_content = text.strip()
                keys = self._extract_keys(new_content)
                self.txt_keys.delete("1.0", "end")
                self.txt_keys.insert("1.0", "\n".join(keys))
                self._update_key_count()
        except Exception:
            pass

    def clear_all(self):
        self.txt_keys.delete("1.0", "end")
        self.txt_results.delete("1.0", "end")
        self._update_key_count()
        self.test_results = {}
        self.lbl_test_summary.configure(text="")

    def clean_failed_keys(self):
        """Remove invalid/failed keys from the textbox and keep only working/untested ones."""
        content = self.txt_keys.get("1.0", "end-1c")
        keys = self._extract_keys(content)
        if not keys:
            return

        working_keys = []
        removed_count = 0
        for k in keys:
            res = self.test_results.get(k)
            if res and not res.get("is_valid", False):
                removed_count += 1
            else:
                working_keys.append(k)

        self.txt_keys.delete("1.0", "end")
        self.txt_keys.insert("1.0", "\n".join(working_keys))
        self._update_key_count()
        messagebox.showinfo(
            "Đã lọc xong",
            f"Đã loại bỏ {removed_count} key bị lỗi!\nCòn lại {len(working_keys)} key trong danh sách."
        )

    def start_test_keys(self):
        if self.is_testing:
            return

        content = self.txt_keys.get("1.0", "end-1c")
        keys = self._extract_keys(content)
        if not keys:
            messagebox.showwarning("Cảnh báo", "Vui lòng nhập ít nhất 1 API Key để kiểm tra!")
            return

        if requests is None:
            messagebox.showerror("Lỗi", "Thư viện 'requests' chưa được cài đặt.")
            return

        self.is_testing = True
        self.btn_test.configure(state="disabled", text="⏳ Đang kiểm tra...")
        self.txt_results.delete("1.0", "end")
        self.txt_results.insert("end", f"Đang bắt đầu kiểm tra song song {len(keys)} API Key...\n" + "-" * 70 + "\n\n")
        self.lbl_test_summary.configure(text=f"0/{len(keys)} đã kiểm tra...")
        self.test_results = {}

        threading.Thread(target=self._run_test_thread, args=(keys,), daemon=True).start()

    def _test_single_key(self, index: int, key: str) -> dict:
        """
        Test single API key — ported from truyen-ngan geminiService.checkSingleKey():
        POST to gemini-3.6-flash:generateContent with minimal payload ("ping", maxOutputTokens=5)
        """
        test_model = "gemini-3.6-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{test_model}:generateContent?key={key}"
        payload = {
            "contents": [{"parts": [{"text": "ping"}]}],
            "generationConfig": {"maxOutputTokens": 5}
        }
        masked = self._mask_key(key)
        start_t = time.time()

        try:
            resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
            elapsed = round(time.time() - start_t, 2)

            if resp.status_code == 200:
                return {
                    "index": index,
                    "key": key,
                    "masked": masked,
                    "is_valid": True,
                    "latency": elapsed,
                    "status_code": 200,
                    "msg": f"✅ Key #{index + 1} ({masked}): HỢP LỆ & HOẠT ĐỘNG TỐT (Độ trễ: {elapsed}s)"
                }
            elif resp.status_code == 429:
                # Rate limit / Resource exhausted — key valid but quota used up
                err_data = resp.json() if resp.text else {}
                err_msg = err_data.get("error", {}).get("message", "")
                is_resource = "resource" in err_msg.lower() or "quota" in err_msg.lower()
                return {
                    "index": index,
                    "key": key,
                    "masked": masked,
                    "is_valid": False,
                    "latency": elapsed,
                    "status_code": 429,
                    "msg": f"⚠️ Key #{index + 1} ({masked}): HẾT QUOTA / RATE LIMIT (HTTP 429{' - Resource Exhausted' if is_resource else ''})"
                }
            else:
                err_data = {}
                try:
                    err_data = resp.json()
                except Exception:
                    pass
                err_msg = err_data.get("error", {}).get("message", f"HTTP {resp.status_code}")
                is_denied = resp.status_code == 403 or "PERMISSION_DENIED" in err_msg or "denied access" in err_msg.lower()
                is_invalid = resp.status_code in (400, 401) or "API_KEY_INVALID" in err_msg

                if is_invalid:
                    label = "KEY KHÔNG HỢP LỆ (API Key Invalid)"
                elif is_denied:
                    label = "KEY BỊ KHÓA / TỪ CHỐI TRUY CẬP (Permission Denied)"
                else:
                    label = f"Lỗi: {err_msg[:80]}"

                return {
                    "index": index,
                    "key": key,
                    "masked": masked,
                    "is_valid": False,
                    "latency": elapsed,
                    "status_code": resp.status_code,
                    "msg": f"❌ Key #{index + 1} ({masked}): {label} (HTTP {resp.status_code})"
                }
        except Exception as e:
            elapsed = round(time.time() - start_t, 2)
            return {
                "index": index,
                "key": key,
                "masked": masked,
                "is_valid": False,
                "latency": elapsed,
                "status_code": 0,
                "msg": f"❌ Key #{index + 1} ({masked}): Không thể kết nối ({e})"
            }

    def _run_test_thread(self, keys: List[str]):
        results = [None] * len(keys)
        completed_count = 0
        valid_count = 0
        failed_count = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(keys))) as executor:
            futures = {executor.submit(self._test_single_key, idx, key): idx for idx, key in enumerate(keys)}

            for fut in concurrent.futures.as_completed(futures):
                idx = futures[fut]
                res = fut.result()
                results[idx] = res
                self.test_results[res["key"]] = res

                completed_count += 1
                if res["is_valid"]:
                    valid_count += 1
                else:
                    failed_count += 1

                # Update live log
                msg = res["msg"]
                self.after(0, lambda m=msg: self._append_result_line(m))
                self.after(0, lambda c=completed_count, t=len(keys), v=valid_count, f=failed_count: self.lbl_test_summary.configure(
                    text=f"Đã test: {c}/{t} ({v} Hoạt động | {f} Lỗi)"
                ))

        self.after(0, lambda v=valid_count, f=failed_count, t=len(keys): self._finish_testing(v, f, t))

    def _append_result_line(self, line: str):
        self.txt_results.insert("end", line + "\n")
        self.txt_results.see("end")

    def _finish_testing(self, valid: int, failed: int, total: int):
        self.is_testing = False
        self.btn_test.configure(state="normal", text="🧪 Test Toàn Bộ Key (Đa luồng)")
        self.txt_results.insert("end", "\n" + "=" * 70 + "\n")
        self.txt_results.insert("end", f"🎉 KẾT QUẢ: {valid}/{total} Key hoạt động tốt, {failed} Key lỗi hoặc hết hạn.\n")
        self.txt_results.see("end")

        if failed > 0:
            messagebox.showwarning(
                "Kiểm tra hoàn tất",
                f"Kiểm tra xong {total} Key:\n- {valid} Key hoạt động tốt ✅\n- {failed} Key bị lỗi / hết Quota ❌\n\nBạn có thể bấm '🧹 Loại Bỏ Key Lỗi' để dọn dẹp danh sách!"
            )
        else:
            messagebox.showinfo(
                "Kiểm tra hoàn tất",
                f"Tuyệt vời! Tất cả {valid}/{total} API Key đều hoạt động tốt 100%! 🎉"
            )

    def save_and_close(self):
        content = self.txt_keys.get("1.0", "end-1c")
        keys = self._extract_keys(content)
        keys_str = ", ".join(keys)

        if self.on_save_callback:
            self.on_save_callback(keys_str)

        self.destroy()
