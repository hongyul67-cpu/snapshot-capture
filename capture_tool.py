# -*- coding: utf-8 -*-
"""
SnapShot - 간단하고 깔끔한 화면 캡처 도구
기능: 영역/전체화면/활성창/모니터별 캡처, 연속(인터벌) 캡처,
      MP4 동영상 녹화, 전역 단축키, 캡처 모션 피드백, 자동 저장/파일명 규칙
"""
import ctypes
import os
import sys
import json
import time
import threading
import subprocess
from datetime import datetime

# --- DPI 인식: 멀티모니터 좌표 정확도를 위해 Tk 생성 전에 설정 ---
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import mss
from PIL import Image
from pynput import keyboard
import imageio_ffmpeg

# ----------------------------------------------------------------------
# 설정 저장 위치
# ----------------------------------------------------------------------
APP_NAME = "SnapShot"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "save_dir": os.path.join(os.path.expanduser("~"), "Pictures", "SnapShot"),
    "filename_prefix": "",          # 비어있으면 날짜시간 기본값 사용
    "img_format": "png",            # png / jpg
    "hide_window_on_capture": True,
    "interval_seconds": 5,
    "interval_target": "전체화면",
    "video_fps": 15,
    "video_target": "전체화면",
    "hotkeys": {
        "region":   "<ctrl>+<shift>+1",
        "fullscreen": "<ctrl>+<shift>+2",
        "window":   "<ctrl>+<shift>+3",
        "monitor":  "<ctrl>+<shift>+4",
        "video":    "<ctrl>+<shift>+5",
        "interval": "<ctrl>+<shift>+6",
    },
    "monitor_index": 1,             # 모니터 캡처 대상(1부터)
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if k == "hotkeys" and isinstance(v, dict):
                cfg["hotkeys"].update(v)
            else:
                cfg[k] = v
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("config save error:", e)


# ----------------------------------------------------------------------
# Windows 활성창 사각형 얻기 (그림자 제외한 실제 프레임)
# ----------------------------------------------------------------------
def get_active_window_rect():
    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = RECT()
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    res = dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(rect), ctypes.sizeof(rect))
    if res != 0:
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
    left, top = rect.left, rect.top
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    return {"left": left, "top": top, "width": width, "height": height}


# ----------------------------------------------------------------------
# 파일명 생성
# ----------------------------------------------------------------------
def next_filename(cfg, ext):
    save_dir = cfg["save_dir"]
    os.makedirs(save_dir, exist_ok=True)
    prefix = cfg.get("filename_prefix", "").strip()
    if prefix:
        # prefix_001, prefix_002 ... 폴더 내 기존 최대 순번 다음 번호
        n = 1
        existing = 0
        try:
            for name in os.listdir(save_dir):
                base, e = os.path.splitext(name)
                if base.startswith(prefix + "_"):
                    tail = base[len(prefix) + 1:]
                    if tail.isdigit():
                        existing = max(existing, int(tail))
        except Exception:
            pass
        n = existing + 1
        return os.path.join(save_dir, f"{prefix}_{n:03d}.{ext}")
    else:
        # 날짜시간 기본값(밀리초 포함 → 연속 캡처도 안 겹침)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        return os.path.join(save_dir, f"capture_{stamp}.{ext}")


# ----------------------------------------------------------------------
# 실제 화면 grab (BGRA/RGB)
# ----------------------------------------------------------------------
def grab_region(region):
    with mss.mss() as sct:
        shot = sct.grab(region)
        img = Image.frombytes("RGB", shot.size, shot.rgb)
    return img


def get_monitors():
    """mss monitors[0]=가상데스크톱 전체, [1..]=각 모니터"""
    with mss.mss() as sct:
        return [dict(m) for m in sct.monitors]


# ----------------------------------------------------------------------
# 동영상 녹화기 (ffmpeg 파이프)
# ----------------------------------------------------------------------
class VideoRecorder:
    def __init__(self, region, fps, out_path, on_state=None):
        self.region = dict(region)
        # yuv420p 위해 폭/높이 짝수화
        self.region["width"] -= self.region["width"] % 2
        self.region["height"] -= self.region["height"] % 2
        self.fps = max(1, int(fps))
        self.out_path = out_path
        self.on_state = on_state
        self._stop = threading.Event()
        self._thread = None
        self.proc = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        w, h = self.region["width"], self.region["height"]
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg, "-y",
            "-f", "rawvideo", "-pix_fmt", "bgra",
            "-s", f"{w}x{h}", "-r", str(self.fps),
            "-i", "-",
            "-an",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            self.out_path,
        ]
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            print("ffmpeg start error:", e)
            if self.on_state:
                self.on_state("error")
            return

        frame_interval = 1.0 / self.fps
        next_t = time.perf_counter()
        try:
            with mss.mss() as sct:
                while not self._stop.is_set():
                    shot = sct.grab(self.region)
                    try:
                        self.proc.stdin.write(bytes(shot.raw))
                    except (BrokenPipeError, ValueError):
                        break
                    next_t += frame_interval
                    sleep = next_t - time.perf_counter()
                    if sleep > 0:
                        time.sleep(sleep)
                    else:
                        next_t = time.perf_counter()
        finally:
            try:
                if self.proc and self.proc.stdin:
                    self.proc.stdin.close()
            except Exception:
                pass
            try:
                if self.proc:
                    self.proc.wait(timeout=10)
            except Exception:
                pass
            if self.on_state:
                self.on_state("done")


# ----------------------------------------------------------------------
# 색상 테마
# ----------------------------------------------------------------------
C_BG = "#0f172a"        # 진한 남색 배경
C_CARD = "#1e293b"      # 카드
C_CARD2 = "#273449"
C_ACCENT = "#6366f1"    # 인디고
C_ACCENT_H = "#818cf8"
C_TEXT = "#e2e8f0"
C_SUB = "#94a3b8"
C_GREEN = "#22c55e"
C_RED = "#ef4444"
C_BORDER = "#334155"


class CaptureApp:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.recorder = None
        self.recording = False
        self.interval_running = False
        self._interval_after = None
        self.hotkey_listener = None
        self._recording_edit = None  # 단축키 편집중인 필드

        root.title("SnapShot — 화면 캡처")
        root.configure(bg=C_BG)
        root.resizable(False, False)
        try:
            root.iconbitmap(default="")  # 아이콘 없어도 무시
        except Exception:
            pass

        self._build_style()
        self._build_ui()
        self._register_hotkeys()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        # 화면 중앙 배치
        root.update_idletasks()
        w, h = root.winfo_width(), root.winfo_height()
        x = (root.winfo_screenwidth() - w) // 2
        y = (root.winfo_screenheight() - h) // 3
        root.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------
    def _build_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("TCombobox", fieldbackground=C_CARD2, background=C_CARD2,
                     foreground=C_TEXT, arrowcolor=C_TEXT, borderwidth=0)
        st.map("TCombobox", fieldbackground=[("readonly", C_CARD2)],
               foreground=[("readonly", C_TEXT)])

    # ------------------------------------------------------------------
    def _card(self, parent, title):
        wrap = tk.Frame(parent, bg=C_CARD, highlightbackground=C_BORDER,
                        highlightthickness=1)
        wrap.pack(fill="x", padx=16, pady=(0, 12))
        if title:
            tk.Label(wrap, text=title, bg=C_CARD, fg=C_SUB,
                     font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=14, pady=(10, 2))
        return wrap

    def _mkbtn(self, parent, text, cmd, bg=C_CARD2, fg=C_TEXT, hover=C_ACCENT,
               font=("Segoe UI", 10), pad=(0, 8), width=None):
        b = tk.Label(parent, text=text, bg=bg, fg=fg, font=font,
                     cursor="hand2", padx=10, pady=pad[1])
        if width:
            b.configure(width=width)
        b.bind("<Enter>", lambda e: b.configure(bg=hover, fg="#ffffff"))
        b.bind("<Leave>", lambda e: b.configure(bg=bg, fg=fg))
        b.bind("<Button-1>", lambda e: cmd())
        b._base_bg = bg
        b._base_fg = fg
        return b

    def _build_ui(self):
        root = self.root

        # ---- 헤더 ----
        header = tk.Frame(root, bg=C_BG)
        header.pack(fill="x", padx=16, pady=(16, 12))
        tk.Label(header, text="📸  SnapShot", bg=C_BG, fg=C_TEXT,
                 font=("Segoe UI", 16, "bold")).pack(side="left")
        tk.Label(header, text="화면 캡처 도구", bg=C_BG, fg=C_SUB,
                 font=("Segoe UI", 9)).pack(side="left", padx=(8, 0), pady=(6, 0))

        # ---- 캡처 모드 버튼 ----
        card = self._card(root, "캡처")
        grid = tk.Frame(card, bg=C_CARD)
        grid.pack(fill="x", padx=14, pady=(4, 6))
        modes = [
            ("⬚  영역 선택", self.capture_region),
            ("🖥  전체 화면", self.capture_fullscreen),
            ("🪟  활성 창", self.capture_window),
            ("🖵  모니터별", self.capture_monitor),
        ]
        for i, (txt, cmd) in enumerate(modes):
            b = self._mkbtn(grid, txt, cmd, font=("Segoe UI", 10), pad=(0, 12))
            b.grid(row=i // 2, column=i % 2, sticky="ew", padx=4, pady=4)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        # 모니터 선택 드롭다운
        monrow = tk.Frame(card, bg=C_CARD)
        monrow.pack(fill="x", padx=14, pady=(0, 12))
        tk.Label(monrow, text="모니터별 대상:", bg=C_CARD, fg=C_SUB,
                 font=("Segoe UI", 9)).pack(side="left")
        mons = get_monitors()
        self.monitor_choices = [f"모니터 {i}" for i in range(1, len(mons))]
        if not self.monitor_choices:
            self.monitor_choices = ["모니터 1"]
        self.monitor_var = tk.StringVar(
            value=f"모니터 {min(self.cfg.get('monitor_index',1), len(self.monitor_choices))}")
        self.monitor_cb = ttk.Combobox(monrow, values=self.monitor_choices,
                                       textvariable=self.monitor_var, state="readonly",
                                       width=12, font=("Segoe UI", 9))
        self.monitor_cb.pack(side="left", padx=8)

        # ---- 동영상 녹화 ----
        vcard = self._card(root, "동영상 녹화 (MP4)")
        vrow = tk.Frame(vcard, bg=C_CARD)
        vrow.pack(fill="x", padx=14, pady=(4, 12))
        self.video_btn = self._mkbtn(vrow, "●  녹화 시작", self.toggle_video,
                                     bg=C_CARD2, hover=C_RED, font=("Segoe UI", 10, "bold"),
                                     pad=(0, 10))
        self.video_btn.pack(side="left")
        tk.Label(vrow, text="대상:", bg=C_CARD, fg=C_SUB,
                 font=("Segoe UI", 9)).pack(side="left", padx=(12, 4))
        self.video_target_var = tk.StringVar(value=self.cfg.get("video_target", "전체화면"))
        vtargets = ["전체화면", "활성창"] + self.monitor_choices
        ttk.Combobox(vrow, values=vtargets, textvariable=self.video_target_var,
                     state="readonly", width=10, font=("Segoe UI", 9)).pack(side="left")
        tk.Label(vrow, text="FPS", bg=C_CARD, fg=C_SUB,
                 font=("Segoe UI", 9)).pack(side="left", padx=(10, 3))
        self.fps_var = tk.StringVar(value=str(self.cfg.get("video_fps", 15)))
        tk.Entry(vrow, textvariable=self.fps_var, width=4, bg=C_CARD2, fg=C_TEXT,
                 insertbackground=C_TEXT, relief="flat", justify="center",
                 font=("Segoe UI", 9)).pack(side="left")

        # ---- 연속(인터벌) 캡처 ----
        icard = self._card(root, "연속 캡처 (지정 초마다)")
        irow = tk.Frame(icard, bg=C_CARD)
        irow.pack(fill="x", padx=14, pady=(4, 12))
        self.interval_btn = self._mkbtn(irow, "▶  시작", self.toggle_interval,
                                        bg=C_CARD2, hover=C_GREEN,
                                        font=("Segoe UI", 10, "bold"), pad=(0, 10))
        self.interval_btn.pack(side="left")
        tk.Label(irow, text="간격", bg=C_CARD, fg=C_SUB,
                 font=("Segoe UI", 9)).pack(side="left", padx=(12, 3))
        self.interval_var = tk.StringVar(value=str(self.cfg.get("interval_seconds", 5)))
        tk.Entry(irow, textvariable=self.interval_var, width=4, bg=C_CARD2, fg=C_TEXT,
                 insertbackground=C_TEXT, relief="flat", justify="center",
                 font=("Segoe UI", 9)).pack(side="left")
        tk.Label(irow, text="초", bg=C_CARD, fg=C_SUB,
                 font=("Segoe UI", 9)).pack(side="left", padx=(3, 10))
        tk.Label(irow, text="대상:", bg=C_CARD, fg=C_SUB,
                 font=("Segoe UI", 9)).pack(side="left")
        self.interval_target_var = tk.StringVar(value=self.cfg.get("interval_target", "전체화면"))
        itargets = ["전체화면", "활성창"] + self.monitor_choices
        ttk.Combobox(irow, values=itargets, textvariable=self.interval_target_var,
                     state="readonly", width=8, font=("Segoe UI", 9)).pack(side="left", padx=4)

        # ---- 저장 설정 ----
        scard = self._card(root, "저장 설정")
        # 폴더
        f1 = tk.Frame(scard, bg=C_CARD)
        f1.pack(fill="x", padx=14, pady=(4, 4))
        tk.Label(f1, text="폴더", bg=C_CARD, fg=C_SUB, width=6, anchor="w",
                 font=("Segoe UI", 9)).pack(side="left")
        self.dir_var = tk.StringVar(value=self.cfg["save_dir"])
        tk.Entry(f1, textvariable=self.dir_var, bg=C_CARD2, fg=C_TEXT,
                 insertbackground=C_TEXT, relief="flat",
                 font=("Segoe UI", 9)).pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._mkbtn(f1, "찾기", self.browse_dir, bg=C_CARD2, pad=(0, 4)).pack(side="left")
        # 이름/포맷
        f2 = tk.Frame(scard, bg=C_CARD)
        f2.pack(fill="x", padx=14, pady=(4, 12))
        tk.Label(f2, text="이름", bg=C_CARD, fg=C_SUB, width=6, anchor="w",
                 font=("Segoe UI", 9)).pack(side="left")
        self.prefix_var = tk.StringVar(value=self.cfg.get("filename_prefix", ""))
        e = tk.Entry(f2, textvariable=self.prefix_var, bg=C_CARD2, fg=C_TEXT,
                     insertbackground=C_TEXT, relief="flat", font=("Segoe UI", 9))
        e.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._add_placeholder(e, self.prefix_var, "비우면 날짜_시간 자동")
        tk.Label(f2, text="형식", bg=C_CARD, fg=C_SUB,
                 font=("Segoe UI", 9)).pack(side="left", padx=(4, 3))
        self.fmt_var = tk.StringVar(value=self.cfg.get("img_format", "png"))
        ttk.Combobox(f2, values=["png", "jpg"], textvariable=self.fmt_var,
                     state="readonly", width=5, font=("Segoe UI", 9)).pack(side="left")

        # ---- 단축키 ----
        hcard = self._card(root, "단축키 (클릭 후 원하는 키 조합 입력)")
        self.hotkey_labels = {}
        hk_names = [
            ("region", "영역 선택"), ("fullscreen", "전체 화면"),
            ("window", "활성 창"), ("monitor", "모니터별"),
            ("video", "녹화 시작/정지"), ("interval", "연속 시작/정지"),
        ]
        hg = tk.Frame(hcard, bg=C_CARD)
        hg.pack(fill="x", padx=14, pady=(4, 12))
        for i, (key, label) in enumerate(hk_names):
            r, c = i // 2, (i % 2) * 2
            tk.Label(hg, text=label, bg=C_CARD, fg=C_SUB, anchor="w",
                     font=("Segoe UI", 9), width=10).grid(row=r, column=c, sticky="w", pady=3)
            lbl = tk.Label(hg, text=self._pretty_hotkey(self.cfg["hotkeys"][key]),
                           bg=C_CARD2, fg=C_TEXT, font=("Consolas", 9),
                           padx=8, pady=3, cursor="hand2")
            lbl.grid(row=r, column=c + 1, sticky="ew", padx=(4, 12), pady=3)
            lbl.bind("<Button-1>", lambda e, k=key: self._start_hotkey_edit(k))
            self.hotkey_labels[key] = lbl
        hg.columnconfigure(1, weight=1)
        hg.columnconfigure(3, weight=1)

        # 옵션 체크
        opt = tk.Frame(root, bg=C_BG)
        opt.pack(fill="x", padx=18, pady=(0, 6))
        self.hide_var = tk.BooleanVar(value=self.cfg.get("hide_window_on_capture", True))
        cb = tk.Checkbutton(opt, text="캡처 시 이 창 잠깐 숨기기",
                            variable=self.hide_var, bg=C_BG, fg=C_SUB,
                            selectcolor=C_CARD2, activebackground=C_BG,
                            activeforeground=C_TEXT, font=("Segoe UI", 9),
                            highlightthickness=0, bd=0)
        cb.pack(side="left")

        # ---- 상태바 ----
        self.status = tk.Label(root, text="준비됨", bg=C_CARD, fg=C_SUB,
                               anchor="w", font=("Segoe UI", 9), padx=16, pady=6)
        self.status.pack(fill="x", side="bottom")

    def _add_placeholder(self, entry, var, text):
        def on_focus_in(e):
            if entry._is_ph:
                entry.delete(0, "end")
                entry.configure(fg=C_TEXT)
                entry._is_ph = False

        def on_focus_out(e):
            if not var.get().strip():
                entry._is_ph = True
                entry.configure(fg=C_SUB)
                entry.delete(0, "end")
                entry.insert(0, text)

        entry._is_ph = False
        if not var.get().strip():
            entry._is_ph = True
            entry.configure(fg=C_SUB)
            entry.insert(0, text)
        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)

    def _get_prefix(self):
        # 플레이스홀더 상태 처리
        v = self.prefix_var.get().strip()
        if v == "비우면 날짜_시간 자동":
            return ""
        return v

    # ------------------------------------------------------------------
    # 단축키
    # ------------------------------------------------------------------
    def _pretty_hotkey(self, hk):
        return (hk.replace("<ctrl>", "Ctrl").replace("<shift>", "Shift")
                .replace("<alt>", "Alt").replace("<cmd>", "Win").replace("+", " + "))

    def _register_hotkeys(self):
        if self.hotkey_listener:
            try:
                self.hotkey_listener.stop()
            except Exception:
                pass
        mapping = {}
        hk = self.cfg["hotkeys"]
        actions = {
            "region": self.capture_region,
            "fullscreen": self.capture_fullscreen,
            "window": self.capture_window,
            "monitor": self.capture_monitor,
            "video": self.toggle_video,
            "interval": self.toggle_interval,
        }
        for key, combo in hk.items():
            if combo and key in actions:
                # pynput 콜백은 별도 스레드 → 메인스레드로 마샬링
                mapping[combo] = (lambda a=actions[key]: self.root.after(0, a))
        try:
            self.hotkey_listener = keyboard.GlobalHotKeys(mapping)
            self.hotkey_listener.start()
        except Exception as e:
            print("hotkey register error:", e)

    def _start_hotkey_edit(self, key):
        lbl = self.hotkey_labels[key]
        lbl.configure(text="키 입력...", bg=C_ACCENT, fg="#ffffff")
        self._recording_edit = key
        self._pressed_mods = set()
        # 전역 리스너 잠시 중지
        if self.hotkey_listener:
            try:
                self.hotkey_listener.stop()
            except Exception:
                pass
        self.root.bind("<KeyPress>", self._on_hotkey_key)

    def _on_hotkey_key(self, event):
        if not self._recording_edit:
            return
        mods = []
        if event.state & 0x0004:
            mods.append("<ctrl>")
        if event.state & 0x0001:
            mods.append("<shift>")
        if event.state & 0x20000 or event.state & 0x0008:
            mods.append("<alt>")
        keysym = event.keysym
        mod_keys = {"Control_L", "Control_R", "Shift_L", "Shift_R",
                    "Alt_L", "Alt_R", "Win_L", "Win_R"}
        if keysym in mod_keys:
            return  # 조합키만으론 확정 안 함
        # 키 이름 정규화
        k = keysym.lower()
        special = {"prior": "page_up", "next": "page_down"}
        k = special.get(k, k)
        if len(k) == 1 or k.isdigit():
            keypart = k
        elif keysym.startswith("F") and keysym[1:].isdigit():
            keypart = f"<{keysym.lower()}>"
        else:
            keypart = k
        combo = "+".join(mods + [keypart])
        key = self._recording_edit
        self.cfg["hotkeys"][key] = combo
        self.hotkey_labels[key].configure(
            text=self._pretty_hotkey(combo), bg=C_CARD2, fg=C_TEXT)
        self._recording_edit = None
        self.root.unbind("<KeyPress>")
        save_config(self._collect_cfg())
        self._register_hotkeys()
        self.set_status(f"단축키 설정됨: {self._pretty_hotkey(combo)}")

    # ------------------------------------------------------------------
    # 저장/설정 수집
    # ------------------------------------------------------------------
    def _collect_cfg(self):
        try:
            fps = int(self.fps_var.get())
        except Exception:
            fps = 15
        try:
            interval = max(1, int(self.interval_var.get()))
        except Exception:
            interval = 5
        mon_idx = 1
        try:
            mon_idx = int(self.monitor_var.get().split()[-1])
        except Exception:
            pass
        self.cfg.update({
            "save_dir": self.dir_var.get().strip() or DEFAULT_CONFIG["save_dir"],
            "filename_prefix": self._get_prefix(),
            "img_format": self.fmt_var.get(),
            "hide_window_on_capture": bool(self.hide_var.get()),
            "interval_seconds": interval,
            "interval_target": self.interval_target_var.get(),
            "video_fps": fps,
            "video_target": self.video_target_var.get(),
            "monitor_index": mon_idx,
        })
        return self.cfg

    def browse_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get())
        if d:
            self.dir_var.set(d)

    def set_status(self, text, color=C_SUB):
        self.status.configure(text=text, fg=color)

    # ------------------------------------------------------------------
    # 대상 → region 계산
    # ------------------------------------------------------------------
    def _target_region(self, target):
        mons = get_monitors()
        if target == "전체화면":
            return dict(mons[0])
        if target == "활성창":
            r = get_active_window_rect()
            return r
        if target.startswith("모니터"):
            try:
                idx = int(target.split()[-1])
            except Exception:
                idx = 1
            if idx < len(mons):
                return dict(mons[idx])
            return dict(mons[0])
        return dict(mons[0])

    def _selected_monitor_region(self):
        mons = get_monitors()
        try:
            idx = int(self.monitor_var.get().split()[-1])
        except Exception:
            idx = 1
        if idx < len(mons):
            return dict(mons[idx])
        return dict(mons[0])

    # ------------------------------------------------------------------
    # 이미지 캡처 진입점들
    # ------------------------------------------------------------------
    def _do_capture(self, region, flash=True):
        if not region or region["width"] <= 0 or region["height"] <= 0:
            self.set_status("캡처 대상을 찾을 수 없습니다", C_RED)
            return
        self._collect_cfg()
        ext = self.cfg["img_format"]
        pil_ext = "JPEG" if ext == "jpg" else "PNG"
        try:
            img = grab_region(region)
            path = next_filename(self.cfg, ext)
            if pil_ext == "JPEG":
                img = img.convert("RGB")
                img.save(path, pil_ext, quality=92)
            else:
                img.save(path, pil_ext)
        except Exception as e:
            self.set_status(f"저장 실패: {e}", C_RED)
            return
        if flash:
            self.flash(region)
        self.set_status(f"저장됨 · {os.path.basename(path)}", C_GREEN)

    def _capture_with_hide(self, region_fn):
        """캡처 시 창 숨기기 옵션 처리 후 캡처"""
        if self.hide_var.get():
            self.root.withdraw()
            self.root.after(160, lambda: self._after_hide(region_fn))
        else:
            region = region_fn()
            self._do_capture(region)

    def _after_hide(self, region_fn):
        region = region_fn()
        self._do_capture(region)
        self.root.deiconify()

    def capture_fullscreen(self):
        self._capture_with_hide(lambda: self._target_region("전체화면"))

    def capture_window(self):
        # 활성창은 숨기면 대상이 바뀌므로 숨기지 않고 바로 캡처
        region = get_active_window_rect()
        self._do_capture(region)

    def capture_monitor(self):
        self._capture_with_hide(self._selected_monitor_region)

    def capture_region(self):
        RegionSelector(self.root, self._on_region_selected)

    def _on_region_selected(self, region):
        if region:
            self._do_capture(region, flash=True)
        else:
            self.set_status("영역 선택 취소됨")

    # ------------------------------------------------------------------
    # 캡처 모션 (플래시)
    # ------------------------------------------------------------------
    def flash(self, region):
        try:
            fl = tk.Toplevel(self.root)
            fl.overrideredirect(True)
            fl.attributes("-topmost", True)
            fl.configure(bg="white")
            fl.geometry(f"{region['width']}x{region['height']}+{region['left']}+{region['top']}")
            fl.attributes("-alpha", 0.55)

            def fade(a=0.55):
                if a <= 0:
                    fl.destroy()
                    return
                fl.attributes("-alpha", a)
                fl.after(20, lambda: fade(a - 0.07))
            fade()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 동영상
    # ------------------------------------------------------------------
    def toggle_video(self):
        if self.recording:
            self.stop_video()
        else:
            self.start_video()

    def start_video(self):
        self._collect_cfg()
        target = self.video_target_var.get()
        region = self._target_region(target)
        if not region:
            self.set_status("녹화 대상을 찾을 수 없습니다", C_RED)
            return
        ext = "mp4"
        path = next_filename(self.cfg, ext)
        self.recorder = VideoRecorder(
            region, self.cfg["video_fps"], path,
            on_state=lambda s: self.root.after(0, lambda: self._on_video_state(s, path)))
        self.recorder.start()
        self.recording = True
        self.video_btn.configure(text="■  녹화 정지", bg=C_RED, fg="#ffffff")
        self.video_btn._base_bg = C_RED
        self.set_status(f"● 녹화 중... ({target})", C_RED)

    def stop_video(self):
        if self.recorder:
            self.recorder.stop()
        self.recording = False
        self.video_btn.configure(text="●  녹화 시작", bg=C_CARD2, fg=C_TEXT)
        self.video_btn._base_bg = C_CARD2
        self.set_status("녹화 저장 중...", C_SUB)

    def _on_video_state(self, state, path):
        if state == "done":
            self.set_status(f"동영상 저장됨 · {os.path.basename(path)}", C_GREEN)
        elif state == "error":
            self.recording = False
            self.video_btn.configure(text="●  녹화 시작", bg=C_CARD2, fg=C_TEXT)
            self.set_status("녹화 오류(ffmpeg)", C_RED)

    # ------------------------------------------------------------------
    # 연속(인터벌) 캡처
    # ------------------------------------------------------------------
    def toggle_interval(self):
        if self.interval_running:
            self.stop_interval()
        else:
            self.start_interval()

    def start_interval(self):
        self._collect_cfg()
        self.interval_running = True
        self.interval_btn.configure(text="■  정지", bg=C_GREEN, fg="#ffffff")
        self.interval_btn._base_bg = C_GREEN
        self._interval_tick(first=True)

    def stop_interval(self):
        self.interval_running = False
        if self._interval_after:
            try:
                self.root.after_cancel(self._interval_after)
            except Exception:
                pass
        self.interval_btn.configure(text="▶  시작", bg=C_CARD2, fg=C_TEXT)
        self.interval_btn._base_bg = C_CARD2
        self.set_status("연속 캡처 정지됨")

    def _interval_tick(self, first=False):
        if not self.interval_running:
            return
        target = self.interval_target_var.get()
        region = self._target_region(target)
        self._do_capture(region, flash=True)
        secs = self.cfg.get("interval_seconds", 5)
        self.set_status(f"연속 캡처 중 · {secs}초마다 ({target})", C_GREEN)
        self._interval_after = self.root.after(secs * 1000, self._interval_tick)

    # ------------------------------------------------------------------
    def on_close(self):
        try:
            self._collect_cfg()
            save_config(self.cfg)
        except Exception:
            pass
        if self.recording and self.recorder:
            self.recorder.stop()
        if self.hotkey_listener:
            try:
                self.hotkey_listener.stop()
            except Exception:
                pass
        self.root.destroy()


# ----------------------------------------------------------------------
# 영역 선택 오버레이 (가상 데스크톱 전체 커버)
# ----------------------------------------------------------------------
class RegionSelector:
    def __init__(self, root, callback):
        self.callback = callback
        mons = get_monitors()
        v = mons[0]  # 가상 데스크톱 전체
        self.vx, self.vy = v["left"], v["top"]

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.geometry(f"{v['width']}x{v['height']}+{v['left']}+{v['top']}")
        self.top.attributes("-alpha", 0.3)
        self.top.configure(bg="black", cursor="crosshair")

        self.canvas = tk.Canvas(self.top, bg="black", highlightthickness=0,
                                width=v["width"], height=v["height"])
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            v["width"] // 2, 40,
            text="드래그하여 영역 선택  ·  ESC 취소",
            fill="#e2e8f0", font=("Segoe UI", 13))

        self.start = None
        self.rect = None
        self.canvas.bind("<Button-1>", self._down)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._up)
        self.top.bind("<Escape>", lambda e: self._cancel())
        self.top.focus_force()

    def _down(self, e):
        self.start = (e.x, e.y)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                                 outline=C_ACCENT_H, width=2)

    def _move(self, e):
        if self.start and self.rect:
            self.canvas.coords(self.rect, self.start[0], self.start[1], e.x, e.y)

    def _up(self, e):
        if not self.start:
            return self._cancel()
        x1, y1 = self.start
        x2, y2 = e.x, e.y
        left, top = min(x1, x2), min(y1, y2)
        w, h = abs(x2 - x1), abs(y2 - y1)
        self.top.destroy()
        if w < 5 or h < 5:
            self.callback(None)
            return
        region = {"left": self.vx + left, "top": self.vy + top,
                  "width": w, "height": h}
        self.callback(region)

    def _cancel(self):
        self.top.destroy()
        self.callback(None)


def _selftest():
    """frozen exe에서 ffmpeg 녹화가 동작하는지 검증 (내부 진단용)."""
    out = os.path.join(os.path.dirname(sys.executable), "_selftest.mp4")
    region = {"left": 0, "top": 0, "width": 200, "height": 120}
    rec = VideoRecorder(region, 10, out)
    rec.start()
    time.sleep(1.5)
    rec.stop()
    time.sleep(2)
    ok = os.path.exists(out) and os.path.getsize(out) > 0
    with open(os.path.join(os.path.dirname(sys.executable), "_selftest.txt"), "w") as f:
        f.write(f"ffmpeg={imageio_ffmpeg.get_ffmpeg_exe()}\nvideo_ok={ok}\n")
    sys.exit(0)


def main():
    if "--selftest" in sys.argv:
        _selftest()
    root = tk.Tk()
    app = CaptureApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
