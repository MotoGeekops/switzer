"""Double-click the desktop shortcut to control Switzer without a terminal."""

import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
from tkinter.scrolledtext import ScrolledText

from bot_runtime import InstanceLock, STATUS_PREFIX

ROOT = Path(__file__).resolve().parent
BG = "#10131d"
PANEL = "#1b2030"
TEXT = "#eef0fa"
MUTED = "#a9b2c9"


class BotProcess:
    """Own only the subprocess launched by this controller."""

    def __init__(self, command=None, cwd=ROOT):
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe":
            executable = executable.with_name("python.exe")
        self.command = command or [str(executable), "-u", str(ROOT / "bot.py"), "--controlled"]
        self.cwd = cwd
        self.process = None
        self.lines = queue.Queue(maxsize=4000)
        self.reader = None
        self.stopping = False
        self.deadline = None
        self.forced = False

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        if self.process is not None:
            return False
        environment = os.environ.copy()
        environment.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self.process = subprocess.Popen(
            self.command, cwd=self.cwd, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.stopping = self.forced = False
        self.deadline = None
        output = self.process.stdout

        def read_output():
            try:
                for line in output:
                    self.lines.put(line)
            finally:
                output.close()

        self.reader = threading.Thread(target=read_output, daemon=True)
        self.reader.start()
        return True

    def stop(self):
        if not self.running or self.stopping:
            return
        self.stopping = True
        self.deadline = time.monotonic() + 12
        try:
            self.process.stdin.write("stop\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def check_timeout(self):
        if self.running and self.deadline and time.monotonic() >= self.deadline:
            self.process.kill()
            self.forced = True
            self.deadline = None

    def collect_exit(self):
        # Drain the reader before starting another process, preserving log order.
        if self.process is None or self.running or self.reader.is_alive() or not self.lines.empty():
            return None
        code = self.process.wait()
        self.process.stdin.close()
        self.process = None
        self.deadline = None
        return code


def set_windows_keep_awake(enabled: bool):
    """Prevent Windows from sleeping while the local 24/7 bot is running."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


class BotController:
    def __init__(self, root):
        self.root = root
        self.bot = BotProcess()
        self.restart_pending = False
        self.closing = False
        self.started_at = None
        self.auto_restart = tk.BooleanVar(master=root, value=False)
        self.keep_awake = tk.BooleanVar(master=root, value=True)
        root.title("Switzer • 24/7 Bot Control")
        root.geometry("820x640")
        root.minsize(640, 520)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)

        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=28, pady=(26, 18))
        tk.Label(header, text="SWITZER • 24/7 AI HELPER", font=("Segoe UI", 22, "bold"), fg=TEXT, bg=BG).pack(anchor="w")
        tk.Label(header, text="Grok-style AI server helper & 24/7 autonomous task runner on your PC.", font=("Segoe UI", 11), fg=MUTED, bg=BG).pack(anchor="w", pady=(2, 0))

        card = tk.Frame(root, bg=PANEL, padx=20, pady=18)
        card.pack(fill="x", padx=28)
        self.status = tk.Label(card, text="●  Stopped", font=("Segoe UI", 17, "bold"), fg=MUTED, bg=PANEL)
        self.status.pack(anchor="w")
        self.detail = tk.Label(card, text="Press Start Bot to bring Switzer online.", font=("Segoe UI", 10), fg=MUTED, bg=PANEL)
        self.detail.pack(anchor="w", pady=(6, 0))

        controls = tk.Frame(root, bg=BG)
        controls.pack(fill="x", padx=28, pady=(18, 10))
        self.start_button = self.button(controls, "▶  Start Bot", self.start, "#5865f2")
        self.stop_button = self.button(controls, "■  Stop Bot", self.stop, "#bc4055")
        self.restart_button = self.button(controls, "↻  Restart", self.restart, "#353d57")
        self.stop_button.configure(state="disabled")
        self.restart_button.configure(state="disabled")

        options = tk.Frame(root, bg=BG)
        options.pack(fill="x", padx=28, pady=(0, 12))
        tk.Checkbutton(
            options,
            text="24/7 Auto-Restart on crash",
            variable=self.auto_restart,
            fg=MUTED,
            bg=BG,
            selectcolor=PANEL,
            activebackground=BG,
            activeforeground=TEXT,
            font=("Segoe UI", 10),
        ).pack(side="left", padx=(0, 18))
        tk.Checkbutton(
            options,
            text="Keep PC awake while bot is running",
            variable=self.keep_awake,
            command=self._sync_awake_state,
            fg=MUTED,
            bg=BG,
            selectcolor=PANEL,
            activebackground=BG,
            activeforeground=TEXT,
            font=("Segoe UI", 10),
        ).pack(side="left")

        log_header = tk.Frame(root, bg=BG)
        log_header.pack(fill="x", padx=28, pady=(0, 8))
        tk.Label(log_header, text="LIVE LOG", font=("Segoe UI", 10, "bold"), fg=MUTED, bg=BG).pack(side="left")
        tk.Button(log_header, text="Clear log", command=self.clear_log, fg=MUTED, bg=BG, activebackground=PANEL, activeforeground=TEXT, relief="flat", cursor="hand2").pack(side="right")
        tk.Label(root, text="Keep this app open while the bot runs 24/7 on your PC. Closing it stops the bot.", font=("Segoe UI", 9), bg=BG, fg=MUTED).pack(side="bottom", pady=(0, 16))
        self.log = ScrolledText(root, bg="#0b0e16", fg="#c7d0e6", insertbackground=TEXT, font=("Consolas", 10), relief="flat", padx=12, pady=12, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, padx=28, pady=(0, 12))
        self.write("Ready. Your existing .env settings will be used.\n")
        root.after(100, self.poll)

    def _sync_awake_state(self):
        set_windows_keep_awake(self.bot.running and self.keep_awake.get())

    @staticmethod
    def button(parent, text, command, color):
        button = tk.Button(parent, text=text, command=command, bg=color, fg="white", activebackground=color, activeforeground="white", disabledforeground="#9298ae", font=("Segoe UI", 11, "bold"), relief="flat", bd=0, padx=18, pady=12, cursor="hand2")
        button.pack(side="left", padx=(0, 10))
        return button

    def set_status(self, text, color=MUTED):
        self.status.configure(text="●  " + text, fg=color)

    def write(self, text):
        at_bottom = self.log.yview()[1] >= 0.99
        self.log.configure(state="normal")
        self.log.insert("end", text)
        count = int(self.log.index("end-1c").split(".")[0])
        if count > 1200:
            self.log.delete("1.0", f"{count - 1200}.0")
        self.log.configure(state="disabled")
        if at_bottom:
            self.log.see("end")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def start(self):
        if self.closing or self.bot.process is not None:
            return
        try:
            self.bot.start()
        except OSError as error:
            self.write(f"Could not start the bot: {error}\n")
            self.set_status("Could not start", "#ff8995")
            return
        self.started_at = time.monotonic()
        self._sync_awake_state()
        self.set_status("Starting...", "#f3cb77")
        self.write("\nStarting Switzer...\n")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.restart_button.configure(state="normal")

    def stop(self):
        self.restart_pending = False
        self.request_stop()

    def request_stop(self):
        self.bot.stop()
        self.set_status("Stopping...", "#f3cb77")
        self.stop_button.configure(state="disabled")
        self.restart_button.configure(state="disabled")

    def restart(self):
        self.restart_pending = True
        self.request_stop()

    def close(self):
        self.closing = True
        self.restart_pending = False
        set_windows_keep_awake(False)
        self.start_button.configure(state="disabled")
        if self.bot.process is None:
            self.root.destroy()
        else:
            self.request_stop()

    def poll(self):
        for _ in range(300):
            try:
                line = self.bot.lines.get_nowait()
            except queue.Empty:
                break
            if line.startswith(STATUS_PREFIX):
                if not self.bot.stopping:
                    status = line[len(STATUS_PREFIX):].strip()
                    self.set_status(status, "#72e6ab" if status == "Online" else "#f3cb77")
            else:
                self.write(line)
        self.bot.check_timeout()
        code = self.bot.collect_exit()
        if code is not None:
            expected = self.bot.stopping
            if self.bot.forced:
                self.write("Shutdown took too long; the bot process was stopped.\n")
            self.write(f"Bot stopped (exit code {code}).\n")
            self.started_at = None
            set_windows_keep_awake(False)
            self.set_status("Stopped" if expected or code == 0 else "Failed to start / stopped unexpectedly", MUTED if expected or code == 0 else "#ff8995")
            self.detail.configure(text="Press Start Bot to bring Switzer online." if expected or code == 0 else "Check the log below for the error, then try Start Bot again.")
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.restart_button.configure(state="disabled")
            if self.closing:
                self.root.destroy()
                return
            if self.restart_pending or (not expected and code != 0 and self.auto_restart.get()):
                self.restart_pending = False
                if not expected and code != 0:
                    self.write("24/7 Auto-Restart enabled: restarting Switzer...\n")
                self.start()
        if self.started_at is not None and self.bot.process is not None:
            elapsed = int(time.monotonic() - self.started_at)
            self.detail.configure(text=f"Session: {elapsed // 3600:02}:{elapsed // 60 % 60:02}:{elapsed % 60:02}  •  PID {self.bot.process.pid}")
        self.root.after(100, self.poll)


def main():
    root = tk.Tk()
    root.withdraw()
    lock = InstanceLock(ROOT / ".controller.lock")
    try:
        if not lock.acquire():
            messagebox.showinfo("Switzer", "Bot Control is already open. Switch to its window on the taskbar.", parent=root)
            root.destroy()
            return
        app = BotController(root)
        app.auto_restart.set(True)
        root.deiconify()
        root.mainloop()
    finally:
        set_windows_keep_awake(False)
        lock.release()


if __name__ == "__main__":
    main()

