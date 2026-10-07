from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import unicodedata
import webbrowser
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any
from urllib.parse import quote_plus

import ttkbootstrap as tb
import requests
from ttkbootstrap.style.theme import Colors, ThemeDefinition
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
YT_DLP_TIMEOUT_ARGS = "--socket-timeout 20 --retries 2 --fragment-retries 2"
YT_DLP_COMPAT_ARGS = f"{YT_DLP_TIMEOUT_ARGS} --extractor-args youtube:player_client=android"
YOUTUBE_BROWSERS = {"Χωρίς σύνδεση": None, "Firefox": "firefox", "Chrome": "chrome", "Edge": "edge"}


def repair_ytdlp_args(browser: str | None = None) -> str:
    # Authenticated requests need yt-dlp's cookie-compatible default clients.
    if browser in YOUTUBE_BROWSERS.values() and browser:
        return f"{YT_DLP_TIMEOUT_ARGS} --cookies-from-browser {browser}"
    return YT_DLP_COMPAT_ARGS


def youtube_access_error(line: str) -> str | None:
    text = line.lower()
    if any(marker in text for marker in ("sign in to confirm your age", "age-restricted", "inappropriate for some users")):
        return "Το YouTube απαιτεί επιβεβαίωση ηλικίας. Άνοιξε το βίντεο στον συνδεδεμένο browser, αποδέξου την προειδοποίηση και επίλεξε τον ίδιο browser στη «Σύνδεση YouTube» πριν ξαναδοκιμάσεις."
    if any(marker in text for marker in ("could not copy", "failed to decrypt", "could not find firefox cookies", "could not find chrome cookies", "could not find edge cookies")):
        return "Δεν διαβάστηκε η σύνδεση του browser. Αν χρησιμοποιείς Chrome/Edge, κλείσε τον browser και ξαναδοκίμασε ή επίλεξε Firefox αφού συνδεθείς στο YouTube εκεί."
    return None


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


def hidden_subprocess_options() -> dict[str, Any]:
    """Return Windows flags that prevent child console windows from flashing."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def yt_dlp_command() -> list[str]:
    if getattr(sys, "frozen", False):
        # The GUI executable is not a Python interpreter. Use the bundled
        # console worker so search JSON and errors reach the parent process.
        return [str(Path(sys.executable).resolve().parent / "spotdl-cli.exe"), "--yt-dlp"]
    wrapper = resource_path("yt_dlp_no_window.py")
    if wrapper.exists():
        return [sys.executable, str(wrapper)]
    return [sys.executable, "-m", "yt_dlp"]


def clean_spotify_url(url: str) -> str:
    match = re.search(r"https?://open\.spotify\.com/(album|playlist|track)/[^?\s#]+", url.strip())
    return match.group(0) if match else url.strip()


def audio_files_in(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def parse_number_tag(value: object, default: int = 0) -> int:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    match = re.match(r"\s*(\d+)", str(value or ""))
    return int(match.group(1)) if match else default


def album_audio_positions(folder: Path) -> set[tuple[int, int]]:
    """Return the (disc, track) positions that really exist on disk."""
    positions: set[tuple[int, int]] = set()
    for audio_path in audio_files_in(folder):
        disc_number = 1
        track_number = 0
        try:
            audio = File(audio_path, easy=True)
            tags = getattr(audio, "tags", None) if audio is not None else None
            if tags:
                disc_number = parse_number_tag(tags.get("discnumber"), 1)
                track_number = parse_number_tag(tags.get("tracknumber"), 0)
        except Exception:
            pass

        if track_number <= 0:
            track_number = parse_number_tag(audio_path.stem, 0)
        if disc_number <= 1:
            disc_match = re.fullmatch(r"Disc\s+(\d+)", audio_path.parent.name, flags=re.IGNORECASE)
            if disc_match:
                disc_number = int(disc_match.group(1))
        if track_number > 0:
            positions.add((max(disc_number, 1), track_number))
    return positions


def normalize_match_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9\u0370-\u03ff]+", "", value)


def match_words(value: str) -> set[str]:
    value = unicodedata.normalize("NFKD", value.lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    words = re.findall(r"[a-z0-9\u0370-\u03ff]+", value)
    stop_words = {"and", "feat", "ft", "official", "audio", "video", "lyrics", "remix", "explicit"}
    result: set[str] = set()
    for word in words:
        if len(word) <= 1 or word in stop_words:
            continue
        result.add(word)
        if word in {"sto", "στο", "στo"}:
            result.update({"sto", "στο", "στo"})
    return result


def match_variants(value: str) -> list[str]:
    without_features = re.sub(r"\s*[\(\[]?\b(?:feat|ft|featuring)\b.*$", "", value, flags=re.IGNORECASE).strip()
    return [item for item in dict.fromkeys([value, without_features]) if item]


def normalized_match_text(value: str) -> str:
    words = match_words(value)
    return " ".join(sorted(words))


def track_title_from_expected(expected: str) -> str:
    if " - " in expected:
        return expected.split(" - ", 1)[1].strip()
    return expected.strip()


def exact_title_bonus(expected: str, candidate_title: str) -> float:
    expected_track = track_title_from_expected(expected)
    expected_norm = normalized_match_text(expected_track)
    candidate_norm = normalized_match_text(candidate_title)
    if not expected_norm or not candidate_norm:
        return 0.0
    if candidate_norm == expected_norm:
        return 0.22
    expected_words = set(expected_norm.split())
    candidate_words = set(candidate_norm.split())
    if expected_words and expected_words <= candidate_words:
        return 0.10
    return 0.0


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


def youtube_official_score(candidate: dict[str, str]) -> float:
    text = " ".join(
        candidate.get(key, "")
        for key in ("title", "channel", "description")
    ).lower()
    score = 0.0
    if "auto-generated by youtube" in text:
        score += 0.30
    if "provided to youtube by" in text:
        score += 0.24
    if "official artist channel" in text:
        score += 0.10
    if " - topic" in candidate.get("channel", "").lower():
        score += 0.08
    noisy_terms = ("live", "cover", "karaoke", "reaction", "remix", "sped up", "slowed", "nightcore")
    if any(term in text for term in noisy_terms):
        score -= 0.22
    if text.strip() and "auto-generated by youtube" not in text and "provided to youtube by" not in text:
        score -= 0.06
    return score


def duration_match_score(expected_seconds: float | None, candidate_seconds: float | None) -> float:
    if not expected_seconds or not candidate_seconds:
        return 0.0
    diff = abs(expected_seconds - candidate_seconds)
    if diff <= 4:
        return 0.24
    if diff <= 10:
        return 0.08
    if diff >= 25:
        return -0.35
    if diff >= 20:
        return -0.35
    if diff >= 15:
        return -0.22
    return -0.08


def song_album_name(song: object) -> str:
    for name in ("album_name", "album", "album_title"):
        value = getattr(song, name, "")
        if value:
            return str(value)
    return ""


def is_risky_manual_title(title: str) -> bool:
    words = match_words(title)
    risky_terms = {
        "show", "intro", "interlude", "outro", "skit", "theme",
        "mix", "part", "pt", "version", "radio", "edit", "live",
    }
    return bool(words & risky_terms)


def song_duration_seconds(song: object) -> float | None:
    for name in ("duration", "duration_seconds"):
        value = getattr(song, name, None)
        if value:
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                continue
            return seconds / 1000 if seconds > 10000 else seconds
    return None


def format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} ώρες" if hours != 1 else "1 ώρα")
    if minutes:
        parts.append(f"{minutes} λεπτά" if minutes != 1 else "1 λεπτό")
    if secs or not parts:
        parts.append(f"{secs} δευτ.")
    return " ".join(parts)


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
    access_error = youtube_access_error(text)
    if access_error:
        return access_error + "\n"
    if "ERROR:" in text or "AudioProviderError:" in text:
        return text + "\n"

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
            cover_path = album_dir / "Cover.jpg"
            legacy_cover = next(
                (
                    path
                    for path in album_dir.iterdir()
                    if path.is_file()
                    and path.name.casefold() == "cover.jpg"
                    and path.name != cover_path.name
                ),
                None,
            )
            if legacy_cover is not None:
                temporary_cover = album_dir / ".spotdl-cover-case.tmp"
                legacy_cover.replace(temporary_cover)
                temporary_cover.replace(cover_path)
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


def select_album_output_dir(
    folders: set[Path],
    new_folders: set[Path],
    before_audio: dict[Path, tuple[int, int]],
    songs: list[object],
) -> Path | None:
    """Find only the folder produced or matched by the current album run."""
    changed_with_audio: list[Path] = []
    for folder in folders:
        for audio_path in audio_files_in(folder):
            signature = (audio_path.stat().st_size, audio_path.stat().st_mtime_ns)
            if before_audio.get(audio_path.resolve()) != signature:
                changed_with_audio.append(folder)
                break

    if changed_with_audio:
        return max(changed_with_audio, key=lambda p: p.stat().st_mtime)
    if new_folders:
        # A failed album download can still create its correct, empty destination
        # folder. Never substitute an unrelated older album containing audio.
        return max(new_folders, key=lambda p: p.stat().st_mtime)

    expected_titles = {
        normalize_match_text(str(getattr(song, "name", "") or ""))
        for song in songs
    }
    expected_titles.discard("")
    minimum_matches = 1 if len(expected_titles) <= 1 else 2
    matching_existing: list[tuple[int, Path]] = []
    for folder in folders:
        actual_titles: set[str] = set()
        for audio_path in audio_files_in(folder):
            actual_titles.add(normalize_match_text(read_audio_title(audio_path)))
            filename_title = re.sub(r"^\d+[.\s_-]*", "", audio_path.stem)
            actual_titles.add(normalize_match_text(filename_title))
        matches = len(expected_titles & actual_titles)
        if matches >= minimum_matches:
            matching_existing.append((matches, folder))
    if not matching_existing:
        return None
    return max(matching_existing, key=lambda item: (item[0], item[1].stat().st_mtime))[1]


class SpotDLApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        icon_path = resource_path("spotdl_gui_icon.ico")
        if icon_path.exists():
            self.iconbitmap(str(icon_path))
        self._set_startup_geometry()
        self.configure(bg="#f3f5f7")

        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.cover_photo: ImageTk.PhotoImage | None = None
        self.cover_preview_job: str | None = None
        self.cover_preview_generation = 0
        self.preview_cover_url = ""
        self.last_album_dir: Path | None = None
        self.last_clipboard = ""
        self.log_buffer = ""
        self.progress_log_lines: list[str] = []
        self.track_rows: dict[str, dict[str, str]] = {}
        self.track_lookup: list[tuple[str, str]] = []
        self.track_songs: dict[str, object] = {}
        self.download_started_at: float | None = None
        self.suppress_url_reset = False
        self.operation_active = False
        self.active_download_url = ""

        self.url_var = tk.StringVar()
        self.root_var = tk.StringVar(value=str(Path.home() / "Music"))
        self.format_var = tk.StringVar(value="mp3")
        self.bitrate_var = tk.StringVar(value="320k")
        self.album_name_var = tk.StringVar(value="Θα δημιουργηθεί αυτόματα")
        self.status_var = tk.StringVar(value="Έτοιμο")
        self.selected_track_var = tk.StringVar(value="Δεν έχει επιλεγεί κομμάτι")
        self.repair_url_var = tk.StringVar()
        self.youtube_browser_var = tk.StringVar(value="Χωρίς σύνδεση")
        self.cover_var = tk.BooleanVar(value=True)
        self.folder_jpg_var = tk.BooleanVar(value=True)
        self.lyrics_var = tk.BooleanVar(value=True)
        self.info_var = tk.BooleanVar(value=True)
        self.errors_var = tk.BooleanVar(value=True)
        self.auto_paste_var = tk.BooleanVar(value=True)
        self.threads_var = tk.IntVar(value=4)

        self._configure_style()
        self._load_settings()
        self._build_ui()
        self.url_var.trace_add("write", self._on_url_change)
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
        navy = "#13294b"
        navy_hover = "#1d3f66"
        navy_pressed = "#0b1d35"
        style = tb.Style()
        if "spotdl-navy" not in style.theme_names():
            colors = Colors(
                primary=navy,
                secondary="#66717d",
                success=navy,
                info="#315b82",
                warning="#c17b00",
                danger="#b02a37",
                light="#eff2f5",
                dark="#17202a",
                bg="#ffffff",
                fg="#212529",
                selectbg="#dce6f0",
                selectfg=navy,
                border="#d6dce2",
                inputfg="#212529",
                inputbg="#ffffff",
                active="#eef1f4",
            )
            style.register_theme(ThemeDefinition("spotdl-navy", colors, mode="light"))
        style.theme_use("spotdl-navy")
        self.app_style = style
        ink = "#171a1f"
        muted = "#68717d"
        app_bg = "#f3f5f7"
        card_bg = "#ffffff"
        line = "#dde2e7"

        style.configure(".", font=("Segoe UI", 10))
        style.configure("App.TFrame", background=app_bg)
        style.configure("Card.TFrame", background=card_bg, bordercolor=line, borderwidth=1, relief="solid")
        style.configure("CardBody.TFrame", background=card_bg)
        style.configure("Hero.TFrame", background="#191414")
        style.configure("TLabel", background=app_bg, foreground=ink)
        style.configure("Card.TLabel", background=card_bg, foreground=ink)
        style.configure(
            "HeroTitle.TLabel",
            background="#191414",
            foreground="#ffffff",
            font=("Segoe UI Variable Display", 23, "bold"),
        )
        style.configure(
            "HeroSubtitle.TLabel",
            background="#191414",
            foreground="#c8c8c8",
            font=("Segoe UI", 10),
        )
        style.configure(
            "HeroBadge.TLabel",
            background=navy,
            foreground="#ffffff",
            font=("Segoe UI", 9, "bold"),
            padding=(12, 7),
        )
        style.configure("Title.TLabel", background=app_bg, foreground=ink, font=("Segoe UI Variable Display", 22, "bold"))
        style.configure("Subtitle.TLabel", background=app_bg, foreground=muted)
        style.configure("Section.TLabel", background=card_bg, foreground=ink, font=("Segoe UI Variable Display", 12, "bold"))
        style.configure("Field.TLabel", background=card_bg, foreground="#4e5965", font=("Segoe UI", 9, "bold"))
        style.configure("Muted.TLabel", background=card_bg, foreground=muted)
        style.configure("Status.TLabel", background=card_bg, foreground="#3d4650", font=("Segoe UI", 9))
        style.configure("StatusDot.TLabel", background=card_bg, foreground=navy, font=("Segoe UI", 12, "bold"))
        style.configure("TButton", padding=(13, 8), font=("Segoe UI", 9, "bold"))
        style.configure(
            "Primary.TButton",
            background=navy,
            foreground="#ffffff",
            bordercolor=navy,
            padding=(20, 10),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("active", navy_hover), ("pressed", navy_pressed), ("disabled", "#b8c4d1")],
            bordercolor=[("active", navy_hover), ("pressed", navy_pressed), ("disabled", "#b8c4d1")],
            foreground=[("disabled", "#617286")],
        )
        style.configure(
            "YouTube.TButton",
            background="#ff0033",
            foreground="#ffffff",
            bordercolor="#ff0033",
            focuscolor="#ff0033",
            font=("Segoe UI", 9, "bold"),
            padding=(13, 9),
        )
        style.map(
            "YouTube.TButton",
            background=[("active", "#d9002b"), ("pressed", "#bd0025")],
            bordercolor=[("active", "#d9002b"), ("pressed", "#bd0025")],
        )
        style.configure(
            "Outline.TButton",
            background=card_bg,
            foreground="#37404a",
            bordercolor="#cfd5dc",
            padding=(13, 8),
        )
        style.map(
            "Outline.TButton",
            background=[("active", "#f0f3f5"), ("pressed", "#e5e9ed")],
            bordercolor=[("active", "#aab3bd")],
        )
        style.configure(
            "DangerOutline.TButton",
            background=card_bg,
            foreground="#c23b43",
            bordercolor="#e8b7bb",
            padding=(13, 8),
        )
        style.map("DangerOutline.TButton", background=[("active", "#fff1f2")], bordercolor=[("active", "#dc747b")])
        style.configure("TEntry", fieldbackground="#ffffff", foreground=ink, bordercolor="#cfd5dc", padding=8)
        style.map("TEntry", bordercolor=[("focus", navy)])
        style.configure("TCombobox", fieldbackground="#ffffff", foreground=ink, bordercolor="#cfd5dc", padding=7)
        style.map("TCombobox", bordercolor=[("focus", navy)])
        style.configure("Modern.TCheckbutton", background=card_bg, foreground="#3d4650", padding=(0, 3))
        style.map("Modern.TCheckbutton", background=[("active", card_bg)])
        style.configure("Options.TLabelframe", background=card_bg, bordercolor=line, borderwidth=1, relief="solid")
        style.configure(
            "Options.TLabelframe.Label",
            background=card_bg,
            foreground=ink,
            font=("Segoe UI", 10, "bold"),
        )
        style.configure("Spotify.Horizontal.TProgressbar", troughcolor="#e8ecef", background=navy, bordercolor="#e8ecef")
        style.configure(
            "Tracks.Treeview",
            background=card_bg,
            fieldbackground=card_bg,
            foreground="#303740",
            borderwidth=0,
            relief="flat",
            font=("Segoe UI", 10),
            rowheight=32,
        )
        style.map("Tracks.Treeview", background=[("selected", "#dce6f0")], foreground=[("selected", navy)])
        style.configure(
            "Tracks.Treeview.Heading",
            background="#eef1f3",
            foreground="#35404a",
            borderwidth=0,
            relief="flat",
            padding=(8, 9),
            font=("Segoe UI", 9, "bold"),
        )
        style.map("Tracks.Treeview.Heading", background=[("active", "#e3e8eb")])

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=18, style="App.TFrame")
        outer.pack(fill="both", expand=True)

        hero = ttk.Frame(outer, padding=(22, 17), style="Hero.TFrame")
        hero.pack(fill="x", pady=(0, 14))
        hero_copy = ttk.Frame(hero, style="Hero.TFrame")
        hero_copy.pack(side="left", fill="x", expand=True)
        ttk.Label(hero_copy, text="SpotDL Album Downloader", style="HeroTitle.TLabel").pack(anchor="w")
        ttk.Label(
            hero_copy,
            text="Spotify λήψεις, οργανωμένες σωστά — με εξώφυλλο, metadata και στίχους.",
            style="HeroSubtitle.TLabel",
        ).pack(anchor="w", pady=(3, 0))
        ttk.Label(hero, text="DESKTOP  ·  PRO v4", style="HeroBadge.TLabel").pack(side="right", padx=(18, 0))

        content = ttk.Panedwindow(outer, orient="horizontal")
        content.pack(fill="both", expand=True)

        left = ttk.Frame(content, style="Card.TFrame", padding=20)
        right_shell = ttk.Frame(content, style="Card.TFrame")
        content.add(left, weight=3)
        content.add(right_shell, weight=1)

        right_canvas = tk.Canvas(
            right_shell,
            bg="#ffffff",
            highlightthickness=0,
            bd=0,
            yscrollincrement=8,
        )
        right_scroll = ttk.Scrollbar(right_shell, orient="vertical", command=right_canvas.yview)
        right_canvas.configure(yscrollcommand=right_scroll.set)
        right_canvas.pack(side="left", fill="both", expand=True)
        right_scroll.pack(side="right", fill="y")

        right = ttk.Frame(right_canvas, style="CardBody.TFrame", padding=18)
        right_window = right_canvas.create_window((0, 0), window=right, anchor="nw")

        def resize_right_panel(_event: tk.Event) -> None:
            right_canvas.itemconfigure(right_window, width=right_canvas.winfo_width())

        def update_right_scrollregion(_event: tk.Event) -> None:
            right_canvas.configure(scrollregion=right_canvas.bbox("all"))

        def scroll_right_panel(event: tk.Event) -> str:
            steps = int(-event.delta / 30)
            if steps == 0:
                steps = -1 if event.delta > 0 else 1
            right_canvas.yview_scroll(steps, "units")
            return "break"

        def bind_right_mousewheel(widget: tk.Widget) -> None:
            widget.bind("<MouseWheel>", scroll_right_panel, add="+")
            for child in widget.winfo_children():
                bind_right_mousewheel(child)

        right_canvas.bind("<Configure>", resize_right_panel)
        right.bind("<Configure>", update_right_scrollregion)
        right_canvas.bind("<MouseWheel>", scroll_right_panel, add="+")

        # Keep the essential fields permanently visible. The track list owns all
        # remaining vertical space instead of competing with a scrollable control
        # pane whose buttons could disappear above it.
        controls = ttk.Frame(left, style="CardBody.TFrame")
        controls.pack(fill="x")
        tracks_frame = ttk.Frame(left, style="CardBody.TFrame")
        tracks_frame.pack(fill="both", expand=True, pady=(8, 0))

        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="SPOTIFY URL", style="Field.TLabel").grid(row=0, column=0, sticky="w", pady=7)
        self.url_entry = ttk.Entry(controls, textvariable=self.url_var)
        self.url_entry.grid(row=0, column=1, sticky="ew", padx=(12, 8), pady=7)
        ttk.Button(controls, text="Επικόλληση", style="Outline.TButton", command=self._paste_url).grid(row=0, column=2, pady=7)

        ttk.Label(controls, text="ΟΝΟΜΑ ΦΑΚΕΛΟΥ", style="Field.TLabel").grid(row=1, column=0, sticky="w", pady=7)
        ttk.Entry(controls, textvariable=self.album_name_var, state="readonly").grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=7
        )

        ttk.Label(controls, text="ΑΠΟΘΗΚΕΥΣΗ ΣΕ", style="Field.TLabel").grid(row=2, column=0, sticky="w", pady=7)
        self.root_entry = ttk.Entry(controls, textvariable=self.root_var)
        self.root_entry.grid(row=2, column=1, sticky="ew", padx=(12, 8), pady=7)
        ttk.Button(controls, text="Αναζήτηση…", style="Outline.TButton", command=self._browse).grid(row=2, column=2, pady=7)

        ttk.Label(controls, text="ΜΟΡΦΗ ΗΧΟΥ", style="Field.TLabel").grid(row=3, column=0, sticky="w", pady=7)
        ttk.Combobox(
            controls,
            textvariable=self.format_var,
            values=("mp3", "m4a", "opus", "flac", "ogg", "wav"),
            state="readonly",
            width=12,
        ).grid(row=3, column=1, sticky="w", padx=(12, 0), pady=7)

        ttk.Label(controls, text="BITRATE", style="Field.TLabel").grid(row=4, column=0, sticky="w", pady=7)
        ttk.Combobox(
            controls,
            textvariable=self.bitrate_var,
            values=("320k", "256k", "192k", "128k"),
            state="readonly",
            width=12,
        ).grid(row=4, column=1, sticky="w", padx=(12, 0), pady=7)

        actions = ttk.Frame(controls, style="CardBody.TFrame")
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(12, 9))
        self.download_btn = ttk.Button(actions, text="↓  ΛΗΨΗ", style="Primary.TButton", command=self._start)
        self.download_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Διακοπή", style="DangerOutline.TButton", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(actions, text="Άνοιγμα φακέλου", style="Outline.TButton", command=self._open_album).pack(side="right")

        self.progress = tb.Progressbar(controls, mode="indeterminate", bootstyle="success-striped")
        self.progress.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 7))

        status_row = ttk.Frame(controls, style="CardBody.TFrame")
        status_row.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(0, 5))
        ttk.Label(status_row, text="●", style="StatusDot.TLabel").pack(side="left", padx=(0, 7))
        ttk.Label(status_row, textvariable=self.status_var, style="Status.TLabel").pack(side="left")

        self.progress_log = tk.Text(
            controls,
            height=3,
            wrap="word",
            state="disabled",
            font=("Segoe UI", 10),
            bg="#f7f9fa",
            fg="#3f454c",
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground="#e0e5e9",
            padx=10,
            pady=8,
        )
        self.progress_log.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.progress_log.grid_remove()

        track_header = ttk.Frame(tracks_frame, style="CardBody.TFrame")
        track_header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 7))
        ttk.Label(track_header, text="Tracklist", style="Section.TLabel").pack(side="left")
        self.clear_tracks_btn = ttk.Button(
            track_header,
            text="Καθαρισμός tracklist",
            style="DangerOutline.TButton",
            command=self._clear_tracks_from_button,
        )
        self.clear_tracks_btn.pack(side="right")
        tracks_frame.columnconfigure(0, weight=1)
        tracks_frame.rowconfigure(1, weight=1)

        self.tracks = ttk.Treeview(
            tracks_frame,
            columns=("number", "artist", "title", "state"),
            show="headings",
            height=6,
            style="Tracks.Treeview",
            selectmode="browse",
        )
        self.tracks.heading("number", text="#")
        self.tracks.heading("artist", text="Καλλιτέχνης")
        self.tracks.heading("title", text="Τίτλος")
        self.tracks.heading("state", text="Κατάσταση")
        self.tracks.column("number", width=46, minwidth=42, anchor="center", stretch=False)
        self.tracks.column("artist", width=190, minwidth=120, anchor="w")
        self.tracks.column("title", width=300, minwidth=180, anchor="w")
        self.tracks.column("state", width=120, minwidth=110, anchor="center", stretch=False)
        self.tracks.grid(row=1, column=0, sticky="nsew")
        tracks_scroll = ttk.Scrollbar(tracks_frame, orient="vertical", command=self.tracks.yview)
        tracks_scroll.grid(row=1, column=1, sticky="ns")
        self.tracks.configure(yscrollcommand=tracks_scroll.set)
        self.tracks.tag_configure("waiting", foreground="#737c86")
        self.tracks.tag_configure("active", foreground="#315b82")
        self.tracks.tag_configure("success", foreground="#13294b")
        self.tracks.tag_configure("warning", foreground="#a06300")
        self.tracks.tag_configure("error", foreground="#bd3540")
        self.tracks.bind("<MouseWheel>", self._scroll_tracks)
        self.tracks.bind("<<TreeviewSelect>>", lambda _event: self._update_repair_panel())

        ttk.Label(right, text="Εξώφυλλο άλμπουμ", style="Section.TLabel").pack(anchor="w")
        self.cover_label = tk.Label(
            right,
            text="Επικόλλησε Spotify album URL\nγια άμεση προεπισκόπηση.",
            bg="#eef1f3",
            fg="#70757a",
            width=28,
            height=15,
            justify="center",
            relief="flat",
        )
        self.cover_label.pack(fill="both", pady=(12, 18))

        options = ttk.LabelFrame(right, text="  Επιλογές αρχείων  ", padding=10, style="Options.TLabelframe")
        options.pack(fill="x", pady=(0, 18))
        options.columnconfigure(0, weight=1)
        options.columnconfigure(1, weight=1)
        ttk.Checkbutton(options, text="Cover.jpg", style="Modern.TCheckbutton", variable=self.cover_var).grid(row=0, column=0, sticky="w", padx=4, pady=3)
        ttk.Checkbutton(options, text="folder.jpg", style="Modern.TCheckbutton", variable=self.folder_jpg_var).grid(row=0, column=1, sticky="w", padx=4, pady=3)
        ttk.Checkbutton(options, text="Στίχοι .lrc", style="Modern.TCheckbutton", variable=self.lyrics_var).grid(row=1, column=0, sticky="w", padx=4, pady=3)
        ttk.Checkbutton(options, text="Album Info.txt", style="Modern.TCheckbutton", variable=self.info_var).grid(row=1, column=1, sticky="w", padx=4, pady=3)
        ttk.Checkbutton(options, text="Missing Songs.txt", style="Modern.TCheckbutton", variable=self.errors_var).grid(row=2, column=0, sticky="w", padx=4, pady=3)
        ttk.Checkbutton(options, text="Αυτόματη επικόλληση", style="Modern.TCheckbutton", variable=self.auto_paste_var).grid(row=2, column=1, sticky="w", padx=4, pady=3)
        ttk.Label(options, text="Παράλληλες λήψεις", style="Muted.TLabel").grid(row=3, column=0, sticky="w", padx=4, pady=3)
        ttk.Spinbox(options, from_=1, to=8, textvariable=self.threads_var, width=5).grid(row=3, column=1, sticky="w", padx=4, pady=3)

        ttk.Label(right, text="Αυτόματη ονομασία", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            right,
            text="{album-artist} - {album} ({year})",
            style="Card.TLabel",
            wraplength=240,
            justify="left",
        ).pack(anchor="w", pady=(7, 10))
        ttk.Button(right, text="Άνοιγμα βασικού φακέλου", style="Outline.TButton", command=self._open_root).pack(fill="x", pady=(0, 4))

        ttk.Label(right, text="Διορθώσεις", style="Section.TLabel").pack(anchor="w", pady=(18, 8))
        self.selected_track_entry = ttk.Entry(right, textvariable=self.selected_track_var, state="readonly")
        self.selected_track_entry.pack(fill="x", pady=(0, 8))
        ttk.Button(
            right,
            text="▶  Αναζήτηση τραγουδιού στο YouTube",
            style="YouTube.TButton",
            command=self._open_selected_track_youtube_search,
        ).pack(fill="x", pady=(0, 8))
        self.repair_url_entry = ttk.Entry(right, textvariable=self.repair_url_var)
        self.repair_url_entry.pack(fill="x", pady=(0, 6))
        ttk.Button(right, text="Επικόλληση YouTube URL", style="Outline.TButton", command=self._paste_repair_url).pack(fill="x", pady=4)
        auth_row = ttk.Frame(right)
        auth_row.pack(fill="x", pady=4)
        ttk.Label(auth_row, text="Σύνδεση YouTube:").pack(side="left", padx=(0, 6))
        ttk.Combobox(auth_row, textvariable=self.youtube_browser_var, values=list(YOUTUBE_BROWSERS), state="readonly", width=16).pack(side="left", fill="x", expand=True)
        ttk.Button(right, text="Χρήση URL στο επιλεγμένο", style="Outline.TButton", command=self._start_repair_selected_with_url).pack(fill="x", pady=4)
        self.repair_btn = ttk.Button(right, text="Αυτόματη αναζήτηση", style="Primary.TButton", command=self._start_repair_selected)
        self.repair_btn.pack(fill="x", pady=4)

        bind_right_mousewheel(right)
        self._install_entry_bindings(self.url_entry)
        self._install_entry_bindings(self.root_entry)
        self._install_entry_bindings(self.selected_track_entry)
        self._install_entry_bindings(self.repair_url_entry)
        self._install_track_bindings()
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

    def _install_track_bindings(self) -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Αυτόματη διόρθωση", command=self._start_repair_selected)
        menu.add_command(label="Χρήση YouTube URL", command=self._start_repair_selected_with_url)
        menu.add_command(
            label="Άνοιγμα αναζήτησης του τραγουδιού στο YouTube",
            command=self._open_selected_track_youtube_search,
        )
        menu.add_separator()
        menu.add_command(label="Αντιγραφή γραμμής", command=self._copy_selected_track_row)
        menu.add_command(label="Αντιγραφή τίτλου", command=self._copy_selected_track_title)

        def show_menu(event: tk.Event) -> None:
            item_id = self.tracks.identify_row(event.y)
            if item_id:
                self.tracks.selection_set(item_id)
                self.tracks.focus(item_id)
                self._update_repair_panel()
            menu.tk_popup(event.x_root, event.y_root)

        self.tracks.bind("<Button-3>", show_menu, add="+")
        self.tracks.bind("<Control-c>", lambda _event: self._copy_selected_track_row())
        self.tracks.bind("<Control-C>", lambda _event: self._copy_selected_track_title())

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

    def _paste_repair_url(self) -> None:
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            messagebox.showwarning(APP_TITLE, "Το clipboard δεν περιέχει κείμενο.")
            return
        match = re.search(r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[^\s]+", text)
        self.repair_url_var.set(match.group(0) if match else text)

    def _on_url_change(self, *_args: object) -> None:
        if self.suppress_url_reset:
            return
        if self.operation_active:
            if self.active_download_url and self.url_var.get() != self.active_download_url:
                self.suppress_url_reset = True
                try:
                    self.url_var.set(self.active_download_url)
                finally:
                    self.suppress_url_reset = False
                self.status_var.set("Η τρέχουσα λήψη είναι ενεργή — το URL παραμένει κλειδωμένο.")
            return
        self._reset_loaded_album_state()
        url = clean_spotify_url(self.url_var.get())
        if re.search(r"open\.spotify\.com/album/", url):
            self._schedule_cover_preview(url)

    def _clear_url(self) -> None:
        self.url_var.set("")
        self._reset_loaded_album_state()

    def _reset_loaded_album_state(self) -> None:
        self.cover_preview_generation += 1
        if self.cover_preview_job is not None:
            try:
                self.after_cancel(self.cover_preview_job)
            except tk.TclError:
                pass
            self.cover_preview_job = None
        self.preview_cover_url = ""
        self.last_album_dir = None
        self.album_name_var.set("Θα δημιουργηθεί αυτόματα")
        self.selected_track_var.set("Δεν έχει επιλεγεί κομμάτι")
        self.repair_url_var.set("")
        self._clear_tracks()
        if hasattr(self, "cover_label"):
            self.cover_photo = None
            self.cover_label.configure(
                image="",
                text="Επικόλλησε Spotify album URL\nγια άμεση προεπισκόπηση.",
                width=28,
                height=15,
            )

    def _schedule_cover_preview(self, album_url: str) -> None:
        generation = self.cover_preview_generation
        self.cover_label.configure(image="", text="Φόρτωση εξωφύλλου...", width=28, height=15)
        self.cover_preview_job = self.after(
            300,
            lambda: self._start_cover_preview(album_url, generation),
        )

    def _start_cover_preview(self, album_url: str, generation: int) -> None:
        self.cover_preview_job = None
        if generation != self.cover_preview_generation:
            return
        if clean_spotify_url(self.url_var.get()) != album_url:
            return
        threading.Thread(
            target=self._load_cover_preview,
            args=(album_url, generation),
            daemon=True,
        ).start()

    def _load_cover_preview(self, album_url: str, generation: int) -> None:
        try:
            response = requests.get(
                "https://open.spotify.com/oembed",
                params={"url": album_url},
                timeout=(4, 10),
            )
            response.raise_for_status()
            metadata = response.json()
            thumbnail_url = str(metadata.get("thumbnail_url") or "").strip()
            if not thumbnail_url:
                raise ValueError("Το Spotify δεν επέστρεψε εικόνα.")
            image_response = requests.get(thumbnail_url, timeout=(4, 12))
            image_response.raise_for_status()
            if len(image_response.content) > 10 * 1024 * 1024:
                raise ValueError("Το εξώφυλλο είναι υπερβολικά μεγάλο.")
            self.events.put(
                (
                    "cover_preview",
                    (generation, album_url, image_response.content),
                )
            )
        except Exception as exc:
            self.events.put(("cover_preview_error", (generation, album_url, str(exc))))

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
                "duration": song_duration_seconds(song) if song is not None else None,
            })
        return entries

    def _missing_song_matches(self, errors_path: Path, album_url: str) -> list[str]:
        matches: list[str] = []
        entries = self._missing_song_entries(errors_path, album_url)
        for index, entry in enumerate(entries, 1):
            spotify_url = str(entry["spotify_url"])
            expected = str(entry["expected"])
            self.events.put(("track_text", (expected, "Αναζήτηση εναλλακτικής")))
            self.events.put(("status", f"Αναζήτηση εναλλακτικής {index}/{len(entries)}: {expected}"))
            youtube_url, title, _score = self._find_best_youtube_url(
                expected,
                [str(query) for query in entry["queries"]],
                str(entry["direct_url"]) if entry["direct_url"] else None,
                float(entry["duration"]) if entry["duration"] else None,
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
                **hidden_subprocess_options(),
            )
        except Exception:
            return None
        return next((line.strip() for line in result.stdout.splitlines() if line.strip()), None)

    def _youtube_candidates(self, query: str, limit: int = 15) -> list[dict[str, str]]:
        command = [
            *yt_dlp_command(),
            f"ytsearch{limit}:{query}",
            "--dump-json",
            "--no-playlist",
            "--skip-download",
            "--flat-playlist",
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                **hidden_subprocess_options(),
            )
        except Exception:
            return []

        candidates: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            video_id = str(item.get("id") or "")
            title = str(item.get("title") or "")
            if not video_id or not title:
                continue
            webpage_url = str(item.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}")
            candidates.append({
                "title": title,
                "url": webpage_url,
                "channel": str(item.get("channel") or item.get("uploader") or ""),
                "description": str(item.get("description") or ""),
                "duration": str(item.get("duration") or ""),
            })
        return candidates

    def _find_best_youtube_url(
        self,
        expected: str,
        queries: list[str],
        direct_url: str | None = None,
        expected_duration: float | None = None,
    ) -> tuple[str | None, str | None, float]:
        best: tuple[str | None, str | None, float] = (None, None, 0.0)
        seen_urls: set[str] = set()

        if direct_url:
            title = self._youtube_title(direct_url)
            if title:
                score = match_score(expected, title)
                best = (direct_url, title, score)
                seen_urls.add(direct_url)

        for query in queries:
            for candidate in self._youtube_candidates(query):
                title = candidate["title"]
                url = candidate["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                try:
                    candidate_duration = float(candidate.get("duration", "") or 0) or None
                except ValueError:
                    candidate_duration = None
                official_score = youtube_official_score(candidate)
                duration_score = duration_match_score(expected_duration, candidate_duration)
                title_bonus = exact_title_bonus(expected, title)
                score = max(
                    0.0,
                    min(
                        1.0,
                        match_score(expected, title)
                        + title_bonus
                        + official_score
                        + duration_score,
                    ),
                )
                if score > best[2]:
                    best = (url, title, score)
                if score >= 0.98 and official_score >= 0.20 and duration_score >= 0:
                    return best
                if score >= 0.90 and title_bonus >= 0.10 and duration_score >= 0.20:
                    return best

        if best[0] and best[2] >= 0.55:
            return best
        if best[0] and best[1]:
            self.events.put(("log", f"Απορρίφθηκε πιθανό λάθος match: {expected} -> {best[1]}\n"))
        return None, None, 0.0

    def _manual_track_query(self, spotify_url: str, song: object) -> str:
        title = str(getattr(song, "name", "") or "").strip()
        artist = str(getattr(song, "artist", "") or "").strip()
        artists = getattr(song, "artists", [])
        artist_text = ", ".join(str(item) for item in artists) if isinstance(artists, list) else artist
        album = song_album_name(song)
        expected = f"{artist} - {title}".strip(" -") if title else spotify_url
        queries = [
            f"{artist} - {title}".strip(" -"),
            f"{artist_text} - {title}".strip(" -"),
            f"{title} {artist}".strip(),
            f"{title} {artist} official audio".strip(),
            f"{title} {album}".strip(),
            f"{album} {title}".strip(),
            f"{title} {album} low bap".strip(),
            f"{title} low bap".strip(),
            title,
        ]

        youtube_url, youtube_title, score = self._find_best_youtube_url(
            expected,
            [item for item in dict.fromkeys(queries) if item],
            expected_duration=song_duration_seconds(song),
        )
        required_score = 0.60 if is_risky_manual_title(title) else 0.70
        if youtube_url and score >= required_score:
            self.events.put(("log", f"Βρέθηκε ακριβές YouTube match: {youtube_title or youtube_url}\n"))
            return f"{youtube_url}|{spotify_url}"

        if youtube_title:
            self.events.put(("log", f"Δεν χρησιμοποιήθηκε αβέβαιο YouTube match: {youtube_title}\n"))
        return spotify_url

    def _album_download_queries(self, album_url: str, songs: list[object]) -> list[str]:
        if not songs:
            return [album_url]

        queries: list[str] = []
        for song in songs:
            title = str(getattr(song, "name", "") or "").strip()
            song_url = str(getattr(song, "url", "") or "").strip()
            if title and song_url and is_risky_manual_title(title):
                queries.append(self._manual_track_query(song_url, song))
            elif song_url:
                queries.append(song_url)
        return queries or [album_url]

    def _manual_download_queries(self, spotify_url: str, songs: list[object]) -> list[str]:
        if not songs:
            return [spotify_url]

        queries: list[str] = []
        for song in songs:
            song_url = str(getattr(song, "url", "") or "").strip()
            queries.append(self._manual_track_query(song_url or spotify_url, song))
        return queries

    def _download_threads(self) -> int:
        try:
            threads = int(self.threads_var.get())
        except (tk.TclError, ValueError):
            threads = 4
        threads = max(1, min(8, threads))
        self.threads_var.set(threads)
        return threads

    def _retry_missing_songs(self, errors_path: Path, album_url: str, output_template: str) -> bool:
        max_attempts = 3
        had_errors = errors_path.exists() and errors_path.stat().st_size > 0

        # YouTube can reject its preferred audio-only stream with HTTP 403 even
        # though the compatible Android combined stream is available. Retry the
        # original Spotify tracks once with that client before doing slower,
        # title-based manual matching for every missing song.
        missing_entries = self._missing_song_entries(errors_path, album_url)
        fallback_queries = [str(entry["spotify_url"]) for entry in missing_entries]
        if fallback_queries:
            fallback_errors = errors_path.with_name("_spotdl_compat_missing_songs.txt")
            if fallback_errors.exists():
                fallback_errors.unlink()

            for entry in missing_entries:
                self.events.put(("track_text", (str(entry["expected"]), "Εναλλακτική λήψη")))
            self.events.put((
                "status",
                f"Εναλλακτική λήψη για {len(fallback_queries)} κομμάτια...",
            ))
            self.events.put((
                "log",
                f"\nΔοκιμή συμβατής λήψης για {len(fallback_queries)} χαμένα κομμάτια...\n",
            ))

            fallback_command = [
                *spotdl_command(), "download", *fallback_queries,
                "--audio", "youtube-music", "youtube",
                "--output", output_template,
                "--format", self.format_var.get(),
                "--bitrate", self.bitrate_var.get(),
                "--threads", str(self._download_threads()),
                "--max-retries", "2",
                "--yt-dlp-args", YT_DLP_COMPAT_ARGS,
                "--search-query", "{artist} - {title}",
                "--max-filename-length", "160",
                "--print-errors",
                "--save-errors", str(fallback_errors),
            ]
            self.process = subprocess.Popen(
                fallback_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=spotdl_environment(),
                **hidden_subprocess_options(),
            )
            assert self.process.stdout is not None
            fallback_errors_detected = False
            for line in self.process.stdout:
                if "LookupError:" in line or "AudioProviderError:" in line:
                    fallback_errors_detected = True
                self._update_tracks_from_raw_line(line)
                log_line = concise_spotdl_log(line)
                if log_line:
                    self.events.put(("log", log_line))
            fallback_code = self.process.wait()
            self.process = None

            if fallback_errors.exists() and fallback_errors.stat().st_size > 0:
                errors_path.write_text(
                    fallback_errors.read_text(encoding="utf-8", errors="replace"),
                    encoding="utf-8",
                )
                fallback_errors.unlink()
                had_errors = True
            else:
                if fallback_errors.exists():
                    fallback_errors.unlink()
                if fallback_code == 0 and not fallback_errors_detected:
                    if errors_path.exists():
                        errors_path.unlink()
                    return False

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
                "--bitrate", self.bitrate_var.get(),
                "--threads", str(self._download_threads()),
                "--max-retries", "2",
                "--yt-dlp-args", YT_DLP_COMPAT_ARGS,
                "--max-filename-length", "160",
                "--print-errors",
                "--save-errors", str(manual_errors),
            ]
            self.events.put(("log", f"\nΑυτόματη προσπάθεια {attempt}/{max_attempts} για {len(matches)} χαμένα κομμάτια...\n"))

            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=spotdl_environment(),
                **hidden_subprocess_options(),
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

    def _reconcile_album_track_results(
        self,
        album_dir: Path,
        songs: list[object],
        create_errors: bool,
    ) -> list[object]:
        """Make the UI and Missing Songs file reflect the files actually present."""
        present = album_audio_positions(album_dir)
        missing: list[object] = []
        states: dict[tuple[int, int], str] = {}
        for song in songs:
            position = (
                int(getattr(song, "disc_number", 1) or 1),
                int(getattr(song, "track_number", 0) or 0),
            )
            if position[1] <= 0:
                continue
            if position in present:
                states[position] = "✓ ΟΚ"
            else:
                states[position] = "Δεν βρέθηκε"
                missing.append(song)
        self.events.put(("track_results", states))

        errors_path = album_dir / "Missing Songs.txt"
        if missing and create_errors:
            lines = ["Δεν βρέθηκαν τα παρακάτω κομμάτια:", ""]
            for song in missing:
                disc_number = int(getattr(song, "disc_number", 1) or 1)
                track_number = int(getattr(song, "track_number", 0) or 0)
                disc_count = int(getattr(song, "disc_count", 1) or 1)
                number = f"{disc_number}-{track_number}" if disc_count > 1 else str(track_number)
                artist = str(getattr(song, "artist", "") or "")
                title = str(getattr(song, "name", "") or "")
                spotify_url = str(getattr(song, "url", "") or "")
                lines.append(f"#{number} | {artist} - {title}".rstrip(" -"))
                if spotify_url:
                    lines.append(spotify_url)
                lines.append("")
            errors_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8-sig")
        elif errors_path.exists():
            errors_path.unlink()
        return missing

    def _watch_clipboard(self) -> None:
        if self.auto_paste_var.get() and not self.operation_active:
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
        self.status_var.set("Έτοιμο")
        if hasattr(self, "progress"):
            self.progress.stop()
            self.progress.configure(value=0)

    def _clear_tracks(self) -> None:
        self.track_rows.clear()
        self.track_lookup.clear()
        self.track_songs.clear()
        self.selected_track_var.set("Δεν έχει επιλεγεί κομμάτι")
        if hasattr(self, "tracks"):
            children = self.tracks.get_children()
            if children:
                self.tracks.delete(*children)

    def _clear_tracks_from_button(self) -> None:
        if self.operation_active:
            messagebox.showwarning(APP_TITLE, "Περίμενε να ολοκληρωθεί η τρέχουσα διαδικασία.")
            return
        self._clear_tracks()
        self.repair_url_var.set("")
        self.status_var.set("Η tracklist καθαρίστηκε")

    def _scroll_tracks(self, event: tk.Event) -> str:
        self.tracks.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def _track_display_title(self, song: object) -> str:
        return str(getattr(song, "name", "") or "Άγνωστος τίτλος")

    def _load_track_rows(self, songs: list[object]) -> None:
        self._clear_tracks()
        for index, song in enumerate(songs, 1):
            disc_number = int(getattr(song, "disc_number", 1) or 1)
            track_number = int(getattr(song, "track_number", index) or index)
            display_number = str(track_number) if int(getattr(song, "disc_count", 1) or 1) == 1 else f"{disc_number}-{track_number}"
            title = self._track_display_title(song)
            artist = str(getattr(song, "artist", "") or "Άγνωστος καλλιτέχνης")
            item_id = f"track_{index}"
            self.tracks.insert(
                "",
                "end",
                iid=item_id,
                values=(display_number, artist, title, "Αναμονή"),
                tags=("waiting",),
            )
            self.track_rows[item_id] = {"state": "Αναμονή", "artist": artist, "title": title}
            self.track_songs[item_id] = song
            song_title = str(getattr(song, "name", "") or "")
            for key in (song_title, f"{artist} - {song_title}", f"{song_title} - {artist}", title):
                normalized = normalize_match_text(key)
                if normalized:
                    self.track_lookup.append((normalized, item_id))

    @staticmethod
    def _track_state_tag(state: str) -> str:
        normalized = state.casefold()
        if "σφάλμα" in normalized or "απέτυχ" in normalized:
            return "error"
        if "εκκρεμ" in normalized:
            return "warning"
        if "οκ" in normalized or "✓" in state or "ολοκληρ" in normalized:
            return "success"
        if "κατεβα" in normalized or "διόρθ" in normalized or "λήψη" in normalized or "αναζήτη" in normalized:
            return "active"
        return "waiting"

    def _set_track_state(self, item_id: str, state: str) -> None:
        if item_id not in self.track_rows:
            return
        values = list(self.tracks.item(item_id, "values"))
        if len(values) >= 4:
            values[3] = state
            self.tracks.item(item_id, values=values, tags=(self._track_state_tag(state),))
            self.track_rows[item_id]["state"] = state
            if item_id in self.tracks.selection():
                self._update_repair_panel()

    def _update_repair_panel(self) -> None:
        if not hasattr(self, "tracks"):
            return
        selection = self.tracks.selection()
        if not selection:
            self.selected_track_var.set("Δεν έχει επιλεγεί κομμάτι")
            return
        values = self.tracks.item(selection[0], "values")
        if len(values) >= 4:
            self.selected_track_var.set(f"#{values[0]} | {values[1]} | {values[2]} | {values[3]}")
        else:
            self.selected_track_var.set("Δεν έχει επιλεγεί κομμάτι")

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

    def _apply_track_results(self, states: dict[tuple[int, int], str]) -> None:
        for item_id, song in self.track_songs.items():
            position = (
                int(getattr(song, "disc_number", 1) or 1),
                int(getattr(song, "track_number", 0) or 0),
            )
            state = states.get(position)
            if state:
                self._set_track_state(item_id, state)

    def _selected_track_title(self) -> str:
        selection = self.tracks.selection() if hasattr(self, "tracks") else ()
        if not selection:
            return ""
        values = self.tracks.item(selection[0], "values")
        if len(values) < 3:
            return ""
        artist = str(values[1])
        title = str(values[2])
        return f"{title} - {artist}" if artist else title

    def _selected_track_row_text(self) -> str:
        selection = self.tracks.selection() if hasattr(self, "tracks") else ()
        if not selection:
            return ""
        values = self.tracks.item(selection[0], "values")
        if len(values) >= 4:
            return "\t".join(str(value) for value in values[:4])
        return ""

    def _copy_selected_track_row(self) -> str:
        text = self._selected_track_row_text()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.status_var.set("Αντιγράφηκε η γραμμή κομματιού")
        return "break"

    def _copy_selected_track_title(self) -> None:
        title = self._selected_track_title()
        if not title:
            return
        self.clipboard_clear()
        self.clipboard_append(title)
        self.status_var.set("Αντιγράφηκε ο τίτλος κομματιού")

    def _open_selected_track_youtube_search(self) -> None:
        title = self._selected_track_title()
        if not title:
            messagebox.showwarning(APP_TITLE, "Επίλεξε πρώτα ένα κομμάτι.")
            return
        webbrowser.open(f"https://www.youtube.com/results?search_query={quote_plus(title)}")
        self.status_var.set("Άνοιξε η αναζήτηση του επιλεγμένου τραγουδιού στο YouTube")

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
                elif kind == "track_results":
                    self._apply_track_results(payload)
                elif kind == "name":
                    self.album_name_var.set(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "cover_preview":
                    generation, album_url, image_data = payload
                    if (
                        int(generation) == self.cover_preview_generation
                        and clean_spotify_url(self.url_var.get()) == str(album_url)
                    ):
                        self._show_cover_bytes(image_data)
                        self.preview_cover_url = str(album_url)
                elif kind == "cover_preview_error":
                    generation, album_url, error_text = payload
                    if (
                        int(generation) == self.cover_preview_generation
                        and clean_spotify_url(self.url_var.get()) == str(album_url)
                    ):
                        self.cover_label.configure(
                            image="",
                            text="Δεν ήταν δυνατή η άμεση\nφόρτωση εξωφύλλου.",
                            width=28,
                            height=15,
                        )
                        self._append_log(f"Αδυναμία άμεσης φόρτωσης εξωφύλλου: {error_text}\n")
                elif kind == "cover":
                    self._show_cover(Path(payload))
                elif kind == "done":
                    self._finish(int(payload))
                elif kind == "repair_done":
                    item_id, code, started_at = payload
                    self._finish_repair(str(item_id), int(code), float(started_at))
                elif kind == "error":
                    self._append_log(f"\nΣΦΑΛΜΑ: {payload}\n")
                    self._finish(1)
        except queue.Empty:
            pass
        self.after(120, self._drain_events)

    def _start(self) -> None:
        if self.operation_active:
            messagebox.showwarning(APP_TITLE, "Περίμενε να τελειώσει η τρέχουσα λήψη.")
            return
        url = clean_spotify_url(self.url_var.get())
        self.suppress_url_reset = True
        try:
            self.url_var.set(url)
        finally:
            self.suppress_url_reset = False
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
        audio_format = self.format_var.get()
        download_threads = self._download_threads()
        generate_lrc = self.lyrics_var.get()
        create_cover = self.cover_var.get()
        create_folder_jpg = self.folder_jpg_var.get()
        create_info = self.info_var.get()
        create_errors = self.errors_var.get()
        self.operation_active = True
        self.active_download_url = url
        self.last_album_dir = None
        self._clear_tracks()
        self._clear_progress_log()
        self.download_started_at = time.monotonic()
        self.album_name_var.set("Ανάγνωση στοιχείων από το Spotify...")
        self.status_var.set("Η λήψη ξεκίνησε...")
        self.download_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.repair_btn.configure(state="disabled")
        self.progress.start(10)
        self._append_log(f"\n=== Νέα λήψη ===\nΒασικός φάκελος: {root}\n")
        threading.Thread(
            target=self._run_download,
            args=(
                url,
                root,
                audio_format,
                download_threads,
                generate_lrc,
                create_cover,
                create_folder_jpg,
                create_info,
                create_errors,
            ),
            daemon=True,
        ).start()

    def _start_repair_selected(self) -> None:
        self._start_repair_selected_impl(None)

    def _start_repair_selected_with_url(self) -> None:
        youtube_url = self.repair_url_var.get().strip()
        match = re.search(r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[^\s]+", youtube_url)
        if not match:
            messagebox.showwarning(APP_TITLE, "Επικόλλησε πρώτα ένα έγκυρο YouTube URL.")
            return
        self._start_repair_selected_impl(match.group(0))

    def _start_repair_selected_impl(self, youtube_url: str | None) -> None:
        if self.operation_active:
            messagebox.showwarning(APP_TITLE, "Περίμενε να τελειώσει η τρέχουσα λήψη.")
            return
        selection = self.tracks.selection()
        if not selection:
            messagebox.showwarning(APP_TITLE, "Επίλεξε πρώτα ένα κομμάτι από την tracklist.")
            return
        item_id = selection[0]
        song = self.track_songs.get(item_id)
        if song is None:
            messagebox.showwarning(APP_TITLE, "Δεν υπάρχουν στοιχεία Spotify για αυτό το κομμάτι.")
            return

        started_at = time.monotonic()
        self.operation_active = True
        self.active_download_url = clean_spotify_url(self.url_var.get())
        self._set_track_state(item_id, "Διόρθωση")
        self.status_var.set("Διόρθωση επιλεγμένου κομματιού...")
        self.download_btn.configure(state="disabled")
        self.repair_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.start(10)
        threading.Thread(
            target=self._run_repair_selected,
            args=(item_id, song, started_at, youtube_url, self.errors_var.get(),
                  YOUTUBE_BROWSERS.get(self.youtube_browser_var.get()) if youtube_url else None,
                  self.bitrate_var.get()),
            daemon=True,
        ).start()

    def _run_repair_selected(
        self,
        item_id: str,
        song: object,
        started_at: float,
        youtube_url: str | None = None,
        create_errors: bool = True,
        youtube_browser: str | None = None,
        bitrate: str = "320k",
    ) -> None:
        code = 1
        try:
            spotify_url = str(getattr(song, "url", "") or "").strip()
            if not spotify_url:
                self.events.put(("log", "Δεν βρέθηκε Spotify URL για το επιλεγμένο κομμάτι.\n"))
                return

            matched_query = f"{youtube_url}|{spotify_url}" if youtube_url else self._manual_track_query(spotify_url, song)
            if "|" not in matched_query:
                self.events.put(("log", "Δεν βρέθηκε αρκετά αξιόπιστο YouTube match για διόρθωση.\n"))
                return

            root = Path(self.root_var.get()).expanduser()
            album_url = clean_spotify_url(self.url_var.get())
            if "/album/" in album_url:
                output_template = str(root / "{album-artist} - {album} ({year})" / "{track-number}.{title}.{output-ext}")
            else:
                output_template = str(root / "Tracks" / "{artists} - {title}.{output-ext}")

            command = [
                *spotdl_command(), "download", matched_query,
                "--audio", "youtube",
                "--output", output_template,
                "--format", self.format_var.get(),
                "--bitrate", bitrate,
                "--threads", "1",
                "--max-retries", "2",
                "--yt-dlp-args", repair_ytdlp_args(youtube_browser),
                "--overwrite", "force",
                "--max-filename-length", "160",
                "--print-errors",
            ]
            if youtube_url:
                self.events.put(("log", f"\nΔιόρθωση με χειροκίνητο YouTube URL: {youtube_url}\n"))
            else:
                self.events.put(("log", "\nΔιόρθωση επιλεγμένου κομματιού...\n"))
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=spotdl_environment(),
                **hidden_subprocess_options(),
            )
            assert self.process.stdout is not None
            download_failed = False
            for line in self.process.stdout:
                if "ERROR:" in line or "AudioProviderError:" in line:
                    download_failed = True
                self._update_tracks_from_raw_line(line)
                log_line = concise_spotdl_log(line)
                if log_line:
                    self.events.put(("log", log_line))
            code = self.process.wait()
            if download_failed:
                code = 1
            self.process = None
            if code == 0 and self.last_album_dir and "/album/" in album_url:
                self._organize_album_from_spotify(self.last_album_dir, album_url)
                songs = list(self._album_track_map(album_url).values())
                if songs:
                    self._reconcile_album_track_results(self.last_album_dir, songs, create_errors)
                    selected_position = (
                        int(getattr(song, "disc_number", 1) or 1),
                        int(getattr(song, "track_number", 0) or 0),
                    )
                    if selected_position not in album_audio_positions(self.last_album_dir):
                        code = 1
        except FileNotFoundError:
            self.events.put(("log", "Δεν βρέθηκε το spotdl. Τρέξε πρώτα το Install or Update.bat.\n"))
        except Exception as exc:
            self.events.put(("log", f"ΣΦΑΛΜΑ διόρθωσης: {exc}\n"))
        finally:
            self.process = None
            self.events.put(("repair_done", (item_id, code, started_at)))

    def _run_download(
        self,
        url: str,
        root: Path,
        audio_format: str,
        download_threads: int,
        generate_lrc: bool,
        create_cover: bool,
        create_folder_jpg: bool,
        create_info: bool,
        create_errors: bool,
    ) -> None:
        try:
            before = {p for p in root.iterdir() if p.is_dir()}
            before_audio = {
                audio_path.resolve(): (audio_path.stat().st_size, audio_path.stat().st_mtime_ns)
                for folder in before
                for audio_path in audio_files_in(folder)
            }

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
                output_template = str(root / "{album-artist} - {album} ({year})" / "{track-number}.{title}.{output-ext}")
                download_queries = [url]
                audio_providers = ["youtube-music", "youtube"]
            else:
                output_template = str(root / "Tracks" / "{artists} - {title}.{output-ext}")
                download_queries = self._manual_download_queries(url, songs)
                audio_providers = ["youtube"] if any("|" in query for query in download_queries) else ["youtube-music", "youtube"]
            temp_errors = root / "_spotdl_missing_songs.txt"
            if temp_errors.exists():
                temp_errors.unlink()

            command = [
                *spotdl_command(), "download", *download_queries,
                "--audio", *audio_providers,
                "--output", output_template,
                "--format", audio_format,
                "--bitrate", self.bitrate_var.get(),
                "--threads", str(download_threads),
                "--max-retries", "2",
                "--yt-dlp-args", YT_DLP_COMPAT_ARGS,
                "--search-query", "{artist} - {title}",
                "--max-filename-length", "160",
                "--print-errors",
                "--save-errors", str(temp_errors),
            ]
            if generate_lrc:
                command.append("--generate-lrc")

            self.events.put(("log", "Ξεκίνησε η λήψη.\n"))

            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=spotdl_environment(),
                **hidden_subprocess_options(),
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
                album_dir = select_album_output_dir(after, set(candidates), before_audio, songs)

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

                if is_album and songs:
                    missing_songs = self._reconcile_album_track_results(album_dir, songs, create_errors)
                    errors_detected = errors_detected or bool(missing_songs)
                    verified_count = len(songs) - len(missing_songs)
                    self.events.put(("log", f"Επαλήθευση αρχείων: {verified_count}/{len(songs)} κομμάτια υπάρχουν.\n"))

                if is_album and create_cover:
                    cover = extract_cover(album_dir, create_folder_jpg)
                    if cover:
                        self.events.put(("cover", str(cover)))
                        self.events.put(("log", "Δημιουργήθηκε Cover.jpg.\n"))

                if is_album and create_info:
                    if create_album_info(album_dir, url):
                        self.events.put(("log", "Δημιουργήθηκε Album Info.txt.\n"))
            else:
                self.events.put(("name", "Δεν δημιουργήθηκε φάκελος"))
                self.events.put(("status", "Η διαδικασία ολοκληρώθηκε χωρίς αρχεία ήχου"))
                if is_album and songs:
                    missing_states = {
                        (
                            int(getattr(song, "disc_number", 1) or 1),
                            int(getattr(song, "track_number", 0) or 0),
                        ): "Δεν βρέθηκε"
                        for song in songs
                        if int(getattr(song, "track_number", 0) or 0) > 0
                    }
                    self.events.put(("track_results", missing_states))
                    errors_detected = True

            if code == 0 and errors_detected:
                code = 1
            self.events.put(("done", str(code)))
        except FileNotFoundError:
            self.events.put(("error", "Δεν βρέθηκε το spotdl. Τρέξε πρώτα το Install or Update.bat."))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _finish(self, code: int) -> None:
        self.operation_active = False
        self.active_download_url = ""
        self.progress.stop()
        self.download_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.repair_btn.configure(state="normal")
        elapsed_text = ""
        if self.download_started_at is not None:
            elapsed_text = format_elapsed(time.monotonic() - self.download_started_at)
            self.download_started_at = None

        if code == 0:
            for item_id, row in list(self.track_rows.items()):
                if row.get("state") in {"Αναμονή", "Κατεβαίνει"}:
                    self._set_track_state(item_id, "Δεν επιβεβαιώθηκε")
            self._append_log("\nΗ διαδικασία ολοκληρώθηκε.\n")
            if elapsed_text:
                self._append_log(f"Χρόνος λήψης: {elapsed_text}\n")
            self._show_done_dialog()
        else:
            for item_id, row in list(self.track_rows.items()):
                if row.get("state") in {"Αναμονή", "Κατεβαίνει"}:
                    self._set_track_state(item_id, "Εκκρεμεί")
            self.status_var.set("Ολοκληρώθηκε με σφάλματα ή ελλείψεις")
            self._append_log(f"\nΗ διαδικασία τερματίστηκε με κωδικό {code}.\n")
            if elapsed_text:
                self._append_log(f"Χρόνος λήψης: {elapsed_text}\n")
            self._show_error_dialog()

    def _finish_repair(self, item_id: str, code: int, started_at: float) -> None:
        self.operation_active = False
        self.active_download_url = ""
        self.progress.stop()
        self.download_btn.configure(state="normal")
        self.repair_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        elapsed_text = format_elapsed(time.monotonic() - started_at)
        if code == 0:
            self._set_track_state(item_id, "✓ ΟΚ")
            self.status_var.set(f"Η διόρθωση ολοκληρώθηκε σε {elapsed_text}")
            self._append_log(f"Η διόρθωση ολοκληρώθηκε. Χρόνος: {elapsed_text}\n")
        else:
            self._set_track_state(item_id, "Σφάλμα")
            self.status_var.set("Η διόρθωση δεν ολοκληρώθηκε")
            self._append_log(f"Η διόρθωση απέτυχε. Χρόνος: {elapsed_text}\n")

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
            with Image.open(path) as image:
                self._display_cover_image(image.copy())
        except Exception as exc:
            self._append_log(f"Αδυναμία εμφάνισης εξωφύλλου: {exc}\n")

    def _show_cover_bytes(self, image_data: bytes) -> None:
        try:
            with Image.open(BytesIO(image_data)) as image:
                self._display_cover_image(image.copy())
        except Exception as exc:
            self._append_log(f"Αδυναμία εμφάνισης άμεσου εξωφύλλου: {exc}\n")

    def _display_cover_image(self, image: Image.Image) -> None:
        image.thumbnail((265, 265))
        self.cover_photo = ImageTk.PhotoImage(image)
        self.cover_label.configure(image=self.cover_photo, text="", width=265, height=265)

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
        self.bitrate_var.set(data.get("bitrate", self.bitrate_var.get()))
        self.cover_var.set(data.get("cover", True))
        self.folder_jpg_var.set(data.get("folder_jpg", True))
        self.lyrics_var.set(data.get("lyrics", True))
        self.info_var.set(data.get("info", True))
        self.errors_var.set(data.get("errors", True))
        self.auto_paste_var.set(data.get("auto_paste", True))
        try:
            self.threads_var.set(max(1, min(8, int(data.get("threads", self.threads_var.get())))))
        except (tk.TclError, TypeError, ValueError):
            self.threads_var.set(4)

    def _save_settings(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(
                {
                    "root": self.root_var.get(),
                    "format": self.format_var.get(),
                    "bitrate": self.bitrate_var.get(),
                    "cover": self.cover_var.get(),
                    "folder_jpg": self.folder_jpg_var.get(),
                    "lyrics": self.lyrics_var.get(),
                    "info": self.info_var.get(),
                    "errors": self.errors_var.get(),
                    "auto_paste": self.auto_paste_var.get(),
                    "threads": self._download_threads(),
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
