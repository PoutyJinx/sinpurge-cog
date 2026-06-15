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
ALL_CHANNEL_WORDS = {"true", "all", "yes", "y", "server", "global", "everywhere"}
CURRENT_CHANNEL_WORDS = {"false", "current", "channel", "here", "no", "n"}


class SinPurge(commands.Cog):
    """SIN Corporation user purge tool."""

    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def parse_timeframe(value: str) -> timedelta:
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
    def clean_user_input(value: str) -> str:
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1].strip()
        return value

    def parse_prefix_args(self, args: str) -> Tuple[str, str, bool]:
        parts = args.strip().split()
        if len(parts) < 2:
            raise ValueError(
                "Usage: `!sinpurge @User 1h`, `!sinpurge Username 1h`, or `!sinpurge Username 1h all`."
            )

        all_channels = False
        last = parts[-1].lower()

        if last in ALL_CHANNEL_WORDS:
            all_channels = True
            parts.pop()
        elif last in CURRENT_CHANNEL_WORDS:
            all_channels = False
            parts.pop()

        if len(parts) < 2:
            raise ValueError("Missing user or timeframe. Example: `!sinpurge @User 1h all`.")

        timeframe = parts[-1]
        self.parse_timeframe(timeframe)

        target_query = self.clean_user_input(" ".join(parts[:-1]))
        if not target_query:
            raise ValueError("Missing user. Example: `!sinpurge @User 1h`.")

        return target_query, timeframe, all_channels

    async def resolve_member(self, ctx: commands.Context, query: str) -> discord.Member:
        guild = ctx.guild
        if guild is None:
            raise commands.BadArgument("This command only works inside a server.")

        query = self.clean_user_input(query)

        try:
            return await commands.MemberConverter().convert(ctx, query)
        except commands.BadArgument:
            pass

        if query.isdigit():
            member = guild.get_member(int(query))
            if member:
                return member

            try:
                member = await guild.fetch_member(int(query))
                if member:
                    return member
            except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                pass

        lowered = query.lower()
        exact_matches = [
            member for member in guild.members
            if member.display_name.lower() == lowered
            or member.name.lower() == lowered
            or str(member).lower() == lowered
            or member.global_name and member.global_name.lower() == lowered
        ]

        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(exact_matches) > 1:
            names = ", ".join(member.display_name for member in exact_matches[:5])
            raise commands.BadArgument(f"Multiple members matched `{query}`: {names}. Use their ID or mention instead.")

        partial_matches = [
            member for member in guild.members
            if lowered in member.display_name.lower()
            or lowered in member.name.lower()
            or member.global_name and lowered in member.global_name.lower()
        ]

        if len(partial_matches) == 1:
            return partial_matches[0]
        if len(partial_matches) > 1:
            names = ", ".join(member.display_name for member in partial_matches[:5])
            raise commands.BadArgument(f"Multiple members matched `{query}`: {names}. Use their ID or mention instead.")

        raise commands.BadArgument(f"Member `{query}` not found. Try a mention, exact username, nickname, or user ID.")

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
        total_deleted = 0

        try:
            async for message in channel.history(limit=None, after=after_time, oldest_first=False):
                if message.author.id != target.id:
                    continue

                matched_messages.append(message)

                if len(matched_messages) >= 100:
                    total_deleted += await self.safe_delete_batch(channel, matched_messages)
                    matched_messages = []

            if matched_messages:
                total_deleted += await self.safe_delete_batch(channel, matched_messages)

            return total_deleted, True

        except (discord.Forbidden, discord.HTTPException):
            return total_deleted, total_deleted > 0

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
                raise ValueError("This command can only purge the current channel unless `all` is enabled from a normal text channel.")
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
    @commands.command(name="sinpurge", aliases=["purgeuser", "userpurge"])
    async def sinpurge_prefix(self, ctx: commands.Context, *, args: str):
        """
        Purge recent messages from a specific user.

        Examples:
        [p]sinpurge @user 1h
        [p]sinpurge username 1h all
        [p]sinpurge User With Spaces 1h
        [p]sinpurge 123456789012345678 1h all
        """
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return await ctx.send("This command only works inside a server.")

        try:
            target_query, timeframe, all_channels = self.parse_prefix_args(args)
            target = await self.resolve_member(ctx, target_query)
        except (ValueError, commands.BadArgument) as error:
            return await ctx.send(str(error))

        allowed, reason = self.can_use_against(ctx.author, target)
        if not allowed:
            return await ctx.send(reason)

        await ctx.send(
            f"SIN Corp audit started for {target.mention}. "
            f"Searching the last `{timeframe}` "
            f"in {'all visible text channels' if all_channels else 'this channel'}."
        )

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

    @app_commands.command(name="sinpurge", description="Purge recent messages from a specific user.")
    @app_commands.describe(
        target="The user whose recent messages should be purged.",
        timeframe="Time range. Max 14d.",
        all_channels="Scan all text channels instead of only this channel."
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
    async def sinpurge_slash(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        timeframe: app_commands.Choice[str],
        all_channels: bool = False,
    ):
        guild = interaction.guild
        moderator = interaction.user

        if guild is None or not isinstance(moderator, discord.Member):
            return await interaction.response.send_message("This command only works inside a server.", ephemeral=True)

        if not moderator.guild_permissions.manage_messages:
            return await interaction.response.send_message("You need `Manage Messages` to use this SIN Corp tool.", ephemeral=True)

        if guild.me is None or not guild.me.guild_permissions.manage_messages:
            return await interaction.response.send_message("I need `Manage Messages` to purge anything.", ephemeral=True)

        allowed, reason = self.can_use_against(moderator, target)
        if not allowed:
            return await interaction.response.send_message(reason, ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        try:
            deleted, scanned, skipped = await self.run_purge(
                guild=guild,
                moderator=moderator,
                target=target,
                timeframe=timeframe.value,
                all_channels=all_channels,
                source_channel=interaction.channel,
            )
        except ValueError as error:
            return await interaction.followup.send(str(error), ephemeral=True)

        if deleted > 0 and interaction.channel is not None:
            try:
                await interaction.channel.send(self.lore_message(target, deleted))
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            f"SIN Corp audit complete. Deleted `{deleted}` messages. Scanned `{scanned}` channel(s). Skipped `{skipped}` channel(s).",
            ephemeral=True,
        )
