"""
Smart Transfer - Modern folder copy/zip tool with smart exclusions
Built with CustomTkinter for a professional developer-tool appearance.
"""

import os
import sys
import json
import shutil
import zipfile
import threading
import time
import queue
from pathlib import Path
from datetime import datetime
from tkinter import filedialog, messagebox
import tkinter as tk

import customtkinter as ctk


# ─── App Configuration ────────────────────────────────────────────────────────

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

APP_NAME = "Smart Transfer"
VERSION = "1.0.0"
CONFIG_FILE = Path.home() / ".smart_transfer_config.json"

PRESETS = {
    "MERN": ["node_modules", ".git", ".env"],
    "React Native": ["node_modules", ".git", "android/build", "ios/build"],
    "Python": ["__pycache__", ".venv", "venv", ".git", "*.pyc", ".pytest_cache", "dist", "build", "*.egg-info"],
    "Flutter": [".dart_tool", "build", ".flutter-plugins", ".pub-cache", ".git"],
    "Java / Maven": ["target", ".git", "*.class", "*.jar"],
    "Node.js": ["node_modules", ".git", ".env", "dist", "build", ".nyc_output", "coverage"],
}

# Colour tokens
CLR_BG       = "#0f1117"   # deepest background
CLR_SURFACE  = "#1a1d27"   # card / panel
CLR_BORDER   = "#2a2d3a"   # subtle border
CLR_ACCENT   = "#5b8cff"   # primary blue
CLR_ACCENT2  = "#7c5cfc"   # secondary purple
CLR_SUCCESS  = "#22c55e"
CLR_WARNING  = "#f59e0b"
CLR_DANGER   = "#ef4444"
CLR_TEXT     = "#e2e8f0"
CLR_MUTED    = "#64748b"
CLR_SIDEBAR  = "#13161e"


# ─── Utilities ────────────────────────────────────────────────────────────────

def format_size(n_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} PB"


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {"recent_projects": [], "custom_presets": {}}


def save_config(cfg: dict):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


# ─── Scanner ──────────────────────────────────────────────────────────────────

class Scanner:
    """Scans a source folder respecting the exclusion list."""

    def __init__(self, source: Path, exclusions: set[str]):
        self.source = source
        self.exclusions = exclusions
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _is_excluded(self, name: str) -> bool:
        return name in self.exclusions

    def scan(self, progress_cb=None):
        """
        Returns:
            stats      – dict with totals
            large_dirs – list of (rel_path, size, file_count)
            file_list  – list of Path objects to transfer
        """
        total_files = total_dirs = 0
        original_size = excluded_size = transfer_size = 0
        dir_sizes: dict[Path, list] = {}  # rel_path -> [size, count]
        file_list: list[Path] = []

        root = self.source
        for dirpath, dirnames, filenames in os.walk(root):
            if self._cancel:
                break

            rel_dir = Path(dirpath).relative_to(root)
            # Prune excluded directories in-place so os.walk skips them
            pruned = []
            for d in list(dirnames):
                if self._is_excluded(d):
                    # measure excluded subtree size quickly
                    exc_path = Path(dirpath) / d
                    exc_size = sum(
                        f.stat().st_size
                        for f in exc_path.rglob("*")
                        if f.is_file()
                    )
                    excluded_size += exc_size
                else:
                    pruned.append(d)
            dirnames[:] = pruned
            total_dirs += len(dirnames)

            for fname in filenames:
                if self._cancel:
                    break
                if self._is_excluded(fname):
                    exc_size = (Path(dirpath) / fname).stat().st_size
                    excluded_size += exc_size
                    continue
                fpath = Path(dirpath) / fname
                try:
                    fsize = fpath.stat().st_size
                except OSError:
                    continue
                total_files += 1
                original_size += fsize
                transfer_size += fsize
                file_list.append(fpath)

                # Accumulate per first-level-dir
                parts = rel_dir.parts
                if parts:
                    top = Path(parts[0])
                    if top not in dir_sizes:
                        dir_sizes[top] = [0, 0]
                    dir_sizes[top][0] += fsize
                    dir_sizes[top][1] += 1

            if progress_cb:
                progress_cb(total_files)

        # Add any excluded top-level dirs to dir_sizes list for display
        for dirpath, dirnames, _ in os.walk(root):
            if Path(dirpath) != root:
                break
            for d in os.listdir(root):
                fp = root / d
                if fp.is_dir() and self._is_excluded(d) and Path(d) not in dir_sizes:
                    try:
                        s = sum(f.stat().st_size for f in fp.rglob("*") if f.is_file())
                        c = sum(1 for f in fp.rglob("*") if f.is_file())
                        dir_sizes[Path(d)] = [s, c]
                    except Exception:
                        pass

        large_dirs = sorted(
            [(str(k), v[0], v[1]) for k, v in dir_sizes.items()],
            key=lambda x: x[1], reverse=True
        )[:20]

        stats = {
            "total_files": total_files,
            "total_dirs": total_dirs,
            "original_size": original_size + excluded_size,
            "excluded_size": excluded_size,
            "transfer_size": transfer_size,
        }
        return stats, large_dirs, file_list


# ─── Transfer Engine ──────────────────────────────────────────────────────────

class TransferEngine:
    """Handles copy-to-folder and create-ZIP operations with progress reporting."""

    def __init__(self, source: Path, exclusions: set[str], msg_queue: queue.Queue):
        self.source = source
        self.exclusions = exclusions
        self.q = msg_queue
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _is_excluded(self, name: str) -> bool:
        return name in self.exclusions

    def _put(self, **kwargs):
        self.q.put(kwargs)

    def copy_folder(self, dest: Path, file_list: list[Path], total_size: int):
        copied = skipped = errors = 0
        copied_size = 0
        start = time.time()

        for i, src_file in enumerate(file_list):
            if self._cancel:
                self._put(type="cancelled")
                return
            rel = src_file.relative_to(self.source)
            dst_file = dest / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src_file, dst_file)
                copied_size += src_file.stat().st_size
                copied += 1
            except PermissionError as e:
                errors += 1
                self._put(type="warning", msg=f"Permission denied: {rel}")
            except OSError as e:
                if "No space left" in str(e):
                    self._put(type="error", msg="Disk full — transfer aborted.")
                    return
                errors += 1

            pct = copied_size / total_size if total_size else 0
            self._put(type="progress", pct=pct, current=str(rel),
                      done=i + 1, total=len(file_list))

        duration = time.time() - start
        self._put(type="done", copied=copied, skipped=skipped, errors=errors,
                  size=copied_size, duration=duration, dest=str(dest))

    def create_zip(self, zip_path: Path, file_list: list[Path], total_size: int):
        added = errors = 0
        added_size = 0
        start = time.time()

        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for i, src_file in enumerate(file_list):
                    if self._cancel:
                        self._put(type="cancelled")
                        return
                    rel = src_file.relative_to(self.source)
                    try:
                        zf.write(src_file, rel)
                        added_size += src_file.stat().st_size
                        added += 1
                    except PermissionError:
                        errors += 1
                    except OSError as e:
                        if "No space left" in str(e):
                            self._put(type="error", msg="Disk full — ZIP creation aborted.")
                            return
                        errors += 1

                    pct = added_size / total_size if total_size else 0
                    self._put(type="progress", pct=pct, current=str(rel),
                              done=i + 1, total=len(file_list))

            final_size = zip_path.stat().st_size
        except Exception as e:
            self._put(type="error", msg=f"ZIP creation failed: {e}")
            return

        duration = time.time() - start
        self._put(type="done", copied=added, skipped=0, errors=errors,
                  size=added_size, zip_size=final_size, duration=duration, dest=str(zip_path))


# ─── UI Components ────────────────────────────────────────────────────────────

class SidebarButton(ctk.CTkButton):
    def __init__(self, master, text, icon, command, **kwargs):
        super().__init__(
            master,
            text=f"  {icon}  {text}",
            command=command,
            anchor="w",
            height=42,
            corner_radius=8,
            fg_color="transparent",
            hover_color=CLR_BORDER,
            text_color=CLR_TEXT,
            font=ctk.CTkFont(size=13, weight="normal"),
            **kwargs,
        )

    def set_active(self, active: bool):
        self.configure(
            fg_color=CLR_ACCENT if active else "transparent",
            font=ctk.CTkFont(size=13, weight="bold" if active else "normal"),
        )


class StatCard(ctk.CTkFrame):
    def __init__(self, master, label: str, value: str = "—", color=CLR_TEXT, **kwargs):
        super().__init__(master, fg_color=CLR_SURFACE, corner_radius=10,
                         border_width=1, border_color=CLR_BORDER, **kwargs)
        self.value_lbl = ctk.CTkLabel(self, text=value,
                                      font=ctk.CTkFont(size=22, weight="bold"),
                                      text_color=color)
        self.value_lbl.pack(pady=(16, 2))
        ctk.CTkLabel(self, text=label, font=ctk.CTkFont(size=11),
                     text_color=CLR_MUTED).pack(pady=(0, 14))

    def update_value(self, value: str, color=CLR_TEXT):
        self.value_lbl.configure(text=value, text_color=color)


class PathSelector(ctk.CTkFrame):
    def __init__(self, master, label: str, placeholder: str,
                 mode: str = "folder", **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.mode = mode
        self._var = ctk.StringVar()

        ctk.CTkLabel(self, text=label, font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=CLR_MUTED).pack(anchor="w", pady=(0, 4))
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x")
        self.entry = ctk.CTkEntry(row, textvariable=self._var,
                                  placeholder_text=placeholder,
                                  fg_color=CLR_SURFACE, border_color=CLR_BORDER,
                                  height=38)
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(row, text="Browse", width=80, height=38,
                      fg_color=CLR_ACCENT, hover_color=CLR_ACCENT2,
                      command=self._browse).pack(side="left")

    def _browse(self):
        if self.mode == "folder":
            p = filedialog.askdirectory(title="Select folder")
        elif self.mode == "zip":
            p = filedialog.asksaveasfilename(
                title="Save ZIP as",
                defaultextension=".zip",
                filetypes=[("ZIP archive", "*.zip")],
            )
        else:
            p = filedialog.askopenfilename()
        if p:
            self._var.set(p)

    def get(self) -> str:
        return self._var.get().strip()

    def set(self, value: str):
        self._var.set(value)


# ─── Pages ────────────────────────────────────────────────────────────────────

class BasePage(ctk.CTkFrame):
    def __init__(self, master, app, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.app = app

    def on_show(self):
        pass


class TransferPage(BasePage):
    """Main transfer page: source, exclusions, analyze, copy/zip."""

    def __init__(self, master, app, **kwargs):
        super().__init__(master, app, **kwargs)
        self._file_list: list[Path] = []
        self._stats: dict = {}
        self._scan_thread: threading.Thread | None = None
        self._transfer_thread: threading.Thread | None = None
        self._engine: TransferEngine | None = None
        self._msg_queue: queue.Queue = queue.Queue()
        self._build_ui()

    # ── UI builder ─────────────────────────────────────────────────────────

    def _build_ui(self):
        # Title row
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 20))
        ctk.CTkLabel(hdr, text="Smart Transfer",
                     font=ctk.CTkFont(size=26, weight="bold"),
                     text_color=CLR_TEXT).pack(side="left")
        ctk.CTkLabel(hdr, text=f"v{VERSION}",
                     font=ctk.CTkFont(size=11),
                     text_color=CLR_MUTED).pack(side="left", padx=8, pady=(8, 0))

        # Two-column layout
        cols = ctk.CTkFrame(self, fg_color="transparent")
        cols.pack(fill="both", expand=True)
        cols.columnconfigure(0, weight=3)
        cols.columnconfigure(1, weight=2)
        cols.rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(cols, fg_color="transparent",
                                      scrollbar_button_color=CLR_BORDER)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        right = ctk.CTkScrollableFrame(cols, fg_color="transparent",
                                       scrollbar_button_color=CLR_BORDER)
        right.grid(row=0, column=1, sticky="nsew")

        self._build_left(left)
        self._build_right(right)

    def _section(self, parent, title: str) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color=CLR_SURFACE, corner_radius=12,
                             border_width=1, border_color=CLR_BORDER)
        frame.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(frame, text=title,
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=CLR_ACCENT).pack(anchor="w", padx=16, pady=(14, 8))
        return frame

    def _build_left(self, parent):
        # ── Source ─────────────────────────────────────────────────────────
        sec = self._section(parent, "📂  Source Folder")
        inner = ctk.CTkFrame(sec, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=(0, 16))

        self.src_selector = PathSelector(inner, "Source", "Select source folder…")
        self.src_selector.pack(fill="x", pady=(0, 8))

        # Recent projects
        recent_row = ctk.CTkFrame(inner, fg_color="transparent")
        recent_row.pack(fill="x")
        ctk.CTkLabel(recent_row, text="Recent:", text_color=CLR_MUTED,
                     font=ctk.CTkFont(size=11)).pack(side="left")
        self.recent_var = ctk.StringVar(value="")
        self.recent_menu = ctk.CTkOptionMenu(
            recent_row, variable=self.recent_var,
            values=self._get_recent_labels(),
            command=self._on_recent_select,
            fg_color=CLR_BORDER, button_color=CLR_ACCENT,
            width=280, height=28,
        )
        self.recent_menu.pack(side="left", padx=8)

        # ── Exclusion List ─────────────────────────────────────────────────
        sec2 = self._section(parent, "🚫  Exclusion List")
        exc_inner = ctk.CTkFrame(sec2, fg_color="transparent")
        exc_inner.pack(fill="x", padx=16, pady=(0, 16))

        # Preset row
        preset_row = ctk.CTkFrame(exc_inner, fg_color="transparent")
        preset_row.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(preset_row, text="Preset:", text_color=CLR_MUTED,
                     font=ctk.CTkFont(size=11)).pack(side="left")
        self.preset_var = ctk.StringVar(value="Select preset…")
        preset_names = list(PRESETS.keys()) + list(self.app.cfg.get("custom_presets", {}).keys())
        ctk.CTkOptionMenu(
            preset_row, variable=self.preset_var,
            values=preset_names,
            command=self._apply_preset,
            fg_color=CLR_BORDER, button_color=CLR_ACCENT,
            width=200, height=28,
        ).pack(side="left", padx=8)
        ctk.CTkButton(preset_row, text="Save as Preset", width=110, height=28,
                      fg_color="transparent", border_width=1, border_color=CLR_ACCENT,
                      text_color=CLR_ACCENT, hover_color=CLR_BORDER,
                      command=self._save_preset).pack(side="left")

        # Input row
        add_row = ctk.CTkFrame(exc_inner, fg_color="transparent")
        add_row.pack(fill="x", pady=(0, 8))
        self.exc_entry = ctk.CTkEntry(add_row, placeholder_text="e.g. node_modules",
                                      fg_color=CLR_SURFACE, border_color=CLR_BORDER,
                                      height=36)
        self.exc_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.exc_entry.bind("<Return>", lambda _: self._add_exclusion())
        ctk.CTkButton(add_row, text="Add", width=60, height=36,
                      fg_color=CLR_ACCENT, hover_color=CLR_ACCENT2,
                      command=self._add_exclusion).pack(side="left")

        # Listbox (using CTkTextbox with tag tricks → use CTkScrollableFrame + labels)
        self.exc_listbox = tk.Listbox(
            exc_inner,
            bg=CLR_SURFACE, fg=CLR_TEXT,
            selectbackground=CLR_ACCENT, selectforeground="#fff",
            relief="flat", highlightthickness=1,
            highlightbackground=CLR_BORDER,
            font=("Consolas", 12),
            height=8,
            activestyle="none",
        )
        self.exc_listbox.pack(fill="x", pady=(0, 8))

        btn_row = ctk.CTkFrame(exc_inner, fg_color="transparent")
        btn_row.pack(fill="x")
        ctk.CTkButton(btn_row, text="Remove Selected", width=130, height=30,
                      fg_color="transparent", border_width=1, border_color=CLR_DANGER,
                      text_color=CLR_DANGER, hover_color=CLR_BORDER,
                      command=self._remove_exclusion).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_row, text="Clear All", width=90, height=30,
                      fg_color="transparent", border_width=1, border_color=CLR_MUTED,
                      text_color=CLR_MUTED, hover_color=CLR_BORDER,
                      command=self._clear_exclusions).pack(side="left")
        self.exc_count_lbl = ctk.CTkLabel(btn_row, text="0 exclusions",
                                           text_color=CLR_MUTED, font=ctk.CTkFont(size=11))
        self.exc_count_lbl.pack(side="right")

        # ── Destination ────────────────────────────────────────────────────
        sec3 = self._section(parent, "📁  Destination")
        dest_inner = ctk.CTkFrame(sec3, fg_color="transparent")
        dest_inner.pack(fill="x", padx=16, pady=(0, 16))

        self.dst_folder = PathSelector(dest_inner, "Copy to folder", "Select destination folder…")
        self.dst_folder.pack(fill="x", pady=(0, 10))
        self.dst_zip = PathSelector(dest_inner, "Create ZIP at", "Select ZIP file path…", mode="zip")
        self.dst_zip.pack(fill="x")

        # ── Action Buttons ─────────────────────────────────────────────────
        actions = ctk.CTkFrame(parent, fg_color="transparent")
        actions.pack(fill="x", pady=(4, 0))
        actions.columnconfigure((0, 1, 2), weight=1)

        self.analyze_btn = ctk.CTkButton(
            actions, text="🔍  Analyze", height=44,
            fg_color=CLR_BORDER, hover_color="#343750",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._start_analyze,
        )
        self.analyze_btn.grid(row=0, column=0, padx=(0, 6), sticky="ew")

        self.copy_btn = ctk.CTkButton(
            actions, text="📋  Copy Folder", height=44,
            fg_color=CLR_ACCENT, hover_color=CLR_ACCENT2,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._start_copy,
        )
        self.copy_btn.grid(row=0, column=1, padx=6, sticky="ew")

        self.zip_btn = ctk.CTkButton(
            actions, text="🗜  Create ZIP", height=44,
            fg_color="#5c3dc8", hover_color=CLR_ACCENT2,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._start_zip,
        )
        self.zip_btn.grid(row=0, column=2, padx=(6, 0), sticky="ew")

    def _build_right(self, parent):
        # ── Stats cards ────────────────────────────────────────────────────
        sec = self._section(parent, "📊  Project Statistics")
        cards_outer = ctk.CTkFrame(sec, fg_color="transparent")
        cards_outer.pack(fill="x", padx=12, pady=(0, 16))
        cards_outer.columnconfigure((0, 1), weight=1)

        self.stat_total_files = StatCard(cards_outer, "Total Files")
        self.stat_total_files.grid(row=0, column=0, padx=4, pady=4, sticky="nsew")
        self.stat_total_dirs = StatCard(cards_outer, "Total Folders")
        self.stat_total_dirs.grid(row=0, column=1, padx=4, pady=4, sticky="nsew")
        self.stat_orig = StatCard(cards_outer, "Original Size")
        self.stat_orig.grid(row=1, column=0, padx=4, pady=4, sticky="nsew")
        self.stat_excl = StatCard(cards_outer, "Excluded Size", color=CLR_WARNING)
        self.stat_excl.grid(row=1, column=1, padx=4, pady=4, sticky="nsew")
        self.stat_transfer = StatCard(cards_outer, "Transfer Size", color=CLR_SUCCESS)
        self.stat_transfer.grid(row=2, column=0, padx=4, pady=4, sticky="nsew")
        self.stat_saved = StatCard(cards_outer, "Space Saved", color=CLR_ACCENT)
        self.stat_saved.grid(row=2, column=1, padx=4, pady=4, sticky="nsew")

        # ── Large Folder Detection ─────────────────────────────────────────
        sec2 = self._section(parent, "📦  Large Folder Detection")
        self.large_frame = ctk.CTkScrollableFrame(sec2, fg_color="transparent",
                                                   height=220,
                                                   scrollbar_button_color=CLR_BORDER)
        self.large_frame.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkLabel(self.large_frame, text="Run Analyze to detect large folders.",
                     text_color=CLR_MUTED).pack(pady=20)

        self.add_selected_btn = ctk.CTkButton(
            sec2, text="➕  Add Checked to Exclusions", height=32,
            fg_color="transparent", border_width=1, border_color=CLR_ACCENT,
            text_color=CLR_ACCENT, hover_color=CLR_BORDER,
            command=self._add_large_to_exclusions,
        )
        self.add_selected_btn.pack(pady=(0, 14))

        # ── Progress ───────────────────────────────────────────────────────
        sec3 = self._section(parent, "⚡  Progress")
        prog_inner = ctk.CTkFrame(sec3, fg_color="transparent")
        prog_inner.pack(fill="x", padx=16, pady=(0, 16))

        self.progress_bar = ctk.CTkProgressBar(prog_inner, height=12,
                                                progress_color=CLR_ACCENT,
                                                fg_color=CLR_BORDER)
        self.progress_bar.pack(fill="x", pady=(0, 8))
        self.progress_bar.set(0)

        self.current_file_lbl = ctk.CTkLabel(prog_inner, text="Idle",
                                              text_color=CLR_MUTED,
                                              font=ctk.CTkFont(size=11),
                                              anchor="w", wraplength=340)
        self.current_file_lbl.pack(fill="x")

        self.progress_count_lbl = ctk.CTkLabel(prog_inner, text="",
                                                text_color=CLR_MUTED,
                                                font=ctk.CTkFont(size=11),
                                                anchor="w")
        self.progress_count_lbl.pack(fill="x")

        self.cancel_btn = ctk.CTkButton(
            prog_inner, text="Cancel", height=32, width=100,
            fg_color=CLR_DANGER, hover_color="#b91c1c",
            command=self._cancel_operation, state="disabled",
        )
        self.cancel_btn.pack(anchor="e", pady=(8, 0))

        # Status bar
        self.status_lbl = ctk.CTkLabel(prog_inner, text="",
                                        font=ctk.CTkFont(size=12, weight="bold"),
                                        text_color=CLR_SUCCESS)
        self.status_lbl.pack(fill="x", pady=(6, 0))

        # ── Large folder check-vars ─────────────────────────────────────────
        self._large_check_vars: list[tuple[ctk.BooleanVar, str]] = []

    # ── Exclusion helpers ──────────────────────────────────────────────────

    def _get_exclusions(self) -> list[str]:
        return list(self.exc_listbox.get(0, tk.END))

    def _exclusion_set(self) -> set[str]:
        return set(self._get_exclusions())

    def _add_exclusion(self, value: str | None = None):
        raw = value or self.exc_entry.get().strip()
        if not raw:
            return
        # Split only on commas or newlines — spaces are valid in file/folder names
        import re
        tokens = [t.strip() for t in re.split(r"[,\n]+", raw) if t.strip()]
        existing = set(self._get_exclusions())
        for token in tokens:
            if token not in existing:
                self.exc_listbox.insert(tk.END, token)
                existing.add(token)
        self._update_exc_count()
        self.exc_entry.delete(0, tk.END)

    def _remove_exclusion(self):
        for idx in reversed(self.exc_listbox.curselection()):
            self.exc_listbox.delete(idx)
        self._update_exc_count()

    def _clear_exclusions(self):
        self.exc_listbox.delete(0, tk.END)
        self._update_exc_count()

    def _update_exc_count(self):
        n = self.exc_listbox.size()
        self.exc_count_lbl.configure(text=f"{n} exclusion{'s' if n != 1 else ''}")

    def _apply_preset(self, name: str):
        all_presets = {**PRESETS, **self.app.cfg.get("custom_presets", {})}
        items = all_presets.get(name, [])
        self._clear_exclusions()
        for item in items:
            self._add_exclusion(item)

    def _save_preset(self):
        items = self._get_exclusions()
        if not items:
            messagebox.showwarning("No Exclusions", "Add at least one exclusion first.")
            return
        dialog = ctk.CTkInputDialog(text="Enter preset name:", title="Save Preset")
        name = dialog.get_input()
        if name:
            self.app.cfg.setdefault("custom_presets", {})[name] = items
            save_config(self.app.cfg)
            messagebox.showinfo("Saved", f"Preset '{name}' saved.")

    # ── Recent projects ─────────────────────────────────────────────────────

    def _get_recent_labels(self) -> list[str]:
        recents = self.app.cfg.get("recent_projects", [])
        return recents[:8] if recents else ["(none)"]

    def _on_recent_select(self, value: str):
        if value and value != "(none)":
            self.src_selector.set(value)

    def _push_recent(self, path: str):
        recents = self.app.cfg.setdefault("recent_projects", [])
        if path in recents:
            recents.remove(path)
        recents.insert(0, path)
        self.app.cfg["recent_projects"] = recents[:10]
        save_config(self.app.cfg)
        self.recent_menu.configure(values=self._get_recent_labels())

    # ── Validation ─────────────────────────────────────────────────────────

    def _validate_source(self) -> Path | None:
        s = self.src_selector.get()
        if not s:
            messagebox.showerror("Missing Source", "Please select a source folder.")
            return None
        p = Path(s)
        if not p.exists() or not p.is_dir():
            messagebox.showerror("Invalid Source", f"Folder not found:\n{s}")
            return None
        return p

    # ── Analyze ────────────────────────────────────────────────────────────

    def _start_analyze(self):
        src = self._validate_source()
        if not src:
            return
        self._push_recent(str(src))
        exclusions = self._exclusion_set()

        self._set_busy(True, "Scanning…")
        self.analyze_btn.configure(state="disabled", text="Scanning…")

        def run():
            scanner = Scanner(src, exclusions)
            self._active_scanner = scanner
            count = [0]

            def prog(n):
                count[0] = n
                self.after(0, lambda: self.status_lbl.configure(
                    text=f"Scanned {n:,} files…", text_color=CLR_MUTED))

            stats, large_dirs, file_list = scanner.scan(progress_cb=prog)
            self._stats = stats
            self._file_list = file_list
            self.after(0, lambda: self._on_analyze_done(stats, large_dirs))

        self._scan_thread = threading.Thread(target=run, daemon=True)
        self._scan_thread.start()

    def _on_analyze_done(self, stats: dict, large_dirs: list):
        self.analyze_btn.configure(state="normal", text="🔍  Analyze")
        self._set_busy(False, "")
        self.status_lbl.configure(text="✓ Analysis complete", text_color=CLR_SUCCESS)

        # Stats
        orig = stats["original_size"]
        excl = stats["excluded_size"]
        tx   = stats["transfer_size"]
        saved_pct = (excl / orig * 100) if orig else 0

        self.stat_total_files.update_value(f"{stats['total_files']:,}")
        self.stat_total_dirs.update_value(f"{stats['total_dirs']:,}")
        self.stat_orig.update_value(format_size(orig))
        self.stat_excl.update_value(format_size(excl), CLR_WARNING)
        self.stat_transfer.update_value(format_size(tx), CLR_SUCCESS)
        self.stat_saved.update_value(f"{saved_pct:.0f}%", CLR_ACCENT)

        # Large folder list
        for w in self.large_frame.winfo_children():
            w.destroy()
        self._large_check_vars.clear()

        if not large_dirs:
            ctk.CTkLabel(self.large_frame, text="No sub-folders found.",
                         text_color=CLR_MUTED).pack(pady=10)
            return

        hdr = ctk.CTkFrame(self.large_frame, fg_color=CLR_BORDER, corner_radius=6)
        hdr.pack(fill="x", pady=(0, 4))
        for col, w in [("Folder", 160), ("Size", 80), ("Files", 70)]:
            ctk.CTkLabel(hdr, text=col, width=w, anchor="w",
                         font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=CLR_MUTED).pack(side="left", padx=6, pady=4)

        for name, size, count in large_dirs:
            excluded = name in self._exclusion_set()
            row = ctk.CTkFrame(self.large_frame, fg_color="transparent")
            row.pack(fill="x", pady=1)
            var = ctk.BooleanVar(value=excluded)
            self._large_check_vars.append((var, name))
            clr = CLR_WARNING if excluded else CLR_TEXT
            ctk.CTkCheckBox(row, text="", variable=var, width=24,
                            checkbox_height=16, checkbox_width=16,
                            fg_color=CLR_ACCENT).pack(side="left", padx=4)
            ctk.CTkLabel(row, text=name, width=148, anchor="w",
                         font=ctk.CTkFont(size=12, family="Consolas"),
                         text_color=clr).pack(side="left")
            ctk.CTkLabel(row, text=format_size(size), width=80, anchor="w",
                         font=ctk.CTkFont(size=12),
                         text_color=CLR_WARNING if size > 500_000_000 else CLR_TEXT
                         ).pack(side="left")
            ctk.CTkLabel(row, text=f"{count:,}", width=70, anchor="w",
                         font=ctk.CTkFont(size=12),
                         text_color=CLR_MUTED).pack(side="left")

    def _add_large_to_exclusions(self):
        for var, name in self._large_check_vars:
            if var.get():
                self._add_exclusion(name)

    # ── Copy / ZIP ─────────────────────────────────────────────────────────

    def _start_copy(self):
        src = self._validate_source()
        if not src:
            return
        dst_str = self.dst_folder.get()
        if not dst_str:
            messagebox.showerror("Missing Destination", "Please select a destination folder.")
            return
        dst = Path(dst_str)
        if dst == src:
            messagebox.showerror("Invalid Destination", "Source and destination must differ.")
            return

        if not self._file_list:
            messagebox.showinfo("Analyze First",
                                "Click Analyze before copying to calculate what will be transferred.")
            return

        dst.mkdir(parents=True, exist_ok=True)
        self._run_transfer(src, dst, "copy")

    def _start_zip(self):
        src = self._validate_source()
        if not src:
            return
        zip_str = self.dst_zip.get()
        if not zip_str:
            messagebox.showerror("Missing ZIP Path", "Please select a destination ZIP path.")
            return

        if not self._file_list:
            messagebox.showinfo("Analyze First",
                                "Click Analyze before creating ZIP to calculate what will be transferred.")
            return

        self._run_transfer(src, Path(zip_str), "zip")

    def _run_transfer(self, src: Path, dest: Path, mode: str):
        self._push_recent(str(src))
        exclusions = self._exclusion_set()
        file_list = self._file_list
        total_size = self._stats.get("transfer_size", 1) or 1

        self._msg_queue = queue.Queue()
        engine = TransferEngine(src, exclusions, self._msg_queue)
        self._engine = engine

        self._set_busy(True, "Preparing…")
        self.progress_bar.set(0)
        self.status_lbl.configure(text="", text_color=CLR_TEXT)
        self.current_file_lbl.configure(text="Starting…")

        def run():
            if mode == "copy":
                engine.copy_folder(dest, file_list, total_size)
            else:
                engine.create_zip(dest, file_list, total_size)

        self._transfer_thread = threading.Thread(target=run, daemon=True)
        self._transfer_thread.start()
        self._poll_queue(mode, src, dest, file_list)

    def _poll_queue(self, mode, src, dest, file_list):
        try:
            while True:
                msg = self._msg_queue.get_nowait()
                mtype = msg.get("type")

                if mtype == "progress":
                    self.progress_bar.set(msg["pct"])
                    self.current_file_lbl.configure(text=msg.get("current", ""))
                    self.progress_count_lbl.configure(
                        text=f"{msg['done']:,} / {msg['total']:,} files")

                elif mtype == "warning":
                    pass  # Log silently

                elif mtype == "error":
                    self._set_busy(False, "")
                    messagebox.showerror("Transfer Error", msg["msg"])
                    return

                elif mtype == "cancelled":
                    self._set_busy(False, "")
                    self.status_lbl.configure(text="⚠ Cancelled", text_color=CLR_WARNING)
                    self.progress_bar.set(0)
                    return

                elif mtype == "done":
                    self.progress_bar.set(1)
                    self._set_busy(False, "")
                    duration = msg.get("duration", 0)
                    verb = "archived" if mode == "zip" else "copied"
                    self.status_lbl.configure(
                        text=f"✓ Done — {msg['copied']:,} files {verb} in {format_duration(duration)}",
                        text_color=CLR_SUCCESS,
                    )
                    # Store report data
                    self.app.last_report = {
                        "source": str(src),
                        "destination": str(dest),
                        "mode": mode,
                        "datetime": datetime.now().isoformat(sep=" ", timespec="seconds"),
                        "files_processed": msg["copied"],
                        "files_skipped": len(file_list) - msg["copied"],
                        "errors": msg.get("errors", 0),
                        "original_size": self._stats.get("original_size", 0),
                        "transfer_size": msg["size"],
                        "zip_size": msg.get("zip_size"),
                        "excluded_size": self._stats.get("excluded_size", 0),
                        "duration": duration,
                    }
                    return

        except queue.Empty:
            pass

        if self._transfer_thread and self._transfer_thread.is_alive():
            self.after(80, lambda: self._poll_queue(mode, src, dest, file_list))

    def _cancel_operation(self):
        if self._engine:
            self._engine.cancel()
        if hasattr(self, "_active_scanner"):
            self._active_scanner.cancel()

    def _set_busy(self, busy: bool, text: str):
        state = "normal" if not busy else "disabled"
        self.copy_btn.configure(state=state)
        self.zip_btn.configure(state=state)
        self.analyze_btn.configure(state=state)
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        if text:
            self.status_lbl.configure(text=text, text_color=CLR_MUTED)


class ReportPage(BasePage):
    """Shows the last transfer report and allows export."""

    def __init__(self, master, app, **kwargs):
        super().__init__(master, app, **kwargs)
        self._build_ui()

    def _build_ui(self):
        ctk.CTkLabel(self, text="Transfer Report",
                     font=ctk.CTkFont(size=26, weight="bold"),
                     text_color=CLR_TEXT).pack(anchor="w", pady=(0, 20))

        self.report_box = ctk.CTkTextbox(
            self, fg_color=CLR_SURFACE, border_color=CLR_BORDER,
            border_width=1, corner_radius=12,
            font=ctk.CTkFont(family="Consolas", size=13),
            text_color=CLR_TEXT,
        )
        self.report_box.pack(fill="both", expand=True)
        self.report_box.insert("0.0", "No transfer report available yet.\nRun a Copy or ZIP operation first.")
        self.report_box.configure(state="disabled")

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", pady=(12, 0))
        ctk.CTkButton(btn_row, text="📥  Export as TXT", width=160, height=38,
                      fg_color=CLR_ACCENT, hover_color=CLR_ACCENT2,
                      command=self._export).pack(side="left")
        ctk.CTkButton(btn_row, text="Refresh", width=90, height=38,
                      fg_color=CLR_BORDER, hover_color="#343750",
                      command=self.on_show).pack(side="left", padx=8)

    def on_show(self):
        report = self.app.last_report
        if not report:
            return
        saved = report["original_size"] - report["transfer_size"]
        saved_pct = (saved / report["original_size"] * 100) if report["original_size"] else 0
        mode_label = "ZIP Archive" if report["mode"] == "zip" else "Folder Copy"
        lines = [
            "═" * 52,
            f"  Smart Transfer Report  —  {mode_label}",
            "═" * 52,
            f"  Date / Time   : {report['datetime']}",
            f"  Source        : {report['source']}",
            f"  Destination   : {report['destination']}",
            "─" * 52,
            f"  Files copied  : {report['files_processed']:,}",
            f"  Files skipped : {report['files_skipped']:,}",
            f"  Errors        : {report.get('errors', 0):,}",
            "─" * 52,
            f"  Original size : {format_size(report['original_size'])}",
            f"  Excluded size : {format_size(report['excluded_size'])}",
            f"  Transfer size : {format_size(report['transfer_size'])}",
        ]
        if report.get("zip_size"):
            lines.append(f"  ZIP file size : {format_size(report['zip_size'])}")
        lines += [
            f"  Space saved   : {saved_pct:.1f}%",
            f"  Duration      : {format_duration(report['duration'])}",
            "═" * 52,
        ]
        text = "\n".join(lines)
        self.report_box.configure(state="normal")
        self.report_box.delete("0.0", tk.END)
        self.report_box.insert("0.0", text)
        self.report_box.configure(state="disabled")
        self._cached_text = text

    def _export(self):
        if not hasattr(self, "_cached_text"):
            messagebox.showinfo("No Report", "No report to export yet.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text file", "*.txt")],
            initialfile="smart_transfer_report.txt",
        )
        if path:
            try:
                Path(path).write_text(self._cached_text, encoding="utf-8")
                messagebox.showinfo("Exported", f"Report saved to:\n{path}")
            except Exception as e:
                messagebox.showerror("Export Failed", str(e))


class PresetsPage(BasePage):
    """View and manage built-in and custom presets."""

    def __init__(self, master, app, **kwargs):
        super().__init__(master, app, **kwargs)
        self._build_ui()

    def _build_ui(self):
        ctk.CTkLabel(self, text="Presets",
                     font=ctk.CTkFont(size=26, weight="bold"),
                     text_color=CLR_TEXT).pack(anchor="w", pady=(0, 6))
        ctk.CTkLabel(self, text="Built-in and custom exclusion presets.",
                     text_color=CLR_MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", pady=(0, 18))

        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True)
        self._render_presets()

    def _render_presets(self):
        for w in self.scroll.winfo_children():
            w.destroy()

        all_p = {**PRESETS, **self.app.cfg.get("custom_presets", {})}
        custom_keys = set(self.app.cfg.get("custom_presets", {}).keys())

        for name, items in all_p.items():
            is_custom = name in custom_keys
            card = ctk.CTkFrame(self.scroll, fg_color=CLR_SURFACE, corner_radius=10,
                                border_width=1, border_color=CLR_BORDER)
            card.pack(fill="x", pady=6)

            hdr = ctk.CTkFrame(card, fg_color="transparent")
            hdr.pack(fill="x", padx=14, pady=10)
            ctk.CTkLabel(hdr, text=name,
                         font=ctk.CTkFont(size=14, weight="bold"),
                         text_color=CLR_TEXT).pack(side="left")
            if is_custom:
                ctk.CTkLabel(hdr, text="custom", fg_color=CLR_ACCENT2,
                             corner_radius=4, text_color="#fff",
                             font=ctk.CTkFont(size=10),
                             padx=6, pady=2).pack(side="left", padx=8)
                ctk.CTkButton(hdr, text="Delete", width=70, height=26,
                              fg_color="transparent", border_width=1,
                              border_color=CLR_DANGER, text_color=CLR_DANGER,
                              hover_color=CLR_BORDER,
                              command=lambda n=name: self._delete_preset(n)
                              ).pack(side="right")

            body = ctk.CTkFrame(card, fg_color="transparent")
            body.pack(fill="x", padx=14, pady=(0, 10))
            text = "  ·  ".join(items)
            ctk.CTkLabel(body, text=text, text_color=CLR_MUTED,
                         font=ctk.CTkFont(size=11, family="Consolas"),
                         anchor="w", wraplength=520).pack(fill="x")

    def _delete_preset(self, name: str):
        if messagebox.askyesno("Delete Preset", f"Delete preset '{name}'?"):
            self.app.cfg.setdefault("custom_presets", {}).pop(name, None)
            save_config(self.app.cfg)
            self._render_presets()

    def on_show(self):
        self._render_presets()


class AboutPage(BasePage):
    def __init__(self, master, app, **kwargs):
        super().__init__(master, app, **kwargs)
        ctk.CTkLabel(self, text="Smart Transfer",
                     font=ctk.CTkFont(size=30, weight="bold"),
                     text_color=CLR_TEXT).pack(pady=(40, 4))
        ctk.CTkLabel(self, text=f"Version {VERSION}",
                     text_color=CLR_MUTED, font=ctk.CTkFont(size=14)).pack()
        ctk.CTkLabel(self,
                     text="Copy folders and create ZIPs with smart exclusions.\n"
                          "Built for developers who need to transfer large projects\n"
                          "without dragging along gigabytes of build artefacts.",
                     text_color=CLR_TEXT, font=ctk.CTkFont(size=13),
                     justify="center").pack(pady=24)

        info = [
            ("Engine", "Python 3.10+, CustomTkinter"),
            ("I/O", "shutil · zipfile · pathlib"),
            ("Threading", "Fully non-blocking UI"),
        ]
        for k, v in info:
            row = ctk.CTkFrame(self, fg_color="transparent")
            row.pack()
            ctk.CTkLabel(row, text=f"{k}:", width=90, anchor="e",
                         text_color=CLR_MUTED, font=ctk.CTkFont(size=12)).pack(side="left")
            ctk.CTkLabel(row, text=v, anchor="w",
                         text_color=CLR_TEXT, font=ctk.CTkFont(size=12)).pack(side="left", padx=8)


# ─── Main Application Window ──────────────────────────────────────────────────

class SmartTransferApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1100x740")
        self.minsize(900, 620)
        self.configure(fg_color=CLR_BG)

        self.cfg = load_config()
        self.last_report: dict | None = None

        self._build_layout()
        self._show_page("transfer")

    def _build_layout(self):
        # Sidebar
        sidebar = ctk.CTkFrame(self, fg_color=CLR_SIDEBAR, width=200, corner_radius=0)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        # Logo
        logo = ctk.CTkFrame(sidebar, fg_color="transparent")
        logo.pack(fill="x", padx=14, pady=(22, 28))
        ctk.CTkLabel(logo, text="⇄",
                     font=ctk.CTkFont(size=28, weight="bold"),
                     text_color=CLR_ACCENT).pack(side="left")
        ctk.CTkLabel(logo, text=" Smart\nTransfer",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=CLR_TEXT, justify="left").pack(side="left", padx=4)

        nav_items = [
            ("Transfer", "transfer", "🔄"),
            ("Presets", "presets", "🗂"),
            ("Report", "report", "📄"),
            ("About", "about", "ℹ"),
        ]
        self._nav_buttons: dict[str, SidebarButton] = {}
        for label, key, icon in nav_items:
            btn = SidebarButton(sidebar, label, icon,
                                command=lambda k=key: self._show_page(k))
            btn.pack(fill="x", padx=8, pady=2)
            self._nav_buttons[key] = btn

        # Separator + theme toggle
        ctk.CTkFrame(sidebar, height=1, fg_color=CLR_BORDER).pack(
            fill="x", padx=14, pady=(16, 10))
        ctk.CTkLabel(sidebar, text=f"v{VERSION}", text_color=CLR_MUTED,
                     font=ctk.CTkFont(size=10)).pack(pady=(0, 4))

        # Content area
        self._content = ctk.CTkFrame(self, fg_color=CLR_BG, corner_radius=0)
        self._content.pack(side="left", fill="both", expand=True, padx=24, pady=24)

        # Build pages
        self._pages: dict[str, BasePage] = {
            "transfer": TransferPage(self._content, self),
            "presets": PresetsPage(self._content, self),
            "report": ReportPage(self._content, self),
            "about": AboutPage(self._content, self),
        }

    def _show_page(self, key: str):
        for k, p in self._pages.items():
            p.pack_forget()
        for k, btn in self._nav_buttons.items():
            btn.set_active(k == key)
        page = self._pages[key]
        page.pack(fill="both", expand=True)
        page.on_show()


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    app = SmartTransferApp()
    app.mainloop()


if __name__ == "__main__":
    main()
