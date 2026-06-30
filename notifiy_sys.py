import json
import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, VERTICAL, W, X, Y
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import paramiko

try:
    import pygame
except Exception:
    pygame = None



def get_app_dir() -> Path:
    """Return the directory of the running application (exe or script)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = get_app_dir()
CONFIG_FILE = APP_DIR / "notify_sys_config.json"
ICON_FILE = "notification_bell.ico"
DEFAULT_POLL_INTERVAL = 2


@dataclass
class MonitorSettings:
    host: str
    username: str
    key_path: str
    mp3_path: str
    directories: list[str]
    directory_timeouts: dict[str, int]


class ConfigManager:
    @staticmethod
    def load() -> dict:
        if not CONFIG_FILE.exists():
            return {}
        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    @staticmethod
    def save(data: dict) -> None:
        try:
            with CONFIG_FILE.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            raise RuntimeError(f"Unable to save config file: {exc}") from exc


class AudioPlayer:
    """Single audio channel policy: restart playback from beginning on each trigger."""

    def __init__(self):
        self._lock = threading.Lock()
        self._initialized = False
        self._last_loaded = None
        self._play_timer: threading.Timer | None = None

    def trigger(self, mp3_path: str, duration_seconds: int = 1200) -> tuple[bool, str]:
        if pygame is None:
            return False, "pygame is not installed. Install pygame to enable audio playback."

        with self._lock:
            try:
                if not self._initialized:
                    pygame.mixer.init()
                    self._initialized = True

                normalized = os.path.abspath(mp3_path)
                if self._last_loaded != normalized:
                    pygame.mixer.music.load(normalized)
                    self._last_loaded = normalized

                # Cancel any existing timed stop before restarting.
                if self._play_timer is not None:
                    self._play_timer.cancel()
                    self._play_timer = None

                # Loop indefinitely; a timer will stop it after the requested duration.
                pygame.mixer.music.stop()
                pygame.mixer.music.play(loops=-1)

                self._play_timer = threading.Timer(duration_seconds, self._timed_stop)
                self._play_timer.daemon = True
                self._play_timer.start()

                mins = duration_seconds // 60
                return True, f"Audio playback started (looping for {mins} minute(s))."
            except Exception as exc:
                return False, f"Audio playback error: {exc}"

    def _timed_stop(self) -> None:
        """Called by the timer thread to stop playback after the scheduled duration."""
        with self._lock:
            try:
                if self._initialized:
                    pygame.mixer.music.stop()
            except Exception:
                pass
            finally:
                self._play_timer = None

    def shutdown(self) -> None:
        if pygame is None:
            return
        with self._lock:
            try:
                if self._play_timer is not None:
                    self._play_timer.cancel()
                    self._play_timer = None
                if self._initialized:
                    pygame.mixer.music.stop()
                    pygame.mixer.quit()
            except Exception:
                pass
            finally:
                self._initialized = False
                self._last_loaded = None

    def stop(self) -> tuple[bool, str]:
        if pygame is None:
            return False, "pygame is not installed."
        with self._lock:
            try:
                if self._play_timer is not None:
                    self._play_timer.cancel()
                    self._play_timer = None
                if not self._initialized:
                    return True, "No active audio playback."
                pygame.mixer.music.stop()
                return True, "Audio playback stopped."
            except Exception as exc:
                return False, f"Audio stop error: {exc}"


class MonitorWorker(threading.Thread):
    def __init__(self, settings: MonitorSettings, event_queue: queue.Queue, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.settings = settings
        self.event_queue = event_queue
        self.stop_event = stop_event
        self.state_lock = threading.Lock()

        self.ssh_client = None
        self.sftp = None
        self.previous_snapshots: dict[str, dict[str, tuple[int, int]]] = {}
        self.last_change_time: dict[str, float] = {}
        self.timeout_triggered: dict[str, bool] = {}

    def emit(self, event_type: str, **payload):
        payload["event_type"] = event_type
        self.event_queue.put(payload)

    def connect(self) -> None:
        self.emit("log", message=f"Connecting to {self.settings.host}...")
        self.ssh_client = paramiko.SSHClient()
        self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh_client.connect(
            hostname=self.settings.host,
            username=self.settings.username,
            key_filename=self.settings.key_path,
            timeout=15,
        )
        self.sftp = self.ssh_client.open_sftp()
        self.emit("connected", message="SSH/SFTP connected.")

    def close_connections(self) -> None:
        try:
            if self.sftp is not None:
                self.sftp.close()
        except Exception:
            pass
        finally:
            self.sftp = None

        try:
            if self.ssh_client is not None:
                self.ssh_client.close()
        except Exception:
            pass
        finally:
            self.ssh_client = None

    def get_snapshot(self, remote_dir: str) -> dict[str, tuple[int, int]]:
        snapshot = {}
        for file_attr in self.sftp.listdir_attr(remote_dir):
            snapshot[file_attr.filename] = (file_attr.st_size, int(file_attr.st_mtime))
        return snapshot

    def initialize_directory_state(self):
        now = time.time()
        with self.state_lock:
            self.previous_snapshots.clear()
            self.last_change_time.clear()
            self.timeout_triggered.clear()

            for directory in self.settings.directories:
                try:
                    snapshot = self.get_snapshot(directory)
                    self.previous_snapshots[directory] = snapshot
                    self.last_change_time[directory] = now
                    self.timeout_triggered[directory] = False
                    self.emit("directory_ready", directory=directory, message="Ready")
                except Exception as exc:
                    self.previous_snapshots[directory] = {}
                    self.last_change_time[directory] = now
                    self.timeout_triggered[directory] = False
                    self.emit("directory_error", directory=directory, message=f"Cannot access directory: {exc}")

    def remonitor_directory(self, directory: str) -> tuple[bool, str]:
        with self.state_lock:
            if directory not in self.settings.directories:
                return False, "Directory is not part of the active monitoring list."
            if self.sftp is None:
                return False, "SSH/SFTP is not currently connected."

            try:
                snapshot = self.get_snapshot(directory)
            except Exception as exc:
                self.emit("directory_error", directory=directory, message=f"Cannot remonitor directory: {exc}")
                return False, f"Cannot access directory: {exc}"

            self.previous_snapshots[directory] = snapshot
            self.last_change_time[directory] = time.time()
            self.timeout_triggered[directory] = False
            remaining = self.settings.directory_timeouts.get(directory, 0)

        self.emit(
            "directory_remonitored",
            directory=directory,
            remaining_seconds=remaining,
            message="Directory monitoring timer reset.",
        )
        return True, "Directory monitoring timer reset."

    def run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    self.connect()
                    self.initialize_directory_state()
                    self.monitor_loop()
                except Exception as exc:
                    self.emit("error", message=f"SSH error: {exc}")
                    self.emit("log", message="Will retry SSH connection in 5 seconds.")
                    self.close_connections()
                    if self._sleep_with_stop(5):
                        break
        except Exception:
            self.emit("error", message="Unexpected monitor failure.")
            self.emit("log", message=traceback.format_exc())
        finally:
            self.close_connections()
            self.emit("stopped", message="Monitoring stopped.")

    def monitor_loop(self):
        while not self.stop_event.is_set():
            now = time.time()
            for directory in self.settings.directories:
                if self.stop_event.is_set():
                    break

                with self.state_lock:
                    try:
                        current_snapshot = self.get_snapshot(directory)
                    except Exception as exc:
                        if self._should_reconnect_on_error(exc):
                            # Bubble up so run() can close handles and reconnect.
                            raise RuntimeError(f"Connection lost while reading {directory}: {exc}") from exc
                        self.emit("directory_error", directory=directory, message=f"Directory read failed: {exc}")
                        continue

                    previous_snapshot = self.previous_snapshots.get(directory, {})
                    if current_snapshot != previous_snapshot:
                        self._emit_changes(directory, previous_snapshot, current_snapshot)
                        self.previous_snapshots[directory] = current_snapshot
                        self.last_change_time[directory] = now
                        self.timeout_triggered[directory] = False

                    elapsed = now - self.last_change_time.get(directory, now)
                    timeout_seconds = self.settings.directory_timeouts.get(directory, 0)
                    remaining = max(0, timeout_seconds - int(elapsed))
                    self.emit("countdown", directory=directory, remaining_seconds=remaining)

                    if elapsed >= timeout_seconds and not self.timeout_triggered.get(directory, False):
                        self.timeout_triggered[directory] = True
                        self.emit("timeout", directory=directory, message="Timeout reached with no changes.")

            if self._sleep_with_stop(DEFAULT_POLL_INTERVAL):
                return

    def _should_reconnect_on_error(self, exc: Exception) -> bool:
        reconnect_types = (
            paramiko.SSHException,
            paramiko.ssh_exception.NoValidConnectionsError,
            OSError,
            EOFError,
        )

        current = exc
        visited = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))

            if isinstance(current, reconnect_types):
                return True

            text = str(current).lower()
            if "socket is closed" in text or "connection reset" in text or "broken pipe" in text:
                return True

            current = current.__cause__ or current.__context__

        return False

    def _emit_changes(self, directory: str, old_snapshot: dict, new_snapshot: dict):
        new_files = set(new_snapshot) - set(old_snapshot)
        removed_files = set(old_snapshot) - set(new_snapshot)
        modified_files = {
            name
            for name in set(new_snapshot) & set(old_snapshot)
            if new_snapshot[name] != old_snapshot[name]
        }

        if new_files:
            for filename in sorted(new_files):
                self.emit("file_change", directory=directory, change_type="NEW", filename=filename)
        if removed_files:
            for filename in sorted(removed_files):
                self.emit("file_change", directory=directory, change_type="REMOVED", filename=filename)
        if modified_files:
            for filename in sorted(modified_files):
                self.emit("file_change", directory=directory, change_type="MODIFIED", filename=filename)

    def _sleep_with_stop(self, seconds: float) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            if self.stop_event.is_set():
                return True
            time.sleep(0.2)
        return False


class NotifyApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SSH Directory Timeout Monitoring System")
        self._apply_window_icon()
        self.geometry("1100x760")
        self.minsize(980, 680)

        self.event_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.audio_player = AudioPlayer()

        self.host_var = tk.StringVar()
        self.user_var = tk.StringVar()
        self.key_var = tk.StringVar()
        self.mp3_var = tk.StringVar(value=r"C:\Windows\Media\Alarm01.wav")
        self.play_duration_var = tk.StringVar(value="20")
        self.initial_dir_count_var = tk.StringVar(value="1")

        self.directory_rows = []
        self.directory_status_items = {}

        self._build_ui()
        self._load_config_into_ui()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self.process_event_queue)

    def _apply_window_icon(self):
        """Set app window icon from notification_bell.ico if available."""
        try:
            icon_candidates = [
                APP_DIR / ICON_FILE,
                Path.cwd() / ICON_FILE,
            ]
            for icon_path in icon_candidates:
                if icon_path.exists():
                    self.iconbitmap(str(icon_path))
                    return
        except Exception:
            # Keep startup resilient if icon path/format has issues.
            pass

    def _build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill=BOTH, expand=True)

        cfg = ttk.LabelFrame(root, text="Connection and Settings", padding=10)
        cfg.pack(fill=X)

        ttk.Label(cfg, text="SSH Host:").grid(row=0, column=0, sticky=W, padx=4, pady=4)
        ttk.Entry(cfg, textvariable=self.host_var, width=48).grid(row=0, column=1, sticky=W, padx=4, pady=4)

        ttk.Label(cfg, text="SSH Username:").grid(row=0, column=2, sticky=W, padx=4, pady=4)
        ttk.Entry(cfg, textvariable=self.user_var, width=22).grid(row=0, column=3, sticky=W, padx=4, pady=4)

        ttk.Label(cfg, text="Private Key:").grid(row=1, column=0, sticky=W, padx=4, pady=4)
        ttk.Entry(cfg, textvariable=self.key_var, width=72).grid(row=1, column=1, columnspan=2, sticky=W, padx=4, pady=4)
        ttk.Button(cfg, text="Browse", command=self.browse_key).grid(row=1, column=3, sticky=W, padx=4, pady=4)

        ttk.Label(cfg, text="Music File:").grid(row=2, column=0, sticky=W, padx=4, pady=4)
        ttk.Entry(cfg, textvariable=self.mp3_var, width=72).grid(row=2, column=1, columnspan=2, sticky=W, padx=4, pady=4)
        mp3_actions = ttk.Frame(cfg)
        mp3_actions.grid(row=2, column=3, sticky=W, padx=4, pady=4)
        ttk.Button(mp3_actions, text="Browse", command=self.browse_mp3).pack(side=LEFT, padx=(0, 4))
        ttk.Button(mp3_actions, text="Stop Music", command=self.stop_music).pack(side=LEFT)

        ttk.Label(cfg, text="Play Duration:").grid(row=3, column=0, sticky=W, padx=4, pady=4)
        play_dur_frame = ttk.Frame(cfg)
        play_dur_frame.grid(row=3, column=1, sticky=W, padx=4, pady=4)
        ttk.Entry(play_dur_frame, textvariable=self.play_duration_var, width=8).pack(side=LEFT)
        ttk.Label(play_dur_frame, text="minutes  (music loops until this duration is reached)").pack(side=LEFT, padx=(6, 0))

        actions = ttk.Frame(root, padding=(0, 8, 0, 8))
        actions.pack(fill=X)
        self.start_btn = ttk.Button(actions, text="Start Monitoring", command=self.start_monitoring)
        self.start_btn.pack(side=LEFT, padx=4)
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop_monitoring, state="disabled")
        self.stop_btn.pack(side=LEFT, padx=4)

        dirs_box = ttk.LabelFrame(root, text="Remote Directories", padding=10)
        dirs_box.pack(fill=X, pady=(0, 8))

        top_dirs = ttk.Frame(dirs_box)
        top_dirs.pack(fill=X)
        ttk.Label(top_dirs, text="Initial rows:").pack(side=LEFT, padx=(0, 6))
        ttk.Entry(top_dirs, textvariable=self.initial_dir_count_var, width=8).pack(side=LEFT)
        ttk.Button(top_dirs, text="Generate Rows", command=self.generate_directory_rows).pack(side=LEFT, padx=6)
        ttk.Button(top_dirs, text="Add Directory", command=self.add_directory_row).pack(side=LEFT)

        self.rows_frame = ttk.Frame(dirs_box)
        self.rows_frame.pack(fill=X, pady=(8, 0))

        bottom = ttk.Panedwindow(root, orient="horizontal")
        bottom.pack(fill=BOTH, expand=True)

        status_panel = ttk.Labelframe(bottom, text="Per-Directory Countdown / State", padding=8)
        log_panel = ttk.Labelframe(bottom, text="Log / Status", padding=8)
        bottom.add(status_panel, weight=1)
        bottom.add(log_panel, weight=1)

        self.status_tree = ttk.Treeview(status_panel, columns=("directory", "remaining", "state"), show="headings", height=16)
        self.status_tree.heading("directory", text="Directory")
        self.status_tree.heading("remaining", text="Remaining")
        self.status_tree.heading("state", text="State")
        self.status_tree.column("directory", width=420, anchor=W)
        self.status_tree.column("remaining", width=100, anchor="center")
        self.status_tree.column("state", width=160, anchor=W)
        self.status_tree.pack(side=LEFT, fill=BOTH, expand=True)

        tree_scroll = ttk.Scrollbar(status_panel, orient=VERTICAL, command=self.status_tree.yview)
        tree_scroll.pack(side=RIGHT, fill=Y)
        self.status_tree.configure(yscrollcommand=tree_scroll.set)

        self.log_text = tk.Text(log_panel, wrap="word", height=18, state="disabled")
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)
        log_scroll = ttk.Scrollbar(log_panel, orient=VERTICAL, command=self.log_text.yview)
        log_scroll.pack(side=RIGHT, fill=Y)
        self.log_text.configure(yscrollcommand=log_scroll.set)

        footer = ttk.Frame(root, padding=(0, 8, 0, 0))
        footer.pack(fill=X)
        ttk.Label(footer, text="version=3.0").pack(side=RIGHT)
        ttk.Label(footer, text="Please feedback any bug to yee.liang.sak@altera.com").pack(anchor=W)
        doc_label = ttk.Label(
            footer,
            text="Documentation: https://altera-corp.atlassian.net/wiki/x/OwS1T",
            foreground="blue",
            cursor="hand2",
        )
        doc_label.pack(anchor=W)
        doc_label.bind("<Button-1>", lambda _event: webbrowser.open("https://altera-corp.atlassian.net/wiki/x/OwS1T"))

        self.generate_directory_rows(initial=True)

    def browse_key(self):
        path = filedialog.askopenfilename(title="Select SSH Private Key")
        if path:
            self.key_var.set(path)

    def browse_mp3(self):
        path = filedialog.askopenfilename(
            title="Select Music File",
            initialdir=r"C:\Windows\Media",
            initialfile="Alarm01.wav",
            filetypes=[
                ("Audio files", "*.mp3 *.wav"),
                ("MP3 files", "*.mp3"),
                ("WAV files", "*.wav"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.mp3_var.set(path)

    def stop_music(self):
        ok, detail = self.audio_player.stop()
        if ok:
            self.append_log(detail)
        else:
            self.append_log(f"Playback error: {detail}")

    def generate_directory_rows(self, initial: bool = False):
        try:
            count = int(self.initial_dir_count_var.get().strip())
        except ValueError:
            if not initial:
                messagebox.showerror("Invalid Number", "Initial rows must be an integer.")
            return

        if count < 1:
            if not initial:
                messagebox.showerror("Invalid Number", "Initial rows must be at least 1.")
            return

        for row in self.directory_rows:
            row["frame"].destroy()
        self.directory_rows.clear()

        for _ in range(count):
            self.add_directory_row()

    def add_directory_row(self, value: str = ""):
        row_frame = ttk.Frame(self.rows_frame)
        row_frame.pack(fill=X, pady=2)

        dir_var = tk.StringVar(value=value)
        timeout_h_var = tk.StringVar(value="0")
        timeout_m_var = tk.StringVar(value="10")
        timeout_s_var = tk.StringVar(value="0")
        ttk.Label(row_frame, text="Path:").pack(side=LEFT, padx=(0, 6))
        entry = ttk.Entry(row_frame, textvariable=dir_var, width=70)
        entry.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(row_frame, text="Timeout(H:M:S):").pack(side=LEFT, padx=(10, 4))
        ttk.Entry(row_frame, textvariable=timeout_h_var, width=4).pack(side=LEFT)
        ttk.Label(row_frame, text=":").pack(side=LEFT)
        ttk.Entry(row_frame, textvariable=timeout_m_var, width=4).pack(side=LEFT)
        ttk.Label(row_frame, text=":").pack(side=LEFT)
        ttk.Entry(row_frame, textvariable=timeout_s_var, width=4).pack(side=LEFT)
        remonitor_btn = ttk.Button(row_frame, text="Remonitor", command=lambda: self.remonitor_row(row_frame))
        remonitor_btn.pack(side=LEFT, padx=(6, 0))
        remove_btn = ttk.Button(row_frame, text="Remove", command=lambda: self.remove_directory_row(row_frame))
        remove_btn.pack(side=LEFT, padx=(6, 0))

        self.directory_rows.append(
            {
                "frame": row_frame,
                "var": dir_var,
                "entry": entry,
                "timeout_h_var": timeout_h_var,
                "timeout_m_var": timeout_m_var,
                "timeout_s_var": timeout_s_var,
                "remonitor": remonitor_btn,
                "remove": remove_btn,
            }
        )

    def remonitor_row(self, frame):
        row = next((r for r in self.directory_rows if r["frame"] == frame), None)
        if row is None:
            return

        directory = row["var"].get().strip()
        if not directory:
            messagebox.showwarning("Missing Directory", "Please enter a directory path first.")
            return

        if self.worker is None or not self.worker.is_alive():
            messagebox.showinfo("Not Running", "Monitoring is not running right now.")
            return

        ok, detail = self.worker.remonitor_directory(directory)
        if ok:
            self.append_log(f"REMONITOR [{directory}] {detail}")
            self._set_directory_state(directory, state="Monitoring")
            timeout_seconds = self.worker.settings.directory_timeouts.get(directory, 0)
            self._set_directory_remaining(directory, timeout_seconds)
        else:
            messagebox.showerror("Remonitor Failed", detail)

    def remove_directory_row(self, frame):
        if len(self.directory_rows) <= 1:
            messagebox.showwarning("Cannot Remove", "At least one directory row is required.")
            return

        row_to_remove = None
        for row in self.directory_rows:
            if row["frame"] == frame:
                row_to_remove = row
                break

        if row_to_remove is not None:
            row_to_remove["frame"].destroy()
            self.directory_rows.remove(row_to_remove)

    def append_log(self, message: str):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert(END, line)
        self.log_text.see(END)
        self.log_text.configure(state="disabled")

    def collect_directory_configs(self) -> list[dict]:
        configs = []
        for row in self.directory_rows:
            path = row["var"].get().strip()
            if path:
                configs.append(
                    {
                        "path": path,
                        "hours": row["timeout_h_var"].get().strip(),
                        "minutes": row["timeout_m_var"].get().strip(),
                        "seconds": row["timeout_s_var"].get().strip(),
                    }
                )
        return configs

    def validate_inputs(self) -> MonitorSettings | None:
        host = self.host_var.get().strip()
        username = self.user_var.get().strip()
        key_path = self.key_var.get().strip()
        mp3_path = self.mp3_var.get().strip()

        directory_configs = self.collect_directory_configs()

        if not host:
            messagebox.showerror("Missing Field", "SSH host is required.")
            return None
        if not username:
            messagebox.showerror("Missing Field", "SSH username is required.")
            return None
        if not key_path:
            messagebox.showerror("Missing Field", "SSH private key path is required.")
            return None
        if not os.path.isfile(key_path):
            messagebox.showerror("Invalid Key", "SSH private key file does not exist.")
            return None
        if not mp3_path:
            messagebox.showerror("Missing Field", "Local music file path is required.")
            return None
        if not os.path.isfile(mp3_path):
            messagebox.showerror("Invalid Music File", "Music file does not exist.")
            return None

        play_duration_str = self.play_duration_var.get().strip()
        try:
            play_duration_minutes = int(play_duration_str)
            if play_duration_minutes < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Duration", "Play duration must be a positive integer (minutes).")
            return None
        if not directory_configs:
            messagebox.showerror("Missing Directories", "Enter at least one remote directory.")
            return None

        directories: list[str] = []
        directory_timeouts: dict[str, int] = {}
        seen_directories: set[str] = set()
        for index, cfg in enumerate(directory_configs, start=1):
            directory = cfg["path"]
            if directory in seen_directories:
                messagebox.showerror(
                    "Duplicate Directory",
                    f"Directory row {index} duplicates an existing path. Use unique directory paths.",
                )
                return None
            seen_directories.add(directory)
            try:
                h = int(cfg["hours"])
                m = int(cfg["minutes"])
                s = int(cfg["seconds"])
            except ValueError:
                messagebox.showerror(
                    "Invalid Timeout",
                    f"Directory row {index} has non-integer timeout values. Use H:M:S integers.",
                )
                return None

            if h < 0 or m < 0 or s < 0 or m > 59 or s > 59:
                messagebox.showerror(
                    "Invalid Timeout",
                    f"Directory row {index} has invalid timeout. Minutes/seconds must be 0-59.",
                )
                return None

            timeout_seconds = h * 3600 + m * 60 + s
            if timeout_seconds <= 0:
                messagebox.showerror(
                    "Invalid Timeout",
                    f"Directory row {index} timeout must be greater than zero.",
                )
                return None

            directories.append(directory)
            directory_timeouts[directory] = timeout_seconds

        return MonitorSettings(
            host=host,
            username=username,
            key_path=key_path,
            mp3_path=mp3_path,
            directories=directories,
            directory_timeouts=directory_timeouts,
        )

    def save_current_config(self):
        data = {
            "host": self.host_var.get().strip(),
            "username": self.user_var.get().strip(),
            "key_path": self.key_var.get().strip(),
            "mp3_path": self.mp3_var.get().strip(),
            "play_duration_minutes": self.play_duration_var.get().strip(),
            "directories": self.collect_directory_configs(),
            "initial_dir_count": self.initial_dir_count_var.get().strip(),
        }
        ConfigManager.save(data)

    def _load_config_into_ui(self):
        data = ConfigManager.load()
        if not data:
            return

        self.host_var.set(str(data.get("host", "")))
        self.user_var.set(str(data.get("username", "")))
        self.key_var.set(str(data.get("key_path", "")))
        self.mp3_var.set(str(data.get("mp3_path", r"C:\Windows\Media\Alarm01.wav")))
        self.play_duration_var.set(str(data.get("play_duration_minutes", "20")))

        saved_dirs = data.get("directories", [])
        if isinstance(saved_dirs, list) and saved_dirs:
            self.initial_dir_count_var.set(str(len(saved_dirs)))
            self.generate_directory_rows()
            for idx, value in enumerate(saved_dirs):
                if idx < len(self.directory_rows):
                    row = self.directory_rows[idx]
                    if isinstance(value, dict):
                        row["var"].set(str(value.get("path", "")))
                        row["timeout_h_var"].set(str(value.get("hours", "0")))
                        row["timeout_m_var"].set(str(value.get("minutes", "10")))
                        row["timeout_s_var"].set(str(value.get("seconds", "0")))
                    else:
                        # Backward compatibility for old config format.
                        row["var"].set(str(value))
                        row["timeout_h_var"].set(str(data.get("timeout_hours", "0")))
                        row["timeout_m_var"].set(str(data.get("timeout_minutes", "10")))
                        row["timeout_s_var"].set(str(data.get("timeout_seconds", "0")))
        else:
            self.generate_directory_rows(initial=True)

    def _clear_status_table(self):
        for item in self.status_tree.get_children():
            self.status_tree.delete(item)
        self.directory_status_items.clear()

    def _init_status_table(self, directories: list[str], directory_timeouts: dict[str, int]):
        self._clear_status_table()
        for d in directories:
            item = self.status_tree.insert(
                "",
                END,
                values=(d, self.format_seconds(directory_timeouts[d]), "Waiting"),
            )
            self.directory_status_items[d] = item

    @staticmethod
    def format_seconds(value: int) -> str:
        value = max(0, int(value))
        h = value // 3600
        m = (value % 3600) // 60
        s = value % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def set_buttons_running(self, running: bool):
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")

    def start_monitoring(self):
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo("Already Running", "Monitoring is already running.")
            return

        settings = self.validate_inputs()
        if settings is None:
            return

        try:
            self.save_current_config()
        except Exception as exc:
            messagebox.showerror("Config Error", str(exc))
            return

        self.stop_event.clear()
        self._init_status_table(settings.directories, settings.directory_timeouts)
        self.worker = MonitorWorker(settings, self.event_queue, self.stop_event)
        self.worker.start()
        self.append_log("Monitoring started.")
        self.set_buttons_running(True)

    def stop_monitoring(self):
        ok, detail = self.audio_player.stop()
        if ok:
            self.append_log(detail)
        else:
            self.append_log(f"Playback error: {detail}")

        if self.worker is None:
            return

        self.stop_event.set()
        self.append_log("Stopping monitor thread...")

        # Join with timeout so UI does not hang indefinitely.
        self.worker.join(timeout=4)
        if self.worker.is_alive():
            self.append_log("Monitor thread is still stopping in background.")
        else:
            self.append_log("Monitor thread stopped.")
            self.worker = None

        self.set_buttons_running(False)

    def process_event_queue(self):
        try:
            while True:
                event = self.event_queue.get_nowait()
                self.handle_event(event)
        except queue.Empty:
            pass
        finally:
            self.after(150, self.process_event_queue)

    def handle_event(self, event: dict):
        event_type = event.get("event_type")

        if event_type == "log":
            self.append_log(event.get("message", ""))
        elif event_type == "connected":
            self.append_log(event.get("message", "Connected"))
        elif event_type == "error":
            self.append_log(f"ERROR: {event.get('message', '')}")
        elif event_type == "directory_ready":
            directory = event.get("directory", "")
            self.append_log(f"Directory ready: {directory}")
            self._set_directory_state(directory, state="Monitoring")
        elif event_type == "directory_error":
            directory = event.get("directory", "")
            msg = event.get("message", "")
            self.append_log(f"Directory error [{directory}]: {msg}")
            self._set_directory_state(directory, state="Error")
        elif event_type == "file_change":
            directory = event.get("directory", "")
            ctype = event.get("change_type", "")
            filename = event.get("filename", "")
            self.append_log(f"{ctype} [{directory}] {filename}")
            self._set_directory_state(directory, state="Changed")
        elif event_type == "countdown":
            directory = event.get("directory", "")
            remaining = int(event.get("remaining_seconds", 0))
            self._set_directory_remaining(directory, remaining)
            if remaining > 0:
                self._set_directory_state(directory, state="Monitoring")
        elif event_type == "timeout":
            directory = event.get("directory", "")
            self.append_log(f"TIMEOUT [{directory}] {event.get('message', '')}")
            self._set_directory_state(directory, state="Timeout")
            self._set_directory_remaining(directory, 0)
            self.show_timeout_popup(directory)
            if self.worker is not None:
                try:
                    duration_minutes = int(self.play_duration_var.get().strip())
                except ValueError:
                    duration_minutes = 20
                duration_seconds = max(60, duration_minutes * 60)
                ok, detail = self.audio_player.trigger(self.worker.settings.mp3_path, duration_seconds)
                if ok:
                    self.append_log(f"Playback event [{directory}]: {detail}")
                else:
                    self.append_log(f"Playback error [{directory}]: {detail}")
        elif event_type == "directory_remonitored":
            directory = event.get("directory", "")
            remaining = int(event.get("remaining_seconds", 0))
            self.append_log(f"REMONITOR [{directory}] {event.get('message', '')}")
            self._set_directory_state(directory, state="Monitoring")
            self._set_directory_remaining(directory, remaining)
        elif event_type == "stopped":
            self.append_log(event.get("message", "Stopped"))
            self.worker = None
            self.set_buttons_running(False)

    def show_timeout_popup(self, directory: str):
        popup = tk.Toplevel(self)
        popup.title("Directory Timeout Alert")
        width = 460
        height = 180
        screen_w = popup.winfo_screenwidth()
        screen_h = popup.winfo_screenheight()
        pos_x = max(0, (screen_w - width) // 2)
        pos_y = max(0, (screen_h - height) // 2)
        popup.geometry(f"{width}x{height}+{pos_x}+{pos_y}")
        popup.minsize(420, 150)
        popup.transient(self)

        container = ttk.Frame(popup, padding=14)
        container.pack(fill=BOTH, expand=True)

        ttk.Label(container, text="Monitoring timeout detected.", font=("Segoe UI", 11, "bold")).pack(anchor=W)
        ttk.Label(container, text=f"Timed out path: {directory}", wraplength=420, justify=LEFT).pack(anchor=W, pady=(8, 4))
        ttk.Label(container, text="Use the Remonitor button for this row to restart only this path.", wraplength=420, justify=LEFT).pack(anchor=W)

        actions = ttk.Frame(container)
        actions.pack(fill=X, pady=(12, 0))
        ttk.Button(actions, text="Close", command=popup.destroy).pack(side=RIGHT)

        popup.lift()
        popup.attributes("-topmost", True)
        popup.after(500, lambda: popup.attributes("-topmost", False))

    def _set_directory_state(self, directory: str, state: str):
        item = self.directory_status_items.get(directory)
        if not item:
            return
        values = list(self.status_tree.item(item, "values"))
        if len(values) != 3:
            values = [directory, "00:00:00", state]
        else:
            values[2] = state
        self.status_tree.item(item, values=values)

    def _set_directory_remaining(self, directory: str, remaining_seconds: int):
        item = self.directory_status_items.get(directory)
        if not item:
            return
        values = list(self.status_tree.item(item, "values"))
        if len(values) != 3:
            values = [directory, self.format_seconds(remaining_seconds), "Monitoring"]
        else:
            values[1] = self.format_seconds(remaining_seconds)
        self.status_tree.item(item, values=values)

    def on_close(self):
        try:
            self.save_current_config()
        except Exception as exc:
            self.append_log(f"Config save error during close: {exc}")

        try:
            self.stop_event.set()
            if self.worker is not None:
                self.worker.join(timeout=4)
                self.worker = None
        except Exception:
            pass

        try:
            self.audio_player.shutdown()
        except Exception:
            pass

        self.destroy()


def main():
    app = NotifyApp()
    app.mainloop()


if __name__ == "__main__":
    main()
