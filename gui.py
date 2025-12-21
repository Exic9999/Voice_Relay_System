#!/usr/bin/env python3
"""
===================================================================================
VOICE RELAY SYSTEM - GUI
===================================================================================
Features:
    - Dark theme interface
    - Bot permission testing
    - Real-time speech transcription display
    - Adjustable squad uplink timeout
    - Start/Stop bots with one click
===================================================================================
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import json
import sys
import threading
import asyncio
import queue
import logging
from pathlib import Path
from datetime import datetime

# ===================================================================================
# CONFIGURATION
# ===================================================================================

PROJECT_DIR = Path(__file__).parent
CONFIG_FILE = PROJECT_DIR / "config.json"

DEFAULT_CONFIG = {
    "commander_token": "",
    "drone_alpha_token": "",
    "drone_bravo_token": "",
    "drone_alpha_channel_id": "",
    "drone_bravo_channel_id": "",
    "command_prefix": "!",
    "max_buffer_frames": 100,
    "squad_uplink_timeout": 1.0,
    "whisper_model": "base",
    "log_level": "DEBUG"
}

WHISPER_MODELS = {
    "tiny": "Tiny (~75MB) - Fastest, lower accuracy",
    "base": "Base (~142MB) - Fast, fair accuracy (default)",
    "small": "Small (~466MB) - Moderate speed, good accuracy",
    "medium": "Medium (~1.5GB) - Slower, better accuracy",
    "large": "Large (~3GB) - Slowest, best accuracy"
}

# ===================================================================================
# DARK THEME COLORS
# ===================================================================================

COLORS = {
    'bg_dark': '#1a1a2e',
    'bg_medium': '#16213e',
    'bg_light': '#0f3460',
    'bg_input': '#252550',
    'fg_primary': '#e0e0e0',
    'fg_secondary': '#a0a0a0',
    'fg_dim': '#606080',
    'accent': '#00d4ff',
    'success': '#00ff88',
    'warning': '#ffaa00',
    'error': '#ff4757',
    'info': '#4fc3f7',
    'debug': '#81c784',
    'border': '#2a2a4a',
    'test': '#ff79c6',
    'transcript_commander': '#ffd700',
    'transcript_squad': '#87ceeb'
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                for key, value in DEFAULT_CONFIG.items():
                    if key not in config:
                        config[key] = value
                return config
        except Exception as e:
            print(f"ERROR loading config: {e}", file=sys.stderr)
    return DEFAULT_CONFIG.copy()


def save_config(config: dict):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)


# ===================================================================================
# LOGGING HANDLER
# ===================================================================================

class QueueHandler(logging.Handler):
    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue
    
    def emit(self, record):
        self.log_queue.put(("log", self.format(record)))


# ===================================================================================
# BOT TESTER
# ===================================================================================

async def test_single_bot(token: str, bot_name: str, channel_id: str = None, log_func=None) -> dict:
    import discord
    
    result = {
        "name": bot_name,
        "token_valid": False,
        "bot_user": None,
        "bot_id": None,
        "guilds": [],
        "channel_found": False,
        "channel_name": None,
        "channel_permissions": {},
        "errors": []
    }
    
    if log_func:
        log_func(f"   Connecting {bot_name}...", 'DEBUG')
    
    intents = discord.Intents.all()
    intents.guilds = True
    intents.members = True
    
    client = discord.Client(intents=intents)
    ready_event = asyncio.Event()
    
    @client.event
    async def on_ready():
        result["token_valid"] = True
        result["bot_user"] = str(client.user)
        result["bot_id"] = client.user.id
        
        for guild in client.guilds:
            result["guilds"].append({"name": guild.name, "id": guild.id})
            
            if channel_id:
                try:
                    channel = guild.get_channel(int(channel_id))
                    if channel:
                        result["channel_found"] = True
                        result["channel_name"] = channel.name
                        perms = channel.permissions_for(guild.me)
                        result["channel_permissions"] = {
                            "connect": perms.connect,
                            "speak": perms.speak,
                            "use_voice_activation": perms.use_voice_activation,
                            "view_channel": perms.view_channel
                        }
                except Exception as e:
                    print(f"ERROR checking channel: {e}", file=sys.stderr)

        ready_event.set()
    
    try:
        task = asyncio.create_task(client.start(token))
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            if not result["token_valid"]:
                result["errors"].append("Connection timeout")
    except discord.LoginFailure:
        result["errors"].append("Invalid token")
    except Exception as e:
        result["errors"].append(str(e))
    finally:
        try:
            await client.close()
        except Exception as e:
            print(f"ERROR closing client: {e}", file=sys.stderr)
    
    return result


async def test_all_bots(config: dict, log_func) -> dict:
    results = {"mothership": None, "alpha": None, "bravo": None, "overall_success": False}
    
    log_func("Testing MOTHERSHIP...", 'TEST')
    results["mothership"] = await test_single_bot(config["commander_token"], "Mothership", None, log_func)
    
    log_func("Testing ALPHA DRONE...", 'TEST')
    results["alpha"] = await test_single_bot(config["drone_alpha_token"], "Alpha", config["drone_alpha_channel_id"], log_func)
    
    log_func("Testing BRAVO DRONE...", 'TEST')
    results["bravo"] = await test_single_bot(config["drone_bravo_token"], "Bravo", config["drone_bravo_channel_id"], log_func)
    
    results["overall_success"] = (
        results["mothership"]["token_valid"] and
        results["alpha"]["token_valid"] and
        results["bravo"]["token_valid"] and
        results["alpha"]["channel_found"] and
        results["bravo"]["channel_found"]
    )
    
    return results


# ===================================================================================
# MAIN GUI
# ===================================================================================

class VoiceRelayGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Voice Relay System")
        self.root.geometry("1000x900")
        self.root.minsize(900, 800)
        self.root.configure(bg=COLORS['bg_dark'])
        
        self.config = load_config()
        self.bot_thread = None
        self.bot_running = False
        self.testing = False
        self.stop_event = threading.Event()
        self.log_queue = queue.Queue()
        
        self._create_menu()
        self._create_main_layout()
        self._poll_log_queue()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._show_startup_info()
        
        # Auto-start bots after a short delay
        self.root.after(1000, self._auto_start_bots)
    
    def _create_menu(self):
        menubar = tk.Menu(self.root, bg=COLORS['bg_medium'], fg=COLORS['fg_primary'],
                         activebackground=COLORS['accent'], activeforeground=COLORS['bg_dark'])
        self.root.config(menu=menubar)
        
        file_menu = tk.Menu(menubar, tearoff=0, bg=COLORS['bg_medium'], fg=COLORS['fg_primary'])
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Save Config", command=self._save_config, accelerator="Ctrl+S")
        file_menu.add_command(label="Reload Config", command=self._reload_config)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        
        tools_menu = tk.Menu(menubar, tearoff=0, bg=COLORS['bg_medium'], fg=COLORS['fg_primary'])
        menubar.add_cascade(label="Tools", menu=tools_menu)
        tools_menu.add_command(label="Test All Bots", command=self._test_bots, accelerator="Ctrl+T")
        tools_menu.add_separator()
        tools_menu.add_command(label="Clear Log", command=self._clear_log)
        
        help_menu = tk.Menu(menubar, tearoff=0, bg=COLORS['bg_medium'], fg=COLORS['fg_primary'])
        menubar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="Setup Instructions", command=self._show_setup_instructions)
        help_menu.add_command(label="About", command=self._show_about)
        
        self.root.bind('<Control-s>', lambda e: self._save_config())
        self.root.bind('<Control-t>', lambda e: self._test_bots())
    
    def _create_main_layout(self):
        main_frame = tk.Frame(self.root, bg=COLORS['bg_dark'], padx=15, pady=15)
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Title
        title_frame = tk.Frame(main_frame, bg=COLORS['bg_dark'])
        title_frame.pack(fill=tk.X, pady=(0, 15))
        
        tk.Label(title_frame, text="⚔ VOICE RELAY SYSTEM",
                font=('Segoe UI', 18, 'bold'),
                bg=COLORS['bg_dark'], fg=COLORS['accent']).pack(side=tk.LEFT)
        
        # Status
        self.status_frame = tk.Frame(title_frame, bg=COLORS['bg_dark'])
        self.status_frame.pack(side=tk.RIGHT)
        
        self.status_dot = tk.Label(self.status_frame, text="●", font=('Segoe UI', 16),
                                   bg=COLORS['bg_dark'], fg=COLORS['error'])
        self.status_dot.pack(side=tk.LEFT)
        
        self.status_label = tk.Label(self.status_frame, text="OFFLINE",
                                    font=('Segoe UI', 10, 'bold'),
                                    bg=COLORS['bg_dark'], fg=COLORS['fg_secondary'])
        self.status_label.pack(side=tk.LEFT, padx=(5, 0))
        
        # Config section
        config_frame = tk.LabelFrame(main_frame, text=" CONFIGURATION ",
                                    font=('Segoe UI', 11, 'bold'),
                                    bg=COLORS['bg_dark'], fg=COLORS['accent'],
                                    bd=1, relief=tk.SOLID)
        config_frame.pack(fill=tk.X, pady=(0, 10))
        
        config_inner = tk.Frame(config_frame, bg=COLORS['bg_dark'], padx=15, pady=15)
        config_inner.pack(fill=tk.X)
        self._create_config_section(config_inner)
        
        # Controls
        control_frame = tk.Frame(main_frame, bg=COLORS['bg_dark'])
        control_frame.pack(fill=tk.X, pady=(0, 10))
        self._create_control_section(control_frame)
        
        # Log section
        log_frame = tk.LabelFrame(main_frame, text=" LOG OUTPUT & TRANSCRIPTIONS ",
                                 font=('Segoe UI', 11, 'bold'),
                                 bg=COLORS['bg_dark'], fg=COLORS['accent'],
                                 bd=1, relief=tk.SOLID)
        log_frame.pack(fill=tk.BOTH, expand=True)
        
        log_inner = tk.Frame(log_frame, bg=COLORS['bg_dark'], padx=10, pady=10)
        log_inner.pack(fill=tk.BOTH, expand=True)
        self._create_log_section(log_inner)
    
    def _create_config_section(self, parent):
        # Tokens
        tk.Label(parent, text="BOT TOKENS", font=('Segoe UI', 10, 'bold'),
                bg=COLORS['bg_dark'], fg=COLORS['fg_primary']).pack(anchor=tk.W)
        
        self._create_token_row(parent, "Mothership:", "commander_token")
        self._create_token_row(parent, "Alpha Drone:", "drone_alpha_token")
        self._create_token_row(parent, "Bravo Drone:", "drone_bravo_token")
        
        tk.Frame(parent, height=2, bg=COLORS['border']).pack(fill=tk.X, pady=10)
        
        # IDs
        tk.Label(parent, text="DISCORD CHANNEL IDs", font=('Segoe UI', 10, 'bold'),
                bg=COLORS['bg_dark'], fg=COLORS['fg_primary']).pack(anchor=tk.W)
        
        self._create_id_row(parent, "Alpha Channel:", "drone_alpha_channel_id")
        self._create_id_row(parent, "Bravo Channel:", "drone_bravo_channel_id")
        
        tk.Frame(parent, height=2, bg=COLORS['border']).pack(fill=tk.X, pady=10)
        
        # Squad Uplink Timeout
        timeout_frame = tk.Frame(parent, bg=COLORS['bg_dark'])
        timeout_frame.pack(fill=tk.X, pady=5)
        
        tk.Label(timeout_frame, text="SQUAD UPLINK TIMEOUT", font=('Segoe UI', 10, 'bold'),
                bg=COLORS['bg_dark'], fg=COLORS['fg_primary']).pack(anchor=tk.W)
        
        tk.Label(timeout_frame, text="Auto-end squad message after this much silence:",
                font=('Segoe UI', 9), bg=COLORS['bg_dark'], fg=COLORS['fg_dim']).pack(anchor=tk.W)
        
        slider_frame = tk.Frame(timeout_frame, bg=COLORS['bg_dark'])
        slider_frame.pack(fill=tk.X, pady=5)
        
        self.timeout_var = tk.DoubleVar(value=self.config.get("squad_uplink_timeout", 1.0))
        
        self.timeout_slider = tk.Scale(
            slider_frame,
            from_=0.5, to=5.0,
            resolution=0.1,
            orient=tk.HORIZONTAL,
            variable=self.timeout_var,
            length=300,
            bg=COLORS['bg_dark'],
            fg=COLORS['fg_primary'],
            troughcolor=COLORS['bg_input'],
            highlightthickness=0,
            activebackground=COLORS['accent'],
            command=self._on_timeout_change
        )
        self.timeout_slider.pack(side=tk.LEFT)
        
        self.timeout_label = tk.Label(slider_frame, text=f"{self.timeout_var.get():.1f} seconds",
                                     font=('Segoe UI', 10, 'bold'),
                                     bg=COLORS['bg_dark'], fg=COLORS['accent'])
        self.timeout_label.pack(side=tk.LEFT, padx=15)

        tk.Frame(parent, height=2, bg=COLORS['border']).pack(fill=tk.X, pady=10)

        # Whisper Model Selection
        whisper_frame = tk.Frame(parent, bg=COLORS['bg_dark'])
        whisper_frame.pack(fill=tk.X, pady=5)

        tk.Label(whisper_frame, text="WHISPER MODEL", font=('Segoe UI', 10, 'bold'),
                bg=COLORS['bg_dark'], fg=COLORS['fg_primary']).pack(anchor=tk.W)

        tk.Label(whisper_frame, text="Speech recognition model (larger = more accurate, slower):",
                font=('Segoe UI', 9), bg=COLORS['bg_dark'], fg=COLORS['fg_dim']).pack(anchor=tk.W)

        model_select_frame = tk.Frame(whisper_frame, bg=COLORS['bg_dark'])
        model_select_frame.pack(fill=tk.X, pady=5)

        self.whisper_model_var = tk.StringVar(value=self.config.get("whisper_model", "base"))

        self.whisper_combo = ttk.Combobox(
            model_select_frame,
            textvariable=self.whisper_model_var,
            values=list(WHISPER_MODELS.keys()),
            state="readonly",
            width=10,
            font=('Segoe UI', 10)
        )
        self.whisper_combo.pack(side=tk.LEFT)

        self.whisper_download_btn = tk.Button(
            model_select_frame,
            text="⬇ Download",
            font=('Segoe UI', 9),
            bg=COLORS['bg_light'],
            fg=COLORS['fg_primary'],
            relief=tk.FLAT,
            cursor='hand2',
            command=self._download_whisper_model
        )
        self.whisper_download_btn.pack(side=tk.LEFT, padx=5)

        self.whisper_desc_label = tk.Label(
            model_select_frame,
            text=WHISPER_MODELS.get(self.whisper_model_var.get(), ""),
            font=('Segoe UI', 9),
            bg=COLORS['bg_dark'],
            fg=COLORS['fg_secondary']
        )
        self.whisper_desc_label.pack(side=tk.LEFT, padx=10)

        # Progress bar for model download
        self.whisper_progress_frame = tk.Frame(whisper_frame, bg=COLORS['bg_dark'])
        self.whisper_progress_frame.pack(fill=tk.X, pady=5)

        self.whisper_progress = ttk.Progressbar(
            self.whisper_progress_frame,
            mode='determinate',
            length=400
        )

        self.whisper_progress_label = tk.Label(
            self.whisper_progress_frame,
            text="",
            font=('Segoe UI', 9),
            bg=COLORS['bg_dark'],
            fg=COLORS['accent']
        )

        self.whisper_combo.bind("<<ComboboxSelected>>", self._on_whisper_model_change)

    def _on_timeout_change(self, value):
        self.timeout_label.config(text=f"{float(value):.1f} seconds")

    def _on_whisper_model_change(self, event):
        model = self.whisper_model_var.get()
        self.whisper_desc_label.config(text=WHISPER_MODELS.get(model, ""))

    def _download_whisper_model(self):
        """Download the selected Whisper model with progress."""
        model = self.whisper_model_var.get()
        self.whisper_download_btn.config(state=tk.DISABLED, text="Downloading...")
        self.whisper_progress.pack(side=tk.LEFT)
        self.whisper_progress_label.pack(side=tk.LEFT, padx=10)
        self.whisper_progress['value'] = 0
        self.whisper_progress_label.config(text="Starting download...")

        threading.Thread(target=self._do_download_model, args=(model,), daemon=True).start()

    def _do_download_model(self, model_name):
        """Background thread to download whisper model."""
        try:
            import whisper
            import urllib.request
            import os

            # Model URLs from whisper
            model_urls = {
                "tiny": "https://openaipublic.azureedge.net/main/whisper/models/65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9/tiny.pt",
                "base": "https://openaipublic.azureedge.net/main/whisper/models/ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e/base.pt",
                "small": "https://openaipublic.azureedge.net/main/whisper/models/9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794/small.pt",
                "medium": "https://openaipublic.azureedge.net/main/whisper/models/345ae4da62f9b3d59415adc60127b97c714f32e89e936602e85993674d08dcb1/medium.pt",
                "large": "https://openaipublic.azureedge.net/main/whisper/models/e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb/large-v3.pt",
            }

            url = model_urls.get(model_name)
            if not url:
                self.root.after(0, lambda: self._download_complete(False, "Unknown model"))
                return

            # Check if already downloaded
            cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "whisper")
            os.makedirs(cache_dir, exist_ok=True)
            model_file = os.path.join(cache_dir, os.path.basename(url))

            if os.path.exists(model_file):
                self.root.after(0, lambda: self._update_progress(100, "Model already downloaded!"))
                self.root.after(1000, lambda: self._download_complete(True, "Already exists"))
                return

            # Download with progress
            def progress_hook(block_num, block_size, total_size):
                if total_size > 0:
                    percent = min(100, (block_num * block_size * 100) // total_size)
                    downloaded = block_num * block_size
                    total_mb = total_size / (1024 * 1024)
                    downloaded_mb = downloaded / (1024 * 1024)
                    self.root.after(0, lambda p=percent, d=downloaded_mb, t=total_mb:
                        self._update_progress(p, f"{d:.1f} / {t:.1f} MB ({p}%)"))

            self.root.after(0, lambda: self._update_progress(0, "Connecting..."))
            urllib.request.urlretrieve(url, model_file, progress_hook)

            # Verify by loading
            self.root.after(0, lambda: self._update_progress(100, "Verifying model..."))
            whisper.load_model(model_name)

            self.root.after(0, lambda: self._download_complete(True, "Download complete!"))

        except Exception as e:
            self.root.after(0, lambda: self._download_complete(False, str(e)))

    def _update_progress(self, percent, text):
        self.whisper_progress['value'] = percent
        self.whisper_progress_label.config(text=text)

    def _download_complete(self, success, message):
        self.whisper_download_btn.config(state=tk.NORMAL, text="⬇ Download")
        if success:
            self.whisper_progress_label.config(text=f"✓ {message}", fg=COLORS['success'])
            self._log(f"Whisper model '{self.whisper_model_var.get()}' ready", 'SUCCESS')
        else:
            self.whisper_progress_label.config(text=f"✗ {message}", fg=COLORS['error'])
            self._log(f"Download failed: {message}", 'ERROR')
        # Hide progress bar after 3 seconds
        self.root.after(3000, self._hide_progress)

    def _hide_progress(self):
        self.whisper_progress.pack_forget()
        self.whisper_progress_label.pack_forget()

    def _create_token_row(self, parent, label_text, config_key):
        row = tk.Frame(parent, bg=COLORS['bg_dark'])
        row.pack(fill=tk.X, pady=2)
        
        tk.Label(row, text=label_text, width=12, anchor=tk.W, font=('Segoe UI', 10),
                bg=COLORS['bg_dark'], fg=COLORS['fg_primary']).pack(side=tk.LEFT)
        
        var = tk.StringVar(value=self.config.get(config_key, ""))
        setattr(self, f"{config_key}_var", var)
        
        entry = tk.Entry(row, textvariable=var, show="●", font=('Consolas', 10),
                        bg=COLORS['bg_input'], fg=COLORS['fg_primary'],
                        insertbackground=COLORS['fg_primary'], relief=tk.FLAT,
                        highlightthickness=1, highlightbackground=COLORS['border'],
                        highlightcolor=COLORS['accent'])
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5, ipady=4)
        setattr(self, f"{config_key}_entry", entry)
        
        tk.Button(row, text="Show", width=5, font=('Segoe UI', 9),
                 bg=COLORS['bg_light'], fg=COLORS['fg_primary'],
                 relief=tk.FLAT, cursor='hand2',
                 command=lambda e=entry: self._toggle_show(e)).pack(side=tk.LEFT)
    
    def _create_id_row(self, parent, label_text, config_key):
        row = tk.Frame(parent, bg=COLORS['bg_dark'])
        row.pack(fill=tk.X, pady=2)
        
        tk.Label(row, text=label_text, width=12, anchor=tk.W, font=('Segoe UI', 10),
                bg=COLORS['bg_dark'], fg=COLORS['fg_primary']).pack(side=tk.LEFT)
        
        var = tk.StringVar(value=self.config.get(config_key, ""))
        setattr(self, f"{config_key}_var", var)
        
        entry = tk.Entry(row, textvariable=var, width=25, font=('Consolas', 10),
                        bg=COLORS['bg_input'], fg=COLORS['fg_primary'],
                        insertbackground=COLORS['fg_primary'], relief=tk.FLAT,
                        highlightthickness=1, highlightbackground=COLORS['border'],
                        highlightcolor=COLORS['accent'])
        entry.pack(side=tk.LEFT, padx=5, ipady=4)
    
    def _create_control_section(self, parent):
        btn_frame = tk.Frame(parent, bg=COLORS['bg_dark'])
        btn_frame.pack(side=tk.LEFT)
        
        self.test_btn = tk.Button(btn_frame, text="🔍 TEST",
                                 font=('Segoe UI', 11, 'bold'),
                                 bg=COLORS['test'], fg=COLORS['bg_dark'],
                                 relief=tk.FLAT, cursor='hand2', width=8,
                                 command=self._test_bots)
        self.test_btn.pack(side=tk.LEFT, padx=(0, 10), ipady=5)
        
        self.start_btn = tk.Button(btn_frame, text="▶ START",
                                  font=('Segoe UI', 11, 'bold'),
                                  bg=COLORS['success'], fg=COLORS['bg_dark'],
                                  relief=tk.FLAT, cursor='hand2', width=8,
                                  command=self._start_bots)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 10), ipady=5)
        
        self.stop_btn = tk.Button(btn_frame, text="■ STOP",
                                 font=('Segoe UI', 11, 'bold'),
                                 bg=COLORS['error'], fg='white',
                                 relief=tk.FLAT, cursor='hand2', width=8,
                                 state=tk.DISABLED, command=self._stop_bots)
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 10), ipady=5)
        
        tk.Button(btn_frame, text="💾 Save", font=('Segoe UI', 10),
                 bg=COLORS['bg_light'], fg=COLORS['fg_primary'],
                 relief=tk.FLAT, cursor='hand2',
                 command=self._save_config).pack(side=tk.LEFT, padx=(0, 10), ipady=3)
        
        tk.Button(btn_frame, text="🗑 Clear", font=('Segoe UI', 10),
                 bg=COLORS['bg_light'], fg=COLORS['fg_primary'],
                 relief=tk.FLAT, cursor='hand2',
                 command=self._clear_log).pack(side=tk.LEFT, ipady=3)
    
    def _create_log_section(self, parent):
        self.log_text = scrolledtext.ScrolledText(
            parent, wrap=tk.WORD, font=('Consolas', 10),
            bg='#0d0d1a', fg=COLORS['fg_primary'],
            insertbackground='white', relief=tk.FLAT, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        # Log tags
        self.log_text.tag_configure('TIMESTAMP', foreground='#666688')
        self.log_text.tag_configure('INFO', foreground=COLORS['info'])
        self.log_text.tag_configure('DEBUG', foreground=COLORS['debug'])
        self.log_text.tag_configure('WARNING', foreground=COLORS['warning'])
        self.log_text.tag_configure('ERROR', foreground=COLORS['error'])
        self.log_text.tag_configure('SUCCESS', foreground=COLORS['success'])
        self.log_text.tag_configure('HEADER', foreground=COLORS['accent'], font=('Consolas', 10, 'bold'))
        self.log_text.tag_configure('STEP', foreground='#ffaa00')
        self.log_text.tag_configure('TEST', foreground=COLORS['test'])
        self.log_text.tag_configure('PASS', foreground='#00ff88')
        self.log_text.tag_configure('FAIL', foreground='#ff4757')
        
        # Transcription tags
        self.log_text.tag_configure('TRANSCRIPT_COMMANDER', foreground=COLORS['transcript_commander'],
                                   font=('Consolas', 10, 'bold'))
        self.log_text.tag_configure('TRANSCRIPT_SQUAD', foreground=COLORS['transcript_squad'],
                                   font=('Consolas', 10, 'bold'))
        self.log_text.tag_configure('SPEECH', foreground='#ffffff', font=('Consolas', 10, 'italic'))
        self.log_text.tag_configure('PROCESS_TIME', foreground='#888888', font=('Consolas', 9))
    
    def _toggle_show(self, entry):
        entry.config(show='' if entry.cget('show') == '●' else '●')
    
    def _log(self, message, level='INFO'):
        self.log_text.config(state=tk.NORMAL)
        timestamp = datetime.now().strftime('%H:%M:%S')
        self.log_text.insert(tk.END, f"[{timestamp}] ", 'TIMESTAMP')
        self.log_text.insert(tk.END, f"{message}\n", level)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
    
    def _log_raw(self, message, tag='INFO'):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"{message}\n", tag)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
    
    def _log_transcription(self, speaker_name: str, speaker_role: str, text: str, process_time: float = 0.0):
        """Display a transcription in the log - commander's orders in gold."""
        self.log_text.config(state=tk.NORMAL)
        timestamp = datetime.now().strftime('%H:%M:%S')

        # Determine tag based on role
        if "COMMANDER" in speaker_role.upper():
            role_tag = 'TRANSCRIPT_COMMANDER'
            icon = "👑"
            prefix = "ORDER"
        else:
            role_tag = 'TRANSCRIPT_SQUAD'
            icon = "📡"
            prefix = "UPLINK"

        self.log_text.insert(tk.END, f"[{timestamp}] ", 'TIMESTAMP')
        self.log_text.insert(tk.END, f"{icon} [{prefix}] {speaker_name}: ", role_tag)
        self.log_text.insert(tk.END, f"\"{text}\"", 'SPEECH')
        if process_time > 0:
            self.log_text.insert(tk.END, f" ({process_time:.2f}s)", 'PROCESS_TIME')
        self.log_text.insert(tk.END, "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
    
    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)
        self._log("Log cleared.", 'INFO')
    
    def _poll_log_queue(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, tuple):
                    msg_type = item[0]
                    if msg_type == "log":
                        message = item[1]
                        level = 'INFO'
                        for lvl in ['DEBUG', 'INFO', 'WARNING', 'ERROR']:
                            if f'| {lvl}' in message:
                                level = lvl
                                break
                        self._log(message, level)
                    elif msg_type == "transcription":
                        speaker_name, speaker_role, text = item[1], item[2], item[3]
                        process_time = item[4] if len(item) > 4 else 0.0
                        self._log_transcription(speaker_name, speaker_role, text, process_time)
                else:
                    self._log(str(item), 'INFO')
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)
    
    def _show_startup_info(self):
        self._log_raw("=" * 60, 'HEADER')
        self._log_raw("   VOICE RELAY SYSTEM", 'HEADER')
        self._log_raw("=" * 60, 'HEADER')
        self._log_raw("", 'INFO')
        self._log_raw("⚡ CONNECTING TO DISCORD...", 'WARNING')
        self._log_raw("", 'INFO')
        self._log_raw("HOW IT WORKS:", 'HEADER')
        self._log_raw("  1. Type !list in #relay-chat", 'STEP')
        self._log_raw("  2. Type !setcommander [#] (auto-gets User ID!)", 'STEP')
        self._log_raw("  3. Commander says BOYS to broadcast!", 'PASS')
        self._log_raw("", 'INFO')
        self._log_raw("📝 Commander's orders appear in this log!", 'PASS')
        self._log_raw("", 'INFO')
        self._log_raw("Commands:", 'INFO')
        self._log_raw("  !list           - View eligible commanders", 'INFO')
        self._log_raw("  !setcommander 1 - Set commander", 'INFO')
        self._log_raw("  !stop           - Stop broadcasting", 'INFO')
        self._log_raw("  !status         - Show status", 'INFO')
        self._log_raw("", 'INFO')
        self._log_raw("-" * 60, 'INFO')
        self._log_raw("", 'INFO')
    
    def _get_config_from_gui(self) -> dict:
        return {
            "commander_token": self.commander_token_var.get().strip(),
            "drone_alpha_token": self.drone_alpha_token_var.get().strip(),
            "drone_bravo_token": self.drone_bravo_token_var.get().strip(),
            "drone_alpha_channel_id": self.drone_alpha_channel_id_var.get().strip(),
            "drone_bravo_channel_id": self.drone_bravo_channel_id_var.get().strip(),
            "squad_uplink_timeout": self.timeout_var.get(),
            "whisper_model": self.whisper_model_var.get(),
            "command_prefix": "!",
            "max_buffer_frames": 100,
            "log_level": "DEBUG"
        }
    
    def _save_config(self):
        self.config = self._get_config_from_gui()
        save_config(self.config)
        self._log("Configuration saved", 'SUCCESS')
    
    def _reload_config(self):
        self.config = load_config()
        self.commander_token_var.set(self.config.get("commander_token", ""))
        self.drone_alpha_token_var.set(self.config.get("drone_alpha_token", ""))
        self.drone_bravo_token_var.set(self.config.get("drone_bravo_token", ""))
        self.drone_alpha_channel_id_var.set(self.config.get("drone_alpha_channel_id", ""))
        self.drone_bravo_channel_id_var.set(self.config.get("drone_bravo_channel_id", ""))
        self.timeout_var.set(self.config.get("squad_uplink_timeout", 1.0))
        self.whisper_model_var.set(self.config.get("whisper_model", "base"))
        self._on_whisper_model_change(None)  # Update description label
        self._log("Configuration reloaded", 'INFO')
    
    def _validate_config(self) -> bool:
        config = self._get_config_from_gui()
        errors = []
        
        if not config["commander_token"] or len(config["commander_token"]) < 50:
            errors.append("Mothership token invalid")
        if not config["drone_alpha_token"] or len(config["drone_alpha_token"]) < 50:
            errors.append("Alpha token invalid")
        if not config["drone_bravo_token"] or len(config["drone_bravo_token"]) < 50:
            errors.append("Bravo token invalid")
        if not config["drone_alpha_channel_id"] or not config["drone_alpha_channel_id"].isdigit():
            errors.append("Alpha Channel ID invalid")
        if not config["drone_bravo_channel_id"] or not config["drone_bravo_channel_id"].isdigit():
            errors.append("Bravo Channel ID invalid")
        
        if errors:
            for e in errors:
                self._log(f"⚠️ {e}", 'ERROR')
            self._log("Fix errors above, then click START", 'WARNING')
            return False
        return True
    
    def _test_bots(self):
        if self.testing or self.bot_running:
            return
        if not self._validate_config():
            return
        
        self._save_config()
        self.testing = True
        self.test_btn.config(state=tk.DISABLED)
        self.start_btn.config(state=tk.DISABLED)
        self._update_status("TESTING...", COLORS['test'])
        
        threading.Thread(target=self._run_bot_tests, daemon=True).start()
    
    def _run_bot_tests(self):
        config = self._get_config_from_gui()
        
        def log_cb(msg, tag):
            self.root.after(0, lambda: self._log_raw(msg, tag))
        
        try:
            results = asyncio.run(test_all_bots(config, log_cb))
            self.root.after(0, lambda: self._display_test_results(results))
        except Exception as e:
            self.root.after(0, lambda: self._log(f"Test failed: {e}", 'ERROR'))
        finally:
            self.root.after(0, self._on_tests_complete)
    
    def _display_test_results(self, results):
        self._log_raw("", 'INFO')
        self._log_raw("=" * 50, 'HEADER')
        
        for key, name in [("mothership", "MOTHERSHIP"), ("alpha", "ALPHA"), ("bravo", "BRAVO")]:
            r = results[key]
            if r["token_valid"]:
                self._log_raw(f"✓ {name}: {r['bot_user']}", 'PASS')
                if r.get("channel_found"):
                    self._log_raw(f"  Channel: #{r['channel_name']}", 'DEBUG')
            else:
                self._log_raw(f"✗ {name}: {r['errors']}", 'FAIL')
        
        self._log_raw("", 'INFO')
        if results["overall_success"]:
            self._log_raw("✓ ALL TESTS PASSED", 'PASS')
        else:
            self._log_raw("✗ SOME TESTS FAILED", 'FAIL')
        self._log_raw("=" * 50, 'HEADER')
    
    def _on_tests_complete(self):
        self.testing = False
        self.test_btn.config(state=tk.NORMAL)
        self.start_btn.config(state=tk.NORMAL)
        self._update_status("OFFLINE", COLORS['error'])
    
    def _auto_start_bots(self):
        """Automatically start bots on application launch."""
        if not self._validate_config():
            self._log("⚠️ Fix config errors above, then click START", 'ERROR')
            return
        self._start_bots()
    
    def _start_bots(self):
        if not self._validate_config():
            return
        
        self._save_config()
        config = self._get_config_from_gui()
        
        self._log_raw("", 'INFO')
        self._log_raw("Connecting to Discord...", 'INFO')
        
        self.test_btn.config(state=tk.DISABLED)
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self._update_status("STARTING...", COLORS['warning'])
        
        self.stop_event.clear()
        self.bot_thread = threading.Thread(target=self._run_bot_thread, daemon=True)
        self.bot_thread.start()
    
    def _run_bot_thread(self):
        try:
            import voice_relay
            
            # Set up log handler
            handler = QueueHandler(self.log_queue)
            handler.setFormatter(logging.Formatter(
                '%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s',
                datefmt='%H:%M:%S'))
            logging.getLogger().addHandler(handler)
            logging.getLogger().setLevel(logging.DEBUG)
            
            # Set up transcription callback
            def transcription_cb(speaker, role, text, process_time=0.0):
                self.log_queue.put(("transcription", speaker, role, text, process_time))

            voice_relay.set_transcription_callback(transcription_cb)
            
            self.root.after(0, lambda: self._update_status("ONLINE", COLORS['success']))
            self.bot_running = True
            
            asyncio.run(voice_relay.run_with_config(self._get_config_from_gui(), self.stop_event))
        except Exception as e:
            self.log_queue.put(("log", f"ERROR | {e}"))
        finally:
            self.bot_running = False
            self.root.after(0, self._on_bot_stopped)
    
    def _update_status(self, text, color):
        self.status_dot.config(fg=color)
        self.status_label.config(text=text)
    
    def _stop_bots(self):
        self._log("Stopping bots...", 'WARNING')
        self._update_status("STOPPING...", COLORS['warning'])
        self.stop_event.set()
    
    def _on_bot_stopped(self):
        self.test_btn.config(state=tk.NORMAL)
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self._update_status("OFFLINE", COLORS['error'])
        self._log("System stopped", 'WARNING')
    
    def _show_setup_instructions(self):
        self._clear_log()
        self._show_startup_info()
    
    def _show_about(self):
        messagebox.showinfo("About",
            "Voice Relay System v4.1\n\n"
            "Features:\n"
            "• Auto User ID detection\n"
            "• Commander's orders in log\n"
            "• Real-time transcription\n"
            "• Squad uplinks supported\n\n"
            "Quick Start:\n"
            "1. Type !list in #relay-chat\n"
            "2. Type !setcommander [#]\n"
            "3. Say BOYS to broadcast!\n\n"
            "Voice Commands:\n"
            "• BOYS - Start broadcast\n"
            "• END COMMS - Stop broadcast\n"
            "• COMMANDER - Squad uplink")
    
    def _on_close(self):
        if self.bot_running:
            if messagebox.askyesno("Exit", "Stop bots and exit?"):
                self.stop_event.set()
                self.root.after(1000, self.root.destroy)
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    VoiceRelayGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
