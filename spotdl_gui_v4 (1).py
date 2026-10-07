from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import unicodedata
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from mutagen import File
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, TIT2, TPOS, TRCK
from PIL import Image, ImageTk


APP_TITLE = "SpotDL Album Downloader"
APP_DIR = Path.home() / ".spotdl_album_downloader"
SETTINGS_FILE = APP_DIR / "settings_v4.json"
AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav"}
INSTANCE_MUTEX_NAME = "Local\\SpotDLAlbumDownloaderV4"
_INSTANCE_MUTEX_HANDLE: int | None = None


def resource_path(relative_path: str) -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / relative_path
    return Path(__file__).resolve().parent / relative_path


def restore_window(hwnd: int) -> bool:
    if os.name != "nt" or not hwnd:
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def activate_existing_instance() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        hwnd = ctypes.windll.user32.FindWindowW(None, APP_TITLE)
        return restore_window(hwnd)
    except Exception:
        return False


def ensure_single_instance() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        global _INSTANCE_MUTEX_HANDLE
        kernel32 = ctypes.windll.kernel32
        _INSTANCE_MUTEX_HANDLE = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            activate_existing_instance()
            return False
    except Exception:
        return True
    return True


def spotdl_command() -> list[str]:
    if getattr(sys, "frozen", False):
        bundled_cli = Path(sys.executable).resolve().parent / "spotdl-cli.exe"
        if bundled_cli.exists():
            return [str(bundled_cli)]
    wrapper = resource_path("spotdl_cli_entry.py")
    if wrapper.exists():
        return [sys.executable, str(wrapper)]
    return [sys.executable, "-m", "spotdl"]


def spotdl_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    return env


def yt_dlp_command() -> list[str]:
    wrapper = resource_path("yt_dlp_no_window.py")
    if wrapper.exists():
        return [sys.executable, str(wrapper)]
    return [sys.executable, "-m", "yt_dlp"]


def clean_spotify_url(url: str) -> str:
    match = re.search(r"https?://open\.spotify\.com/(album|playlist|track)/[^?\s#]+", url.strip())
    return match.group(0) if match else url.strip()


def audio_files_in(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def normalize_match_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9\u0370-\u03ff]+", "", value)


def match_words(value: str) -> set[str]:
    value = unicodedata.normalize("NFKD", value.lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    words = re.findall(r"[a-z0-9\u0370-\u03ff]+", value)
    stop_words = {"feat", "ft", "official", "audio", "video", "lyrics", "remix", "explicit"}
    return {word for word in words if len(word) > 1 and word not in stop_words}


def match_variants(value: str) -> list[str]:
    without_features = re.sub(r"\s*[\(\[]?\b(?:feat|ft|featuring)\b.*$", "", value, flags=re.IGNORECASE).strip()
    return [item for item in dict.fromkeys([value, without_features]) if item]


def match_score(expected: str, candidate: str) -> float:
    candidate_words = match_words(candidate)
    if not candidate_words:
        return 0.0
    best = 0.0
    for expected_variant in match_variants(expected):
        expected_words = match_words(expected_variant)
        if not expected_words:
            continue
        overlap = len(expected_words & candidate_words) / len(expected_words)
        extra_penalty = max(0, len(candidate_words - expected_words) - 5) * 0.03
        best = max(best, max(0.0, min(1.0, overlap - extra_penalty)))
    return best


def read_audio_title(audio_path: Path) -> str:
    audio = File(audio_path, easy=True)
    if audio is None or not getattr(audio, "tags", None):
        return audio_path.stem
    values = audio.tags.get("title")
    return str(values[0]) if values else audio_path.stem


def set_track_disc_tags(audio_path: Path, track_number: int, track_count: int, disc_number: int, disc_count: int) -> None:
    track_text = f"{track_number}/{track_count}" if track_count else str(track_number)
    disc_text = f"{disc_number}/{disc_count}" if disc_count else str(disc_number)
    try:
        audio = EasyID3(audio_path)
        audio["tracknumber"] = track_text
        audio["discnumber"] = disc_text
        audio.save()
    except Exception:
        tags = ID3(audio_path)
        tags["TRCK"] = TRCK(encoding=3, text=track_text)
        tags["TPOS"] = TPOS(encoding=3, text=disc_text)
        tags.save()


def set_title_tag_to_filename(audio_path: Path) -> None:
    title_text = audio_path.stem
    try:
        audio = File(audio_path, easy=True)
        if audio is None:
            return
        if audio.tags is None:
            audio.add_tags()
        audio["title"] = title_text
        audio.save()
    except Exception:
        if audio_path.suffix.lower() != ".mp3":
            return
        tags = ID3(audio_path)
        tags["TIT2"] = TIT2(encoding=3, text=title_text)
        tags.save()


def set_titles_to_filenames(folder: Path) -> None:
    for audio_path in audio_files_in(folder):
        set_title_tag_to_filename(audio_path)


def concise_spotdl_log(line: str) -> str | None:
    text = line.strip()
    if not text:
        return None

    found = re.search(r"Found\s+(\d+)\s+songs?\s+in\s+(.+)", text)
    if found:
        return f"Βρέθηκαν {found.group(1)} τραγούδια: {found.group(2)}\n"

    downloaded = re.search(r'Downloaded\s+"(.+?)"', text)
    if downloaded:
        return f"Κατέβηκε: {downloaded.group(1)}\n"

    processing = re.search(r"Processing query:\s*(.+)", text)
    if processing:
        query = processing.group(1)
        if "open.spotify.com/album/" in query:
            return "Ανάγνωση Spotify album...\n"
        if "open.spotify.com/track/" in query or "youtube.com/watch" in query:
            return None
        return f"Επεξεργασία: {query}\n"

    if "LookupError:" in text and "No results found for song:" in text:
        missing = text.split("No results found for song:", 1)[1].strip()
        if missing.count("(") > missing.count(")") or missing.endswith(","):
            return "Δεν βρέθηκε ένα κομμάτι. Θα γίνει δεύτερη προσπάθεια.\n"
        return "Δεν βρέθηκε: " + missing + "\n"

    if "AudioProviderError:" in text:
        return None
    if text.startswith("Saved errors to "):
        return "Αποθηκεύτηκαν οι ελλείψεις στο Missing Songs.txt.\n"
    if "Error occurred while reinitializing song" in text:
        return "Προσωρινό σφάλμα σύνδεσης στο Spotify, θα γίνει συνέχεια όπου είναι δυνατό.\n"
    return None


def write_bytes_forced(path: Path, data: bytes) -> None:
    existing = next((p for p in path.parent.iterdir() if p.name.lower() == path.name.lower()), path)
    if existing.exists() and os.name == "nt":
        existing.chmod(0o666)
        try:
            import ctypes

            ctypes.windll.kernel32.SetFileAttributesW(str(existing), 0x80)
        except Exception:
            pass
    existing.write_bytes(data)


def extract_cover(album_dir: Path, make_folder_jpg: bool = True) -> Path | None:
    for audio_path in audio_files_in(album_dir):
        audio = File(audio_path)
        if audio is None:
            continue

        image_data = None
        tags = getattr(audio, "tags", None)
        if tags:
            for key in tags.keys():
                if str(key).startswith("APIC"):
                    image_data = tags[key].data
                    break
        pictures = getattr(audio, "pictures", None)
        if image_data is None and pictures:
            image_data = pictures[0].data
        if image_data is None and tags and "covr" in tags and tags["covr"]:
            image_data = bytes(tags["covr"][0])

        if image_data:
            cover_path = album_dir / "cover.jpg"
            write_bytes_forced(cover_path, image_data)
            if make_folder_jpg:
                write_bytes_forced(album_dir / "folder.jpg", image_data)
            return cover_path
    return None


def read_easy_tags(audio_path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    audio = File(audio_path, easy=True)
    if audio is None or not getattr(audio, "tags", None):
        return result

    mapping = {
        "artist": ("albumartist", "artist"),
        "album": ("album",),
        "date": ("date", "year"),
        "genre": ("genre",),
    }
    for target, keys in mapping.items():
        for key in keys:
            value = audio.tags.get(key)
            if value:
                result[target] = str(value[0])
                break
    return result


def create_album_info(album_dir: Path, spotify_url: str) -> Path | None:
    audio_files = audio_files_in(album_dir)
    if not audio_files:
        return None

    metadata = read_easy_tags(audio_files[0])
    info_path = album_dir / "Album Info.txt"
    lines = [
        f"Καλλιτέχνης: {metadata.get('artist', 'Άγνωστο')}",
        f"Άλμπουμ: {metadata.get('album', 'Άγνωστο')}",
        f"Χρονολογία: {metadata.get('date', 'Άγνωστη')}",
        f"Είδος: {metadata.get('genre', 'Άγνωστο')}",
        f"Αρχεία ήχου: {len(audio_files)}",
        f"Spotify URL: {spotify_url}",
        "",
        "Περιεχόμενα:",
    ]
    lines.extend(f"- {p.relative_to(album_dir)}" for p in audio_files)
    info_path.write_text("\n".join(lines), encoding="utf-8-sig")
    return info_path


def newest_album_folder(root: Path) -> Path | None:
    folders = [p for p in root.iterdir() if p.is_dir() and audio_files_in(p)]
    if not folders:
        return None
    return max(folders, key=lambda p: max((f.stat().st_mtime for f in audio_files_in(p)), default=p.stat().st_mtime))


class SpotDLApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        icon_path = resource_path("spotdl_gui_icon.ico")
        if icon_path.exists():
            self.iconbitmap(str(icon_path))
        self._set_startup_geometry()
        self.configure(bg="#f5f6f8")

        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.cover_photo: ImageTk.PhotoImage | None = None
        self.last_album_dir: Path | None = None
        self.last_clipboard = ""
        self.log_buffer = ""
        self.progress_log_lines: list[str] = []
        self.track_rows: dict[str, dict[str, str]] = {}
        self.track_lookup: list[tuple[str, str]] = []

        self.url_var = tk.StringVar()
        self.root_var = tk.StringVar(value=str(Path.home() / "Music"))
        self.format_var = tk.StringVar(value="mp3")
        self.album_name_var = tk.StringVar(value="Θα δημιουργηθεί αυτόματα")
        self.status_var = tk.StringVar(value="Έτοιμο")
        self.cover_var = tk.BooleanVar(value=True)
        self.folder_jpg_var = tk.BooleanVar(value=True)
        self.lyrics_var = tk.BooleanVar(value=True)
        self.info_var = tk.BooleanVar(value=True)
        self.errors_var = tk.BooleanVar(value=True)
        self.auto_paste_var = tk.BooleanVar(value=True)

        self._configure_style()
        self._load_settings()
        self._build_ui()
        self.after(250, self._bring_to_front)
        self.after(120, self._drain_events)
        self.after(800, self._watch_clipboard)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_startup_geometry(self) -> None:
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        width = min(1180, max(980, screen_width - 160))
        height = min(920, max(760, screen_height - 180))
        x = max((screen_width - width) // 2, 0)
        y = max((screen_height - height) // 2, 0)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(min(940, width), min(720, height))

    def _bring_to_front(self) -> None:
        self.update_idletasks()
        try:
            self.state("normal")
        except tk.TclError:
            pass
        self.deiconify()
        restore_window(self.winfo_id())
        self.lift()
        self.focus_force()
        self.attributes("-topmost", True)
        self.after(150, lambda: restore_window(self.winfo_id()))
        self.after(700, self._release_topmost)

    def _release_topmost(self) -> None:
        try:
            self.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        green = "#1db954"
        style.configure(".", font=("Segoe UI", 10))
        style.configure("TFrame", background="#f5f6f8")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("TLabel", background="#f5f6f8", foreground="#161616")
        style.configure("Card.TLabel", background="#ffffff", foreground="#161616")
        style.configure("Title.TLabel", background="#f5f6f8", foreground="#111111", font=("Segoe UI", 22, "bold"))
        style.configure("Subtitle.TLabel", background="#f5f6f8", foreground="#60646c")
        style.configure("Section.TLabel", background="#ffffff", foreground="#111111", font=("Segoe UI", 12, "bold"))
        style.configure("Status.TLabel", background="#ffffff", foreground="#60646c")
        style.configure("TButton", padding=(12, 8))
        style.configure("Primary.TButton", background=green, foreground="#ffffff", padding=(16, 10), font=("Segoe UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", "#179c47"), ("disabled", "#a6d8b6")], foreground=[("disabled", "#ffffff")])
        style.configure("TEntry", fieldbackground="#ffffff", foreground="#111111", padding=6)
        style.configure("TCombobox", fieldbackground="#ffffff", foreground="#111111", padding=5)
        style.configure("TCheckbutton", background="#ffffff", foreground="#222222")
        style.map("TCheckbutton", background=[("active", "#ffffff")])
        style.configure("TLabelframe", background="#ffffff", foreground="#111111")
        style.configure("TLabelframe.Label", background="#ffffff", foreground="#111111", font=("Segoe UI", 10, "bold"))
        style.configure("Horizontal.TProgressbar", troughcolor="#e7e9ed", background=green)
        style.configure("Tracks.Treeview", font=("Segoe UI", 10), rowheight=28)
        style.configure("Tracks.Treeview.Heading", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=22)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="SpotDL Album Downloader", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="Κατέβασε και οργάνωσε άλμπουμ/κομμάτια με αυτόματο όνομα φακέλου, εξώφυλλο και metadata.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(3, 18))

        content = ttk.Panedwindow(outer, orient="horizontal")
        content.pack(fill="both", expand=True)

        left = ttk.Frame(content, style="Card.TFrame", padding=18)
        right = ttk.Frame(content, style="Card.TFrame", padding=18)
        content.add(left, weight=3)
        content.add(right, weight=1)

        left_split = ttk.Panedwindow(left, orient="vertical")
        left_split.pack(fill="both", expand=True)
        controls = ttk.Frame(left_split, style="Card.TFrame")
        tracks_frame = ttk.Frame(left_split, style="Card.TFrame")
        left_split.add(controls, weight=0)
        left_split.add(tracks_frame, weight=1)

        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Spotify URL", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=7)
        self.url_entry = ttk.Entry(controls, textvariable=self.url_var)
        self.url_entry.grid(row=0, column=1, sticky="ew", padx=(12, 8), pady=7)
        ttk.Button(controls, text="Επικόλληση", command=self._paste_url).grid(row=0, column=2, pady=7)

        ttk.Label(controls, text="Όνομα φακέλου", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=7)
        ttk.Entry(controls, textvariable=self.album_name_var, state="readonly").grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=7
        )

        ttk.Label(controls, text="Βασικός φάκελος", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=7)
        self.root_entry = ttk.Entry(controls, textvariable=self.root_var)
        self.root_entry.grid(row=2, column=1, sticky="ew", padx=(12, 8), pady=7)
        ttk.Button(controls, text="Αναζήτηση...", command=self._browse).grid(row=2, column=2, pady=7)

        ttk.Label(controls, text="Μορφή ήχου", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=7)
        ttk.Combobox(
            controls,
            textvariable=self.format_var,
            values=("mp3", "m4a", "opus", "flac", "ogg", "wav"),
            state="readonly",
            width=12,
        ).grid(row=3, column=1, sticky="w", padx=(12, 0), pady=7)

        options = ttk.LabelFrame(controls, text="Επιλογές", padding=12)
        options.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(15, 12))
        ttk.Checkbutton(options, text="cover.jpg", variable=self.cover_var).grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(options, text="folder.jpg", variable=self.folder_jpg_var).grid(row=0, column=1, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(options, text="Στίχοι .lrc", variable=self.lyrics_var).grid(row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(options, text="Album Info.txt", variable=self.info_var).grid(row=1, column=1, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(options, text="Missing Songs.txt", variable=self.errors_var).grid(row=2, column=0, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(options, text="Αυτόματη επικόλληση URL", variable=self.auto_paste_var).grid(row=2, column=1, sticky="w", padx=6, pady=4)

        actions = ttk.Frame(controls, style="Card.TFrame")
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        self.download_btn = ttk.Button(actions, text="ΛΗΨΗ", style="Primary.TButton", command=self._start)
        self.download_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Διακοπή", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(actions, text="Άνοιγμα φακέλου", command=self._open_album).pack(side="right")

        self.progress = ttk.Progressbar(controls, mode="indeterminate")
        self.progress.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 7))

        ttk.Label(controls, textvariable=self.status_var, style="Status.TLabel").grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(0, 10)
        )

        self.progress_log = tk.Text(
            controls,
            height=3,
            wrap="word",
            state="disabled",
            font=("Segoe UI", 10),
            bg="#ffffff",
            fg="#3f454c",
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=0,
            pady=0,
        )
        self.progress_log.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        self.progress_log.grid_remove()

        ttk.Label(controls, text="Κομμάτια", style="Section.TLabel").grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )
        tracks_frame.columnconfigure(0, weight=1)
        tracks_frame.rowconfigure(0, weight=1)

        self.tracks = ttk.Treeview(
            tracks_frame,
            columns=("number", "state", "title"),
            show="headings",
            height=6,
            style="Tracks.Treeview",
            selectmode="browse",
        )
        self.tracks.heading("number", text="#")
        self.tracks.heading("state", text="Κατάσταση")
        self.tracks.heading("title", text="Τίτλος")
        self.tracks.column("number", width=46, minwidth=42, anchor="center", stretch=False)
        self.tracks.column("state", width=120, minwidth=110, anchor="center", stretch=False)
        self.tracks.column("title", width=420, minwidth=220, anchor="w")
        self.tracks.grid(row=0, column=0, sticky="nsew")
        tracks_scroll = ttk.Scrollbar(tracks_frame, orient="vertical", command=self.tracks.yview)
        tracks_scroll.grid(row=0, column=1, sticky="ns")
        self.tracks.configure(yscrollcommand=tracks_scroll.set)
        self.tracks.bind("<MouseWheel>", self._scroll_tracks)

        ttk.Label(right, text="Εξώφυλλο", style="Section.TLabel").pack(anchor="w")
        self.cover_label = tk.Label(
            right,
            text="Το εξώφυλλο θα εμφανιστεί\nμετά τη λήψη.",
            bg="#f1f2f4",
            fg="#70757a",
            width=28,
            height=15,
            justify="center",
            relief="flat",
        )
        self.cover_label.pack(fill="both", pady=(12, 18))

        ttk.Label(right, text="Αυτόματη ονομασία", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            right,
            text="{album-artist} - {album}({year})\n\nΠαράδειγμα:\nPink Floyd - The Dark Side of the Moon(1973)",
            style="Card.TLabel",
            wraplength=240,
            justify="left",
        ).pack(anchor="w", pady=(8, 18))

        ttk.Label(right, text="Γρήγορες ενέργειες", style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Button(right, text="Επικόλληση από clipboard", command=self._paste_url).pack(fill="x", pady=4)
        ttk.Button(right, text="Άνοιγμα βασικού φακέλου", command=self._open_root).pack(fill="x", pady=4)
        ttk.Button(right, text="Καθαρισμός URL", command=lambda: self.url_var.set("")).pack(fill="x", pady=4)
        ttk.Button(right, text="Καθαρισμός καταγραφής", command=self._clear_log).pack(fill="x", pady=4)

        self._install_entry_bindings(self.url_entry)
        self._install_entry_bindings(self.root_entry)
        self._install_log_bindings()
        self.url_entry.focus_set()

    def _install_entry_bindings(self, widget: ttk.Entry) -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Αποκοπή", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="Αντιγραφή", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="Επικόλληση", command=lambda: widget.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="Επιλογή όλων", command=lambda: self._select_all(widget))

        def show_menu(event: tk.Event) -> None:
            widget.focus_set()
            menu.tk_popup(event.x_root, event.y_root)

        widget.bind("<Button-3>", show_menu)
        widget.bind("<Control-v>", lambda e, w=widget: self._paste_into(w))
        widget.bind("<Shift-Insert>", lambda e, w=widget: self._paste_into(w))
        widget.bind("<Control-a>", lambda e, w=widget: self._select_all(w))

    def _install_log_bindings(self) -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Αντιγραφή", command=self._copy_log_selection)
        menu.add_command(label="Αντιγραφή όλων", command=self._copy_all_logs)
        menu.add_command(label="Επιλογή όλων", command=self._select_all_logs)

        def show_menu(event: tk.Event) -> None:
            self.progress_log.focus_set()
            menu.tk_popup(event.x_root, event.y_root)

        self.progress_log.bind("<Button-3>", show_menu)
        self.progress_log.bind("<Control-c>", lambda _event: self._copy_log_selection())
        self.progress_log.bind("<Control-a>", lambda _event: self._select_all_logs())

    def _select_all(self, widget: ttk.Entry) -> str:
        widget.selection_range(0, "end")
        widget.icursor("end")
        return "break"

    def _select_all_logs(self) -> str:
        self.progress_log.configure(state="normal")
        self.progress_log.tag_add("sel", "1.0", "end")
        self.progress_log.configure(state="disabled")
        return "break"

    def _copy_log_selection(self) -> str:
        try:
            text = self.progress_log.get("sel.first", "sel.last")
        except tk.TclError:
            text = self.log_buffer
        if not text:
            return "break"
        self.clipboard_clear()
        self.clipboard_append(text)
        return "break"

    def _copy_all_logs(self) -> str:
        if not self.log_buffer:
            return "break"
        self.clipboard_clear()
        self.clipboard_append(self.log_buffer)
        return "break"

    def _paste_into(self, widget: ttk.Entry) -> str:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return "break"
        try:
            if widget.selection_present():
                widget.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        widget.insert("insert", text)
        return "break"

    def _paste_url(self) -> None:
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            messagebox.showwarning(APP_TITLE, "Το clipboard δεν περιέχει κείμενο.")
            return
        self.url_var.set(clean_spotify_url(text))

    def _spotify_track_map(self, spotify_url: str) -> dict[str, object]:
        try:
            from spotdl.types.album import Album
            from spotdl.types.song import Song
            from spotdl.utils.spotify import SpotifyClient, SpotifyError

            try:
                SpotifyClient()
            except SpotifyError:
                SpotifyClient.init("", "", headless=True)

            if "/track/" in spotify_url:
                songs = [Song.from_url(spotify_url)]
            else:
                _, songs = Album.get_metadata(spotify_url)
        except Exception as exc:
            self.events.put(("log", f"Δεν ήταν δυνατή η ανάγνωση Spotify tracklist: {exc}\n"))
            return {}
        return {song.url: song for song in songs}

    def _album_track_map(self, album_url: str) -> dict[str, object]:
        return self._spotify_track_map(album_url)

    def _missing_song_entries(self, errors_path: Path, album_url: str) -> list[dict[str, object]]:
        if not errors_path.exists() or errors_path.stat().st_size == 0:
            return []

        lines = errors_path.read_text(encoding="utf-8", errors="replace").splitlines()
        latest_start = 0
        for index, line in enumerate(lines):
            if re.match(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$", line.strip()):
                latest_start = index + 1

        track_map = self._spotify_track_map(album_url)
        entries: list[dict[str, object]] = []
        seen: set[str] = set()
        for line in lines[latest_start:]:
            url_match = re.search(r"(https://open\.spotify\.com/track/[^\s]+)", line)
            if not url_match:
                continue

            spotify_url = url_match.group(1)
            if spotify_url in seen:
                continue
            seen.add(spotify_url)

            song = track_map.get(spotify_url)
            title_match = re.search(r"song:\s*(.+?)(?:\s*-\s*(?:LookupError|AudioProviderError):|$)", line)
            query = title_match.group(1) if title_match else ""
            if not query:
                query = re.sub(r"^https://open\.spotify\.com/track/[^\s]+\s*-\s*", "", line).strip()
                query = re.sub(r"^(?:LookupError|AudioProviderError):\s*", "", query).strip()
                query = re.sub(r"^No results found for song:\s*", "", query).strip()
                query = re.sub(r"^YT-DLP download error\s*-\s*", "", query).strip()
            if not query and song is not None:
                query = f"{getattr(song, 'artist', '')} - {getattr(song, 'name', '')}"
            if not query:
                continue

            direct_youtube = re.search(r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[^\s]+", line)
            song_name = getattr(song, "name", "") if song is not None else ""
            artist = getattr(song, "artist", "") if song is not None else ""
            artists = getattr(song, "artists", []) if song is not None else []
            artist_text = ", ".join(artists) if isinstance(artists, list) else artist
            expected = f"{artist} - {song_name}".strip(" -") if song_name else query
            queries = [
                expected,
                f"{artist_text} - {song_name}".strip(" -"),
                f"{artist} {song_name}".strip(),
                f"{song_name} {artist} official audio".strip(),
                query,
            ]
            entries.append({
                "spotify_url": spotify_url,
                "expected": expected,
                "queries": [item for item in dict.fromkeys(queries) if item],
                "direct_url": direct_youtube.group(0) if direct_youtube else None,
            })
        return entries

    def _missing_song_matches(self, errors_path: Path, album_url: str) -> list[str]:
        matches: list[str] = []
        for entry in self._missing_song_entries(errors_path, album_url):
            spotify_url = str(entry["spotify_url"])
            expected = str(entry["expected"])
            youtube_url, title, _score = self._find_best_youtube_url(
                expected,
                [str(query) for query in entry["queries"]],
                str(entry["direct_url"]) if entry["direct_url"] else None,
            )
            if youtube_url:
                matches.append(f"{youtube_url}|{spotify_url}")
                self.events.put(("track_text", (expected, "Κατεβαίνει")))
                self.events.put(("log", f"Manual match: {expected} -> {title or youtube_url}\n"))
            else:
                self.events.put(("track_text", (expected, "Δεν βρέθηκε")))
                self.events.put(("log", f"Δεν βρέθηκε αξιόπιστο YouTube match για: {expected}\n"))
        return matches

    def _youtube_title(self, url: str) -> str | None:
        command = [*yt_dlp_command(), url, "--get-title", "--no-playlist"]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=35,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception:
            return None
        return next((line.strip() for line in result.stdout.splitlines() if line.strip()), None)

    def _youtube_candidates(self, query: str, limit: int = 6) -> list[tuple[str, str]]:
        command = [*yt_dlp_command(), f"ytsearch{limit}:{query}", "--get-title", "--get-id", "--no-playlist"]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception:
            return []

        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        candidates: list[tuple[str, str]] = []
        for index in range(0, len(lines) - 1, 2):
            first, second = lines[index], lines[index + 1]
            if re.fullmatch(r"[\w-]{11}", first):
                video_id, title = first, second
            elif re.fullmatch(r"[\w-]{11}", second):
                title, video_id = first, second
            else:
                continue
            if title:
                candidates.append((title, f"https://www.youtube.com/watch?v={video_id}"))
        return candidates

    def _find_best_youtube_url(self, expected: str, queries: list[str], direct_url: str | None = None) -> tuple[str | None, str | None, float]:
        best: tuple[str | None, str | None, float] = (None, None, 0.0)
        seen_urls: set[str] = set()

        if direct_url:
            title = self._youtube_title(direct_url)
            if title:
                score = match_score(expected, title)
                best = (direct_url, title, score)
                seen_urls.add(direct_url)

        for query in queries:
            for title, url in self._youtube_candidates(query):
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                score = match_score(expected, title)
                if score > best[2]:
                    best = (url, title, score)
                if score >= 0.92:
                    return best

        if best[0] and best[2] >= 0.55:
            return best
        if best[0] and best[1]:
            self.events.put(("log", f"Απορρίφθηκε πιθανό λάθος match: {expected} -> {best[1]}\n"))
        return None, None, 0.0

    def _retry_missing_songs(self, errors_path: Path, album_url: str, output_template: str) -> bool:
        max_attempts = 3
        had_errors = errors_path.exists() and errors_path.stat().st_size > 0

        for attempt in range(1, max_attempts + 1):
            matches = self._missing_song_matches(errors_path, album_url)
            if not matches:
                return errors_path.exists() and errors_path.stat().st_size > 0

            manual_errors = errors_path.with_name(f"_spotdl_manual_missing_songs_{attempt}.txt")
            if manual_errors.exists():
                manual_errors.unlink()

            command = [
                *spotdl_command(), "download", *matches,
                "--audio", "youtube",
                "--output", output_template,
                "--format", self.format_var.get(),
                "--max-filename-length", "160",
                "--print-errors",
                "--save-errors", str(manual_errors),
            ]
            self.events.put(("log", f"\nΑυτόματη προσπάθεια {attempt}/{max_attempts} για {len(matches)} χαμένα κομμάτια...\n"))

            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=spotdl_environment(),
                creationflags=flags,
            )
            assert self.process.stdout is not None
            retry_errors = False
            for line in self.process.stdout:
                if "LookupError:" in line or "AudioProviderError:" in line:
                    retry_errors = True
                self._update_tracks_from_raw_line(line)
                log_line = concise_spotdl_log(line)
                if log_line:
                    self.events.put(("log", log_line))
            retry_code = self.process.wait()
            self.process = None

            if manual_errors.exists() and manual_errors.stat().st_size > 0:
                errors_path.write_text(manual_errors.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                manual_errors.unlink()
                had_errors = True
                if attempt < max_attempts:
                    continue
                return True

            if manual_errors.exists():
                manual_errors.unlink()
            if retry_code == 0 and not retry_errors:
                if errors_path.exists():
                    errors_path.unlink()
                return False
            had_errors = True

        return had_errors

    def _organize_album_from_spotify(self, album_dir: Path, album_url: str) -> None:
        songs = self._album_track_map(album_url)
        by_title: dict[str, object] = {}
        for song in songs.values():
            key = normalize_match_text(getattr(song, "name", ""))
            if key:
                by_title[key] = song

        for audio_path in audio_files_in(album_dir):
            audio_title = read_audio_title(audio_path)
            song = by_title.get(normalize_match_text(audio_title))
            if song is None:
                filename_title = re.sub(r"^\d+\.", "", audio_path.stem)
                song = by_title.get(normalize_match_text(filename_title))
            if song is None:
                continue

            disc_number = int(getattr(song, "disc_number", 1) or 1)
            disc_count = int(getattr(song, "disc_count", 1) or 1)
            track_number = int(getattr(song, "track_number", 0) or 0)
            track_count = int(getattr(song, "tracks_count", 0) or 0)
            destination_dir = album_dir if disc_count == 1 else album_dir / f"Disc {disc_number}"
            destination_dir.mkdir(exist_ok=True)
            destination_name = re.sub(r"^\d+\.", f"{track_number:02d}.", audio_path.name)
            destination = destination_dir / destination_name
            if audio_path.resolve() != destination.resolve():
                if destination.exists():
                    destination.unlink()
                shutil.move(str(audio_path), str(destination))
                audio_path = destination
            set_track_disc_tags(audio_path, track_number, track_count, disc_number, disc_count)
            set_title_tag_to_filename(audio_path)

        for folder in album_dir.glob("Disc *"):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()

    def _watch_clipboard(self) -> None:
        if self.auto_paste_var.get() and self.process is None:
            try:
                text = self.clipboard_get().strip()
                if text != self.last_clipboard:
                    self.last_clipboard = text
                    match = re.search(r"https?://open\.spotify\.com/(album|playlist|track)/[^\s]+", text)
                    if match:
                        self.url_var.set(clean_spotify_url(match.group(0)))
            except tk.TclError:
                pass
        self.after(800, self._watch_clipboard)

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.root_var.get() or str(Path.home() / "Music"))
        if chosen:
            self.root_var.set(chosen)

    def _append_log(self, text: str) -> None:
        self.log_buffer += text
        self._append_progress_log(text)

    def _append_progress_log(self, text: str) -> None:
        latest_line = None
        for line in text.splitlines():
            line = line.strip()
            if (
                not line
                or line.startswith("===")
                or line.startswith("Βασικός φάκελος:")
                or line.startswith("Αυτόματος φάκελος:")
            ):
                continue
            latest_line = line
        if latest_line:
            self.progress_log_lines = [latest_line]
            self.status_var.set(latest_line)
        if not hasattr(self, "progress_log"):
            return
        self.progress_log.configure(state="normal")
        self.progress_log.delete("1.0", "end")
        self.progress_log.insert("end", "\n".join(self.progress_log_lines))
        self.progress_log.configure(state="disabled")
        self.progress_log.grid_remove()

    def _clear_progress_log(self) -> None:
        self.progress_log_lines = []
        if hasattr(self, "progress_log"):
            self.progress_log.configure(state="normal")
            self.progress_log.delete("1.0", "end")
            self.progress_log.configure(state="disabled")
            self.progress_log.grid_remove()

    def _clear_log(self) -> None:
        self.log_buffer = ""
        self._clear_progress_log()

    def _clear_tracks(self) -> None:
        self.track_rows.clear()
        self.track_lookup.clear()
        if hasattr(self, "tracks"):
            children = self.tracks.get_children()
            if children:
                self.tracks.delete(*children)

    def _scroll_tracks(self, event: tk.Event) -> str:
        self.tracks.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def _track_display_title(self, song: object) -> str:
        title = str(getattr(song, "name", "") or "Άγνωστος τίτλος")
        artist = str(getattr(song, "artist", "") or "")
        return f"{title} - {artist}" if artist else title

    def _load_track_rows(self, songs: list[object]) -> None:
        self._clear_tracks()
        for index, song in enumerate(songs, 1):
            disc_number = int(getattr(song, "disc_number", 1) or 1)
            track_number = int(getattr(song, "track_number", index) or index)
            display_number = str(track_number) if int(getattr(song, "disc_count", 1) or 1) == 1 else f"{disc_number}-{track_number}"
            title = self._track_display_title(song)
            item_id = f"track_{index}"
            self.tracks.insert("", "end", iid=item_id, values=(display_number, "Αναμονή", title))
            self.track_rows[item_id] = {"state": "Αναμονή", "title": title}
            song_title = str(getattr(song, "name", "") or "")
            artist = str(getattr(song, "artist", "") or "")
            for key in (song_title, f"{artist} - {song_title}", title):
                normalized = normalize_match_text(key)
                if normalized:
                    self.track_lookup.append((normalized, item_id))

    def _set_track_state(self, item_id: str, state: str) -> None:
        if item_id not in self.track_rows:
            return
        values = list(self.tracks.item(item_id, "values"))
        if len(values) >= 3:
            values[1] = state
            self.tracks.item(item_id, values=values)
            self.track_rows[item_id]["state"] = state

    def _find_track_item(self, text: str) -> str | None:
        normalized = normalize_match_text(text)
        if not normalized:
            return None
        best_item = None
        best_length = 0
        for key, item_id in self.track_lookup:
            if item_id not in self.track_rows:
                continue
            if key and (key in normalized or normalized in key) and len(key) > best_length:
                best_item = item_id
                best_length = len(key)
        return best_item

    def _update_track_by_text(self, text: str, state: str) -> None:
        item_id = self._find_track_item(text)
        if item_id:
            self._set_track_state(item_id, state)

    def _update_tracks_from_raw_line(self, line: str) -> None:
        text = line.strip()
        downloaded = re.search(r'Downloaded\s+"(.+?)"', text)
        if downloaded:
            self.events.put(("track_text", (downloaded.group(1), "✓ ΟΚ")))
            return

        processing = re.search(r"Processing query:\s*(.+)", text)
        if processing:
            query = processing.group(1)
            if "open.spotify.com/" not in query and "youtube.com/watch" not in query:
                self.events.put(("track_text", (query, "Κατεβαίνει")))
            return

        if "LookupError:" in text and "No results found for song:" in text:
            missing = text.split("No results found for song:", 1)[1].strip()
            self.events.put(("track_text", (missing, "Δεν βρέθηκε")))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "tracks":
                    self._load_track_rows(payload)
                elif kind == "track_text":
                    text, state = payload
                    self._update_track_by_text(text, state)
                elif kind == "name":
                    self.album_name_var.set(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "cover":
                    self._show_cover(Path(payload))
                elif kind == "done":
                    self._finish(int(payload))
                elif kind == "error":
                    self._append_log(f"\nΣΦΑΛΜΑ: {payload}\n")
                    self._finish(1)
        except queue.Empty:
            pass
        self.after(120, self._drain_events)

    def _start(self) -> None:
        url = clean_spotify_url(self.url_var.get())
        self.url_var.set(url)
        if not url:
            messagebox.showerror(APP_TITLE, "Επικόλλησε πρώτα ένα Spotify URL.")
            return
        if not re.search(r"open\.spotify\.com/(album|track)/", url):
            messagebox.showerror(APP_TITLE, "Χρησιμοποίησε Spotify album ή track link.")
            return

        root = Path(self.root_var.get()).expanduser()
        try:
            root.mkdir(parents=True, exist_ok=True)
            test = root / ".spotdl_write_test"
            test.write_text("ok", encoding="utf-8")
            test.unlink()
        except OSError as exc:
            messagebox.showerror(
                APP_TITLE,
                f"Δεν είναι δυνατή η εγγραφή στον φάκελο:\n{root}\n\nΑν χρησιμοποιείς τον D:, έλεγξε το BitLocker.\n\n{exc}",
            )
            return

        self._save_settings()
        self._clear_tracks()
        self._clear_progress_log()
        self.album_name_var.set("Ανάγνωση στοιχείων από το Spotify...")
        self.status_var.set("Η λήψη ξεκίνησε...")
        self.download_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.start(10)
        self._append_log(f"\n=== Νέα λήψη ===\nΒασικός φάκελος: {root}\n")
        threading.Thread(target=self._run_download, daemon=True).start()

    def _run_download(self) -> None:
        try:
            root = Path(self.root_var.get()).expanduser()
            url = clean_spotify_url(self.url_var.get())
            before = {p for p in root.iterdir() if p.is_dir()}

            is_album = "/album/" in url
            track_map = self._spotify_track_map(url)
            songs = sorted(
                track_map.values(),
                key=lambda song: (
                    int(getattr(song, "disc_number", 1) or 1),
                    int(getattr(song, "track_number", 0) or 0),
                    str(getattr(song, "name", "")),
                ),
            )
            if songs:
                self.events.put(("tracks", songs))

            if is_album:
                output_template = str(root / "{album-artist} - {album}({year})" / "{track-number}.{title}.{output-ext}")
            else:
                output_template = str(root / "Tracks" / "{artists} - {title}.{output-ext}")
            temp_errors = root / "_spotdl_missing_songs.txt"
            if temp_errors.exists():
                temp_errors.unlink()

            command = [
                *spotdl_command(), "download", url,
                "--audio", "youtube-music", "youtube",
                "--output", output_template,
                "--format", self.format_var.get(),
                "--search-query", "{artist} - {title}",
                "--max-filename-length", "160",
                "--print-errors",
                "--save-errors", str(temp_errors),
            ]
            if self.lyrics_var.get():
                command.append("--generate-lrc")

            self.events.put(("log", "Ξεκίνησε η λήψη.\n"))

            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=spotdl_environment(),
                creationflags=flags,
            )

            assert self.process.stdout is not None
            errors_detected = False
            for line in self.process.stdout:
                if "LookupError:" in line or "AudioProviderError:" in line:
                    errors_detected = True
                self._update_tracks_from_raw_line(line)
                log_line = concise_spotdl_log(line)
                if log_line:
                    self.events.put(("log", log_line))

            code = self.process.wait()
            self.process = None

            if temp_errors.exists() and temp_errors.stat().st_size > 0:
                errors_detected = self._retry_missing_songs(temp_errors, url, output_template)

            after = {p for p in root.iterdir() if p.is_dir()}
            candidates = list(after - before)
            album_dir = None

            if not is_album:
                tracks_dir = root / "Tracks"
                if tracks_dir.exists() and audio_files_in(tracks_dir):
                    album_dir = tracks_dir
            else:
                candidates_with_audio = [p for p in candidates if audio_files_in(p)]
                album_dir = max(candidates_with_audio, key=lambda p: p.stat().st_mtime) if candidates_with_audio else newest_album_folder(root)

            if album_dir:
                self.last_album_dir = album_dir
                self.events.put(("name", album_dir.name))
                self.events.put(("status", f"Ολοκληρώθηκε: {album_dir.name}"))
                self.events.put(("log", f"\nΑυτόματος φάκελος: {album_dir}\n"))

                if is_album:
                    self._organize_album_from_spotify(album_dir, url)
                else:
                    set_titles_to_filenames(album_dir)

                destination = album_dir / "Missing Songs.txt"
                if not temp_errors.exists() and destination.exists():
                    destination.unlink()

                if temp_errors.exists():
                    if temp_errors.stat().st_size > 0:
                        errors_detected = True
                        if destination.exists():
                            with destination.open("a", encoding="utf-8") as target:
                                target.write("\n")
                                target.write(temp_errors.read_text(encoding="utf-8"))
                            temp_errors.unlink()
                        else:
                            shutil.move(str(temp_errors), destination)
                    else:
                        temp_errors.unlink()

                if is_album and self.cover_var.get():
                    cover = extract_cover(album_dir, self.folder_jpg_var.get())
                    if cover:
                        self.events.put(("cover", str(cover)))
                        self.events.put(("log", "Δημιουργήθηκε cover.jpg.\n"))

                if is_album and self.info_var.get():
                    if create_album_info(album_dir, url):
                        self.events.put(("log", "Δημιουργήθηκε Album Info.txt.\n"))
            else:
                self.events.put(("name", "Δεν δημιουργήθηκε φάκελος"))
                self.events.put(("status", "Η διαδικασία ολοκληρώθηκε χωρίς αρχεία ήχου"))

            if code == 0 and errors_detected:
                code = 1
            self.events.put(("done", str(code)))
        except FileNotFoundError:
            self.events.put(("error", "Δεν βρέθηκε το spotdl. Τρέξε πρώτα το Install or Update.bat."))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _finish(self, code: int) -> None:
        self.progress.stop()
        self.download_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

        if code == 0:
            for item_id, row in list(self.track_rows.items()):
                if row.get("state") != "✓ ΟΚ":
                    self._set_track_state(item_id, "✓ ΟΚ")
            self._append_log("\nΗ διαδικασία ολοκληρώθηκε.\n")
            self._show_done_dialog()
        else:
            for item_id, row in list(self.track_rows.items()):
                if row.get("state") in {"Αναμονή", "Κατεβαίνει"}:
                    self._set_track_state(item_id, "Εκκρεμεί")
            self.status_var.set("Ολοκληρώθηκε με σφάλματα ή ελλείψεις")
            self._append_log(f"\nΗ διαδικασία τερματίστηκε με κωδικό {code}.\n")
            self._show_error_dialog()

    def _show_done_dialog(self) -> None:
        self._show_result_dialog("Η λήψη ολοκληρώθηκε.", show_missing=False)

    def _show_error_dialog(self) -> None:
        self._show_result_dialog(
            "Υπήρξαν σφάλματα ή ελλείψεις.",
            "Δες την καταγραφή και το Missing Songs.txt.",
            show_missing=True,
        )

    def _show_result_dialog(self, title: str, detail: str = "", show_missing: bool = False) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(APP_TITLE)
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.configure(bg="#ffffff")

        body = ttk.Frame(dialog, padding=18, style="Card.TFrame")
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=title, style="Section.TLabel").pack(anchor="w")
        if detail:
            ttk.Label(body, text=detail, style="Card.TLabel", wraplength=460, foreground="#60646c").pack(anchor="w", pady=(8, 0))
        if self.last_album_dir:
            ttk.Label(body, text=str(self.last_album_dir), style="Card.TLabel", wraplength=460, foreground="#60646c").pack(anchor="w", pady=(8, 16))

        buttons = ttk.Frame(body, style="Card.TFrame")
        buttons.pack(fill="x")
        ttk.Button(buttons, text="ΟΚ", command=dialog.destroy).pack(side="right")
        if self.last_album_dir and self.last_album_dir.exists():
            ttk.Button(buttons, text="Άνοιγμα φακέλου", command=lambda: (self._open_album(), dialog.destroy())).pack(side="right", padx=(0, 8))
            missing_path = self.last_album_dir / "Missing Songs.txt"
            if show_missing and missing_path.exists():
                ttk.Button(
                    buttons,
                    text="Άνοιγμα Missing Songs",
                    command=lambda: (os.startfile(missing_path), dialog.destroy()),
                ).pack(side="right", padx=(0, 8))

        dialog.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        dialog.grab_set()
        dialog.focus_set()

    def _show_cover(self, path: Path) -> None:
        try:
            image = Image.open(path)
            image.thumbnail((265, 265))
            self.cover_photo = ImageTk.PhotoImage(image)
            self.cover_label.configure(image=self.cover_photo, text="", width=265, height=265)
        except Exception as exc:
            self._append_log(f"Αδυναμία εμφάνισης εξωφύλλου: {exc}\n")

    def _stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.status_var.set("Η λήψη διακόπτεται...")
            self._append_log("\nΖητήθηκε διακοπή...\n")

    def _open_album(self) -> None:
        if self.last_album_dir and self.last_album_dir.exists():
            os.startfile(self.last_album_dir)
        else:
            messagebox.showinfo(APP_TITLE, "Δεν έχει ολοκληρωθεί ακόμη λήψη.")

    def _open_root(self) -> None:
        root = Path(self.root_var.get()).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        os.startfile(root)

    def _load_settings(self) -> None:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return

        self.root_var.set(data.get("root", self.root_var.get()))
        self.format_var.set(data.get("format", self.format_var.get()))
        self.cover_var.set(data.get("cover", True))
        self.folder_jpg_var.set(data.get("folder_jpg", True))
        self.lyrics_var.set(data.get("lyrics", True))
        self.info_var.set(data.get("info", True))
        self.errors_var.set(data.get("errors", True))
        self.auto_paste_var.set(data.get("auto_paste", True))

    def _save_settings(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(
                {
                    "root": self.root_var.get(),
                    "format": self.format_var.get(),
                    "cover": self.cover_var.get(),
                    "folder_jpg": self.folder_jpg_var.get(),
                    "lyrics": self.lyrics_var.get(),
                    "info": self.info_var.get(),
                    "errors": self.errors_var.get(),
                    "auto_paste": self.auto_paste_var.get(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _on_close(self) -> None:
        self._save_settings()
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno(APP_TITLE, "Υπάρχει ενεργή λήψη. Θέλεις να κλείσεις;"):
                return
            self.process.terminate()
        self.destroy()


if __name__ == "__main__":
    if ensure_single_instance():
        SpotDLApp().mainloop()
