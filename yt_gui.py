import os
import queue
import re
import subprocess
import threading
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from urllib.parse import unquote, urlparse

import customtkinter as ctk
from tkinter import filedialog, messagebox

import requests

import yt3

CURRENT_VERSION = "1.0.8"
GITHUB_RELEASES_LATEST_URL = (
    "https://api.github.com/repos/trjn-dev/YoutubeDownloader/releases/latest"
)

def resource_path(relative_path):
    """Get absolute path to resource for dev or PyInstaller builds."""
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(base_path, relative_path)
    if os.path.isfile(p):
        return p
    if getattr(sys, "frozen", False):
        alt = os.path.join(os.path.dirname(sys.executable), relative_path)
        if os.path.isfile(alt):
            return alt
    return p


FFMPEG_EXE_PATH = resource_path("ffmpeg.exe")


def get_settings_file_path():
    """
    Return a stable per-user settings file path.
    Uses %APPDATA% on Windows so settings persist for PyInstaller onefile apps.
    """
    appdata_dir = os.getenv("APPDATA")
    if appdata_dir:
        app_dir = os.path.join(appdata_dir, "YtDownloader")
    else:
        # Fallback for environments without APPDATA.
        app_dir = os.path.join(os.path.expanduser("~"), ".yt_downloader")
    os.makedirs(app_dir, exist_ok=True)
    return os.path.join(app_dir, "yt_gui_settings.json")


SETTINGS_FILE = get_settings_file_path()


def _ps_escape_shortcut_str(s: str) -> str:
    """Escape for PowerShell single-quoted strings."""
    return (s or "").replace("'", "''")


def ensure_youtube_downloader_start_menu_shortcut(target_exe=None) -> None:
    """Create/overwrite Start Menu shortcut 'Youtube Downloader' (Windows only)."""
    if sys.platform != "win32":
        return
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return
    programs = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs")
    try:
        os.makedirs(programs, exist_ok=True)
    except OSError:
        return
    lnk_path = os.path.join(programs, "Youtube Downloader.lnk")

    if target_exe:
        target = target_exe
        arguments = ""
        workdir = os.path.dirname(target_exe)
        icon_location = f"{target_exe},0"
    elif getattr(sys, "frozen", False):
        target = sys.executable
        arguments = ""
        workdir = os.path.dirname(sys.executable)
        icon_location = f"{target},0"
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(here, "yt_gui.py")
        exe_dir = os.path.dirname(sys.executable)
        pythonw = os.path.join(exe_dir, "pythonw.exe")
        target = pythonw if os.path.isfile(pythonw) else sys.executable
        arguments = script
        workdir = here
        ico = os.path.join(here, "app_icon.ico")
        icon_location = f"{ico},0" if os.path.isfile(ico) else f"{target},0"

    # Ensure updates override the old shortcut (same filename / path).
    try:
        if os.path.isfile(lnk_path):
            os.remove(lnk_path)
    except Exception:
        pass

    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{_ps_escape_shortcut_str(os.path.normpath(lnk_path))}'); "
        f"$s.TargetPath = '{_ps_escape_shortcut_str(os.path.normpath(target))}'; "
        f"$s.Arguments = '{_ps_escape_shortcut_str(arguments)}'; "
        f"$s.WorkingDirectory = '{_ps_escape_shortcut_str(os.path.normpath(workdir))}'; "
        f"$s.IconLocation = '{_ps_escape_shortcut_str(icon_location)}'; "
        "$s.Save()"
    )
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["powershell", "-NoProfile", "-Sta", "-Command", ps],
            capture_output=True,
            timeout=45,
            creationflags=creationflags,
        )
    except Exception:
        pass


def _windows_set_app_user_model_id() -> None:
    """Needed so Windows taskbar uses this app's icon instead of a generic Python/Tk icon."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "trjn.YoutubeDownloader.GUI.1.0"
        )
    except Exception:
        pass


def _win32_set_icons_from_ico(root: ctk.CTk, ico_path: str) -> None:
    """Set title-bar / taskbar icons via Win32 (reliable with CustomTkinter nested HWNDs)."""
    try:
        import ctypes

        ico_path = os.path.abspath(os.path.normpath(ico_path))
        if not os.path.isfile(ico_path):
            return

        user32 = ctypes.windll.user32
        root.update_idletasks()
        kid = int(root.winfo_id())
        GA_ROOT = 2
        hwnd = int(user32.GetAncestor(kid, GA_ROOT) or 0)
        if not hwnd:
            hwnd = int(user32.GetParent(kid) or kid)

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        LR_DEFAULTSIZE = 0x0040
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1

        h_big = user32.LoadImageW(
            None, ico_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
        )
        h_small = user32.LoadImageW(None, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if not h_big:
            h_big = user32.LoadImageW(None, ico_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
        if h_big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, h_big)
        if h_small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, h_small)
    except Exception:
        pass


def _apply_window_icon_to(root: ctk.CTk) -> None:
    """
    Black tile + white arrow from app_icon.ico. Delay helps CustomTkinter; Win32 also sets HWND icons.
    """
    def apply_icons() -> None:
        ico_path = os.path.abspath(os.path.normpath(resource_path("app_icon.ico")))
        if not os.path.isfile(ico_path):
            return

        if sys.platform == "win32":
            try:
                root.iconbitmap(ico_path)
            except Exception:
                pass
            _win32_set_icons_from_ico(root, ico_path)
            return

        try:
            from PIL import Image, ImageTk

            png_path = os.path.abspath(os.path.normpath(resource_path("app_icon.png")))
            src = png_path if os.path.isfile(png_path) else ico_path
            im = Image.open(src)
            if im.mode != "RGBA":
                im = im.convert("RGBA")
            photos = []
            for size in (64, 32, 16):
                thumb = im.resize((size, size), Image.Resampling.LANCZOS)
                photos.append(ImageTk.PhotoImage(thumb))
            root._window_icon_photos = photos
            root.wm_iconphoto(True, *photos)
        except Exception:
            pass

    root.after(201, apply_icons)
    root.after(600, apply_icons)


def _version_tuple(s: str):
    """Parse a semver-like string (e.g. 'v1.1.0' or '1.0.0') into a comparable tuple."""
    s = (s or "").strip().lstrip("vV")
    nums = [int(x) for x in re.findall(r"\d+", s)]
    if not nums:
        return (0, 0, 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:4])


def _is_remote_version_newer(remote_tag: str, local_version: str) -> bool:
    return _version_tuple(remote_tag) > _version_tuple(local_version)


class _QueueWriter:
    """File-like stream that forwards text chunks into a queue."""
    def __init__(self, q: "queue.Queue[str]"):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(str(text))
        return len(text or "")

    def flush(self):
        return


class YtDownloaderApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("YouTube Downloader")
        self.geometry("720x420")
        self.minsize(650, 360)
        _apply_window_icon_to(self)

        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.download_in_progress = False
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.saved_download_dir = self._load_saved_download_dir()

        self._update_download_url = None
        self._update_check_started = False

        self.url_var = ctk.StringVar(value="")
        initial_dir = self.saved_download_dir or getattr(yt3, "DOWNLOAD_FOLDER", "").rstrip()
        self.dir_var = ctk.StringVar(value=initial_dir)

        # UI
        self._build_ui()
        self.default_status_color = self.status_label.cget("text_color")

        # Pre-flight checks
        self._run_startup_checks()
        self.after(75, self._drain_log_queue)
        self._start_update_check_once()

    def _build_ui(self):
        pad = 12

        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_columnconfigure(2, weight=0)

        title = ctk.CTkLabel(self, text="YouTube Downloader", font=ctk.CTkFont(size=18, weight="bold"))
        title.grid(row=0, column=0, columnspan=3, padx=pad, pady=(pad, 6), sticky="w")

        # Optional update banner (shown only when a newer GitHub release exists)
        self.update_frame = ctk.CTkFrame(self)
        self.update_frame.grid_columnconfigure(0, weight=1)
        self._update_label = ctk.CTkLabel(self.update_frame, text="", anchor="w")
        self._update_label.grid(row=0, column=0, padx=(10, 8), pady=8, sticky="ew")
        self._update_btn = ctk.CTkButton(self.update_frame, text="Update Now", width=120, command=self._on_update_now)
        self._update_btn.grid(row=0, column=1, padx=(0, 10), pady=8, sticky="e")

        # URL row
        url_label = ctk.CTkLabel(self, text="YouTube URL:")
        url_label.grid(row=2, column=0, padx=pad, pady=6, sticky="w")

        self.url_entry = ctk.CTkEntry(self, textvariable=self.url_var)
        self.url_entry.grid(row=2, column=1, padx=pad, pady=6, sticky="ew")

        # Download folder row
        dir_label = ctk.CTkLabel(self, text="Download folder:")
        dir_label.grid(row=3, column=0, padx=pad, pady=6, sticky="w")

        self.dir_entry = ctk.CTkEntry(self, textvariable=self.dir_var)
        self.dir_entry.grid(row=3, column=1, padx=pad, pady=6, sticky="ew")

        browse_btn = ctk.CTkButton(self, text="Browse...", width=120, command=self.on_browse)
        browse_btn.grid(row=3, column=2, padx=(0, pad), pady=6, sticky="e")

        # Buttons row
        buttons_frame = ctk.CTkFrame(self)
        buttons_frame.grid(row=4, column=0, columnspan=3, padx=pad, pady=(12, 8), sticky="ew")
        buttons_frame.grid_columnconfigure(0, weight=1)
        buttons_frame.grid_columnconfigure(1, weight=1)
        buttons_frame.grid_rowconfigure(1, weight=1)

        # Video Mode dropdown (default: Compatibility)
        self.video_mode_var = ctk.StringVar(value="Compatibility")
        video_mode_menu = ctk.CTkOptionMenu(
            buttons_frame,
            values=["Compatibility", "Quality"],
            variable=self.video_mode_var,
            command=lambda _sel: None,
        )
        video_mode_menu.grid(row=0, column=0, columnspan=2, padx=10, pady=(10, 6), sticky="ew")

        # Keep the original two download buttons (video + audio). The video mode is taken from the dropdown.
        self.video_btn = ctk.CTkButton(
            buttons_frame,
            text="Download Video (MP4)",
            command=lambda: self.on_download("video_compat" if self.video_mode_var.get() == "Compatibility" else "video_quality"),
        )
        self.video_btn.grid(row=1, column=0, padx=10, pady=(6, 10), sticky="ew")

        self.audio_btn = ctk.CTkButton(
            buttons_frame,
            text="Download Audio (MP3)",
            command=lambda: self.on_download("audio"),
        )
        self.audio_btn.grid(row=1, column=1, padx=10, pady=(6, 10), sticky="ew")

        # Status / log
        self.status_label = ctk.CTkLabel(self, text="Ready.", anchor="w")
        self.status_label.grid(row=5, column=0, columnspan=3, padx=pad, pady=(0, 8), sticky="ew")

        self.log_box = ctk.CTkTextbox(self, height=140, wrap="word")
        self.log_box.grid(row=6, column=0, columnspan=3, padx=pad, pady=(0, pad), sticky="nsew")
        self.grid_rowconfigure(6, weight=1)

    def _run_startup_checks(self):
        ffmpeg_exe_path = FFMPEG_EXE_PATH if os.path.isfile(FFMPEG_EXE_PATH) else None
        if ffmpeg_exe_path is None:
            # Without ffmpeg, audio conversion (and many video merges) may fail.
            self._set_status("FFmpeg missing. Add ffmpeg.exe near app/script.", error=True)
            self.audio_btn.configure(state="disabled")
            self.video_btn.configure(state="disabled")
            messagebox.showerror(
                "FFmpeg missing",
                "FFmpeg is required by yt-dlp post-processing (e.g., MP3 extraction/merge).\n\n"
                "Put `ffmpeg.exe` in the same folder as the script/app,\n"
                "then restart the app.",
            )
        else:
            self._set_status("Ready.")

    def _start_update_check_once(self):
        if self._update_check_started:
            return
        self._update_check_started = True
        threading.Thread(target=self._check_for_updates_worker, daemon=True).start()

    def _check_for_updates_worker(self):
        try:
            resp = requests.get(
                GITHUB_RELEASES_LATEST_URL,
                timeout=15,
                headers={"Accept": "application/vnd.github+json", "User-Agent": "YtDownloader-UpdateCheck"},
            )
            resp.raise_for_status()
            data = resp.json()
            tag_name = (data.get("tag_name") or "").strip()
            if not tag_name or not _is_remote_version_newer(tag_name, CURRENT_VERSION):
                return

            exe_url = None
            for asset in data.get("assets") or []:
                name = (asset.get("name") or "").lower()
                if name.endswith(".exe"):
                    exe_url = asset.get("browser_download_url")
                    if exe_url:
                        break
            if not exe_url:
                for asset in data.get("assets") or []:
                    u = asset.get("browser_download_url") or ""
                    if u.lower().endswith(".exe"):
                        exe_url = u
                        break
            if not exe_url:
                return

            self.after(0, lambda: self._show_update_banner(tag_name, exe_url))
        except Exception:
            pass

    def _show_update_banner(self, tag_name: str, exe_url: str):
        self._update_download_url = exe_url
        self._update_label.configure(
            text=f"A newer version is available: {tag_name} (installed: {CURRENT_VERSION})"
        )
        pad = 12
        self.update_frame.grid(row=1, column=0, columnspan=3, padx=pad, pady=(0, 8), sticky="ew")

    def _on_update_now(self):
        url = self._update_download_url
        if not url:
            return
        self._update_btn.configure(state="disabled")
        threading.Thread(target=self._update_download_worker, args=(url,), daemon=True).start()

    def _update_download_worker(self, url: str):
        try:
            path = self._download_release_exe(url)
            self.after(0, lambda: self._on_update_download_finished(path, None))
        except Exception as e:
            self.after(0, lambda: self._on_update_download_finished(None, e))

    def _on_update_download_finished(self, path, err):
        self._update_btn.configure(state="normal")
        if err is not None:
            messagebox.showerror("Update download failed", str(err))
            return

        # Apply update: replace the currently-running exe in-place (same filename),
        # and recreate the Start Menu shortcut to ensure it points at the new binary.
        try:
            if getattr(sys, "frozen", False) and os.path.isfile(sys.executable):
                self._schedule_update_apply(downloaded_exe_path=path)
                messagebox.showinfo("Restarting", "The update has been downloaded. The application will now restart to apply changes.")
                sys.exit(0)
            else:
                messagebox.showinfo("Update downloaded", f"Installer saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Update failed", str(e))

    def _schedule_update_apply(self, downloaded_exe_path: str) -> None:
        """Replace the currently running exe with `downloaded_exe_path` (same filename) and refresh the Start Menu shortcut."""
        target_exe_path = sys.executable
        if not target_exe_path or not os.path.isfile(target_exe_path):
            raise FileNotFoundError("Current executable not found; cannot apply update in-place.")
        if not downloaded_exe_path or not os.path.isfile(downloaded_exe_path):
            raise FileNotFoundError("Downloaded update file not found.")

        process_name = os.path.splitext(os.path.basename(target_exe_path))[0]

        # Start Menu shortcut path (same name as before) so it overrides the old one.
        appdata = os.environ.get("APPDATA") or ""
        shortcut_path = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Youtube Downloader.lnk") if appdata else ""

        helper_dir = os.path.dirname(downloaded_exe_path) or os.getcwd()
        helper_ps1 = os.path.join(helper_dir, "yt_update_apply.ps1")

        # External helper is required because the current exe can't be replaced while this process is running.
        # The helper waits for the process to stop, then replaces the file and starts the new exe.
        ps1 = f"""param(
  [string]$TempExe,
  [string]$TargetExe,
  [string]$ShortcutPath,
  [string]$ProcessName
)
$ErrorActionPreference = 'Stop'

$timeoutSeconds = 180
$sw = [Diagnostics.Stopwatch]::StartNew()
while ($sw.Elapsed.TotalSeconds -lt $timeoutSeconds) {{
  $p = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue
  if (-not $p) {{ break }}
  Start-Sleep -Milliseconds 500
}}

try {{
  Move-Item -Force $TempExe $TargetExe
}} catch {{
  Copy-Item -Force $TempExe $TargetExe
  Remove-Item -Force $TempExe
}}

  if ($ShortcutPath -and (Test-Path (Split-Path $ShortcutPath))) {{
    try {{ if (Test-Path $ShortcutPath) {{ Remove-Item -Force $ShortcutPath }} }} catch {{ }}
    $ws = New-Object -ComObject WScript.Shell
    $s = $ws.CreateShortcut($ShortcutPath)
    $s.TargetPath = $TargetExe
    $s.Arguments = ''
    $s.WorkingDirectory = (Split-Path $TargetExe)
    $s.IconLocation = (\"$TargetExe,0\")
    $s.Save()
  }}

  # Clear PyInstaller environment variables to prevent "Failed to load Python DLL" error
  foreach ($var in @('_MEIPASS', '_MEIPASS2', 'PYTHONPATH', 'PYTHONHOME', '_PYI_PROGNAME')) {{
    [Environment]::SetEnvironmentVariable($var, $null, 'Process')
  }}

  # Start the new process with the proper working directory
  Start-Process -FilePath $TargetExe -WorkingDirectory (Split-Path $TargetExe)
"""

        with open(helper_ps1, "w", encoding="utf-8") as f:
            f.write(ps1)

        # Environment cleanup for PyInstaller:
        # If we are running in a frozen (PyInstaller) environment, we should clear
        # certain environment variables so that the child process (the new exe)
        # doesn't try to reuse the old process's temporary extraction directory.
        safe_env = os.environ.copy()
        # Common PyInstaller internal variables:
        for k in list(safe_env.keys()):
            if k.upper() in ["_MEIPASS", "_MEIPASS2", "PYTHONPATH", "PYTHONHOME", "_PYI_PROGNAME"]:
                del safe_env[k]

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-Sta",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                helper_ps1,
                downloaded_exe_path,
                target_exe_path,
                shortcut_path,
                process_name,
            ],
            env=safe_env,
            creationflags=creationflags,
        )

    @staticmethod
    def _download_release_exe(url: str) -> str:
        parsed = urlparse(url)
        name = os.path.basename(unquote(parsed.path))
        if not name or not name.lower().endswith(".exe"):
            name = "YoutubeDownloader_update.exe"

        # If we're running a frozen exe, download next to it as a temp file,
        # then an external helper will replace the current exe after this app exits.
        if getattr(sys, "frozen", False) and os.path.isfile(sys.executable):
            target_exe = sys.executable
            target_dir = os.path.dirname(target_exe)
            target_name = os.path.basename(target_exe)
            dest = os.path.join(target_dir, f"{target_name}.update_download")
        else:
            downloads = os.path.join(os.path.expanduser("~"), "Downloads")
            os.makedirs(downloads, exist_ok=True)
            dest = os.path.join(downloads, name)

        with requests.get(url, stream=True, timeout=120, headers={"User-Agent": "YtDownloader-UpdateDownload"}) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        f.write(chunk)
        return dest

    def on_browse(self):
        chosen = filedialog.askdirectory(title="Select download directory")
        if chosen:
            self.dir_var.set(chosen)
            self._save_download_dir(chosen)

    def _set_status(self, text: str, error: bool = False):
        self.status_label.configure(text=text)
        self.status_label.configure(text_color=("red" if error else self.default_status_color))

    @staticmethod
    def _clean_url(url: str) -> str:
        # Mirror your existing CLI cleanup for noisy tracking query params.
        return url.split("?si=")[0].split("&si=")[0].strip()

    def on_download(self, media_type: str):
        if self.download_in_progress:
            return

        raw_url = (self.url_var.get() or "").strip()
        if not raw_url:
            messagebox.showwarning("Missing URL", "Please paste a YouTube URL first.")
            return

        download_dir = (self.dir_var.get() or "").strip()
        if not download_dir:
            messagebox.showwarning("Missing folder", "Please select a download folder.")
            return

        os.makedirs(download_dir, exist_ok=True)
        yt3.DOWNLOAD_FOLDER = download_dir
        self._save_download_dir(download_dir)

        clean_url = self._clean_url(raw_url)
        self._append_log(f"Starting {media_type.upper()} download:\n{clean_url}\n")

        self.download_in_progress = True
        self._set_status("Downloading... (please wait)")
        self.audio_btn.configure(state="disabled")
        self.video_btn.configure(state="disabled")

        t = threading.Thread(target=self._download_worker, args=(clean_url, media_type), daemon=True)
        t.start()

    def _download_worker(self, url: str, media_type: str):
        stream = _QueueWriter(self.log_queue)
        try:
            # Stream yt-dlp output live to the app log.
            with redirect_stdout(stream), redirect_stderr(stream):
                yt3.download_media(url, media_type, FFMPEG_EXE_PATH)
            self.after(0, self._on_download_complete)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            self.after(0, lambda: self._on_download_error(msg))

    def _append_log(self, text: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _drain_log_queue(self):
        chunks = []
        while True:
            try:
                chunks.append(self.log_queue.get_nowait())
            except queue.Empty:
                break

        if chunks:
            text = "".join(chunks).replace("\r", "\n")
            if text:
                self._append_log(text)

        self.after(75, self._drain_log_queue)

    def _on_download_complete(self):
        self.download_in_progress = False
        self.audio_btn.configure(state="normal")
        self.video_btn.configure(state="normal")
        self._set_status("Download finished.")
        self._append_log("\nDone.\n")

    def _on_download_error(self, msg: str):
        self.download_in_progress = False
        self.audio_btn.configure(state="normal")
        self.video_btn.configure(state="normal")
        self._set_status("Download failed. See log.", error=True)
        self._append_log(f"\n--- Error ---\n{msg}\n")

    def _load_saved_download_dir(self):
        try:
            if not os.path.isfile(SETTINGS_FILE):
                return ""
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved_dir = (data.get("download_dir") or "").strip()
            return saved_dir
        except Exception:
            return ""

    def _save_download_dir(self, path: str):
        try:
            data = {"download_dir": path}
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=True, indent=2)
        except Exception:
            # Non-fatal: app should still work if settings cannot be written.
            pass


def main():
    ensure_youtube_downloader_start_menu_shortcut()
    _windows_set_app_user_model_id()
    # Initialize textbox state on start.
    app = YtDownloaderApp()
    app.log_box.configure(state="disabled")
    app.mainloop()


if __name__ == "__main__":
    main()

