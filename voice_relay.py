#!/usr/bin/env python3
"""
VOICE RELAY SYSTEM
==================
Commands work in #relay-chat text channel.
"""

import asyncio
import logging
import sys
import json
import threading
import wave
import io
from collections import deque
from pathlib import Path
from typing import Optional, Dict, Callable

import discord
from discord.ext import commands

# ===================================================================================
# IMPORTS
# ===================================================================================

try:
    import discord.ext.voice_recv as voice_recv
    VOICE_RECV_AVAILABLE = True
except ImportError:
    VOICE_RECV_AVAILABLE = False
    print("WARNING: discord-ext-voice-recv not installed")

def check_and_install_whisper():
    """Check if Whisper is installed, offer to install if not."""
    try:
        import whisper
        return True
    except ImportError:
        print("\n" + "=" * 60)
        print("OpenAI Whisper is not installed.")
        print("Whisper is required for voice trigger detection.")
        print("=" * 60)
        response = input("Install openai-whisper now? (y/n): ").strip().lower()
        if response == 'y':
            import subprocess
            print("\nInstalling openai-whisper... This may take a few minutes.")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", "openai-whisper"])
                print("Whisper installed successfully!")
                return True
            except subprocess.CalledProcessError as e:
                print(f"Failed to install Whisper: {e}")
                return False
        else:
            print("Whisper not installed. Voice recognition will be disabled.")
            return False

# Check/install Whisper on import
WHISPER_AVAILABLE = check_and_install_whisper()

if WHISPER_AVAILABLE:
    import whisper
    import numpy as np
    WHISPER_MODEL = None  # Lazy load
else:
    whisper = None
    np = None
    WHISPER_MODEL = None

# ===================================================================================
# LOGGING
# ===================================================================================

logger = logging.getLogger("VoiceRelay")

transcription_callback: Optional[Callable[[str, str, str, float], None]] = None

def set_transcription_callback(callback):
    global transcription_callback
    transcription_callback = callback

def log_transcription(speaker_name: str, speaker_role: str, text: str, process_time: float = 0.0):
    if process_time > 0:
        logger.info("[%s] %s: \"%s\" (%.2fs)", speaker_role, speaker_name, text, process_time)
    else:
        logger.info("[%s] %s: \"%s\"", speaker_role, speaker_name, text)
    if transcription_callback:
        try:
            transcription_callback(speaker_name, speaker_role, text, process_time)
        except Exception as e:
            print(f"ERROR in transcription callback: {e}", file=sys.stderr)

def setup_logging(level: str = "DEBUG"):
    log_level = getattr(logging, level.upper(), logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s', datefmt='%H:%M:%S')
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.setLevel(log_level)
    root = logging.getLogger()
    root.setLevel(log_level)
    root.addHandler(handler)
    
    # Suppress noisy discord logs including packet loss warnings
    logging.getLogger('discord').setLevel(logging.WARNING)
    logging.getLogger('discord.player').setLevel(logging.ERROR)
    logging.getLogger('discord.voice_client').setLevel(logging.ERROR)
    logging.getLogger('discord.voice_state').setLevel(logging.CRITICAL)
    logging.getLogger('discord.gateway').setLevel(logging.ERROR)

    # Suppress the decoder packet loss warnings and voice_recv router errors
    for name in ['discord.ext.voice_recv', 'discord.ext.voice_recv.router',
                 'voice_recv', 'voice_recv.router', 'discord.opus']:
        logging.getLogger(name).setLevel(logging.CRITICAL)

# ===================================================================================
# AUDIO CONSTANTS
# ===================================================================================

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2
FRAME_DURATION_MS = 20
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)
BYTES_PER_FRAME = FRAME_SIZE * CHANNELS * SAMPLE_WIDTH
SILENCE_FRAME = b'\x00' * BYTES_PER_FRAME

TRIGGER_START = "boys"
TRIGGER_STOP = "end comms"
TRIGGER_UPLINK = "commander"

SILENCE_THRESHOLD = 500
COMMANDER_SILENCE_TIMEOUT = 5.0
SQUAD_UPLINK_TIMEOUT = 1.0

# Larger buffer to prevent packet loss
DEFAULT_BUFFER_FRAMES = 100  # ~2 seconds of audio at 20ms per frame

# ===================================================================================
# GLOBAL STATE
# ===================================================================================

squad_uplink_timeout: float = SQUAD_UPLINK_TIMEOUT

def set_squad_uplink_timeout(seconds: float):
    global squad_uplink_timeout
    squad_uplink_timeout = seconds

class AudioBuffer:
    """Thread-safe audio buffer with larger capacity to prevent packet loss."""
    def __init__(self, name: str, max_frames: int = DEFAULT_BUFFER_FRAMES):
        self.name = name
        self._buffer: deque = deque(maxlen=max_frames)
        self._active = False
        import threading
        self._lock = threading.Lock()
    
    def activate(self):
        with self._lock:
            self._active = True
            self._buffer.clear()
        logger.info("[%s] Buffer ACTIVE", self.name)
    
    def deactivate(self):
        with self._lock:
            self._active = False
            self._buffer.clear()
        logger.info("[%s] Buffer INACTIVE", self.name)
    
    def push(self, data: bytes):
        with self._lock:
            if self._active:
                self._buffer.append(data)
    
    def pop(self) -> bytes:
        with self._lock:
            if self._active and self._buffer:
                return self._buffer.popleft()
            return SILENCE_FRAME
    
    @property
    def size(self) -> int:
        with self._lock:
            return len(self._buffer)

broadcast_buffer: Optional[AudioBuffer] = None
uplink_buffer: Optional[AudioBuffer] = None

# ===================================================================================
# WHISPER MODEL
# ===================================================================================

WHISPER_MODEL_NAME = "base"  # Default model, can be changed via config

def set_whisper_model(model_name: str):
    """Set which Whisper model to use."""
    global WHISPER_MODEL_NAME, WHISPER_MODEL
    valid_models = ["tiny", "base", "small", "medium", "large"]
    if model_name in valid_models:
        if WHISPER_MODEL is not None and model_name != WHISPER_MODEL_NAME:
            logger.info("Whisper model changed to '%s' - will load on next use", model_name)
            WHISPER_MODEL = None  # Force reload
        WHISPER_MODEL_NAME = model_name

def get_whisper_model():
    """Lazy load Whisper model."""
    global WHISPER_MODEL
    if not WHISPER_AVAILABLE:
        return None
    if WHISPER_MODEL is None:
        model_sizes = {
            "tiny": "~75MB",
            "base": "~142MB",
            "small": "~466MB",
            "medium": "~1.5GB",
            "large": "~3GB"
        }
        size = model_sizes.get(WHISPER_MODEL_NAME, "")
        logger.info("Loading Whisper model (%s)... This may take a moment.", WHISPER_MODEL_NAME)
        logger.info("First run will download the model (%s)", size)
        WHISPER_MODEL = whisper.load_model(WHISPER_MODEL_NAME)
        logger.info("Whisper model loaded.")
    return WHISPER_MODEL

# ===================================================================================
# VOICE TRIGGER DETECTOR
# ===================================================================================

class VoiceTriggerDetector:
    def __init__(self, triggers: list, speaker_name: str = "Unknown", speaker_role: str = "UNKNOWN"):
        self.triggers = [t.lower() for t in triggers]
        self.speaker_name = speaker_name
        self.speaker_role = speaker_role
        self._buffer = b''
        self._max_bytes = int(SAMPLE_RATE * 2 * 2 * 2.5)  # ~2.5 seconds of stereo 16-bit audio
        self._last_check = 0
        self._last_text = ""

    def update_speaker(self, name: str, role: str):
        self.speaker_name = name
        self.speaker_role = role

    def feed_audio(self, pcm: bytes) -> Optional[str]:
        if not WHISPER_AVAILABLE:
            return None

        self._buffer += pcm
        if len(self._buffer) > self._max_bytes:
            self._buffer = self._buffer[-self._max_bytes:]

        import time
        now = time.time()
        if now - self._last_check < 1.5:  # Slightly longer interval for Whisper
            return None
        self._last_check = now

        if len(self._buffer) < self._max_bytes // 2:
            return None

        try:
            text, process_time = self._recognize(self._buffer)
            if text and text != self._last_text:
                self._last_text = text
                log_transcription(self.speaker_name, self.speaker_role, text, process_time)

                text_lower = text.lower()
                for trigger in self.triggers:
                    if trigger in text_lower:
                        self._buffer = b''
                        return trigger
        except Exception as e:
            print(f"ERROR in feed_audio: {e}", file=sys.stderr)
        return None

    def transcribe_only(self, pcm: bytes) -> Optional[str]:
        if not WHISPER_AVAILABLE:
            return None

        self._buffer += pcm
        if len(self._buffer) > self._max_bytes:
            self._buffer = self._buffer[-self._max_bytes:]

        import time
        now = time.time()
        if now - self._last_check < 1.5:
            return None
        self._last_check = now

        if len(self._buffer) < self._max_bytes // 2:
            return None

        try:
            text, process_time = self._recognize(self._buffer)
            if text and text != self._last_text:
                self._last_text = text
                log_transcription(self.speaker_name, self.speaker_role, text, process_time)
                return text
        except Exception as e:
            print(f"ERROR in transcribe_only: {e}", file=sys.stderr)
        return None

    def _recognize(self, pcm: bytes) -> tuple:
        """Returns (text, processing_time) tuple."""
        model = get_whisper_model()
        if model is None:
            return (None, 0.0)

        try:
            import struct
            import time

            start_time = time.time()

            # Stereo 16-bit PCM to mono float32 numpy array
            samples = len(pcm) // 4
            mono = []
            for i in range(samples):
                left = struct.unpack_from('<h', pcm, i * 4)[0]
                right = struct.unpack_from('<h', pcm, i * 4 + 2)[0]
                mono.append((left + right) / 2 / 32768.0)  # Normalize to [-1, 1]

            audio_np = np.array(mono, dtype=np.float32)

            # Resample from 48kHz to 16kHz (Whisper expects 16kHz)
            # Simple decimation by factor of 3
            audio_16k = audio_np[::3]

            # Pad or trim to 30 seconds (Whisper expects this)
            audio_16k = whisper.pad_or_trim(audio_16k)

            # Transcribe
            result = model.transcribe(
                audio_16k,
                language="en",
                fp16=False,  # Use fp32 for CPU compatibility
                without_timestamps=True
            )

            process_time = time.time() - start_time
            text = result.get("text", "").strip()
            return (text if text else None, process_time)

        except Exception as e:
            print(f"ERROR in _recognize: {e}", file=sys.stderr)
            return (None, 0.0)
    
    def check_silence(self, pcm: bytes) -> bool:
        import struct
        samples = struct.unpack(f'<{len(pcm)//2}h', pcm)
        rms = (sum(s*s for s in samples) / len(samples)) ** 0.5
        return rms < SILENCE_THRESHOLD
    
    def clear(self):
        self._buffer = b''
        self._last_text = ""

# ===================================================================================
# AUDIO SOURCE
# ===================================================================================

class BufferAudioSource(discord.AudioSource):
    def __init__(self, buffer: AudioBuffer):
        self.buffer = buffer
    
    def read(self) -> bytes:
        return self.buffer.pop()
    
    def is_opus(self) -> bool:
        return False
    
    def cleanup(self):
        pass

# ===================================================================================
# VOICE SINKS
# ===================================================================================

class CommanderVoiceSink(voice_recv.AudioSink):
    """Captures commander's voice. Says BOYS to start, END COMMS to stop.
    Transcribes all commander speech to the log."""

    def __init__(self, buffer: AudioBuffer, target_id: int, name: str, callback, loop):
        self.buffer = buffer
        self.target_id = target_id
        self.name = name
        self.callback = callback
        self.loop = loop
        self._listening = True
        self._broadcasting = False
        self._accumulator = b''
        self._silence_frames = 0
        self._silence_max = int(COMMANDER_SILENCE_TIMEOUT * 1000 / FRAME_DURATION_MS)
        # Listen for both "BOYS" to start and "END COMMS" to stop
        self.detector = VoiceTriggerDetector([TRIGGER_START, TRIGGER_STOP], name, "COMMANDER")
    
    def update_commander(self, user_id: int, name: str):
        self.target_id = user_id
        self.name = name
        self.detector.update_speaker(name, "COMMANDER")
        if self._broadcasting:
            self._broadcasting = False
            self.buffer.deactivate()
    
    def wants_opus(self) -> bool:
        return False
    
    def write(self, user, data):
        if not self._listening or user is None or user.id != self.target_id:
            return
        
        pcm = data.pcm
        if pcm is None:
            return
        
        self._accumulator += pcm
        while len(self._accumulator) >= BYTES_PER_FRAME:
            frame = self._accumulator[:BYTES_PER_FRAME]
            self._accumulator = self._accumulator[BYTES_PER_FRAME:]
            
            if not self._broadcasting:
                # Not broadcasting - check for "BOYS" trigger
                trigger = self.detector.feed_audio(frame)

                if trigger == TRIGGER_START:
                    logger.info("🎙️ BOYS - Broadcast START")
                    self._broadcasting = True
                    self.buffer.activate()
                    self._silence_frames = 0
                    asyncio.run_coroutine_threadsafe(self.callback("start"), self.loop)
                    self.detector.clear()
            else:
                # Broadcasting - transcribe speech AND check for "END COMMS"
                trigger = self.detector.feed_audio(frame)

                if trigger == TRIGGER_STOP:
                    logger.info("🛑 END COMMS - Broadcast STOP")
                    self._broadcasting = False
                    self.buffer.deactivate()
                    asyncio.run_coroutine_threadsafe(self.callback("stop"), self.loop)
                    self.detector.clear()
                    continue

                # Check for silence timeout
                if self.detector.check_silence(frame):
                    self._silence_frames += 1
                    if self._silence_frames > self._silence_max:
                        logger.info("⏸️ Auto-stop (silence)")
                        self._broadcasting = False
                        self.buffer.deactivate()
                        asyncio.run_coroutine_threadsafe(self.callback("stop"), self.loop)
                        continue
                else:
                    self._silence_frames = 0

                # Push audio to broadcast buffer
                self.buffer.push(frame)
    
    def cleanup(self):
        self._listening = False
        self._broadcasting = False
        self.buffer.deactivate()
        self._broadcasting = False
        self.buffer.deactivate()


class SquadVoiceSink(voice_recv.AudioSink):
    def __init__(self, buffer: AudioBuffer, drone_name: str, callback, loop):
        self.buffer = buffer
        self.drone_name = drone_name
        self.callback = callback
        self.loop = loop
        self._listening = True
        self._uplinking = False
        self._uplink_user_id = None
        self._accumulator = b''
        self._silence_frames = 0
        self.detector = VoiceTriggerDetector([TRIGGER_UPLINK], "Squad", f"SQUAD-{drone_name}")
    
    def _silence_max(self) -> int:
        return int(squad_uplink_timeout * 1000 / FRAME_DURATION_MS)
    
    def wants_opus(self) -> bool:
        return False
    
    def write(self, user, data):
        if not self._listening or user is None:
            return
        
        pcm = data.pcm
        if pcm is None:
            return
        
        self._accumulator += pcm
        while len(self._accumulator) >= BYTES_PER_FRAME:
            frame = self._accumulator[:BYTES_PER_FRAME]
            self._accumulator = self._accumulator[BYTES_PER_FRAME:]
            
            self.detector.update_speaker(user.display_name, f"SQUAD-{self.drone_name}")
            
            if not self._uplinking:
                trigger = self.detector.feed_audio(frame)
                if trigger == TRIGGER_UPLINK:
                    logger.info("[%s] %s starting uplink", self.drone_name, user.display_name)
                    self._uplinking = True
                    self._uplink_user_id = user.id
                    self.buffer.activate()
                    self._silence_frames = 0
                    asyncio.run_coroutine_threadsafe(self.callback("start", user), self.loop)
                    self.detector.clear()
                continue

            if self._uplinking and user.id == self._uplink_user_id:
                self.detector.transcribe_only(frame)

                if self.detector.check_silence(frame):
                    self._silence_frames += 1
                    if self._silence_frames > self._silence_max():
                        logger.info("[%s] Uplink auto-ended", self.drone_name)
                        self._uplinking = False
                        self._uplink_user_id = None
                        self.buffer.deactivate()
                        asyncio.run_coroutine_threadsafe(self.callback("stop", user), self.loop)
                        self.detector.clear()
                        continue
                else:
                    self._silence_frames = 0
                self.buffer.push(frame)
    
    def cleanup(self):
        self._listening = False
        self._uplinking = False
        self.buffer.deactivate()

# ===================================================================================
# MOTHERSHIP BOT
# ===================================================================================

class MothershipBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True
        intents.members = True
        
        super().__init__(command_prefix="!", intents=intents)

        # Default commander (can be changed via !setcommander)
        self.commander_user_id = 276201168412082179
        self.commander_name = None
        self.drone_bots: Dict[str, 'DroneBot'] = {}
        self.voice_client = None
        self.commander_sink = None
        self.is_broadcasting = False
        self._admin_list_cache = []
        
        logger.info("MothershipBot initialized")
    
    async def setup_hook(self):
        """Called when bot is ready to set up commands."""
        # Remove default help
        self.remove_command('help')
    
    async def on_ready(self):
        logger.info("=" * 50)
        logger.info("MOTHERSHIP ONLINE: %s", self.user.name)
        logger.info("Guilds: %s", [g.name for g in self.guilds])
        logger.info("=" * 50)

        # Find and set up default commander
        for guild in self.guilds:
            member = guild.get_member(self.commander_user_id)
            if member:
                self.commander_name = member.display_name
                logger.info("👑 Default Commander: %s (ID: %d)", member.display_name, self.commander_user_id)
                if member.voice and member.voice.channel:
                    logger.info("🎙️ Commander in voice - joining channel...")
                    await self._join_channel(member.voice.channel)
                else:
                    logger.info("⏳ Waiting for commander to join voice...")
                break
        else:
            logger.info("⚠️ Default commander (ID: %d) not found in any guild", self.commander_user_id)

        logger.info("=" * 50)
    
    async def on_message(self, message):
        """Handle all messages - commands work in relay-chat."""
        if message.author.bot:
            return
        
        # Only process commands in relay-chat
        if message.channel.name != "relay-chat":
            return
        
        content = message.content.strip()
        
        # !list - Show eligible commanders
        if content == "!list":
            await self._cmd_list(message)
        
        # !setcommander - Set commander by number or mention
        elif content.startswith("!setcommander"):
            parts = content.split(maxsplit=1)
            target = parts[1] if len(parts) > 1 else None
            await self._cmd_setcommander(message, target)
        
        # !commander - Show current commander
        elif content == "!commander":
            await self._cmd_commander(message)
        
        # !status - Show status
        elif content == "!status":
            await self._cmd_status(message)
        
        # !stop - Stop broadcasting
        elif content == "!stop":
            await self._cmd_stop(message)
        
        # !help - Show help
        elif content == "!help":
            await self._cmd_help(message)
    
    async def _cmd_list(self, message):
        """List eligible commanders."""
        guild = message.guild
        if not guild:
            await message.channel.send("Could not access server.")
            return
        
        admins = []
        for member in guild.members:
            if member.bot:
                continue
            if member.guild_permissions.administrator:
                admins.append(member)
        
        if not admins:
            await message.channel.send("No administrators found.")
            return
        
        self._admin_list_cache = admins
        
        lines = ["**👑 ELIGIBLE COMMANDERS**", ""]
        lines.append("```")
        for i, m in enumerate(admins, 1):
            current = " ★ CURRENT" if m.id == self.commander_user_id else ""
            if m.voice and m.voice.channel:
                status = f"🎤 {m.voice.channel.name}"
            elif m.status == discord.Status.online:
                status = "🟢 Online"
            elif m.status == discord.Status.idle:
                status = "🟡 Idle"
            else:
                status = "⚫ Offline"
            lines.append(f"  {i}. {m.display_name:<18} {status}{current}")
        lines.append("```")
        lines.append("Use `!setcommander [#]` to choose")
        
        await message.channel.send("\n".join(lines))
    
    async def _cmd_setcommander(self, message, target):
        """Set commander by number or mention."""
        if not target:
            await message.channel.send("Usage: `!setcommander [#]` or `!setcommander @user`\nRun `!list` first.")
            return
        
        member = None
        
        # By number
        if target.isdigit():
            idx = int(target) - 1
            if not self._admin_list_cache:
                await message.channel.send("Run `!list` first.")
                return
            if idx < 0 or idx >= len(self._admin_list_cache):
                await message.channel.send(f"Invalid number. Use 1-{len(self._admin_list_cache)}")
                return
            member = self._admin_list_cache[idx]
        
        # By mention
        elif target.startswith('<@') and target.endswith('>'):
            user_id_str = target.replace('<@', '').replace('>', '').replace('!', '')
            try:
                user_id = int(user_id_str)
                member = message.guild.get_member(user_id)
            except ValueError:
                pass  # Invalid user ID format
        
        # By name
        else:
            for m in message.guild.members:
                if m.display_name.lower() == target.lower() or m.name.lower() == target.lower():
                    member = m
                    break
        
        if not member:
            await message.channel.send(f"Could not find: {target}")
            return
        
        if member.bot:
            await message.channel.send("Cannot set a bot as Commander.")
            return
        
        # Update commander
        self.commander_user_id = member.id
        self.commander_name = member.display_name
        
        if self.commander_sink:
            self.commander_sink.update_commander(member.id, member.display_name)
        
        logger.info("Commander set: %s (ID: %d)", member.display_name, member.id)
        
        # Join commander's voice channel if they're in one
        if member.voice and member.voice.channel:
            await self._join_channel(member.voice.channel)
            await message.channel.send(f"✅ **{member.display_name}** is now Commander! (ID: `{member.id}`)\n"
                                       f"🎙️ Listening... Say **\"BOYS\"** to broadcast, **\"END COMMS\"** to stop.")
        else:
            await message.channel.send(f"✅ **{member.display_name}** is now Commander! (ID: `{member.id}`)\n"
                                       f"⚠️ Join a voice channel, then say **\"BOYS\"** to broadcast.")
    
    async def _cmd_commander(self, message):
        """Show current commander."""
        if not self.commander_user_id:
            await message.channel.send("⚠️ No commander set. Use `!list` then `!setcommander [#]`")
            return
        
        commander = None
        for guild in self.guilds:
            commander = guild.get_member(self.commander_user_id)
            if commander:
                break
        
        status = "🎙️ BROADCASTING" if self.is_broadcasting else "⏸️ Standing by"
        if commander:
            await message.channel.send(f"👑 Commander: **{commander.display_name}** (ID: {self.commander_user_id})\nStatus: {status}")
        else:
            await message.channel.send(f"👑 Commander ID: {self.commander_user_id}\nStatus: {status}")
    
    async def _cmd_status(self, message):
        """Show system status."""
        lines = ["**📡 RELAY STATUS**", ""]
        
        if self.commander_user_id:
            commander = None
            for guild in self.guilds:
                commander = guild.get_member(self.commander_user_id)
                if commander:
                    break
            lines.append(f"👑 Commander: **{commander.display_name if commander else 'Unknown'}** (ID: {self.commander_user_id})")
        else:
            lines.append("👑 Commander: **NOT SET** - Use `!setcommander`")
        
        lines.append(f"📢 Broadcasting: {'YES' if self.is_broadcasting else 'No'}")
        lines.append(f"⏱️ Squad timeout: {squad_uplink_timeout:.1f}s")
        lines.append("")
        
        for name, drone in self.drone_bots.items():
            if drone.voice_client:
                if self.is_broadcasting:
                    status = "🎙️ Broadcasting"
                else:
                    status = "✅ Connected (standby)"
            else:
                status = "❌ Not connected"
            lines.append(f"🤖 {name}: {status}")
        
        await message.channel.send("\n".join(lines))
    
    async def _cmd_stop(self, message):
        """Stop broadcasting."""
        if not self.is_broadcasting:
            await message.channel.send("⚠️ Not currently broadcasting.")
            return
        
        await self._stop_broadcast()
        await message.channel.send("🛑 **Broadcast stopped.**\nUse `!setcommander` to restart.")
    
    async def _cmd_help(self, message):
        """Show help."""
        await message.channel.send("""**📻 RELAY COMMANDS**

**Setup:**
`!list` - View eligible commanders
`!setcommander [#]` - Set commander by number
`!setcommander @user` - Set by mention
`!stop` - Stop broadcasting

**Info:**
`!commander` - Show current commander
`!status` - Show system status

**Voice:**
🎙️ Commander says **"BOYS"** → Start broadcast
🛑 Commander says **"END COMMS"** → Stop broadcast
📡 Squad says **"COMMANDER"** → Talk to Commander
""")
    
    async def on_voice_state_update(self, member, before, after):
        # Ignore if no commander set
        if not self.commander_user_id:
            return
        
        if member.id != self.commander_user_id:
            return
        
        # Commander joined or switched voice channel
        if after.channel and (not before.channel or before.channel != after.channel):
            self.commander_name = member.display_name
            logger.info("Commander %s joined %s - listening for BOYS", member.display_name, after.channel.name)
            await self._join_channel(after.channel)
        
        # Commander left voice
        elif before.channel and not after.channel:
            logger.info("Commander left voice")
            if self.is_broadcasting:
                await self._stop_broadcast()
            await self._cleanup()
    
    async def _join_channel(self, channel):
        if not VOICE_RECV_AVAILABLE:
            logger.error("voice_recv not available!")
            return
        
        if not self.commander_user_id:
            logger.error("No commander set!")
            return
        
        if self.voice_client:
            await self._cleanup()
        
        try:
            self.voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
            logger.info("Mothership joined: %s", channel.name)

            loop = asyncio.get_running_loop()
            self.commander_sink = CommanderVoiceSink(
                broadcast_buffer, self.commander_user_id, self.commander_name, self._on_trigger, loop
            )
            self.voice_client.listen(self.commander_sink)
        except Exception as e:
            logger.error("Failed to join: %s", e)
    
    async def _on_trigger(self, action: str):
        if action == "start":
            self.is_broadcasting = True
            logger.info("=" * 40)
            logger.info("📡 BROADCAST STARTED - Commander's orders:")
            logger.info("=" * 40)
            for drone in self.drone_bots.values():
                await drone.start_broadcast()
        elif action == "stop":
            await self._stop_broadcast()
    
    async def _stop_broadcast(self):
        """Stop broadcasting to all drones."""
        if not self.is_broadcasting:
            return
        
        self.is_broadcasting = False
        
        for name, drone in self.drone_bots.items():
            try:
                await drone.stop_broadcast()
            except Exception as e:
                logger.error("[%s] Failed to stop: %s", name, e)
        
        logger.info("=" * 40)
        logger.info("🛑 BROADCAST STOPPED")
        logger.info("=" * 40)
    
    async def start_uplink_audio(self):
        if self.voice_client and not self.voice_client.is_playing():
            self.voice_client.play(BufferAudioSource(uplink_buffer))
    
    async def stop_uplink_audio(self):
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
    
    async def _cleanup(self):
        self.is_broadcasting = False
        if self.commander_sink:
            self.commander_sink.cleanup()
            self.commander_sink = None
        if self.voice_client:
            try:
                self.voice_client.stop_listening()
            except Exception as e:
                logger.debug("Cleanup stop_listening: %s", e)
            try:
                await self.voice_client.disconnect()
            except Exception as e:
                logger.debug("Cleanup disconnect: %s", e)
            self.voice_client = None

# ===================================================================================
# DRONE BOT
# ===================================================================================

class DroneBot(commands.Bot):
    def __init__(self, name: str, channel_id: int, mothership: MothershipBot):
        intents = discord.Intents.all()
        intents.voice_states = True
        intents.guilds = True
        intents.members = True
        
        super().__init__(command_prefix="!", intents=intents)
        
        self.drone_name = name
        self.channel_id = channel_id
        self.mothership = mothership
        self.voice_client = None
        self.squad_sink = None
        self.audio_source = None
    
    async def on_ready(self):
        logger.info("DRONE [%s] ONLINE", self.drone_name)
        # Auto-connect to voice channel
        await asyncio.sleep(2)  # Wait for bot to fully initialize
        await self._connect_to_channel()
    
    async def _connect_to_channel(self):
        """Connect to the assigned voice channel and listen for squad uplinks."""
        if not VOICE_RECV_AVAILABLE:
            logger.error("[%s] voice_recv not available!", self.drone_name)
            return False
        
        try:
            channel = self.get_channel(self.channel_id)
            if not channel:
                logger.error("[%s] Channel %s not found!", self.drone_name, self.channel_id)
                return False
            
            self.voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
            logger.info("[%s] Connected to %s", self.drone_name, channel.name)

            # Start listening for squad uplinks
            loop = asyncio.get_running_loop()
            self.squad_sink = SquadVoiceSink(uplink_buffer, self.drone_name, self._on_squad_trigger, loop)
            self.voice_client.listen(self.squad_sink)
            logger.info("[%s] Listening for squad uplinks", self.drone_name)
            
            return True
        except Exception as e:
            logger.error("[%s] Failed to connect: %s", self.drone_name, e)
            return False
    
    async def start_broadcast(self):
        """Start playing broadcast audio."""
        if not self.voice_client:
            logger.warning("[%s] Not connected, cannot broadcast", self.drone_name)
            return False
        
        try:
            if self.voice_client.is_playing():
                self.voice_client.stop()
            
            self.audio_source = BufferAudioSource(broadcast_buffer)
            self.voice_client.play(self.audio_source)
            logger.info("[%s] Broadcasting started", self.drone_name)
            return True
        except Exception as e:
            logger.error("[%s] Broadcast failed: %s", self.drone_name, e)
            return False
    
    async def _on_squad_trigger(self, action: str, user):
        if action == "start":
            # Stop broadcast to hear uplink
            if self.voice_client and self.voice_client.is_playing():
                self.voice_client.stop()
            await self.mothership.start_uplink_audio()
        elif action == "stop":
            await self.mothership.stop_uplink_audio()
            # Resume broadcast if still active
            if self.mothership.is_broadcasting and self.voice_client:
                self.audio_source = BufferAudioSource(broadcast_buffer)
                self.voice_client.play(self.audio_source)
    
    async def stop_broadcast(self):
        """Stop playing broadcast audio (but stay connected)."""
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        self.audio_source = None
        logger.info("[%s] Broadcast stopped", self.drone_name)
    
    async def disconnect(self):
        """Fully disconnect from voice channel."""
        if self.squad_sink:
            self.squad_sink.cleanup()
            self.squad_sink = None
        if self.voice_client:
            try:
                self.voice_client.stop_listening()
            except Exception as e:
                logger.debug("[%s] Disconnect stop_listening: %s", self.drone_name, e)
            if self.voice_client.is_playing():
                self.voice_client.stop()
            try:
                await self.voice_client.disconnect()
            except Exception as e:
                logger.debug("[%s] Disconnect: %s", self.drone_name, e)
            self.voice_client = None
        logger.info("[%s] Disconnected", self.drone_name)

# ===================================================================================
# MAIN
# ===================================================================================

async def run_with_config(config: dict, stop_event: threading.Event = None):
    global broadcast_buffer, uplink_buffer, squad_uplink_timeout

    setup_logging(config.get("log_level", "DEBUG"))

    squad_uplink_timeout = config.get("squad_uplink_timeout", 1.0)
    set_whisper_model(config.get("whisper_model", "base"))

    logger.info("=" * 60)
    logger.info("VOICE RELAY SYSTEM")
    logger.info("=" * 60)
    logger.info("")
    logger.info("All 3 bots connecting simultaneously...")
    logger.info("")
    logger.info("HOW IT WORKS:")
    logger.info("  1. Type !list in #relay-chat")
    logger.info("  2. Type !setcommander [#] (auto-detects User ID!)")
    logger.info("  3. Commander says BOYS to start broadcasting")
    logger.info("")
    logger.info("Commands in #relay-chat:")
    logger.info("  !list          - View eligible commanders")
    logger.info("  !setcommander  - Set commander (auto-gets ID)")
    logger.info("  !stop          - Stop broadcasting")
    logger.info("  !status        - Show status")
    logger.info("")
    logger.info("Voice triggers:")
    logger.info("  'BOYS'        - Start broadcast")
    logger.info("  'END COMMS'   - Stop broadcast")
    logger.info("  'COMMANDER'   - Squad uplink")
    logger.info("")
    
    broadcast_buffer = AudioBuffer("BROADCAST", config.get("max_buffer_frames", DEFAULT_BUFFER_FRAMES))
    uplink_buffer = AudioBuffer("UPLINK", config.get("max_buffer_frames", DEFAULT_BUFFER_FRAMES))
    
    mothership = MothershipBot()
    alpha = DroneBot("ALPHA", int(config["drone_alpha_channel_id"]), mothership)
    bravo = DroneBot("BRAVO", int(config["drone_bravo_channel_id"]), mothership)
    
    mothership.drone_bots["ALPHA"] = alpha
    mothership.drone_bots["BRAVO"] = bravo
    
    async def run_bot(bot, token, name):
        try:
            await bot.start(token)
        except Exception as e:
            logger.error("%s failed: %s", name, e)
    
    async def stop_checker():
        while stop_event and not stop_event.is_set():
            await asyncio.sleep(0.5)
        if stop_event and stop_event.is_set():
            for bot in [mothership, alpha, bravo]:
                try:
                    await bot.close()
                except Exception as e:
                    logger.debug("Stop checker close: %s", e)

    try:
        tasks = [
            run_bot(mothership, config["commander_token"], "Mothership"),
            run_bot(alpha, config["drone_alpha_token"], "Alpha"),
            run_bot(bravo, config["drone_bravo_token"], "Bravo"),
        ]
        if stop_event:
            tasks.append(stop_checker())

        await asyncio.gather(*tasks)
    finally:
        for bot in [alpha, bravo]:
            try:
                await bot.disconnect()
                await bot.close()
            except Exception as e:
                logger.debug("Shutdown bot: %s", e)
        try:
            await mothership._cleanup()
            await mothership.close()
        except Exception as e:
            logger.debug("Shutdown mothership: %s", e)


def main():
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        print("ERROR: config.json not found!")
        sys.exit(1)
    
    with open(config_path, 'r') as f:
        config = json.load(f)
    
    try:
        asyncio.run(run_with_config(config))
    except KeyboardInterrupt:
        print("\nShutdown.")


if __name__ == "__main__":
    main()
