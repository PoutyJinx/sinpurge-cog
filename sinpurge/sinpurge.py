import random
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

import discord
from discord import app_commands
from redbot.core import commands
from redbot.core.utils.chat_formatting import box


TIME_RE = re.compile(r"^(\d+)\s*(m|h|d)$", re.IGNORECASE)
ALL_KEYWORDS = {"true", "all", "yes", "y", "server", "global", "guild"}
CURRENT_KEYWORDS = {"false", "current", "channel", "here", "no", "n"}


class SinPurge(commands.Cog):
    """SIN Corporation moderation tool for purging a user's recent messages."""

    def __init__(self, bot):
        self.bot = bot

    # ---------- Helpers ----------

    def parse_timeframe(self, value: str) -> timedelta:
        match = TIME_RE.match(value.strip())
        if not match:
            raise ValueError("Invalid timeframe. Use `30m`, `1h`, `5h`, `1d`, etc.")

        amount = int(match.group(1))
        unit = match.group(2).lower()

        if amount <= 0:
            raise ValueError("Timeframe must be above 0.")

        if unit == "m":
            delta = timedelta(minutes=amount)
        elif unit == "h":
            delta = timedelta(hours=amount)
        elif unit == "d":
            delta = timedelta(days=amount)
        else:
            raise ValueError("Invalid timeframe unit. Use `m`, `h`, or `d`.")

        if delta > timedelta(days=14):
            raise ValueError("Discord only allows reliable cleanup within the last 14 days. Use `14d` or less.")

        return delta

    def parse_scope(self, value: Optional[str]) -> bool:
        """Return True for all-channel scan, False for current-channel only."""
        if value is None:
            return False

        cleaned = value.strip().lower()
        if cleaned in ALL_KEYWORDS:
            return True
        if cleaned in CURRENT_KEYWORDS:
            return False

        raise ValueError("Invalid scope. Use `all`/`true` for all channels, or leave it empty for current channel only.")

    async def resolve_member(self, ctx: commands.Context, user_text: str) -> discord.Member:
        """Resolve member from mention, ID, username, nickname, or spaced display name."""
        text = user_text.strip()

        # First use Discord.py's converter. This handles mentions, IDs, exact names, and many nickname cases.
        try:
            return await commands.MemberConverter().convert(ctx, text)
        except commands.BadArgument:
            pass

        # Fallback: case-insensitive exact matching for display name/name/global name.
        lowered = text.lower()
        matches = []
        for member in ctx.guild.members:
            names = {
                member.name.lower(),
                member.display_name.lower(),
            }
            if getattr(member, "global_name", None):
                names.add(member.global_name.lower())

            if lowered in names:
                matches.append(member)

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise commands.BadArgument(
                "Multiple members match that name. Use a mention or user ID instead."
            )

        raise commands.BadArgument(
            f'Member "{user_text}" not found. Try mentioning them, using their full display name, or using their user ID.'
        )

    def build_channel_list(self, guild: discord.Guild, announce_channel, all_channels: bool) -> List[discord.abc.GuildChannel]:
        """Include normal text channels, forum channels where supported, and voice-channel text chats."""
        if not all_channels:
            return [announce_channel]

        channels: List[discord.abc.GuildChannel] = []
        channels.extend(guild.text_channels)

        # Voice channels can have text chat/history in Discord. They are not part of guild.text_channels.
        channels.extend(guild.voice_channels)

        # Some Red/discord.py versions expose forum channels. We include them if available.
        forum_channels = getattr(guild, "forum_channels", [])
        channels.extend(forum_channels)

        # Remove duplicates while preserving order.
        seen = set()
        unique = []
        for channel in channels:
            channel_id = getattr(channel, "id", None)
            if channel_id is not None and channel_id not in seen:
                seen.add(channel_id)
                unique.append(channel)

        return unique

    def channel_supports_history(self, channel) -> bool:
        return hasattr(channel, "history") and hasattr(channel, "permissions_for")

    def lore_message(self, target: discord.Member, deleted: int) -> str:
        messages = [
            "SIN Corporation has audited {user}. {count} suspicious memo(s) were shredded.",
            "{user} has been escorted out of the Castle. {count} message(s) mysteriously vanished.",
            "CEO Jinx reviewed the paperwork. {user} failed the vibe check. {count} message(s) deleted.",
            "A Dweller cleanup crew swept away {count} piece(s) of nonsense from {user}.",
            "{user} has been transferred to the Department of Consequences. {count} message(s) purged.",
            "SIN Corp Security bonked {user} with the compliance clipboard. {count} message(s) removed.",
        ]
        return random.choice(messages).format(user=target.mention, count=deleted)

    def can_target_member(self, guild: discord.Guild, moderator: discord.Member, target: discord.Member) -> Tuple[bool, str]:
        if target == moderator:
            return False, "You cannot purge yourself. That paperwork is cursed."

        if target == guild.owner:
            return False, "The server owner is protected by forbidden HR magic."

        if target.top_role >= moderator.top_role and moderator != guild.owner:
            return False, "You cannot purge someone with an equal or higher role."

        return True, ""

    async def safe_delete_batch(self, channel, messages: List[discord.Message]) -> int:
        if not messages:
            return 0

        try:
            if len(messages) == 1:
                await messages[0].delete()
                return 1

            await channel.delete_messages(messages)
            return len(messages)
        except (discord.HTTPException, discord.Forbidden):
            deleted = 0
            for msg in messages:
                try:
                    await msg.delete()
                    deleted += 1
                except (discord.HTTPException, discord.Forbidden):
                    pass
            return deleted

    async def purge_user_messages(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        target: discord.Member,
        timeframe: str,
        all_channels: bool,
        announce_channel,
    ) -> Tuple[int, int, int]:
        delta = self.parse_timeframe(timeframe)
        after_time = datetime.now(timezone.utc) - delta

        channels = self.build_channel_list(guild, announce_channel, all_channels)

        total_deleted = 0
        scanned_channels = 0
        skipped_channels = 0

        for channel in channels:
            if not self.channel_supports_history(channel):
                skipped_channels += 1
                continue

            try:
                bot_perms = channel.permissions_for(guild.me)
                mod_perms = channel.permissions_for(moderator)
            except Exception:
                skipped_channels += 1
                continue

            if not bot_perms.view_channel or not bot_perms.read_message_history or not bot_perms.manage_messages:
                skipped_channels += 1
                continue

            if not mod_perms.view_channel or not mod_perms.manage_messages:
                skipped_channels += 1
                continue

            scanned_channels += 1
            batch: List[discord.Message] = []

            try:
                async for message in channel.history(limit=None, after=after_time, oldest_first=False):
                    if message.author.id == target.id:
                        batch.append(message)

                        if len(batch) >= 100:
                            total_deleted += await self.safe_delete_batch(channel, batch)
                            batch = []

                if batch:
                    total_deleted += await self.safe_delete_batch(channel, batch)

            except (discord.Forbidden, discord.HTTPException):
                skipped_channels += 1
            except Exception:
                skipped_channels += 1

        if total_deleted > 0:
            try:
                await announce_channel.send(self.lore_message(target, total_deleted))
            except (discord.HTTPException, discord.Forbidden):
                pass

        return total_deleted, scanned_channels, skipped_channels

    # ---------- Prefix command ----------

    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True, read_message_history=True)
    @commands.command(name="sinpurge", aliases=["purgeuser", "userpurge"])
    async def sinpurge_prefix(self, ctx: commands.Context, *, args: str):
        """
        Purge recent messages from a user.

        Examples:
        [p]sinpurge @User 1h
        [p]sinpurge Username 1h
        [p]sinpurge User With Spaces 1h
        [p]sinpurge 123456789012345678 1h all
        [p]sinpurge @User 5h true
        """
        parts = args.rsplit(maxsplit=2)

        if len(parts) < 2:
            return await ctx.send(
                "Usage: `!sinpurge @User 1h` or `!sinpurge User With Spaces 1h all`"
            )

        if len(parts) == 2:
            user_text, timeframe = parts
            scope_text = None
        else:
            possible_user_text, possible_timeframe, possible_scope = parts
            if possible_scope.lower() in ALL_KEYWORDS or possible_scope.lower() in CURRENT_KEYWORDS:
                user_text = possible_user_text
                timeframe = possible_timeframe
                scope_text = possible_scope
            else:
                # Allows names with two last words before timeframe, fallback format.
                user_text = f"{possible_user_text} {possible_timeframe}"
                timeframe = possible_scope
                scope_text = None

        try:
            all_channels = self.parse_scope(scope_text)
            self.parse_timeframe(timeframe)
        except ValueError as e:
            return await ctx.send(str(e))

        try:
            target = await self.resolve_member(ctx, user_text)
        except commands.BadArgument as e:
            return await ctx.send(str(e))

        allowed, reason = self.can_target_member(ctx.guild, ctx.author, target)
        if not allowed:
            return await ctx.send(reason)

        scope_msg = "all accessible text and voice-channel chats" if all_channels else "this channel"
        await ctx.send(
            f"SIN Corp audit started for {target.mention}. Searching the last `{timeframe}` in {scope_msg}."
        )

        try:
            deleted, scanned, skipped = await self.purge_user_messages(
                guild=ctx.guild,
                moderator=ctx.author,
                target=target,
                timeframe=timeframe,
                all_channels=all_channels,
                announce_channel=ctx.channel,
            )
        except ValueError as e:
            return await ctx.send(str(e))

        await ctx.send(
            f"Deleted `{deleted}` message(s). Scanned `{scanned}` channel(s). Skipped `{skipped}` channel(s)."
        )

    # ---------- Slash command ----------

    @app_commands.command(
        name="sinpurge",
        description="Purge recent messages from a specific user."
    )
    @app_commands.describe(
        user="The user to purge.",
        timeframe="How far back to search. Max 14 days.",
        scope="Current channel only, or all accessible channels including voice-channel chats."
    )
    @app_commands.choices(
        timeframe=[
            app_commands.Choice(name="Last 30 minutes", value="30m"),
            app_commands.Choice(name="Last 1 hour", value="1h"),
            app_commands.Choice(name="Last 5 hours", value="5h"),
            app_commands.Choice(name="Last 12 hours", value="12h"),
            app_commands.Choice(name="Last 1 day", value="1d"),
            app_commands.Choice(name="Last 7 days", value="7d"),
            app_commands.Choice(name="Last 14 days", value="14d"),
        ],
        scope=[
            app_commands.Choice(name="Current channel only", value="current"),
            app_commands.Choice(name="All accessible channels", value="all"),
        ]
    )
    async def sinpurge_slash(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        timeframe: app_commands.Choice[str],
        scope: app_commands.Choice[str],
    ):
        guild = interaction.guild
        moderator = interaction.user

        if guild is None:
            return await interaction.response.send_message(
                "This command only works inside a server.",
                ephemeral=True,
            )

        if not isinstance(moderator, discord.Member):
            return await interaction.response.send_message(
                "Could not verify your server permissions.",
                ephemeral=True,
            )

        if not moderator.guild_permissions.manage_messages:
            return await interaction.response.send_message(
                "You need `Manage Messages` to use this SIN Corp tool.",
                ephemeral=True,
            )

        if not guild.me.guild_permissions.manage_messages:
            return await interaction.response.send_message(
                "I need `Manage Messages` to purge anything.",
                ephemeral=True,
            )

        allowed, reason = self.can_target_member(guild, moderator, user)
        if not allowed:
            return await interaction.response.send_message(reason, ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        all_channels = scope.value == "all"

        try:
            deleted, scanned, skipped = await self.purge_user_messages(
                guild=guild,
                moderator=moderator,
                target=user,
                timeframe=timeframe.value,
                all_channels=all_channels,
                announce_channel=interaction.channel,
            )
        except ValueError as e:
            return await interaction.followup.send(str(e), ephemeral=True)

        await interaction.followup.send(
            f"SIN Corp audit complete. Deleted `{deleted}` message(s). Scanned `{scanned}` channel(s). Skipped `{skipped}` channel(s).",
            ephemeral=True,
        )
