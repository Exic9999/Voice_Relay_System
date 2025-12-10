# Voice Relay System (VRS)

Voice-activated Discord relay system for commander-to-squad communication.

## Features

- **Auto User ID** - No need to configure your User ID, just select commander via `!setcommander`
- **Real-time transcription** - Commander's orders appear in the GUI log
- **Voice-activated** - Say "BOYS" to start, "END COMMS" to stop
- **Squad uplinks** - Squad members can talk to commander
- **Multi-channel** - Broadcast to Alpha and Bravo simultaneously

## Quick Start

1. Run `install_and_run.bat`
2. In Discord **#relay-chat**, type `!list`
3. Type `!setcommander 1` (system auto-detects the User ID!)
4. Commander says **"BOYS"** to start broadcasting!
5. **Commander's orders appear in the GUI log!**

## Commands (in #relay-chat)

| Command | What it does |
|---------|--------------|
| `!list` | View eligible commanders |
| `!setcommander 1` | Set commander (auto-detects User ID) |
| `!stop` | Stop broadcasting |
| `!commander` | Show current commander & ID |
| `!status` | Show system status |

## Voice Commands

| Who | Say This | What Happens |
|-----|----------|--------------|
| Commander | **"BOYS"** | Start broadcasting |
| Commander | **"END COMMS"** | Stop broadcasting |
| Squad | **"COMMANDER"** | Talk to Commander |

## Transcription

When broadcasting, the commander's speech is transcribed and displayed in the GUI log window:

```
[14:32:15] 👑 [ORDER] Commander: "Alpha team move to position"
[14:32:18] 👑 [ORDER] Commander: "Bravo team provide cover"
```

Squad uplinks are also transcribed:

```
[14:32:45] 📡 [UPLINK] JohnDoe: "Commander we need backup"
```

## Requirements

- Text channel named **#relay-chat**
- 3 Discord bots with voice permissions
- Python 3.10+
- FFmpeg

## Config

Edit `config.json` with your bot tokens and channel IDs:

```json
{
    "commander_token": "your-mothership-bot-token",
    "drone_alpha_token": "your-alpha-bot-token",
    "drone_bravo_token": "your-bravo-bot-token",
    "drone_alpha_channel_id": "alpha-voice-channel-id",
    "drone_bravo_channel_id": "bravo-voice-channel-id"
}
```

**Note:** You no longer need to configure `commander_user_id`. The system automatically detects it when you use `!setcommander`.
