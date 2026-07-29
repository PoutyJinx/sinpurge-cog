import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import discord
from discord import app_commands
from redbot.core import Config, commands


TIME_RE = re.compile(r"^(\d+)\s*(m|h|d)$", re.IGNORECASE)
USER_ID_RE = re.compile(r"^(?:<@!?(\d{15,25})>|(\d{15,25}))$")

ALL_KEYWORDS = {"true", "all", "yes", "y", "server", "global", "guild", "everywhere"}
CURRENT_KEYWORDS = {"false", "current", "channel", "here", "no", "n"}
NO_TIMEOUT_KEYWORDS = {"0", "none", "no", "off", "false", "skip", "no-timeout", "notimeout", "without-timeout"}

DEFAULT_TIMEOUT = timedelta(minutes=5)
MAX_PURGE_WINDOW = timedelta(days=14)
MAX_TIMEOUT = timedelta(days=28)


@dataclass
class PurgeTarget:
    """A target can be a current member, a raw user ID, or a name-only lookup for banned/left users."""

    raw: str
    member: Optional[discord.Member] = None
    user_id: Optional[int] = None
    fetched_user: Optional[discord.User] = None
    name_query: Optional[str] = None


@dataclass
class PurgeResult:
    deleted: int
    scanned: int
    skipped: int


@dataclass
class TimeoutResult:
    requested: bool
    applied: bool
    duration_text: str
    note: str


class SinPurge(commands.Cog):
    """SIN Corporation moderation tool for purging a user's recent messages."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=938475620145, force_registration=True)
        self.config.register_guild(modlog_channel=None)

    # ---------- Parsing helpers ----------

    def parse_duration(self, value: str, *, max_duration: timedelta, purpose: str) -> timedelta:
        match = TIME_RE.match(value.strip())
        if not match:
            raise ValueError(f"Invalid {purpose}. Use `30m`, `1h`, `5h`, `1d`, etc.")

        amount = int(match.group(1))
        unit = match.group(2).lower()

        if amount <= 0:
            raise ValueError(f"{purpose.capitalize()} must be above 0.")

        if unit == "m":
            delta = timedelta(minutes=amount)
        elif unit == "h":
            delta = timedelta(hours=amount)
        elif unit == "d":
            delta = timedelta(days=amount)
        else:
            raise ValueError(f"Invalid {purpose} unit. Use `m`, `h`, or `d`.")

        if delta > max_duration:
            if purpose == "purge timeframe":
                raise ValueError("Discord only allows reliable cleanup within the last 14 days. Use `14d` or less.")
            raise ValueError("Discord timeouts can be at most 28 days. Use `28d` or less.")

        return delta

    def parse_timeframe(self, value: str) -> timedelta:
        return self.parse_duration(value, max_duration=MAX_PURGE_WINDOW, purpose="purge timeframe")

    def parse_timeout(self, value: Optional[str]) -> Optional[timedelta]:
        """Return a timeout duration. None means no timeout. Missing means default 5 minutes."""
        if value is None:
            return DEFAULT_TIMEOUT

        cleaned = value.strip().lower()
        if cleaned.startswith("timeout="):
            cleaned = cleaned.split("=", 1)[1].strip()
        elif cleaned.startswith("timeout:"):
            cleaned = cleaned.split(":", 1)[1].strip()

        if cleaned in NO_TIMEOUT_KEYWORDS:
            return None

        return self.parse_duration(cleaned, max_duration=MAX_TIMEOUT, purpose="timeout")

    def is_timeout_token(self, token: str) -> bool:
        cleaned = token.strip().lower()
        if cleaned in NO_TIMEOUT_KEYWORDS:
            return True
        if cleaned.startswith("timeout=") or cleaned.startswith("timeout:"):
            return True
        return TIME_RE.match(cleaned) is not None

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

        try:
            value = str(author)
            if value and value.strip():
                names.add(value.strip().lower())
        except Exception:
            pass

        return names

    def parse_prefix_args(self, args: str) -> Tuple[str, str, Optional[str], Optional[str]]:
        """
        Parse: [p]sinpurge <target> <purge_timeframe> [scope] [timeout]

        Supported examples:
        [p]sinpurge @User 1h
        [p]sinpurge User With Spaces 1h all
        [p]sinpurge @User 1h all none
        [p]sinpurge @User 1h all 10m
        [p]sinpurge @User 1h timeout=30m
        """
        tokens = args.split()
        if len(tokens) < 2:
            raise ValueError(
                "Usage: `!sinpurge @User 1h`, `!sinpurge User With Spaces 1h all`, "
                "or `!sinpurge USER_ID 1h all none`."
            )

        timeout_text = None
        scope_text = None

        # Only treat the last token as timeout if it is clearly an extra option after the purge timeframe.
        # This avoids breaking names with spaces such as: User With Spaces 1h
        if len(tokens) >= 3:
            last = tokens[-1]
            before_last = tokens[-2].lower()
            if self.is_timeout_token(last) and (before_last in ALL_KEYWORDS or before_last in CURRENT_KEYWORDS or TIME_RE.match(before_last)):
                timeout_text = tokens.pop()

        if len(tokens) >= 3 and tokens[-1].lower() in ALL_KEYWORDS.union(CURRENT_KEYWORDS):
            scope_text = tokens.pop()

        if len(tokens) < 2:
            raise ValueError(
                "Usage: `!sinpurge @User 1h`, `!sinpurge @User 1h all`, or `!sinpurge @User 1h all none`."
            )

        timeframe = tokens.pop()
        user_text = " ".join(tokens).strip()

        if not user_text:
            raise ValueError("Target cannot be empty.")

        return user_text, timeframe, scope_text, timeout_text

    # ---------- Target helpers ----------

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
        return PurgeTarget(raw=text, name_query=search_without_at)

    def message_matches_target(self, message: discord.Message, target: PurgeTarget) -> bool:
        if target.user_id is not None and message.author.id == target.user_id:
            return True

        if target.name_query:
            query = target.name_query.strip().lower()
            if query and query in self.names_for_author(message.author):
                return True

        return False

    # ---------- Display / permission helpers ----------

    def target_display(self, target: PurgeTarget, *, plain: bool = False) -> str:
        if target.member is not None:
            if plain:
                return f"{target.member} ({target.member.id})"
            return target.member.mention
        if target.fetched_user is not None and target.user_id is not None:
            return f"{target.fetched_user} ({target.user_id})" if plain else f"`{target.fetched_user}` (`{target.user_id}`)"
        if target.user_id is not None:
            return f"user ID {target.user_id}" if plain else f"user ID `{target.user_id}`"
        if target.name_query is not None:
            return target.name_query if plain else f"`{target.name_query}`"
        return target.raw if plain else f"`{target.raw}`"

    def target_audit_note(self, target: PurgeTarget) -> Optional[str]:
        if target.member is None and target.user_id is not None:
            return "Target is not currently in the server, so SIN Corp is matching by user ID. This is ideal for banned scammers."
        if target.member is None and target.name_query is not None:
            return "Target is not currently in the server, so SIN Corp is matching old messages by exact username/display name. User ID is safer when available."
        return None

    def format_duration(self, delta: Optional[timedelta]) -> str:
        if delta is None:
            return "No timeout"

        total_seconds = int(delta.total_seconds())
        if total_seconds % 86400 == 0:
            days = total_seconds // 86400
            return f"{days}d"
        if total_seconds % 3600 == 0:
            hours = total_seconds // 3600
            return f"{hours}h"
        minutes = max(1, total_seconds // 60)
        return f"{minutes}m"

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

    def can_timeout_member(self, guild: discord.Guild, moderator: discord.Member, target: PurgeTarget) -> Tuple[bool, str]:
        member = target.member

        if member is None:
            return False, "Target is not currently in the server, so timeout was skipped. Purge can still run against old messages."

        if member == guild.owner:
            return False, "Timeout skipped because the server owner cannot be timed out."

        if member.guild_permissions.administrator:
            return False, "Timeout skipped because users with Administrator are exempt from Discord timeouts."

        if not moderator.guild_permissions.moderate_members and moderator != guild.owner:
            return False, "Timeout skipped because the moderator needs the `Timeout Members` permission."

        bot_member = guild.me
        if bot_member is None:
            return False, "Timeout skipped because I could not verify my own server member permissions."

        if not bot_member.guild_permissions.moderate_members:
            return False, "Timeout skipped because I need the `Timeout Members` permission."

        if member.top_role >= bot_member.top_role:
            return False, "Timeout skipped because my role is not above the target's highest role."

        return True, ""

    def build_channel_list(self, guild: discord.Guild, announce_channel, all_channels: bool) -> List:
        """Include normal text channels, active threads, forum posts, and voice-channel text chats where supported."""
        if not all_channels:
            return [announce_channel]

        channels: List = []
        channels.extend(guild.text_channels)
        channels.extend(guild.voice_channels)
        channels.extend(getattr(guild, "threads", []))

        # Forum channels themselves usually do not hold messages, but including them is harmless if the lib supports history.
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

    # ---------- Config helpers ----------

    async def get_modlog_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        channel_id = await self.config.guild(guild).modlog_channel()
        if not channel_id:
            return None

        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    async def send_modlog_report(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        target: PurgeTarget,
        timeframe: str,
        all_channels: bool,
        timeout_result: TimeoutResult,
        result: PurgeResult,
        command_channel,
    ) -> bool:
        modlog = await self.get_modlog_channel(guild)
        if modlog is None:
            return False

        scope_text = "All accessible channels" if all_channels else "Current channel only"
        command_channel_text = getattr(command_channel, "mention", None) or getattr(command_channel, "name", "Unknown channel")
        target_text = self.target_display(target, plain=True)
        target_id_text = str(target.user_id) if target.user_id is not None else "Unknown / name-only match"

        timeout_line = timeout_result.note
        if timeout_result.requested and timeout_result.applied:
            timeout_line = f"Applied for {timeout_result.duration_text}"
        elif not timeout_result.requested:
            timeout_line = "No timeout requested"

        message = (
            "📌 **SIN PURGE REPORT**\n"
            f"**Moderator:** {moderator.mention}\n"
            f"**Target:** {target_text}\n"
            f"**Target ID:** `{target_id_text}`\n"
            f"**Purge Window:** `{timeframe}`\n"
            f"**Scope:** {scope_text}\n"
            f"**Timeout:** {timeout_line}\n"
            f"**Deleted:** `{result.deleted}` message(s)\n"
            f"**Scanned:** `{result.scanned}` channel(s)\n"
            f"**Skipped:** `{result.skipped}` channel(s)\n"
            f"**Command Channel:** {command_channel_text}"
        )

        note = self.target_audit_note(target)
        if note:
            message += f"\n**Note:** {note}"

        try:
            await modlog.send(message, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    # ---------- Action helpers ----------

    async def safe_delete_batch(self, channel, messages: List[discord.Message]) -> int:
        if not messages:
            return 0

        try:
            if len(messages) > 1 and hasattr(channel, "delete_messages"):
                await channel.delete_messages(messages)
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

    async def apply_timeout(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        target: PurgeTarget,
        timeout_delta: Optional[timedelta],
    ) -> TimeoutResult:
        if timeout_delta is None:
            return TimeoutResult(
                requested=False,
                applied=False,
                duration_text="No timeout",
                note="No timeout requested.",
            )

        duration_text = self.format_duration(timeout_delta)
        allowed, reason = self.can_timeout_member(guild, moderator, target)
        if not allowed:
            return TimeoutResult(requested=True, applied=False, duration_text=duration_text, note=reason)

        until = datetime.now(timezone.utc) + timeout_delta
        reason = f"SIN Purge timeout by {moderator} ({moderator.id})"

        try:
            await target.member.timeout(until, reason=reason)
            return TimeoutResult(
                requested=True,
                applied=True,
                duration_text=duration_text,
                note=f"Timeout applied for `{duration_text}`.",
            )
        except AttributeError:
            try:
                await target.member.edit(timed_out_until=until, reason=reason)
                return TimeoutResult(
                    requested=True,
                    applied=True,
                    duration_text=duration_text,
                    note=f"Timeout applied for `{duration_text}`.",
                )
            except (discord.Forbidden, discord.HTTPException):
                return TimeoutResult(
                    requested=True,
                    applied=False,
                    duration_text=duration_text,
                    note="Timeout failed. Check bot role position and Timeout Members permission.",
                )
        except (discord.Forbidden, discord.HTTPException):
            return TimeoutResult(
                requested=True,
                applied=False,
                duration_text=duration_text,
                note="Timeout failed. Check bot role position and Timeout Members permission.",
            )

    async def purge_user_messages(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        target: PurgeTarget,
        timeframe: str,
        all_channels: bool,
        announce_channel,
    ) -> PurgeResult:
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

        return PurgeResult(deleted=total_deleted, scanned=scanned_channels, skipped=skipped_channels)

    async def run_sinpurge(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        target: PurgeTarget,
        timeframe: str,
        all_channels: bool,
        timeout_delta: Optional[timedelta],
        announce_channel,
    ) -> Tuple[PurgeResult, TimeoutResult, bool]:
        timeout_result = await self.apply_timeout(guild, moderator, target, timeout_delta)
        result = await self.purge_user_messages(
            guild=guild,
            moderator=moderator,
            target=target,
            timeframe=timeframe,
            all_channels=all_channels,
            announce_channel=announce_channel,
        )

        modlog_sent = await self.send_modlog_report(
            guild=guild,
            moderator=moderator,
            target=target,
            timeframe=timeframe,
            all_channels=all_channels,
            timeout_result=timeout_result,
            result=result,
            command_channel=announce_channel,
        )

        return result, timeout_result, modlog_sent

    # ---------- Prefix settings ----------

    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @commands.group(name="sinpurgeset", invoke_without_command=True)
    async def sinpurgeset_prefix(self, ctx: commands.Context):
        """Configure SIN Purge."""
        modlog = await self.get_modlog_channel(ctx.guild)
        if modlog:
            await ctx.send(f"SIN Purge mod-log channel is currently set to {modlog.mention}.")
        else:
            await ctx.send("SIN Purge mod-log channel is not set. Use `!sinpurgeset modlog #channel`.")

    @sinpurgeset_prefix.command(name="modlog", aliases=["channel", "modchannel"])
    async def sinpurgeset_modlog_prefix(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the mod-log channel for purge reports."""
        await self.config.guild(ctx.guild).modlog_channel.set(channel.id)
        await ctx.send(f"SIN Purge mod-log channel set to {channel.mention}.")

    @sinpurgeset_prefix.command(name="clear", aliases=["disable", "off"])
    async def sinpurgeset_clear_prefix(self, ctx: commands.Context):
        """Clear the mod-log channel."""
        await self.config.guild(ctx.guild).modlog_channel.set(None)
        await ctx.send("SIN Purge mod-log channel cleared.")

    # ---------- Prefix purge command ----------

    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True, read_message_history=True)
    @commands.command(name="sinpurge", aliases=["purgeuser", "userpurge"])
    async def sinpurge_prefix(self, ctx: commands.Context, *, args: str):
        """
        Purge recent messages from a user.

        Examples:
        [p]sinpurge @User 1h
        [p]sinpurge Username 1h all
        [p]sinpurge User With Spaces 1h all none
        [p]sinpurge 123456789012345678 1h all 10m
        [p]sinpurge BannedScammerName 1d all no-timeout
        """
        try:
            user_text, timeframe, scope_text, timeout_text = self.parse_prefix_args(args)
            all_channels = self.parse_scope(scope_text)
            self.parse_timeframe(timeframe)
            timeout_delta = self.parse_timeout(timeout_text)
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
        timeout_msg = self.format_duration(timeout_delta)
        await ctx.send(
            f"SIN Corp audit started for {self.target_display(target)}. "
            f"Searching the last `{timeframe}` in {scope_msg}. Timeout: `{timeout_msg}`."
        )

        note = self.target_audit_note(target)
        if note:
            await ctx.send(note)

        try:
            result, timeout_result, modlog_sent = await self.run_sinpurge(
                guild=ctx.guild,
                moderator=ctx.author,
                target=target,
                timeframe=timeframe,
                all_channels=all_channels,
                timeout_delta=timeout_delta,
                announce_channel=ctx.channel,
            )
        except ValueError as e:
            return await ctx.send(str(e))

        if result.deleted > 0:
            try:
                await ctx.send(self.lore_message(target, result.deleted))
            except (discord.HTTPException, discord.Forbidden):
                pass

        modlog_note = " Mod-log report sent." if modlog_sent else " Mod-log channel not set or unavailable."
        await ctx.send(
            f"Deleted `{result.deleted}` message(s). Scanned `{result.scanned}` channel(s). "
            f"Skipped `{result.skipped}` channel(s). {timeout_result.note}{modlog_note}"
        )

    # ---------- Slash settings command ----------

    @app_commands.command(
        name="sinpurgeset",
        description="Set, show, or clear the SIN Purge mod-log channel."
    )
    @app_commands.describe(
        action="Choose what to do with the mod-log channel.",
        channel="Channel used for SIN Purge reports. Required when action is Set."
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Show current mod-log channel", value="show"),
            app_commands.Choice(name="Set mod-log channel", value="set"),
            app_commands.Choice(name="Clear mod-log channel", value="clear"),
        ]
    )
    async def sinpurgeset_slash(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        channel: Optional[discord.TextChannel] = None,
    ):
        guild = interaction.guild
        moderator = interaction.user

        if guild is None:
            return await interaction.response.send_message("This command only works inside a server.", ephemeral=True)

        if not isinstance(moderator, discord.Member):
            return await interaction.response.send_message("Could not verify your server permissions.", ephemeral=True)

        if not moderator.guild_permissions.manage_guild and moderator != guild.owner:
            return await interaction.response.send_message("You need `Manage Server` to configure SIN Purge.", ephemeral=True)

        if action.value == "show":
            modlog = await self.get_modlog_channel(guild)
            if modlog:
                return await interaction.response.send_message(f"SIN Purge mod-log channel is {modlog.mention}.", ephemeral=True)
            return await interaction.response.send_message("SIN Purge mod-log channel is not set.", ephemeral=True)

        if action.value == "clear":
            await self.config.guild(guild).modlog_channel.set(None)
            return await interaction.response.send_message("SIN Purge mod-log channel cleared.", ephemeral=True)

        if action.value == "set":
            if channel is None:
                return await interaction.response.send_message("Choose a channel when using `Set mod-log channel`.", ephemeral=True)
            await self.config.guild(guild).modlog_channel.set(channel.id)
            return await interaction.response.send_message(f"SIN Purge mod-log channel set to {channel.mention}.", ephemeral=True)

        await interaction.response.send_message("Unknown action.", ephemeral=True)

    # ---------- Slash purge command ----------

    @app_commands.command(
        name="sinpurge",
        description="Purge recent messages from a user, including banned/left users by ID or exact name."
    )
    @app_commands.rename(target_text="target")
    @app_commands.describe(
        target_text="Mention, username, display name, or user ID. Use user ID for banned scammers when possible.",
        timeframe="How far back to search. Max 14 days.",
        scope="Current channel only, or all accessible channels including voice-channel chats.",
        timeout="Optional timeout before purging. Choose No timeout for cleanup only. Default is 5 minutes."
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
        ],
        timeout=[
            app_commands.Choice(name="Default: 5 minutes", value="5m"),
            app_commands.Choice(name="No timeout", value="none"),
            app_commands.Choice(name="1 minute", value="1m"),
            app_commands.Choice(name="10 minutes", value="10m"),
            app_commands.Choice(name="30 minutes", value="30m"),
            app_commands.Choice(name="1 hour", value="1h"),
            app_commands.Choice(name="1 day", value="1d"),
        ]
    )
    async def sinpurge_slash(
        self,
        interaction: discord.Interaction,
        target_text: str,
        timeframe: app_commands.Choice[str],
        scope: app_commands.Choice[str],
        timeout: Optional[app_commands.Choice[str]] = None,
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

        if guild.me is None or not guild.me.guild_permissions.manage_messages:
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

        timeout_value = timeout.value if timeout is not None else None
        try:
            timeout_delta = self.parse_timeout(timeout_value)
        except ValueError as e:
            return await interaction.response.send_message(str(e), ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        all_channels = scope.value == "all"

        try:
            result, timeout_result, modlog_sent = await self.run_sinpurge(
                guild=guild,
                moderator=moderator,
                target=target,
                timeframe=timeframe.value,
                all_channels=all_channels,
                timeout_delta=timeout_delta,
                announce_channel=interaction.channel,
            )
        except ValueError as e:
            return await interaction.followup.send(str(e), ephemeral=True)

        if result.deleted > 0:
            try:
                await interaction.channel.send(self.lore_message(target, result.deleted))
            except (discord.HTTPException, discord.Forbidden, AttributeError):
                pass

        note = self.target_audit_note(target)
        notes = [timeout_result.note]
        if note:
            notes.append(note)
        notes.append("Mod-log report sent." if modlog_sent else "Mod-log channel not set or unavailable.")
        note_text = "\n" + "\n".join(notes)

        await interaction.followup.send(
            f"SIN Corp audit complete. Deleted `{result.deleted}` message(s). "
            f"Scanned `{result.scanned}` channel(s). Skipped `{result.skipped}` channel(s).{note_text}",
            ephemeral=True,
        )
