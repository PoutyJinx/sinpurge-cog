import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import discord
from discord import app_commands
from redbot.core import commands


TIME_RE = re.compile(r"^(\d+)\s*(m|h|d)$", re.IGNORECASE)
USER_ID_RE = re.compile(r"^(?:<@!?(\d{15,25})>|(\d{15,25}))$")
ALL_KEYWORDS = {"true", "all", "yes", "y", "server", "global", "guild", "everywhere"}
CURRENT_KEYWORDS = {"false", "current", "channel", "here", "no", "n"}


@dataclass
class PurgeTarget:
    """A target can be a current member, a raw user ID, or a name-only lookup for banned/left users."""

    raw: str
    member: Optional[discord.Member] = None
    user_id: Optional[int] = None
    fetched_user: Optional[discord.User] = None
    name_query: Optional[str] = None


class SinPurge(commands.Cog):
    """SIN Corporation moderation tool for purging a user's recent messages."""

    def __init__(self, bot):
        self.bot = bot

    # ---------- Parsing helpers ----------

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

        raise ValueError("Invalid scope. Use `all` for all channels, or leave it empty for current channel only.")

    def clean_target_text(self, value: str) -> str:
        text = value.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            text = text[1:-1].strip()
        return text

    def extract_user_id(self, value: str) -> Optional[int]:
        match = USER_ID_RE.match(value.strip())
        if not match:
            return None

        raw_id = match.group(1) or match.group(2)
        try:
            return int(raw_id)
        except (TypeError, ValueError):
            return None

    def names_for_author(self, author) -> set:
        """Return possible names for a message author/member for exact case-insensitive matching."""
        names = set()

        for attr in ("name", "display_name", "global_name", "nick"):
            value = getattr(author, attr, None)
            if isinstance(value, str) and value.strip():
                names.add(value.strip().lower())

        # str(author) may be username or username#1234 depending on Discord/discord.py version.
        try:
            value = str(author)
            if value and value.strip():
                names.add(value.strip().lower())
        except Exception:
            pass

        return names

    async def resolve_target_from_text(self, guild: discord.Guild, user_text: str, ctx: Optional[commands.Context] = None) -> PurgeTarget:
        """
        Resolve a target from mention, user ID, current member name, or a name-only fallback.

        Name-only fallback is important for banned/left scammers. Once someone is banned,
        Discord no longer treats them as a Member, but their old messages can still exist.
        """
        text = self.clean_target_text(user_text)
        if not text:
            raise commands.BadArgument("Target cannot be empty.")

        # Mentions and raw numeric IDs work even after the user has left or been banned.
        user_id = self.extract_user_id(text)
        if user_id is not None:
            member = guild.get_member(user_id)
            fetched_user = None
            if member is None:
                try:
                    fetched_user = await self.bot.fetch_user(user_id)
                except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                    fetched_user = None

            return PurgeTarget(raw=text, member=member, user_id=user_id, fetched_user=fetched_user)

        # Prefix commands can use the built-in member converter for mentions, usernames, IDs, and nicknames.
        if ctx is not None:
            try:
                member = await commands.MemberConverter().convert(ctx, text)
                return PurgeTarget(raw=text, member=member, user_id=member.id)
            except commands.BadArgument:
                pass

        # Manual exact member match for slash command strings and spaced display names.
        search_text = text.strip()
        search_without_at = search_text[1:].strip() if search_text.startswith("@") else search_text
        lowered_options = {search_text.lower(), search_without_at.lower()}

        matches = []
        for member in guild.members:
            member_names = self.names_for_author(member)
            if member_names.intersection(lowered_options):
                matches.append(member)

        if len(matches) == 1:
            member = matches[0]
            return PurgeTarget(raw=text, member=member, user_id=member.id)

        if len(matches) > 1:
            raise commands.BadArgument("Multiple members match that name. Use a mention or user ID instead.")

        # Final fallback: name-only matching inside message history.
        # This allows purging messages from banned/left users when only their username is known.
        return PurgeTarget(raw=text, name_query=search_without_at)

    # ---------- Display / permission helpers ----------

    def target_display(self, target: PurgeTarget) -> str:
        if target.member is not None:
            return target.member.mention
        if target.fetched_user is not None and target.user_id is not None:
            return f"`{target.fetched_user}` (`{target.user_id}`)"
        if target.user_id is not None:
            return f"user ID `{target.user_id}`"
        if target.name_query is not None:
            return f"`{target.name_query}`"
        return f"`{target.raw}`"

    def target_audit_note(self, target: PurgeTarget) -> Optional[str]:
        if target.member is None and target.user_id is not None:
            return "Target is not currently in the server, so SIN Corp is matching by user ID. This is ideal for banned scammers."
        if target.member is None and target.name_query is not None:
            return "Target is not currently in the server, so SIN Corp is matching old messages by exact username/display name. User ID is safer when available."
        return None

    def lore_message(self, target: PurgeTarget, deleted: int) -> str:
        target_name = self.target_display(target)
        messages = [
            "SIN Corporation has audited {user}. {count} suspicious memo(s) were shredded.",
            "{user} has been escorted out of the Castle. {count} message(s) mysteriously vanished.",
            "CEO Jinx reviewed the paperwork. {user} failed the vibe check. {count} message(s) deleted.",
            "A Dweller cleanup crew swept away {count} piece(s) of nonsense from {user}.",
            "{user} has been transferred to the Department of Consequences. {count} message(s) purged.",
            "SIN Corp Security bonked {user} with the compliance clipboard. {count} message(s) removed.",
        ]
        return random.choice(messages).format(user=target_name, count=deleted)

    def can_target_member(self, guild: discord.Guild, moderator: discord.Member, target: PurgeTarget) -> Tuple[bool, str]:
        # If the target is not a current server member, role checks are impossible and unnecessary.
        # This is exactly the case for banned/left scammers whose old messages remain.
        if target.member is None:
            return True, ""

        member = target.member

        if member == moderator:
            return False, "You cannot purge yourself. That paperwork is cursed."

        if member == guild.owner:
            return False, "The server owner is protected by forbidden HR magic."

        if member.top_role >= moderator.top_role and moderator != guild.owner:
            return False, "You cannot purge someone with an equal or higher role."

        return True, ""

    # ---------- Channel / deletion helpers ----------

    def build_channel_list(self, guild: discord.Guild, announce_channel, all_channels: bool) -> List[discord.abc.GuildChannel]:
        """Include normal text channels, active threads, and voice-channel text chats."""
        if not all_channels:
            return [announce_channel]

        channels: List[discord.abc.GuildChannel] = []
        channels.extend(guild.text_channels)
        channels.extend(guild.voice_channels)

        # Active threads only. Archived threads are not listed here and are intentionally skipped.
        channels.extend(getattr(guild, "threads", []))

        # Some discord.py versions expose forum channels. They usually do not have normal message history,
        # but including them safely does not hurt because unsupported channels are skipped.
        channels.extend(getattr(guild, "forum_channels", []))

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

    def message_matches_target(self, message: discord.Message, target: PurgeTarget) -> bool:
        if target.user_id is not None:
            return message.author.id == target.user_id

        if target.name_query is not None:
            query = target.name_query.strip().lower()
            return query in self.names_for_author(message.author)

        return False

    async def safe_delete_batch(self, channel, messages: List[discord.Message]) -> int:
        if not messages:
            return 0

        # Bulk delete when the channel supports it. Some messageable channels, including certain
        # voice-channel chats/threads depending on library version, may not expose delete_messages.
        bulk_delete = getattr(channel, "delete_messages", None)
        if callable(bulk_delete) and len(messages) > 1:
            try:
                await bulk_delete(messages)
                return len(messages)
            except (discord.HTTPException, discord.Forbidden):
                pass

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
        target: PurgeTarget,
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
                    if self.message_matches_target(message, target):
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
        [p]sinpurge BannedScammerName 1d all
        """
        parts = args.rsplit(maxsplit=2)

        if len(parts) < 2:
            return await ctx.send(
                "Usage: `!sinpurge @User 1h`, `!sinpurge User With Spaces 1h all`, or `!sinpurge USER_ID 1h all`"
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
                user_text = f"{possible_user_text} {possible_timeframe}"
                timeframe = possible_scope
                scope_text = None

        try:
            all_channels = self.parse_scope(scope_text)
            self.parse_timeframe(timeframe)
        except ValueError as e:
            return await ctx.send(str(e))

        try:
            target = await self.resolve_target_from_text(ctx.guild, user_text, ctx=ctx)
        except commands.BadArgument as e:
            return await ctx.send(str(e))

        allowed, reason = self.can_target_member(ctx.guild, ctx.author, target)
        if not allowed:
            return await ctx.send(reason)

        scope_msg = "all accessible text channels, voice-channel chats, and active threads" if all_channels else "this channel"
        await ctx.send(
            f"SIN Corp audit started for {self.target_display(target)}. Searching the last `{timeframe}` in {scope_msg}."
        )

        note = self.target_audit_note(target)
        if note:
            await ctx.send(note)

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
        description="Purge recent messages from a user, including banned/left users by ID or exact name."
    )
    @app_commands.rename(target_text="target")
    @app_commands.describe(
        target_text="Mention, username, display name, or user ID. Use user ID for banned scammers when possible.",
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
        target_text: str,
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

        try:
            target = await self.resolve_target_from_text(guild, target_text, ctx=None)
        except commands.BadArgument as e:
            return await interaction.response.send_message(str(e), ephemeral=True)

        allowed, reason = self.can_target_member(guild, moderator, target)
        if not allowed:
            return await interaction.response.send_message(reason, ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        all_channels = scope.value == "all"

        try:
            deleted, scanned, skipped = await self.purge_user_messages(
                guild=guild,
                moderator=moderator,
                target=target,
                timeframe=timeframe.value,
                all_channels=all_channels,
                announce_channel=interaction.channel,
            )
        except ValueError as e:
            return await interaction.followup.send(str(e), ephemeral=True)

        note = self.target_audit_note(target)
        note_text = f"\n{note}" if note else ""

        await interaction.followup.send(
            f"SIN Corp audit complete. Deleted `{deleted}` message(s). Scanned `{scanned}` channel(s). Skipped `{skipped}` channel(s).{note_text}",
            ephemeral=True,
        )
