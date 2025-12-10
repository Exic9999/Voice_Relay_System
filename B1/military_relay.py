#!/usr/bin/env python3
"""
MILITARY HIERARCHY VOICE RELAY SYSTEM
=====================================
Commands work in #relay-chat text channel.

FIXED: Proper audio relay so drone bots light up green when commander speaks.
"""

import asyncio
import logging
import sys
import json
import threading
import wave
import io
import struct
import time
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

try:
    import speech_recognition as sr
    SPEECH_RECOGNITION_AVAILABLE = True
except ImportError:
    SPEECH_RECOGNITION_AVAILABLE = False
    print("WARNING: speech_recognition not installed - transcription disabled")

# ===================================================================================
# LOGGING
# ===================================================================================

logger = logging.getLogger("MilitaryRelay")

transcription_callback: Optional[Callable[[str, str, str], None]] = None

def set_transcription_callback(callback):
    global transcription_callback
    transcription_callback = callback

def log_transcription(speaker_name: str, speaker_role: str, text: str):
    logger.info("[%s] %s: \"%s\"", speaker_role, speaker_name, text)
    if transcription_callback:
        try:
            transcription_callback(speaker_name, speaker_role, text)
        except:
            pass

def setup_logging(level: str = "DEBUG"):
    log_level = getattr(logging, level.upper(), logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s', datefmt='%H:%M:%S')
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.setLevel(log_level)
    root = logging.getLogger()
    root.setLevel(log_level)
    root.addHandler(handler)
    
    # Suppress noisy discord logs
    logging.getLogger('discord').setLevel(logging.WARNING)
    logging.getLogger('discord.player').setLevel(logging.ERROR)
    logging.getLogger('discord.voice_client').setLevel(logging.ERROR)
    for name in ['discord.ext.voice_recv', 'voice_recv', 'discord.opus']:
        logging.getLogger(name).setLevel(logging.ERROR)

# ===================================================================================
# AUDIO CONSTANTS
# ===================================================================================

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2  # 16-bit
FRAME_DURATION_MS = 20
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)  # 960 samples
BYTES_PER_FRAME = FRAME_SIZE * CHANNELS * SAMPLE_WIDTH    # 3840 bytes

# SILENCE_FRAME must be exactly 3840 bytes of zeros
SILENCE_FRAME = b'\x00' * BYTES_PER_FRAME

TRIGGER_START = "boys"
TRIGGER_STOP = "end comms"
TRIGGER_UPLINK = "commander"

SILENCE_THRESHOLD = 500
COMMANDER_SILENCE_TIMEOUT = 5.0
SQUAD_UPLINK_TIMEOUT = 1.0

# Buffer configuration
DEFAULT_BUFFER_FRAMES = 150      # ~3 seconds buffer capacity
MIN_PREBUFFER_FRAMES = 5         # Minimum frames before starting playback (~100ms)

# ===================================================================================
# GLOBAL STATE
# ===================================================================================

squad_uplink_timeout: float = SQUAD_UPLINK_TIMEOUT

def set_squad_uplink_timeout(seconds: float):
    global squad_uplink_timeout
    squad_uplink_timeout = seconds

class StreamingAudioBuffer:
    """
    Thread-safe audio buffer optimized for streaming relay.
    
    Key features:
    - Pre-buffering before playback starts
    - Continuous audio flow with silence padding when needed
    - Separate active states for writing and reading
    """
    
    def __init__(self, name: str, max_frames: int = DEFAULT_BUFFER_FRAMES):
        self.name = name
        self.max_frames = max_frames
        self._buffer: deque = deque(maxlen=max_frames)
        self._write_active = False
        self._read_active = False
        self._lock = threading.Lock()
        self._frames_written = 0
        self._frames_read = 0
        self._prebuffer_ready = threading.Event()
    
    def start_writing(self):
        """Enable writing to buffer (called when commander starts speaking)."""
        with self._lock:
            self._buffer.clear()
            self._write_active = True
            self._read_active = False
            self._frames_written = 0
            self._frames_read = 0
            self._prebuffer_ready.clear()
        logger.debug("[%s] Write ACTIVE", self.name)
    
    def start_reading(self):
        """Enable reading from buffer (called when drones start playing)."""
        with self._lock:
            self._read_active = True
        logger.debug("[%s] Read ACTIVE", self.name)
    
    def stop(self):
        """Stop both writing and reading."""
        with self._lock:
            self._write_active = False
            self._read_active = False
            # Don't clear buffer yet - let remaining audio play out
        self._prebuffer_ready.set()  # Unblock any waiting readers
        logger.debug("[%s] STOPPED", self.name)
    
    def push(self, data: bytes) -> bool:
        """Push audio frame to buffer. Returns True if successful."""
        with self._lock:
            if not self._write_active:
                return False
            
            self._buffer.append(data)
            self._frames_written += 1
            
            # Signal when pre-buffer is ready
            if self._frames_written >= MIN_PREBUFFER_FRAMES and not self._prebuffer_ready.is_set():
                self._prebuffer_ready.set()
                logger.debug("[%s] Pre-buffer ready (%d frames)", self.name, self._frames_written)
            
            return True
    
    def pop(self) -> bytes:
        """
        Pop audio frame from buffer.
        Returns actual audio if available, otherwise returns silence.
        """
        with self._lock:
            if self._buffer:
                self._frames_read += 1
                return self._buffer.popleft()
            elif self._read_active or self._write_active:
                # Still active but buffer empty - return silence to maintain stream
                return SILENCE_FRAME
            else:
                # Completely stopped
                return SILENCE_FRAME
    
    def wait_for_prebuffer(self, timeout: float = 1.0) -> bool:
        """Wait until pre-buffer has enough frames. Returns True if ready."""
        return self._prebuffer_ready.wait(timeout=timeout)
    
    @property
    def size(self) -> int:
        with self._lock:
            return len(self._buffer)
    
    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._write_active or self._read_active
    
    def get_stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._buffer),
                "written": self._frames_written,
                "read": self._frames_read,
                "write_active": self._write_active,
                "read_active": self._read_active
            }


# Global buffers
broadcast_buffer: Optional[StreamingAudioBuffer] = None
uplink_buffer: Optional[StreamingAudioBuffer] = None

# ===================================================================================
# VOICE TRIGGER DETECTOR
# ===================================================================================

class VoiceTriggerDetector:
    """Detects voice trigger phrases and transcribes speech."""
    
    def __init__(self, triggers: list, speaker_name: str = "Unknown", speaker_role: str = "UNKNOWN"):
        self.triggers = [t.lower() for t in triggers]
        self.speaker_name = speaker_name
        self.speaker_role = speaker_role
        self.recognizer = sr.Recognizer() if SPEECH_RECOGNITION_AVAILABLE else None
        self._buffer = b''
        self._max_bytes = int(SAMPLE_RATE * 2 * 2 * 2.5)  # ~2.5 seconds
        self._last_check = 0
        self._last_text = ""
    
    def update_speaker(self, name: str, role: str):
        self.speaker_name = name
        self.speaker_role = role
    
    def feed_audio(self, pcm: bytes) -> Optional[str]:
        """Feed audio and check for trigger words. Returns trigger if found."""
        if not SPEECH_RECOGNITION_AVAILABLE:
            return None
        
        self._buffer += pcm
        if len(self._buffer) > self._max_bytes:
            self._buffer = self._buffer[-self._max_bytes:]
        
        now = time.time()
        if now - self._last_check < 1.0:
            return None
        self._last_check = now
        
        if len(self._buffer) < self._max_bytes // 2:
            return None
        
        try:
            text = self._recognize(self._buffer)
            if text and text != self._last_text:
                self._last_text = text
                log_transcription(self.speaker_name, self.speaker_role, text)
                
                text_lower = text.lower()
                for trigger in self.triggers:
                    if trigger in text_lower:
                        self._buffer = b''
                        return trigger
        except:
            pass
        return None
    
    def transcribe_only(self, pcm: bytes) -> Optional[str]:
        """Transcribe without checking triggers."""
        if not SPEECH_RECOGNITION_AVAILABLE:
            return None
        
        self._buffer += pcm
        if len(self._buffer) > self._max_bytes:
            self._buffer = self._buffer[-self._max_bytes:]
        
        now = time.time()
        if now - self._last_check < 1.0:
            return None
        self._last_check = now
        
        if len(self._buffer) < self._max_bytes // 2:
            return None
        
        try:
            text = self._recognize(self._buffer)
            if text and text != self._last_text:
                self._last_text = text
                log_transcription(self.speaker_name, self.speaker_role, text)
                return text
        except:
            pass
        return None
    
    def _recognize(self, pcm: bytes) -> Optional[str]:
        """Convert PCM to text using Google Speech Recognition."""
        if not self.recognizer:
            return None
        try:
            # Stereo to mono conversion
            samples = len(pcm) // 4
            mono = []
            for i in range(samples):
                left = struct.unpack_from('<h', pcm, i * 4)[0]
                right = struct.unpack_from('<h', pcm, i * 4 + 2)[0]
                mono.append(struct.pack('<h', (left + right) // 2))
            mono_data = b''.join(mono)
            
            # Create WAV in memory
            wav_buf = io.BytesIO()
            with wave.open(wav_buf, 'wb') as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(mono_data)
            wav_buf.seek(0)
            
            with sr.AudioFile(wav_buf) as source:
                audio = self.recognizer.record(source)
                return self.recognizer.recognize_google(audio)
        except sr.UnknownValueError:
            return None
        except sr.RequestError:
            return None
        except:
            return None
    
    def check_silence(self, pcm: bytes) -> bool:
        """Check if audio frame is silence."""
        samples = struct.unpack(f'<{len(pcm)//2}h', pcm)
        rms = (sum(s*s for s in samples) / len(samples)) ** 0.5
        return rms < SILENCE_THRESHOLD
    
    def clear(self):
        self._buffer = b''
        self._last_text = ""

# ===================================================================================
# STREAMING AUDIO SOURCE FOR PLAYBACK
# ===================================================================================

class StreamingAudioSource(discord.AudioSource):
    """
    Audio source that reads from a streaming buffer.
    
    This provides continuous PCM audio to discord.py's voice player.
    When the buffer is empty, it returns silence to keep the stream alive.
    """
    
    def __init__(self, buffer: StreamingAudioBuffer, name: str = ""):
        self.buffer = buffer
        self.name = name
        self._running = True
        self._read_count = 0
        logger.debug("[%s AudioSource] Created", self.name)
    
    def read(self) -> bytes:
        """
        Read exactly 3840 bytes (20ms of 48kHz stereo PCM).
        This is called by discord.py's voice player thread.
        """
        if not self._running:
            return b''
        
        data = self.buffer.pop()
        self._read_count += 1
        
        # Debug logging every 50 frames (~1 second)
        if self._read_count % 50 == 0:
            stats = self.buffer.get_stats()
            logger.debug("[%s AudioSource] Reads: %d, Buffer: %d frames", 
                        self.name, self._read_count, stats['size'])
        
        return data
    
    def is_opus(self) -> bool:
        """Return False to indicate we're providing PCM, not Opus."""
        return False
    
    def cleanup(self):
        """Called when playback stops."""
        self._running = False
        logger.debug("[%s AudioSource] Cleanup, total reads: %d", self.name, self._read_count)

# ===================================================================================
# VOICE SINKS (Audio Capture)
# ===================================================================================

class CommanderVoiceSink(voice_recv.AudioSink):
    """
    Captures commander's voice.
    - Listens for "BOYS" to start broadcasting
    - Listens for "END COMMS" to stop broadcasting
    - Pushes audio to broadcast buffer while active
    """
    
    def __init__(self, buffer: StreamingAudioBuffer, target_id: int, name: str, callback):
        self.buffer = buffer
        self.target_id = target_id
        self.name = name
        self.callback = callback
        self._listening = True
        self._broadcasting = False
        self._accumulator = b''
        self._silence_frames = 0
        self._silence_max = int(COMMANDER_SILENCE_TIMEOUT * 1000 / FRAME_DURATION_MS)
        self._audio_frames_sent = 0
        self.detector = VoiceTriggerDetector([TRIGGER_START, TRIGGER_STOP], name, "COMMANDER")
    
    def update_commander(self, user_id: int, name: str):
        self.target_id = user_id
        self.name = name
        self.detector.update_speaker(name, "COMMANDER")
        if self._broadcasting:
            self._broadcasting = False
            self.buffer.stop()
    
    def wants_opus(self) -> bool:
        return False
    
    def write(self, user, data):
        """Called by voice_recv when audio data is received."""
        if not self._listening:
            return
        if user is None or user.id != self.target_id:
            return
        
        pcm = data.pcm
        if pcm is None:
            return
        
        # Accumulate until we have a full frame
        self._accumulator += pcm
        
        while len(self._accumulator) >= BYTES_PER_FRAME:
            frame = self._accumulator[:BYTES_PER_FRAME]
            self._accumulator = self._accumulator[BYTES_PER_FRAME:]
            
            if not self._broadcasting:
                # Not broadcasting - listen for "BOYS" trigger
                trigger = self.detector.feed_audio(frame)
                
                if trigger == TRIGGER_START:
                    logger.info("🎙️ BOYS detected - Starting broadcast")
                    self._broadcasting = True
                    self._audio_frames_sent = 0
                    self.buffer.start_writing()
                    # Push first frame immediately
                    self.buffer.push(frame)
                    self._audio_frames_sent += 1
                    self._silence_frames = 0
                    self.detector.clear()
                    # Notify callback (this starts the drones)
                    asyncio.create_task(self.callback("start"))
            else:
                # Currently broadcasting
                
                # Check for "END COMMS" trigger
                trigger = self.detector.feed_audio(frame)
                if trigger == TRIGGER_STOP:
                    logger.info("🛑 END COMMS detected - Stopping broadcast (sent %d frames)", 
                              self._audio_frames_sent)
                    self._broadcasting = False
                    self.buffer.stop()
                    asyncio.create_task(self.callback("stop"))
                    self.detector.clear()
                    continue
                
                # Check for silence timeout
                is_silence = self.detector.check_silence(frame)
                if is_silence:
                    self._silence_frames += 1
                    if self._silence_frames > self._silence_max:
                        logger.info("⏸️ Auto-stop due to silence (sent %d frames)", 
                                  self._audio_frames_sent)
                        self._broadcasting = False
                        self.buffer.stop()
                        asyncio.create_task(self.callback("stop"))
                        continue
                else:
                    self._silence_frames = 0
                
                # Push audio to buffer for relay
                self.buffer.push(frame)
                self._audio_frames_sent += 1
                
                # Debug logging every 50 frames
                if self._audio_frames_sent % 50 == 0:
                    logger.debug("Commander audio: %d frames sent, buffer size: %d", 
                               self._audio_frames_sent, self.buffer.size)
    
    def cleanup(self):
        self._listening = False
        if self._broadcasting:
            self._broadcasting = False
            self.buffer.stop()


class SquadVoiceSink(voice_recv.AudioSink):
    """Captures squad member voice for uplink to commander."""
    
    def __init__(self, buffer: StreamingAudioBuffer, drone_name: str, callback):
        self.buffer = buffer
        self.drone_name = drone_name
        self.callback = callback
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
                    self.buffer.start_writing()
                    self.buffer.push(frame)
                    self._silence_frames = 0
                    asyncio.create_task(self.callback("start", user))
                    self.detector.clear()
            elif self._uplinking and user.id == self._uplink_user_id:
                self.detector.transcribe_only(frame)
                
                if self.detector.check_silence(frame):
                    self._silence_frames += 1
                    if self._silence_frames > self._silence_max():
                        logger.info("[%s] Uplink auto-ended", self.drone_name)
                        self._uplinking = False
                        self._uplink_user_id = None
                        self.buffer.stop()
                        asyncio.create_task(self.callback("stop", user))
                        self.detector.clear()
                        continue
                else:
                    self._silence_frames = 0
                self.buffer.push(frame)
    
    def cleanup(self):
        self._listening = False
        if self._uplinking:
            self._uplinking = False
            self.buffer.stop()

# ===================================================================================
# MOTHERSHIP BOT (Commander's Bot)
# ===================================================================================

class MothershipBot(commands.Bot):
    """Main bot that listens to commander and coordinates drones."""
    
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True
        intents.members = True
        
        super().__init__(command_prefix="!", intents=intents)
        
        self.commander_user_id = None
        self.commander_name = None
        self.drone_bots: Dict[str, 'DroneBot'] = {}
        self.voice_client = None
        self.commander_sink = None
        self.is_broadcasting = False
        self._admin_list_cache = []
        
        logger.info("MothershipBot initialized")
    
    async def setup_hook(self):
        self.remove_command('help')
    
    async def on_ready(self):
        logger.info("=" * 50)
        logger.info("MOTHERSHIP ONLINE: %s", self.user.name)
        logger.info("Guilds: %s", [g.name for g in self.guilds])
        logger.info("=" * 50)
        logger.info("⚠️  NO COMMANDER SET")
        logger.info("Type !list in #relay-chat, then !setcommander [#]")
        logger.info("=" * 50)
        
        # Check if commander_user_id was pre-configured
        if self.commander_user_id:
            for guild in self.guilds:
                member = guild.get_member(self.commander_user_id)
                if member:
                    self.commander_name = member.display_name
                    if member.voice and member.voice.channel:
                        await self._join_channel(member.voice.channel)
                    break
    
    async def on_message(self, message):
        if message.author.bot:
            return
        
        if message.channel.name != "relay-chat":
            return
        
        content = message.content.strip()
        
        if content == "!list":
            await self._cmd_list(message)
        elif content.startswith("!setcommander"):
            parts = content.split(maxsplit=1)
            target = parts[1] if len(parts) > 1 else None
            await self._cmd_setcommander(message, target)
        elif content == "!commander":
            await self._cmd_commander(message)
        elif content == "!status":
            await self._cmd_status(message)
        elif content == "!stop":
            await self._cmd_stop(message)
        elif content == "!help":
            await self._cmd_help(message)
        elif content == "!debug":
            await self._cmd_debug(message)
    
    async def _cmd_list(self, message):
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
        if not target:
            await message.channel.send("Usage: `!setcommander [#]` or `!setcommander @user`\nRun `!list` first.")
            return
        
        member = None
        
        if target.isdigit():
            idx = int(target) - 1
            if not self._admin_list_cache:
                await message.channel.send("Run `!list` first.")
                return
            if idx < 0 or idx >= len(self._admin_list_cache):
                await message.channel.send(f"Invalid number. Use 1-{len(self._admin_list_cache)}")
                return
            member = self._admin_list_cache[idx]
        elif target.startswith('<@') and target.endswith('>'):
            user_id_str = target.replace('<@', '').replace('>', '').replace('!', '')
            try:
                user_id = int(user_id_str)
                member = message.guild.get_member(user_id)
            except:
                pass
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
        
        self.commander_user_id = member.id
        self.commander_name = member.display_name
        
        if self.commander_sink:
            self.commander_sink.update_commander(member.id, member.display_name)
        
        logger.info("Commander set: %s (ID: %d)", member.display_name, member.id)
        
        if member.voice and member.voice.channel:
            await self._join_channel(member.voice.channel)
            await message.channel.send(
                f"✅ **{member.display_name}** is now Commander! (ID: `{member.id}`)\n"
                f"🎙️ Listening... Say **\"BOYS\"** to broadcast, **\"END COMMS\"** to stop."
            )
        else:
            await message.channel.send(
                f"✅ **{member.display_name}** is now Commander! (ID: `{member.id}`)\n"
                f"⚠️ Join a voice channel, then say **\"BOYS\"** to broadcast."
            )
    
    async def _cmd_commander(self, message):
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
            await message.channel.send(
                f"👑 Commander: **{commander.display_name}** (ID: {self.commander_user_id})\nStatus: {status}"
            )
        else:
            await message.channel.send(f"👑 Commander ID: {self.commander_user_id}\nStatus: {status}")
    
    async def _cmd_status(self, message):
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
        
        if broadcast_buffer:
            stats = broadcast_buffer.get_stats()
            lines.append(f"📊 Buffer: {stats['size']} frames, W:{stats['written']} R:{stats['read']}")
        
        lines.append("")
        
        for name, drone in self.drone_bots.items():
            if drone.voice_client and drone.voice_client.is_connected():
                if drone.voice_client.is_playing():
                    status = "🎙️ TRANSMITTING"
                elif self.is_broadcasting:
                    status = "📡 Broadcast active"
                else:
                    status = "✅ Connected (standby)"
            else:
                status = "❌ Not connected"
            lines.append(f"🤖 {name}: {status}")
        
        await message.channel.send("\n".join(lines))
    
    async def _cmd_debug(self, message):
        """Debug command to check audio system status."""
        lines = ["**🔧 DEBUG INFO**", ""]
        
        lines.append(f"voice_recv available: {VOICE_RECV_AVAILABLE}")
        lines.append(f"speech_recognition available: {SPEECH_RECOGNITION_AVAILABLE}")
        lines.append(f"Opus loaded: {discord.opus.is_loaded()}")
        
        if not discord.opus.is_loaded():
            lines.append("⚠️ OPUS NOT LOADED - Audio may not work!")
        
        lines.append("")
        lines.append(f"Commander sink active: {self.commander_sink is not None}")
        lines.append(f"Mothership voice client: {self.voice_client is not None}")
        if self.voice_client:
            lines.append(f"  - Connected: {self.voice_client.is_connected()}")
        
        lines.append("")
        for name, drone in self.drone_bots.items():
            lines.append(f"{name}:")
            lines.append(f"  - Voice client: {drone.voice_client is not None}")
            if drone.voice_client:
                lines.append(f"  - Connected: {drone.voice_client.is_connected()}")
                lines.append(f"  - Playing: {drone.voice_client.is_playing()}")
        
        await message.channel.send("\n".join(lines))
    
    async def _cmd_stop(self, message):
        if not self.is_broadcasting:
            await message.channel.send("⚠️ Not currently broadcasting.")
            return
        
        await self._stop_broadcast()
        await message.channel.send("🛑 **Broadcast stopped.**")
    
    async def _cmd_help(self, message):
        await message.channel.send("""**📻 RELAY COMMANDS**

**Setup:**
`!list` - View eligible commanders
`!setcommander [#]` - Set commander by number
`!setcommander @user` - Set by mention
`!stop` - Stop broadcasting
`!debug` - Show debug info

**Info:**
`!commander` - Show current commander
`!status` - Show system status

**Voice:**
🎙️ Commander says **"BOYS"** → Start broadcast
🛑 Commander says **"END COMMS"** → Stop broadcast
📡 Squad says **"COMMANDER"** → Talk to Commander
""")
    
    async def on_voice_state_update(self, member, before, after):
        if not self.commander_user_id:
            return
        
        if member.id != self.commander_user_id:
            return
        
        if after.channel and (not before.channel or before.channel != after.channel):
            self.commander_name = member.display_name
            logger.info("Commander %s joined %s", member.display_name, after.channel.name)
            await self._join_channel(after.channel)
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
            
            self.commander_sink = CommanderVoiceSink(
                broadcast_buffer, 
                self.commander_user_id, 
                self.commander_name or "Commander",
                self._on_trigger
            )
            self.voice_client.listen(self.commander_sink)
            logger.info("Listening for commander voice...")
        except Exception as e:
            logger.error("Failed to join channel: %s", e)
    
    async def _on_trigger(self, action: str):
        if action == "start":
            await self._start_broadcast()
        elif action == "stop":
            await self._stop_broadcast()
    
    async def _start_broadcast(self):
        """Start broadcasting commander's voice to all drones."""
        if self.is_broadcasting:
            return
        
        self.is_broadcasting = True
        logger.info("=" * 40)
        logger.info("📡 BROADCAST STARTING")
        logger.info("=" * 40)
        
        # Wait for pre-buffer to fill (ensures smooth start)
        logger.debug("Waiting for pre-buffer...")
        broadcast_buffer.wait_for_prebuffer(timeout=0.5)
        
        # Enable reading from buffer
        broadcast_buffer.start_reading()
        
        # Start all drones
        for name, drone in self.drone_bots.items():
            success = await drone.start_broadcast()
            if success:
                logger.info("[%s] Broadcasting", name)
            else:
                logger.error("[%s] Failed to start broadcast", name)
    
    async def _stop_broadcast(self):
        """Stop broadcasting to all drones."""
        if not self.is_broadcasting:
            return
        
        self.is_broadcasting = False
        broadcast_buffer.stop()
        
        for name, drone in self.drone_bots.items():
            try:
                await drone.stop_broadcast()
            except Exception as e:
                logger.error("[%s] Failed to stop: %s", name, e)
        
        logger.info("=" * 40)
        logger.info("🛑 BROADCAST STOPPED")
        logger.info("=" * 40)
    
    async def start_uplink_audio(self):
        """Play squad uplink audio to commander."""
        if self.voice_client and not self.voice_client.is_playing():
            uplink_buffer.start_reading()
            source = StreamingAudioSource(uplink_buffer, "Uplink")
            self.voice_client.play(source)
    
    async def stop_uplink_audio(self):
        """Stop squad uplink audio."""
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        uplink_buffer.stop()
    
    async def _cleanup(self):
        self.is_broadcasting = False
        if self.commander_sink:
            self.commander_sink.cleanup()
            self.commander_sink = None
        if self.voice_client:
            try:
                self.voice_client.stop_listening()
            except:
                pass
            try:
                await self.voice_client.disconnect()
            except:
                pass
            self.voice_client = None

# ===================================================================================
# DRONE BOT
# ===================================================================================

class DroneBot(commands.Bot):
    """Bot that joins squad channel and relays commander's audio."""
    
    def __init__(self, name: str, channel_id: int, mothership: MothershipBot):
        intents = discord.Intents.default()
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
        await asyncio.sleep(2)  # Wait for full initialization
        await self._connect_to_channel()
    
    async def _connect_to_channel(self):
        """Connect to assigned voice channel."""
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
            
            # Set up listening for squad uplinks
            self.squad_sink = SquadVoiceSink(uplink_buffer, self.drone_name, self._on_squad_trigger)
            self.voice_client.listen(self.squad_sink)
            logger.info("[%s] Ready for broadcast relay", self.drone_name)
            
            return True
        except Exception as e:
            logger.error("[%s] Failed to connect: %s", self.drone_name, e)
            import traceback
            traceback.print_exc()
            return False
    
    async def start_broadcast(self) -> bool:
        """Start playing broadcast audio from commander."""
        if not self.voice_client or not self.voice_client.is_connected():
            logger.warning("[%s] Not connected, cannot broadcast", self.drone_name)
            return False
        
        try:
            # Stop any current playback
            if self.voice_client.is_playing():
                self.voice_client.stop()
                await asyncio.sleep(0.1)  # Brief pause
            
            # Create audio source from broadcast buffer
            self.audio_source = StreamingAudioSource(broadcast_buffer, self.drone_name)
            
            # Start playing - this makes the bot icon "light up green"
            self.voice_client.play(
                self.audio_source,
                after=lambda e: logger.debug("[%s] Playback ended: %s", self.drone_name, e) if e else None
            )
            
            logger.info("[%s] 🟢 Broadcasting started - bot should be speaking", self.drone_name)
            return True
            
        except Exception as e:
            logger.error("[%s] Broadcast failed: %s", self.drone_name, e)
            import traceback
            traceback.print_exc()
            return False
    
    async def _on_squad_trigger(self, action: str, user):
        """Handle squad uplink triggers."""
        if action == "start":
            # Pause broadcast to receive uplink
            if self.voice_client and self.voice_client.is_playing():
                self.voice_client.stop()
            await self.mothership.start_uplink_audio()
        elif action == "stop":
            await self.mothership.stop_uplink_audio()
            # Resume broadcast if still active
            if self.mothership.is_broadcasting and self.voice_client:
                await self.start_broadcast()
    
    async def stop_broadcast(self):
        """Stop playing broadcast audio."""
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        if self.audio_source:
            self.audio_source.cleanup()
            self.audio_source = None
        logger.info("[%s] 🔴 Broadcast stopped", self.drone_name)
    
    async def disconnect(self):
        """Fully disconnect from voice channel."""
        if self.squad_sink:
            self.squad_sink.cleanup()
            self.squad_sink = None
        if self.voice_client:
            try:
                self.voice_client.stop_listening()
            except:
                pass
            if self.voice_client.is_playing():
                self.voice_client.stop()
            try:
                await self.voice_client.disconnect()
            except:
                pass
            self.voice_client = None
        logger.info("[%s] Disconnected", self.drone_name)

# ===================================================================================
# MAIN ENTRY POINTS
# ===================================================================================

async def run_with_config(config: dict, stop_event: threading.Event = None):
    """Run the relay system with provided configuration."""
    global broadcast_buffer, uplink_buffer, squad_uplink_timeout
    
    setup_logging(config.get("log_level", "DEBUG"))
    
    squad_uplink_timeout = config.get("squad_uplink_timeout", 1.0)
    
    logger.info("=" * 60)
    logger.info("MILITARY HIERARCHY VOICE RELAY SYSTEM")
    logger.info("=" * 60)
    logger.info("")
    logger.info("All 3 bots connecting simultaneously...")
    logger.info("")
    logger.info("HOW IT WORKS:")
    logger.info("  1. Type !list in #relay-chat")
    logger.info("  2. Type !setcommander [#] (auto-detects User ID!)")
    logger.info("  3. Commander says BOYS to start broadcasting")
    logger.info("")
    logger.info("Voice triggers:")
    logger.info("  'BOYS'        - Start broadcast")
    logger.info("  'END COMMS'   - Stop broadcast")
    logger.info("  'COMMANDER'   - Squad uplink")
    logger.info("")
    
    # Initialize buffers with improved streaming implementation
    buffer_frames = config.get("max_buffer_frames", DEFAULT_BUFFER_FRAMES)
    broadcast_buffer = StreamingAudioBuffer("BROADCAST", buffer_frames)
    uplink_buffer = StreamingAudioBuffer("UPLINK", buffer_frames)
    
    # Create bots
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
            logger.info("Stop signal received, shutting down...")
            for bot in [mothership, alpha, bravo]:
                try:
                    await bot.close()
                except:
                    pass
    
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
        # Cleanup
        for bot in [alpha, bravo]:
            try:
                await bot.disconnect()
                await bot.close()
            except:
                pass
        try:
            await mothership._cleanup()
            await mothership.close()
        except:
            pass


def main():
    """Standalone entry point."""
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
