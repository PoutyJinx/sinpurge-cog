# sinpurge-cog

SIN Corporation moderation tool for Red-DiscordBot.

## Usage

Current channel only:

```txt
[p]sinpurge @User 1h
[p]sinpurge Username 1h
[p]sinpurge User With Spaces 1h
[p]sinpurge 123456789012345678 1h
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

Old `true` syntax still works:

```txt
[p]sinpurge @User 1h true
```

## Slash command

```txt
/sinpurge
```

The slash command now uses a text target field so moderators can purge users who are already banned or have left the server.

Use a user ID when possible for banned scammers. Exact username/display-name matching is supported as a fallback.

## Notes

- Requires Manage Messages.
- Bot needs View Channel, Read Message History, and Manage Messages in scanned channels.
- Maximum search range is 14 days due to Discord limitations.
- Name-only matching is useful for banned users, but user ID matching is safer.


## Automatic Timeout

When the target is still a current server member, SIN Purge attempts to apply a 5-minute timeout before deleting messages. This gives moderators time to ban scammers or raiders. The bot and moderator both need the `Timeout Members` permission, and the bot role must be above the target role. If the target is already banned or has left, timeout is skipped and the purge still runs by ID or exact name.
