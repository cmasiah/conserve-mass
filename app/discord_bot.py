"""Discord bot that DMs the owner, plus a few owner-only slash commands."""
import asyncio
import logging

import discord
from discord import app_commands
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.utils import fmt_delta, fmt_isk, fmt_m3

log = logging.getLogger(__name__)

COLORS = {
    "new_contract": 0x2ECC71, "expiring": 0xF1C40F, "delivery_due": 0xE67E22,
    "status_failed": 0xE74C3C, "status_cancelled": 0x95A5A6, "status_deleted": 0x95A5A6,
    "status_finished": 0x3498DB, "status_accepted": 0x9B59B6, "token_invalid": 0xE74C3C,
}


class ConserveMassBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.none())  # DMs need no privileged intents
        self.tree = app_commands.CommandTree(self)
        self._owner: discord.User | None = None
        self._task: asyncio.Task | None = None
        self._register_commands()

    # ------------------------------------------------------------ lifecycle
    @property
    def configured(self) -> bool:
        s = get_settings()
        return bool(s.discord_bot_token and s.discord_owner_user_id)

    def start_background(self) -> None:
        if not self.configured:
            log.warning("Discord not configured (DISCORD_BOT_TOKEN / DISCORD_OWNER_USER_ID); "
                        "notifications will queue until it is.")
            return
        self._task = asyncio.create_task(self._run(), name="discord-bot")

    async def _run(self) -> None:
        try:
            await self.start(get_settings().discord_bot_token)
        except discord.LoginFailure:
            log.error("Discord login failed — check DISCORD_BOT_TOKEN")
        except Exception:  # noqa: BLE001
            log.exception("Discord bot crashed")

    async def stop(self) -> None:
        if not self.is_closed():
            await self.close()
        if self._task:
            self._task.cancel()

    async def setup_hook(self) -> None:
        try:
            await self.tree.sync()
        except discord.HTTPException as e:
            log.warning("Slash command sync failed: %s", e)

    async def on_ready(self) -> None:
        log.info("Discord bot connected as %s", self.user)

    def is_ready_for_dm(self) -> bool:
        return self.configured and self.is_ready()

    async def _get_owner(self) -> discord.User:
        if self._owner is None:
            self._owner = await self.fetch_user(get_settings().discord_owner_user_id)
        return self._owner

    async def send_owner(self, title: str, body: str, url: str | None, kind: str) -> None:
        embed = discord.Embed(title=title, description=body[:4000], url=url,
                              color=COLORS.get(kind, 0x7F8C8D))
        embed.set_footer(text="Conserve Mass Ops")
        owner = await self._get_owner()
        await owner.send(embed=embed)

    # ------------------------------------------------------------ commands
    def _register_commands(self) -> None:
        def owner_only(interaction: discord.Interaction) -> bool:
            return interaction.user.id == get_settings().discord_owner_user_id

        ctx = app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
        inst = app_commands.allowed_installs(guilds=True, users=True)

        @self.tree.command(name="contracts", description="List open Conserve Mass courier contracts")
        @ctx
        @inst
        async def contracts_cmd(interaction: discord.Interaction) -> None:
            if not owner_only(interaction):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            from app.models import Contract
            with session_scope() as db:
                rows = db.scalars(
                    select(Contract).where(Contract.status.in_(["outstanding", "in_progress"]))
                    .order_by(Contract.date_issued)).all()
                if not rows:
                    text = "No open courier contracts. 🛌"
                else:
                    lines = []
                    for c in rows[:20]:
                        who = c.customer.name if c.customer and c.customer.name else c.issuer_id
                        a = c.start_location.system_name if c.start_location else "?"
                        b = c.end_location.system_name if c.end_location else "?"
                        when = (f"due {fmt_delta(c.deadline)}" if c.deadline
                                else f"expires {fmt_delta(c.date_expired)}")
                        lines.append(f"`{c.status:<11}` **{a} → {b}** · {fmt_m3(c.volume_m3)} · "
                                     f"{fmt_isk(c.reward_isk)} · {who} · {when}")
                    text = "\n".join(lines)
            await interaction.response.send_message(text[:1900], ephemeral=True)

        @self.tree.command(name="sync", description="Poll ESI for contracts now")
        @ctx
        @inst
        async def sync_cmd(interaction: discord.Interaction) -> None:
            if not owner_only(interaction):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            from app.jobs.scheduler import trigger_sync_now
            trigger_sync_now()
            await interaction.response.send_message(
                "Sync queued (ESI caches contracts for ~5 min).", ephemeral=True)


bot = ConserveMassBot()
