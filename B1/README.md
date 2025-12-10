# Military Voice Relay System v5.0

Voice-activated Discord relay system for commander-to-squad communication. **Commander speaks, drone bots relay to squad channels!**

## What's Fixed in v5.0

- ✅ **Drone bots now properly light up green** when commander broadcasts
- ✅ **Improved audio streaming** with pre-buffering for smooth playback
- ✅ **Better synchronization** between commander capture and drone playback
- ✅ **Debug command** (`!debug`) to troubleshoot audio issues
- ✅ **Enhanced logging** to track audio flow

## How It Works

```
┌─────────────────┐     ┌──────────────┐     ┌─────────────────┐
│   Commander     │────▶│  Mothership  │────▶│  Alpha Drone    │
│ (Voice Channel) │     │    Bot       │     │ (Squad Alpha)   │
└─────────────────┘     │              │     └─────────────────┘
        │               │   Captures   │              │
        │               │   voice &    │              ▼
      BOYS!             │   relays     │     🟢 Bot speaks!
                        │              │     
                        │              │     ┌─────────────────┐
                        │              │────▶│  Bravo Drone    │
                        │              │     │ (Squad Bravo)   │
                        └──────────────┘     └─────────────────┘
                                                      │
                                                      ▼
                                             🟢 Bot speaks!
```

## Quick Start

1. **Run `install_and_run.bat`** (Windows)
2. **Edit `config.json`** with your bot tokens and channel IDs
3. In Discord **#relay-chat**, type `!list`
4. Type `!setcommander 1` (system auto-detects the User ID!)
5. Commander says **"BOYS"** to start broadcasting!
6. **Watch the drone bots light up green and relay your voice!**

## Requirements

- **Python 3.10+**
- **FFmpeg** (for audio encoding)
- **3 Discord Bots** with these permissions:
  - Connect to voice channels
  - Speak in voice channels
  - Read/Send messages in #relay-chat
- **Text channel** named `#relay-chat`

## Config Setup

Edit `config.json`:

```json
{
    "commander_token": "your-mothership-bot-token",
    "drone_alpha_token": "your-alpha-bot-token",
    "drone_bravo_token": "your-bravo-bot-token",
    "drone_alpha_channel_id": "alpha-voice-channel-id",
    "drone_bravo_channel_id": "bravo-voice-channel-id",
    "squad_uplink_timeout": 1.0,
    "max_buffer_frames": 150,
    "log_level": "DEBUG"
}
```

### Getting Channel IDs

1. Enable Developer Mode in Discord (Settings → App Settings → Advanced → Developer Mode)
2. Right-click a voice channel → Copy ID

### Getting Bot Tokens

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create 3 applications (Mothership, Alpha, Bravo)
3. For each: Bot → Reset Token → Copy

### Bot Permissions

Each bot needs these permissions:
- `Connect` - Join voice channels
- `Speak` - Transmit audio
- `Use Voice Activity` - Detect when speaking
- `View Channel` - See channels
- `Send Messages` - For #relay-chat

## Commands (in #relay-chat)

| Command | Description |
|---------|-------------|
| `!list` | View eligible commanders (admins) |
| `!setcommander 1` | Set commander by number from list |
| `!setcommander @user` | Set commander by mention |
| `!stop` | Stop broadcasting |
| `!status` | Show system status with buffer info |
| `!debug` | Show debug info (Opus status, connections) |
| `!commander` | Show current commander |
| `!help` | Show all commands |

## Voice Commands

| Who | Say This | What Happens |
|-----|----------|--------------|
| Commander | **"BOYS"** | Start broadcasting to all squads |
| Commander | **"END COMMS"** | Stop broadcasting |
| Squad | **"COMMANDER"** | Open uplink to talk to commander |

## Troubleshooting

### Bots Not Lighting Up Green

1. Run `!debug` in #relay-chat to check:
   - Is Opus loaded? (Required for audio)
   - Are drones connected?
   - Is the voice client playing?

2. Check FFmpeg is installed:
   ```
   ffmpeg -version
   ```

3. Make sure bots have `Speak` permission in the voice channels

### "BOYS" Not Being Detected

- Speak clearly and loudly
- Make sure SpeechRecognition is installed
- Check that Mothership bot is in the same voice channel as you

### Audio Choppy or Delayed

- Increase `max_buffer_frames` in config.json (default: 150)
- Check your internet connection
- Reduce squad_uplink_timeout if uplinks end too quickly

## Files

```
VoiceRelaySystem/
├── military_relay.py    # Core relay logic
├── gui.py               # GUI interface
├── config.json          # Your configuration
├── requirements.txt     # Python dependencies
├── install_and_run.bat  # Windows installer
└── README.md            # This file
```

## Technical Details

- **Audio Format**: 48kHz, 16-bit, Stereo PCM
- **Frame Size**: 3840 bytes (20ms)
- **Pre-buffer**: 5 frames (~100ms) before playback starts
- **Buffer Capacity**: 150 frames (~3 seconds)

## License

MIT License - Use freely for your Discord servers!
