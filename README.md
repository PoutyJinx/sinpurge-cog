# sinpurge-cog

SIN Corporation moderation tool for Red-DiscordBot.

## Setup

Set the mod-log channel for purge reports:

```txt
[p]sinpurgeset modlog #mod-log
```

Show the current mod-log channel:

```txt
[p]sinpurgeset
```

Clear the mod-log channel:

```txt
[p]sinpurgeset clear
```

Slash setup is also available:

```txt
/sinpurgeset
```

## Prefix usage

Current channel only, default 5-minute timeout:

```txt
[p]sinpurge @User 1h
[p]sinpurge Username 30m
[p]sinpurge User With Spaces 5h
[p]sinpurge 123456789012345678 1d
[p]sinpurge BannedScammerName 1h
```

All accessible channels, including text channels, active threads, and voice-channel chats:

```txt
[p]sinpurge @User 1h all
[p]sinpurge Username 5h all
[p]sinpurge User With Spaces 1d all
[p]sinpurge 123456789012345678 1d all
[p]sinpurge BannedScammerName 1d all
```

No timeout:

```txt
[p]sinpurge @User 1h all none
[p]sinpurge @User 1h no-timeout
```

Custom timeout:

```txt
[p]sinpurge @User 1h all 10m
[p]sinpurge @User 1h timeout=30m
[p]sinpurge @User 1d all 1h
```

Old `true` syntax still works for all channels:

```txt
[p]sinpurge @User 1h true
```

## Slash command

```txt
/sinpurge
```

Options:

- Target: mention, username, display name, or user ID
- Timeframe: 30m, 1h, 5h, 12h, 1d, 7d, or 14d
- Scope: current channel or all accessible channels
- Timeout: default 5 minutes, no timeout, 1 minute, 10 minutes, 30 minutes, 1 hour, or 1 day

The slash command uses a text target field so moderators can purge users who are already banned or have left the server. Use a user ID when possible for banned scammers. Exact username/display-name matching is supported as a fallback.

## Mod-log reports

When a mod-log channel is configured, every purge sends a report containing:

- Moderator
- Target and target ID when available
- Purge window
- Scope
- Timeout result
- Deleted message count
- Scanned channel count
- Skipped channel count
- Command channel

## Notes

- Requires Manage Messages.
- Timeout requires Timeout Members for both the moderator and the bot.
- The bot role must be above the target role to apply timeouts.
- Bot needs View Channel, Read Message History, and Manage Messages in scanned channels.
- Maximum purge range is 14 days due to Discord limitations.
- Maximum timeout duration is 28 days.
- If the target is already banned or has left, timeout is skipped, but purge can still run by user ID or exact name.
