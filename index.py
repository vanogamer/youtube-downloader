import subprocess
import sys
import os
import threading
import queue
import re
import difflib
import shutil
import unicodedata
import importlib
import json
import time
import logging
from logging.handlers import RotatingFileHandler
import traceback

# ============================
# ლოგირება — logs/ ფოლდერი, აბსოლუტურად ყველა პრობლემის ჩანაწერი
# ============================
# ეს ფოლდერი ინახავს ყველა warning/error-ს (download loop-ის შეცდომები, yt-dlp-ის
# საკუთარი შიდა შეტყობინებები quiet-ის მიუხედავად, დაუჭერელი გამონაკლისები
# background thread-ებსა და Tkinter callback-ებში).
LOGS_DIR_NAME = "logs"


def get_logs_root():
    """logs ფოლდერი ყოველთვის იქმნება უშუალოდ ამ .py ფაილის გვერდით."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    logs_root = os.path.join(script_dir, LOGS_DIR_NAME)
    os.makedirs(logs_root, exist_ok=True)
    return logs_root


def _build_app_logger():
    """ცენტრალური logger: absolute ყველაფერი (debug→error) იწერება logs/app.log-ში;
    კონსოლზე მხოლოდ warning+ ჩანს, რომ ტერმინალი spam-ით არ გავსებულიყო."""
    logger = logging.getLogger('ytdlp_gui')
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if logger.handlers:
        return logger

    try:
        log_path = os.path.join(get_logs_root(), 'app.log')
        file_handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(threadName)s: %(message)s'))
        logger.addHandler(file_handler)
    except Exception:
        # თუ ფაილში ჩაწერა ვერ ხერხდება (მაგ. read-only საქაღალდე), აპლიკაცია მაინც უნდა იმუშაოს.
        pass

    try:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.WARNING)
        console_handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
        logger.addHandler(console_handler)
    except Exception:
        pass

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    return logger


APP_LOGGER = _build_app_logger()


class _LoggingStreamTee:
    """print()-ის ყველა ხაზი (status/debug ტექსტები, download loop-ებში გაბნეული
    'შეცდომა: ...' სტრიქონები და ა.შ.) ასევე logs/app.log-ში იწერება, კონსოლის
    ჩვეულებრივი ქცევის დაურღვევლად."""

    def __init__(self, original_stream, log_func):
        self._original_stream = original_stream
        self._log_func = log_func
        self._buffer = ''

    def write(self, text):
        try:
            self._original_stream.write(text)
        except Exception:
            pass
        self._buffer += text
        while '\n' in self._buffer:
            line, self._buffer = self._buffer.split('\n', 1)
            if line.strip():
                try:
                    self._log_func(line)
                except Exception:
                    pass

    def flush(self):
        try:
            self._original_stream.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self._original_stream.isatty()
        except Exception:
            return False


def install_stdio_logging_tee():
    """sys.stdout/stderr-ს ისე ცვლის, რომ ყველა print() ასევე logs/app.log-ში ჩაიწეროს."""
    try:
        if not isinstance(sys.stdout, _LoggingStreamTee):
            sys.stdout = _LoggingStreamTee(sys.stdout, lambda line: APP_LOGGER.info(line))
        if not isinstance(sys.stderr, _LoggingStreamTee):
            sys.stderr = _LoggingStreamTee(sys.stderr, lambda line: APP_LOGGER.error(line))
    except Exception:
        pass


def _log_uncaught_exception(exc_type, exc_value, exc_tb):
    try:
        APP_LOGGER.error('დაუჭერელი გამონაკლისი (მთავარი thread)', exc_info=(exc_type, exc_value, exc_tb))
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _log_thread_exception(args):
    try:
        thread_name = args.thread.name if args.thread else '?'
        APP_LOGGER.error(
            f'დაუჭერელი გამონაკლისი thread-ში: {thread_name}',
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
    except Exception:
        pass


def _tk_report_callback_exception(_self, exc_type, exc_value, exc_tb):
    try:
        APP_LOGGER.error('გამონაკლისი Tkinter callback-ში', exc_info=(exc_type, exc_value, exc_tb))
    except Exception:
        pass
    try:
        traceback.print_exception(exc_type, exc_value, exc_tb)
    except Exception:
        pass


def install_global_exception_logging():
    """დაუჭერელი გამონაკლისები — მთავარ thread-ში, background thread-ებში და
    Tkinter callback-ებში — ყველგან logs/app.log-ში აისახება."""
    try:
        sys.excepthook = _log_uncaught_exception
    except Exception:
        pass
    try:
        threading.excepthook = _log_thread_exception
    except Exception:
        pass
    try:
        tk.Tk.report_callback_exception = _tk_report_callback_exception
    except Exception:
        pass


def log_download_problem(error, context=''):
    """ჩამოტვირთვის/დამუშავების ნებისმიერი პრობლემის ცენტრალური ლოგირება logs/app.log-ში —
    სრული stack trace-ით, თუ error გამონაკლისის obj-ია."""
    try:
        prefix = f'{context}: ' if context else ''
        if isinstance(error, BaseException):
            APP_LOGGER.error(f'{prefix}ჩამოტვირთვის შეცდომა', exc_info=error)
        else:
            APP_LOGGER.error(f'{prefix}ჩამოტვირთვის შეცდომა: {error}')
    except Exception:
        pass


# ============================
# FFmpeg — explicit Windows path
# ============================
# yt-dlp-ს FFmpeg/FFprobe პირდაპირ მივუთითოთ, რათა Windows-ის PATH-ში
# არსებული სხვა FFmpeg-ის ვერსიაზე დამოკიდებული არ იყოს.
FFMPEG_DIR = os.path.abspath(os.environ.get("YTDLP_FFMPEG_DIR", r"C:\ffmpeg\bin"))
FFMPEG_EXE = os.path.join(FFMPEG_DIR, "ffmpeg.exe")
FFPROBE_EXE = os.path.join(FFMPEG_DIR, "ffprobe.exe")


def get_ffmpeg_location():
    """დააბრუნე მოქმედი FFmpeg საქაღალდე; პრიორიტეტი აქვს C:\ffmpeg\bin-ს."""
    if os.path.isfile(FFMPEG_EXE) and os.path.isfile(FFPROBE_EXE):
        return FFMPEG_DIR

    # Fallback: თუ მომხმარებელმა მომავალში FFmpeg-ის ადგილი შეცვალა,
    # პროგრამამ PATH-იდანაც შეძლოს მისი პოვნა.
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path and ffprobe_path:
        return os.path.dirname(os.path.abspath(ffmpeg_path))

    return None
import socket
import hashlib
import html
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote_plus
from typing import Any
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

install_stdio_logging_tee()
install_global_exception_logging()

# ============================
# პროგრამის გვერდით შენახული JSON ისტორია
# ============================

DOWNLOAD_HISTORY_DIR_NAME = "download_history"


def get_download_history_root():
    """JSON ისტორიების ფოლდერი ყოველთვის იქმნება უშუალოდ ამ .py ფაილის გვერდით."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    history_root = os.path.join(script_dir, DOWNLOAD_HISTORY_DIR_NAME)
    os.makedirs(history_root, exist_ok=True)
    return history_root


# ============================
# ენის პარამეტრი — თავიდან ინგლისური, გადართვა ქართულზეც შესაძლებელი,
# არჩევანი ყოველთვის საიმედოდ (ატომური ჩაწერით) ინახება
# ============================
SETTINGS_DIR_NAME = "settings"
SETTINGS_FILE_NAME = "app_settings.json"
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("en", "ka")


def get_settings_root():
    """settings ფოლდერი ყოველთვის იქმნება უშუალოდ ამ .py ფაილის გვერდით."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    settings_root = os.path.join(script_dir, SETTINGS_DIR_NAME)
    os.makedirs(settings_root, exist_ok=True)
    return settings_root


def get_settings_path():
    return os.path.join(get_settings_root(), SETTINGS_FILE_NAME)


def load_app_settings():
    """უსაფრთხოდ კითხულობს settings/app_settings.json-ს; დაზიანების/არქონის დროს ცარიელ dict-ს აბრუნებს."""
    try:
        with open(get_settings_path(), 'r', encoding='utf-8') as file_obj:
            data = json.load(file_obj)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_app_settings(data):
    """ატომური ჩაწერა (tmp ფაილი + os.replace), რომ პარამეტრი (მაგ. ენა) არასდროს დაზიანდეს
    ან ნახევრად ჩაწერილი არ დარჩეს, თუნდაც პროგრამა ამ დროს დაიხუროს."""
    path = get_settings_path()
    tmp_path = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp_path, 'w', encoding='utf-8', newline='\n') as file_obj:
            json.dump(data, file_obj, ensure_ascii=False, indent=2)
            file_obj.write('\n')
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(tmp_path, path)
        return True
    except Exception as exc:
        log_internal_error('save_app_settings', exc)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


_APP_SETTINGS = load_app_settings()
_saved_language = _APP_SETTINGS.get('language')
CURRENT_LANGUAGE = _saved_language if _saved_language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE

# გვერდითი ეფექტების გარეშე ტექსტების ლექსიკონი. ინგლისური default-ია; ქართული — ალტერნატივა.
TRANSLATIONS = {
    'en': {
        'app_title': 'YouTube Downloader Pro - MAX Quality',
        'main_title': 'YouTube Downloader',
        'main_subtitle': 'Maximum Quality — 4K/8K/HDR',
        'smart_cookies_status': '●  Smart Cookies — standby mode · activates only when authorization is required',
        'menu_video_title': 'Download Video',
        'menu_video_desc': 'Download a single video or playlist',
        'menu_channel_title': 'Channel Videos',
        'menu_channel_desc': 'All channel videos with date filtering',
        'menu_shorts_title': 'Shorts Videos',
        'menu_shorts_desc': 'Find and download channel Shorts — named: number + title',
        'menu_channel_shorts_title': 'Channel + Shorts',
        'menu_channel_shorts_desc': 'Find and download both regular channel videos and Shorts together',
        'menu_playlists_title': 'Playlists',
        'menu_playlists_desc': 'Find and download channel playlists',
        'menu_bulk_title': 'Multiple Links at Once',
        'menu_bulk_desc': 'Start several video links at once — each at its own maximum quality',
        'footer_text': 'yt-dlp  •  MAX Quality  •  Protected cookie fallback',
        'language_switch_label': 'Language:',
        'window_title_video': 'Video Download - MAX Quality',
        'window_title_channel': 'Channel Videos - MAX Quality',
        'window_title_shorts': 'Shorts Videos - MAX Quality',
        'window_title_channel_shorts': 'Channel + Shorts - MAX Quality',
        'window_title_playlists': 'Playlists - MAX Quality',
        'window_title_bulk': 'Multiple Links - MAX Quality',
    },
    'ka': {
        'app_title': 'YouTube Downloader Pro - MAX Quality',
        'main_title': 'YouTube Downloader',
        'main_subtitle': 'მაქსიმალური ხარისხი — 4K/8K/HDR',
        'smart_cookies_status': '●  Smart Cookies — ლოდინის რეჟიმი · ჩაირთვება მხოლოდ ავტორიზაციის მოთხოვნისას',
        'menu_video_title': 'ვიდეოს ჩამოტვირთვა',
        'menu_video_desc': 'ერთი ვიდეოს ან პლეილისტის ჩამოტვირთვა',
        'menu_channel_title': 'არხის ვიდეოები',
        'menu_channel_desc': 'არხის ყველა ვიდეო თარიღების ფილტრით',
        'menu_shorts_title': 'Shorts ვიდეოები',
        'menu_shorts_desc': 'არხის Shorts ვიდეოების მოძიება და ჩამოტვირთვა — სახელები: ნომერი + სათაური',
        'menu_channel_shorts_title': 'არხი + Shorts',
        'menu_channel_shorts_desc': 'ერთად მოძებნის და ჩამოტვირთავს არხის ჩვეულებრივ ვიდეოებსაც და Shorts-საც',
        'menu_playlists_title': 'პლეილისტები',
        'menu_playlists_desc': 'არხის პლეილისტების მოძიება და ჩამოტვირთვა',
        'menu_bulk_title': 'რამდენიმე ლინკი ერთად',
        'menu_bulk_desc': 'რამდენიმე ვიდეო ლინკის ერთდროული დაწყება — თითოეული თავის მაქსიმუმზე',
        'footer_text': 'yt-dlp  •  MAX Quality  •  დაცული cookie fallback',
        'language_switch_label': 'ენა:',
        'window_title_video': 'ვიდეოს ჩამოტვირთვა - MAX Quality',
        'window_title_channel': 'არხის ვიდეოები - MAX Quality',
        'window_title_shorts': 'Shorts ვიდეოები - MAX Quality',
        'window_title_channel_shorts': 'არხი + Shorts - MAX Quality',
        'window_title_playlists': 'პლეილისტები - MAX Quality',
        'window_title_bulk': 'რამდენიმე ლინკი - MAX Quality',
    },
}


def t(key):
    """მიმდინარე ენაზე ტექსტის დაბრუნება; ნაკლული key-ისთვის ინგლისურზე/თვითონ key-ზე დაბრუნდება."""
    table = TRANSLATIONS.get(CURRENT_LANGUAGE) or TRANSLATIONS[DEFAULT_LANGUAGE]
    return table.get(key) or TRANSLATIONS[DEFAULT_LANGUAGE].get(key, key)


def set_current_language(lang):
    """ენის შეცვლა + დისკზე დაუყოვნებლივი, საიმედო შენახვა."""
    global CURRENT_LANGUAGE
    if lang not in SUPPORTED_LANGUAGES or lang == CURRENT_LANGUAGE:
        return False
    CURRENT_LANGUAGE = lang
    _APP_SETTINGS['language'] = lang
    return save_app_settings(_APP_SETTINGS)


# yt-dlp ინსტალაცია / განახლება
# შენიშვნა: yt-dlp ახლა იტვირთება lazy რეჟიმში, რომ პროგრამამ GUI მაინც გახსნას
# მაშინაც კი, როცა პაკეტი არ არის დაყენებული ან ინტერნეტი დროებით გათიშულია.
MIN_RECOMMENDED_YTDLP_VERSION = (2026, 7, 20)  # YouTube player-client 403 fixes landed after stable 2026.07.04


def log_internal_error(context, error):
    """ჩუმი exception-ების ლოგირება — logs/app.log-ში (სრული stack trace-ით) და კონსოლზეც, crash-ის გარეშე."""
    try:
        APP_LOGGER.warning(f'{context}: {error}', exc_info=True)
    except Exception:
        pass


def _parse_bool_env(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _parse_yt_dlp_version_tuple(version_text):
    match = re.match(r'^(\d{4})\.(\d{2})\.(\d{2})', str(version_text or '').strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _get_installed_yt_dlp_version(module):
    try:
        return module.version.__version__
    except Exception:
        return getattr(module, '__version__', '')


def _pip_install_yt_dlp(force_upgrade=False, use_nightly=False):
    """Install/update yt-dlp. Nightly is used when a recent YouTube extractor fix is required."""
    cmd = [sys.executable, '-m', 'pip', 'install']
    if force_upgrade:
        cmd.append('-U')
    if use_nightly:
        cmd.append('--pre')
    cmd.extend(['yt-dlp[default]', '-q'])
    subprocess.check_call(cmd)


def install_yt_dlp():
    """yt-dlp-ის ჩატვირთვა; საჭიროებისას ინსტალაცია/განახლება EJS მხარდაჭერით."""
    auto_upgrade = _parse_bool_env('YTDLP_AUTO_UPGRADE', True)

    try:
        import yt_dlp as current_yt_dlp
    except ImportError:
        print('yt-dlp ინსტალაცია მიმდინარეობს...')
        _pip_install_yt_dlp(force_upgrade=True, use_nightly=True)
        import yt_dlp as current_yt_dlp
        return current_yt_dlp

    version_text = _get_installed_yt_dlp_version(current_yt_dlp)
    version_tuple = _parse_yt_dlp_version_tuple(version_text)

    needs_upgrade = version_tuple is None or version_tuple < MIN_RECOMMENDED_YTDLP_VERSION
    if auto_upgrade and needs_upgrade:
        try:
            print(f'yt-dlp განახლება მიმდინარეობს... (ამჟამინდელი ვერსია: {version_text or "უცნობი"})')
            _pip_install_yt_dlp(force_upgrade=True, use_nightly=True)
            importlib.invalidate_caches()
            current_yt_dlp = importlib.reload(current_yt_dlp)
        except Exception as upgrade_error:
            print(f'გაფრთხილება: yt-dlp update ვერ შესრულდა: {upgrade_error}')

    return current_yt_dlp


yt_dlp: Any = None
download_range_func: Any = None
ytdlp_sanitize_filename: Any = None
_YTDLP_LOAD_ERROR: Any = None
_ytdlp_load_lock = threading.Lock()


def ensure_yt_dlp_loaded():
    """yt-dlp-ის lazy ჩატვირთვა. GUI აღარ კვდება import/start ეტაპზე.

    thread-safe: თუ ერთდროულად რამდენიმე ფანჯარა/thread-ი პირველივე გაშვებაზე
    ითხოვს yt-dlp-ს, pip install მხოლოდ ერთხელ გაეშვება."""
    global yt_dlp, download_range_func, ytdlp_sanitize_filename, _YTDLP_LOAD_ERROR

    if yt_dlp is not None:
        return yt_dlp

    with _ytdlp_load_lock:
        if yt_dlp is not None:
            return yt_dlp

        try:
            yt_dlp = install_yt_dlp()
            try:
                ensure_pot_provider_installed()
            except Exception as pot_error:
                log_internal_error('ensure_pot_provider_installed', pot_error)

            try:
                from yt_dlp.utils import download_range_func as _download_range_func
                download_range_func = _download_range_func
            except Exception as import_error:
                download_range_func = None
                log_internal_error('yt-dlp download_range_func import', import_error)

            try:
                from yt_dlp.utils import sanitize_filename as _sanitize_filename
                ytdlp_sanitize_filename = _sanitize_filename
            except Exception as import_error:
                ytdlp_sanitize_filename = None
                log_internal_error('yt-dlp sanitize_filename import', import_error)

            _YTDLP_LOAD_ERROR = None
            return yt_dlp

        except Exception as load_error:
            _YTDLP_LOAD_ERROR = load_error
            raise RuntimeError(
                "yt-dlp ვერ ჩაიტვირთა/დაყენდა.\n\n"
                "გააკეთე ერთხელ PowerShell/CMD-ში:\n"
                "py -m pip install -U --pre yt-dlp[default]\n\n"
                f"ტექნიკური შეცდომა: {load_error}"
            )


def get_download_range_func_safe() -> Any:
    """Search-mode clip range-ის helper; საჭიროებისას yt-dlp-ს lazy ჩატვირთავს."""
    ensure_yt_dlp_loaded()
    if not callable(download_range_func):
        raise RuntimeError(
            "yt-dlp-ის download_range_func ვერ ჩაიტვირთა. "
            "განაახლე yt-dlp: py -m pip install -U yt-dlp[default]"
        )
    return download_range_func

# ============================
# ინტერნეტის გათიშვისას გაგრძელება / resume
# ============================

NETWORK_RESUME_MAX_WAIT_SECONDS = 24 * 60 * 60  # 24 საათი
NETWORK_RESUME_CHECK_INTERVAL_SECONDS = 60
NETWORK_RESUME_CONNECT_TIMEOUT_SECONDS = 8

NETWORK_ERROR_KEYWORDS = (
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection refused",
    "network is unreachable",
    "temporary failure",
    "temporary failure in name resolution",
    "name resolution",
    "dns",
    "getaddrinfo failed",
    "no route to host",
    "failed to establish a new connection",
    "remote end closed connection",
    "read operation timed out",
    "the read operation timed out",
    "internet connection",
    "network connection",
    "network error",
    "connection error",
    "ssl: wrong version number",
    "ssl handshake",
    "winerror 10054",
    "winerror 10060",
    "winerror 10061",
    "winerror 11001",
)


def has_internet_connection():
    """ამოწმებს დაბრუნდა თუ არა ინტერნეტი."""
    targets = [
        ("1.1.1.1", 53),
        ("8.8.8.8", 53),
        ("youtube.com", 443),
    ]

    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=NETWORK_RESUME_CONNECT_TIMEOUT_SECONDS):
                return True
        except OSError:
            continue

    return False


def is_probably_network_error(error):
    """ვამოწმებთ შეცდომა ინტერნეტს ჰგავს თუ სხვა პრობლემაა."""
    text = str(error or "").lower()
    return any(keyword in text for keyword in NETWORK_ERROR_KEYWORDS)


def wait_for_network_restore():
    """ინტერნეტის დაბრუნებამდე ლოდინი."""
    started_at = time.monotonic()

    while True:
        if has_internet_connection():
            print("ინტერნეტი დაბრუნდა — ჩამოტვირთვა გაგრძელდება...")
            return True

        elapsed = time.monotonic() - started_at
        if NETWORK_RESUME_MAX_WAIT_SECONDS is not None and elapsed >= NETWORK_RESUME_MAX_WAIT_SECONDS:
            return False

        elapsed_minutes = int(elapsed // 60)
        max_wait_text = (
            "უსასრულო"
            if NETWORK_RESUME_MAX_WAIT_SECONDS is None
            else f"{int(NETWORK_RESUME_MAX_WAIT_SECONDS // 3600)} საათი"
        )
        print(
            f"ინტერნეტი არ არის — გასულია {elapsed_minutes} წუთი, "
            f"ლიმიტი: {max_wait_text}. "
            f"{NETWORK_RESUME_CHECK_INTERVAL_SECONDS} წამში თავიდან შემოწმდება..."
        )
        time.sleep(NETWORK_RESUME_CHECK_INTERVAL_SECONDS)


def prepare_resume_download_options(opts):
    """yt-dlp პარამეტრები, რომ გაწყვეტის შემდეგ .part ფაილიდან გააგრძელოს."""
    prepared = apply_cookie_preferences(opts)
    prepared.setdefault("continuedl", True)
    prepared.setdefault("nopart", False)
    prepared.setdefault("overwrites", False)
    return prepared



class ModernStyle:
    """ერთიანი, თანამედროვე მუქი ინტერფეისის პალიტრა."""

    BG_MAIN = "#090e1a"
    BG_CARD = "#121a2b"
    BG_INPUT = "#1a2538"
    BG_CARD_HOVER = "#18243a"
    BORDER = "#263550"

    ACCENT = "#7c5cff"
    ACCENT_HOVER = "#9278ff"
    ACCENT_GLOW = "#c4b5fd"

    SUCCESS = "#10b981"
    WARNING = "#f59e0b"
    PINK = "#ec4899"
    CYAN = "#22d3ee"

    TEXT = "#f8fafc"
    TEXT_GRAY = "#94a3b8"
    TEXT_DIM = "#64748b"

    @classmethod
    def apply(cls, root):
        """სტილის გამოყენება"""
        style = ttk.Style()
        style.theme_use('clam')

        style.configure("Main.TFrame", background=cls.BG_MAIN)
        style.configure("Card.TFrame", background=cls.BG_CARD)
        style.configure("CardHover.TFrame", background=cls.BG_CARD_HOVER)

        style.configure("Title.TLabel",
                       background=cls.BG_MAIN,
                       foreground=cls.ACCENT_GLOW,
                       font=("Segoe UI", 22, "bold"))

        style.configure("Subtitle.TLabel",
                       background=cls.BG_MAIN,
                       foreground=cls.TEXT_GRAY,
                       font=("Segoe UI", 10))

        style.configure("Card.TLabel",
                       background=cls.BG_CARD,
                       foreground=cls.TEXT,
                       font=("Segoe UI", 11))

        style.configure("CardBold.TLabel",
                       background=cls.BG_CARD,
                       foreground=cls.CYAN,
                       font=("Segoe UI", 11, "bold"))

        style.configure("Info.TLabel",
                       background=cls.BG_CARD,
                       foreground=cls.TEXT_GRAY,
                       font=("Segoe UI", 10))

        style.configure("CardSubtitle.TLabel",
                       background=cls.BG_CARD,
                       foreground=cls.TEXT_GRAY,
                       font=("Segoe UI", 10))

        style.configure("Status.TLabel",
                       background=cls.BG_INPUT,
                       foreground=cls.SUCCESS,
                       padding=(12, 7),
                       font=("Segoe UI Semibold", 9))

        style.configure("Card.TRadiobutton",
                       background=cls.BG_CARD,
                       foreground=cls.TEXT,
                       font=("Segoe UI", 11))

        style.configure("Card.TCheckbutton",
                       background=cls.BG_CARD,
                       foreground=cls.TEXT,
                       font=("Segoe UI", 10))

        style.configure("Accent.Horizontal.TProgressbar",
                       background=cls.ACCENT,
                       troughcolor=cls.BG_INPUT,
                       thickness=24)

        style.configure("Custom.Vertical.TScrollbar",
                       background=cls.BG_INPUT,
                       troughcolor=cls.BG_MAIN,
                       arrowcolor=cls.TEXT_GRAY)

        style.configure("TCombobox",
                       fieldbackground=cls.BG_INPUT,
                       background=cls.BG_INPUT,
                       foreground=cls.TEXT,
                       arrowcolor=cls.TEXT_GRAY,
                       bordercolor=cls.BORDER,
                       lightcolor=cls.BORDER,
                       darkcolor=cls.BORDER,
                       padding=6)
        style.map("TCombobox",
                  fieldbackground=[("readonly", cls.BG_INPUT)],
                  foreground=[("readonly", cls.TEXT)],
                  selectbackground=[("readonly", cls.BG_INPUT)],
                  selectforeground=[("readonly", cls.TEXT)])


class ScrollableFrame(ttk.Frame):
    """სქროლვადი ფრეიმი"""

    def __init__(self, container, *args, **kwargs):
        super().__init__(container, *args, **kwargs)

        self.canvas = tk.Canvas(self, bg=ModernStyle.BG_MAIN, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                       style="Custom.Vertical.TScrollbar")
        self.scrollable_frame = ttk.Frame(self.canvas, style="Main.TFrame")

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.canvas_frame = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", self._on_mousewheel)
        self.canvas.bind_all("<Button-5>", self._on_mousewheel)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def _on_mousewheel(self, event):
        if event.num == 5 or event.delta < 0:
            self.canvas.yview_scroll(1, "units")
        elif event.num == 4 or event.delta > 0:
            self.canvas.yview_scroll(-1, "units")

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_frame, width=event.width)


# ============================
# მაღალი ხარისხის ფორმატის ფუნქციები
# ============================


def normalize_quality_height(quality):
    """ხარისხის ტექსტიდან height-ის ამოღება"""
    if quality in (None, "", "best"):
        return None
    match = re.search(r'(\d{3,4})p', str(quality))
    return int(match.group(1)) if match else None


def quality_label_from_height(height):
    """height -> გამოსაჩენი ტექსტი"""
    labels = {
        4320: "4320p (8K)",
        2160: "2160p (4K)",
        1440: "1440p (2K)",
        1080: "1080p (Full HD)",
        720: "720p (HD)",
        480: "480p",
        360: "360p",
        240: "240p",
        144: "144p",
    }
    return labels.get(height, f"{height}p")


def extract_quality_code_from_label(label):
    """Combobox label -> quality code"""
    label = (label or "").strip()
    if not label or label.startswith("მაქსიმალური") or label.startswith("ავტომატური"):
        return "best"

    height = normalize_quality_height(label)
    return f"{height}p" if height else "best"


def is_video_format(fmt):
    """ნამდვილი ვიდეო ფორმატის შემოწმება"""
    if not isinstance(fmt, dict):
        return False

    vcodec = fmt.get('vcodec')
    if not vcodec or vcodec == 'none':
        return False

    if fmt.get('ext') == 'mhtml':
        return False
    if fmt.get('format_note') == 'storyboard':
        return False

    return True


def get_available_video_heights(info):
    """ინფოდან ხელმისაწვდომი ვიდეო height-ების სია"""
    heights = set()

    for fmt in info.get('formats', []) or []:
        if not is_video_format(fmt):
            continue

        height = fmt.get('height')
        if isinstance(height, int) and height > 0:
            heights.add(height)

    return sorted(heights, reverse=True)


def get_max_available_height(info):
    """ინფოდან მაქსიმალური ხელმისაწვდომი ხარისხი"""
    heights = get_available_video_heights(info)
    return heights[0] if heights else None


def get_quality_combo_values(info):
    """Combobox-ისთვის ხელმისაწვდომი ხარისხების სია"""
    heights = get_available_video_heights(info)
    values = []

    if heights:
        values.append(f"მაქსიმალური ({heights[0]}p)")
        values.extend(quality_label_from_height(h) for h in heights)
    else:
        values.extend([
            "მაქსიმალური (4K/8K)",
            "2160p (4K)",
            "1440p (2K)",
            "1080p (Full HD)",
            "720p (HD)",
            "480p",
            "360p",
        ])

    return values


def is_ffmpeg_available():
    """ffmpeg/ffprobe ხელმისაწვდომობის შემოწმება.

    პირველ რიგში ამოწმებს C:\ffmpeg\bin-ს, შემდეგ PATH-ს.
    """
    return get_ffmpeg_location() is not None


def needs_merged_av(target_height):
    """1080p-ზე მაღალი ხარისხები ხშირად მოდის ცალკე ვიდეო+აუდიო ნაკადებად"""
    return isinstance(target_height, int) and target_height > 1080


def ensure_video_pipeline_ready(info=None, quality="best"):
    """არ დავუშვათ 1080p-ზე ზემოთ ჩუმი/არასწორი ფაილი ffmpeg-ის გარეშე"""
    target_height = resolve_target_height(info or {}, quality)
    if needs_merged_av(target_height) and not is_ffmpeg_available():
        raise RuntimeError(
            "1080p-ზე მაღალი ხარისხი (2K/4K/8K) ამ საიტზე ჩვეულებრივ ცალკე ვიდეო+აუდიო სტრიმებად მოდის. "
            "საბოლოოდ ერთიანი სტანდარტული ვიდეო რომ მიიღოთ, სისტემაში ffmpeg და ffprobe უნდა იყოს დაყენებული. "
            "ამის გარეშე ჩუმი ან არასწორი ფაილის შექმნას აღარ ვუშვებ."
        )
    return target_height


def get_best_quality_format(quality="best"):
    """
    უსაფრთხო ფორმატის selector:
    - best => თითო ვიდეოზე მაქსიმალური ხელმისაწვდომი ხარისხი
    - კონკრეტული ხარისხი => მხოლოდ ზუსტად ის height, არანაირი დაბალზე ჩასვლა
    """
    if quality == "best":
        return "bv*+ba/b"

    height = normalize_quality_height(quality)
    if not height:
        return "bv*+ba/b"

    return f"bv*[height={height}]+ba/b[height={height}]"


def choose_video_container(height):
    """1080p და ქვემოთ -> MP4, ზემოთ -> AUTO (წყაროს შესაბამისი კონტეინერი)"""
    if isinstance(height, int) and height > 0 and height <= 1080:
        return 'mp4'
    return None


def get_video_format_selector(quality="best", prefer_mp4=False):
    """
    ვიდეო ფორმატის selector.
    prefer_mp4=True როცა საბოლოო კონტეინერი MP4 უნდა იყოს,
    რათა ჯერ mp4/m4a ნაკადები ვცადოთ და მერე საჭიროებისას ზოგად ვარიანტზე გადავიდეთ.
    """
    if not prefer_mp4:
        return get_best_quality_format(quality)

    height = normalize_quality_height(quality)
    if height:
        return (
            f"bv*[height={height}][ext=mp4]+ba[ext=m4a]"
            f"/b[height={height}][ext=mp4]"
            f"/bv*[height={height}]+ba"
            f"/b[height={height}]"
        )

    return "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"


def resolve_target_height(info, quality="best"):
    """არჩეული ხარისხიდან ან ინფოდან საბოლოო target height"""
    if quality == 'best':
        return get_max_available_height(info or {})
    return normalize_quality_height(quality)


def resolve_playlist_quality_fallback(info, quality="best"):
    """პლეილისტზე არჩეული ხარისხის ეფექტური fallback."""
    if quality == 'best':
        target_height = get_max_available_height(info or {})
        return 'best', target_height, False

    requested_height = normalize_quality_height(quality)
    heights = get_available_video_heights(info or {})

    if not requested_height or not heights:
        return quality, requested_height, False

    if requested_height in heights:
        return f"{requested_height}p", requested_height, False

    lower_or_equal = [h for h in heights if h <= requested_height]
    if lower_or_equal:
        fallback_height = max(lower_or_equal)
        return f"{fallback_height}p", fallback_height, True

    fallback_height = min(heights)
    return f"{fallback_height}p", fallback_height, True


SIZE_LIMIT_BYTES_2GB = 2_000_000_000


def get_estimated_size_bytes(fmt):
    """ფორმატის სავარაუდო ზომა bytes-ში"""
    if not isinstance(fmt, dict):
        return None
    for key in ('filesize', 'filesize_approx'):
        value = fmt.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def estimate_best_audio_size_bytes(info):
    """საუკეთესო აუდიო სტრიმის ზომის შეფასება"""
    sizes = []
    for fmt in info.get('formats', []) or []:
        if not isinstance(fmt, dict):
            continue
        if fmt.get('vcodec') == 'none' and fmt.get('acodec') not in (None, 'none'):
            size = get_estimated_size_bytes(fmt)
            if size:
                sizes.append(size)
    return max(sizes) if sizes else 0


def estimate_video_size_for_height(info, height):
    """კონკრეტული height-ის საბოლოო ვიდეო+აუდიოს სავარაუდო ზომა"""
    if not isinstance(height, int) or height <= 0:
        return None

    audio_size = estimate_best_audio_size_bytes(info or {})
    candidates = []

    for fmt in info.get('formats', []) or []:
        if not is_video_format(fmt):
            continue
        if fmt.get('height') != height:
            continue

        size = get_estimated_size_bytes(fmt)
        if not size:
            continue

        if fmt.get('acodec') not in (None, 'none'):
            candidates.append(size)
        else:
            candidates.append(size + audio_size)

    return max(candidates) if candidates else None


def resolve_video_quality_with_size_limit(info, quality="best", size_limit_enabled=False, size_limit_bytes=SIZE_LIMIT_BYTES_2GB):
    """ხარისხის fallback + სურვილის შემთხვევაში 2GB ზღვრის გამო დაბლა ჩამოსვლა"""
    effective_quality, effective_height, used_fallback = resolve_playlist_quality_fallback(info, quality)
    estimated_size = estimate_video_size_for_height(info or {}, effective_height)
    used_size_limit = False
    size_limit_from_height = None

    if size_limit_enabled and isinstance(effective_height, int) and effective_height > 0:
        heights = [h for h in get_available_video_heights(info or {}) if h <= effective_height]
        if not heights:
            heights = [effective_height]

        size_limit_from_height = effective_height
        final_height = effective_height
        final_size = estimated_size
        for candidate_height in heights:
            candidate_size = estimate_video_size_for_height(info or {}, candidate_height)
            if candidate_size is None or candidate_size < size_limit_bytes:
                final_height = candidate_height
                final_size = candidate_size
                break
        else:
            final_height = heights[-1]
            final_size = estimate_video_size_for_height(info or {}, final_height)

        used_size_limit = final_height != effective_height
        effective_height = final_height
        estimated_size = final_size
        effective_quality = 'best' if quality == 'best' and not used_size_limit and effective_height == get_max_available_height(info or {}) else f"{effective_height}p"

    return effective_quality, effective_height, used_fallback, used_size_limit, estimated_size, size_limit_from_height


def build_video_download_options(info=None, quality="best"):
    """
    არჩეული/რეალური height-ის მიხედვით ვიდეოს download ოფციები.
    - 1080p და ქვემოთ: საბოლოო ფაილი MP4
    - 1080p-ზე ზემოთ: AUTO კონტეინერი (რაც წყაროს/სტრიმებს ბუნებრივად ერგება; ხშირად WebM ან MKV)
    """
    target_height = resolve_target_height(info, quality)
    container = choose_video_container(target_height)
    prefer_mp4 = (container == 'mp4')

    opts = {
        'format': get_video_format_selector(quality, prefer_mp4=prefer_mp4),
        'prefer_ffmpeg': True,
        'ffmpeg_location': get_ffmpeg_location() or FFMPEG_DIR,
        'keepvideo': False,
        'postprocessor_args': {
            'ffmpeg': ['-c', 'copy'],
        },
    }

    if container:
        opts['merge_output_format'] = container

    return opts, container, target_height


def normalize_audio_quality(audio_quality):
    """აუდიოს არჩეული ხარისხის ნორმალიზაცია"""
    q = str(audio_quality or "320").strip()
    return q if q in {"320", "256", "192", "128"} else "320"


def get_best_audio_format():
    """
    აუდიოსთვის რეალურად საუკეთესო წყაროს ამორჩევა.
    აღარ ვანიჭებთ ხელოვნურ უპირატესობას მხოლოდ opus/m4a-ს,
    რადგან ზოგ ვიდეოზე საუკეთესო აუდიო შეიძლება სხვა კონტეინერში ან სხვა bitrate-ით იყოს.
    """
    return "bestaudio/best"


def get_audio_download_options(audio_quality):
    """
    MP3 ჩამოტვირთვის/კონვერტაციის სტაბილური ოფციები.
    ახლა აუდიო ფაილში ავტომატურად იწერება:
    - YouTube thumbnail/logo როგორც cover art
    - title / artist / uploader / album / date და სხვა metadata, რაც წყაროდან მოდის
    """
    quality = normalize_audio_quality(audio_quality)
    return {
        'format': get_best_audio_format(),
        'prefer_ffmpeg': True,
        'ffmpeg_location': get_ffmpeg_location() or FFMPEG_DIR,
        'keepvideo': False,

        # საჭიროა cover art-ისთვის: yt-dlp ჩამოწერს thumbnail-ს და EmbedThumbnail ჩასვამს MP3-ში.
        'writethumbnail': True,

        # MP3 metadata + cover art. ეს მუშაობს ყველა აუდიო რეჟიმში,
        # რადგან ყველა ფანჯარა საერთო get_audio_download_options() ფუნქციას იყენებს.
        'postprocessors': [
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': quality,
            },
            {
                'key': 'FFmpegMetadata',
                'add_metadata': True,
                'add_chapters': True,
            },
            {
                'key': 'EmbedThumbnail',
                'already_have_thumbnail': False,
            },
        ],

        # Windows/ძველ პლეერებთან უკეთესი თავსებადობა MP3 tag-ებისთვის.
        'postprocessor_args': {
            'ffmpeg': ['-id3v2_version', '3'],
        },
    }


_COOKIE_FILE_WARNED = set()


def _looks_like_netscape_cookie_file(path):
    """მარტივი ვალიდაცია: ფაილს ნამდვილი Netscape cookies ფორმატი აქვს თუ არა
    (row-based, tab-გამოყოფილი — არა ბრაუზერიდან პირდაპირ დაკოპირებული 'name=value; name2=value2' სტრიქონი).
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for _ in range(200):
                line = f.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                # ნამდვილი Netscape ხაზი: domain \t flag \t path \t secure \t expiration \t name \t value
                fields = stripped.split('\t')
                return len(fields) >= 6
    except OSError:
        return False
    return False


def validate_cookie_file(path):
    """cook-დაცვა ცალკე ფაილისთვის: ვამოწმებთ რომ cookies ფაილი ნამდვილად
    გამოსადეგია, სანამ საერთოდ yt-dlp-ს გადავცემთ. თუ ცარიელია, არასწორი
    ფორმატისაა ან ძალიან ძველია — გამოტოვება (ჩარევის/crash-ის ნაცვლად).
    """
    if not path or not os.path.isfile(path):
        return False

    try:
        if os.path.getsize(path) == 0:
            if path not in _COOKIE_FILE_WARNED:
                print('გაფრთხილება: cookies ფაილი ცარიელია — {}'.format(path))
                _COOKIE_FILE_WARNED.add(path)
            return False
    except OSError:
        return False

    if not _looks_like_netscape_cookie_file(path):
        if path not in _COOKIE_FILE_WARNED:
            print(
                'გაფრთხილება: "{}" არ ჰგავს სწორ Netscape cookies ფაილს '
                '(სავარაუდოდ ბრაუზერის raw Cookie header არის დაკოპირებული პირდაპირ, '
                'არა სპეციალური გაფართოებით ("Get cookies.txt LOCALLY" და მისთ.) ექსპორტირებული). '
                'ეს ფაილი გამოტოვებულია, cookies არ გამოყენებულა.'.format(path)
            )
            _COOKIE_FILE_WARNED.add(path)
        return False

    try:
        age_days = (time.time() - os.path.getmtime(path)) / 86400
        age_key = path + ':age'
        if age_days > 20 and age_key not in _COOKIE_FILE_WARNED:
            print(
                'გაფრთხილება: cookies ფაილი "{}" დაახლოებით {} დღის წინაა გატანილი — '
                'YouTube-ის session ხშირად rotate/ვადაგასულდება. თუ auth ისევ ჩავარდება, '
                'გაექსპორტეთ ახალი ფაილი.'.format(os.path.basename(path), int(age_days))
            )
            _COOKIE_FILE_WARNED.add(age_key)
    except OSError:
        pass

    return True


def get_default_cookie_file():
    """იპოვე ხელით გატანილი cookies.txt/youtube-cookies.txt, თუ არსებობს და ვალიდურია."""
    candidates = []

    env_cookie_file = os.environ.get('YTDLP_COOKIES_FILE', '').strip()
    if env_cookie_file:
        candidates.append(env_cookie_file)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    cwd = os.getcwd()

    for base in (script_dir, cwd):
        candidates.extend([
            os.path.join(base, 'youtube-cookies.txt'),
            os.path.join(base, 'cookies.txt'),
        ])

    seen = set()
    for candidate in candidates:
        normalized = os.path.abspath(os.path.expandvars(os.path.expanduser(candidate)))
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.isfile(normalized) and validate_cookie_file(normalized):
            return normalized
    return None


SUPPORTED_COOKIE_BROWSERS = {
    'brave', 'chrome', 'chromium', 'edge', 'firefox', 'opera', 'safari', 'vivaldi', 'whale'
}

SUPPORTED_JS_RUNTIMES = {'node', 'deno', 'bun', 'quickjs'}


def get_cookie_browser_priority():
    """Browser priority. Override with YTDLP_BROWSER_ORDER=firefox,edge,chrome if needed."""
    env_value = os.environ.get('YTDLP_BROWSER_ORDER', '').strip()
    defaults = ['firefox', 'edge', 'chrome']

    if not env_value:
        return defaults

    items = []
    seen = set()
    for raw in env_value.split(','):
        name = raw.strip().lower()
        if name in SUPPORTED_COOKIE_BROWSERS and name not in seen:
            items.append(name)
            seen.add(name)

    return items or defaults


BROWSER_PROFILE_HINTS = {
    'firefox': [
        os.path.join(os.environ.get('APPDATA', ''), 'Mozilla', 'Firefox', 'Profiles'),
    ],
    'edge': [
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Edge', 'User Data'),
    ],
    'chrome': [
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Google', 'Chrome', 'User Data'),
    ],
    'brave': [
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'BraveSoftware', 'Brave-Browser', 'User Data'),
    ],
    'chromium': [
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Chromium', 'User Data'),
    ],
    'opera': [
        os.path.join(os.environ.get('APPDATA', ''), 'Opera Software'),
    ],
    'vivaldi': [
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Vivaldi', 'User Data'),
    ],
    'whale': [
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Naver', 'Naver Whale', 'User Data'),
    ],
}


def _browser_profile_exists(browser):
    for raw_path in BROWSER_PROFILE_HINTS.get(browser, []):
        if not raw_path:
            continue
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(raw_path)))
        if os.path.isdir(path):
            return True
    return False


def get_cookie_auth_mode():
    """ერთი ავტორიზაციის რეჟიმი: auto/browser/file/none."""
    mode = os.environ.get('YTDLP_AUTH_MODE', 'auto').strip().lower()
    return mode if mode in {'auto', 'browser', 'file', 'none'} else 'auto'



def get_browser_cookie_source():
    """
    ერთჯერადი browser auth წყარო.
    პრიორიტეტი:
      1) YTDLP_BROWSER_COOKIES თუ მითითებულია
      2) auto-detect პროფილებიდან (ნაგულისხმევად Firefox -> Edge -> Chrome)
    """
    explicit_browser = os.environ.get('YTDLP_BROWSER_COOKIES', '').strip().lower()
    if explicit_browser in SUPPORTED_COOKIE_BROWSERS:
        return (explicit_browser,)

    for browser in get_cookie_browser_priority():
        if _browser_profile_exists(browser):
            return (browser,)

    return None



def strip_cookie_options(opts):
    """Return opts without cookie-related keys."""
    cleaned = dict(opts or {})
    cleaned.pop('cookiefile', None)
    cleaned.pop('cookiesfrombrowser', None)
    return cleaned



def resolve_cookie_source():
    """აირჩიე ზუსტად ერთი auth წყარო: cookie file (youtube-cookies.txt) -> browser -> no cookies.

    ფაილი პრიორიტეტულია, რადგან მომხმარებელმა ხელით მოამზადა/ექსპორტა და
    კონკრეტულ ანგარიშზეა მიბმული — browser auto-detect მხოლოდ fallback-ია,
    თუ ვალიდური ფაილი ვერ მოიძებნა.
    """
    mode = get_cookie_auth_mode()
    cookie_file = get_default_cookie_file()
    browser_cookie_source = get_browser_cookie_source()

    if mode == 'none':
        return ('no_cookies', None)

    if mode == 'file':
        if cookie_file:
            return ('cookie_file', cookie_file)
        return ('no_cookies', None)

    if mode == 'browser':
        if browser_cookie_source:
            return ('browser', browser_cookie_source[0])
        return ('no_cookies', None)

    if cookie_file:
        return ('cookie_file', cookie_file)

    if browser_cookie_source:
        return ('browser', browser_cookie_source[0])

    return ('no_cookies', None)



def apply_cookie_preferences(opts):
    """დაამატე მხოლოდ ერთი auth წყარო, რომ არ გამრავლდეს ზედმეტი მცდელობები/ერორები."""
    cleaned = strip_cookie_options(opts)
    source_kind, source_value = resolve_cookie_source()

    if source_kind == 'cookie_file':
        cleaned['cookiefile'] = source_value
    elif source_kind == 'browser':
        cleaned['cookiesfrombrowser'] = (source_value,)

    return cleaned


def _module_exists(module_name):
    try:
        importlib.import_module(module_name)
        return True
    except Exception:
        return False


def _find_js_runtime_path(runtime_name):
    custom_path = os.environ.get('YTDLP_JS_RUNTIME_PATH', '').strip()
    if custom_path:
        expanded = os.path.abspath(os.path.expandvars(os.path.expanduser(custom_path)))
        if os.path.exists(expanded):
            return expanded

    candidates = {
        'node': ['node'],
        'deno': ['deno'],
        'bun': ['bun'],
        'quickjs': ['qjs', 'quickjs'],
    }.get(runtime_name, [])

    for binary in candidates:
        found = shutil.which(binary)
        if found:
            return found
    return None


def get_js_runtime_options():
    """EJS/JavaScript challenge-სთვის runtime-ის არჩევა."""
    if _parse_bool_env('YTDLP_DISABLE_JS_RUNTIME', False):
        return {}

    preferred = os.environ.get('YTDLP_JS_RUNTIME', '').strip().lower()
    order = [preferred] if preferred in SUPPORTED_JS_RUNTIMES else ['node', 'deno', 'bun', 'quickjs']

    for runtime_name in order:
        runtime_path = _find_js_runtime_path(runtime_name)
        if runtime_path:
            return {runtime_name: {'path': runtime_path}}
    return {}


POT_PROVIDER_PACKAGE = 'bgutil-ytdlp-pot-provider'
POT_PROVIDER_MODULE = 'bgutil_ytdlp_pot_provider'


def ensure_pot_provider_installed():
    """YouTube-ის PO Token-ს ავტომატურად აგენერირებს (HTTP 403-ის თავიდან ასაცილებლად).

    yt-dlp თავად აღმოაჩენს pip-ით დაყენებულ plugin-ს და 'script' მეთოდით
    ამოიღებს PO token-ს უკვე დაყენებული JS runtime-ის (node/deno/bun) გამოყენებით —
    მომხმარებელს აღარ სჭირდება ტოკენის ხელით მოძებნა/env ცვლადის დაყენება.
    წარუმატებლობისას ჩუმად წყდება; YTDLP_YT_PO_TOKEN და მსგავსი env ცვლადები
    კვლავ მუშაობს როგორც manual fallback."""
    if _parse_bool_env('YTDLP_DISABLE_POT_PROVIDER', False):
        return False

    if _module_exists(POT_PROVIDER_MODULE):
        return True

    if not get_js_runtime_options():
        # script-მეთოდს node/deno/bun სჭირდება; მის გარეშე დაყენება უსარგებლოა.
        return False

    try:
        subprocess.check_call([
            sys.executable, '-m', 'pip', 'install', '-U', POT_PROVIDER_PACKAGE, '-q',
        ])
        importlib.invalidate_caches()
        return _module_exists(POT_PROVIDER_MODULE)
    except Exception as install_error:
        log_internal_error('bgutil-ytdlp-pot-provider install', install_error)
        return False


def get_remote_components_options(runtime_opts=None):
    """თუ yt-dlp-ejs არ დგას, მივცეთ remote fallback."""
    if _module_exists('yt_dlp_ejs'):
        return set()

    runtime_opts = runtime_opts or {}
    if not runtime_opts:
        return {'ejs:github'}

    runtime_name = next(iter(runtime_opts.keys()))
    if runtime_name in {'deno', 'bun'}:
        return {'ejs:npm', 'ejs:github'}
    return {'ejs:github'}


def _parse_csv_env(name):
    value = os.environ.get(name, '').strip()
    if not value:
        return []
    return [item.strip() for item in value.split(',') if item.strip()]


def get_youtube_extractor_args():
    """YouTube extractor args.

    IMPORTANT: do not hard-code a player-client chain by default. YouTube changes client
    enforcement often and yt-dlp maintains its own current defaults. A custom client chain
    is only applied when YTDLP_YT_PLAYER_CLIENTS is explicitly set by the user.
    """
    clients = _parse_csv_env('YTDLP_YT_PLAYER_CLIENTS')

    po_tokens = []
    raw_tokens = _parse_csv_env('YTDLP_YT_PO_TOKEN')
    po_tokens.extend(raw_tokens)

    prefixed_token_envs = [
        ('YTDLP_YT_WEB_GVS_PO_TOKEN', 'web.gvs'),
        ('YTDLP_YT_WEB_PLAYER_PO_TOKEN', 'web.player'),
        ('YTDLP_YT_MWEB_GVS_PO_TOKEN', 'mweb.gvs'),
        ('YTDLP_YT_MWEB_PLAYER_PO_TOKEN', 'mweb.player'),
    ]

    for env_name, prefix in prefixed_token_envs:
        token_value = os.environ.get(env_name, '').strip()
        if token_value:
            po_tokens.append(f'{prefix}+{token_value}')

    youtube_args = {}
    if clients:
        youtube_args['player_client'] = clients
    if po_tokens:
        youtube_args['po_token'] = po_tokens

    return {'youtube': youtube_args} if youtube_args else {}


class YdlInternalLogger:
    """yt-dlp-ის ყველა შიდა შეტყობინება (debug/warning/error) — quiet-ის მიუხედავად —
    logs/app.log-ში იწერება, რომ ჩამოტვირთვის ნებისმიერი პრობლემა აბსოლუტურად აისახოს იქ.
    კონსოლზე მხოლოდ warning/error გამოდის (APP_LOGGER-ის console handler-ის დონის მიხედვით),
    debug/info მხოლოდ ფაილში რჩება, რომ ტერმინალი spam-ით არ გავსებულიყო."""

    def debug(self, msg):
        APP_LOGGER.debug(str(msg))

    def info(self, msg):
        APP_LOGGER.debug(str(msg))

    def warning(self, msg):
        APP_LOGGER.warning(str(msg))

    def error(self, msg):
        APP_LOGGER.error(str(msg))


YDL_INTERNAL_LOGGER = YdlInternalLogger()


def apply_runtime_preferences(opts):
    """JS runtime / remote components / extractor args / optional network overrides."""
    opts.setdefault('logger', YDL_INTERNAL_LOGGER)

    runtime_opts = get_js_runtime_options()
    if runtime_opts:
        opts['js_runtimes'] = runtime_opts

    remote_components = get_remote_components_options(runtime_opts)
    if remote_components:
        opts['remote_components'] = remote_components

    extractor_args = get_youtube_extractor_args()
    if extractor_args:
        opts['extractor_args'] = extractor_args

    source_address = os.environ.get('YTDLP_SOURCE_ADDRESS', '').strip()
    if source_address:
        opts['source_address'] = source_address

    custom_user_agent = os.environ.get('YTDLP_USER_AGENT', '').strip()
    if custom_user_agent:
        opts.setdefault('http_headers', {})['User-Agent'] = custom_user_agent

    return opts


def build_cookie_failure_message(source_label, error_message):
    """Single-source auth error ტექსტი."""
    cleaned = strip_ansi_codes(str(error_message or '').strip().replace('\r', ''))
    cleaned = cleaned.replace('\n\n', '\n')
    preview = cleaned.split('\n', 1)[0].strip() or 'Unknown yt-dlp error.'
    if len(preview) > 220:
        preview = preview[:217] + '...'

    lines = [f'Auth method failed: {source_label}', f'- {preview}', '', 'Tips:']
    lines.append('1) ეს build cookies-ს რთავს მხოლოდ ცალსახა sign-in/age/member/bot მოთხოვნაზე; ჩვეულებრივი HTTP 403 cookies retry-ს აღარ ააქტიურებს.')
    lines.append('2) ნაგულისხმევად პრიორიტეტია browser cookies: Firefox -> Edge -> Chrome; შეგიძლია შეცვალო YTDLP_AUTH_MODE=file/browser/none-ით.')
    lines.append('3) თუ bot ბლოკი დარჩა, ბრაუზერში ჯერ CAPTCHA გაიარე და იგივე IP-ით გაუშვი პროგრამა.')
    lines.append('4) თუ cookies ფაილს იყენებ, გაანახლე fresh youtube-cookies.txt ინკოგნიტოდან.')
    return '\n'.join(lines)



def make_auth_source_label(source_kind, source_value):
    """auth წყაროს უსაფრთხო label — აქ აღარ დაეცემა None/tuple/path ტიპებზე."""
    if source_kind == 'cookie_file':
        if source_value:
            return 'cookie_file:{}'.format(os.path.basename(str(source_value)))
        return 'cookie_file:missing'

    if source_kind == 'browser':
        if isinstance(source_value, (tuple, list)):
            source_value = source_value[0] if source_value else ''
        return 'browser:{}'.format(str(source_value or 'auto'))

    return 'no_cookies'


def _attempt_ydl_without_cookies(opts):
    """cookies-ის გარეშე მცდელობა — ეს არის ახლა ნაგულისხმევი, პირველი მცდელობა."""
    fallback_opts = strip_cookie_options(dict(opts or {}))
    fallback_opts.setdefault('continuedl', True)
    fallback_opts.setdefault('nopart', False)
    fallback_opts.setdefault('overwrites', False)
    return fallback_opts


def prepare_attempt_options(opts, *, use_cookies):
    """cookie-დაცვა: cookies opts-ს ვამატებთ მხოლოდ იმ შემთხვევაში, თუ use_cookies=True.
    ნაგულისხმევად (use_cookies=False) cookies საერთოდ არ ერთვება.
    """
    if use_cookies:
        protected = prepare_resume_download_options(opts)
        # ავტორიზებული სესია უფრო ფრთხილად გამოვიყენოთ: ერთი ფრაგმენტი და მცირე
        # პაუზები მნიშვნელოვნად ამცირებს ანგარიშიდან ერთდროულ მოთხოვნებს.
        protected['concurrent_fragment_downloads'] = 1
        protected.setdefault('sleep_interval_requests', 1.0)
        protected.setdefault('sleep_interval', 1.0)
        protected.setdefault('max_sleep_interval', 3.0)
        return protected
    return _attempt_ydl_without_cookies(opts)


ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*m')


def strip_ansi_codes(value):
    return ANSI_ESCAPE_RE.sub('', str(value or ''))


PERMANENT_VIDEO_ERROR_KEYWORDS = (
    'video unavailable',
    'this video is unavailable',
    'has been removed by the uploader',
    'removed by the uploader',
    'unavailable video',
    'this video has been removed',
    'account associated with this video has been terminated',
)


AUTH_RECOVERY_ERROR_KEYWORDS = (
    'sign in',
    'login',
    'cookies',
    'cookie',
    'captcha',
    'not a bot',
    'http error 403',
    'http error 429',
    'failed to decrypt',
    'dpapi',
    'authentication',
    'auth',
)


def is_permanent_video_unavailable_error(error):
    text = strip_ansi_codes(error).lower()
    return any(keyword in text for keyword in PERMANENT_VIDEO_ERROR_KEYWORDS)


def should_try_without_cookies(error):
    text = strip_ansi_codes(error).lower()
    if is_permanent_video_unavailable_error(text):
        return False
    return any(keyword in text for keyword in AUTH_RECOVERY_ERROR_KEYWORDS)


# ვიწრო, ზუსტი სია — ეს keyword-ები რეალურად ნიშნავს, რომ YouTube-მა
# ნამდვილად მოითხოვა sign-in/ასაკის დადასტურება/bot-შემოწმება.
# განზრახ არ შედის აქ ზოგადი 'cookie(s)'/'http error 403/429'/'auth' სიტყვები,
# რადგან ესენი ხშირად სხვა მიზეზითაც ჩნდება და ტყუილად "გამოაღვიძებდა" cookies-ს.
SIGN_IN_REQUIRED_KEYWORDS = (
    'sign in',
    'age-restricted',
    'confirm your age',
    "confirm you're not a bot",
    'confirm that you’re not a bot',
    'not a bot',
    'members-only',
    "channel's members",
)


def requires_cookie_auth(error):
    """განსაზღვრავს, საჭიროა თუ არა cookies/auth retry."""
    text = strip_ansi_codes(error).lower()

    if is_permanent_video_unavailable_error(text):
        return False

    # Generic HTTP 403/429 is NOT treated as an auth request. On modern YouTube it can
    # be a player-client / GVS PO-token / temporary media URL problem. Turning on stale
    # account cookies for every 403 can make recovery worse and creates misleading logs.
    auth_keywords = (
        'sign in',
        'age-restricted',
        'confirm your age',
        "confirm you're not a bot",
        'confirm that you’re not a bot',
        'not a bot',
        'members-only',
        "channel's members",
        'login required',
    )

    return any(keyword in text for keyword in auth_keywords)


def sanitize_ytdlp_options(opts):
    """yt-dlp options-ის დაცვა ცარიელი/არასწორი cookie მნიშვნელობებისგან."""
    cleaned = dict(opts or {})

    cookie_file = cleaned.get('cookiefile')
    if not cookie_file:
        cleaned.pop('cookiefile', None)
    else:
        cleaned['cookiefile'] = os.path.abspath(os.path.expandvars(os.path.expanduser(str(cookie_file))))

    browser_cookie = cleaned.get('cookiesfrombrowser')
    if not browser_cookie:
        cleaned.pop('cookiesfrombrowser', None)
    elif isinstance(browser_cookie, str):
        cleaned['cookiesfrombrowser'] = (browser_cookie,)
    elif isinstance(browser_cookie, (list, tuple)):
        cleaned['cookiesfrombrowser'] = tuple(item for item in browser_cookie if item)
        if not cleaned['cookiesfrombrowser']:
            cleaned.pop('cookiesfrombrowser', None)
    else:
        cleaned.pop('cookiesfrombrowser', None)

    return cleaned


def create_ytdlp_instance(opts):
    """YoutubeDL ობიექტის უსაფრთხო შექმნა ერთ ადგილზე."""
    module = ensure_yt_dlp_loaded()
    cleaned_opts = sanitize_ytdlp_options(opts)

    # ცენტრალური დაცვა: ყველა YoutubeDL instance-ს ჰქონდეს FFmpeg-ის
    # პირდაპირი მისამართი, მიუხედავად იმისა, რომელ download რეჟიმშია.
    cleaned_opts.setdefault('ffmpeg_location', get_ffmpeg_location() or FFMPEG_DIR)

    ydl_class = getattr(module, 'YoutubeDL')
    return ydl_class(cleaned_opts)


def run_ytdlp_extract_info(url, opts, *, download=False):
    """extract_info without `with ... as ydl` and without repeated inline YoutubeDL calls."""
    ydl_obj = create_ytdlp_instance(opts)
    try:
        return ydl_obj.extract_info(url, download=download)
    finally:
        close_func = getattr(ydl_obj, 'close', None)
        if callable(close_func):
            try:
                close_func()
            except Exception:
                pass


def run_ytdlp_download(urls, opts):
    """download without `with ... as ydl` and without repeated inline YoutubeDL calls."""
    ydl_obj = create_ytdlp_instance(opts)
    try:
        return ydl_obj.download(urls)
    finally:
        close_func = getattr(ydl_obj, 'close', None)
        if callable(close_func):
            try:
                close_func()
            except Exception:
                pass


def _run_ydl_with_cookie_defense(opts, run_attempt, progress_label):
    """cookie-დაცვის საერთო ლოგიკა (extract_info-სთვისაც და download-სთვისაც).

    სტრატეგია — "მხოლოდ საჭიროებისას":
      1) ყოველთვის პირველ რიგში ვცდილობთ cookies-ის ᲒᲐᲠᲔᲨᲔ (სწრაფი, არ
         ხარჯავს/ არ აბრუნებს YouTube session-ს, არ ჰგავს ბოტს).
      2) cookies ერთვება მხოლოდ მაშინ, თუ yt-dlp-ის შეცდომა ცალსახად
         sign-in/ასაკის დადასტურებას/bot-შემოწმებას ითხოვს (requires_cookie_auth).
      3) ყველა სხვა შეცდომაზე (ქსელი, დროებითი, permanent unavailable და ა.შ.)
         cookies საერთოდ არ ერთვება — cookies "იარაღია", რომელიც მხოლოდ საჭირო
         დროს იშვება, არა ყოველ მოთხოვნაზე.

    run_attempt(attempt_opts) -> ფაქტობრივად უშვებს yt-dlp-ს მოცემული opts-ით.
    """
    used_cookies = False
    cookie_attempted = False
    attempt_number = 0

    while True:
        attempt_number += 1
        attempt_opts = prepare_attempt_options(opts, use_cookies=used_cookies)

        try:
            return run_attempt(attempt_opts)

        except Exception as e:
            if is_user_requested_stop_error(e):
                raise UserRequestedStop(USER_STOP_TOKEN)
            if is_permanent_video_unavailable_error(e):
                raise Exception(format_ydl_error(e))
            if is_probably_network_error(e) or not has_internet_connection():
                print('ინტერნეტის პრობლემა დაფიქსირდა {}. მცდელობა #{}'.format(progress_label, attempt_number))
                print('პროცესი არ შეწყდება — ველოდები ინტერნეტის დაბრუნებას...')

                restored = wait_for_network_restore()
                if restored:
                    print('ინტერნეტი დაბრუნდა — თავიდან ვცდი...')
                    continue

                raise Exception(
                    'ინტერნეტი 24 საათის განმავლობაში არ დაბრუნდა და ლოდინის ლიმიტი ამოიწურა.'
                )

            # cookies ჯერ არ გვიცდია, და yt-dlp რეალურად sign-in-ს ითხოვს -> ახლა ჩავრთოთ cookies.
            if not used_cookies and not cookie_attempted and requires_cookie_auth(e):
                cookie_attempted = True
                source_kind, source_value = resolve_cookie_source()
                source_label = make_auth_source_label(source_kind, source_value)

                if source_kind == 'no_cookies':
                    combined = build_cookie_failure_message(source_label, e)
                    raise Exception(format_ydl_error('{}\n\n{}'.format(e, combined)))

                print('YouTube-მა sign-in მოითხოვა — ვრთავ cookies-ს ({}) და ვცდი ხელახლა...'.format(source_label))
                used_cookies = True
                continue

            # cookies უკვე ჩართული იყო ამ მცდელობისას და მაინც ჩავარდა -> საბოლოო შეცდომა.
            if used_cookies:
                source_kind, source_value = resolve_cookie_source()
                source_label = make_auth_source_label(source_kind, source_value)
                combined = build_cookie_failure_message(source_label, e)
                raise Exception(format_ydl_error('{}\n\n{}'.format(e, combined)))

            raise Exception(format_ydl_error(e))


def ydl_extract_info_with_cookie_fallback(url, opts, *, download=False):
    """extract_info + cookie-დაცვა: cookies მხოლოდ მაშინ ერთვება, როცა ნამდვილად საჭიროა."""
    return _run_ydl_with_cookie_defense(
        opts,
        lambda attempt_opts: run_ytdlp_extract_info(url, attempt_opts, download=download),
        'metadata/download ეტაპზე',
    )


def ydl_download_with_cookie_fallback(urls, opts):
    """download + cookie-დაცვა: cookies მხოლოდ მაშინ ერთვება, როცა ნამდვილად საჭიროა."""
    return _run_ydl_with_cookie_defense(
        opts,
        lambda attempt_opts: run_ytdlp_download(urls, attempt_opts),
        'ჩამოტვირთვისას',
    )


def format_ydl_error(error):
    """მეგობრული ტექსტი yt-dlp / cookies / DPAPI / JS runtime / PO Token შეცდომებისთვის.
    ასევე ამ ერთ ცენტრალურ წერტილში ლოგავს სრულ ტექნიკურ დეტალს logs/app.log-ში,
    რადგან ყველა downloader-ფანჯარა საბოლოო შეცდომას სწორედ ამ ფუნქციაზე ატარებს."""
    log_download_problem(error)
    text = strip_ansi_codes(error)
    lowered = text.lower()

    if 'failed to decrypt with dpapi' in text.lower():
        extra = (
            "\n\nBrowser cookie decryption failed on Windows.\n"
            "ეს build ახლა მხოლოდ ერთ auth წყაროს იყენებს.\n"
            "თუ Windows-ზე DPAPI ჭედავს, სცადე Firefox cookies ან fresh youtube-cookies.txt."
        )
        if extra not in text:
            text += extra

    if 'no supported javascript runtime could be found' in lowered:
        extra = (
            "\n\nYouTube-ისთვის JS runtime საჭიროა.\n"
            "ეს build ავტომატურად ეძებს Node/Deno/Bun/QuickJS-ს და ამატებს yt-dlp-ის JS runtime პარამეტრებს.\n"
            "თუ Node დაყენებულია, გაუშვი პროგრამა თავიდან; თუ არა, დააყენე Node 20+ ან Deno."
        )
        if extra not in text:
            text += extra

    if 'po token' in lowered or 'missing a url' in lowered or 'sabr' in lowered or 'http error 403' in lowered:
        pot_provider_ready = _module_exists(POT_PROVIDER_MODULE)
        if pot_provider_ready:
            extra = (
                "\n\nYouTube-ის ნაწილ ფორმატებს დღეს PO Token შეიძლება სჭირდებოდეს.\n"
                "ეს build ავტომატურად ტოკენს აგენერირებს (bgutil-ytdlp-pot-provider + JS runtime), მაგრამ PO Token არ იძლევა 403-ის სრულ გარანტიას — YouTube-მა შეიძლება ეს კონკრეტული ფორმატი/IP დროებით დაბლოკოს.\n"
                "სცადე ისევ რამდენიმე წუთის შემდეგ, ან სცადე სხვა player_client env ცვლადით: YTDLP_YT_PLAYER_CLIENTS=tv,web"
            )
        else:
            extra = (
                "\n\nYouTube-ის ნაწილ ფორმატებს დღეს PO Token შეიძლება სჭირდებოდეს.\n"
                "ეს build ცდილობს PO token-ის ავტომატურ გენერაციას (bgutil-ytdlp-pot-provider), მაგრამ ამისთვის სჭირდება Node.js/Deno/Bun — გადაამოწმე რომ ერთ-ერთი დაყენებულია და გაუშვი პროგრამა თავიდან.\n"
                "ხელით PO token-საც შეგიძლია მიუთითო env ცვლადით:\n"
                "- YTDLP_YT_PO_TOKEN=web.gvs+TOKEN\n"
                "- ან YTDLP_YT_MWEB_GVS_PO_TOKEN=TOKEN"
            )
        if extra not in text:
            text += extra

    if 'http error 429' in lowered or 'not a bot' in lowered:
        extra = (
            "\n\nYouTube-მა IP/cookies რბილად დაგბლოკა.\n"
            "გაიარე CAPTCHA ბრაუზერში, მერე იგივე IP-ით გაუშვი ეს პროგრამა. საჭიროებისას დააყენე YTDLP_SOURCE_ADDRESS და იგივე ბრაუზერის User-Agent."
        )
        if extra not in text:
            text += extra

    log_hint = f"\n\nსრული ტექნიკური დეტალები ჩაწერილია: {get_logs_root()}"
    if log_hint not in text:
        text += log_hint

    return text


def summarize_error_for_ui(error, max_length=280):
    """დიდი yt-dlp traceback/tips ტექსტიდან მოკლე, გასაგები ერთი ხაზის ამოღება."""
    text = strip_ansi_codes(error).replace('\r', '\n')
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    if is_permanent_video_unavailable_error(text):
        for line in lines:
            lowered = line.lower()
            if 'video unavailable' in lowered or 'removed by the uploader' in lowered:
                cleaned = re.sub(r'^\S*ERROR:\S*\s*', '', line, flags=re.I).strip()
                cleaned = re.sub(r'^\[youtube\]\s*', '', cleaned, flags=re.I).strip()
                return cleaned[:max_length]
        return 'Video unavailable: ვიდეო წაშლილია ან აღარ არის ხელმისაწვდომი.'

    skipped_prefixes = (
        'auth method failed:',
        'tips:',
        '1)',
        '2)',
        '3)',
        '4)',
        '-',
    )
    for line in lines:
        lowered = line.lower()
        if lowered.startswith(skipped_prefixes):
            continue
        cleaned = re.sub(r'^\S*ERROR:\S*\s*', '', line, flags=re.I).strip()
        if cleaned:
            return cleaned[:max_length]

    return (lines[0] if lines else str(error or 'უცნობი შეცდომა'))[:max_length]


def get_max_quality_options():
    """YouTube-სთვის გამაგრებული yt-dlp პარამეტრები."""
    opts = {
        # yt-dlp-ს პირდაპირ ვეუბნებით სად არის FFmpeg/FFprobe.
        'ffmpeg_location': get_ffmpeg_location() or FFMPEG_DIR,

        'legacy_server_connect': True,
        'nocheckcertificate': True,
        'retries': 20,
        'fragment_retries': 20,
        'extractor_retries': 6,

        'continuedl': True,
        'nopart': False,
        'overwrites': False,

        # Do not pin a years-old browser User-Agent here. yt-dlp maintains appropriate
        # request headers itself; YTDLP_USER_AGENT can still override it explicitly.

        'socket_timeout': 60,
        'concurrent_fragment_downloads': 8,
        'buffersize': 1024 * 1024,

        'geo_bypass': True,
        'geo_bypass_country': 'US',

        'format_sort': [
            'res',
            'fps',
            'hdr:12',
            'vbr',
            'abr',
            'vcodec',
            'acodec',
            'size',
            'br',
            'proto',
            'ext',
        ],

        'prefer_free_formats': False,
        'check_formats': 'selected',
        'windowsfilenames': True,
    }
    opts = apply_runtime_preferences(opts)
    return apply_cookie_preferences(opts)


def fetch_video_metadata(url, *, noplaylist=False):
    """სწრაფი metadata ექსტრაქცია კონკრეტული ვიდეოსთვის"""
    opts = get_max_quality_options()
    opts.update({
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
    })
    if noplaylist:
        opts['noplaylist'] = True
    return ydl_extract_info_with_cookie_fallback(url, opts, download=False)


def extract_urls_from_text(text):
    """მრავალი ლინკის ტექსტიდან ამოღება, დუბლიკატების გამოტოვებით"""
    urls = []
    seen = set()

    cleaned_text = html.unescape(str(text or '')).replace('\u200b', '').replace('\ufeff', '')
    for raw_line in cleaned_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        matches = re.findall(r'(https?://[^\s<>"\']+|www\.[^\s<>"\']+)', line)
        for match in matches:
            url = match.strip().strip("\"'`")
            while url and url[-1] in '.,;:!?)]}<>':
                url = url[:-1].rstrip()
            if url.startswith('www.'):
                url = 'https://' + url
            try:
                parsed = urlparse(url)
                if not parsed.scheme or not parsed.netloc:
                    continue
            except Exception:
                continue
            if url not in seen:
                seen.add(url)
                urls.append(url)

    return urls


def normalize_search_text(text):
    """Unicode-safe ტექსტის ნორმალიზაცია search/filter-ისთვის"""
    value = unicodedata.normalize('NFKC', str(text or ''))
    value = ''.join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r'\s+', ' ', value).strip()
    return value.casefold()

def split_multi_search_input(text):
    """მრავალძებნის ტექსტის დაყოფა: ახალი ხაზი, მძიმე ან წერტილმძიმე"""
    raw_value = str(text or '').replace('\r', '\n')
    return [part.strip() for part in re.split(r'[\n,;]+', raw_value) if str(part).strip()]


def dedupe_search_terms(raw_terms):
    """საძიებო ფრაზების დუბლიკატების მოცილება normalized შედარებით"""
    terms = []
    seen = set()
    for part in raw_terms or []:
        cleaned = str(part or '').strip()
        normalized = normalize_search_text(cleaned)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        terms.append(cleaned)
    return terms


def parse_multi_search_terms(text):
    """სერჩ ველიდან რამდენიმე საძიებო ფრაზის normalized სია"""
    return [normalize_search_text(term) for term in dedupe_search_terms(split_multi_search_input(text))]


def _collect_search_terms_from_json_node(node, results):
    """JSON-იდან ვიდეო სახელების/საძიებო ფრაზების ამოღება"""
    if isinstance(node, str):
        results.extend(split_multi_search_input(node))
        return

    if isinstance(node, list):
        for item in node:
            _collect_search_terms_from_json_node(item, results)
        return

    if isinstance(node, dict):
        priority_keys = (
            'titles', 'title', 'names', 'name', 'queries', 'query',
            'search', 'searches', 'videos', 'video_titles', 'items',
            'episodes', 'episode_titles'
        )
        used_priority = False
        for key in priority_keys:
            if key in node:
                used_priority = True
                _collect_search_terms_from_json_node(node.get(key), results)
        if not used_priority:
            for value in node.values():
                _collect_search_terms_from_json_node(value, results)


def read_search_terms_from_file(file_path):
    """TXT/JSON ფაილიდან საძიებო ფრაზების წაკითხვა"""
    extension = os.path.splitext(str(file_path or ''))[1].lower()

    if extension == '.json':
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
        raw_terms = []
        _collect_search_terms_from_json_node(data, raw_terms)
        return dedupe_search_terms(raw_terms)

    if extension == '.txt':
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            content = f.read()
        return dedupe_search_terms(split_multi_search_input(content))

    raise ValueError('მხარდაჭერილია მხოლოდ .txt და .json ფაილები')

def normalize_channel_playlists_url(url):
    """არხის URL-ს საჭიროების შემთხვევაში playlists ტაბზე გადავიყვანთ"""
    value = (url or '').strip()
    if not value:
        return value

    if not re.match(r'^https?://', value, re.I):
        value = 'https://' + value.lstrip('/')

    try:
        parsed = urlparse(value)
    except Exception:
        return value

    host = (parsed.netloc or '').lower()
    if 'youtube.com' not in host and 'youtu.be' not in host:
        return value

    path = re.sub(r'/+', '/', parsed.path or '/')
    if '/playlist' in path or 'list=' in (parsed.query or ''):
        return parsed._replace(path=path).geturl()

    tail = path.rstrip('/')
    if tail.endswith(('/videos', '/featured', '/streams', '/shorts', '/live')):
        path = tail.rsplit('/', 1)[0] + '/playlists'
    elif not tail.endswith('/playlists'):
        path = tail + '/playlists' if tail else '/playlists'

    return parsed._replace(path=path).geturl()



def normalize_channel_videos_url(url):
    """არხის URL-ს Videos ტაბზე გადაყვანა, მათ შორის /shorts ან /featured ლინკიდან."""
    value = (url or '').strip()
    if not value:
        return value

    if not re.match(r'^https?://', value, re.I):
        value = 'https://' + value.lstrip('/')

    try:
        parsed = urlparse(value)
    except Exception:
        return value

    host = (parsed.netloc or '').lower()
    if 'youtube.com' not in host and 'youtu.be' not in host:
        return value

    path = re.sub(r'/+', '/', parsed.path or '/')
    tail = path.rstrip('/')

    if tail.endswith('/videos'):
        return parsed._replace(path=tail, query='').geturl()

    if tail.endswith(('/shorts', '/featured', '/streams', '/live', '/playlists')):
        path = tail.rsplit('/', 1)[0] + '/videos'
    elif '/shorts/' in tail:
        parts = [part for part in tail.split('/') if part]
        if len(parts) >= 2:
            path = '/' + '/'.join(parts[:-2] or parts[:1]) + '/videos'
        else:
            path = '/videos'
    else:
        path = tail + '/videos' if tail else '/videos'

    return parsed._replace(path=path, query='').geturl()


def normalize_channel_shorts_url(url):
    """არხის URL-ს Shorts ტაბზე გადაყვანა."""
    value = (url or '').strip()
    if not value:
        return value

    if not re.match(r'^https?://', value, re.I):
        value = 'https://' + value.lstrip('/')

    try:
        parsed = urlparse(value)
    except Exception:
        return value

    host = (parsed.netloc or '').lower()
    if 'youtube.com' not in host and 'youtu.be' not in host:
        return value

    path = re.sub(r'/+', '/', parsed.path or '/')
    tail = path.rstrip('/')

    if tail.endswith('/shorts'):
        return parsed._replace(path=tail, query='').geturl()

    if tail.endswith(('/videos', '/featured', '/streams', '/live', '/playlists')):
        path = tail.rsplit('/', 1)[0] + '/shorts'
    elif '/shorts/' in tail:
        return parsed._replace(path=tail, query='').geturl()
    else:
        path = tail + '/shorts' if tail else '/shorts'

    return parsed._replace(path=path, query='').geturl()




YOUTUBE_SEARCH_RESULTS_LIMIT = 80


def get_youtube_search_results_limit():
    """YouTube search URL-იდან რამდენი შედეგი წამოვიღოთ ერთ ცდაზე."""
    raw_value = os.environ.get('YTDLP_SEARCH_RESULTS_LIMIT', '').strip()
    if raw_value:
        try:
            return max(1, min(300, int(raw_value)))
        except Exception:
            pass
    return YOUTUBE_SEARCH_RESULTS_LIMIT


def extract_youtube_search_query_from_url(value):
    """youtube.com/results?search_query=... ლინკიდან საძიებო ტექსტის ამოღება."""
    raw_value = str(value or '').strip()
    if not raw_value or raw_value.startswith('მაგ:'):
        return ''

    url_value = raw_value
    if not re.match(r'^https?://', url_value, re.I):
        url_value = 'https://' + url_value.lstrip('/')

    try:
        parsed = urlparse(url_value)
    except Exception:
        return ''

    host = (parsed.netloc or '').lower()
    path = (parsed.path or '').rstrip('/') or '/'
    if 'youtube.com' not in host or path != '/results':
        return ''

    query_values = parse_qs(parsed.query or '').get('search_query') or []
    if not query_values:
        return ''

    return unquote_plus(str(query_values[0] or '')).strip()


def is_youtube_search_results_url(value):
    """ამოწმებს, არის თუ არა შეყვანილი ლინკი YouTube search results გვერდი."""
    return bool(extract_youtube_search_query_from_url(value))


def collect_playlist_entries(node, results=None, seen=None):
    """yt-dlp-ის nested შედეგებიდან ყველა პლეილისტის ამოღება"""
    if results is None:
        results = []
    if seen is None:
        seen = set()

    if isinstance(node, dict):
        playlist_id = node.get('id')
        title = node.get('title') or node.get('playlist_title') or 'უცნობი პლეილისტი'
        raw_url = node.get('url') or node.get('webpage_url') or node.get('original_url') or ''

        playlist_url = ''
        if isinstance(raw_url, str) and 'list=' in raw_url:
            playlist_url = raw_url
        elif isinstance(raw_url, str) and raw_url.startswith('VL'):
            playlist_url = f'https://www.youtube.com/playlist?list={raw_url[2:]}'
        elif isinstance(playlist_id, str) and playlist_id:
            if playlist_id.startswith('VL'):
                playlist_url = f'https://www.youtube.com/playlist?list={playlist_id[2:]}'
            elif playlist_id.startswith(('PL', 'UU', 'LL', 'FL', 'RD', 'OLAK5uy_')):
                playlist_url = f'https://www.youtube.com/playlist?list={playlist_id}'

        if playlist_url:
            key = playlist_id or playlist_url
            if key not in seen:
                seen.add(key)
                results.append({'id': playlist_id or '', 'title': title, 'url': playlist_url})

        for subkey in ('entries', 'tabs', 'content', 'contents', 'items', 'data'):
            value = node.get(subkey)
            if isinstance(value, (dict, list)):
                collect_playlist_entries(value, results, seen)

    elif isinstance(node, list):
        for item in node:
            collect_playlist_entries(item, results, seen)

    return results


def get_clipboard_text(window):
    """კლიპბორდიდან ტექსტის უსაფრთხოდ ამოღება"""
    try:
        return window.clipboard_get()
    except Exception:
        return ""


def paste_into_entry(window, entry):
    """Entry-ში სტაბილური paste: Ctrl+V / Shift+Insert / menu"""
    text = get_clipboard_text(window)
    if not text:
        return "break"
    try:
        entry.delete("sel.first", "sel.last")
    except Exception:
        pass
    entry.insert("insert", text)
    return "break"


def paste_into_text(window, text_widget):
    """Text widget-ში სტაბილური paste: Ctrl+V / Shift+Insert / menu"""
    text = get_clipboard_text(window)
    if not text:
        return "break"
    try:
        text_widget.delete("sel.first", "sel.last")
    except Exception:
        pass
    text_widget.insert("insert", text)
    return "break"


def select_all_in_entry(entry):
    """Entry-ის სრული მონიშვნა"""
    entry.select_range(0, "end")
    entry.icursor("end")
    return "break"


def select_all_in_text(text_widget):
    """Text widget-ის სრული მონიშვნა"""
    text_widget.tag_add("sel", "1.0", "end-1c")
    text_widget.mark_set("insert", "1.0")
    text_widget.see("insert")
    return "break"


def handle_keyboard_shortcut(event, window, widget):
    """Ctrl+V / Ctrl+A / Shift+Insert fallback, მათ შორის უცხო keyboard layout-ზე"""
    state = getattr(event, 'state', 0)
    keycode = getattr(event, 'keycode', None)
    keysym = (getattr(event, 'keysym', '') or '').lower()

    ctrl_pressed = bool(state & 0x4)
    shift_pressed = bool(state & 0x1)

    is_entry = isinstance(widget, (tk.Entry, ttk.Entry))
    is_text = isinstance(widget, tk.Text)

    if not (is_entry or is_text):
        return None

    paste_requested = (
        (ctrl_pressed and (keysym == 'v' or keycode == 86)) or
        (shift_pressed and keysym == 'insert')
    )
    if paste_requested:
        if is_text:
            return paste_into_text(window, widget)
        return paste_into_entry(window, widget)

    select_all_requested = ctrl_pressed and (keysym == 'a' or keycode == 65)
    if select_all_requested:
        if is_text:
            return select_all_in_text(widget)
        return select_all_in_entry(widget)

    return None


def canonical_video_key(url=None, entry=None, info=None):
    """ვიდეოს სტაბილური უნიკალური key დუბლიკატების გამოსავლენად"""
    for obj in (info, entry):
        if isinstance(obj, dict):
            vid = obj.get('id')
            if vid:
                return f"id:{vid}"

    candidate = url
    if not candidate and isinstance(entry, dict):
        candidate = entry.get('webpage_url') or entry.get('url')
    if not candidate:
        return None

    candidate = str(candidate).strip()
    if not candidate:
        return None

    if not candidate.startswith('http'):
        if re.fullmatch(r'[A-Za-z0-9_-]{6,}', candidate):
            return f"id:{candidate}"
        return f"url:{candidate}"

    try:
        parsed = urlparse(candidate)
        host = (parsed.netloc or '').lower()
        path = parsed.path or ''

        if 'youtu.be' in host:
            vid = path.strip('/').split('/')[0]
            if vid:
                return f"id:{vid}"

        if 'youtube.com' in host or 'youtube-nocookie.com' in host:
            qs = parse_qs(parsed.query or '')
            vid = (qs.get('v') or [None])[0]
            if vid:
                return f"id:{vid}"

            parts = [part for part in path.split('/') if part]
            if len(parts) >= 2 and parts[0] in {'shorts', 'embed', 'live', 'v'}:
                return f"id:{parts[1]}"

        normalized = parsed._replace(fragment='').geturl()
        return f"url:{normalized}"
    except Exception:
        return f"url:{candidate}"



# ============================
# უსაფრთხო JSON ისტორია / ჩამოტვირთვის გაგრძელება
# ============================

HISTORY_SCHEMA_VERSION = 1
HISTORY_TEMP_DIR_NAME = ".download_tmp"
HISTORY_LOCK_FILE_NAME = ".history.lock"
HISTORY_WRITE_RETRIES = 10
HISTORY_WRITE_RETRY_BASE_SECONDS = 0.08


def get_download_temp_root():
    """yt-dlp-ის დროებითი ფაილები ყოველთვის ინახება უშუალოდ ამ .py ფაილის გვერდით."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    temp_root = os.path.join(script_dir, HISTORY_TEMP_DIR_NAME)
    os.makedirs(temp_root, exist_ok=True)
    return temp_root
HISTORY_ACTIVE_ITEM_STATES = {"running", "downloading", "postprocessing"}
HISTORY_DONE_ITEM_STATES = {"completed", "skipped_duplicate"}
USER_STOP_TOKEN = "__YTDLP_USER_REQUESTED_STOP__"


class UserRequestedStop(Exception):
    """შიდა გამონაკლისი — მომხმარებელმა მიმდინარე ჩამოტვირთვის შეწყვეტა მოითხოვა."""


def is_user_requested_stop_error(error):
    return isinstance(error, UserRequestedStop) or USER_STOP_TOKEN in str(error or "")


def history_now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sanitize_history_name(value, fallback="download_history", max_length=90):
    """Unicode-safe და Windows-safe საქაღალდის/JSON სახელის შექმნა."""
    name = unicodedata.normalize("NFKC", str(value or "")).strip()
    if name.lower().endswith(".json"):
        name = name[:-5]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', name)
    name = re.sub(r'\s+', ' ', name).strip(' ._')

    reserved = {
        'con', 'prn', 'aux', 'nul',
        *(f'com{i}' for i in range(1, 10)),
        *(f'lpt{i}' for i in range(1, 10)),
    }
    if name.casefold() in reserved:
        name = f"_{name}"

    if not name:
        name = fallback
    return name[:max_length].rstrip(' ._') or fallback


def normalize_history_source(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if re.match(r'^https?://', raw, re.I) else 'https://' + raw.lstrip('/'))
        if parsed.netloc:
            parsed = parsed._replace(fragment='')
            return parsed.geturl()
    except Exception:
        pass
    return raw


def build_history_signature(job_type, sources, settings=None):
    normalized_sources = sorted({normalize_history_source(item) for item in (sources or []) if str(item or '').strip()})
    identity_settings = {}
    for key in ('format', 'quality', 'audio_quality', 'size_limit', 'skip_existing_by_name', 'search_mode', 'range_start'):
        if isinstance(settings, dict) and key in settings:
            identity_settings[key] = settings.get(key)
    payload = {
        'job_type': str(job_type or 'download'),
        'sources': normalized_sources,
        'settings': identity_settings,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def make_history_item_key(url=None, entry=None, info=None):
    key = canonical_video_key(url=url, entry=entry, info=info)
    if key:
        return key
    raw = str(url or '')
    if isinstance(entry, dict):
        raw = raw or str(entry.get('url') or entry.get('webpage_url') or entry.get('title') or '')
    digest = hashlib.sha256(raw.encode('utf-8', errors='replace')).hexdigest()[:20]
    return f"item:{digest}"


def build_history_item(url, index, title='', entry=None, info=None, extra=None):
    item = {
        'key': make_history_item_key(url=url, entry=entry, info=info),
        'url': str(url or ''),
        'title': str(title or (entry or {}).get('title') or (info or {}).get('title') or ''),
        'index': int(index or 0),
    }
    if isinstance(extra, dict):
        item.update(extra)
    return item


def _safe_read_json_file(path):
    try:
        with open(path, 'r', encoding='utf-8-sig') as file_obj:
            data = json.load(file_obj)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _history_json_candidates(folder):
    if not os.path.isdir(folder):
        return []
    try:
        return [
            os.path.join(folder, name)
            for name in os.listdir(folder)
            if name.lower().endswith('.json') and not name.lower().endswith('.bak.json')
        ]
    except Exception:
        return []


def find_history_by_signature(base_dir, signature):
    """ავტომატური სახელისას უკვე არსებული იმავე job-ის ისტორიის პოვნა."""
    if not os.path.isdir(base_dir):
        return None
    try:
        children = list(os.scandir(base_dir))[:1000]
    except Exception:
        return None

    for child in children:
        if not child.is_dir(follow_symlinks=False):
            continue
        for json_path in _history_json_candidates(child.path):
            data = _safe_read_json_file(json_path)
            if data and data.get('source_signature') == signature:
                return child.path, json_path
    return None


def _history_path_signature(json_path):
    data = _safe_read_json_file(json_path)
    if data is None:
        data = _safe_read_json_file(str(json_path) + '.bak')
    return data.get('source_signature') if data else None


def derive_auto_history_name(job_type, sources, preferred_title, signature):
    title = sanitize_history_name(preferred_title, fallback='', max_length=60) if preferred_title else ''
    if title:
        return sanitize_history_name(f"{title}_{signature[:8]}")

    first = normalize_history_source((sources or [''])[0] if sources else '')
    source_id = ''
    try:
        parsed = urlparse(first)
        query = parse_qs(parsed.query or '')
        source_id = (query.get('list') or query.get('v') or [''])[0]
        if not source_id:
            parts = [part for part in (parsed.path or '').split('/') if part]
            source_id = parts[-1] if parts else ''
    except Exception:
        source_id = ''

    type_name = sanitize_history_name(job_type or 'download', fallback='download', max_length=30)
    source_id = sanitize_history_name(source_id, fallback='', max_length=40) if source_id else ''
    if source_id:
        return sanitize_history_name(f"{type_name}_{source_id}_{signature[:8]}")
    return sanitize_history_name(f"{type_name}_{len(sources or [])}_{signature[:10]}")


def resolve_history_paths(base_dir, job_type, sources, custom_name='', preferred_title='', settings=None):
    base_dir = os.path.abspath(os.path.expandvars(os.path.expanduser(str(base_dir or '.'))))
    os.makedirs(base_dir, exist_ok=True)
    signature = build_history_signature(job_type, sources, settings)

    custom_name = str(custom_name or '').strip()
    if not custom_name:
        existing = find_history_by_signature(base_dir, signature)
        if existing:
            folder, json_path = existing
            return folder, json_path, signature, os.path.splitext(os.path.basename(json_path))[0]

    desired = sanitize_history_name(
        custom_name or derive_auto_history_name(job_type, sources, preferred_title, signature)
    )

    suffix = 0
    while True:
        name = desired if suffix == 0 else sanitize_history_name(f"{desired}_{suffix + 1}")
        folder = os.path.join(base_dir, name)
        json_path = os.path.join(folder, f"{name}.json")

        if not os.path.exists(folder):
            return folder, json_path, signature, name

        existing_sig = _history_path_signature(json_path)
        if existing_sig == signature:
            return folder, json_path, signature, name

        if existing_sig is None:
            json_files = _history_json_candidates(folder)
            for candidate in json_files:
                if _history_path_signature(candidate) == signature:
                    return folder, candidate, signature, os.path.splitext(os.path.basename(candidate))[0]

        suffix += 1


def _pid_is_running(pid):
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _remove_readonly_and_retry(func, path, exc_info):
    try:
        os.chmod(path, 0o700)
        func(path)
    except Exception:
        pass


def safe_remove_tree(path):
    if not path or not os.path.exists(path):
        return
    for attempt in range(4):
        try:
            try:
                shutil.rmtree(path, onexc=_remove_readonly_and_retry)
            except TypeError:
                # Python < 3.12 არ იცნობს onexc-ს; ძველი onerror-ზე დავბრუნდეთ.
                shutil.rmtree(path, onerror=_remove_readonly_and_retry)
            return
        except Exception:
            if attempt == 3:
                raise
            time.sleep(0.15 * (attempt + 1))


def safe_remove_file(path):
    if not path or not os.path.isfile(path):
        return False
    for attempt in range(4):
        try:
            os.remove(path)
            return True
        except PermissionError:
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        except FileNotFoundError:
            return False
        except Exception:
            if attempt == 3:
                return False
        time.sleep(0.15 * (attempt + 1))
    return False


YTDLP_TEMP_SUFFIXES = (
    '.part', '.ytdl', '.tmp', '.temp', '.download', '.frag',
)


def is_probably_ytdlp_temp_file(path):
    """მხოლოდ yt-dlp-ის დროებითი/შუალედური ნარჩენების ამოცნობა."""
    name = os.path.basename(str(path or '')).lower()
    if not name:
        return False
    if name.endswith(YTDLP_TEMP_SUFFIXES):
        return True
    if '.part-' in name or ('.f' in name and name.endswith('.part')):
        return True
    if name.endswith(('.webp', '.jpg', '.jpeg', '.png')) and any(token in name for token in ('.temp', '.tmp', '.part')):
        return True
    return False


def cleanup_download_temp_artifacts(output_dir, temp_dir=None, started_epoch=None, keep_paths=None):
    """
    დასრულებული item-ის შემდეგ ვშლით მხოლოდ უსაფრთხო დროებით ნარჩენებს:
    - app-ის ცალკე temp საქაღალდეს მთლიანად;
    - output ფოლდერში მხოლოდ yt-dlp-ის temp suffix მქონე ფაილებს;
    - ძველი unrelated temp ფაილები არ იშლება, თუ item-ის დაწყებამდეა შექმნილი.
    """
    removed = 0
    keep = {os.path.abspath(path) for path in (keep_paths or []) if path}

    if temp_dir:
        try:
            if os.path.exists(temp_dir):
                safe_remove_tree(temp_dir)
                removed += 1
        except Exception:
            pass

    output_root = os.path.abspath(os.path.expandvars(os.path.expanduser(str(output_dir or ''))))
    if not output_root or not os.path.isdir(output_root):
        return removed

    try:
        start_time = float(started_epoch or 0)
    except Exception:
        start_time = 0

    for root, dirs, files in os.walk(output_root, topdown=True):
        dirs[:] = [
            d for d in dirs
            if d not in {'.yt_downloader_dedupe', DOWNLOAD_HISTORY_DIR_NAME}
        ]
        for filename in files:
            path = os.path.abspath(os.path.join(root, filename))
            if path in keep or not is_probably_ytdlp_temp_file(path):
                continue
            try:
                if os.path.commonpath([path, output_root]) != output_root:
                    continue
            except Exception:
                continue
            if start_time:
                try:
                    if os.path.getmtime(path) + 5 < start_time:
                        continue
                except Exception:
                    continue
            if safe_remove_file(path):
                removed += 1

    for root, dirs, _files in os.walk(output_root, topdown=False):
        for dirname in dirs:
            if dirname.lower() not in {'tmp', 'temp', '.tmp'}:
                continue
            path = os.path.join(root, dirname)
            try:
                os.rmdir(path)
                removed += 1
            except OSError:
                pass
            except Exception:
                pass

    return removed


def is_probably_media_output(path):
    if not path or not os.path.isfile(path):
        return False
    lower = path.lower()
    if lower.endswith(('.part', '.ytdl', '.tmp', '.temp', '.json', '.lock')):
        return False
    media_extensions = {
        '.mp4', '.mkv', '.webm', '.mov', '.avi', '.flv', '.m4v', '.ts', '.3gp',
        '.mp3', '.m4a', '.aac', '.opus', '.ogg', '.oga', '.wav', '.flac', '.wma',
        '.mka', '.weba',
    }
    if os.path.splitext(lower)[1] not in media_extensions:
        return False
    try:
        return os.path.getsize(path) > 0
    except Exception:
        return False


VIDEO_OUTPUT_EXTENSIONS = {
    '.mp4', '.mkv', '.webm', '.mov', '.avi', '.flv', '.m4v', '.ts', '.3gp'
}

AUDIO_OUTPUT_EXTENSIONS = {
    '.mp3', '.m4a', '.aac', '.opus', '.ogg', '.oga', '.wav', '.flac', '.wma',
    '.mka', '.weba'
}

MEDIA_OUTPUT_EXTENSIONS = VIDEO_OUTPUT_EXTENSIONS | AUDIO_OUTPUT_EXTENSIONS


def get_output_extensions_for_format(format_kind):
    return AUDIO_OUTPUT_EXTENSIONS if str(format_kind) == 'audio' else VIDEO_OUTPUT_EXTENSIONS


class DownloadHistorySession:
    """ერთი ჩამოტვირთვის job-ის უსაფრთხო, ატომური JSON ისტორია."""

    def __init__(self, output_dir, job_type, sources, custom_name='', preferred_title='', settings=None):
        self.settings = dict(settings or {})
        self.sources = [str(item) for item in (sources or [])]
        self.job_type = str(job_type or 'download')

        # ვიდეო/აუდიო რჩება მომხმარებლის არჩეულ საქაღალდეში.
        requested_output_dir = os.path.abspath(
            os.path.expandvars(os.path.expanduser(str(output_dir or '.')))
        )

        # JSON, backup და lock ინახება მხოლოდ პროგრამის გვერდით შექმნილ download_history-ში.
        history_root = get_download_history_root()
        self.history_dir, self.json_path, self.signature, self.name = resolve_history_paths(
            history_root,
            self.job_type,
            self.sources,
            custom_name=custom_name,
            preferred_title=preferred_title,
            settings=self.settings,
        )
        self.backup_path = self.json_path + '.bak'

        # უკვე არსებული ისტორიის გაგრძელებისას იგივე ვიდეოების საქაღალდე გამოვიყენოთ.
        existing_data = _safe_read_json_file(self.json_path)
        if existing_data is None:
            existing_data = _safe_read_json_file(self.backup_path)
        stored_output_dir = ''
        if isinstance(existing_data, dict) and existing_data.get('source_signature') == self.signature:
            stored_output_dir = str(existing_data.get('output_folder') or '').strip()

        self.output_dir = os.path.abspath(
            os.path.expandvars(os.path.expanduser(stored_output_dir or requested_output_dir))
        )

        # დროებითი ჩამოტვირთვები აღარ იქმნება მომხმარებლის არჩეულ output საქაღალდეში.
        # ისინი ინახება პროგრამის გვერდით: .download_tmp/<job-signature>/<item-hash>/
        self.temp_root = os.path.join(get_download_temp_root(), self.signature[:24])

        self.lock_path = os.path.join(self.history_dir, HISTORY_LOCK_FILE_NAME)
        self._mutex = threading.RLock()
        self._last_progress_write = {}
        self._candidate_files = {}
        self._closed = False
        self._last_save_error = None

        os.makedirs(self.history_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)
        self._acquire_lock()
        try:
            self.data = self._load_or_create()
            self._recover_interrupted_items()
            self.data['status'] = 'running'
            self.data['last_run_at'] = history_now_iso()
            self._append_event('job_started', {'pid': os.getpid()})
            self._save()
        except Exception:
            self.close()
            raise

    def _acquire_lock(self):
        lock_payload = json.dumps({
            'pid': os.getpid(),
            'created_at': history_now_iso(),
            'source_signature': self.signature,
        }, ensure_ascii=False)

        for _ in range(2):
            try:
                fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                try:
                    os.write(fd, lock_payload.encode('utf-8'))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return
            except FileExistsError:
                lock_data = _safe_read_json_file(self.lock_path) or {}
                if _pid_is_running(lock_data.get('pid')):
                    raise RuntimeError(
                        f"ეს JSON ისტორია უკვე გამოიყენება სხვა აქტიურ ჩამოტვირთვაში:\n{self.json_path}"
                    )
                try:
                    os.remove(self.lock_path)
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    raise RuntimeError(f"ძველი history lock ვერ გასუფთავდა: {exc}")

        raise RuntimeError('JSON ისტორიის lock ვერ შეიქმნა.')

    def _new_data(self):
        now = history_now_iso()
        return {
            'schema_version': HISTORY_SCHEMA_VERSION,
            'job_name': self.name,
            'job_type': self.job_type,
            'source_signature': self.signature,
            'sources': list(self.sources),
            'settings': dict(self.settings),
            'output_folder': self.output_dir,
            'temp_folder_root': self.temp_root,
            'history_folder': self.history_dir,
            'json_file': self.json_path,
            'status': 'pending',
            'created_at': now,
            'updated_at': now,
            'last_run_at': None,
            'items': [],
            'totals': {
                'total': 0,
                'completed': 0,
                'pending': 0,
                'failed': 0,
                'interrupted': 0,
                'skipped_duplicate': 0,
            },
            'events': [],
        }

    def _load_or_create(self):
        data = _safe_read_json_file(self.json_path)
        recovered_from_backup = False
        if data is None:
            data = _safe_read_json_file(self.backup_path)
            recovered_from_backup = data is not None
        if data is None:
            return self._new_data()

        if data.get('source_signature') != self.signature:
            raise RuntimeError('JSON ისტორია სხვა ლინკების/პარამეტრების job-ს ეკუთვნის.')

        data.setdefault('schema_version', HISTORY_SCHEMA_VERSION)
        data.setdefault('items', [])
        data.setdefault('events', [])
        data.setdefault('settings', dict(self.settings))
        data.setdefault('sources', list(self.sources))
        data.setdefault('totals', {})
        data['output_folder'] = self.output_dir
        data['temp_folder_root'] = self.temp_root
        data['history_folder'] = self.history_dir
        data['json_file'] = self.json_path
        if recovered_from_backup:
            data.setdefault('events', []).append({
                'time': history_now_iso(),
                'type': 'recovered_from_backup',
                'details': {},
            })
        return data

    def _replace_file_with_retry(self, src_path, dst_path):
        last_error = None
        for attempt in range(HISTORY_WRITE_RETRIES):
            try:
                os.replace(src_path, dst_path)
                return True
            except FileNotFoundError:
                return False
            except OSError as exc:
                last_error = exc
                # Windows sometimes keeps the destination busy briefly (AV/indexer/preview).
                time.sleep(HISTORY_WRITE_RETRY_BASE_SECONDS * (attempt + 1))

        try:
            with open(src_path, 'rb') as src_obj:
                payload = src_obj.read()
            with open(dst_path, 'wb') as dst_obj:
                dst_obj.write(payload)
                dst_obj.flush()
                os.fsync(dst_obj.fileno())
            try:
                os.remove(src_path)
            except Exception:
                pass
            return True
        except Exception as fallback_error:
            self._last_save_error = str(fallback_error or last_error)
            return False

    def _write_recovery_copy(self, src_path):
        try:
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            recovery_path = f"{self.json_path}.recovery.{stamp}.{os.getpid()}.json"
            shutil.copy2(src_path, recovery_path)
            self._last_save_error = (
                f"JSON history main file was busy; recovery copy saved: {recovery_path}"
            )
            print(f"გაფრთხილება: JSON history main file busy. Recovery saved: {recovery_path}")
            return recovery_path
        except Exception as exc:
            self._last_save_error = f"JSON history recovery save failed: {exc}"
            print(f"გაფრთხილება: JSON history save failed: {exc}")
            return None

    def _atomic_write(self, payload):
        tmp_path = f"{self.json_path}.tmp.{os.getpid()}.{threading.get_ident()}"
        backup_tmp = f"{self.backup_path}.tmp.{os.getpid()}.{threading.get_ident()}"
        os.makedirs(os.path.dirname(self.json_path), exist_ok=True)

        try:
            with open(tmp_path, 'w', encoding='utf-8', newline='\n') as file_obj:
                json.dump(payload, file_obj, ensure_ascii=False, indent=2)
                file_obj.write('\n')
                file_obj.flush()
                os.fsync(file_obj.fileno())

            # მთავარი JSON იცვლება ატომურად; შემდეგ მისი უახლესი ასლი ინახება .bak-ში.
            if not self._replace_file_with_retry(tmp_path, self.json_path):
                self._write_recovery_copy(tmp_path)
                return
            try:
                shutil.copy2(self.json_path, backup_tmp)
                if not self._replace_file_with_retry(backup_tmp, self.backup_path):
                    self._write_recovery_copy(backup_tmp)
            except Exception:
                try:
                    if os.path.exists(backup_tmp):
                        os.remove(backup_tmp)
                except Exception:
                    pass
        finally:
            for leftover in (tmp_path, backup_tmp):
                try:
                    if os.path.exists(leftover):
                        os.remove(leftover)
                except Exception:
                    pass

    def _save(self):
        with self._mutex:
            self.data['updated_at'] = history_now_iso()
            self._recalculate_totals()
            self._atomic_write(self.data)

    def _append_event(self, event_type, details=None):
        events = self.data.setdefault('events', [])
        events.append({
            'time': history_now_iso(),
            'type': str(event_type),
            'details': dict(details or {}),
        })
        if len(events) > 200:
            del events[:-200]

    def _active_items(self):
        return [item for item in self.data.get('items', []) if item.get('active', True)]

    def _recalculate_totals(self):
        totals = {
            'total': 0,
            'completed': 0,
            'pending': 0,
            'failed': 0,
            'interrupted': 0,
            'skipped_duplicate': 0,
        }
        for item in self._active_items():
            totals['total'] += 1
            status = item.get('status') or 'pending'
            if status in totals:
                totals[status] += 1
            elif status in HISTORY_ACTIVE_ITEM_STATES:
                totals['pending'] += 1
            else:
                totals['pending'] += 1
        self.data['totals'] = totals

    def _item_output_files(self, item):
        found = []
        for stored in item.get('output_files') or []:
            candidate = str(stored or '')
            if not candidate:
                continue
            if not os.path.isabs(candidate):
                candidate = os.path.join(self.output_dir, candidate)
            candidate = os.path.abspath(candidate)
            if is_probably_media_output(candidate):
                found.append(candidate)
        return found

    def _recover_interrupted_items(self):
        changed = False
        for item in self.data.get('items', []):
            if item.get('status') not in HISTORY_ACTIVE_ITEM_STATES:
                continue
            valid_outputs = self._item_output_files(item)
            if valid_outputs:
                item['status'] = 'completed'
                item['completed_at'] = item.get('completed_at') or history_now_iso()
                item['output_files'] = [self._relative_output_path(path) for path in valid_outputs]
                item['last_error'] = None
            else:
                item['status'] = 'interrupted'
                item['interrupted_at'] = history_now_iso()
                item['last_error'] = 'წინა გაშვება შეწყდა დასრულებამდე; ეს ელემენტი თავიდან ჩამოიტვირთება.'
            changed = True
        if changed:
            self._append_event('interrupted_items_recovered', {})

    def _relative_output_path(self, path):
        try:
            absolute = os.path.abspath(path)
            output = os.path.abspath(self.output_dir)
            if os.path.commonpath([absolute, output]) == output:
                return os.path.relpath(absolute, output)
        except Exception:
            pass
        return str(path)

    def ensure_items(self, items):
        with self._mutex:
            existing = {str(item.get('key')): item for item in self.data.get('items', []) if item.get('key')}
            for item in existing.values():
                item['active'] = False

            ordered = []
            seen = set()
            for position, incoming in enumerate(items or [], start=1):
                key = str(incoming.get('key') or '')
                if not key or key in seen:
                    continue
                seen.add(key)
                current = existing.get(key)
                if current is None:
                    current = {
                        'key': key,
                        'status': 'pending',
                        'attempts': 0,
                        'created_at': history_now_iso(),
                        'output_files': [],
                    }
                    existing[key] = current
                current.update({
                    'url': str(incoming.get('url') or current.get('url') or ''),
                    'title': str(incoming.get('title') or current.get('title') or ''),
                    'index': int(incoming.get('index') or position),
                    'active': True,
                })
                for extra_key, extra_value in incoming.items():
                    if extra_key not in {'key', 'url', 'title', 'index'}:
                        current[extra_key] = extra_value
                ordered.append(current)

            inactive = [item for item in existing.values() if not item.get('active')]
            self.data['items'] = ordered + inactive
            self._append_event('items_synchronized', {'active_count': len(ordered)})
            self._save()

    def get_item(self, key):
        for item in self.data.get('items', []):
            if item.get('key') == key:
                return item
        return None

    def is_completed(self, key):
        item = self.get_item(key)
        if not item or item.get('status') not in HISTORY_DONE_ITEM_STATES:
            return False
        if item.get('status') == 'skipped_duplicate':
            return True
        output_files = item.get('output_files') or []
        if not output_files:
            return True
        if self._item_output_files(item):
            return True
        item['status'] = 'pending'
        item['last_error'] = 'JSON-ში დასრულებული იყო, მაგრამ ფაილი აღარ არსებობს; თავიდან ჩამოიტვირთება.'
        self._save()
        return False

    def get_item_temp_dir(self, key):
        digest = hashlib.sha256(str(key).encode('utf-8', errors='replace')).hexdigest()[:20]
        return os.path.join(self.temp_root, digest)

    def _cleanup_empty_temp_dirs(self):
        """დასრულების შემდეგ ცარიელი job/root temp საქაღალდეების მოცილება."""
        for folder in (self.temp_root, os.path.dirname(self.temp_root)):
            try:
                os.rmdir(folder)
            except OSError:
                # არ არის ცარიელი, აღარ არსებობს ან ჯერ სხვა job იყენებს.
                pass
            except Exception:
                pass

    def begin_item(self, key, title='', url='', index=None):
        with self._mutex:
            item = self.get_item(key)
            if item is None:
                item = {
                    'key': key,
                    'url': str(url or ''),
                    'title': str(title or ''),
                    'index': int(index or 0),
                    'status': 'pending',
                    'attempts': 0,
                    'created_at': history_now_iso(),
                    'active': True,
                    'output_files': [],
                }
                self.data.setdefault('items', []).append(item)

            if self.is_completed(key):
                return False

            temp_dir = self.get_item_temp_dir(key)
            safe_remove_tree(temp_dir)
            os.makedirs(temp_dir, exist_ok=True)

            item['status'] = 'downloading'
            item['attempts'] = int(item.get('attempts') or 0) + 1
            item['started_at'] = history_now_iso()
            item['started_epoch'] = time.time()
            item['completed_at'] = None
            item['interrupted_at'] = None
            item['last_error'] = None
            item['progress_percent'] = 0.0
            item['temp_folder'] = temp_dir
            item['output_files'] = []
            if title:
                item['title'] = str(title)
            if url:
                item['url'] = str(url)
            if index is not None:
                item['index'] = int(index)

            self._candidate_files[key] = set()
            self._append_event('item_started', {'key': key, 'attempt': item['attempts']})
            self._save()
            return True

    def update_progress(self, key, percent=None, downloaded_bytes=None, total_bytes=None):
        item = self.get_item(key)
        if not item:
            return
        now = time.monotonic()
        previous = self._last_progress_write.get(key, {'time': 0.0, 'percent': -100.0})
        try:
            numeric_percent = float(percent) if percent is not None else item.get('progress_percent')
        except Exception:
            numeric_percent = item.get('progress_percent')

        if numeric_percent is not None:
            item['progress_percent'] = max(0.0, min(100.0, float(numeric_percent)))
        if downloaded_bytes is not None:
            item['downloaded_bytes'] = int(downloaded_bytes or 0)
        if total_bytes is not None:
            item['total_bytes'] = int(total_bytes or 0)

        should_write = (
            now - float(previous.get('time') or 0) >= 5.0
            or abs(float(item.get('progress_percent') or 0) - float(previous.get('percent') or 0)) >= 5.0
        )
        if should_write:
            self._last_progress_write[key] = {
                'time': now,
                'percent': float(item.get('progress_percent') or 0),
            }
            self._save()

    def mark_postprocessing(self, key):
        item = self.get_item(key)
        if not item:
            return
        if item.get('status') != 'postprocessing':
            item['status'] = 'postprocessing'
            item['progress_percent'] = 100.0
            item['postprocessing_at'] = history_now_iso()
            self._save()

    def _collect_paths_from_hook(self, payload):
        paths = []
        if not isinstance(payload, dict):
            return paths

        for field in ('filename', 'tmpfilename', 'filepath'):
            value = payload.get(field)
            if isinstance(value, str) and value:
                paths.append(value)

        info = payload.get('info_dict')
        if isinstance(info, dict):
            for field in ('filepath', '_filename', 'filename'):
                value = info.get(field)
                if isinstance(value, str) and value:
                    paths.append(value)
            for requested in info.get('requested_downloads') or []:
                if isinstance(requested, dict):
                    for field in ('filepath', 'filename'):
                        value = requested.get(field)
                        if isinstance(value, str) and value:
                            paths.append(value)
            files_to_move = info.get('__files_to_move') or {}
            if isinstance(files_to_move, dict):
                paths.extend([value for value in files_to_move.keys() if isinstance(value, str)])
                paths.extend([value for value in files_to_move.values() if isinstance(value, str)])
        return paths

    def record_hook_paths(self, key, payload):
        candidates = self._candidate_files.setdefault(key, set())
        output_root = os.path.abspath(self.output_dir)
        for raw_path in self._collect_paths_from_hook(payload):
            path = os.path.abspath(raw_path if os.path.isabs(raw_path) else os.path.join(self.output_dir, raw_path))
            try:
                if os.path.commonpath([path, output_root]) != output_root:
                    continue
            except Exception:
                continue
            if HISTORY_TEMP_DIR_NAME in os.path.relpath(path, output_root).split(os.sep):
                continue
            candidates.add(path)

    def make_progress_hook(self, key, stop_checker=None):
        def hook(payload):
            if callable(stop_checker) and stop_checker():
                self.mark_interrupted(key, 'მომხმარებელმა ჩამოტვირთვა შეაჩერა.')
                raise UserRequestedStop(USER_STOP_TOKEN)

            status = payload.get('status')
            self.record_hook_paths(key, payload)
            if status == 'downloading':
                total = payload.get('total_bytes') or payload.get('total_bytes_estimate')
                downloaded = payload.get('downloaded_bytes')
                percent = None
                try:
                    if total and downloaded is not None:
                        percent = (float(downloaded) / float(total)) * 100.0
                    else:
                        percent = float(str(payload.get('_percent_str') or '0').strip().replace('%', ''))
                except Exception:
                    percent = None
                self.update_progress(key, percent, downloaded, total)
            elif status == 'finished':
                self.mark_postprocessing(key)
        return hook

    def make_postprocessor_hook(self, key, stop_checker=None):
        def hook(payload):
            if callable(stop_checker) and stop_checker():
                self.mark_interrupted(key, 'მომხმარებელმა ჩამოტვირთვა შეაჩერა.')
                raise UserRequestedStop(USER_STOP_TOKEN)
            self.record_hook_paths(key, payload)
            if payload.get('status') in {'started', 'processing'}:
                self.mark_postprocessing(key)
        return hook

    def mark_completed(self, key, extra_paths=None):
        with self._mutex:
            item = self.get_item(key)
            if not item:
                return
            candidates = set(self._candidate_files.get(key, set()))
            for path in extra_paths or []:
                if path:
                    candidates.add(os.path.abspath(path))
            final_paths = sorted(path for path in candidates if is_probably_media_output(path))
            if not final_paths:
                try:
                    started_epoch = float(item.get('started_epoch') or 0)
                    for name in os.listdir(self.output_dir):
                        candidate = os.path.join(self.output_dir, name)
                        if not is_probably_media_output(candidate):
                            continue
                        if started_epoch and os.path.getmtime(candidate) + 2 < started_epoch:
                            continue
                        final_paths.append(candidate)
                    final_paths = sorted(set(final_paths))
                except Exception:
                    final_paths = []

            item['status'] = 'completed'
            item['progress_percent'] = 100.0
            item['completed_at'] = history_now_iso()
            item['last_error'] = None
            item['output_files'] = [self._relative_output_path(path) for path in final_paths]
            item['temp_folder'] = None
            self._append_event('item_completed', {'key': key, 'files': item['output_files']})
            self._save()

            try:
                removed = cleanup_download_temp_artifacts(
                    self.output_dir,
                    temp_dir=self.get_item_temp_dir(key),
                    started_epoch=item.get('started_epoch'),
                    keep_paths=final_paths,
                )
                if removed:
                    self._append_event('temp_cleaned', {'key': key, 'removed': removed})
                    self._save()
            except Exception:
                pass
            self._cleanup_empty_temp_dirs()

    def mark_failed(self, key, error):
        item = self.get_item(key)
        if not item:
            return
        item['status'] = 'failed'
        item['failed_at'] = history_now_iso()
        item['last_error'] = str(error or '')[:4000]
        self._append_event('item_failed', {'key': key, 'error': item['last_error'][:500]})
        self._save()
        try:
            safe_remove_tree(self.get_item_temp_dir(key))
        except Exception:
            pass
        self._cleanup_empty_temp_dirs()

    def mark_interrupted(self, key, reason='ჩამოტვირთვა შეწყდა.'):
        item = self.get_item(key)
        if not item or item.get('status') in HISTORY_DONE_ITEM_STATES:
            return
        item['status'] = 'interrupted'
        item['interrupted_at'] = history_now_iso()
        item['last_error'] = str(reason or '')[:4000]
        self._append_event('item_interrupted', {'key': key, 'reason': item['last_error'][:500]})
        self._save()
        try:
            safe_remove_tree(self.get_item_temp_dir(key))
        except Exception:
            pass
        self._cleanup_empty_temp_dirs()

    def mark_skipped_duplicate(self, key, duplicate_of=None):
        item = self.get_item(key)
        if not item:
            return
        item['status'] = 'skipped_duplicate'
        item['completed_at'] = history_now_iso()
        item['duplicate_of'] = duplicate_of
        item['last_error'] = None
        self._append_event('item_skipped_duplicate', {'key': key, 'duplicate_of': duplicate_of})
        self._save()
        try:
            safe_remove_tree(self.get_item_temp_dir(key))
        except Exception:
            pass
        self._cleanup_empty_temp_dirs()

    def finalize(self, interrupted=False):
        with self._mutex:
            active_items = self._active_items()
            if interrupted:
                self.data['status'] = 'interrupted'
            elif active_items and all(item.get('status') in HISTORY_DONE_ITEM_STATES for item in active_items):
                self.data['status'] = 'completed'
            elif any(item.get('status') in {'failed', 'interrupted'} for item in active_items):
                self.data['status'] = 'partial'
            else:
                self.data['status'] = 'pending'
            self.data['finished_at'] = history_now_iso()
            self._append_event('job_finished', {'status': self.data['status']})
            self._save()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if os.path.exists(self.lock_path):
                lock_data = _safe_read_json_file(self.lock_path)
                if lock_data is None or int(lock_data.get('pid') or 0) == os.getpid():
                    os.remove(self.lock_path)
        except Exception:
            pass
        self._cleanup_empty_temp_dirs()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def prepare_history_download_options(opts, session, item_key, output_template,
                                     ui_progress_hook=None, ui_postprocessor_hook=None,
                                     stop_checker=None):
    """yt-dlp-ს აძლევს თითო ელემენტის ცალკე temp საქაღალდეს და JSON hook-ებს."""
    prepared = dict(opts or {})
    if session is None:
        prepared['outtmpl'] = output_template
        if ui_progress_hook:
            prepared['progress_hooks'] = [ui_progress_hook]
        if ui_postprocessor_hook:
            prepared['postprocessor_hooks'] = [ui_postprocessor_hook]
        return prepared

    filename_template = str(output_template or '%(title)s.%(ext)s').replace('\\', '/')
    filename_template = filename_template.rsplit('/', 1)[-1] or '%(title)s.%(ext)s'

    prepared['outtmpl'] = filename_template
    paths = dict(prepared.get('paths') or {})
    paths['home'] = session.output_dir
    paths['temp'] = session.get_item_temp_dir(item_key)
    prepared['paths'] = paths

    # History job-ში მიმდინარე ელემენტი ყოველთვის თავიდან იწყება.
    prepared['continuedl'] = False
    prepared['nopart'] = False
    prepared['overwrites'] = False

    progress_hooks = [session.make_progress_hook(item_key, stop_checker=stop_checker)]
    if ui_progress_hook:
        progress_hooks.append(ui_progress_hook)
    prepared['progress_hooks'] = progress_hooks

    post_hooks = [session.make_postprocessor_hook(item_key, stop_checker=stop_checker)]
    if ui_postprocessor_hook:
        post_hooks.append(ui_postprocessor_hook)
    prepared['postprocessor_hooks'] = post_hooks
    return prepared


def build_download_history_settings(format_kind, quality, audio_quality, size_limit=False, skip_existing_by_name=True, **extra):
    settings = {
        'format': str(format_kind or 'video'),
        'quality': str(quality or 'best'),
        'audio_quality': str(audio_quality or '320'),
        'size_limit': bool(size_limit),
        'skip_existing_by_name': bool(skip_existing_by_name),
    }
    settings.update(extra)
    return settings


def add_skip_existing_name_control(parent, variable):
    """ყველა download ფანჯრის საერთო checkbox: იგივე სახელის ფაილი თუ არსებობს, გამოტოვოს."""
    check = ttk.Checkbutton(
        parent,
        text="თუ ამ ფოლდერში იგივე სახელით ფაილი უკვე არის, გამოტოვოს",
        variable=variable,
        style="Card.TCheckbutton",
    )
    check.pack(anchor="w", padx=12, pady=(0, 10))
    ttk.Label(
        parent,
        text="ჩართულია ნაგულისხმევად: თავიდან აღარ ჩამოწერს უკვე შენახულ ვიდეოს/აუდიოს იმავე სახელით. ასევე ამოიცნობს Unicode/font განსხვავებას, მაგალითად 𝐓𝐢𝐭𝐥𝐞 / Ｔｉｔｌｅ / Title.",
        style="Info.TLabel",
        wraplength=680,
    ).pack(anchor="w", padx=12, pady=(0, 12))
    return check



def add_history_name_controls(parent, variable, bind_callback=None):
    """შენახვის ბარათში JSON ისტორიის სახელის ველი."""
    ttk.Label(
        parent,
        text="JSON ისტორიის სახელი (არასავალდებულო)",
        style="CardBold.TLabel",
    ).pack(anchor="w", padx=12, pady=(2, 6))

    entry = tk.Entry(
        parent,
        textvariable=variable,
        font=("Segoe UI", 10),
        bg=ModernStyle.BG_INPUT,
        fg=ModernStyle.TEXT,
        insertbackground=ModernStyle.ACCENT_GLOW,
        relief="flat",
    )
    entry.pack(fill="x", padx=12, ipady=8, pady=(0, 5))
    if callable(bind_callback):
        bind_callback(entry)

    ttk.Label(
        parent,
        text="ცარიელი დატოვე ავტომატური სახელისთვის. JSON შეინახება პროგრამის .py ფაილის გვერდით შექმნილ download_history საქაღალდეში; ვიდეოების ადგილი არ შეიცვლება.",
        style="Info.TLabel",
        wraplength=680,
    ).pack(anchor="w", padx=12, pady=(0, 12))
    return entry


UI_THREAD_ID = threading.get_ident()
_UI_CALLBACK_QUEUE = queue.Queue()
_UI_QUEUE_PUMP_INSTALLED = set()


def _owner_window_exists(owner):
    try:
        return bool(owner and getattr(owner, 'window', None) and owner.window.winfo_exists())
    except Exception:
        return False


def _run_queued_ui_callbacks(limit=100):
    """Worker thread-ებიდან დაგროვილი UI callback-ების უსაფრთხოდ გაშვება მთავარ Tk thread-ზე."""
    ran = 0
    while ran < limit:
        try:
            owner, callback = _UI_CALLBACK_QUEUE.get_nowait()
        except queue.Empty:
            break
        try:
            if _owner_window_exists(owner):
                callback()
        except Exception as exc:
            log_internal_error('UI callback', exc)
        ran += 1
    return ran


def install_ui_queue_pump(root):
    """Tk mainloop-ში ერთხელ აყენებს poller-ს, რომ worker thread-ებმა UI არ გააფუჭონ."""
    try:
        root_id = str(root)
        if root_id in _UI_QUEUE_PUMP_INSTALLED:
            return
        _UI_QUEUE_PUMP_INSTALLED.add(root_id)
        state = {'stopped': False, 'after_id': None}

        def stop_pump(event=None):
            try:
                if event is not None and getattr(event, 'widget', None) is not root:
                    return
                state['stopped'] = True
                after_id = state.get('after_id')
                if after_id:
                    try:
                        root.after_cancel(after_id)
                    except Exception:
                        pass
            except Exception:
                pass

        def pump():
            if state.get('stopped'):
                return
            try:
                _run_queued_ui_callbacks()
            finally:
                try:
                    if not state.get('stopped') and root.winfo_exists():
                        state['after_id'] = root.after(30, pump)
                except Exception:
                    state['stopped'] = True

        try:
            root.bind('<Destroy>', stop_pump, add='+')
        except Exception:
            pass
        state['after_id'] = root.after(30, pump)
    except Exception as exc:
        log_internal_error('install_ui_queue_pump', exc)

def schedule_ui(owner, callback):
    """UI update-ების thread-safe დაგეგმვა; დახურული ფანჯრის შემთხვევაში callback გამოტოვდება."""
    if not callable(callback):
        return False

    if threading.get_ident() == UI_THREAD_ID:
        try:
            if _owner_window_exists(owner):
                owner.window.after(0, callback)
                return True
        except Exception as exc:
            log_internal_error('schedule_ui direct after', exc)
            try:
                if _owner_window_exists(owner):
                    callback()
                    return True
            except Exception as callback_exc:
                log_internal_error('schedule_ui direct callback', callback_exc)
        return False

    try:
        _UI_CALLBACK_QUEUE.put((owner, callback))
        return True
    except Exception as exc:
        log_internal_error('schedule_ui queued', exc)
        return False


def capture_download_settings(owner):
    """Download worker-ისთვის Tk variable-ების plain snapshot, რომ background thread-მა StringVar.get() არ გამოიძახოს."""
    mapping = {
        'download_path': 'download_path',
        'history_name': 'history_name_var',
        'format': 'format_var',
        'quality': 'quality_var',
        'audio_quality': 'audio_quality_var',
        'limit_size': 'limit_size_var',
        'skip_existing_name': 'skip_existing_name_var',
        'numbered_filenames': 'numbered_filenames_var',
        'reverse_playlist_order': 'reverse_playlist_order_var',
    }
    snapshot = {}
    for key, attr_name in mapping.items():
        var = getattr(owner, attr_name, None)
        if var is None:
            continue
        try:
            snapshot[key] = var.get()
        except Exception as exc:
            log_internal_error(f'capture setting {key}', exc)
    return snapshot


def get_owner_setting(owner, key, variable=None, default=None) -> Any:
    """Main thread-ზე Tk variable-დან, worker thread-ზე snapshot-იდან იღებს მნიშვნელობას."""
    if threading.get_ident() != UI_THREAD_ID:
        snapshot = getattr(owner, '_download_settings_snapshot', None)
        if isinstance(snapshot, dict) and key in snapshot:
            return snapshot[key]
    if variable is not None:
        try:
            return variable.get()
        except Exception as exc:
            log_internal_error(f'get setting {key}', exc)
    return default

def close_downloader_window(owner):
    """ფანჯრის დახურვისას აქტიური ელემენტის interrupted სტატუსის შენახვა."""
    try:
        owner.cancel_requested = True
        if hasattr(owner, 'stop_download'):
            owner.stop_download = True
        session = getattr(owner, 'active_history_session', None)
        item_key = getattr(owner, 'active_history_item_key', None)
        if session is not None and item_key:
            session.mark_interrupted(item_key, 'ფანჯარა დაიხურა ჩამოტვირთვის დასრულებამდე.')
    except Exception:
        pass
    try:
        owner.window.destroy()
    except Exception:
        pass

def make_unique_folder_path(base_dir, folder_name):
    """ერთი და იგივე სახელის ფოლდერებს უნიკალურ suffix-ს ამატებს: ვანო, ვანო1, ვანო2..."""
    safe_name = sanitize_history_name(folder_name, fallback='playlist', max_length=80)
    candidate = os.path.join(base_dir, safe_name)
    suffix = 1

    while os.path.exists(candidate):
        candidate = os.path.join(base_dir, f"{safe_name}{suffix}")
        suffix += 1

    return candidate


def resolve_output_folder_path(base_dir, folder_name, reuse_existing=False):
    """reuse_existing=True დროს იგივე სახელის ფოლდერს იყენებს, რომ არსებული ფაილები დაინახოს."""
    safe_name = sanitize_history_name(folder_name, fallback='playlist', max_length=80)
    candidate = os.path.join(base_dir, safe_name)
    if reuse_existing and os.path.isdir(candidate):
        return candidate
    return make_unique_folder_path(base_dir, safe_name)


def build_numbered_output_template(output_path, index, total_count=1, numbered_only=True):
    """ფაილის სახელის template: 001.ext ან 001 - title.ext."""
    try:
        number = int(index or 1)
    except Exception:
        number = 1
    try:
        pad_width = max(3, len(str(int(total_count or 1))))
    except Exception:
        pad_width = 3
    prefix = f"{number:0{pad_width}d}"
    filename = f"{prefix}.%(ext)s" if numbered_only else f"{prefix} - %(title)s.%(ext)s"
    return os.path.join(output_path, filename)


# Unicode “font” / look-alike სიმბოლოების რუკა დუბლიკატის ამოცნობისთვის.
# NFKC უკვე აერთებს ბევრ სტილიზებულ ასოს: 𝐀/𝓐/Ａ -> A.
# დამატებით ვასწორებთ ხშირ Cyrillic/Greek homoglyph-ებს, რომლებიც თვალით Latin-ს ჰგავს.
VISUAL_CONFUSABLE_TRANSLATION = str.maketrans({
    # Cyrillic -> Latin look-alikes
    'А': 'A', 'а': 'a', 'В': 'B', 'Е': 'E', 'е': 'e', 'Ё': 'E', 'ё': 'e',
    'З': '3', 'з': '3', 'І': 'I', 'і': 'i', 'Ї': 'I', 'ї': 'i', 'Ј': 'J', 'ј': 'j',
    'К': 'K', 'к': 'k', 'М': 'M', 'м': 'm', 'Н': 'H', 'н': 'h', 'О': 'O', 'о': 'o',
    'Р': 'P', 'р': 'p', 'С': 'C', 'с': 'c', 'Т': 'T', 'т': 't', 'У': 'Y', 'у': 'y',
    'Х': 'X', 'х': 'x', 'Ь': 'b', 'ь': 'b', 'Ѕ': 'S', 'ѕ': 's', 'Ӏ': 'I',

    # Greek -> Latin look-alikes
    'Α': 'A', 'α': 'a', 'Β': 'B', 'β': 'b', 'Ε': 'E', 'ε': 'e', 'Ζ': 'Z', 'ζ': 'z',
    'Η': 'H', 'η': 'h', 'Ι': 'I', 'ι': 'i', 'Κ': 'K', 'κ': 'k', 'Μ': 'M', 'μ': 'm',
    'Ν': 'N', 'ν': 'v', 'Ο': 'O', 'ο': 'o', 'Ρ': 'P', 'ρ': 'p', 'Τ': 'T', 'τ': 't',
    'Υ': 'Y', 'υ': 'u', 'Χ': 'X', 'χ': 'x',
})


def normalize_unicode_font_text(value):
    """
    სახელი გადააქვს შედარების ფორმაში ისე, რომ font/style სხვაობამ დუბლიკატი არ დამალოს.
    მაგალითები: 𝐓𝐢𝐭𝐥𝐞 / 𝓣𝓲𝓽𝓵𝓮 / Ｔｉｔｌｅ / Тitle -> title.
    """
    text = unicodedata.normalize('NFKC', str(value or ''))
    text = text.translate(VISUAL_CONFUSABLE_TRANSLATION)

    cleaned_chars = []
    for ch in unicodedata.normalize('NFKD', text):
        category = unicodedata.category(ch)
        # Cf: zero-width/variation selectors; M*: accents/combining marks.
        # ეს საჭიროა, რომ fancy font ან accent სხვაობამ იგივე სახელი არ გაატაროს.
        if category == 'Cf' or category.startswith('M'):
            continue
        cleaned_chars.append(ch)

    return unicodedata.normalize('NFKC', ''.join(cleaned_chars))


def normalize_existing_media_stem(value):
    stem = os.path.splitext(os.path.basename(str(value or '')))[0]
    stem = normalize_unicode_font_text(stem)
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', stem)
    stem = re.sub(r'[\u2010-\u2015]+', '-', stem)
    stem = re.sub(r'[\s_\-.]+', ' ', stem).strip(' ._-')
    return stem.casefold()


def compact_media_stem(value):
    normalized = normalize_existing_media_stem(value)
    return ''.join(ch for ch in normalized if ch.isalnum())


def _media_name_number_tokens(value):
    return re.findall(r'\d+', normalize_existing_media_stem(value))


def is_near_same_media_stem(left, right):
    """ძალიან მკაცრი fallback თითქმის იდენტური სახელებისთვის; ციფრები აუცილებლად უნდა ემთხვეოდეს."""
    left_norm = normalize_existing_media_stem(left)
    right_norm = normalize_existing_media_stem(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True

    left_compact = compact_media_stem(left_norm)
    right_compact = compact_media_stem(right_norm)
    if not left_compact or not right_compact:
        return False
    if left_compact == right_compact:
        return True

    # არ დავუშვათ Episode 1 / Episode 2 ან Part 01 / Part 02 შეცდომით დუბლიკატად ჩაითვალოს.
    if _media_name_number_tokens(left_norm) != _media_name_number_tokens(right_norm):
        return False

    longest = max(len(left_compact), len(right_compact))
    if longest < 18:
        return False
    if abs(len(left_compact) - len(right_compact)) > max(2, int(longest * 0.04)):
        return False

    return difflib.SequenceMatcher(None, left_compact, right_compact).ratio() >= 0.985


def strip_common_media_suffixes(value):
    """ხშირი suffix-ების მოცილება, რომ title და უკვე შენახული filename უკეთ დაემთხვეს."""
    stem = os.path.splitext(os.path.basename(str(value or '')))[0]
    patterns = [
        r'\s*\[[A-Za-z0-9_-]{6,}\]\s*$',
        r'\s*\([A-Za-z0-9_-]{6,}\)\s*$',
        r'\s*-\s*YouTube\s*$',
        r'\s*\|\s*YouTube\s*$',
    ]
    for pattern in patterns:
        stem = re.sub(pattern, '', stem, flags=re.I).strip()
    return stem or value


def render_output_stem_candidates(output_template, title):
    """outtmpl + title -> სავარაუდო final stem-ები, რომ იგივე სახელის ფაილი ვიპოვოთ."""
    title = str(title or '').strip() or 'video'
    title_values = {
        title,
        strip_common_media_suffixes(title),
        sanitize_history_name(title, fallback='video', max_length=220),
        sanitize_history_name(strip_common_media_suffixes(title), fallback='video', max_length=220),
    }
    if callable(ytdlp_sanitize_filename):
        for restricted in (False, True):
            try:
                sanitized = ytdlp_sanitize_filename(title, restricted=restricted)
            except TypeError:
                try:
                    sanitized = ytdlp_sanitize_filename(title)
                except Exception:
                    sanitized = ''
            except Exception:
                sanitized = ''
            if sanitized:
                title_values.add(str(sanitized))

    template_name = os.path.basename(str(output_template or '%(title)s.%(ext)s').replace('\\', '/'))
    template_without_ext = re.sub(r'\.%\([^)]+\)s$', '', template_name)

    candidates = set()
    for title_value in title_values:
        rendered = template_without_ext
        rendered = rendered.replace('%(title)s', title_value)
        rendered = rendered.replace('%(fulltitle)s', title_value)
        rendered = re.sub(r'%\([^)]+\)s', '', rendered)
        rendered = re.sub(r'\s+', ' ', rendered).strip(' ._-')
        if rendered:
            candidates.add(rendered)

    candidates.update(title_values)
    return {candidate for candidate in candidates if str(candidate or '').strip()}


def find_existing_media_by_output_name(output_dir, output_template, title, format_kind='video'):
    """იმავე output ფოლდერში იგივე სახელის დასრულებული მედიის პოვნა."""
    output_dir = os.path.abspath(os.path.expandvars(os.path.expanduser(str(output_dir or ''))))
    if not output_dir or not os.path.isdir(output_dir):
        return None

    allowed_extensions = get_output_extensions_for_format(format_kind)
    candidate_stems = render_output_stem_candidates(output_template, title)
    normalized_candidates = {normalize_existing_media_stem(item) for item in candidate_stems}
    compact_candidates = {compact_media_stem(item) for item in candidate_stems}
    normalized_candidates.discard('')
    compact_candidates.discard('')

    try:
        entries = list(os.scandir(output_dir))
    except Exception:
        return None

    for entry in entries:
        try:
            if not entry.is_file():
                continue
            path = entry.path
            if is_probably_ytdlp_temp_file(path):
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            if ext not in allowed_extensions:
                continue
            if not is_probably_media_output(path):
                continue

            stripped_name = strip_common_media_suffixes(entry.name)
            stem = normalize_existing_media_stem(entry.name)
            compact_stem = compact_media_stem(entry.name)
            stripped_stem = normalize_existing_media_stem(stripped_name)
            compact_stripped_stem = compact_media_stem(stripped_name)

            if (
                stem in normalized_candidates
                or stripped_stem in normalized_candidates
                or (compact_stem and compact_stem in compact_candidates)
                or (compact_stripped_stem and compact_stripped_stem in compact_candidates)
                or any(is_near_same_media_stem(entry.name, candidate) for candidate in candidate_stems)
                or any(is_near_same_media_stem(stripped_name, candidate) for candidate in candidate_stems)
            ):
                return path
        except Exception:
            continue

    return None


def is_existing_file_skip_marker(value):
    return str(value or '').startswith('existing_file:')


def get_duplicate_archive_bucket(format_kind="video", quality="best"):
    """დუბლიკატების არქივის სახელის სტაბილური bucket."""
    kind = "audio" if str(format_kind) == "audio" else "video"

    if kind == "audio":
        quality_token = normalize_audio_quality(quality)
        return f"{kind}_mp3_{quality_token}"

    height = normalize_quality_height(quality)
    quality_token = f"{height}p" if height else "best"
    return f"{kind}_{quality_token}"


def get_duplicate_archive_path(output_path, format_kind="video", quality="best"):
    """იმავე საქაღალდეში ვინახავთ ჩამოწერილი ვიდეოების key-ებს."""
    bucket = get_duplicate_archive_bucket(format_kind, quality)
    archive_dir = os.path.join(output_path, ".yt_downloader_dedupe")
    return os.path.join(archive_dir, f"{bucket}.txt")


def load_duplicate_archive_keys(archive_path):
    """წინასწარ ჩაწერილი ვიდეოების key-ების წამოღება."""
    keys = set()
    if not archive_path or not os.path.exists(archive_path):
        return keys

    try:
        with open(archive_path, 'r', encoding='utf-8') as f:
            for line in f:
                key = line.strip()
                if key:
                    keys.add(key)
    except Exception:
        pass

    return keys


def append_duplicate_archive_key(archive_path, key, cache=None):
    """ახალი key-ის შენახვა ისე, რომ შემდეგ გაშვებაზეც გამოტოვდეს."""
    if not archive_path or not key:
        return

    if cache is not None and key in cache:
        return

    os.makedirs(os.path.dirname(archive_path), exist_ok=True)
    with open(archive_path, 'a', encoding='utf-8') as f:
        f.write(key + '\n')

    if cache is not None:
        cache.add(key)


def format_duration_hms(seconds):
    """წამები -> გამოსაჩენი HH:MM:SS ან MM:SS ტექსტი"""
    if seconds is None:
        return "--:--"

    try:
        total_seconds = max(0, int(round(float(seconds))))
    except Exception:
        return "--:--"

    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def clamp_float(value, minimum=0.0, maximum=1.0):
    """რიცხვის უსაფრთხო შეზღუდვა დიაპაზონში"""
    try:
        value = float(value)
    except Exception:
        return minimum
    return max(minimum, min(maximum, value))


def get_elapsed_seconds(started_at):
    """monotonic start-იდან გასული წამები"""
    if not started_at:
        return None
    return max(0.0, time.monotonic() - started_at)


def estimate_total_seconds(started_at, percent=None, eta_seconds=None, remaining_seconds=None, total_seconds_override=None):
    """გამოითვლის სავარაუდო მთლიან დროს პროცენტით, ETA-თი ან override-ით"""
    elapsed = get_elapsed_seconds(started_at)
    if elapsed is None:
        return None, None

    total = None
    if total_seconds_override is not None:
        try:
            total = max(elapsed, float(total_seconds_override))
        except Exception:
            total = None
    elif remaining_seconds is not None:
        try:
            total = elapsed + max(0.0, float(remaining_seconds))
        except Exception:
            total = None
    else:
        try:
            if eta_seconds is not None:
                eta_value = float(eta_seconds)
                if eta_value >= 0:
                    total = elapsed + eta_value
            elif percent is not None:
                percent_value = float(percent)
                if percent_value > 0:
                    total = elapsed / (percent_value / 100.0)
        except Exception:
            total = None

    return elapsed, total


def build_timing_text(started_at, percent=None, eta_seconds=None, remaining_seconds=None, total_seconds_override=None, display_percent=None):
    """სტატუსისთვის ერთიანი დროის ტექსტი. display_percent მითითების შემთხვევაში ტექსტში ჩნდება პროცენტული მაჩვენებელიც."""
    elapsed, total = estimate_total_seconds(
        started_at,
        percent=percent,
        eta_seconds=eta_seconds,
        remaining_seconds=remaining_seconds,
        total_seconds_override=total_seconds_override,
    )
    elapsed_text = format_duration_hms(elapsed)
    total_text = format_duration_hms(total)

    remaining_value = None
    try:
        if remaining_seconds is not None:
            remaining_value = max(0.0, float(remaining_seconds))
        elif total is not None and elapsed is not None:
            remaining_value = max(0.0, float(total) - float(elapsed))
    except Exception:
        remaining_value = None
    remaining_text = format_duration_hms(remaining_value)

    percent_prefix = ''
    if display_percent is not None:
        try:
            percent_prefix = f"{clamp_float(display_percent, 0.0, 100.0):.0f}% | "
        except Exception:
            percent_prefix = ''
    return f"{percent_prefix}გასულია: {elapsed_text} | დარჩენილი: {remaining_text} | სულ: {total_text}"


def join_status_with_timing(status_text, started_at, percent=None, eta_seconds=None, remaining_seconds=None, total_seconds_override=None, display_percent=None):
    """სტატუსს ამატებს გასულ/სავარაუდო მთლიან დროს. display_percent მითითების შემთხვევაში ცხადად აჩვენებს პროცენტსაც."""
    timing = build_timing_text(
        started_at,
        percent=percent,
        eta_seconds=eta_seconds,
        remaining_seconds=remaining_seconds,
        total_seconds_override=total_seconds_override,
        display_percent=display_percent,
    )
    status = str(status_text or '').strip()
    return f"{status} | {timing}" if status else timing


POSTPROCESS_PROGRESS_SHARE = 0.08


def get_estimated_media_size_bytes(info_dict):
    """info_dict-იდან მედიის სავარაუდო ზომის ამოღება"""
    if not isinstance(info_dict, dict):
        return None

    requested_downloads = info_dict.get('requested_downloads') or []
    sizes = []
    if isinstance(requested_downloads, list):
        for item in requested_downloads:
            size = get_estimated_size_bytes(item)
            if size:
                sizes.append(size)
    if sizes:
        return sum(sizes)

    direct_size = get_estimated_size_bytes(info_dict)
    if direct_size:
        return direct_size

    for key in ('__real_download_bytes', '__filesize_approx', 'filesize', 'filesize_approx'):
        value = info_dict.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)

    return None


def estimate_postprocess_total_seconds(info_dict=None, postprocessor=None):
    """ფორმატირება/merge/conversion-ის სავარაუდო ხანგრძლივობა"""
    name = str(postprocessor or '').strip().lower()
    duration = 0.0
    size_bytes = get_estimated_media_size_bytes(info_dict or {}) or 0
    size_gb = size_bytes / 1_000_000_000 if size_bytes else 0.0

    try:
        duration = max(0.0, float((info_dict or {}).get('duration') or 0))
    except Exception:
        duration = 0.0

    if 'extractaudio' in name or 'audio' in name:
        total = max(6.0, duration * 0.22, size_gb * 45.0)
        return min(total, 7200.0)

    if 'convert' in name and 'video' in name:
        total = max(8.0, duration * 0.30, size_gb * 60.0)
        return min(total, 10800.0)

    if 'merger' in name or 'remux' in name:
        total = max(4.0, duration * 0.04, size_gb * 20.0)
        return min(total, 1800.0)

    if 'metadata' in name or 'thumbnail' in name or 'fixup' in name or 'movefiles' in name:
        total = max(2.0, size_gb * 5.0)
        return min(total, 300.0)

    total = max(5.0, duration * 0.08, size_gb * 18.0)
    return min(total, 2400.0)


def estimate_expected_postprocess_total_seconds(info_dict=None, format_kind='video'):
    """დაწყებამდე სავარაუდო post-process დრო"""
    if str(format_kind) == 'audio':
        return estimate_postprocess_total_seconds(info_dict, 'FFmpegExtractAudio')
    return estimate_postprocess_total_seconds(info_dict, 'FFmpegMerger')


def get_postprocessor_label(postprocessor_name):
    """postprocessor-ის მეგობრული სახელი, ტექნიკური თეგით დასაწყისში, მაგ. '[ExtractAudio] აუდიოს კონვერტაცია...'"""
    raw_name = str(postprocessor_name or '').strip()
    name = raw_name.lower()
    tag = f"[{raw_name}] " if raw_name else ''

    if 'extractaudio' in name:
        return f"{tag}აუდიოს კონვერტაცია..."
    if 'merger' in name:
        return f"{tag}ვიდეო/აუდიოს გაერთიანება..."
    if 'remux' in name:
        return f"{tag}კონტეინერის ფორმატირება..."
    if 'convert' in name and 'video' in name:
        return f"{tag}ვიდეოს კონვერტაცია..."
    if 'metadata' in name:
        return f"{tag}მეტამონაცემების ჩაწერა..."
    if 'thumbnail' in name:
        return f"{tag}thumbnail-ის ჩამატება..."
    if 'fixup' in name:
        return f"{tag}ფაილის გასწორება..."
    if 'movefiles' in name:
        return f"{tag}ფაილის გადატანა..."
    return f"{tag}ფაილის დამუშავება..."


def get_download_phase_percent(download_percent, predicted_postprocess_total=None):
    """download ფაზის პროცენტი მთლიან ამოცანაში, postprocess-ის რეზერვით"""
    reserve = POSTPROCESS_PROGRESS_SHARE if (predicted_postprocess_total or 0) > 0 else 0.0
    normalized = clamp_float((download_percent or 0) / 100.0, 0.0, 1.0)
    return normalized * (1.0 - reserve) * 100.0


def get_postprocess_remaining_seconds(post_started_at, predicted_postprocess_total=None):
    """postprocess-ის დარჩენილი დრო დინამიკური კორექციით"""
    if not post_started_at:
        return predicted_postprocess_total

    elapsed = get_elapsed_seconds(post_started_at) or 0.0
    predicted = max(0.0, float(predicted_postprocess_total or 0.0))
    dynamic_total = max(predicted, elapsed + 2.0)
    return max(dynamic_total - elapsed, 0.0)


def get_postprocess_phase_percent(post_started_at, predicted_postprocess_total=None):
    """postprocess ფაზის პროცენტი მთლიან ამოცანაში"""
    reserve = POSTPROCESS_PROGRESS_SHARE if (predicted_postprocess_total or 0) > 0 else 0.0
    if reserve <= 0.0:
        return 100.0

    base = (1.0 - reserve) * 100.0
    if not post_started_at:
        return base

    elapsed = get_elapsed_seconds(post_started_at) or 0.0
    predicted = max(0.0, float(predicted_postprocess_total or 0.0))
    dynamic_total = max(predicted, elapsed + 2.0)
    ratio = clamp_float(elapsed / dynamic_total, 0.0, 0.99)
    return base + (ratio * reserve * 100.0)


def get_postprocess_step_percent(post_started_at, predicted_postprocess_total=None):
    """მიმდინარე postprocess ნაბიჯის (მაგ. [ExtractAudio]) საკუთარი პროცენტი (0-100%), დამოუკიდებელი მთლიანი job-ის progress bar-ისგან"""
    if not post_started_at:
        return 0.0
    elapsed = get_elapsed_seconds(post_started_at) or 0.0
    predicted = max(0.0, float(predicted_postprocess_total or 0.0))
    dynamic_total = max(predicted, elapsed + 2.0)
    if dynamic_total <= 0:
        return 0.0
    return clamp_float((elapsed / dynamic_total) * 100.0, 0.0, 99.0)


def get_batch_overall_percent(index, total, item_percent):
    """ერთი ელემენტის პროცენტი -> მთელი batch-ის პროცენტი"""
    total = max(int(total or 1), 1)
    item_fraction = clamp_float((item_percent or 0) / 100.0, 0.0, 1.0)
    return (((int(index or 1) - 1) + item_fraction) / total) * 100.0


def get_playlist_overall_percent(playlist_idx, total_playlists, entry_idx, total_entries, item_percent):
    """პლეილისტის შიგნით ვიდეოს პროგრესი -> მთელი job-ის პროცენტი"""
    total_playlists = max(int(total_playlists or 1), 1)
    total_entries = max(int(total_entries or 1), 1)
    item_fraction = clamp_float((item_percent or 0) / 100.0, 0.0, 1.0)
    playlist_fraction = ((int(entry_idx or 1) - 1) + item_fraction) / total_entries
    return (((int(playlist_idx or 1) - 1) + playlist_fraction) / total_playlists) * 100.0


def install_visible_progress(owner, progress_frame):
    """აჩენს ზუსტ პროცენტს პროგრეს-ბარის ქვემოთ ყველა downloader-ში."""
    owner.progress_percent_var = tk.StringVar(value="0.0%")
    owner.progress_percent_label = ttk.Label(
        progress_frame,
        textvariable=owner.progress_percent_var,
        style="CardBold.TLabel",
    )
    owner.progress_percent_label.pack(anchor="e", pady=(5, 0))

    def sync_percent(*_):
        try:
            value = max(0.0, min(100.0, float(owner.progress_var.get())))
            owner.progress_percent_var.set(f"{value:.1f}%")
        except Exception:
            owner.progress_percent_var.set("0.0%")

    try:
        owner.progress_var.trace_add("write", sync_percent)
    except Exception:
        pass
    sync_percent()


# ============================
# მთავარი მენიუ
# ============================
class MainMenuGUI:
    """მთავარი მენიუს კლასი"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(t('app_title'))
        self.root.geometry("820x820")
        self.root.minsize(740, 720)
        self.root.configure(bg=ModernStyle.BG_MAIN)

        install_ui_queue_pump(self.root)
        ModernStyle.apply(self.root)
        self.create_widgets()

    def _switch_language(self, lang):
        """ენის შეცვლა + დისკზე დაუყოვნებლივი შენახვა + მთავარი მენიუს მყისიერი გადათარგმნა."""
        if not set_current_language(lang):
            return
        self.root.title(t('app_title'))
        for child in self.root.winfo_children():
            child.destroy()
        self.create_widgets()

    def create_widgets(self):
        """მთავარი მენიუს ელემენტები"""
        main_frame = ttk.Frame(self.root, style="Main.TFrame")
        main_frame.pack(fill="both", expand=True, padx=30, pady=30)

        # ენის გადამრთველი — მარჯვენა ზედა კუთხეში
        lang_bar = ttk.Frame(main_frame, style="Main.TFrame")
        lang_bar.pack(fill="x")
        lang_inner = ttk.Frame(lang_bar, style="Main.TFrame")
        lang_inner.pack(side="right")
        ttk.Label(lang_inner, text=t('language_switch_label'), style="Subtitle.TLabel").pack(side="left", padx=(0, 8))
        for lang_code, lang_label in (("en", "English"), ("ka", "ქართული")):
            is_active = CURRENT_LANGUAGE == lang_code
            tk.Button(
                lang_inner,
                text=lang_label,
                font=("Segoe UI", 9, "bold" if is_active else "normal"),
                bg=ModernStyle.ACCENT if is_active else ModernStyle.BG_CARD,
                fg=ModernStyle.TEXT,
                relief="flat",
                cursor="hand2",
                padx=10,
                pady=4,
                command=lambda code=lang_code: self._switch_language(code),
            ).pack(side="left", padx=(0, 4))

        # სათაური
        title_frame = ttk.Frame(main_frame, style="Main.TFrame")
        title_frame.pack(fill="x", pady=(20, 30))

        ttk.Label(title_frame,
                 text=t('main_title'),
                 style="Title.TLabel").pack()

        ttk.Label(title_frame,
                  text=t('main_subtitle'),
                  style="Subtitle.TLabel").pack(pady=(8, 0))

        ttk.Label(
            title_frame,
            text=t('smart_cookies_status'),
            style="Status.TLabel",
        ).pack(pady=(14, 0))

        # ღილაკების კონტეინერი
        buttons_frame = ttk.Frame(main_frame, style="Main.TFrame")
        buttons_frame.pack(fill="both", expand=True, pady=20)
        buttons_frame.columnconfigure(0, weight=1)
        buttons_frame.columnconfigure(1, weight=1)

        menu_items = [
            {
                'title': t('menu_video_title'),
                'description': t('menu_video_desc'),
                'bg': ModernStyle.ACCENT,
                'fg': ModernStyle.TEXT,
                'active_bg': ModernStyle.ACCENT_HOVER,
                'active_fg': ModernStyle.TEXT,
                'command': self.open_video_downloader,
            },
            {
                'title': t('menu_channel_title'),
                'description': t('menu_channel_desc'),
                'bg': ModernStyle.CYAN,
                'fg': ModernStyle.BG_MAIN,
                'active_bg': ModernStyle.ACCENT_GLOW,
                'active_fg': ModernStyle.BG_MAIN,
                'command': self.open_channel_downloader,
            },
            {
                'title': t('menu_shorts_title'),
                'description': t('menu_shorts_desc'),
                'bg': ModernStyle.WARNING,
                'fg': ModernStyle.BG_MAIN,
                'active_bg': ModernStyle.ACCENT_GLOW,
                'active_fg': ModernStyle.BG_MAIN,
                'command': self.open_shorts_downloader,
            },
            {
                'title': t('menu_channel_shorts_title'),
                'description': t('menu_channel_shorts_desc'),
                'bg': ModernStyle.ACCENT_GLOW,
                'fg': ModernStyle.BG_MAIN,
                'active_bg': ModernStyle.CYAN,
                'active_fg': ModernStyle.BG_MAIN,
                'command': self.open_channel_and_shorts_downloader,
            },
            {
                'title': t('menu_playlists_title'),
                'description': t('menu_playlists_desc'),
                'bg': ModernStyle.SUCCESS,
                'fg': ModernStyle.TEXT,
                'active_bg': ModernStyle.ACCENT_HOVER,
                'active_fg': ModernStyle.TEXT,
                'command': self.open_playlist_downloader,
            },
            {
                'title': t('menu_bulk_title'),
                'description': t('menu_bulk_desc'),
                'bg': ModernStyle.PINK,
                'fg': ModernStyle.TEXT,
                'active_bg': ModernStyle.ACCENT_HOVER,
                'active_fg': ModernStyle.TEXT,
                'command': self.open_bulk_urls_downloader,
            },
        ]

        for index, item in enumerate(menu_items):
            row = index // 2
            column = index % 2

            item_frame = ttk.Frame(buttons_frame, style="Card.TFrame")
            item_frame.grid(row=row, column=column, sticky="nsew", padx=10, pady=10, ipadx=12, ipady=12)

            button = tk.Button(item_frame,
                               text=item['title'],
                               font=("Segoe UI", 14, "bold"),
                               bg=item['bg'],
                               fg=item['fg'],
                               activebackground=item['active_bg'],
                               activeforeground=item['active_fg'],
                               relief="flat",
                               cursor="hand2",
                               command=item['command'])
            button.pack(fill="x", ipady=15)

            description = ttk.Label(item_frame,
                                    text=item['description'],
                                    style="CardSubtitle.TLabel",
                                    wraplength=220,
                                    justify="center")
            description.pack(fill="x", pady=(10, 0))

        # ქვედა ტექსტი
        footer = ttk.Label(main_frame,
                          text=t('footer_text'),
                          style="Subtitle.TLabel")
        footer.pack(side="bottom", pady=10)

    def open_video_downloader(self):
        """ვიდეო ჩამოტვირთვის ფანჯრის გახსნა"""
        VideoDownloaderWindow(self.root)

    def open_channel_downloader(self):
        """არხის ჩამოტვირთვის ფანჯრის გახსნა"""
        ChannelDownloaderWindow(self.root)

    def open_shorts_downloader(self):
        """არხის Shorts ჩამოტვირთვის ფანჯრის გახსნა"""
        ShortsDownloaderWindow(self.root)

    def open_channel_and_shorts_downloader(self):
        """არხის ვიდეოებისა და Shorts-ის ერთად ჩამოტვირთვის ფანჯრის გახსნა"""
        ChannelAndShortsDownloaderWindow(self.root)

    def open_playlist_downloader(self):
        """პლეილისტის ჩამოტვირთვის ფანჯრის გახსნა"""
        PlaylistDownloaderWindow(self.root)

    def open_bulk_urls_downloader(self):
        """მრავალი ლინკის ჩამოტვირთვის ფანჯრის გახსნა"""
        BulkLinksDownloaderWindow(self.root)

    def run(self):
        """აპლიკაციის გაშვება"""
        self.root.mainloop()


# ============================
# ვიდეო ჩამოტვირთვა
# ============================
class VideoDownloaderWindow:
    """ერთი ვიდეოს ჩამოტვირთვის ფანჯარა"""

    def __init__(self, parent):
        self.window = tk.Toplevel(parent)
        self.window.title(t('window_title_video'))
        self.window.geometry("750x700")
        self.window.minsize(650, 600)
        self.window.configure(bg=ModernStyle.BG_MAIN)

        # ცვლადები
        self.url_var = tk.StringVar()
        self.download_path = tk.StringVar(value=os.path.expanduser("~/Downloads"))
        self.history_name_var = tk.StringVar(value="")
        self.format_var = tk.StringVar(value="video")
        self.quality_var = tk.StringVar(value="best")
        self.audio_quality_var = tk.StringVar(value="320")  # გაზრდილი 192 -> 320
        self.limit_size_var = tk.BooleanVar(value=False)
        self.skip_existing_name_var = tk.BooleanVar(value=True)
        self.progress_var = tk.DoubleVar(value=0)
        self.status_var = tk.StringVar(value="მზად არის ჩამოსატვირთად")

        self.is_downloading = False
        self.cancel_requested = False
        self.active_history_session = None
        self.active_history_item_key = None
        self.active_output_path = None
        self.download_started_at = None
        self.postprocess_started_at = None
        self.predicted_postprocess_total = None
        self.current_postprocessor = None
        self.video_info = None
        self.video_info_url = ""
        self.video_info_key = None
        self._last_url_text = ""
        self.default_quality_values = ["მაქსიმალური (4K/8K)", "2160p (4K)", "1440p (2K)", "1080p (Full HD)", "720p (HD)", "480p", "360p"]
        self.available_heights = []

        self.create_widgets()
        self._last_url_text = self.url_var.get().strip()
        self.url_var.trace_add("write", self._on_url_text_changed)
        self.window.protocol("WM_DELETE_WINDOW", lambda: close_downloader_window(self))

    def create_widgets(self):
        """ინტერფეისის ელემენტები"""
        scroll_frame = ScrollableFrame(self.window, style="Main.TFrame")
        scroll_frame.pack(fill="both", expand=True)

        main_frame = scroll_frame.scrollable_frame
        content = ttk.Frame(main_frame, style="Main.TFrame")
        content.pack(fill="both", expand=True, padx=25, pady=20)

        # სათაური
        title_frame = ttk.Frame(content, style="Main.TFrame")
        title_frame.pack(fill="x", pady=(0, 15))

        ttk.Label(title_frame, text="YouTube Video Downloader", style="Title.TLabel").pack()
        ttk.Label(title_frame, text="მაქსიმალური ხარისხი - AV1/VP9/HDR", style="Subtitle.TLabel").pack(pady=(3, 0))

        # უკან ღილაკი
        back_btn = tk.Button(title_frame,
                             text="< უკან",
                             font=("Segoe UI", 10),
                             bg=ModernStyle.BG_INPUT,
                             fg=ModernStyle.TEXT_GRAY,
                             activebackground=ModernStyle.ACCENT,
                             relief="flat",
                             cursor="hand2",
                             command=lambda: close_downloader_window(self))
        back_btn.pack(anchor="w", pady=(10, 0))

        # URL სექცია
        url_card = ttk.Frame(content, style="Card.TFrame")
        url_card.pack(fill="x", pady=8, ipady=12, ipadx=12)

        ttk.Label(url_card, text="YouTube ლინკი", style="CardBold.TLabel").pack(anchor="w", padx=12, pady=(12, 6))

        url_frame = ttk.Frame(url_card, style="Card.TFrame")
        url_frame.pack(fill="x", padx=12, pady=(0, 12))

        self.url_entry = tk.Entry(url_frame,
                                  textvariable=self.url_var,
                                  font=("Segoe UI", 11),
                                  bg=ModernStyle.BG_INPUT,
                                  fg=ModernStyle.TEXT,
                                  insertbackground=ModernStyle.ACCENT_GLOW,
                                  relief="flat", bd=0)
        self.url_entry.pack(side="left", fill="x", expand=True, ipady=10, padx=(0, 8))
        self._setup_entry_bindings(self.url_entry)

        self.fetch_btn = tk.Button(url_frame,
                                   text="ინფო",
                                   font=("Segoe UI", 10, "bold"),
                                   bg=ModernStyle.CYAN,
                                   fg=ModernStyle.BG_MAIN,
                                   relief="flat",
                                   cursor="hand2",
                                   command=self.fetch_video_info)
        self.fetch_btn.pack(side="right", ipadx=12, ipady=6)

        # ვიდეოს ინფორმაცია
        info_card = ttk.Frame(content, style="Card.TFrame")
        info_card.pack(fill="x", pady=8, ipady=12, ipadx=12)

        self.title_label = ttk.Label(info_card, text="სათაური: ჯერ არ არის არჩეული",
                                     style="Card.TLabel", wraplength=650)
        self.title_label.pack(anchor="w", padx=12, pady=(12, 4))

        info_row = ttk.Frame(info_card, style="Card.TFrame")
        info_row.pack(fill="x", padx=12, pady=(0, 4))

        self.duration_label = ttk.Label(info_row, text="ხანგრძლივობა: -", style="Info.TLabel")
        self.duration_label.pack(side="left", padx=(0, 20))

        self.date_label = ttk.Label(info_row, text="თარიღი: -", style="Info.TLabel")
        self.date_label.pack(side="left", padx=(0, 20))

        self.views_label = ttk.Label(info_row, text="ნახვები: -", style="Info.TLabel")
        self.views_label.pack(side="left")

        # ხელმისაწვდომი ფორმატების ინფო
        self.formats_label = ttk.Label(info_card, text="ხელმისაწვდომი: -", style="Info.TLabel")
        self.formats_label.pack(anchor="w", padx=12, pady=(0, 12))

        # ფორმატი და ხარისხი
        options_card = ttk.Frame(content, style="Card.TFrame")
        options_card.pack(fill="x", pady=8, ipady=12, ipadx=12)

        options_row = ttk.Frame(options_card, style="Card.TFrame")
        options_row.pack(fill="x", padx=12, pady=12)

        # ფორმატი
        format_frame = ttk.Frame(options_row, style="Card.TFrame")
        format_frame.pack(side="left", fill="x", expand=True)

        ttk.Label(format_frame, text="ფორმატი", style="CardBold.TLabel").pack(anchor="w", pady=(0, 8))

        radio_frame = ttk.Frame(format_frame, style="Card.TFrame")
        radio_frame.pack(anchor="w")

        ttk.Radiobutton(radio_frame, text="ვიდეო (1080p↓ MP4 | 1080p↑ AUTO: MKV/WEBM)", variable=self.format_var,
                        value="video", style="Card.TRadiobutton",
                        command=self.toggle_quality).pack(side="left", padx=(0, 15))

        ttk.Radiobutton(radio_frame, text="აუდიო (MP3)", variable=self.format_var,
                        value="audio", style="Card.TRadiobutton",
                        command=self.toggle_quality).pack(side="left")

        # ხარისხი
        quality_frame = ttk.Frame(options_row, style="Card.TFrame")
        quality_frame.pack(side="right")

        ttk.Label(quality_frame, text="ხარისხი", style="CardBold.TLabel").pack(anchor="w", pady=(0, 8))

        self.video_quality_frame = ttk.Frame(quality_frame, style="Card.TFrame")
        self.video_quality_frame.pack(anchor="w")

        self.quality_combo = ttk.Combobox(self.video_quality_frame,
                                          values=self.default_quality_values,
                                          state="readonly",
                                          font=("Segoe UI", 10), width=20)
        self.quality_combo.set("მაქსიმალური (4K/8K)")
        self.quality_combo.pack()
        self.quality_combo.bind("<<ComboboxSelected>>", self.on_quality_change)

        self.limit_size_check = ttk.Checkbutton(
            self.video_quality_frame,
            text="თუ 2GB ან მეტია, დაბალ ხარისხზე ჩამოვიდეს",
            variable=self.limit_size_var,
            style="Card.TCheckbutton"
        )
        self.limit_size_check.pack(anchor="w", pady=(8, 0))

        self.audio_quality_frame = ttk.Frame(quality_frame, style="Card.TFrame")

        audio_values = ["320 kbps (საუკეთესო)", "256 kbps", "192 kbps", "128 kbps"]
        self.audio_combo = ttk.Combobox(self.audio_quality_frame, values=audio_values,
                                        state="readonly", font=("Segoe UI", 10), width=20)
        self.audio_combo.set("320 kbps (საუკეთესო)")
        self.audio_combo.pack()
        self.audio_combo.bind("<<ComboboxSelected>>", self.on_audio_quality_change)

        # შენახვის ადგილი
        path_card = ttk.Frame(content, style="Card.TFrame")
        path_card.pack(fill="x", pady=8, ipady=12, ipadx=12)

        ttk.Label(path_card, text="შენახვის ადგილი", style="CardBold.TLabel").pack(anchor="w", padx=12, pady=(12, 6))

        path_frame = ttk.Frame(path_card, style="Card.TFrame")
        path_frame.pack(fill="x", padx=12, pady=(0, 12))

        self.path_entry = tk.Entry(path_frame, textvariable=self.download_path,
                                   font=("Segoe UI", 10), bg=ModernStyle.BG_INPUT,
                                   fg=ModernStyle.TEXT, insertbackground=ModernStyle.ACCENT_GLOW, relief="flat")
        self.path_entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 8))
        self._setup_entry_bindings(self.path_entry)

        browse_btn = tk.Button(path_frame, text="არჩევა", font=("Segoe UI", 9),
                               bg=ModernStyle.BG_INPUT, fg=ModernStyle.TEXT_GRAY,
                               relief="flat", cursor="hand2", command=self.browse_folder)
        browse_btn.pack(side="right", ipadx=10, ipady=5)

        add_skip_existing_name_control(path_card, self.skip_existing_name_var)

        self.history_name_entry = add_history_name_controls(
            path_card,
            self.history_name_var,
            self._setup_entry_bindings,
        )

        # პროგრესი
        progress_frame = ttk.Frame(content, style="Main.TFrame")
        progress_frame.pack(fill="x", pady=12)

        self.status_label = ttk.Label(progress_frame, textvariable=self.status_var, style="Subtitle.TLabel")
        self.status_label.pack(anchor="w", pady=(0, 6))

        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var,
                                            maximum=100, style="Accent.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x")
        install_visible_progress(self, progress_frame)

        # ჩამოტვირთვის ღილაკი
        self.download_btn = tk.Button(content, text="ჩამოტვირთვა მაქსიმალური ხარისხით",
                                      font=("Segoe UI", 14, "bold"),
                                      bg=ModernStyle.ACCENT, fg=ModernStyle.TEXT,
                                      relief="flat", cursor="hand2", command=self.start_download)
        self.download_btn.pack(fill="x", pady=15, ipady=12)

    def _setup_entry_bindings(self, entry):
        for sequence in ("<Control-v>", "<Control-V>", "<Control-Insert>", "<Shift-Insert>", "<<Paste>>"):
            entry.bind(sequence, lambda e, w=entry: self._paste_text(w))
        entry.bind("<Control-KeyPress>", lambda e, w=entry: handle_keyboard_shortcut(e, self.window, w), add="+")
        entry.bind("<Shift-KeyPress-Insert>", lambda e, w=entry: handle_keyboard_shortcut(e, self.window, w), add="+")
        entry.bind("<Control-a>", lambda e: self._select_all(entry))
        entry.bind("<Control-A>", lambda e: self._select_all(entry))
        entry.bind("<Button-3>", lambda e: self._show_context_menu(e, entry))

    def _paste_text(self, entry):
        return paste_into_entry(self.window, entry)

    def _select_all(self, entry):
        entry.select_range(0, "end")
        entry.icursor("end")
        return "break"

    def _show_context_menu(self, event, entry):
        menu = tk.Menu(self.window, tearoff=0, bg=ModernStyle.BG_CARD, fg=ModernStyle.TEXT)
        menu.add_command(label="ჩასმა", command=lambda: self._paste_text(entry))
        menu.add_command(label="ყველას მონიშვნა", command=lambda: self._select_all(entry))
        menu.add_command(label="გასუფთავება", command=lambda: entry.delete(0, "end"))
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def toggle_quality(self):
        if get_owner_setting(self, 'format', self.format_var, 'video') == "video":
            self.audio_quality_frame.pack_forget()
            self.video_quality_frame.pack(anchor="w")
        else:
            self.video_quality_frame.pack_forget()
            self.audio_quality_frame.pack(anchor="w")

    def on_quality_change(self, event=None):
        self.quality_var.set(extract_quality_code_from_label(self.quality_combo.get()))

    def on_audio_quality_change(self, event=None):
        mapping = {"320 kbps (საუკეთესო)": "320", "256 kbps": "256", "192 kbps": "192", "128 kbps": "128"}
        self.audio_quality_var.set(mapping.get(self.audio_combo.get(), "320"))

    def browse_folder(self):
        folder = filedialog.askdirectory(initialdir=get_owner_setting(self, 'download_path', self.download_path))
        if folder:
            self.download_path.set(folder)

    def _normalize_url_for_info_cache(self, url):
        return normalize_history_source(url) or str(url or '').strip()

    def _make_info_cache_key(self, url=None, info=None):
        return canonical_video_key(url=url, info=info) or self._normalize_url_for_info_cache(url)

    def _urls_match_for_cached_info(self, left_url, right_url):
        left_key = self._make_info_cache_key(url=left_url)
        right_key = self._make_info_cache_key(url=right_url)
        if left_key and right_key and left_key == right_key:
            return True
        return self._normalize_url_for_info_cache(left_url) == self._normalize_url_for_info_cache(right_url)

    def _get_cached_video_info_for_url(self, url):
        """ძველი ვიდეოს info/title არ გამოიყენოს, თუ URL შეიცვალა."""
        if not isinstance(self.video_info, dict):
            return None

        current_key = self._make_info_cache_key(url=url)
        cached_key = self.video_info_key or self._make_info_cache_key(url=self.video_info_url, info=self.video_info)
        if current_key and cached_key and current_key == cached_key:
            return self.video_info

        if self.video_info_url and self._urls_match_for_cached_info(url, self.video_info_url):
            return self.video_info

        return None

    def _cache_video_info(self, url, info):
        self.video_info = info if isinstance(info, dict) else None
        self.video_info_url = str(url or '').strip()
        self.video_info_key = self._make_info_cache_key(url=url, info=info) if isinstance(info, dict) else None

    def _reset_video_info_display(self):
        self.video_info = None
        self.video_info_url = ""
        self.video_info_key = None
        self.available_heights = []
        self.title_label.configure(text="სათაური: ჯერ არ არის არჩეული")
        self.duration_label.configure(text="ხანგრძლივობა: -")
        self.date_label.configure(text="თარიღი: -")
        self.views_label.configure(text="ნახვები: -")
        self.formats_label.configure(text="ხელმისაწვდომი: -")
        self.quality_combo.configure(values=self.default_quality_values)
        self.quality_combo.set("მაქსიმალური (4K/8K)")
        self.quality_var.set("best")

    def _on_url_text_changed(self, *args):
        current_url = self.url_var.get().strip()
        if current_url == self._last_url_text:
            return

        previous_url = self._last_url_text
        self._last_url_text = current_url

        # თუ user-მა ერთი ვიდეოს შემდეგ მეორე URL ჩაწერა, ძველი title/info აღარ უნდა დარჩეს.
        if self.video_info is not None and not self._urls_match_for_cached_info(current_url, self.video_info_url):
            self._reset_video_info_display()
            if current_url:
                self.status_var.set("ლინკი შეიცვალა — ახალი ვიდეო თავიდან შემოწმდება")
            elif previous_url:
                self.status_var.set("ლინკი გასუფთავდა")

    def fetch_video_info(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("გაფრთხილება", "შეიყვანეთ YouTube ლინკი!")
            return

        self.fetch_btn.configure(state="disabled", text="...")
        self.status_var.set("ინფორმაციის მიღება...")

        thread = threading.Thread(target=self._fetch_info_thread, args=(url,))
        thread.daemon = True
        thread.start()

    def _fetch_info_thread(self, url):
        try:
            ydl_opts = get_max_quality_options()
            ydl_opts.update({'quiet': True, 'no_warnings': True})

            info = ydl_extract_info_with_cookie_fallback(url, ydl_opts, download=False)
            schedule_ui(self, lambda url=url, info=info: self._update_info(info, url=url))
        except Exception as e:
            err = format_ydl_error(e)
            schedule_ui(self, lambda err=err: self._show_error(err))

    def _update_info(self, info, url=None):
        # თუ metadata ძველი URL-იდან დაგვიანებით დაბრუნდა, UI-ში ძველი სახელი აღარ ჩავწეროთ.
        if url is not None and not self._urls_match_for_cached_info(self.url_var.get().strip(), url):
            self.fetch_btn.configure(state="normal", text="ინფო")
            self.status_var.set("ლინკი შეიცვალა — ძველი ვიდეოს ინფორმაცია გაუქმდა")
            return

        self._cache_video_info(url or self.url_var.get().strip(), info)
        title = info.get('title', 'უცნობი')
        duration = info.get('duration', 0)
        upload_date = info.get('upload_date', '')
        views = info.get('view_count', 0)

        self.title_label.configure(text=f"სათაური: {title[:70]}{'...' if len(title) > 70 else ''}")

        if duration:
            h, m, s = duration // 3600, (duration % 3600) // 60, duration % 60
            dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
            self.duration_label.configure(text=f"ხანგრძლივობა: {dur_str}")

        if upload_date:
            try:
                date_obj = datetime.strptime(upload_date, '%Y%m%d')
                self.date_label.configure(text=f"თარიღი: {date_obj.strftime('%d.%m.%Y')}")
            except: pass

        if views:
            self.views_label.configure(text=f"ნახვები: {views:,}".replace(',', ' '))

        formats = info.get('formats', [])
        has_hdr = False
        codecs = set()

        for f in formats:
            vcodec = f.get('vcodec', '')
            if vcodec and vcodec != 'none':
                if 'av01' in vcodec:
                    codecs.add('AV1')
                elif 'vp9' in vcodec or 'vp09' in vcodec:
                    codecs.add('VP9')
                elif 'avc1' in vcodec:
                    codecs.add('H.264')
            if f.get('dynamic_range') == 'HDR':
                has_hdr = True

        self.available_heights = get_available_video_heights(info)
        quality_values = get_quality_combo_values(info)
        self.quality_combo.configure(values=quality_values)
        if quality_values:
            self.quality_combo.set(quality_values[0])
        self.quality_var.set("best")

        qualities_text = ", ".join(f"{h}p" for h in self.available_heights[:10]) if self.available_heights else "უცნობი"
        codec_str = ", ".join(sorted(codecs)) if codecs else "უცნობი"
        hdr_str = " + HDR" if has_hdr else ""

        self.formats_label.configure(text=f"ხელმისაწვდომი ხარისხები: {qualities_text} | კოდეკები: {codec_str}{hdr_str}")

        self.fetch_btn.configure(state="normal", text="ინფო")
        self.status_var.set("ინფორმაცია მიღებულია - ახლა მხოლოდ რეალურად არსებული ხარისხები გამოჩნდება")

    def _show_error(self, error):
        self.fetch_btn.configure(state="normal", text="ინფო")
        self.status_var.set("შეცდომა")
        messagebox.showerror("შეცდომა", f"შეცდომა:\n{error}")

    def start_download(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("გაფრთხილება", "შეიყვანეთ YouTube ლინკი!")
            return

        if self.is_downloading:
            return

        self.is_downloading = True
        self.cancel_requested = False
        self.download_started_at = time.monotonic()
        self.postprocess_started_at = None
        self.predicted_postprocess_total = None
        self.current_postprocessor = None
        self.download_btn.configure(state="disabled", text="მიმდინარეობს...", bg=ModernStyle.WARNING)
        self.progress_var.set(0)
        self.progress_percent_var.set("0.0%")
        self.status_var.set(join_status_with_timing("ჩამოტვირთვა იწყება მაქსიმალური ხარისხით...", self.download_started_at, percent=0))

        self._download_settings_snapshot = capture_download_settings(self)
        thread = threading.Thread(target=self._download_thread, args=(url,))
        thread.daemon = True
        thread.start()

    def _download_thread(self, url):
        session = None
        item_key = None
        try:
            base_output_path = get_owner_setting(self, 'download_path', self.download_path)
            os.makedirs(base_output_path, exist_ok=True)

            format_kind = get_owner_setting(self, 'format', self.format_var, 'video')
            cached_info = self._get_cached_video_info_for_url(url)
            info = cached_info or fetch_video_metadata(url, noplaylist=True)
            title = (info or {}).get('title') or 'video'
            settings = build_download_history_settings(
                format_kind,
                get_owner_setting(self, 'quality', self.quality_var, 'best'),
                get_owner_setting(self, 'audio_quality', self.audio_quality_var, '320'),
                get_owner_setting(self, 'limit_size', self.limit_size_var, False),
                get_owner_setting(self, 'skip_existing_name', self.skip_existing_name_var, True),
            )
            session = DownloadHistorySession(
                base_output_path,
                'single_video',
                [url],
                custom_name=get_owner_setting(self, 'history_name', self.history_name_var, ''),
                preferred_title=title,
                settings=settings,
            )
            self.active_history_session = session
            self.active_output_path = session.output_dir

            history_item = build_history_item(url, 1, title=title, info=info)
            item_key = history_item['key']
            self.active_history_item_key = item_key
            session.ensure_items([history_item])

            if session.is_completed(item_key):
                session.finalize()
                schedule_ui(
                    self,
                    lambda path=session.output_dir: self._download_complete(
                        resumed_completed=True,
                        output_path=path,
                    ),
                )
                return

            output_template = os.path.join(session.output_dir, '%(title)s.%(ext)s')
            if get_owner_setting(self, 'skip_existing_name', self.skip_existing_name_var, True):
                existing_path = find_existing_media_by_output_name(
                    session.output_dir,
                    output_template,
                    title,
                    format_kind,
                )
                if existing_path:
                    session.mark_skipped_duplicate(item_key, duplicate_of=f"existing_file:{os.path.basename(existing_path)}")
                    session.finalize()
                    schedule_ui(
                        self,
                        lambda path=session.output_dir: self._download_complete(
                            resumed_completed=True,
                            output_path=path,
                            skipped_existing_by_name=True,
                        ),
                    )
                    return

            session.begin_item(item_key, title=title, url=url, index=1)

            ydl_opts = get_max_quality_options()
            ui_progress_hook = self._progress_hook
            ui_post_hook = self._postprocessor_hook
            ydl_opts.update({
                'windowsfilenames': True,
                'noplaylist': True,
            })
            ydl_opts = prepare_history_download_options(
                ydl_opts,
                session,
                item_key,
                output_template,
                ui_progress_hook=ui_progress_hook,
                ui_postprocessor_hook=ui_post_hook,
                stop_checker=lambda: bool(self.cancel_requested),
            )

            if format_kind == "video":
                requested_quality = get_owner_setting(self, 'quality', self.quality_var, 'best')
                effective_quality, effective_height, used_fallback, used_size_limit, _estimated_size, size_limit_from_height = resolve_video_quality_with_size_limit(
                    info,
                    requested_quality,
                    size_limit_enabled=get_owner_setting(self, 'limit_size', self.limit_size_var, False),
                )
                ensure_video_pipeline_ready(info, effective_quality)
                video_opts, _, _ = build_video_download_options(info, effective_quality)
                ydl_opts.update(video_opts)
                self.predicted_postprocess_total = estimate_expected_postprocess_total_seconds(info, format_kind='video')
                if (used_fallback and requested_quality != 'best') or used_size_limit:
                    quality_text = quality_label_from_height(effective_height) if effective_height else 'ხელმისაწვდომი ხარისხი'
                    note_parts = []
                    if used_fallback and requested_quality != 'best':
                        note_parts.append('არჩეული ხარისხი ვერ მოიძებნა')
                    if used_size_limit:
                        from_quality_text = quality_label_from_height(size_limit_from_height) if size_limit_from_height else 'უცნობი'
                        note_parts.append(f'2GB ლიმიტი: {from_quality_text} → {quality_text}')
                    note_text = ' | '.join(note_parts)
                    schedule_ui(
                        self,
                        lambda q=quality_text, n=note_text: self.status_var.set(
                            join_status_with_timing(f"{n} — ჩაიწერება {q}", self.download_started_at)
                        ),
                    )
            else:
                ydl_opts.update(get_audio_download_options(get_owner_setting(self, 'audio_quality', self.audio_quality_var, '320')))
                self.predicted_postprocess_total = estimate_expected_postprocess_total_seconds(info, format_kind='audio')

            ydl_download_with_cookie_fallback([url], ydl_opts)
            session.mark_completed(item_key)
            session.finalize()

            schedule_ui(
                self,
                lambda path=session.output_dir: self._download_complete(output_path=path),
            )
        except Exception as e:
            if session is not None and item_key:
                if is_user_requested_stop_error(e) or self.cancel_requested:
                    session.mark_interrupted(item_key, 'ჩამოტვირთვა შეწყდა დასრულებამდე.')
                    session.finalize(interrupted=True)
                    return
                session.mark_failed(item_key, e)
                session.finalize()
            err = format_ydl_error(e)
            schedule_ui(self, lambda err=err: self._download_error(err))
        finally:
            if session is not None:
                session.close()
            self.active_history_session = None
            self.active_history_item_key = None

    def _progress_hook(self, d):
        if d['status'] == 'downloading':
            percent_str = d.get('_percent_str', '0%').strip()
            speed = d.get('_speed_str', 'N/A')
            eta = d.get('_eta_str', 'N/A')
            eta_seconds = d.get('eta')
            percent = None
            overall = None
            try:
                percent = float(percent_str.replace('%', ''))
                overall = get_download_phase_percent(percent, self.predicted_postprocess_total)
                schedule_ui(self, lambda value=overall: self.progress_var.set(value))
            except Exception:
                percent = None
                overall = None

            remaining_seconds = None
            try:
                remaining_seconds = max(0.0, float(eta_seconds or 0)) + max(0.0, float(self.predicted_postprocess_total or 0))
            except Exception:
                remaining_seconds = eta_seconds

            downloaded = d.get('downloaded_bytes')
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            fragment_index = d.get('fragment_index')
            fragment_count = d.get('fragment_count')
            detail_parts = []

            try:
                if downloaded is not None:
                    if total:
                        detail_parts.append(
                            f"ზომა: {downloaded / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MiB"
                        )
                    else:
                        detail_parts.append(
                            f"ჩამოტვირთულია: {downloaded / 1024 / 1024:.1f} MiB"
                        )
            except Exception:
                pass

            if fragment_index is not None and fragment_count:
                detail_parts.append(f"ფრაგმენტი: {fragment_index}/{fragment_count}")

            if detail_parts:
                status_text = (
                    f"ჩამოტვირთვა: {percent_str} | სიჩქარე: {speed} | "
                    f"დარჩენილი: {eta} | " + " | ".join(detail_parts)
                )
            else:
                status_text = (
                    f"ჩამოტვირთვა: {percent_str} | სიჩქარე: {speed} | "
                    f"დარჩენილი: {eta}"
                )

            status = join_status_with_timing(
                status_text,
                self.download_started_at,
                percent=overall,
                remaining_seconds=remaining_seconds,
            )
            schedule_ui(self, lambda s=status: self.status_var.set(s))
        elif d['status'] == 'finished':
            finished_percent = get_download_phase_percent(100, self.predicted_postprocess_total)
            schedule_ui(self, lambda value=finished_percent: self.progress_var.set(value))
            schedule_ui(self, lambda: self.status_var.set(join_status_with_timing(
                get_postprocessor_label(self.current_postprocessor),
                self.download_started_at,
                percent=finished_percent,
                remaining_seconds=self.predicted_postprocess_total,
            )))

    def _postprocessor_hook(self, d):
        status = d.get('status')
        if status not in {'started', 'processing', 'finished'}:
            return

        postprocessor_name = d.get('postprocessor') or self.current_postprocessor or ''
        self.current_postprocessor = postprocessor_name

        if self.postprocess_started_at is None and status in {'started', 'processing'}:
            self.postprocess_started_at = time.monotonic()

        if status == 'started' and d.get('info_dict'):
            predicted = estimate_postprocess_total_seconds(d.get('info_dict'), postprocessor_name)
            if predicted:
                self.predicted_postprocess_total = max(float(self.predicted_postprocess_total or 0), float(predicted))

        if status in {'started', 'processing'}:
            overall = get_postprocess_phase_percent(self.postprocess_started_at, self.predicted_postprocess_total)
            step_percent = get_postprocess_step_percent(self.postprocess_started_at, self.predicted_postprocess_total)
            remaining = get_postprocess_remaining_seconds(self.postprocess_started_at, self.predicted_postprocess_total)
            label = get_postprocessor_label(postprocessor_name)
            schedule_ui(self, lambda value=overall: self.progress_var.set(value))
            schedule_ui(self, lambda s=join_status_with_timing(
                label, self.download_started_at, percent=overall, remaining_seconds=remaining,
                display_percent=step_percent,
            ): self.status_var.set(s))

    def _download_complete(self, resumed_completed=False, output_path=None, skipped_existing_by_name=False):
        self.is_downloading = False
        self.download_btn.configure(state="normal", text="ჩამოტვირთვა მაქსიმალური ხარისხით", bg=ModernStyle.ACCENT)
        self.progress_var.set(100)
        self.progress_percent_var.set("100.0%")
        final_path = output_path or self.active_output_path or get_owner_setting(self, 'download_path', self.download_path)
        if skipped_existing_by_name:
            self.status_var.set(join_status_with_timing("იგივე სახელის ფაილი უკვე არსებობს — ჩამოტვირთვა გამოტოვდა.", self.download_started_at, percent=100))
            messagebox.showinfo("გამოტოვებულია", f"ამ საქაღალდეში იგივე სახელით ფაილი უკვე არსებობს და თავიდან აღარ ჩამოიტვირთა:\n{final_path}")
        elif resumed_completed:
            self.status_var.set(join_status_with_timing("JSON ისტორიის მიხედვით ეს ფაილი უკვე დასრულებულია — გამოტოვდა.", self.download_started_at, percent=100))
            messagebox.showinfo("უკვე დასრულებულია", f"ფაილი უკვე ჩაწერილია და თავიდან აღარ ჩამოიტვირთა:\n{final_path}")
        else:
            self.status_var.set(join_status_with_timing("ჩამოტვირთვა დასრულდა მაქსიმალური ხარისხით!", self.download_started_at, percent=100))
            messagebox.showinfo("წარმატება", f"ფაილი შენახულია:\n{final_path}\n\nJSON ისტორია:\n{get_download_history_root()}")

    def _download_error(self, error):
        self.is_downloading = False
        self.download_btn.configure(state="normal", text="ჩამოტვირთვა მაქსიმალური ხარისხით", bg=ModernStyle.ACCENT)
        self.progress_var.set(0)
        self.progress_percent_var.set("0.0%")
        self.status_var.set(join_status_with_timing("შეცდომა!", self.download_started_at))
        messagebox.showerror("შეცდომა", error)


# ============================
# მრავალი ლინკის ჩამოტვირთვა
# ============================
class BulkLinksDownloaderWindow:
    """რამდენიმე ცალკე ვიდეო ლინკის ერთიანად ჩამოტვირთვის ფანჯარა"""

    def __init__(self, parent):
        self.window = tk.Toplevel(parent)
        self.window.title(t('window_title_bulk'))
        self.window.geometry("820x760")
        self.window.minsize(720, 620)
        self.window.configure(bg=ModernStyle.BG_MAIN)

        self.download_path = tk.StringVar(value=os.path.expanduser("~/Downloads"))
        self.history_name_var = tk.StringVar(value="")
        self.format_var = tk.StringVar(value="video")
        self.quality_var = tk.StringVar(value="best")
        self.audio_quality_var = tk.StringVar(value="320")
        self.limit_size_var = tk.BooleanVar(value=False)
        self.skip_existing_name_var = tk.BooleanVar(value=True)
        self.numbered_filenames_var = tk.BooleanVar(value=False)
        self.progress_var = tk.DoubleVar(value=0)
        self.status_var = tk.StringVar(value="ჩასვით რამდენიმე ვიდეო ლინკი — თითო ხაზი თითო ლინკი")

        self.is_downloading = False
        self.cancel_requested = False
        self.active_history_session = None
        self.active_history_item_key = None
        self.active_output_path = None
        self.download_started_at = None
        self.item_timing_states = {}
        self.live_urls_lock = threading.Lock()
        self.live_urls = []
        self._build_widgets()
        self.window.protocol("WM_DELETE_WINDOW", lambda: close_downloader_window(self))

    def _build_widgets(self):
        scroll_frame = ScrollableFrame(self.window, style="Main.TFrame")
        scroll_frame.pack(fill="both", expand=True)

        main_frame = scroll_frame.scrollable_frame
        content = ttk.Frame(main_frame, style="Main.TFrame")
        content.pack(fill="both", expand=True, padx=25, pady=20)

        title_frame = ttk.Frame(content, style="Main.TFrame")
        title_frame.pack(fill="x", pady=(0, 15))

        ttk.Label(title_frame, text="Bulk Link Downloader", style="Title.TLabel").pack()
        ttk.Label(title_frame, text="რამდენიმე ლინკი ერთდროულად — არჩეული ხარისხით ან თითოეული თავის მაქსიმუმზე", style="Subtitle.TLabel").pack(pady=(3, 0))

        back_btn = tk.Button(title_frame,
                             text="< უკან",
                             font=("Segoe UI", 10),
                             bg=ModernStyle.BG_INPUT,
                             fg=ModernStyle.TEXT_GRAY,
                             activebackground=ModernStyle.ACCENT,
                             relief="flat",
                             cursor="hand2",
                             command=lambda: close_downloader_window(self))
        back_btn.pack(anchor="w", pady=(10, 0))

        urls_card = ttk.Frame(content, style="Card.TFrame")
        urls_card.pack(fill="both", expand=True, pady=8, ipady=12, ipadx=12)

        header_frame = ttk.Frame(urls_card, style="Card.TFrame")
        header_frame.pack(fill="x", padx=12, pady=(12, 6))
        ttk.Label(header_frame, text="ვიდეო ლინკები", style="CardBold.TLabel").pack(side="left")
        ttk.Label(header_frame, text="თითო ხაზი = ერთი ლინკი", style="Info.TLabel").pack(side="left", padx=(10, 0))

        btns_frame = ttk.Frame(urls_card, style="Card.TFrame")
        btns_frame.pack(fill="x", padx=12, pady=(0, 8))

        paste_btn = tk.Button(btns_frame, text="კლიპბორდიდან ჩასმა", font=("Segoe UI", 9),
                              bg=ModernStyle.BG_INPUT, fg=ModernStyle.TEXT,
                              relief="flat", cursor="hand2", command=self._paste_urls)
        paste_btn.pack(side="left", padx=(0, 8), ipadx=8, ipady=4)

        clear_btn = tk.Button(btns_frame, text="გასუფთავება", font=("Segoe UI", 9),
                              bg=ModernStyle.BG_INPUT, fg=ModernStyle.TEXT_GRAY,
                              relief="flat", cursor="hand2", command=self._clear_urls)
        clear_btn.pack(side="left", padx=(0, 8), ipadx=8, ipady=4)

        self.numbered_names_btn = ttk.Checkbutton(
            btns_frame,
            text="სახელში დანომვრა",
            variable=self.numbered_filenames_var,
            style="Card.TCheckbutton",
            command=self.toggle_numbered_filenames,
        )
        self.numbered_names_btn.pack(side="left", ipadx=8, ipady=4)
        self._update_numbered_option_button()

        self.urls_text = tk.Text(urls_card,
                                 height=13,
                                 wrap="word",
                                 font=("Segoe UI", 10),
                                 bg=ModernStyle.BG_INPUT,
                                 fg=ModernStyle.TEXT,
                                 insertbackground=ModernStyle.ACCENT_GLOW,
                                 relief="flat")
        self.urls_text.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._setup_text_bindings(self.urls_text)

        note = ttk.Label(urls_card,
                         text="ვიდეო რეჟიმში შეგიძლია მიუთითო ხარისხი. თუ არჩეული ხარისხი კონკრეტულ ვიდეოზე არ არის, აიღებს მის საუკეთესო ხელმისაწვდომ ხარისხს არჩეულის ქვემოთ. თუ მონიშნავ 2GB ლიმიტს, ზედმეტად დიდ ვიდეოზე თვითონ ჩამოვა დაბალ ხარისხზე. 1080p-მდე MP4, ზემოთ AUTO (MKV/WEBM).",
                         style="Info.TLabel")
        note.pack(anchor="w", padx=12, pady=(0, 12))

        format_card = ttk.Frame(content, style="Card.TFrame")
        format_card.pack(fill="x", pady=8, ipady=12, ipadx=12)

        ttk.Label(format_card, text="ფორმატი", style="CardBold.TLabel").pack(anchor="w", padx=12, pady=(12, 6))

        radios_frame = ttk.Frame(format_card, style="Card.TFrame")
        radios_frame.pack(fill="x", padx=12, pady=(0, 8))

        video_rb = ttk.Radiobutton(radios_frame, text="ვიდეო",
                                   variable=self.format_var, value="video",
                                   style="Card.TRadiobutton", command=self.toggle_quality)
        video_rb.pack(anchor="w", pady=2)

        audio_rb = ttk.Radiobutton(radios_frame, text="MP3 — საუკეთესო აუდიო წყარო თითო ლინკზე",
                                   variable=self.format_var, value="audio",
                                   style="Card.TRadiobutton", command=self.toggle_quality)
        audio_rb.pack(anchor="w", pady=2)

        self.video_quality_frame = ttk.Frame(format_card, style="Card.TFrame")
        ttk.Label(self.video_quality_frame, text="ვიდეო ხარისხი", style="Card.TLabel").pack(anchor="w", pady=(6, 4))
        quality_values = ["ავტომატური მაქსიმუმი თითო ვიდეოზე", "2160p (4K)", "1440p (2K)", "1080p (Full HD)", "720p (HD)", "480p", "360p"]
        self.quality_combo = ttk.Combobox(self.video_quality_frame, values=quality_values,
                                          state="readonly", font=("Segoe UI", 10), width=28)
        self.quality_combo.set("ავტომატური მაქსიმუმი თითო ვიდეოზე")
        self.quality_combo.pack(anchor="w")
        self.quality_combo.bind("<<ComboboxSelected>>", self.on_quality_change)

        self.limit_size_check = ttk.Checkbutton(
            self.video_quality_frame,
            text="თუ 2GB ან მეტია, დაბალ ხარისხზე ჩამოვიდეს",
            variable=self.limit_size_var,
            style="Card.TCheckbutton"
        )
        self.limit_size_check.pack(anchor="w", pady=(8, 0))

        self.audio_quality_frame = ttk.Frame(format_card, style="Card.TFrame")
        ttk.Label(self.audio_quality_frame, text="MP3 ხარისხი", style="Card.TLabel").pack(anchor="w", pady=(6, 4))
        audio_values = ["320 kbps (საუკეთესო)", "256 kbps", "192 kbps", "128 kbps"]
        self.audio_combo = ttk.Combobox(self.audio_quality_frame, values=audio_values,
                                        state="readonly", font=("Segoe UI", 10), width=20)
        self.audio_combo.set("320 kbps (საუკეთესო)")
        self.audio_combo.pack(anchor="w")
        self.audio_combo.bind("<<ComboboxSelected>>", self.on_audio_quality_change)
        self.toggle_quality()

        path_card = ttk.Frame(content, style="Card.TFrame")
        path_card.pack(fill="x", pady=8, ipady=12, ipadx=12)

        ttk.Label(path_card, text="შენახვის ადგილი", style="CardBold.TLabel").pack(anchor="w", padx=12, pady=(12, 6))

        path_frame = ttk.Frame(path_card, style="Card.TFrame")
        path_frame.pack(fill="x", padx=12, pady=(0, 12))

        self.path_entry = tk.Entry(path_frame, textvariable=self.download_path,
                                   font=("Segoe UI", 10), bg=ModernStyle.BG_INPUT,
                                   fg=ModernStyle.TEXT, insertbackground=ModernStyle.ACCENT_GLOW, relief="flat")
        self.path_entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 8))
        self._setup_entry_bindings(self.path_entry)

        browse_btn = tk.Button(path_frame, text="არჩევა", font=("Segoe UI", 9),
                               bg=ModernStyle.BG_INPUT, fg=ModernStyle.TEXT_GRAY,
                               relief="flat", cursor="hand2", command=self.browse_folder)
        browse_btn.pack(side="right", ipadx=10, ipady=5)

        add_skip_existing_name_control(path_card, self.skip_existing_name_var)

        self.history_name_entry = add_history_name_controls(
            path_card,
            self.history_name_var,
            self._setup_entry_bindings,
        )

        progress_frame = ttk.Frame(content, style="Main.TFrame")
        progress_frame.pack(fill="x", pady=12)

        self.status_label = ttk.Label(progress_frame, textvariable=self.status_var, style="Subtitle.TLabel")
        self.status_label.pack(anchor="w", pady=(0, 6))

        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var,
                                            maximum=100, style="Accent.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x")
        install_visible_progress(self, progress_frame)

        self.download_btn = tk.Button(content, text="ყველას ჩამოტვირთვა", font=("Segoe UI", 14, "bold"),
                                      bg=ModernStyle.PINK, fg=ModernStyle.TEXT,
                                      relief="flat", cursor="hand2", command=self.start_download)
        self.download_btn.pack(fill="x", pady=15, ipady=12)

    def _setup_text_bindings(self, text_widget):
        for sequence in ("<Control-v>", "<Control-V>", "<Control-Insert>", "<Shift-Insert>", "<<Paste>>"):
            text_widget.bind(sequence, lambda e, w=text_widget: self._paste_text(w))
        text_widget.bind("<Control-KeyPress>", lambda e, w=text_widget: handle_keyboard_shortcut(e, self.window, w), add="+")
        text_widget.bind("<Shift-KeyPress-Insert>", lambda e, w=text_widget: handle_keyboard_shortcut(e, self.window, w), add="+")
        text_widget.bind("<Control-a>", lambda e: self._select_all_text(text_widget))
        text_widget.bind("<Control-A>", lambda e: self._select_all_text(text_widget))
        text_widget.bind("<Button-3>", lambda e: self._show_text_context_menu(e, text_widget))
        text_widget.bind("<<Modified>>", self._on_urls_text_modified, add="+")

    def _setup_entry_bindings(self, entry):
        for sequence in ("<Control-v>", "<Control-V>", "<Control-Insert>", "<Shift-Insert>", "<<Paste>>"):
            entry.bind(sequence, lambda e, w=entry: self._paste_entry(w))
        entry.bind("<Control-a>", lambda e: self._select_all_entry(entry))
        entry.bind("<Control-A>", lambda e: self._select_all_entry(entry))
        entry.bind("<Button-3>", lambda e: self._show_entry_context_menu(e, entry))

    def _paste_urls(self):
        self._paste_text(self.urls_text)

    def _clear_urls(self):
        self.urls_text.delete("1.0", "end")
        self._set_live_urls([])
        self.status_var.set("ველი გასუფთავდა — ჩასვით ლინკები")

    def _paste_text(self, text_widget):
        result = paste_into_text(self.window, text_widget)
        self.window.after(1, self._sync_live_urls_from_text)
        return result

    def _select_all_text(self, text_widget):
        text_widget.tag_add("sel", "1.0", "end-1c")
        text_widget.mark_set("insert", "1.0")
        text_widget.see("insert")
        return "break"

    def _show_text_context_menu(self, event, text_widget):
        menu = tk.Menu(self.window, tearoff=0, bg=ModernStyle.BG_CARD, fg=ModernStyle.TEXT)
        menu.add_command(label="ჩასმა", command=lambda: self._paste_text(text_widget))
        menu.add_command(label="ყველას მონიშვნა", command=lambda: self._select_all_text(text_widget))
        menu.add_command(label="გასუფთავება", command=self._clear_urls)
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _paste_entry(self, entry):
        return paste_into_entry(self.window, entry)

    def _select_all_entry(self, entry):
        entry.select_range(0, "end")
        entry.icursor("end")
        return "break"

    def _show_entry_context_menu(self, event, entry):
        menu = tk.Menu(self.window, tearoff=0, bg=ModernStyle.BG_CARD, fg=ModernStyle.TEXT)
        menu.add_command(label="ჩასმა", command=lambda: self._paste_entry(entry))
        menu.add_command(label="ყველას მონიშვნა", command=lambda: self._select_all_entry(entry))
        menu.add_command(label="გასუფთავება", command=lambda: entry.delete(0, "end"))
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _set_live_urls(self, urls):
        with self.live_urls_lock:
            self.live_urls = list(urls or [])

    def _get_live_urls_snapshot(self):
        with self.live_urls_lock:
            return list(self.live_urls)

    def _sync_live_urls_from_text(self, event=None):
        urls = self._get_urls()
        previous_count = len(self._get_live_urls_snapshot())
        self._set_live_urls(urls)
        current_count = len(urls)
        if not self.is_downloading:
            if current_count:
                self.status_var.set(f"ნაპოვნია {current_count} ლინკი — შეგიძლია დაამატო/წაშალო პირდაპირ ველში")
            else:
                self.status_var.set("ჩასვით რამდენიმე ვიდეო ლინკი — თითო ხაზი თითო ლინკი")
        elif current_count > previous_count:
            added = current_count - previous_count
            self.status_var.set(f"Live input: დაემატა {added} ახალი ლინკი, რიგში ჩაემატება")

    def _on_urls_text_modified(self, event=None):
        try:
            if not self.urls_text.edit_modified():
                return
            self.urls_text.edit_modified(False)
        except Exception:
            pass
        self.window.after(1, self._sync_live_urls_from_text)

    def _update_numbered_option_button(self):
        if not hasattr(self, 'numbered_names_btn'):
            return
        if get_owner_setting(self, 'numbered_filenames', self.numbered_filenames_var, False):
            self.numbered_names_btn.configure(text="სახელში დანომვრა ON")
        else:
            self.numbered_names_btn.configure(text="სახელში დანომვრა")

    def toggle_numbered_filenames(self):
        self._update_numbered_option_button()
        if get_owner_setting(self, 'numbered_filenames', self.numbered_filenames_var, False):
            self.status_var.set("ჩართულია: ფაილის სახელები იქნება 001 - სათაური")
        else:
            self.status_var.set("გამორთულია: ფაილის სახელები იქნება ჩვეულებრივი სათაურით")

    def _build_bulk_output_template(self, output_path, index, total_count):
        if get_owner_setting(self, 'numbered_filenames', self.numbered_filenames_var, False):
            return build_numbered_output_template(output_path, index, total_count, numbered_only=False)
        return os.path.join(output_path, "%(title)s.%(ext)s")

    def toggle_quality(self):
        if get_owner_setting(self, 'format', self.format_var, 'video') == "video":
            self.video_quality_frame.pack(anchor="w", padx=12, pady=(0, 10))
            self.audio_quality_frame.pack_forget()
        else:
            self.video_quality_frame.pack_forget()
            self.audio_quality_frame.pack(anchor="w", padx=12, pady=(0, 10))

    def on_quality_change(self, event=None):
        self.quality_var.set(extract_quality_code_from_label(self.quality_combo.get()))

    def on_audio_quality_change(self, event=None):
        mapping = {"320 kbps (საუკეთესო)": "320", "256 kbps": "256", "192 kbps": "192", "128 kbps": "128"}
        self.audio_quality_var.set(mapping.get(self.audio_combo.get(), "320"))

    def browse_folder(self):
        folder = filedialog.askdirectory(initialdir=get_owner_setting(self, 'download_path', self.download_path))
        if folder:
            self.download_path.set(folder)

    def _get_urls(self):
        text = self.urls_text.get("1.0", "end")
        return extract_urls_from_text(text)

    def start_download(self):
        self._sync_live_urls_from_text()
        urls = self._get_urls()
        if not urls:
            messagebox.showwarning("გაფრთხილება", "ჩასვით მინიმუმ ერთი ლინკი!")
            return

        if self.is_downloading:
            return

        self.is_downloading = True
        self.cancel_requested = False
        self.download_started_at = time.monotonic()
        self.item_timing_states = {}
        self.download_btn.configure(state="disabled", text="მიმდინარეობს...", bg=ModernStyle.WARNING)
        self.progress_var.set(0)
        names_text = "სახელები: 001 - სათაური" if get_owner_setting(self, 'numbered_filenames', self.numbered_filenames_var, False) else "სახელები: ჩვეულებრივი სათაური"
        self.status_var.set(join_status_with_timing(f"იწყება {len(urls)} ლინკის დამუშავება... | {names_text}", self.download_started_at, percent=0))

        self._download_settings_snapshot = capture_download_settings(self)
        thread = threading.Thread(target=self._download_all_thread, args=(urls,))
        thread.daemon = True
        thread.start()

    def _get_item_timing_state(self, index):
        state = self.item_timing_states.get(index)
        if state is None:
            state = {
                'predicted_postprocess_total': None,
                'postprocess_started_at': None,
                'current_postprocessor': '',
            }
            self.item_timing_states[index] = state
        return state

    def _set_item_postprocess_estimate(self, index, seconds):
        state = self._get_item_timing_state(index)
        if seconds:
            state['predicted_postprocess_total'] = max(float(state.get('predicted_postprocess_total') or 0), float(seconds))

    def _make_postprocessor_hook(self, index, total, title=''):
        def hook(d):
            status = d.get('status')
            if status not in {'started', 'processing', 'finished'}:
                return

            state = self._get_item_timing_state(index)
            postprocessor_name = d.get('postprocessor') or state.get('current_postprocessor') or ''
            state['current_postprocessor'] = postprocessor_name

            if state.get('postprocess_started_at') is None and status in {'started', 'processing'}:
                state['postprocess_started_at'] = time.monotonic()

            if status == 'started' and d.get('info_dict'):
                predicted = estimate_postprocess_total_seconds(d.get('info_dict'), postprocessor_name)
                if predicted:
                    state['predicted_postprocess_total'] = max(float(state.get('predicted_postprocess_total') or 0), float(predicted))

            if status in {'started', 'processing'}:
                item_percent = get_postprocess_phase_percent(state.get('postprocess_started_at'), state.get('predicted_postprocess_total'))
                step_percent = get_postprocess_step_percent(state.get('postprocess_started_at'), state.get('predicted_postprocess_total'))
                overall = get_batch_overall_percent(index, total, item_percent)
                remaining = get_postprocess_remaining_seconds(state.get('postprocess_started_at'), state.get('predicted_postprocess_total'))
                short_title = f" | {title[:45]}{'...' if len(title) > 45 else ''}" if title else ''
                label = get_postprocessor_label(postprocessor_name)
                schedule_ui(self, lambda value=overall: self.progress_var.set(value))
                schedule_ui(self, lambda s=join_status_with_timing(
                    f"{index}/{total} | {label}{short_title}", self.download_started_at, percent=overall,
                    remaining_seconds=remaining, display_percent=step_percent,
                ): self.status_var.set(s))
        return hook

    def _make_progress_hook(self, index, total):
        def hook(d):
            state = self._get_item_timing_state(index)
            if d.get('status') == 'downloading':
                percent_str = d.get('_percent_str', '0%').strip()
                speed = d.get('_speed_str', 'N/A')
                eta = d.get('_eta_str', 'N/A')
                percent = None
                overall = None
                try:
                    percent = float(percent_str.replace('%', ''))
                    item_percent = get_download_phase_percent(percent, state.get('predicted_postprocess_total'))
                    overall = get_batch_overall_percent(index, total, item_percent)
                    schedule_ui(self, lambda value=overall: self.progress_var.set(value))
                except Exception:
                    overall = None
                status = join_status_with_timing(
                    f"{index}/{total} | ჩამოტვირთვა: {percent_str} | სიჩქარე: {speed} | დარჩენილი: {eta}",
                    self.download_started_at,
                    percent=overall,
                )
                schedule_ui(self, lambda s=status: self.status_var.set(s))
            elif d.get('status') == 'finished':
                item_percent = get_download_phase_percent(100, state.get('predicted_postprocess_total'))
                overall = get_batch_overall_percent(index, total, item_percent)
                schedule_ui(self, lambda value=overall: self.progress_var.set(value))
                schedule_ui(self, lambda: self.status_var.set(join_status_with_timing(
                    f"{index}/{total} | {get_postprocessor_label(state.get('current_postprocessor'))}",
                    self.download_started_at,
                    percent=overall,
                )))
        return hook

    def _download_all_thread(self, urls):
        session = None
        current_key = None
        successful = 0
        failed = 0
        skipped_duplicates = 0
        skipped_existing_by_name = 0
        resumed_completed = 0
        size_limit_fallbacks = 0
        failed_urls = []
        interrupted = False
        history_items_by_key = {}
        processed_item_keys = set()

        try:
            base_output_path = get_owner_setting(self, 'download_path', self.download_path)
            os.makedirs(base_output_path, exist_ok=True)

            format_kind = get_owner_setting(self, 'format', self.format_var, 'video')
            settings = build_download_history_settings(
                format_kind,
                get_owner_setting(self, 'quality', self.quality_var, 'best'),
                get_owner_setting(self, 'audio_quality', self.audio_quality_var, '320'),
                get_owner_setting(self, 'limit_size', self.limit_size_var, False),
                get_owner_setting(self, 'skip_existing_name', self.skip_existing_name_var, True),
            )
            session = DownloadHistorySession(
                base_output_path,
                'bulk_links',
                urls,
                custom_name=get_owner_setting(self, 'history_name', self.history_name_var, ''),
                preferred_title=f"links_{len(urls)}",
                settings=settings,
            )
            self.active_history_session = session
            self.active_output_path = session.output_dir

            def refresh_history_items():
                live_urls = self._get_live_urls_snapshot() or list(urls)
                new_items = []
                for url in live_urls:
                    item = build_history_item(url, len(history_items_by_key) + 1, title=f"link {len(history_items_by_key) + 1}")
                    item_key = item.get('key')
                    if not item_key or item_key in history_items_by_key:
                        continue
                    history_items_by_key[item_key] = item
                    new_items.append(item)
                if new_items:
                    session.ensure_items(new_items)
                    schedule_ui(
                        self,
                        lambda count=len(history_items_by_key): self.status_var.set(
                            join_status_with_timing(
                                f"Live input: რიგშია {count} ლინკი",
                                self.download_started_at,
                                percent=self.progress_var.get(),
                            )
                        ),
                    )
                return list(history_items_by_key.values())

            refresh_history_items()
            seen_video_keys = set()

            while True:
                history_items = refresh_history_items()
                pending_items = [
                    item for item in history_items
                    if item.get('key') not in processed_item_keys
                ]
                if not pending_items:
                    break

                history_item = pending_items[0]
                index = int(history_item.get('index') or (len(processed_item_keys) + 1))
                total = max(len(history_items), index)
                url = history_item['url']
                current_key = history_item['key']
                self.active_history_item_key = current_key

                if self.cancel_requested:
                    interrupted = True
                    break

                if session.is_completed(current_key):
                    resumed_completed += 1
                    seen_video_keys.add(current_key)
                    processed_item_keys.add(current_key)
                    progress = (index / max(total, 1)) * 100
                    schedule_ui(self, lambda p=progress: self.progress_var.set(p))
                    schedule_ui(
                        self,
                        lambda i=index, t=total: self.status_var.set(
                            join_status_with_timing(
                                f"{i}/{t} | JSON: უკვე დასრულებული იყო — გამოტოვდა",
                                self.download_started_at,
                                percent=(i / max(t, 1)) * 100,
                            )
                        ),
                    )
                    continue

                session.begin_item(current_key, url=url, title=history_item.get('title', ''), index=index)

                try:
                    schedule_ui(
                        self,
                        lambda i=index, t=total: self.status_var.set(
                            join_status_with_timing(
                                f"{i}/{t} | ლინკის ანალიზი...",
                                self.download_started_at,
                                percent=((i - 1) / max(t, 1)) * 100,
                            )
                        ),
                    )
                    schedule_ui(self, lambda i=index, t=total: self.progress_var.set(((i - 1) / max(t, 1)) * 100))

                    info = fetch_video_metadata(url, noplaylist=True)
                    if not isinstance(info, dict) or not info.get('formats'):
                        raise ValueError('ამ რეჟიმში ჩასვით პირდაპირ ვიდეო ლინკები. არხისა და პლეილისტისთვის გამოიყენეთ შესაბამისი ფანჯარა.')

                    title = info.get('title', 'უცნობი ვიდეო')
                    item_record = session.get_item(current_key)
                    if item_record is not None:
                        item_record['title'] = title
                        session._save()

                    video_key = canonical_video_key(url, info=info) or current_key
                    if video_key in seen_video_keys:
                        skipped_duplicates += 1
                        session.mark_skipped_duplicate(current_key, duplicate_of=video_key)
                        processed_item_keys.add(current_key)
                        schedule_ui(
                            self,
                            lambda i=index, t=total: self.status_var.set(
                                join_status_with_timing(
                                    f"{i}/{t} | დუბლიკატი აღმოჩნდა — გამოტოვდა",
                                    self.download_started_at,
                                    percent=(i / max(t, 1)) * 100,
                                )
                            ),
                        )
                        continue

                    output_template = self._build_bulk_output_template(session.output_dir, index, total)
                    if get_owner_setting(self, 'skip_existing_name', self.skip_existing_name_var, True):
                        existing_path = find_existing_media_by_output_name(
                            session.output_dir,
                            output_template,
                            title,
                            format_kind,
                        )
                        if existing_path:
                            skipped_duplicates += 1
                            skipped_existing_by_name += 1
                            session.mark_skipped_duplicate(current_key, duplicate_of=f"existing_file:{os.path.basename(existing_path)}")
                            processed_item_keys.add(current_key)
                            if video_key:
                                seen_video_keys.add(video_key)
                            schedule_ui(
                                self,
                                lambda i=index, t=total, name=os.path.basename(existing_path): self.status_var.set(
                                    join_status_with_timing(
                                        f"{i}/{t} | იგივე სახელის ფაილი უკვე არსებობს — გამოტოვდა: {name[:70]}",
                                        self.download_started_at,
                                        percent=(i / max(t, 1)) * 100,
                                    )
                                ),
                            )
                            continue

                    ydl_opts = get_max_quality_options()
                    ydl_opts.update({
                        'windowsfilenames': True,
                        'noplaylist': True,
                    })

                    if format_kind == 'video':
                        requested_quality = get_owner_setting(self, 'quality', self.quality_var, 'best')
                        effective_quality, effective_height, used_fallback, used_size_limit, _estimated_size, size_limit_from_height = resolve_video_quality_with_size_limit(
                            info,
                            requested_quality,
                            size_limit_enabled=get_owner_setting(self, 'limit_size', self.limit_size_var, False),
                        )
                        if used_size_limit:
                            size_limit_fallbacks += 1
                        ensure_video_pipeline_ready(info, effective_quality)
                        video_opts, container, target_height = build_video_download_options(info, effective_quality)
                        ydl_opts.update(video_opts)
                        self._set_item_postprocess_estimate(index, estimate_expected_postprocess_total_seconds(info, format_kind='video'))

                        quality_text = quality_label_from_height(target_height) if target_height else 'მაქსიმალური ხარისხი'
                        container_text = (container or 'AUTO').upper()
                        status_parts = []
                        if used_fallback:
                            status_parts.append('fallback')
                        if used_size_limit:
                            from_quality_text = quality_label_from_height(size_limit_from_height) if size_limit_from_height else 'უცნობი'
                            status_parts.append(f'2GB: {from_quality_text} → {quality_text}')
                        suffix = f" | {' | '.join(status_parts)}" if status_parts else ''

                        def update_bulk_video_status(i=index, t=total, q=quality_text, c=container_text, sf=suffix, name=title):
                            short_name = name[:55] + ('...' if len(name) > 55 else '')
                            self.status_var.set(
                                join_status_with_timing(
                                    f"{i}/{t} | {q} | {c}{sf} | {short_name}",
                                    self.download_started_at,
                                    percent=((i - 1) / max(t, 1)) * 100,
                                )
                            )
                        schedule_ui(self, update_bulk_video_status)
                    else:
                        self._set_item_postprocess_estimate(index, estimate_expected_postprocess_total_seconds(info, format_kind='audio'))
                        ydl_opts.update(get_audio_download_options(get_owner_setting(self, 'audio_quality', self.audio_quality_var, '320')))
                        schedule_ui(
                            self,
                            lambda i=index, t=total: self.status_var.set(
                                join_status_with_timing(
                                    f"{i}/{t} | საუკეთესო აუდიოს ჩამოტვირთვა იწყება...",
                                    self.download_started_at,
                                    percent=((i - 1) / max(t, 1)) * 100,
                                )
                            ),
                        )

                    ydl_opts = prepare_history_download_options(
                        ydl_opts,
                        session,
                        current_key,
                        output_template,
                        ui_progress_hook=self._make_progress_hook(index, total),
                        ui_postprocessor_hook=self._make_postprocessor_hook(index, total, title),
                        stop_checker=lambda: bool(self.cancel_requested),
                    )

                    ydl_download_with_cookie_fallback([url], ydl_opts)
                    session.mark_completed(current_key)
                    seen_video_keys.add(video_key)
                    successful += 1
                    processed_item_keys.add(current_key)
                except Exception as exc:
                    if is_user_requested_stop_error(exc) or self.cancel_requested:
                        interrupted = True
                        session.mark_interrupted(current_key, 'ჩამოტვირთვა შეწყდა დასრულებამდე.')
                        break
                    session.mark_failed(current_key, exc)
                    failed += 1
                    failed_urls.append((url, summarize_error_for_ui(exc)))
                    processed_item_keys.add(current_key)
                    continue

            session.finalize(interrupted=interrupted)
            schedule_ui(
                self,
                lambda: self._download_complete(
                    successful,
                    failed,
                    skipped_duplicates,
                    size_limit_fallbacks,
                    len(history_items_by_key),
                    failed_urls,
                    resumed_completed=resumed_completed,
                    skipped_existing_by_name=skipped_existing_by_name,
                    output_path=session.output_dir,
                    interrupted=interrupted,
                ),
            )
        except Exception as exc:
            if session is not None and current_key and not is_user_requested_stop_error(exc):
                session.mark_failed(current_key, exc)
                session.finalize()
            schedule_ui(self, lambda err=format_ydl_error(exc): self._download_error(err))
        finally:
            if session is not None:
                session.close()
            self.active_history_session = None
            self.active_history_item_key = None

    def _download_complete(self, successful, failed, skipped_duplicates, size_limit_fallbacks, total, failed_urls,
                           resumed_completed=0, skipped_existing_by_name=0, output_path=None, interrupted=False):
        self.is_downloading = False
        self.download_btn.configure(state="normal", text="ყველას ჩამოტვირთვა", bg=ModernStyle.PINK)
        processed = successful + skipped_duplicates + resumed_completed
        self.progress_var.set((processed / max(total, 1)) * 100 if interrupted else (100 if total else 0))
        final_path = output_path or self.active_output_path or get_owner_setting(self, 'download_path', self.download_path)
        duplicate_by_id = max(0, int(skipped_duplicates or 0) - int(skipped_existing_by_name or 0))

        if interrupted:
            self.status_var.set(join_status_with_timing(
                f"შეჩერდა | ახალი: {successful} | JSON: {resumed_completed} | სახელით: {skipped_existing_by_name} | დუბლიკატი: {duplicate_by_id} | შეცდომა: {failed}",
                self.download_started_at,
                percent=(processed / max(total, 1)) * 100,
            ))
            messagebox.showinfo(
                "ჩამოტვირთვა შეჩერდა",
                f"დასრულებული ელემენტები JSON-ში შენახულია. შემდეგ გაშვებაზე გაგრძელდება პირველი დაუსრულებელი ელემენტიდან.\n\nსაქაღალდე:\n{final_path}",
            )
            return

        if failed:
            self.status_var.set(join_status_with_timing(
                f"დასრულდა ნაწილობრივ: ახალი {successful}, JSON {resumed_completed}, სახელით {skipped_existing_by_name}, დუბლიკატი {duplicate_by_id}, 2GB↓ {size_limit_fallbacks}, შეცდომა {failed}",
                self.download_started_at,
                percent=100,
            ))
            preview = ''
            if failed_urls:
                lines = []
                for url, err in failed_urls[:3]:
                    lines.append(f"• {url}\n  {err}")
                preview = "\n\nპირველი შეცდომები:\n" + "\n".join(lines)
                if len(failed_urls) > 3:
                    preview += f"\n... და კიდევ {len(failed_urls) - 3}"
            messagebox.showwarning(
                "დასრულდა ნაწილობრივ",
                f"სულ: {total}\nახლად ჩაწერილი: {successful}\nJSON-ით უკვე დასრულებული: {resumed_completed}\nიგივე სახელით გამოტოვებული: {skipped_existing_by_name}\nID დუბლიკატი: {duplicate_by_id}\nშეცდომა: {failed}{preview}\n\nფაილების საქაღალდე:\n{final_path}\n\nJSON ისტორია:\n{get_download_history_root()}",
            )
        else:
            self.status_var.set(join_status_with_timing(
                f"დასრულდა: ახალი {successful}, JSON {resumed_completed}, სახელით {skipped_existing_by_name}, დუბლიკატი {duplicate_by_id}, 2GB↓ {size_limit_fallbacks}",
                self.download_started_at,
                percent=100,
            ))
            messagebox.showinfo(
                "წარმატება",
                f"სულ: {total}\nახლად ჩაწერილი: {successful}\nJSON-ით უკვე დასრულებული: {resumed_completed}\nიგივე სახელით გამოტოვებული: {skipped_existing_by_name}\nID დუბლიკატი: {duplicate_by_id}\n\nფაილების საქაღალდე:\n{final_path}\n\nJSON ისტორია:\n{get_download_history_root()}",
            )

    def _download_error(self, error):
        self.is_downloading = False
        self.download_btn.configure(state="normal", text="ყველას ჩამოტვირთვა", bg=ModernStyle.PINK)
        self.status_var.set(join_status_with_timing("შეცდომა!", self.download_started_at))
        messagebox.showerror("შეცდომა", error)

class ChannelDownloaderWindow:
    """არხის ვიდეოების ჩამოტვირთვის ფანჯარა"""

    def __init__(self, parent):
        self.window = tk.Toplevel(parent)
        self.window.title(t('window_title_channel'))
        self.window.geometry("820x860")
        self.window.minsize(720, 640)
        self.window.configure(bg=ModernStyle.BG_MAIN)

        # ცვლადები
        self.channel_url_var = tk.StringVar()
        self.download_path = tk.StringVar(value=os.path.expanduser("~/Downloads"))
        self.history_name_var = tk.StringVar(value="")
        self.format_var = tk.StringVar(value="video")
        self.quality_var = tk.StringVar(value="best")
        self.audio_quality_var = tk.StringVar(value="320")
        self.limit_size_var = tk.BooleanVar(value=False)
        self.skip_existing_name_var = tk.BooleanVar(value=True)
        self.numbered_filenames_var = tk.BooleanVar(value=False)
        self.progress_var = tk.DoubleVar(value=0)
        self.status_var = tk.StringVar(value="შეიყვანეთ არხის ლინკი")
        self.start_date_var = tk.StringVar(value="")
        self.end_date_var = tk.StringVar(value="")
        self.video_search_var = tk.StringVar(value="")
        self.select_count_var = tk.StringVar(value="")

        self.is_downloading = False
        self.cancel_requested = False
        self.active_history_session = None
        self.active_history_item_key = None
        self.active_output_path = None
        self.download_started_at = None
        self.item_timing_states = {}
        self.channel_name = ""
        self.channel_videos = []
        self.filtered_videos = []
        self.video_checkboxes = []
        self.video_vars = {}
        self.stop_download = False
        self.is_search_mode = False      # True — YouTube search URL-ია მიცემული
        self.search_query_text = ""      # search URL-ის საძიებო ტექსტი

        self.create_widgets()
        self.window.protocol("WM_DELETE_WINDOW", lambda: close_downloader_window(self))

    def create_widgets(self):
        scroll_frame = ScrollableFrame(self.window, style="Main.TFrame")
        scroll_frame.pack(fill="both", expand=True)

        main_frame = scroll_frame.scrollable_frame
        content = ttk.Frame(main_frame, style="Main.TFrame")
        content.pack(fill="both", expand=True, padx=25, pady=20)

        # სათაური
        title_frame = ttk.Frame(content, style="Main.TFrame")
        title_frame.pack(fill="x", pady=(0, 15))

        self.title_label = ttk.Label(title_frame, text="Channel Video Downloader", style="Title.TLabel")
        self.title_label.pack()
        self.subtitle_label = ttk.Label(title_frame, text="არხის ვიდეოების ძებნა, მონიშვნა და ჩამოტვირთვა", style="Subtitle.TLabel")
        self.subtitle_label.pack(pady=(3, 0))

        back_btn = tk.Button(title_frame, text="< უკან", font=("Segoe UI", 10),
                             bg=ModernStyle.BG_INPUT, fg=ModernStyle.TEXT_GRAY,
                             relief="flat", cursor="hand2", command=lambda: close_downloader_window(self))
        back_btn.pack(anchor="w", pady=(10, 0))

        # არხის URL
        url_card = ttk.Frame(content, style="Card.TFrame")
        url_card.pack(fill="x", pady=8, ipady=12, ipadx=12)

        ttk.Label(url_card, text="YouTube არხის ლინკი", style="CardBold.TLabel").pack(anchor="w", padx=12, pady=(12, 6))

        url_frame = ttk.Frame(url_card, style="Card.TFrame")
        url_frame.pack(fill="x", padx=12, pady=(0, 12))

        self.url_entry = tk.Entry(url_frame, textvariable=self.channel_url_var,
                                  font=("Segoe UI", 11), bg=ModernStyle.BG_INPUT,
                                  fg=ModernStyle.TEXT, insertbackground=ModernStyle.ACCENT_GLOW,
                                  relief="flat", bd=0)
        self.url_entry.pack(side="left", fill="x", expand=True, ipady=10, padx=(0, 8))
        self.url_entry.insert(0, "მაგ: https://www.youtube.com/@ChannelName")
        self.url_entry.bind("<FocusIn>", lambda e: self._clear_placeholder())
        self._setup_entry_bindings(self.url_entry)

        self.fetch_btn = tk.Button(url_frame, text="ვიდეოების ძებნა",
                                   font=("Segoe UI", 10, "bold"),
                                   bg=ModernStyle.CYAN, fg=ModernStyle.BG_MAIN,
                                   relief="flat", cursor="hand2",
                                   command=self.fetch_channel_videos)
        self.fetch_btn.pack(side="right", ipadx=12, ipady=6)

        # თარიღების სექცია
        date_card = ttk.Frame(content, style="Card.TFrame")
        date_card.pack(fill="x", pady=8, ipady=12, ipadx=12)

        ttk.Label(date_card, text="თარიღის დიაპაზონი (არასავალდებულო)", style="CardBold.TLabel").pack(anchor="w", padx=12, pady=(12, 6))

        date_frame = ttk.Frame(date_card, style="Card.TFrame")
        date_frame.pack(fill="x", padx=12, pady=(0, 12))

        start_frame = ttk.Frame(date_frame, style="Card.TFrame")
        start_frame.pack(side="left", fill="x", expand=True, padx=(0, 10))

        ttk.Label(start_frame, text="საწყისი თარიღი:", style="Info.TLabel").pack(anchor="w")
        self.start_date_entry = tk.Entry(start_frame, textvariable=self.start_date_var,
                                         font=("Segoe UI", 10), bg=ModernStyle.BG_INPUT,
                                         fg=ModernStyle.TEXT, relief="flat", width=15)
        self.start_date_entry.pack(anchor="w", ipady=6, pady=(4, 0))
        self.start_date_entry.insert(0, "YYYY-MM-DD")
        self.start_date_entry.bind("<FocusIn>", lambda e: self._clear_date_placeholder(self.start_date_entry))
        self._setup_entry_bindings(self.start_date_entry)
        self.start_date_entry.bind("<KeyRelease>", self.on_filter_change)

        end_frame = ttk.Frame(date_frame, style="Card.TFrame")
        end_frame.pack(side="left", fill="x", expand=True, padx=(10, 0))

        ttk.Label(end_frame, text="საბოლოო თარიღი:", style="Info.TLabel").pack(anchor="w")
        self.end_date_entry = tk.Entry(end_frame, textvariable=self.end_date_var,
                                       font=("Segoe UI", 10), bg=ModernStyle.BG_INPUT,
                                       fg=ModernStyle.TEXT, relief="flat", width=15)
        self.end_date_entry.pack(anchor="w", ipady=6, pady=(4, 0))
        self.end_date_entry.insert(0, "YYYY-MM-DD")
        self.end_date_entry.bind("<FocusIn>", lambda e: self._clear_date_placeholder(self.end_date_entry))
        self._setup_entry_bindings(self.end_date_entry)
        self.end_date_entry.bind("<KeyRelease>", self.on_filter_change)

        ttk.Label(date_card, text="დატოვეთ ცარიელი ყველა ვიდეოს სანახავად", style="Info.TLabel").pack(anchor="w", padx=12, pady=(0, 8))

        # არხის ინფორმაცია და ვიდეოების სია
        info_card = ttk.Frame(content, style="Card.TFrame")
        info_card.pack(fill="x", pady=8, ipady=12, ipadx=12)

        self.channel_info_label = ttk.Label(info_card, text="არხი: ჯერ არ არის არჩეული",
                                            style="Card.TLabel", wraplength=700)
        self.channel_info_label.pack(anchor="w", padx=12, pady=(12, 4))

        self.videos_count_label = ttk.Label(info_card, text="ვიდეოები: -", style="Info.TLabel")
        self.videos_count_label.pack(anchor="w", padx=12, pady=(0, 8))

        search_frame = ttk.Frame(info_card, style="Card.TFrame")
        search_frame.pack(fill="x", padx=12, pady=(0, 8))

        ttk.Label(search_frame, text="სერჩი:", style="Info.TLabel").pack(side="left", padx=(0, 8))

        self.video_search_entry = tk.Entry(search_frame, textvariable=self.video_search_var,
                                           font=("Segoe UI", 10), bg=ModernStyle.BG_INPUT,
                                           fg=ModernStyle.TEXT, insertbackground=ModernStyle.ACCENT_GLOW,
                                           relief="flat")
        self.video_search_entry.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 8))
        self._setup_entry_bindings(self.video_search_entry)
        self.video_search_entry.bind("<KeyRelease>", self.on_video_search_change)

        search_hint_label = ttk.Label(info_card,
                                      text="შეგიძლია რამდენიმე სახელი ჩაწერო მძიმეებით, ; ან ახალ ხაზზე — ან პირდაპირ ატვირთო TXT/JSON",
                                      style="Info.TLabel")
        search_hint_label.pack(anchor="w", padx=12, pady=(0, 8))

        clear_search_btn = tk.Button(search_frame, text="გასუფთავება",
                                     font=("Segoe UI", 9), bg=ModernStyle.BG_INPUT,
                                     fg=ModernStyle.TEXT_GRAY, relief="flat", cursor="hand2",
                                     command=self.clear_video_search)
        clear_search_btn.pack(side="right", ipadx=8, ipady=4)

        import_search_btn = tk.Button(search_frame, text="TXT/JSON ატვირთვა",
                                      font=("Segoe UI", 9), bg=ModernStyle.WARNING,
                                      fg=ModernStyle.BG_MAIN, relief="flat", cursor="hand2",
                                      command=self.import_search_file)
        import_search_btn.pack(side="right", ipadx=8, ipady=4, padx=(0, 8))

        self.info_card = info_card

        select_btns_frame = ttk.Frame(info_card, style="Card.TFrame")
        self.select_btns_frame = select_btns_frame
        select_btns_frame.pack(fill="x", padx=12, pady=(0, 8))

        select_all_btn = tk.Button(select_btns_frame, text="ყველას მონიშვნა",
                                   font=("Segoe UI", 9), bg=ModernStyle.SUCCESS,
                                   fg=ModernStyle.TEXT, relief="flat", cursor="hand2",
                                   command=self.select_all_videos)
        select_all_btn.pack(side="left", ipadx=8, ipady=3, padx=(0, 8))

        deselect_all_btn = tk.Button(select_btns_frame, text="ყველას მოხსნა",
                                     font=("Segoe UI", 9), bg=ModernStyle.BG_INPUT,
                                     fg=ModernStyle.TEXT_GRAY, relief="flat", cursor="hand2",
                                     command=self.deselect_all_videos)
        deselect_all_btn.pack(side="left", ipadx=8, ipady=3, padx=(0, 8))

        select_filtered_btn = tk.Button(select_btns_frame, text="მოძებნილის მონიშვნა",
                                        font=("Segoe UI", 9), bg=ModernStyle.CYAN,
                                        fg=ModernStyle.BG_MAIN, relief="flat", cursor="hand2",
                                        command=self.select_filtered_videos)
        select_filtered_btn.pack(side="left", ipadx=8, ipady=3, padx=(0, 8))

        deselect_filtered_btn = tk.Button(select_btns_frame, text="მოძებნილის მოხსნა",
                                          font=("Segoe UI", 9), bg=ModernStyle.PINK,
                                          fg=ModernStyle.TEXT, relief="flat", cursor="hand2",
                                          command=self.deselect_filtered_videos)
        deselect_filtered_btn.pack(side="left", ipadx=8, ipady=3, padx=(0, 8))

        select_range_label = ttk.Label(select_btns_frame, text="1-N:", style="Info.TLabel")
        select_range_label.pack(side="left", padx=(8, 6))

        self.select_count_entry = tk.Entry(select_btns_frame, textvariable=self.select_count_var,
                                           font=("Segoe UI", 9), bg=ModernStyle.BG_INPUT,
                                           fg=ModernStyle.TEXT, insertbackground=ModernStyle.ACCENT_GLOW,
                                           relief="flat", width=10)
        self.select_count_entry.pack(side="left", ipady=4, padx=(0, 8))
        self._setup_entry_bindings(self.select_count_entry)
        self.select_count_entry.bind("<Return>", self.select_first_n_videos)
        self.select_count_entry.bind("<KP_Enter>", self.select_first_n_videos)

        select_first_n_btn = tk.Button(select_btns_frame, text="მონიშვნა რაოდენობით",
                                       font=("Segoe UI", 9), bg=ModernStyle.WARNING,
                                       fg=ModernStyle.BG_MAIN, relief="flat", cursor="hand2",
                                       command=self.select_first_n_videos)
        select_first_n_btn.pack(side="left", ipadx=8, ipady=3, padx=(0, 8))

        self.numbered_names_btn = tk.Button(select_btns_frame, text="სახელები: ნომერი+სათაური",
                                            font=("Segoe UI", 9), bg=ModernStyle.BG_INPUT,
                                            fg=ModernStyle.TEXT_GRAY, relief="flat", cursor="hand2",
                                            command=self.toggle_numbered_filenames)
        self.numbered_names_btn.pack(side="left", ipadx=8, ipady=3)
        self._update_numbered_option_button()

        select_range_hint = ttk.Label(info_card,
                                      text="რიცხვი: 64 → მონიშნავს 1-64-ს | დიაპაზონი: 5-20 → მონიშნავს 5-20-ს (რაც ახლა ჩანს იმ სიაში)",
                                      style="Info.TLabel")
        select_range_hint.pack(anchor="w", padx=12, pady=(0, 8))

        videos_list_frame = ttk.Frame(info_card, style="Card.TFrame")
        videos_list_frame.pack(fill="x", padx=12, pady=(0, 12))

        self.videos_canvas = tk.Canvas(videos_list_frame, bg=ModernStyle.BG_CARD,
                                       highlightthickness=0, height=210)
        self.videos_scrollbar = ttk.Scrollbar(videos_list_frame, orient="vertical",
                                              command=self.videos_canvas.yview)
        self.videos_inner_frame = ttk.Frame(self.videos_canvas, style="Card.TFrame")

        self.videos_inner_frame.bind(
            "<Configure>",
            lambda e: self.videos_canvas.configure(scrollregion=self.videos_canvas.bbox("all"))
        )

        self.videos_canvas_window = self.videos_canvas.create_window((0, 0), window=self.videos_inner_frame, anchor="nw")
        self.videos_canvas.configure(yscrollcommand=self.videos_scrollbar.set)
        self.videos_canvas.bind(
            "<Configure>",
            lambda e: self.videos_canvas.itemconfig(self.videos_canvas_window, width=e.width)
        )

        self.videos_canvas.pack(side="left", fill="both", expand=True)
        self.videos_scrollbar.pack(side="right", fill="y")

        # ფორმატი და ხარისხი
        options_card = ttk.Frame(content, style="Card.TFrame")
        options_card.pack(fill="x", pady=8, ipady=12, ipadx=12)

        options_row = ttk.Frame(options_card, style="Card.TFrame")
        options_row.pack(fill="x", padx=12, pady=12)

        format_frame = ttk.Frame(options_row, style="Card.TFrame")
        format_frame.pack(side="left", fill="x", expand=True)

        ttk.Label(format_frame, text="ფორმატი", style="CardBold.TLabel").pack(anchor="w", pady=(0, 8))

        radio_frame = ttk.Frame(format_frame, style="Card.TFrame")
        radio_frame.pack(anchor="w")

        ttk.Radiobutton(radio_frame, text="ვიდეო (1080p↓ MP4 | 1080p↑ AUTO: MKV/WEBM)", variable=self.format_var,
                        value="video", style="Card.TRadiobutton",
                        command=self.toggle_quality).pack(side="left", padx=(0, 15))
        ttk.Radiobutton(radio_frame, text="აუდიო (MP3)", variable=self.format_var,
                        value="audio", style="Card.TRadiobutton",
                        command=self.toggle_quality).pack(side="left")

        quality_frame = ttk.Frame(options_row, style="Card.TFrame")
        quality_frame.pack(side="right")

        ttk.Label(quality_frame, text="ხარისხი", style="CardBold.TLabel").pack(anchor="w", pady=(0, 8))

        self.video_quality_frame = ttk.Frame(quality_frame, style="Card.TFrame")
        self.video_quality_frame.pack(anchor="w")

        quality_values = ["ავტომატური მაქსიმუმი თითო ვიდეოზე", "2160p (4K)", "1440p (2K)", "1080p (Full HD)", "720p (HD)", "480p", "360p"]
        self.quality_combo = ttk.Combobox(self.video_quality_frame, values=quality_values,
                                          state="readonly", font=("Segoe UI", 10), width=26)
        self.quality_combo.set("ავტომატური მაქსიმუმი თითო ვიდეოზე")
        self.quality_combo.pack()
        self.quality_combo.bind("<<ComboboxSelected>>", self.on_quality_change)

        self.limit_size_check = ttk.Checkbutton(
            self.video_quality_frame,
            text="თუ 2GB ან მეტია, დაბალ ხარისხზე ჩამოვიდეს",
            variable=self.limit_size_var,
            style="Card.TCheckbutton"
        )
        self.limit_size_check.pack(anchor="w", pady=(8, 0))

        self.audio_quality_frame = ttk.Frame(quality_frame, style="Card.TFrame")

        audio_values = ["320 kbps (საუკეთესო)", "256 kbps", "192 kbps", "128 kbps"]
        self.audio_combo = ttk.Combobox(self.audio_quality_frame, values=audio_values,
                                        state="readonly", font=("Segoe UI", 10), width=18)
        self.audio_combo.set("320 kbps (საუკეთესო)")
        self.audio_combo.pack()
        self.audio_combo.bind("<<ComboboxSelected>>", self.on_audio_quality_change)

        # შენახვის ადგილი
        path_card = ttk.Frame(content, style="Card.TFrame")
        path_card.pack(fill="x", pady=8, ipady=12, ipadx=12)

        ttk.Label(path_card, text="შენახვის ადგილი", style="CardBold.TLabel").pack(anchor="w", padx=12, pady=(12, 6))

        path_frame = ttk.Frame(path_card, style="Card.TFrame")
        path_frame.pack(fill="x", padx=12, pady=(0, 12))

        self.path_entry = tk.Entry(path_frame, textvariable=self.download_path,
                                   font=("Segoe UI", 10), bg=ModernStyle.BG_INPUT,
                                   fg=ModernStyle.TEXT, relief="flat")
        self.path_entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 8))
        self._setup_entry_bindings(self.path_entry)

        browse_btn = tk.Button(path_frame, text="არჩევა", font=("Segoe UI", 9),
                               bg=ModernStyle.BG_INPUT, fg=ModernStyle.TEXT_GRAY,
                               relief="flat", cursor="hand2", command=self.browse_folder)
        browse_btn.pack(side="right", ipadx=10, ipady=5)

        add_skip_existing_name_control(path_card, self.skip_existing_name_var)

        self.history_name_entry = add_history_name_controls(
            path_card,
            self.history_name_var,
            self._setup_entry_bindings,
        )

        # პროგრესი
        progress_frame = ttk.Frame(content, style="Main.TFrame")
        progress_frame.pack(fill="x", pady=12)

        self.status_label = ttk.Label(progress_frame, textvariable=self.status_var, style="Subtitle.TLabel")
        self.status_label.pack(anchor="w", pady=(0, 6))

        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var,
                                            maximum=100, style="Accent.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x")
        install_visible_progress(self, progress_frame)

        # ღილაკები
        buttons_frame = ttk.Frame(content, style="Main.TFrame")
        buttons_frame.pack(fill="x", pady=15)

        self.download_btn = tk.Button(buttons_frame, text="მონიშნულის ჩამოტვირთვა (MAX)",
                                      font=("Segoe UI", 14, "bold"),
                                      bg=ModernStyle.ACCENT, fg=ModernStyle.TEXT,
                                      relief="flat", cursor="hand2",
                                      command=self.start_download)
        self.download_btn.pack(side="left", fill="x", expand=True, ipady=12, padx=(0, 5))

        self.stop_btn = tk.Button(buttons_frame, text="გაჩერება",
                                  font=("Segoe UI", 14, "bold"),
                                  bg=ModernStyle.WARNING, fg=ModernStyle.BG_MAIN,
                                  relief="flat", cursor="hand2", state="disabled",
                                  command=self.stop_downloading)
        self.stop_btn.pack(side="right", ipadx=20, ipady=12, padx=(5, 0))

    def _setup_entry_bindings(self, entry):
        for sequence in ("<Control-v>", "<Control-V>", "<Control-Insert>", "<Shift-Insert>", "<<Paste>>"):
            entry.bind(sequence, lambda e, w=entry: self._paste_text(w))
        entry.bind("<Control-KeyPress>", lambda e, w=entry: handle_keyboard_shortcut(e, self.window, w), add="+")
        entry.bind("<Shift-KeyPress-Insert>", lambda e, w=entry: handle_keyboard_shortcut(e, self.window, w), add="+")
        entry.bind("<Control-a>", lambda e: self._select_all(entry))
        entry.bind("<Control-A>", lambda e: self._select_all(entry))

    def _paste_text(self, entry):
        return paste_into_entry(self.window, entry)

    def _select_all(self, entry):
        entry.select_range(0, "end")
        entry.icursor("end")
        return "break"

    def _clear_placeholder(self):
        if self.url_entry.get().startswith("მაგ:"):
            self.url_entry.delete(0, "end")

    def _clear_date_placeholder(self, entry):
        if entry.get() == "YYYY-MM-DD":
            entry.delete(0, "end")

    def _update_numbered_option_button(self):
        if not hasattr(self, 'numbered_names_btn'):
            return
        if get_owner_setting(self, 'numbered_filenames', self.numbered_filenames_var, False):
            self.numbered_names_btn.configure(text="სახელები: 1,2,3 ON", bg=ModernStyle.SUCCESS, fg=ModernStyle.TEXT)
        else:
            self.numbered_names_btn.configure(text="სახელები: ნომერი+სათაური", bg=ModernStyle.BG_INPUT, fg=ModernStyle.TEXT_GRAY)

    def toggle_numbered_filenames(self):
        self.numbered_filenames_var.set(not get_owner_setting(self, 'numbered_filenames', self.numbered_filenames_var, False))
        self._update_numbered_option_button()
        if get_owner_setting(self, 'numbered_filenames', self.numbered_filenames_var, False):
            self.status_var.set("ჩართულია: ფაილის სახელები იქნება 001, 002, 003...")
        else:
            self.status_var.set("ჩართულია: ფაილის სახელები იქნება 001 - სათაური")

    def _build_channel_output_template(self, output_path, index, total_count):
        return build_numbered_output_template(output_path, index, total_count, get_owner_setting(self, 'numbered_filenames', self.numbered_filenames_var, False))

    def toggle_quality(self):
        if get_owner_setting(self, 'format', self.format_var, 'video') == "video":
            self.audio_quality_frame.pack_forget()
            self.video_quality_frame.pack(anchor="w")
        else:
            self.video_quality_frame.pack_forget()
            self.audio_quality_frame.pack(anchor="w")

    def on_quality_change(self, event=None):
        self.quality_var.set(extract_quality_code_from_label(self.quality_combo.get()))

    def on_audio_quality_change(self, event=None):
        mapping = {"320 kbps (საუკეთესო)": "320", "256 kbps": "256", "192 kbps": "192", "128 kbps": "128"}
        self.audio_quality_var.set(mapping.get(self.audio_combo.get(), "320"))

    def browse_folder(self):
        folder = filedialog.askdirectory(initialdir=get_owner_setting(self, 'download_path', self.download_path))
        if folder:
            self.download_path.set(folder)

    def parse_date(self, date_str):
        if not date_str or date_str == "YYYY-MM-DD":
            return None
        try:
            return datetime.strptime(date_str.strip(), "%Y-%m-%d")
        except ValueError:
            return None

    def _get_video_var_key(self, video):
        return video.get('url') or video.get('id') or video.get('title')

    def _get_video_var(self, video):
        key = self._get_video_var_key(video)
        var = self.video_vars.get(key)
        if var is None:
            var = tk.BooleanVar(value=False)
            self.video_vars[key] = var
        return var

    def _count_selected_videos(self):
        return sum(1 for video in self.channel_videos if self._get_video_var(video).get())

    def _update_video_counts_text(self):
        total_count = len(self.channel_videos)
        visible_count = len(self.filtered_videos)
        selected_count = self._count_selected_videos()

        if total_count:
            extra = " | ძებნის შედეგი" if self.video_search_var.get().strip() else ""
            self.videos_count_label.configure(
                text=f"ნაპოვნია: {total_count} | გამოჩენილი: {visible_count} | მონიშნული: {selected_count}{extra}"
            )
        else:
            self.videos_count_label.configure(text="ვიდეოები: -")

    def select_all_videos(self):
        for video in self.channel_videos:
            self._get_video_var(video).set(True)
        self._render_videos()
        self.status_var.set(f"მოინიშნა ყველა ვიდეო: {self._count_selected_videos()}")

    def deselect_all_videos(self):
        for video in self.channel_videos:
            self._get_video_var(video).set(False)
        self._render_videos()
        self.status_var.set("ყველა მონიშვნა მოიხსნა")

    def _parse_visible_selection_range(self, raw_value):
        value = str(raw_value or '').strip()
        if not value:
            raise ValueError("შეიყვანე რიცხვი, მაგალითად 64")

        match = re.fullmatch(r'(\d+)\s*-\s*(\d+)', value)
        if match:
            start = int(match.group(1))
            end = int(match.group(2))
        else:
            if not re.fullmatch(r'\d+', value):
                raise ValueError("გამოიყენე მხოლოდ რიცხვი (64) ან დიაპაზონი (5-20)")
            start = 1
            end = int(value)

        if start <= 0 or end <= 0:
            raise ValueError("რიცხვი უნდა იყოს 1-ზე დიდი ან ტოლი")
        if start > end:
            raise ValueError("დასაწყისი რიცხვი ბოლოზე დიდი ვერ იქნება")

        return start, end

    def select_first_n_videos(self, event=None):
        try:
            start, end = self._parse_visible_selection_range(self.select_count_var.get())
        except ValueError as e:
            messagebox.showwarning("გაფრთხილება", str(e))
            return "break"

        visible_videos = self.filtered_videos
        if not visible_videos:
            messagebox.showwarning("გაფრთხილება", "მოსანიშნი ვიდეოები არ ჩანს")
            return "break"

        start_index = start - 1
        end_index = min(end, len(visible_videos))
        if start_index >= len(visible_videos):
            messagebox.showwarning(
                "გაფრთხილება",
                f"სიაში ახლა მხოლოდ {len(visible_videos)} ვიდეო ჩანს, ამიტომ {start}-დან მონიშვნა ვერ შესრულდა"
            )
            return "break"

        selected_slice = visible_videos[start_index:end_index]
        for video in selected_slice:
            self._get_video_var(video).set(True)

        self._render_videos()
        if start == 1:
            self.status_var.set(f"მოინიშნა 1-{end_index} ვიდეო (ახლა გამოჩენილი სიიდან)")
        else:
            self.status_var.set(f"მოინიშნა {start}-{end_index} ვიდეო (ახლა გამოჩენილი სიიდან)")
        return "break"

    def select_filtered_videos(self):
        for video in self.filtered_videos:
            self._get_video_var(video).set(True)
        self._render_videos()
        self.status_var.set(f"მოინიშნა მხოლოდ გაფილტრული/მოძებნილი ვიდეოები: {len(self.filtered_videos)}")

    def deselect_filtered_videos(self):
        for video in self.filtered_videos:
            self._get_video_var(video).set(False)
        self._render_videos()
        self.status_var.set(f"მოხსნილია მხოლოდ გაფილტრული/მოძებნილი ვიდეოები: {len(self.filtered_videos)}")

    def import_search_file(self):
        file_path = filedialog.askopenfilename(
            title="საძიებო სიის ატვირთვა",
            filetypes=[("Search files", "*.txt *.json"), ("Text files", "*.txt"), ("JSON files", "*.json"), ("All files", "*.*")]
        )
        if not file_path:
            return

        try:
            terms = read_search_terms_from_file(file_path)
        except Exception as e:
            messagebox.showerror("შეცდომა", f"ფაილის წაკითხვა ვერ მოხერხდა:\n{e}")
            return

        if not terms:
            messagebox.showwarning("გაფრთხილება", "ატვირთულ ფაილში საძიებო ფრაზები ვერ მოიძებნა")
            return

        self.video_search_var.set('\n'.join(terms))
        self.apply_video_filters()
        self.status_var.set(f"სერჩში ჩაიტვირთა {len(terms)} საძიებო ფრაზა ფაილიდან")

    def clear_video_search(self):
        self.video_search_var.set("")
        self.apply_video_filters()

    def on_video_search_change(self, event=None):
        self.apply_video_filters()

    def on_filter_change(self, event=None):
        if self.channel_videos:
            self.apply_video_filters()

    def apply_video_filters(self):
        start_date = self.parse_date(self.start_date_var.get())
        end_date = self.parse_date(self.end_date_var.get())
        queries = parse_multi_search_terms(self.video_search_var.get())

        self.filtered_videos = []
        for video in self.channel_videos:
            upload_date_str = video.get('upload_date', '')
            parsed_date = None
            if upload_date_str:
                try:
                    parsed_date = datetime.strptime(upload_date_str, '%Y%m%d')
                except Exception:
                    parsed_date = None
            video['parsed_date'] = parsed_date

            if start_date and parsed_date and parsed_date < start_date:
                continue
            if start_date and parsed_date is None:
                continue
            if end_date and parsed_date and parsed_date > end_date:
                continue
            if end_date and parsed_date is None:
                continue

            title_text = normalize_search_text(video.get('title') or '')
            if queries and not any(query in title_text for query in queries):
                continue

            self.filtered_videos.append(video)

        self._render_videos()

    def _render_videos(self):
        for widget in self.videos_inner_frame.winfo_children():
            widget.destroy()
        self.video_checkboxes = []

        total_count = len(self.channel_videos)
        visible_count = len(self.filtered_videos)
        self._update_video_counts_text()

        if not total_count:
            ttk.Label(self.videos_inner_frame, text="ვიდეოები ჯერ არ არის მოძებნილი",
                      style="Info.TLabel").pack(anchor="w", pady=5)
        elif not visible_count:
            ttk.Label(self.videos_inner_frame, text="ფილტრით/სერჩით ვერ მოიძებნა არცერთი ვიდეო",
                      style="Info.TLabel").pack(anchor="w", pady=5)
        else:
            for i, video in enumerate(self.filtered_videos, start=1):
                var = self._get_video_var(video)
                self.video_checkboxes.append(var)

                cb_frame = ttk.Frame(self.videos_inner_frame, style="Card.TFrame")
                cb_frame.pack(fill="x", pady=2)

                title = video.get('title') or 'უცნობი სათაური'
                kind_text = f"[{video.get('kind')}] " if video.get('kind') else ''
                date_text = ''
                if video.get('parsed_date'):
                    date_text = video['parsed_date'].strftime(' [%Y-%m-%d]')

                display_title = f"{kind_text}{title}"
                cb = ttk.Checkbutton(
                    cb_frame,
                    text=f"{i}. {display_title[:65]}{'...' if len(display_title) > 65 else ''}{date_text}",
                    variable=var,
                    style="Card.TCheckbutton",
                    command=self._update_video_counts_text
                )
                cb.pack(anchor="w")

        if total_count:
            selected_count = self._count_selected_videos()
            self.status_var.set(
                f"არხი მზადაა — ნაჩვენებია {visible_count}, მონიშნულია {selected_count}"
            )
        else:
            self.status_var.set("არხის ვიდეოები ვერ მოიძებნა")

    def fetch_channel_videos(self):
        url = self.channel_url_var.get().strip()
        if not url or url.startswith("მაგ:"):
            messagebox.showwarning("გაფრთხილება", "შეიყვანეთ YouTube არხის ლინკი!")
            return

        self.fetch_btn.configure(state="disabled", text="იძებნება...")
        self.status_var.set("არხის ვიდეოების მოძიება...")
        for video in self.channel_videos:
            self._get_video_var(video).set(False)
        self.channel_videos = []
        self.filtered_videos = []
        self.video_vars = {}
        self._render_videos()

        thread = threading.Thread(target=self._fetch_channel_thread, args=(url,))
        thread.daemon = True
        thread.start()

    def _fetch_channel_thread(self, url):
        try:
            videos_url = normalize_channel_videos_url(url)

            ydl_opts = get_max_quality_options()
            ydl_opts.update({
                'quiet': True,
                'no_warnings': True,
                'extract_flat': 'in_playlist',
                'ignoreerrors': True,
                'skip_download': True,
            })

            info = ydl_extract_info_with_cookie_fallback(videos_url, ydl_opts, download=False)
            channel_name = info.get('channel') or info.get('uploader') or info.get('title', 'უცნობი არხი')
            entries = info.get('entries', [])

            self.channel_videos = []
            for entry in entries:
                if entry is None:
                    continue

                entry_id = (entry.get('id') or '').strip()
                if not entry_id:
                    continue

                video_data = {
                    'id': entry_id,
                    'title': entry.get('title', 'უცნობი სათაური'),
                    'url': f"https://www.youtube.com/watch?v={entry_id}",
                    'upload_date': entry.get('upload_date', ''),
                    'kind': 'ვიდეო',
                }
                self.channel_videos.append(video_data)

            self.channel_name = channel_name
            schedule_ui(self, lambda: self._update_channel_info(channel_name))
        except Exception as e:
            err = format_ydl_error(e)
            schedule_ui(self, lambda err=err: self._show_fetch_error(err))

    def _update_channel_info(self, channel_name):
        self.fetch_btn.configure(state="normal", text="ვიდეოების ძებნა")
        self.channel_info_label.configure(text=f"არხი: {channel_name}")
        self.apply_video_filters()

    def _show_fetch_error(self, error):
        self.fetch_btn.configure(state="normal", text="ვიდეოების ძებნა")
        self.status_var.set("შეცდომა")
        messagebox.showerror("შეცდომა", f"არხის ვიდეოები ვერ მოიძებნა:\n{error}")

    def start_download(self):
        selected_videos = [video for video in self.channel_videos if self._get_video_var(video).get()]
        if not selected_videos:
            messagebox.showwarning("გაფრთხილება", "მონიშნეთ ჩამოსატვირთი ვიდეოები!")
            return

        if self.is_downloading:
            return

        self.is_downloading = True
        self.cancel_requested = False
        self.download_started_at = time.monotonic()
        self.item_timing_states = {}
        self.stop_download = False
        self.download_btn.configure(state="disabled", text="მიმდინარეობს...", bg=ModernStyle.WARNING)
        self.stop_btn.configure(state="normal")
        self.progress_var.set(0)
        names_text = "სახელები: 1,2,3" if get_owner_setting(self, 'numbered_filenames', self.numbered_filenames_var, False) else "სახელები: ნომერი + სათაური"
        self.status_var.set(join_status_with_timing(f"იწყება მონიშნული ვიდეოების ჩამოტვირთვა: {len(selected_videos)} | {names_text}", self.download_started_at, percent=0))

        self._download_settings_snapshot = capture_download_settings(self)
        thread = threading.Thread(target=self._download_selected_thread, args=(selected_videos,))
        thread.daemon = True
        thread.start()

    def stop_downloading(self):
        self.stop_download = True
        self.cancel_requested = True
        session = self.active_history_session
        if session is not None and self.active_history_item_key:
            session.mark_interrupted(self.active_history_item_key, 'მომხმარებელმა ჩამოტვირთვა შეაჩერა.')
        self.status_var.set(join_status_with_timing("ჩერდება... მიმდინარე ელემენტი დაუსრულებლად ჩაინიშნა", self.download_started_at))

    def _get_item_timing_state(self, index):
        state = self.item_timing_states.get(index)
        if state is None:
            state = {
                'predicted_postprocess_total': None,
                'postprocess_started_at': None,
                'current_postprocessor': '',
            }
            self.item_timing_states[index] = state
        return state

    def _set_item_postprocess_estimate(self, index, seconds):
        state = self._get_item_timing_state(index)
        if seconds:
            state['predicted_postprocess_total'] = max(float(state.get('predicted_postprocess_total') or 0), float(seconds))

    def _make_channel_postprocessor_hook(self, index, total, title=''):
        def hook(d):
            status = d.get('status')
            if status not in {'started', 'processing', 'finished'}:
                return

            state = self._get_item_timing_state(index)
            postprocessor_name = d.get('postprocessor') or state.get('current_postprocessor') or ''
            state['current_postprocessor'] = postprocessor_name

            if state.get('postprocess_started_at') is None and status in {'started', 'processing'}:
                state['postprocess_started_at'] = time.monotonic()

            if status == 'started' and d.get('info_dict'):
                predicted = estimate_postprocess_total_seconds(d.get('info_dict'), postprocessor_name)
                if predicted:
                    state['predicted_postprocess_total'] = max(float(state.get('predicted_postprocess_total') or 0), float(predicted))

            if status in {'started', 'processing'}:
                item_percent = get_postprocess_phase_percent(state.get('postprocess_started_at'), state.get('predicted_postprocess_total'))
                step_percent = get_postprocess_step_percent(state.get('postprocess_started_at'), state.get('predicted_postprocess_total'))
                overall = get_batch_overall_percent(index, total, item_percent)
                remaining = get_postprocess_remaining_seconds(state.get('postprocess_started_at'), state.get('predicted_postprocess_total'))
                short_title = f" | {title[:40]}{'...' if len(title) > 40 else ''}" if title else ''
                label = get_postprocessor_label(postprocessor_name)
                schedule_ui(self, lambda value=overall: self.progress_var.set(value))
                schedule_ui(self, lambda s=join_status_with_timing(
                    f"ჩამოტვირთვა {index}/{total} | {label}{short_title}", self.download_started_at, percent=overall,
                    remaining_seconds=remaining, display_percent=step_percent,
                ): self.status_var.set(s))
        return hook

    def _make_channel_progress_hook(self, index, total, title=''):
        def hook(d):
            state = self._get_item_timing_state(index)
            if d.get('status') == 'downloading':
                percent_str = d.get('_percent_str', '0%').strip()
                speed = d.get('_speed_str', 'N/A')
                eta = d.get('_eta_str', 'N/A')
                overall = None
                try:
                    percent = float(percent_str.replace('%', ''))
                    item_percent = get_download_phase_percent(percent, state.get('predicted_postprocess_total'))
                    overall = get_batch_overall_percent(index, total, item_percent)
                    schedule_ui(self, lambda value=overall: self.progress_var.set(value))
                except Exception:
                    overall = None
                short_title = f" | {title[:40]}{'...' if len(title) > 40 else ''}" if title else ''
                status = join_status_with_timing(
                    f"ჩამოტვირთვა {index}/{total} | {percent_str} | სიჩქარე: {speed} | დარჩენილი: {eta}{short_title}",
                    self.download_started_at,
                    percent=overall,
                )
                schedule_ui(self, lambda s=status: self.status_var.set(s))
            elif d.get('status') == 'finished':
                item_percent = get_download_phase_percent(100, state.get('predicted_postprocess_total'))
                overall = get_batch_overall_percent(index, total, item_percent)
                schedule_ui(self, lambda value=overall: self.progress_var.set(value))
                schedule_ui(self, lambda: self.status_var.set(join_status_with_timing(
                    f"ჩამოტვირთვა {index}/{total} | {get_postprocessor_label(state.get('current_postprocessor'))}",
                    self.download_started_at,
                    percent=overall,
                )))
        return hook

    def _download_selected_thread(self, videos):
        session = None
        current_key = None
        successful = 0
        failed = 0
        skipped_duplicates = 0
        skipped_existing_by_name = 0
        resumed_completed = 0
        size_limit_fallbacks = 0
        interrupted = False

        try:
            base_output_path = get_owner_setting(self, 'download_path', self.download_path)
            os.makedirs(base_output_path, exist_ok=True)

            class_job_types = {
                'ChannelDownloaderWindow': 'channel_videos',
                'ShortsDownloaderWindow': 'channel_shorts',
                'ChannelAndShortsDownloaderWindow': 'channel_and_shorts',
            }
            job_type = class_job_types.get(self.__class__.__name__, 'channel_download')
            source_url = self.channel_url_var.get().strip()
            sources = [source_url] + [video.get('url', '') for video in videos]
            settings = build_download_history_settings(
                get_owner_setting(self, 'format', self.format_var, 'video'),
                get_owner_setting(self, 'quality', self.quality_var, 'best'),
                get_owner_setting(self, 'audio_quality', self.audio_quality_var, '320'),
                get_owner_setting(self, 'limit_size', self.limit_size_var, False),
                get_owner_setting(self, 'skip_existing_name', self.skip_existing_name_var, True),
                search_mode=bool(getattr(self, 'is_search_mode', False)),
                range_start=90 if getattr(self, 'is_search_mode', False) else None,
            )
            session = DownloadHistorySession(
                base_output_path,
                job_type,
                sources,
                custom_name=get_owner_setting(self, 'history_name', self.history_name_var, ''),
                preferred_title=self.channel_name or job_type,
                settings=settings,
            )
            self.active_history_session = session
            self.active_output_path = session.output_dir

            history_items = [
                build_history_item(
                    video.get('url'),
                    index,
                    title=video.get('title', ''),
                    entry=video,
                    extra={'kind': video.get('kind')},
                )
                for index, video in enumerate(videos, start=1)
            ]
            session.ensure_items(history_items)

            total = len(videos)
            seen_video_keys = set()

            for i, (video, history_item) in enumerate(zip(videos, history_items), start=1):
                current_key = history_item['key']
                self.active_history_item_key = current_key

                if self.stop_download or self.cancel_requested:
                    interrupted = True
                    break

                if session.is_completed(current_key):
                    resumed_completed += 1
                    seen_video_keys.add(current_key)
                    schedule_ui(self, lambda p=(i / max(total, 1)) * 100: self.progress_var.set(p))
                    schedule_ui(
                        self,
                        lambda idx=i, t=total: self.status_var.set(
                            join_status_with_timing(
                                f"ჩამოტვირთვა {idx}/{t}: JSON-ის მიხედვით უკვე დასრულებულია — გამოტოვდა",
                                self.download_started_at,
                                percent=(idx / max(t, 1)) * 100,
                            )
                        ),
                    )
                    continue

                session.begin_item(
                    current_key,
                    title=video.get('title', ''),
                    url=video.get('url', ''),
                    index=i,
                )

                current_title = video.get('title', 'უცნობი სათაური')
                schedule_ui(
                    self,
                    lambda idx=i, t=total, title=current_title: self.status_var.set(
                        join_status_with_timing(
                            f"ჩამოტვირთვა {idx}/{t}: {title[:40]}...",
                            self.download_started_at,
                            percent=((idx - 1) / max(t, 1)) * 100,
                        )
                    ),
                )

                try:
                    downloaded, _used_fallback, _effective_height, used_size_limit, skip_marker = self._download_single_video(
                        video,
                        seen_video_keys,
                        i,
                        total,
                    )
                    if downloaded:
                        session.mark_completed(current_key)
                        successful += 1
                        if used_size_limit:
                            size_limit_fallbacks += 1
                    else:
                        session.mark_skipped_duplicate(
                            current_key,
                            duplicate_of=skip_marker or make_history_item_key(video.get('url'), entry=video),
                        )
                        skipped_duplicates += 1
                        skip_reason = 'existing_name' if is_existing_file_skip_marker(skip_marker) else 'duplicate_id'
                        if skip_reason == 'existing_name':
                            skipped_existing_by_name += 1
                        schedule_ui(
                            self,
                            lambda idx=i, t=total, reason=skip_reason: self.status_var.set(
                                join_status_with_timing(
                                    f"ჩამოტვირთვა {idx}/{t}: {'იგივე სახელის ფაილი უკვე არსებობს' if reason == 'existing_name' else 'დუბლიკატი აღმოჩნდა'} — გამოტოვდა",
                                    self.download_started_at,
                                    percent=(idx / max(t, 1)) * 100,
                                )
                            ),
                        )
                except Exception as exc:
                    if is_user_requested_stop_error(exc) or self.stop_download or self.cancel_requested:
                        interrupted = True
                        session.mark_interrupted(current_key, 'ჩამოტვირთვა შეწყდა დასრულებამდე.')
                        break
                    session.mark_failed(current_key, exc)
                    failed += 1
                    print(f"შეცდომა: {video.get('title', '')} - {exc}")

                schedule_ui(self, lambda p=(i / max(total, 1)) * 100: self.progress_var.set(p))

            session.finalize(interrupted=interrupted)
            schedule_ui(
                self,
                lambda: self._all_downloads_complete(
                    successful,
                    failed,
                    skipped_duplicates,
                    size_limit_fallbacks,
                    total,
                    resumed_completed=resumed_completed,
                    skipped_existing_by_name=skipped_existing_by_name,
                    output_path=session.output_dir,
                    interrupted=interrupted,
                ),
            )
        except Exception as exc:
            if session is not None and current_key and not is_user_requested_stop_error(exc):
                session.mark_failed(current_key, exc)
                session.finalize()
            schedule_ui(self, lambda err=format_ydl_error(exc): self._download_history_error(err))
        finally:
            if session is not None:
                session.close()
            self.active_history_session = None
            self.active_history_item_key = None

    def _download_single_video(self, video, seen_video_keys=None, current_index=1, total_count=1):
        output_path = self.active_output_path or get_owner_setting(self, 'download_path', self.download_path)
        os.makedirs(output_path, exist_ok=True)
        format_kind = get_owner_setting(self, 'format', self.format_var, 'video')

        info = fetch_video_metadata(video['url'], noplaylist=True)
        video_key = canonical_video_key(video['url'], entry=video, info=info)
        if seen_video_keys is not None and video_key and video_key in seen_video_keys:
            return False, False, None, False, video_key

        output_template = self._build_channel_output_template(output_path, current_index, total_count)
        effective_title = info.get('title') or video.get('title') or 'video'
        if get_owner_setting(self, 'skip_existing_name', self.skip_existing_name_var, True):
            existing_path = find_existing_media_by_output_name(
                output_path,
                output_template,
                effective_title,
                format_kind,
            )
            if existing_path:
                if seen_video_keys is not None and video_key:
                    seen_video_keys.add(video_key)
                return False, False, None, False, f"existing_file:{os.path.basename(existing_path)}"

        ydl_opts = get_max_quality_options()
        ydl_opts.update({
            'windowsfilenames': True,
            'quiet': True,
        })

        if getattr(self, 'is_search_mode', False):
            ydl_opts['download_ranges'] = get_download_range_func_safe()([], [(90.0, float('inf'))])

        used_fallback = False
        effective_height = None
        used_size_limit = False
        if format_kind == "video":
            requested_quality = get_owner_setting(self, 'quality', self.quality_var, 'best')
            effective_quality, effective_height, used_fallback, used_size_limit, _estimated_size, size_limit_from_height = resolve_video_quality_with_size_limit(
                info,
                requested_quality,
                size_limit_enabled=get_owner_setting(self, 'limit_size', self.limit_size_var, False),
            )
            ensure_video_pipeline_ready(info, effective_quality)
            video_opts, _, _ = build_video_download_options(info, effective_quality)
            ydl_opts.update(video_opts)
            self._set_item_postprocess_estimate(current_index, estimate_expected_postprocess_total_seconds(info, format_kind='video'))
            if (used_fallback and requested_quality != 'best') or used_size_limit:
                quality_text = quality_label_from_height(effective_height) if effective_height else 'ხელმისაწვდომი ხარისხი'
                note_parts = []
                if used_fallback and requested_quality != 'best':
                    note_parts.append('არჩეული ხარისხი ვერ მოიძებნა')
                if used_size_limit:
                    from_quality_text = quality_label_from_height(size_limit_from_height) if size_limit_from_height else 'უცნობი'
                    note_parts.append(f'2GB ლიმიტი: {from_quality_text} → {quality_text}')
                schedule_ui(
                    self,
                    lambda q=quality_text, title=video.get('title', ''), note=' | '.join(note_parts), idx=current_index, total=total_count: self.status_var.set(
                        join_status_with_timing(
                            f"{note} — {title[:35]}{'...' if len(title) > 35 else ''} → {q}",
                            self.download_started_at,
                            percent=((idx - 1) / max(total, 1)) * 100,
                        )
                    ),
                )
        else:
            ydl_opts.update(get_audio_download_options(get_owner_setting(self, 'audio_quality', self.audio_quality_var, '320')))
            self._set_item_postprocess_estimate(current_index, estimate_expected_postprocess_total_seconds(info, format_kind='audio'))

        item_key = self.active_history_item_key or make_history_item_key(video.get('url'), entry=video, info=info)
        ydl_opts = prepare_history_download_options(
            ydl_opts,
            self.active_history_session,
            item_key,
            output_template,
            ui_progress_hook=self._make_channel_progress_hook(current_index, total_count, video.get('title', '')),
            ui_postprocessor_hook=self._make_channel_postprocessor_hook(current_index, total_count, video.get('title', '')),
            stop_checker=lambda: bool(self.stop_download or self.cancel_requested),
        )

        ydl_download_with_cookie_fallback([video['url']], ydl_opts)

        if seen_video_keys is not None and video_key:
            seen_video_keys.add(video_key)
        return True, used_fallback, effective_height, used_size_limit, ''

    def _all_downloads_complete(self, successful, failed, skipped_duplicates, size_limit_fallbacks, total,
                                resumed_completed=0, skipped_existing_by_name=0, output_path=None, interrupted=False):
        self.is_downloading = False
        self.download_btn.configure(state="normal", text="მონიშნულის ჩამოტვირთვა (MAX)", bg=ModernStyle.ACCENT)
        self.stop_btn.configure(state="disabled")
        final_path = output_path or self.active_output_path or get_owner_setting(self, 'download_path', self.download_path)
        processed = successful + skipped_duplicates + resumed_completed
        self.progress_var.set((processed / max(total, 1)) * 100 if interrupted else (100 if total else 0))
        duplicate_by_id = max(0, int(skipped_duplicates or 0) - int(skipped_existing_by_name or 0))

        if interrupted or self.stop_download:
            self.status_var.set(join_status_with_timing(
                f"გაჩერებულია! ახალი: {successful}/{total} | JSON: {resumed_completed} | სახელით: {skipped_existing_by_name} | დუბლიკატი: {duplicate_by_id} | 2GB↓: {size_limit_fallbacks}",
                self.download_started_at,
                percent=(processed / max(total, 1)) * 100 if total else 0,
            ))
            messagebox.showinfo(
                "გაჩერებულია",
                f"ახლად ჩამოტვირთული: {successful}\nJSON-ით უკვე დასრულებული: {resumed_completed}\nიგივე სახელით გამოტოვებული: {skipped_existing_by_name}\nID დუბლიკატი: {duplicate_by_id}\n2GB ლიმიტით დაბლა ჩამოვიდა: {size_limit_fallbacks}\nშეცდომა: {failed}\n\nშემდეგ გაშვებაზე გაგრძელდება პირველი დაუსრულებელი ელემენტიდან.\n{final_path}",
            )
        else:
            self.status_var.set(join_status_with_timing(
                f"დასრულდა! ახალი: {successful}, JSON: {resumed_completed}, სახელით: {skipped_existing_by_name}, დუბლიკატი: {duplicate_by_id}, 2GB↓: {size_limit_fallbacks}, შეცდომა: {failed}",
                self.download_started_at,
                percent=100,
            ))
            messagebox.showinfo(
                "დასრულდა",
                f"ახლად ჩამოტვირთული: {successful}\nJSON-ით უკვე დასრულებული: {resumed_completed}\nიგივე სახელით გამოტოვებული: {skipped_existing_by_name}\nID დუბლიკატი: {duplicate_by_id}\n2GB ლიმიტით დაბლა ჩამოვიდა: {size_limit_fallbacks}\nშეცდომა: {failed}\n\nფაილების საქაღალდე:\n{final_path}\n\nJSON ისტორია:\n{get_download_history_root()}",
            )

    def _download_history_error(self, error):
        self.is_downloading = False
        self.download_btn.configure(state="normal", text="მონიშნულის ჩამოტვირთვა (MAX)", bg=ModernStyle.ACCENT)
        self.stop_btn.configure(state="disabled")
        self.status_var.set(join_status_with_timing("შეცდომა!", self.download_started_at))
        messagebox.showerror("შეცდომა", error)

class ShortsDownloaderWindow(ChannelDownloaderWindow):
    """არხის Shorts ვიდეოების ჩამოტვირთვის ფანჯარა — ChannelDownloader-ის ყველა ფუნქციით."""

    SHORTS_PLACEHOLDER = "მაგ: youtube.com/@username/shorts"

    def create_widgets(self):
        super().create_widgets()
        self.window.title(t('window_title_shorts'))
        if hasattr(self, 'title_label'):
            self.title_label.configure(text="Shorts Video Downloader")
        if hasattr(self, 'subtitle_label'):
            self.subtitle_label.configure(text="არხის Shorts ვიდეოების ძებნა, მონიშვნა და ჩამოტვირთვა")
        self.channel_url_var.set("")
        try:
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, self.SHORTS_PLACEHOLDER)
        except Exception:
            pass
        self.fetch_btn.configure(text="Shorts მოძებნა")
        self.download_btn.configure(text="მონიშნული Shorts-ის ჩამოტვირთვა (MAX)")
        self.channel_info_label.configure(text="Shorts: ჯერ არ არის არჩეული")
        self.status_var.set("შეიყვანე Shorts ლინკი და დააჭირე Shorts მოძებნას")
        self._update_numbered_option_button()

    def _update_numbered_option_button(self):
        self.numbered_filenames_var.set(False)
        if hasattr(self, 'numbered_names_btn'):
            self.numbered_names_btn.configure(
                text="სახელები: ნომერი+სათაური ON",
                bg=ModernStyle.SUCCESS,
                fg=ModernStyle.TEXT,
                state="disabled",
            )

    def toggle_numbered_filenames(self):
        self.numbered_filenames_var.set(False)
        self._update_numbered_option_button()
        self.status_var.set("Shorts-ში ფაილის სახელები ყოველთვის იქნება 001 - სათაური")

    def _build_channel_output_template(self, output_path, index, total_count):
        return build_numbered_output_template(output_path, index, total_count, numbered_only=False)

    def _clear_placeholder(self):
        value = self.url_entry.get().strip()
        if value.startswith("მაგ:"):
            self.url_entry.delete(0, "end")

    def _fetch_channel_thread(self, url):
        try:
            shorts_url = normalize_channel_shorts_url(url)
            print(f"[Shorts] მოძებნა დაიწყო: {shorts_url}")

            ydl_opts = get_max_quality_options()
            ydl_opts.update({
                'quiet': False,
                'no_warnings': False,
                'extract_flat': 'in_playlist',
                'ignoreerrors': True,
                'skip_download': True,
            })

            info = ydl_extract_info_with_cookie_fallback(shorts_url, ydl_opts, download=False)
            channel_name = info.get('channel') or info.get('uploader') or info.get('title', 'უცნობი არხი')
            entries = info.get('entries', []) or []

            self.channel_videos = []
            seen = set()
            for entry in entries:
                if entry is None:
                    continue

                entry_id = (entry.get('id') or '').strip()
                if not entry_id:
                    raw_url = entry.get('url') or entry.get('webpage_url') or ''
                    match = re.search(r'(?:shorts/|watch\?v=)([A-Za-z0-9_-]{6,})', str(raw_url))
                    entry_id = match.group(1) if match else ''
                if not entry_id or entry_id in seen:
                    continue
                seen.add(entry_id)

                title = entry.get('title') or f"Shorts {len(self.channel_videos) + 1}"
                video_data = {
                    'id': entry_id,
                    'title': title,
                    'url': f"https://www.youtube.com/shorts/{entry_id}",
                    'upload_date': entry.get('upload_date', ''),
                    'kind': 'Shorts',
                }
                self.channel_videos.append(video_data)

            self.channel_name = channel_name
            print(f"[Shorts] ნაპოვნია {len(self.channel_videos)} Shorts ვიდეო | არხი: {channel_name}")
            schedule_ui(self, lambda: self._update_shorts_info(channel_name))
        except Exception as e:
            err = format_ydl_error(e)
            print(f"[Shorts] მოძებნის შეცდომა: {err}")
            schedule_ui(self, lambda err=err: self._show_fetch_error(err))

    def _update_shorts_info(self, channel_name):
        self.fetch_btn.configure(state="normal", text="Shorts მოძებნა")
        self.channel_info_label.configure(text=f"Shorts არხი: {channel_name}")
        self.apply_video_filters()

    def _show_fetch_error(self, error):
        self.fetch_btn.configure(state="normal", text="Shorts მოძებნა")
        self.status_var.set("შეცდომა")
        messagebox.showerror("შეცდომა", f"Shorts ვიდეოები ვერ მოიძებნა:\n{error}")

    def start_download(self):
        self.numbered_filenames_var.set(False)
        super().start_download()

    def _download_single_video(self, video, seen_video_keys=None, current_index=1, total_count=1):
        title = video.get('title', 'უცნობი სათაური')
        print(f"[Shorts] ჩამოწერა დაიწყო {current_index}/{total_count}: {title}")
        result = super()._download_single_video(video, seen_video_keys, current_index, total_count)
        if result[0]:
            print(f"[Shorts] ჩამოწერა დასრულდა {current_index}/{total_count}: {title}")
        else:
            print(f"[Shorts] დუბლიკატი გამოტოვდა {current_index}/{total_count}: {title}")
        return result

    def _all_downloads_complete(self, successful, failed, skipped_duplicates, size_limit_fallbacks, total,
                                resumed_completed=0, skipped_existing_by_name=0, output_path=None, interrupted=False):
        super()._all_downloads_complete(
            successful,
            failed,
            skipped_duplicates,
            size_limit_fallbacks,
            total,
            resumed_completed=resumed_completed,
            skipped_existing_by_name=skipped_existing_by_name,
            output_path=output_path,
            interrupted=interrupted,
        )
        self.download_btn.configure(state="normal", text="მონიშნული Shorts-ის ჩამოტვირთვა (MAX)", bg=ModernStyle.ACCENT)

class ChannelAndShortsDownloaderWindow(ChannelDownloaderWindow):
    """ერთ ფანჯარაში არხის/YouTube Search-ის ჩვეულებრივი ვიდეოებისა და Shorts-ის მოძებნა/ჩამოტვირთვა."""

    COMBINED_PLACEHOLDER = "მაგ: https://www.youtube.com/@ChannelName ან https://www.youtube.com/results?search_query=video"

    def create_widgets(self):
        super().create_widgets()
        self.window.title(t('window_title_channel_shorts'))
        if hasattr(self, 'title_label'):
            self.title_label.configure(text="Channel/Search + Shorts Downloader")
        if hasattr(self, 'subtitle_label'):
            self.subtitle_label.configure(text="არხიდან ან YouTube search results ლინკიდან მოიძებნება Videos და Shorts")

        self.channel_url_var.set("")
        try:
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, self.COMBINED_PLACEHOLDER)
        except Exception:
            pass

        self.fetch_btn.configure(text="ვიდეო + Shorts ძებნა")
        self.download_btn.configure(text="მონიშნულის ჩამოტვირთვა: ვიდეო + Shorts (MAX)")
        self.channel_info_label.configure(text="წყარო: ჯერ არ არის არჩეული")
        self.status_var.set("შეიყვანე არხის ლინკი ან YouTube search results ლინკი — მოიძებნება Videos და Shorts ერთად")
        self._add_kind_selection_buttons()

    def _add_kind_selection_buttons(self):
        """დამატებითი სწრაფი მონიშვნები: ყველა / მხოლოდ ვიდეო / მხოლოდ Shorts."""
        frame = getattr(self, 'select_btns_frame', None)
        if frame is None:
            return

        ttk.Label(frame, text="ტიპი:", style="Info.TLabel").pack(side="left", padx=(8, 6))

        select_all_types_btn = tk.Button(
            frame,
            text="ყველა ტიპი",
            font=("Segoe UI", 9),
            bg=ModernStyle.SUCCESS,
            fg=ModernStyle.TEXT,
            relief="flat",
            cursor="hand2",
            command=self.select_all_videos,
        )
        select_all_types_btn.pack(side="left", ipadx=8, ipady=3, padx=(0, 8))

        select_videos_btn = tk.Button(
            frame,
            text="მხოლოდ ვიდეოები",
            font=("Segoe UI", 9),
            bg=ModernStyle.CYAN,
            fg=ModernStyle.BG_MAIN,
            relief="flat",
            cursor="hand2",
            command=lambda: self.select_only_kind('ვიდეო'),
        )
        select_videos_btn.pack(side="left", ipadx=8, ipady=3, padx=(0, 8))

        select_shorts_btn = tk.Button(
            frame,
            text="მხოლოდ Shorts",
            font=("Segoe UI", 9),
            bg=ModernStyle.WARNING,
            fg=ModernStyle.BG_MAIN,
            relief="flat",
            cursor="hand2",
            command=lambda: self.select_only_kind('Shorts'),
        )
        select_shorts_btn.pack(side="left", ipadx=8, ipady=3, padx=(0, 8))

    def select_only_kind(self, kind):
        """მონიშნავს მხოლოდ მითითებული ტიპის ელემენტებს და დანარჩენს მოხსნის."""
        target_kind = str(kind or '').strip().lower()
        selected = 0
        for video in self.channel_videos:
            current_kind = str(video.get('kind') or '').strip().lower()
            should_select = current_kind == target_kind
            self._get_video_var(video).set(should_select)
            if should_select:
                selected += 1
        self._render_videos()
        label = 'Shorts' if target_kind == 'shorts' else 'ვიდეო'
        self.status_var.set(f"მოინიშნა მხოლოდ {label}: {selected}")

    def _clear_placeholder(self):
        value = self.url_entry.get().strip()
        if value.startswith("მაგ:"):
            self.url_entry.delete(0, "end")

    def _extract_entry_id(self, entry):
        if not isinstance(entry, dict):
            return ''

        entry_id = (entry.get('id') or '').strip()
        if entry_id:
            return entry_id

        for key in ('url', 'webpage_url', 'original_url'):
            raw_url = str(entry.get(key) or '').strip()
            if not raw_url:
                continue
            match = re.search(r'(?:shorts/|watch\?v=|embed/|live/|v=)([A-Za-z0-9_-]{6,})', raw_url)
            if match:
                return match.group(1)
            if re.fullmatch(r'[A-Za-z0-9_-]{6,}', raw_url):
                return raw_url

        return ''

    def _entry_raw_url_text(self, entry):
        values = []
        if isinstance(entry, dict):
            for key in ('url', 'webpage_url', 'original_url', 'extractor_key', 'ie_key'):
                value = entry.get(key)
                if value:
                    values.append(str(value))
        return ' '.join(values)

    def _build_entry_download_url(self, entry, entry_id, kind):
        """entry-დან რეალური ჩამოსაწერი URL-ის შედგენა."""
        raw_url = ''
        if isinstance(entry, dict):
            raw_url = str(entry.get('webpage_url') or entry.get('original_url') or entry.get('url') or '').strip()

        if raw_url.startswith('http://') or raw_url.startswith('https://'):
            return raw_url

        cleaned = raw_url.lstrip('/')
        if cleaned.startswith('shorts/'):
            return 'https://www.youtube.com/' + cleaned
        if cleaned.startswith('watch?'):
            return 'https://www.youtube.com/' + cleaned

        if kind == 'Shorts' and '/shorts/' in raw_url:
            return f"https://www.youtube.com/shorts/{entry_id}"
        if kind == 'Shorts' and str(raw_url).startswith('shorts/'):
            return f"https://www.youtube.com/shorts/{entry_id}"

        return f"https://www.youtube.com/watch?v={entry_id}"

    def _is_probably_shorts_entry(self, entry, source_hint=''):
        raw_text = self._entry_raw_url_text(entry).lower()
        if '/shorts/' in raw_text or raw_text.startswith('shorts/') or 'youtube.com/shorts/' in raw_text:
            return True

        source_hint = str(source_hint or '').lower()
        if 'shorts' in source_hint:
            try:
                duration = entry.get('duration') if isinstance(entry, dict) else None
                if duration is not None and float(duration) <= 90:
                    return True
            except Exception:
                pass

            # flat-search შედეგებში duration ზოგჯერ საერთოდ არ მოდის; Shorts საძიებო ცდაში ნაპოვნი
            # ელემენტები ცალკე მოსანიშნად Shorts ტიპად დავტოვოთ, რომ მომხმარებელმა არჩევა შეძლოს.
            if isinstance(entry, dict) and entry.get('duration') in (None, ''):
                return True

        return False

    def _entry_to_video_item(self, entry, default_kind='ვიდეო', default_title_prefix='ვიდეო', source_hint=''):
        entry_id = self._extract_entry_id(entry)
        if not entry_id:
            return None

        kind = 'Shorts' if self._is_probably_shorts_entry(entry, source_hint=source_hint) else default_kind
        title = entry.get('title') if isinstance(entry, dict) else ''
        if not title:
            title = f"{default_title_prefix} {entry_id}"

        return {
            'id': entry_id,
            'title': title,
            'url': self._build_entry_download_url(entry, entry_id, kind),
            'upload_date': entry.get('upload_date', '') if isinstance(entry, dict) else '',
            'kind': kind,
        }

    def _append_unique_item(self, combined, seen_keys, item):
        if not item:
            return False
        key = canonical_video_key(item.get('url'), entry=item) or item.get('id') or item.get('url')
        if key in seen_keys:
            existing = seen_keys[key]
            if existing.get('kind') != 'Shorts' and item.get('kind') == 'Shorts':
                existing['kind'] = 'Shorts'
                existing['url'] = item.get('url') or existing.get('url')
            return False

        combined.append(item)
        seen_keys[key] = item
        return True

    def _extract_entries_from_tab(self, info, kind, url_builder, default_title_prefix):
        entries = info.get('entries', []) or []
        videos = []
        seen = {}

        for entry in entries:
            if entry is None:
                continue

            entry_id = self._extract_entry_id(entry)
            if not entry_id:
                continue

            title = entry.get('title') or f"{default_title_prefix} {len(videos) + 1}"
            item = {
                'id': entry_id,
                'title': title,
                'url': url_builder(entry_id),
                'upload_date': entry.get('upload_date', ''),
                'kind': kind,
            }
            self._append_unique_item(videos, seen, item)

        return videos

    def _extract_entries_from_search_info(self, info, default_kind='ვიდეო', source_hint=''):
        entries = info.get('entries', []) if isinstance(info, dict) else []
        results = []
        seen = {}
        for entry in entries or []:
            if entry is None:
                continue
            item = self._entry_to_video_item(
                entry,
                default_kind=default_kind,
                default_title_prefix='Shorts' if default_kind == 'Shorts' else 'ვიდეო',
                source_hint=source_hint,
            )
            self._append_unique_item(results, seen, item)
        return results

    def _fetch_tab_info(self, tab_url, ydl_opts, label, errors):
        try:
            schedule_ui(self, lambda label=label: self.status_var.set(f"იძებნება: {label}..."))
            return ydl_extract_info_with_cookie_fallback(tab_url, ydl_opts, download=False)
        except Exception as e:
            errors.append(f"{label}: {format_ydl_error(e)}")
            return None

    def _make_flat_search_opts(self):
        opts = get_max_quality_options()
        opts.update({
            'quiet': True,
            'no_warnings': True,
            'extract_flat': 'in_playlist',
            'ignoreerrors': True,
            'skip_download': True,
        })
        return opts

    def _fetch_channel_thread(self, url):
        search_query = extract_youtube_search_query_from_url(url)
        if search_query:
            self.is_search_mode = True
            self.search_query_text = search_query
            self._fetch_search_results_thread(search_query)
            return

        errors = []
        try:
            self.is_search_mode = False
            self.search_query_text = ""
            videos_url = normalize_channel_videos_url(url)
            shorts_url = normalize_channel_shorts_url(url)
            print(f"[Channel+Shorts] Videos მოძებნა: {videos_url}")
            print(f"[Channel+Shorts] Shorts მოძებნა: {shorts_url}")

            ydl_opts = self._make_flat_search_opts()

            videos_info = self._fetch_tab_info(videos_url, ydl_opts, 'Videos', errors)
            shorts_info = self._fetch_tab_info(shorts_url, ydl_opts, 'Shorts', errors)

            channel_name = 'უცნობი არხი'
            for info in (videos_info, shorts_info):
                if isinstance(info, dict):
                    channel_name = info.get('channel') or info.get('uploader') or info.get('title') or channel_name
                    if channel_name != 'უცნობი არხი':
                        break

            normal_videos = self._extract_entries_from_tab(
                videos_info or {},
                'ვიდეო',
                lambda entry_id: f"https://www.youtube.com/watch?v={entry_id}",
                'ვიდეო',
            )
            shorts_videos = self._extract_entries_from_tab(
                shorts_info or {},
                'Shorts',
                lambda entry_id: f"https://www.youtube.com/shorts/{entry_id}",
                'Shorts',
            )

            combined = []
            seen_keys = {}
            for item in normal_videos + shorts_videos:
                self._append_unique_item(combined, seen_keys, item)

            self.channel_videos = combined
            self.channel_name = channel_name
            print(
                f"[Channel+Shorts] ნაპოვნია ვიდეო: {len(normal_videos)} | "
                f"Shorts: {len(shorts_videos)} | სულ: {len(combined)} | არხი: {channel_name}"
            )

            if not combined and errors:
                raise Exception("\n\n".join(errors))

            schedule_ui(self, lambda: self._update_combined_info(
                channel_name,
                len([item for item in combined if item.get('kind') == 'ვიდეო']),
                len([item for item in combined if item.get('kind') == 'Shorts']),
                len(combined),
                errors,
                source_label='არხი',
            ))
        except Exception as e:
            err = format_ydl_error(e)
            print(f"[Channel+Shorts] მოძებნის შეცდომა: {err}")
            schedule_ui(self, lambda err=err: self._show_fetch_error(err))

    def _fetch_search_results_thread(self, search_query):
        errors = []
        try:
            limit = get_youtube_search_results_limit()
            print(f"[Search+Shorts] ძებნა: {search_query} | limit={limit}")

            ydl_opts = self._make_flat_search_opts()
            search_specs = [
                (f"ytsearch{limit}:{search_query}", 'Search Videos', 'ვიდეო'),
                (f"ytsearch{limit}:{search_query} shorts", 'Search Shorts', 'Shorts'),
            ]

            combined = []
            seen_keys = {}
            for search_spec, label, default_kind in search_specs:
                info = self._fetch_tab_info(search_spec, ydl_opts, label, errors)
                items = self._extract_entries_from_search_info(
                    info or {},
                    default_kind=default_kind,
                    source_hint=label,
                )
                for item in items:
                    self._append_unique_item(combined, seen_keys, item)

            # ფილტრი: მხოლოდ search_query-ის სიტყვებთან დაკავშირებული ვიდეოები
            query_terms = parse_multi_search_terms(search_query)
            if query_terms:
                before_count = len(combined)
                combined = [
                    item for item in combined
                    if any(term in normalize_search_text(item.get('title') or '') for term in query_terms)
                ]
                filtered_out = before_count - len(combined)
                if filtered_out:
                    print(f"[Search+Shorts] სათაური-ფილტრი: {filtered_out} შეუსაბამო ვიდეო გამოიკვეთა | დარჩა: {len(combined)}")

            videos_count = len([item for item in combined if item.get('kind') == 'ვიდეო'])
            shorts_count = len([item for item in combined if item.get('kind') == 'Shorts'])

            self.channel_videos = combined
            self.channel_name = f"YouTube Search: {search_query}"

            print(
                f"[Search+Shorts] ნაპოვნია ვიდეო: {videos_count} | "
                f"Shorts: {shorts_count} | სულ: {len(combined)} | query: {search_query}"
            )

            if not combined and errors:
                raise Exception("\n\n".join(errors))

            schedule_ui(self, lambda: self._update_combined_info(
                search_query,
                videos_count,
                shorts_count,
                len(combined),
                errors,
                source_label='YouTube Search',
            ))
        except Exception as e:
            err = format_ydl_error(e)
            print(f"[Search+Shorts] მოძებნის შეცდომა: {err}")
            schedule_ui(self, lambda err=err: self._show_fetch_error(err))

    def _update_combined_info(self, source_name, videos_count, shorts_count, total_count, errors=None, source_label='წყარო'):
        self.fetch_btn.configure(state="normal", text="ვიდეო + Shorts ძებნა")
        warning_text = ""
        if errors:
            warning_text = f" | გაფრთხილება: {len(errors)} ნაწილი სრულად ვერ წაიკითხა"
        self.channel_info_label.configure(
            text=f"{source_label}: {source_name} | ვიდეო: {videos_count} | Shorts: {shorts_count} | სულ: {total_count}{warning_text}"
        )
        self.apply_video_filters()
        if errors and total_count:
            self.status_var.set(
                f"ნაპოვნია {total_count} ელემენტი, მაგრამ ერთი ნაწილი შეიძლება არასრულად იყოს წაკითხული. დეტალი ჩანს terminal-ში."
            )
            for error in errors:
                print(f"[Channel/Search+Shorts warning] {error}")

    def _show_fetch_error(self, error):
        self.fetch_btn.configure(state="normal", text="ვიდეო + Shorts ძებნა")
        self.status_var.set("შეცდომა")
        messagebox.showerror("შეცდომა", f"ვიდეოები + Shorts ვერ მოიძებნა:\n{error}")

    def start_download(self):
        selected_videos = [video for video in self.channel_videos if self._get_video_var(video).get()]
        if not selected_videos:
            messagebox.showwarning("გაფრთხილება", "მონიშნეთ ჩამოსატვირთი ვიდეოები ან Shorts!")
            return
        super().start_download()

    def _download_selected_thread(self, videos):
        print(f"[Channel/Search+Shorts] ჩამოტვირთვა დაიწყო | მონიშნულია: {len(videos)}")
        super()._download_selected_thread(videos)

    def _all_downloads_complete(self, successful, failed, skipped_duplicates, size_limit_fallbacks, total,
                                resumed_completed=0, skipped_existing_by_name=0, output_path=None, interrupted=False):
        super()._all_downloads_complete(
            successful,
            failed,
            skipped_duplicates,
            size_limit_fallbacks,
            total,
            resumed_completed=resumed_completed,
            skipped_existing_by_name=skipped_existing_by_name,
            output_path=output_path,
            interrupted=interrupted,
        )
        self.download_btn.configure(state="normal", text="მონიშნულის ჩამოტვირთვა: ვიდეო + Shorts (MAX)", bg=ModernStyle.ACCENT)

class PlaylistDownloaderWindow:
    """პლეილისტების ჩამოტვირთვის ფანჯარა"""

    def __init__(self, parent):
        self.window = tk.Toplevel(parent)
        self.window.title(t('window_title_playlists'))
        self.window.geometry("800x700")
        self.window.minsize(700, 600)
        self.window.configure(bg=ModernStyle.BG_MAIN)

        # ცვლადები
        self.url_var = tk.StringVar()
        self.download_path = tk.StringVar(value=os.path.expanduser("~/Downloads"))
        self.history_name_var = tk.StringVar(value="")
        self.format_var = tk.StringVar(value="video")
        self.quality_var = tk.StringVar(value="best")
        self.audio_quality_var = tk.StringVar(value="320")
        self.limit_size_var = tk.BooleanVar(value=False)
        self.skip_existing_name_var = tk.BooleanVar(value=True)
        self.numbered_filenames_var = tk.BooleanVar(value=False)
        self.reverse_playlist_order_var = tk.BooleanVar(value=False)
        self.progress_var = tk.DoubleVar(value=0)
        self.status_var = tk.StringVar(value="შეიყვანეთ არხის პლეილისტების ლინკი")

        self.is_downloading = False
        self.cancel_requested = False
        self.active_history_session = None
        self.active_history_item_key = None
        self.active_output_path = None
        self.download_started_at = None
        self.item_timing_states = {}
        self.playlists_data = []
        self.filtered_playlists_data = []
        self.playlist_checkboxes = []
        self.playlist_vars = {}
        self.playlist_search_var = tk.StringVar()

        self.create_widgets()
        self.window.protocol("WM_DELETE_WINDOW", lambda: close_downloader_window(self))

    def create_widgets(self):
        scroll_frame = ScrollableFrame(self.window, style="Main.TFrame")
        scroll_frame.pack(fill="both", expand=True)

        main_frame = scroll_frame.scrollable_frame
        content = ttk.Frame(main_frame, style="Main.TFrame")
        content.pack(fill="both", expand=True, padx=25, pady=20)

        # სათაური
        title_frame = ttk.Frame(content, style="Main.TFrame")
        title_frame.pack(fill="x", pady=(0, 15))

        ttk.Label(title_frame, text="Playlist Downloader", style="Title.TLabel").pack()
        ttk.Label(title_frame, text="პლეილისტების ჩამოტვირთვა მაქსიმალური ხარისხით", style="Subtitle.TLabel").pack(pady=(3, 0))

        back_btn = tk.Button(title_frame, text="< უკან", font=("Segoe UI", 10),
                             bg=ModernStyle.BG_INPUT, fg=ModernStyle.TEXT_GRAY,
                             relief="flat", cursor="hand2", command=lambda: close_downloader_window(self))
        back_btn.pack(anchor="w", pady=(10, 0))

        # URL სექცია
        url_card = ttk.Frame(content, style="Card.TFrame")
        url_card.pack(fill="x", pady=8, ipady=12, ipadx=12)

        ttk.Label(url_card, text="არხის პლეილისტების ლინკი", style="CardBold.TLabel").pack(anchor="w", padx=12, pady=(12, 6))

        url_frame = ttk.Frame(url_card, style="Card.TFrame")
        url_frame.pack(fill="x", padx=12, pady=(0, 12))

        self.url_entry = tk.Entry(url_frame, textvariable=self.url_var,
                                  font=("Segoe UI", 11), bg=ModernStyle.BG_INPUT,
                                  fg=ModernStyle.TEXT, insertbackground=ModernStyle.ACCENT_GLOW,
                                  relief="flat", bd=0)
        self.url_entry.pack(side="left", fill="x", expand=True, ipady=10, padx=(0, 8))
        self.url_entry.insert(0, "მაგ: youtube.com/@username/playlists")
        self.url_entry.bind("<FocusIn>", lambda e: self._clear_placeholder())
        self._setup_entry_bindings(self.url_entry)

        self.fetch_btn = tk.Button(url_frame, text="პლეილისტების ძებნა",
                                   font=("Segoe UI", 10, "bold"),
                                   bg=ModernStyle.CYAN, fg=ModernStyle.BG_MAIN,
                                   relief="flat", cursor="hand2",
                                   command=self.fetch_playlists)
        self.fetch_btn.pack(side="right", ipadx=12, ipady=6)

        # პლეილისტების სექცია
        playlists_card = ttk.Frame(content, style="Card.TFrame")
        playlists_card.pack(fill="x", pady=8, ipady=12, ipadx=12)

        playlists_header = ttk.Frame(playlists_card, style="Card.TFrame")
        playlists_header.pack(fill="x", padx=12, pady=(12, 6))

        ttk.Label(playlists_header, text="ნაპოვნი პლეილისტები", style="CardBold.TLabel").pack(side="left")
        self.playlists_count_label = ttk.Label(playlists_header, text="", style="Info.TLabel")
        self.playlists_count_label.pack(side="left", padx=(10, 0))

        search_frame = ttk.Frame(playlists_card, style="Card.TFrame")
        search_frame.pack(fill="x", padx=12, pady=(0, 8))

        ttk.Label(search_frame, text="სერჩი:", style="Info.TLabel").pack(side="left", padx=(0, 8))

        self.playlist_search_entry = tk.Entry(search_frame, textvariable=self.playlist_search_var,
                                              font=("Segoe UI", 10), bg=ModernStyle.BG_INPUT,
                                              fg=ModernStyle.TEXT, insertbackground=ModernStyle.ACCENT_GLOW,
                                              relief="flat")
        self.playlist_search_entry.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 8))
        self._setup_entry_bindings(self.playlist_search_entry)
        self.playlist_search_entry.bind("<KeyRelease>", self.on_playlist_search_change)

        clear_search_btn = tk.Button(search_frame, text="გასუფთავება",
                                     font=("Segoe UI", 9), bg=ModernStyle.BG_INPUT,
                                     fg=ModernStyle.TEXT_GRAY, relief="flat", cursor="hand2",
                                     command=self.clear_playlist_search)
        clear_search_btn.pack(side="right", ipadx=8, ipady=4)

        select_btns_frame = ttk.Frame(playlists_card, style="Card.TFrame")
        select_btns_frame.pack(fill="x", padx=12, pady=(0, 6))

        select_all_btn = tk.Button(select_btns_frame, text="ყველას მონიშვნა",
                                   font=("Segoe UI", 9), bg=ModernStyle.SUCCESS,
                                   fg=ModernStyle.TEXT, relief="flat", cursor="hand2",
                                   command=self.select_all_playlists)
        select_all_btn.pack(side="left", ipadx=8, ipady=3, padx=(0, 8))

        deselect_all_btn = tk.Button(select_btns_frame, text="ყველას მოხსნა",
                                     font=("Segoe UI", 9), bg=ModernStyle.BG_INPUT,
                                     fg=ModernStyle.TEXT_GRAY, relief="flat", cursor="hand2",
                                     command=self.deselect_all_playlists)
        deselect_all_btn.pack(side="left", ipadx=8, ipady=3, padx=(0, 8))

        self.numbered_names_btn = tk.Button(select_btns_frame, text="სახელები: № + სათაური",
                                            font=("Segoe UI", 8, "bold"), bg=ModernStyle.BG_INPUT,
                                            fg=ModernStyle.TEXT_GRAY, relief="flat", cursor="hand2",
                                            command=self.toggle_numbered_filenames)
        self.numbered_names_btn.pack(side="left", ipadx=6, ipady=3, padx=(0, 8))

        self.reverse_order_btn = tk.Button(select_btns_frame, text="ჩამოწერა: 1→ბოლო",
                                           font=("Segoe UI", 8, "bold"), bg=ModernStyle.BG_INPUT,
                                           fg=ModernStyle.TEXT_GRAY, relief="flat", cursor="hand2",
                                           command=self.toggle_reverse_playlist_order)
        self.reverse_order_btn.pack(side="left", ipadx=6, ipady=3)
        self._update_playlist_option_buttons()

        # პლეილისტების ჩამონათვალი
        playlists_list_frame = ttk.Frame(playlists_card, style="Card.TFrame")
        playlists_list_frame.pack(fill="x", padx=12, pady=(0, 12))

        self.playlists_canvas = tk.Canvas(playlists_list_frame, bg=ModernStyle.BG_CARD,
                                          highlightthickness=0, height=180)
        self.playlists_scrollbar = ttk.Scrollbar(playlists_list_frame, orient="vertical",
                                                  command=self.playlists_canvas.yview)
        self.playlists_inner_frame = ttk.Frame(self.playlists_canvas, style="Card.TFrame")

        self.playlists_inner_frame.bind("<Configure>",
            lambda e: self.playlists_canvas.configure(scrollregion=self.playlists_canvas.bbox("all")))

        self.playlists_canvas.create_window((0, 0), window=self.playlists_inner_frame, anchor="nw")
        self.playlists_canvas.configure(yscrollcommand=self.playlists_scrollbar.set)

        self.playlists_canvas.pack(side="left", fill="both", expand=True)
        self.playlists_scrollbar.pack(side="right", fill="y")

        # ფორმატი და ხარისხი
        options_card = ttk.Frame(content, style="Card.TFrame")
        options_card.pack(fill="x", pady=8, ipady=12, ipadx=12)

        options_row = ttk.Frame(options_card, style="Card.TFrame")
        options_row.pack(fill="x", padx=12, pady=12)

        format_frame = ttk.Frame(options_row, style="Card.TFrame")
        format_frame.pack(side="left", fill="x", expand=True)

        ttk.Label(format_frame, text="ფორმატი", style="CardBold.TLabel").pack(anchor="w", pady=(0, 8))

        radio_frame = ttk.Frame(format_frame, style="Card.TFrame")
        radio_frame.pack(anchor="w")

        ttk.Radiobutton(radio_frame, text="ვიდეო (1080p↓ MP4 | 1080p↑ AUTO: MKV/WEBM)", variable=self.format_var,
                        value="video", style="Card.TRadiobutton",
                        command=self.toggle_quality).pack(side="left", padx=(0, 15))
        ttk.Radiobutton(radio_frame, text="აუდიო (MP3)", variable=self.format_var,
                        value="audio", style="Card.TRadiobutton",
                        command=self.toggle_quality).pack(side="left")

        quality_frame = ttk.Frame(options_row, style="Card.TFrame")
        quality_frame.pack(side="right")

        ttk.Label(quality_frame, text="ხარისხი", style="CardBold.TLabel").pack(anchor="w", pady=(0, 8))

        self.video_quality_frame = ttk.Frame(quality_frame, style="Card.TFrame")
        self.video_quality_frame.pack(anchor="w")

        quality_values = ["ავტომატური მაქსიმუმი თითო ვიდეოზე", "2160p (4K)", "1440p (2K)", "1080p (Full HD)", "720p (HD)", "480p", "360p"]
        self.quality_combo = ttk.Combobox(self.video_quality_frame, values=quality_values,
                                          state="readonly", font=("Segoe UI", 10), width=26)
        self.quality_combo.set("ავტომატური მაქსიმუმი თითო ვიდეოზე")
        self.quality_combo.pack()
        self.quality_combo.bind("<<ComboboxSelected>>", self.on_quality_change)

        self.limit_size_check = ttk.Checkbutton(
            self.video_quality_frame,
            text="თუ 2GB ან მეტია, დაბალ ხარისხზე ჩამოვიდეს",
            variable=self.limit_size_var,
            style="Card.TCheckbutton"
        )
        self.limit_size_check.pack(anchor="w", pady=(8, 0))

        self.audio_quality_frame = ttk.Frame(quality_frame, style="Card.TFrame")

        audio_values = ["320 kbps (საუკეთესო)", "256 kbps", "192 kbps", "128 kbps"]
        self.audio_combo = ttk.Combobox(self.audio_quality_frame, values=audio_values,
                                        state="readonly", font=("Segoe UI", 10), width=18)
        self.audio_combo.set("320 kbps (საუკეთესო)")
        self.audio_combo.pack()
        self.audio_combo.bind("<<ComboboxSelected>>", self.on_audio_quality_change)

        # შენახვის ადგილი
        path_card = ttk.Frame(content, style="Card.TFrame")
        path_card.pack(fill="x", pady=8, ipady=12, ipadx=12)

        ttk.Label(path_card, text="შენახვის ადგილი", style="CardBold.TLabel").pack(anchor="w", padx=12, pady=(12, 6))

        path_frame = ttk.Frame(path_card, style="Card.TFrame")
        path_frame.pack(fill="x", padx=12, pady=(0, 12))

        self.path_entry = tk.Entry(path_frame, textvariable=self.download_path,
                                   font=("Segoe UI", 10), bg=ModernStyle.BG_INPUT,
                                   fg=ModernStyle.TEXT, relief="flat")
        self.path_entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 8))

        browse_btn = tk.Button(path_frame, text="არჩევა", font=("Segoe UI", 9),
                               bg=ModernStyle.BG_INPUT, fg=ModernStyle.TEXT_GRAY,
                               relief="flat", cursor="hand2", command=self.browse_folder)
        browse_btn.pack(side="right", ipadx=10, ipady=5)

        add_skip_existing_name_control(path_card, self.skip_existing_name_var)

        self.history_name_entry = add_history_name_controls(
            path_card,
            self.history_name_var,
            self._setup_entry_bindings,
        )

        # პროგრესი
        progress_frame = ttk.Frame(content, style="Main.TFrame")
        progress_frame.pack(fill="x", pady=12)

        self.status_label = ttk.Label(progress_frame, textvariable=self.status_var, style="Subtitle.TLabel")
        self.status_label.pack(anchor="w", pady=(0, 6))

        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var,
                                            maximum=100, style="Accent.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x")
        install_visible_progress(self, progress_frame)

        # ჩამოტვირთვის ღილაკი
        self.download_btn = tk.Button(content, text="ჩამოტვირთვა (MAX Quality)",
                                      font=("Segoe UI", 14, "bold"),
                                      bg=ModernStyle.ACCENT, fg=ModernStyle.TEXT,
                                      relief="flat", cursor="hand2",
                                      command=self.start_download)
        self.download_btn.pack(fill="x", pady=15, ipady=12)

    def _setup_entry_bindings(self, entry):
        for sequence in ("<Control-v>", "<Control-V>", "<Control-Insert>", "<Shift-Insert>", "<<Paste>>"):
            entry.bind(sequence, lambda e, w=entry: self._paste_text(w))
        entry.bind("<Control-KeyPress>", lambda e, w=entry: handle_keyboard_shortcut(e, self.window, w), add="+")
        entry.bind("<Shift-KeyPress-Insert>", lambda e, w=entry: handle_keyboard_shortcut(e, self.window, w), add="+")
        entry.bind("<Control-a>", lambda e: self._select_all(entry))
        entry.bind("<Control-A>", lambda e: self._select_all(entry))

    def _paste_text(self, entry):
        return paste_into_entry(self.window, entry)

    def _select_all(self, entry):
        entry.select_range(0, "end")
        entry.icursor("end")
        return "break"

    def _clear_placeholder(self):
        if self.url_entry.get().startswith("მაგ:"):
            self.url_entry.delete(0, "end")

    def toggle_quality(self):
        if get_owner_setting(self, 'format', self.format_var, 'video') == "video":
            self.audio_quality_frame.pack_forget()
            self.video_quality_frame.pack(anchor="w")
        else:
            self.video_quality_frame.pack_forget()
            self.audio_quality_frame.pack(anchor="w")

    def on_quality_change(self, event=None):
        self.quality_var.set(extract_quality_code_from_label(self.quality_combo.get()))

    def on_audio_quality_change(self, event=None):
        mapping = {"320 kbps (საუკეთესო)": "320", "256 kbps": "256", "192 kbps": "192", "128 kbps": "128"}
        self.audio_quality_var.set(mapping.get(self.audio_combo.get(), "320"))

    def browse_folder(self):
        folder = filedialog.askdirectory(initialdir=get_owner_setting(self, 'download_path', self.download_path))
        if folder:
            self.download_path.set(folder)

    def select_all_playlists(self):
        for playlist in self.playlists_data:
            self._get_playlist_var(playlist).set(True)
        self._render_playlists()

    def deselect_all_playlists(self):
        for playlist in self.playlists_data:
            self._get_playlist_var(playlist).set(False)
        self._render_playlists()

    def _update_playlist_option_buttons(self):
        if not hasattr(self, 'numbered_names_btn') or not hasattr(self, 'reverse_order_btn'):
            return

        if get_owner_setting(self, 'numbered_filenames', self.numbered_filenames_var, False):
            self.numbered_names_btn.configure(
                text="სახელები: 1,2,3 ON",
                bg=ModernStyle.WARNING,
                fg=ModernStyle.BG_MAIN,
            )
        else:
            self.numbered_names_btn.configure(
                text="სახელები: № + სათაური",
                bg=ModernStyle.BG_INPUT,
                fg=ModernStyle.TEXT_GRAY,
            )

        if get_owner_setting(self, 'reverse_playlist_order', self.reverse_playlist_order_var, False):
            self.reverse_order_btn.configure(
                text="ჩამოწერა: ბოლო→1 ON",
                bg=ModernStyle.PINK,
                fg=ModernStyle.TEXT,
            )
        else:
            self.reverse_order_btn.configure(
                text="ჩამოწერა: 1→ბოლო",
                bg=ModernStyle.BG_INPUT,
                fg=ModernStyle.TEXT_GRAY,
            )

    def toggle_numbered_filenames(self):
        self.numbered_filenames_var.set(not get_owner_setting(self, 'numbered_filenames', self.numbered_filenames_var, False))
        self._update_playlist_option_buttons()
        if get_owner_setting(self, 'numbered_filenames', self.numbered_filenames_var, False):
            self.status_var.set("ჩართულია: ფაილების სახელები იქნება 1, 2, 3... ფორმატით")
        else:
            self.status_var.set("ჩართულია: ფაილები შეინახება ნომრით და ვიდეოს სათაურით")

    def toggle_reverse_playlist_order(self):
        self.reverse_playlist_order_var.set(not get_owner_setting(self, 'reverse_playlist_order', self.reverse_playlist_order_var, False))
        self._update_playlist_option_buttons()
        if get_owner_setting(self, 'reverse_playlist_order', self.reverse_playlist_order_var, False):
            self.status_var.set("ჩართულია: პლეილისტის ვიდეოები ჩამოიწერება ბოლოდან პირველამდე")
        else:
            self.status_var.set("ჩართულია: პლეილისტის ვიდეოები ჩამოიწერება პირველიდან ბოლომდე")

    def _build_playlist_output_template(self, playlist_folder, save_number, pad_width):
        return os.path.join(playlist_folder, "%(title)s.%(ext)s")

    def clear_playlist_search(self):
        self.playlist_search_var.set("")
        self.apply_playlist_filter()

    def on_playlist_search_change(self, event=None):
        self.apply_playlist_filter()

    def _get_playlist_var_key(self, playlist):
        return playlist.get('url') or playlist.get('id') or playlist.get('title')

    def _get_playlist_var(self, playlist):
        key = self._get_playlist_var_key(playlist)
        var = self.playlist_vars.get(key)
        if var is None:
            var = tk.BooleanVar(value=False)
            self.playlist_vars[key] = var
        return var

    def apply_playlist_filter(self):
        query = normalize_search_text(self.playlist_search_var.get())
        if not query:
            self.filtered_playlists_data = list(self.playlists_data)
        else:
            self.filtered_playlists_data = [
                playlist for playlist in self.playlists_data
                if query in normalize_search_text(playlist.get('title') or '')
            ]
        self._render_playlists()

    def fetch_playlists(self):
        url = self.url_var.get().strip()
        if not url or url.startswith("მაგ:"):
            messagebox.showwarning("გაფრთხილება", "შეიყვანეთ არხის პლეილისტების ლინკი!")
            return

        self.fetch_btn.configure(state="disabled", text="იძებნება...")
        self.status_var.set("პლეილისტების მოძიება...")

        thread = threading.Thread(target=self._fetch_playlists_thread, args=(url,))
        thread.daemon = True
        thread.start()

    def _fetch_playlists_thread(self, url):
        try:
            normalized_url = normalize_channel_playlists_url(url)
            ydl_opts = get_max_quality_options()
            ydl_opts.update({
                'quiet': True,
                'no_warnings': True,
                'extract_flat': 'in_playlist',
                'lazy_playlist': False,
                'legacy_server_connect': True,
                'nocheckcertificate': True,
                'skip_download': True,
            })

            info = ydl_extract_info_with_cookie_fallback(normalized_url, ydl_opts, download=False)

            playlists = collect_playlist_entries(info)
            playlists.sort(key=lambda item: normalize_search_text(item.get('title') or ''))

            self.playlists_data = playlists
            self.filtered_playlists_data = list(playlists)
            schedule_ui(self, self._display_playlists)
        except Exception as e:
            err = format_ydl_error(e)
            msg = f"პლეილისტების მოძიება ვერ მოხერხდა:\n{err}"
            schedule_ui(self, lambda msg=msg: self._show_error(msg))


    def _display_playlists(self):
        self.fetch_btn.configure(state="normal", text="პლეილისტების ძებნა")
        self.apply_playlist_filter()

    def _render_playlists(self):
        for widget in self.playlists_inner_frame.winfo_children():
            widget.destroy()
        self.playlist_checkboxes = []

        total_count = len(self.playlists_data)
        visible_count = len(self.filtered_playlists_data)
        if total_count:
            if self.playlist_search_var.get().strip():
                self.playlists_count_label.configure(text=f"({visible_count}/{total_count} ნაპოვნი)")
            else:
                self.playlists_count_label.configure(text=f"({total_count} ცალი)")
        else:
            self.playlists_count_label.configure(text="")

        if not self.playlists_data:
            ttk.Label(self.playlists_inner_frame, text="პლეილისტები ვერ მოიძებნა",
                      style="Info.TLabel").pack(anchor="w", pady=5)
        elif not self.filtered_playlists_data:
            ttk.Label(self.playlists_inner_frame, text="სერჩით ვერ მოიძებნა არცერთი პლეილისტი",
                      style="Info.TLabel").pack(anchor="w", pady=5)
        else:
            for i, playlist in enumerate(self.filtered_playlists_data, start=1):
                var = self._get_playlist_var(playlist)
                self.playlist_checkboxes.append(var)

                cb_frame = ttk.Frame(self.playlists_inner_frame, style="Card.TFrame")
                cb_frame.pack(fill="x", pady=2)

                title = playlist.get('title') or 'უცნობი პლეილისტი'
                cb = ttk.Checkbutton(
                    cb_frame,
                    text=f"{i}. {title[:60]}{'...' if len(title) > 60 else ''}",
                    variable=var,
                    style="Card.TCheckbutton"
                )
                cb.pack(anchor="w")

        if total_count:
            self.status_var.set(f"{total_count} პლეილისტი მოიძებნა")
        else:
            self.status_var.set("პლეილისტები ვერ მოიძებნა")

    def _show_error(self, error):
        self.fetch_btn.configure(state="normal", text="პლეილისტების ძებნა")
        self.status_var.set("შეცდომა")
        messagebox.showerror("შეცდომა", error)

    def start_download(self):
        selected_playlists = []
        for playlist in self.playlists_data:
            if self._get_playlist_var(playlist).get():
                selected_playlists.append(playlist)

        if not selected_playlists:
            messagebox.showwarning("გაფრთხილება", "მონიშნეთ ჩამოსატვირთი პლეილისტები!")
            return

        if self.is_downloading:
            return

        self.is_downloading = True
        self.cancel_requested = False
        self.download_started_at = time.monotonic()
        self.item_timing_states = {}
        self.download_btn.configure(state="disabled", text="მიმდინარეობს...", bg=ModernStyle.WARNING)
        self.progress_var.set(0)
        order_text = "ბოლოდან→1" if get_owner_setting(self, 'reverse_playlist_order', self.reverse_playlist_order_var, False) else "1→ბოლო"
        names_text = "სახელები: 1,2,3" if get_owner_setting(self, 'numbered_filenames', self.numbered_filenames_var, False) else "სახელები: ნომერი + სათაური"
        self.status_var.set(join_status_with_timing(f"{len(selected_playlists)} პლეილისტის ჩამოტვირთვა იწყება — {order_text} | {names_text}", self.download_started_at, percent=0))

        self._download_settings_snapshot = capture_download_settings(self)
        thread = threading.Thread(target=self._download_playlists_thread, args=(selected_playlists,))
        thread.daemon = True
        thread.start()

    def _download_playlists_thread(self, playlists):
        downloaded_count = 0
        resumed_completed = 0
        skipped_duplicates = 0
        skipped_existing_by_name = 0
        quality_fallbacks = 0
        size_limit_fallbacks = 0
        failed = 0
        interrupted = False
        output_folders = []
        current_session = None
        current_key = None

        try:
            base_output_path = get_owner_setting(self, 'download_path', self.download_path)
            os.makedirs(base_output_path, exist_ok=True)

            total_playlists = len(playlists)
            meta_opts = get_max_quality_options()
            meta_opts.update({
                'extract_flat': 'in_playlist',
                'quiet': True,
                'ignoreerrors': True,
                'skip_download': True,
            })

            custom_base_name = get_owner_setting(self, 'history_name', self.history_name_var, '').strip()

            for playlist_idx, playlist in enumerate(playlists, start=1):
                if self.cancel_requested:
                    interrupted = True
                    break

                current_session = None
                current_key = None
                try:
                    playlist_title = (playlist or {}).get('title') or f'პლეილისტი {playlist_idx}'
                    playlist_url = (playlist or {}).get('url')
                    if not playlist_url:
                        failed += 1
                        schedule_ui(
                            self,
                            lambda p=playlist_idx, t=total_playlists: self.status_var.set(
                                join_status_with_timing(
                                    f"პლეილისტი {p}/{t} ვერ დამუშავდა — URL ვერ მოიძებნა",
                                    self.download_started_at,
                                    percent=((p - 1) / max(t, 1)) * 100,
                                )
                            ),
                        )
                        continue

                    schedule_ui(
                        self,
                        lambda p=playlist_idx, t=total_playlists, n=playlist_title: self.status_var.set(
                            join_status_with_timing(
                                f"პლეილისტი {p}/{t}: {n[:40]} — სიის წაკითხვა...",
                                self.download_started_at,
                                percent=((p - 1) / max(t, 1)) * 100,
                            )
                        ),
                    )

                    playlist_info = ydl_extract_info_with_cookie_fallback(playlist_url, meta_opts, download=False)
                    if not isinstance(playlist_info, dict):
                        raise ValueError('პლეილისტის ინფო ცარიელია.')

                    entries = playlist_info.get('entries') or []
                    if not isinstance(entries, list):
                        entries = list(entries)
                    entries = [entry for entry in entries if isinstance(entry, dict)]
                    if get_owner_setting(self, 'reverse_playlist_order', self.reverse_playlist_order_var, False):
                        entries = list(reversed(entries))

                    prepared_entries = []
                    history_items = []
                    for entry in entries:
                        raw_video_url = entry.get('url') or entry.get('webpage_url')
                        if not raw_video_url:
                            continue
                        initial_key = canonical_video_key(raw_video_url, entry=entry)
                        if initial_key and initial_key.startswith('id:'):
                            video_url = f"https://www.youtube.com/watch?v={initial_key.split(':', 1)[1]}"
                        else:
                            video_url = str(raw_video_url)
                            if not video_url.startswith('http'):
                                video_url = f"https://www.youtube.com/watch?v={video_url}"

                        index = len(prepared_entries) + 1
                        item = build_history_item(
                            video_url,
                            index,
                            title=entry.get('title') or f'ვიდეო {index}',
                            entry=entry,
                        )
                        prepared_entries.append((entry, video_url, item))
                        history_items.append(item)

                    total_entries = len(prepared_entries)
                    if total_entries == 0:
                        raise ValueError('პლეილისტი ცარიელია ან ვიდეოები ვერ მოიძებნა.')

                    if custom_base_name:
                        custom_name = custom_base_name if total_playlists == 1 else f"{custom_base_name} - {playlist_title}"
                    else:
                        custom_name = ''

                    settings = build_download_history_settings(
                        get_owner_setting(self, 'format', self.format_var, 'video'),
                        get_owner_setting(self, 'quality', self.quality_var, 'best'),
                        get_owner_setting(self, 'audio_quality', self.audio_quality_var, '320'),
                        get_owner_setting(self, 'limit_size', self.limit_size_var, False),
                        get_owner_setting(self, 'skip_existing_name', self.skip_existing_name_var, True),
                    )

                    # ძველი ქცევა: თითო პლეილისტის ვიდეოები ინახება არჩეულ
                    # საქაღალდეში, პლეილისტის სახელის ცალკე ფოლდერში.
                    safe_playlist_title = re.sub(r'[<>:"/\\|?*]', '_', playlist_title)[:50]
                    playlist_output_dir = resolve_output_folder_path(
                        base_output_path,
                        safe_playlist_title or f'playlist_{playlist_idx}',
                        reuse_existing=get_owner_setting(self, 'skip_existing_name', self.skip_existing_name_var, True),
                    )

                    current_session = DownloadHistorySession(
                        playlist_output_dir,
                        'playlist',
                        [playlist_url],
                        custom_name=custom_name,
                        preferred_title=playlist_title,
                        settings=settings,
                    )
                    self.active_history_session = current_session
                    self.active_output_path = current_session.output_dir
                    output_folders.append(current_session.output_dir)
                    current_session.ensure_items(history_items)

                    pad_width = max(3, len(str(total_entries)))
                    seen_video_keys = set()

                    for entry_idx, (entry, video_url, history_item) in enumerate(prepared_entries, start=1):
                        current_key = history_item['key']
                        self.active_history_item_key = current_key

                        if self.cancel_requested:
                            interrupted = True
                            break

                        if current_session.is_completed(current_key):
                            resumed_completed += 1
                            seen_video_keys.add(current_key)
                            overall = get_playlist_overall_percent(
                                playlist_idx,
                                total_playlists,
                                entry_idx,
                                total_entries,
                                100,
                            )
                            schedule_ui(self, lambda value=overall: self.progress_var.set(value))
                            schedule_ui(
                                self,
                                lambda p=playlist_idx, t=total_playlists, e=entry_idx, te=total_entries, ov=overall: self.status_var.set(
                                    join_status_with_timing(
                                        f"პლეილისტი {p}/{t} | ვიდეო {e}/{te} — JSON: უკვე დასრულებულია",
                                        self.download_started_at,
                                        percent=ov,
                                    )
                                ),
                            )
                            continue

                        current_session.begin_item(
                            current_key,
                            title=history_item.get('title', ''),
                            url=video_url,
                            index=entry_idx,
                        )

                        video_key = canonical_video_key(video_url, entry=entry) or current_key
                        if video_key in seen_video_keys:
                            skipped_duplicates += 1
                            current_session.mark_skipped_duplicate(current_key, duplicate_of=video_key)
                            continue

                        schedule_ui(
                            self,
                            lambda p=playlist_idx, t=total_playlists, e=entry_idx, te=total_entries: self.status_var.set(
                                join_status_with_timing(
                                    f"პლეილისტი {p}/{t} | ვიდეო {e}/{te}",
                                    self.download_started_at,
                                    percent=(((p - 1) + ((e - 1) / max(te, 1))) / max(t, 1)) * 100,
                                )
                            ),
                        )

                        try:
                            ydl_opts = get_max_quality_options()
                            ydl_opts.update({
                                'windowsfilenames': True,
                                'quiet': True,
                                'ignoreerrors': False,
                                'noplaylist': True,
                            })

                            output_template = self._build_playlist_output_template(
                                current_session.output_dir,
                                entry_idx,
                                pad_width,
                            )

                            if get_owner_setting(self, 'format', self.format_var, 'video') == "video":
                                requested_quality = get_owner_setting(self, 'quality', self.quality_var, 'best')
                                info = fetch_video_metadata(video_url, noplaylist=True)
                                video_key = canonical_video_key(video_url, entry=entry, info=info) or video_key

                                item_record = current_session.get_item(current_key)
                                if item_record is not None:
                                    item_record['title'] = info.get('title') or item_record.get('title')
                                    current_session._save()

                                if get_owner_setting(self, 'skip_existing_name', self.skip_existing_name_var, True):
                                    existing_path = find_existing_media_by_output_name(
                                        current_session.output_dir,
                                        output_template,
                                        info.get('title') or entry.get('title') or history_item.get('title') or 'video',
                                        'video',
                                    )
                                    if existing_path:
                                        skipped_duplicates += 1
                                        skipped_existing_by_name += 1
                                        current_session.mark_skipped_duplicate(current_key, duplicate_of=f"existing_file:{os.path.basename(existing_path)}")
                                        if video_key:
                                            seen_video_keys.add(video_key)
                                        schedule_ui(
                                            self,
                                            lambda p=playlist_idx, t=total_playlists, e=entry_idx, te=total_entries, name=os.path.basename(existing_path): self.status_var.set(
                                                join_status_with_timing(
                                                    f"პლეილისტი {p}/{t} | ვიდეო {e}/{te} — იგივე სახელის ფაილი არსებობს, გამოტოვდა: {name[:60]}",
                                                    self.download_started_at,
                                                    percent=(((p - 1) + (e / max(te, 1))) / max(t, 1)) * 100,
                                                )
                                            ),
                                        )
                                        continue

                                effective_quality, effective_height, used_fallback, used_size_limit, _estimated_size, size_limit_from_height = resolve_video_quality_with_size_limit(
                                    info,
                                    requested_quality,
                                    size_limit_enabled=get_owner_setting(self, 'limit_size', self.limit_size_var, False),
                                )
                                if used_fallback and requested_quality != 'best':
                                    quality_fallbacks += 1
                                if used_size_limit:
                                    size_limit_fallbacks += 1
                                ensure_video_pipeline_ready(info, effective_quality)
                                video_opts, _, _ = build_video_download_options(info, effective_quality)
                                ydl_opts.update(video_opts)
                                self._set_item_postprocess_estimate(
                                    playlist_idx,
                                    entry_idx,
                                    estimate_expected_postprocess_total_seconds(info, format_kind='video'),
                                )

                                if (used_fallback and requested_quality != 'best') or used_size_limit:
                                    quality_text = quality_label_from_height(effective_height) if effective_height else 'ხელმისაწვდომი ხარისხი'
                                    note_parts = []
                                    if used_fallback and requested_quality != 'best':
                                        note_parts.append('არჩეული ხარისხი ვერ მოიძებნა')
                                    if used_size_limit:
                                        from_quality_text = quality_label_from_height(size_limit_from_height) if size_limit_from_height else 'უცნობი'
                                        note_parts.append(f'2GB ლიმიტი: {from_quality_text} → {quality_text}')
                                    note_text = ' | '.join(note_parts)
                                    video_title = entry.get('title') or info.get('title') or 'უცნობი ვიდეო'
                                    schedule_ui(
                                        self,
                                        lambda p=playlist_idx, t=total_playlists, e=entry_idx, te=total_entries, q=quality_text, n=note_text, title=video_title: self.status_var.set(
                                            join_status_with_timing(
                                                f"პლეილისტი {p}/{t} | ვიდეო {e}/{te} — {n} | {title[:45]}{'...' if len(title) > 45 else ''} → {q}",
                                                self.download_started_at,
                                                percent=(((p - 1) + ((e - 1) / max(te, 1))) / max(t, 1)) * 100,
                                            )
                                        ),
                                    )
                            else:
                                audio_info = fetch_video_metadata(video_url, noplaylist=True)
                                ydl_opts.update(get_audio_download_options(get_owner_setting(self, 'audio_quality', self.audio_quality_var, '320')))
                                self._set_item_postprocess_estimate(
                                    playlist_idx,
                                    entry_idx,
                                    estimate_expected_postprocess_total_seconds(audio_info, format_kind='audio'),
                                )
                                item_record = current_session.get_item(current_key)
                                if item_record is not None:
                                    item_record['title'] = audio_info.get('title') or item_record.get('title')
                                    current_session._save()

                                if get_owner_setting(self, 'skip_existing_name', self.skip_existing_name_var, True):
                                    existing_path = find_existing_media_by_output_name(
                                        current_session.output_dir,
                                        output_template,
                                        audio_info.get('title') or entry.get('title') or history_item.get('title') or 'audio',
                                        'audio',
                                    )
                                    if existing_path:
                                        skipped_duplicates += 1
                                        skipped_existing_by_name += 1
                                        current_session.mark_skipped_duplicate(current_key, duplicate_of=f"existing_file:{os.path.basename(existing_path)}")
                                        if video_key:
                                            seen_video_keys.add(video_key)
                                        schedule_ui(
                                            self,
                                            lambda p=playlist_idx, t=total_playlists, e=entry_idx, te=total_entries, name=os.path.basename(existing_path): self.status_var.set(
                                                join_status_with_timing(
                                                    f"პლეილისტი {p}/{t} | აუდიო {e}/{te} — იგივე სახელის ფაილი არსებობს, გამოტოვდა: {name[:60]}",
                                                    self.download_started_at,
                                                    percent=(((p - 1) + (e / max(te, 1))) / max(t, 1)) * 100,
                                                )
                                            ),
                                        )
                                        continue

                            ydl_opts = prepare_history_download_options(
                                ydl_opts,
                                current_session,
                                current_key,
                                output_template,
                                ui_progress_hook=self._make_playlist_progress_hook(
                                    playlist_idx,
                                    total_playlists,
                                    entry_idx,
                                    total_entries,
                                    entry.get('title') or '',
                                ),
                                ui_postprocessor_hook=self._make_playlist_postprocessor_hook(
                                    playlist_idx,
                                    total_playlists,
                                    entry_idx,
                                    total_entries,
                                    entry.get('title') or '',
                                ),
                                stop_checker=lambda: bool(self.cancel_requested),
                            )

                            ydl_download_with_cookie_fallback([video_url], ydl_opts)
                            current_session.mark_completed(current_key)
                            seen_video_keys.add(video_key)
                            downloaded_count += 1
                        except Exception as entry_error:
                            if is_user_requested_stop_error(entry_error) or self.cancel_requested:
                                interrupted = True
                                current_session.mark_interrupted(current_key, 'ჩამოტვირთვა შეწყდა დასრულებამდე.')
                                break
                            current_session.mark_failed(current_key, entry_error)
                            failed += 1
                            err_text = summarize_error_for_ui(entry_error)
                            schedule_ui(
                                self,
                                lambda p=playlist_idx, t=total_playlists, e=entry_idx, te=total_entries, err=err_text: self.status_var.set(
                                    join_status_with_timing(
                                        f"პლეილისტი {p}/{t} | ვიდეო {e}/{te} ვერ ჩაიწერა — {err[:80]}",
                                        self.download_started_at,
                                        percent=(((p - 1) + (e / max(te, 1))) / max(t, 1)) * 100,
                                    )
                                ),
                            )
                            continue

                    current_session.finalize(interrupted=interrupted)
                    if interrupted:
                        break

                except Exception as playlist_error:
                    if current_session is not None and current_key and not is_user_requested_stop_error(playlist_error):
                        current_session.mark_failed(current_key, playlist_error)
                        current_session.finalize()
                    if is_user_requested_stop_error(playlist_error) or self.cancel_requested:
                        interrupted = True
                        break
                    failed += 1
                    err_text = summarize_error_for_ui(playlist_error)
                    schedule_ui(
                        self,
                        lambda p=playlist_idx, t=total_playlists, err=err_text: self.status_var.set(
                            join_status_with_timing(
                                f"პლეილისტი {p}/{t} შეწყდა — {err[:80]}",
                                self.download_started_at,
                                percent=((p - 1) / max(t, 1)) * 100,
                            )
                        ),
                    )
                    continue
                finally:
                    if current_session is not None:
                        current_session.close()
                    self.active_history_session = None
                    self.active_history_item_key = None

            summary = {
                'downloaded': downloaded_count,
                'resumed_completed': resumed_completed,
                'duplicates': skipped_duplicates,
                'skipped_existing_by_name': skipped_existing_by_name,
                'quality_fallbacks': quality_fallbacks,
                'size_limit_fallbacks': size_limit_fallbacks,
                'failed': failed,
                'interrupted': interrupted,
                'output_folders': output_folders,
            }
            schedule_ui(self, lambda s=summary: self._download_complete(s))
        except Exception as exc:
            if current_session is not None and current_key and not is_user_requested_stop_error(exc):
                current_session.mark_failed(current_key, exc)
                current_session.finalize()
            err = format_ydl_error(exc)
            schedule_ui(self, lambda err=err: self._download_error(f"პლეილისტების ჩამოტვირთვა შეწყდა:\n{err}"))
        finally:
            if current_session is not None:
                current_session.close()
            self.active_history_session = None
            self.active_history_item_key = None

    def _get_item_timing_state(self, playlist_idx, entry_idx):
        key = (playlist_idx, entry_idx)
        state = self.item_timing_states.get(key)
        if state is None:
            state = {
                'predicted_postprocess_total': None,
                'postprocess_started_at': None,
                'current_postprocessor': '',
            }
            self.item_timing_states[key] = state
        return state

    def _set_item_postprocess_estimate(self, playlist_idx, entry_idx, seconds):
        state = self._get_item_timing_state(playlist_idx, entry_idx)
        if seconds:
            state['predicted_postprocess_total'] = max(float(state.get('predicted_postprocess_total') or 0), float(seconds))

    def _make_playlist_postprocessor_hook(self, playlist_idx, total_playlists, entry_idx, total_entries, title=''):
        def hook(d):
            status = d.get('status')
            if status not in {'started', 'processing', 'finished'}:
                return

            state = self._get_item_timing_state(playlist_idx, entry_idx)
            postprocessor_name = d.get('postprocessor') or state.get('current_postprocessor') or ''
            state['current_postprocessor'] = postprocessor_name

            if state.get('postprocess_started_at') is None and status in {'started', 'processing'}:
                state['postprocess_started_at'] = time.monotonic()

            if status == 'started' and d.get('info_dict'):
                predicted = estimate_postprocess_total_seconds(d.get('info_dict'), postprocessor_name)
                if predicted:
                    state['predicted_postprocess_total'] = max(float(state.get('predicted_postprocess_total') or 0), float(predicted))

            if status in {'started', 'processing'}:
                item_percent = get_postprocess_phase_percent(state.get('postprocess_started_at'), state.get('predicted_postprocess_total'))
                step_percent = get_postprocess_step_percent(state.get('postprocess_started_at'), state.get('predicted_postprocess_total'))
                overall = get_playlist_overall_percent(playlist_idx, total_playlists, entry_idx, total_entries, item_percent)
                remaining = get_postprocess_remaining_seconds(state.get('postprocess_started_at'), state.get('predicted_postprocess_total'))
                short_title = f" | {title[:45]}{'...' if len(title) > 45 else ''}" if title else ''
                label = get_postprocessor_label(postprocessor_name)
                schedule_ui(self, lambda value=overall: self.progress_var.set(value))
                schedule_ui(self, lambda s=join_status_with_timing(
                    f"პლეილისტი {playlist_idx}/{total_playlists} | ვიდეო {entry_idx}/{total_entries} | {label}{short_title}",
                    self.download_started_at, percent=overall, remaining_seconds=remaining, display_percent=step_percent,
                ): self.status_var.set(s))
        return hook

    def _make_playlist_progress_hook(self, playlist_idx, total_playlists, entry_idx, total_entries, title=''):
        def hook(d):
            state = self._get_item_timing_state(playlist_idx, entry_idx)
            if d.get('status') == 'downloading':
                percent_str = d.get('_percent_str', '0%').strip()
                speed = d.get('_speed_str', 'N/A')
                eta = d.get('_eta_str', 'N/A')
                overall = None
                try:
                    percent = float(percent_str.replace('%', ''))
                    item_percent = get_download_phase_percent(percent, state.get('predicted_postprocess_total'))
                    overall = get_playlist_overall_percent(playlist_idx, total_playlists, entry_idx, total_entries, item_percent)
                    schedule_ui(self, lambda value=overall: self.progress_var.set(value))
                except Exception:
                    overall = None
                short_title = f" | {title[:45]}{'...' if len(title) > 45 else ''}" if title else ''
                status = join_status_with_timing(
                    f"პლეილისტი {playlist_idx}/{total_playlists} | ვიდეო {entry_idx}/{total_entries} | {percent_str} | სიჩქარე: {speed} | დარჩენილი: {eta}{short_title}",
                    self.download_started_at,
                    percent=overall,
                )
                schedule_ui(self, lambda s=status: self.status_var.set(s))
            elif d.get('status') == 'finished':
                item_percent = get_download_phase_percent(100, state.get('predicted_postprocess_total'))
                overall = get_playlist_overall_percent(playlist_idx, total_playlists, entry_idx, total_entries, item_percent)
                schedule_ui(self, lambda value=overall: self.progress_var.set(value))
                schedule_ui(self, lambda: self.status_var.set(join_status_with_timing(
                    f"პლეილისტი {playlist_idx}/{total_playlists} | ვიდეო {entry_idx}/{total_entries} | {get_postprocessor_label(state.get('current_postprocessor'))}",
                    self.download_started_at,
                    percent=overall,
                )))
        return hook

    def _download_complete(self, summary=None):
        self.is_downloading = False
        self.download_btn.configure(state="normal", text="ჩამოტვირთვა (MAX Quality)", bg=ModernStyle.ACCENT)
        summary = summary or {}
        interrupted = bool(summary.get('interrupted'))
        downloaded = summary.get('downloaded', 0)
        resumed = summary.get('resumed_completed', 0)
        duplicates = summary.get('duplicates', 0)
        skipped_existing_by_name = summary.get('skipped_existing_by_name', 0)
        duplicate_by_id = max(0, int(duplicates or 0) - int(skipped_existing_by_name or 0))
        failed = summary.get('failed', 0)
        output_folders = summary.get('output_folders') or []
        folders_preview = "\n".join(output_folders[:5]) or get_owner_setting(self, 'download_path', self.download_path)
        if len(output_folders) > 5:
            folders_preview += f"\n... და კიდევ {len(output_folders) - 5} საქაღალდე"

        if interrupted:
            self.status_var.set(join_status_with_timing(
                f"შეჩერდა | ახალი: {downloaded} | JSON: {resumed} | სახელით: {skipped_existing_by_name} | დუბლიკატი: {duplicate_by_id} | შეცდომა: {failed}",
                self.download_started_at,
            ))
            messagebox.showinfo(
                "ჩამოტვირთვა შეჩერდა",
                f"დასრულებული ვიდეოები JSON-ში შენახულია. მიმდინარე დაუსრულებელი ვიდეო შემდეგ გაშვებაზე თავიდან დაიწყება.\n\nიგივე სახელით გამოტოვებული: {skipped_existing_by_name}\nID დუბლიკატი: {duplicate_by_id}\n\nსაქაღალდეები:\n{folders_preview}",
            )
            return

        self.progress_var.set(100)
        self.status_var.set(join_status_with_timing(
            f"დასრულდა | ახალი: {downloaded} | JSON: {resumed} | სახელით: {skipped_existing_by_name} | დუბლიკატი: {duplicate_by_id} | "
            f"დაბალ ხარისხზე: {summary.get('quality_fallbacks', 0)} | 2GB↓: {summary.get('size_limit_fallbacks', 0)} | შეცდომა: {failed}",
            self.download_started_at,
            percent=100,
        ))
        messagebox.showinfo(
            "დასრულდა",
            f"ახლად ჩაწერილი: {downloaded}\n"
            f"JSON-ით უკვე დასრულებული და გამოტოვებული: {resumed}\n"
            f"იგივე სახელით გამოტოვებული: {skipped_existing_by_name}\n"
            f"ID დუბლიკატი: {duplicate_by_id}\n"
            f"არჩეულზე დაბალ ხარისხზე გადავიდა: {summary.get('quality_fallbacks', 0)}\n"
            f"2GB ლიმიტით დაბლა ჩამოვიდა: {summary.get('size_limit_fallbacks', 0)}\n"
            f"შეცდომა: {failed}\n\n"
            f"თითო პლეილისტის ვიდეოები შენახულია საკუთარ საქაღალდეში:\n{folders_preview}\n\nJSON ისტორიები:\n{get_download_history_root()}",
        )

    def _download_error(self, error):
        self.is_downloading = False
        self.download_btn.configure(state="normal", text="ჩამოტვირთვა (MAX Quality)", bg=ModernStyle.ACCENT)
        self.progress_var.set(0)
        self.status_var.set(join_status_with_timing("შეცდომა!", self.download_started_at))
        messagebox.showerror("შეცდომა", error)


# ============================
# მთავარი ფუნქცია
# ============================
def main():
    try:
        app = MainMenuGUI()
        app.run()
    except Exception as error:
        try:
            APP_LOGGER.error('პროგრამის გაშვების შეცდომა', exc_info=True)
        except Exception:
            pass
        try:
            messagebox.showerror(
                'პროგრამის გაშვების შეცდომა',
                f'{error}\n\nსრული დეტალები ჩაწერილია:\n{get_logs_root()}',
            )
        except Exception:
            print(f'პროგრამის გაშვების შეცდომა: {error}')
        raise


if __name__ == "__main__":
    main()