"""Check Space controls and durable results, separate from Add Link history."""

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tkinter import messagebox

from modules.actions.capcut_add_link import CAPCUT_LOGIN_URL
from modules.actions.capcut_check_space import (
    run_capcut_check_fam_origin_workflow,
    run_capcut_check_space_workflow,
    run_capcut_out_fam_workflow,
)
from modules.actions.capcut_workflow import CapCutWorkflowInterrupted


class CheckSpaceMixin:
    def start_check_space(self):
        self._start_space_task(out_fam=False)

    def start_out_fam(self):
        self._start_space_task(out_fam=True)

    def start_check_fam_origin(self):
        self._start_space_task(check_fam_origin=True)

    def _start_space_task(self, out_fam=False, check_fam_origin=False):
        operation = "Check Gốc Fam" if check_fam_origin else ("Out Fam" if out_fam else "Check Space")
        if self.task_thread and self.task_thread.is_alive():
            messagebox.showinfo("Thông báo", "Một tác vụ CapCut đang chạy")
            return
        accounts = [dict(a) for a in self.add_link_accounts
                    if a["user"].casefold() in self.add_link_account_selected]
        accounts = list({a["user"].casefold(): a for a in accounts}.values())
        if not accounts:
            messagebox.showinfo("Thông báo", f"Hãy tích chọn tài khoản cần {operation}")
            return
        try:
            threads = max(1, int(self.add_link_threads_entry.get().strip() or "1"))
            delay = max(0.0, float(self.add_link_delay_entry.get().strip() or "0"))
        except ValueError:
            messagebox.showinfo("Thông báo", "Số luồng và Delay phải là số")
            return
        directory = "check gốc fam" if check_fam_origin else ("out fam" if out_fam else "check space")
        self.check_space_directory = Path(directory)
        try:
            self.check_space_directory.mkdir(exist_ok=True)
        except OSError as exc:
            messagebox.showerror(operation, f"Không thể lưu kết quả: {exc}")
            return
        self.check_space_lock = threading.Lock()
        for account in accounts:
            waiting = "Chờ Check Gốc Fam" if check_fam_origin else ("Chờ Out Fam" if out_fam else "Chờ kiểm tra")
            self._set_space_status(account["user"], waiting)
        self.stop_event.clear()
        self.current_accounts = accounts
        self.task_thread = threading.Thread(target=self.run_check_space_accounts,
                                            args=(accounts, threads, delay, out_fam, check_fam_origin), daemon=True)
        self.task_thread.start()

    def _record_space_event(self, user, event, data):
        entry = {"time": datetime.now().isoformat(timespec="seconds"),
                 "account": user, "event": event, **data}
        with self.check_space_lock:
            with (self.check_space_directory / "results.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            if event == "owner_found":
                # Keep a durable, directly exportable mapping even when the account
                # owns the space and cannot leave it through the member API.
                fields = (user, data["member_username"], data["owner_username"])
                with (self.check_space_directory / "owners.txt").open("a", encoding="utf-8") as f:
                    f.write("|".join(str(v).replace("\n", " ").replace("\r", " ") for v in fields) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            elif event == "fam_checked":
                fields = (user, data.get("owner_username", ""), data.get("workspace_id", ""),
                          data.get("space_name", ""),
                          data.get("member_count", 0), data.get("account_count", 0))
                with (self.check_space_directory / "members.txt").open("a", encoding="utf-8") as f:
                    f.write("|".join(str(v).replace("\n", " ").replace("\r", " ") for v in fields) + "\n")
                    f.flush()
                    os.fsync(f.fileno())

    def export_out_fam_accounts(self):
        selected = {user.casefold() for user in self.add_link_account_selected}
        source = Path("out fam") / "owners.txt"
        if not selected or not source.exists():
            messagebox.showinfo("Thông báo", "Chưa có dữ liệu Out Fam của tài khoản đã tích chọn")
            return
        rows = []
        seen = set()
        try:
            for raw in source.read_text(encoding="utf-8-sig").splitlines():
                parts = [part.strip() for part in raw.split("|")]
                if (len(parts) != 3 or parts[0].casefold() not in selected
                        or not re.fullmatch(r"user\d{6,}", parts[1], re.I)
                        or not re.fullmatch(r"user\d{6,}", parts[2], re.I)):
                    continue
                row = "|".join(parts)
                if row.casefold() not in seen:
                    seen.add(row.casefold())
                    rows.append(row)
        except OSError as exc:
            messagebox.showerror("Xuất Out Fam", f"Không thể đọc dữ liệu: {exc}")
            return
        if not rows:
            messagebox.showinfo("Thông báo", "Chưa có dữ liệu Out Fam của tài khoản đã tích chọn")
            return
        destination = Path("out fam") / "accounts.txt"
        try:
            destination.write_text("\n".join(rows) + "\n", encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Xuất Out Fam", f"Không thể xuất tài khoản: {exc}")
            return
        print(f"[CAPCUT][OUT FAM] Exported {len(rows)} rows to {destination}")

    def export_checked_fam_members(self):
        """Export the latest completed Fam count for checked owner accounts."""
        selected = {user.casefold() for user in self.add_link_account_selected}
        source = Path("check gốc fam") / "results.jsonl"
        if not selected or not source.exists():
            messagebox.showinfo(
                "Thông báo",
                "Chưa có kết quả Check Gốc Fam của tài khoản đã tích chọn",
            )
            return
        latest = {}
        try:
            for raw in source.read_text(encoding="utf-8-sig").splitlines():
                try:
                    entry = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(entry, dict) or entry.get("event") != "completed":
                    continue
                user = str(entry.get("account") or "").strip()
                owner_username = str(entry.get("owner_username") or "").strip()
                count = entry.get("total_members")
                if (user.casefold() not in selected
                        or not re.fullmatch(r"user\d{6,}", owner_username, re.I)
                        or type(count) is not int or count < 0):
                    continue
                latest[user.casefold()] = (user, owner_username, count)
        except OSError as exc:
            messagebox.showerror("Xuất Check Gốc Fam", f"Không thể đọc dữ liệu: {exc}")
            return
        # Follow the visible table order and export each selected owner once.
        rows = []
        for account in self.add_link_accounts:
            key = account["user"].casefold()
            if key in selected and key in latest:
                rows.append(f"{account['user']}|{latest[key][1]}|{latest[key][2]}")
        if not rows:
            messagebox.showinfo(
                "Thông báo",
                "Các tài khoản đã tích chọn chưa có kết quả Check Gốc Fam hoàn tất",
            )
            return
        destination = Path("check gốc fam") / "accounts.txt"
        try:
            destination.write_text("\n".join(rows) + "\n", encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Xuất Check Gốc Fam", f"Không thể xuất tài khoản: {exc}")
            return
        print(f"[CAPCUT][CHECK GỐC FAM] Exported {len(rows)} rows to {destination}")

    def _set_space_status(self, user, result):
        key = user.casefold()
        for account in self.add_link_accounts:
            if account["user"].casefold() == key:
                account["space_status"] = result
                break
        self._save_add_link_account_state()
        try:
            self.root.after(0, self.refresh_add_link_status)
        except RuntimeError:
            pass

    def run_check_space_accounts(self, accounts, threads, delay, out_fam=False, check_fam_origin=False):
        operation = "CHECK GỐC FAM" if check_fam_origin else ("OUT FAM" if out_fam else "CHECK SPACE")
        try:
            if self.proxies:
                self.reset_loaded_proxy_ips()
            with ThreadPoolExecutor(max_workers=threads) as pool:
                futures = []
                for index, account in enumerate(accounts):
                    if self.stop_event.is_set():
                        break
                    futures.append(pool.submit(self.run_check_space_worker, account, index, threads,
                                               out_fam, check_fam_origin))
                    if index < len(accounts) - 1 and self.stop_event.wait(delay):
                        break
                for future in futures:
                    future.result()
        finally:
            if self.stop_event.is_set():
                print(f"[CAPCUT][{operation}] Stopped")
            else:
                print(f"[CAPCUT][{operation}] Finished. Results: {self.check_space_directory}")
            self.current_accounts = []
            self.task_thread = None

    def run_check_space_worker(self, account, index, threads, out_fam=False, check_fam_origin=False):
        user = account["user"]
        operation = "CHECK GỐC FAM" if check_fam_origin else ("OUT FAM" if out_fam else "CHECK SPACE")
        process = None
        try:
            if self.stop_event.is_set():
                raise CapCutWorkflowInterrupted("Stop requested")
            running = "Đang Check Gốc Fam" if check_fam_origin else ("Đang Out Fam" if out_fam else "Đang kiểm tra Space")
            self._set_space_status(user, running)
            process, start = self._start_account_browser(account, index, threads, startup_url=CAPCUT_LOGIN_URL)
            workflow = (run_capcut_check_fam_origin_workflow if check_fam_origin else
                        (run_capcut_out_fam_workflow if out_fam else run_capcut_check_space_workflow))
            result = workflow(
                account, {"start_result": start, "stop_event": self.stop_event},
                lambda event, data: self._record_space_event(user, event, data),
            )
            self._record_space_event(user, "completed", result)
            if result["status"] == "no owned teams":
                text = "Không có space Teams do tài khoản này làm owner"
            elif check_fam_origin:
                details = ", ".join(
                    f"{space['space_name'] or space['workspace_id']}: {space['member_count']} member"
                    for space in result["spaces"]
                )
                text = (f"Tổng {result['total_members']} member (không tính owner) | "
                        f"{result['total_accounts']} tài khoản gồm owner")
                if len(result["spaces"]) > 1:
                    text += f" | {details}"
            elif result["status"] == "no teams":
                text = "Không tham gia space Teams nào"
            elif out_fam:
                owners = ", ".join(s["owner_username"] for s in result["left"])
                text = f"Đã out toàn bộ {len(result['left'])} fam"
                if owners:
                    text += f" | Owner: {owners}"
            else:
                owners = ", ".join(s["owner_username"] for s in result["left"])
                text = f"Đã xác minh còn 1 space Teams; đã rời {len(result['left'])}"
                if owners:
                    text += f" | Owner: {owners}"
        except CapCutWorkflowInterrupted:
            text = "Đã dừng — xem results.jsonl nếu đang rời space"
            self._record_space_event(user, "stopped", {})
        except Exception as exc:
            text = f"Chưa xác minh: {exc}"
            self._record_space_event(user, "error", {"message": str(exc)})
        finally:
            self._close_standalone_process(process)
        self._set_space_status(user, text)
        print(f"[CAPCUT][{operation}] {user} | {text}")
