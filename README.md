# SIN Purge Cog

A Red-DiscordBot moderation cog for purging recent messages from one specific user.

## Features

- Prefix command and slash/hybrid command support.
- Deletes recent messages from one user.
- Can purge only the current channel or all visible text channels.
- Checks moderator permissions.
- Checks bot permissions.
- Prevents purging yourself, the server owner, or users with equal/higher roles.
- Caps purge timeframes to 14 days because Discord bulk deletion does not support older messages.
- Sends a random SIN Corporation lore message after successful purges.

## Commands

```txt
[p]sinpurge @User 1h
[p]sinpurge @User 5h true
[p]sinpurge @User 2d false
/sinpurge
```

Slash command options:

- `target`: the user to purge
- `timeframe`: 30m, 1h, 5h, 12h, 1d, 7d, or 14d
- `all_channels`: true or false

## Required Discord permissions

The bot needs:

- View Channel
- Read Message History
- Manage Messages

Moderators need:

- Manage Messages

## Install through Red Downloader

Replace the GitHub URL with your real repository URL.

```txt
[p]repo add sinpurge-cog https://github.com/YOURNAME/sinpurge-cog
[p]cog install sinpurge-cog sinpurge
[p]load sinpurge
```

Then, if you use slash commands, sync them:

```txt
[p]slash sync
```

Depending on your Red setup, slash commands can take a little while to appear in Discord.
