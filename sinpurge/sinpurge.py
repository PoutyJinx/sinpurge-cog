import asyncio
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

import discord
from discord import app_commands
from redbot.core import commands


TIME_RE = re.compile(r"^(\d+)\s*(m|h|d)$", re.IGNORECASE)
MAX_PURGE_WINDOW = timedelta(days=14)


class SinPurge(commands.Cog):
    """SIN Corporation user purge tool."""

    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def parse_timeframe(value: str) -> timedelta:
        """Turn values like 30m, 1h, 5h, 2d into a timedelta."""
        match = TIME_RE.match(value.strip())
        if not match:
            raise ValueError("Invalid timeframe. Use `30m`, `1h`, `5h`, `2d`, etc.")

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

        if delta > MAX_PURGE_WINDOW:
            raise ValueError("Discord bulk deletion is limited to messages newer than 14 days. Use `14d` or less.")

        return delta

    @staticmethod
    def lore_message(target: discord.Member, deleted: int) -> str:
        messages = [
            "SIN Corporation has audited {user}. `{count}` suspicious memos were shredded.",
            "{user} has been escorted out of the Castle. `{count}` messages mysteriously vanished.",
            "CEO Jinx has reviewed the paperwork. {user} failed the vibe check. `{count}` messages deleted.",
            "A Dweller cleanup crew swept away `{count}` pieces of nonsense from {user}.",
            "{user} has been transferred to the Department of Consequences. `{count}` messages purged.",
            "SIN Corp Security bonked {user} with the compliance clipboard. `{count}` messages removed.",
            "The shredder demanded tribute. {user} provided `{count}` messages.",
            "Audit complete. {user}'s spam portfolio has lost `{count}` assets.",
        ]
        return random.choice(messages).format(user=target.mention, count=deleted)

    @staticmethod
    def can_use_against(ctx_author: discord.Member, target: discord.Member) -> Tuple[bool, str]:
        guild = ctx_author.guild

        if target == ctx_author:
            return False, "You cannot purge yourself. That paperwork is cursed."

        if target == guild.owner:
            return False, "The server owner is protected by forbidden HR magic."

        if ctx_author != guild.owner and target.top_role >= ctx_author.top_role:
            return False, "You cannot purge someone with an equal or higher role."

        return True, ""

    @staticmethod
    def chunk_messages(messages: List[discord.Message], size: int = 100) -> Iterable[List[discord.Message]]:
        for index in range(0, len(messages), size):
            yield messages[index:index + size]

    async def safe_delete_batch(self, channel: discord.TextChannel, messages: List[discord.Message]) -> int:
        """Delete messages in a safe way, falling back to individual deletes if needed."""
        if not messages:
            return 0

        deleted = 0

        for batch in self.chunk_messages(messages, 100):
            try:
                if len(batch) == 1:
                    await batch[0].delete()
                    deleted += 1
                else:
                    await channel.delete_messages(batch)
                    deleted += len(batch)

                await asyncio.sleep(0.5)

            except discord.HTTPException:
                for message in batch:
                    try:
                        await message.delete()
                        deleted += 1
                        await asyncio.sleep(0.15)
                    except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                        pass
            except discord.Forbidden:
                pass

        return deleted

    async def purge_in_channel(
        self,
        channel: discord.TextChannel,
        moderator: discord.Member,
        target: discord.Member,
        after_time: datetime,
    ) -> Tuple[int, bool]:
        """Return deleted count and whether this channel was scanned."""
        guild = channel.guild
        bot_member = guild.me

        if bot_member is None:
            return 0, False

        bot_perms = channel.permissions_for(bot_member)
        mod_perms = channel.permissions_for(moderator)

        if not (bot_perms.view_channel and bot_perms.read_message_history and bot_perms.manage_messages):
            return 0, False

        if not mod_perms.manage_messages:
            return 0, False

        matched_messages: List[discord.Message] = []

        try:
            async for message in channel.history(limit=None, after=after_time, oldest_first=False):
                if message.author.id == target.id:
                    matched_messages.append(message)

                if len(matched_messages) >= 100:
                    break

            deleted = await self.safe_delete_batch(channel, matched_messages)
            return deleted, True

        except (discord.Forbidden, discord.HTTPException):
            return 0, False

    async def run_purge(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        target: discord.Member,
        timeframe: str,
        all_channels: bool,
        source_channel: discord.abc.Messageable,
    ) -> Tuple[int, int, int]:
        delta = self.parse_timeframe(timeframe)
        after_time = datetime.now(timezone.utc) - delta

        if all_channels:
            channels = list(guild.text_channels)
        else:
            if not isinstance(source_channel, discord.TextChannel):
                raise ValueError("This command can only purge the current channel unless `all_channels` is enabled from a normal text channel.")
            channels = [source_channel]

        total_deleted = 0
        scanned_channels = 0
        skipped_channels = 0

        for channel in channels:
            deleted, scanned = await self.purge_in_channel(channel, moderator, target, after_time)
            if scanned:
                scanned_channels += 1
                total_deleted += deleted
            else:
                skipped_channels += 1

        return total_deleted, scanned_channels, skipped_channels

    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True, read_message_history=True)
    @commands.hybrid_command(name="sinpurge", aliases=["purgeuser", "userpurge"])
    @app_commands.describe(
        target="The user whose recent messages should be purged.",
        timeframe="Time range like 30m, 1h, 5h, 2d. Max 14d.",
        all_channels="True = scan all text channels. False = only this channel."
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
        ]
    )
    async def sinpurge(
        self,
        ctx: commands.Context,
        target: discord.Member,
        timeframe: str,
        all_channels: Optional[bool] = False,
    ):
        """
        Purge recent messages from a specific user.

        Examples:
        [p]sinpurge @user 1h
        [p]sinpurge @user 5h true
        [p]sinpurge @user 2d false
        """
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return await ctx.send("This command only works inside a server.")

        allowed, reason = self.can_use_against(ctx.author, target)
        if not allowed:
            return await ctx.send(reason)

        all_channels = bool(all_channels)

        try:
            delta = self.parse_timeframe(timeframe)
        except ValueError as error:
            return await ctx.send(str(error))

        started_message = (
            f"SIN Corp audit started for {target.mention}. "
            f"Searching the last `{timeframe}` "
            f"in {'all visible text channels' if all_channels else 'this channel'}."
        )

        await ctx.send(started_message)

        try:
            deleted, scanned, skipped = await self.run_purge(
                guild=ctx.guild,
                moderator=ctx.author,
                target=target,
                timeframe=timeframe,
                all_channels=all_channels,
                source_channel=ctx.channel,
            )
        except ValueError as error:
            return await ctx.send(str(error))

        if deleted > 0:
            await ctx.send(self.lore_message(target, deleted))
        else:
            await ctx.send(f"Audit complete. No recent messages from {target.mention} were found.")

        await ctx.send(
            f"Deleted `{deleted}` messages. Scanned `{scanned}` channel(s). Skipped `{skipped}` channel(s)."
        )
