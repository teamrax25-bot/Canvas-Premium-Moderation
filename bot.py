import os
import re
import json
import time
from pathlib import Path
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

# =========================
# LOAD ENV + CONFIG
# =========================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

CONFIG_PATH = Path("config.json")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(data):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


config = load_config()

# =========================
# DISCORD INTENTS
# =========================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# =========================
# RUNTIME STORAGE
# =========================
warns = defaultdict(int)
message_times = defaultdict(lambda: deque(maxlen=20))
join_times = defaultdict(lambda: deque(maxlen=50))

LINK_REGEX = re.compile(
    r"(https?://\S+|www\.\S+|discord\.gg/\S+|discord\.com/invite/\S+)",
    re.IGNORECASE,
)

RGB_COLORS = [
    0xFF0000,
    0xFF7F00,
    0xFFFF00,
    0x00FF00,
    0x00FFFF,
    0x0000FF,
    0x8B00FF,
    0xFF1493,
]
rgb_index = 0

PRESENCE_TEXTS = [
    "🛡 Canvas server protected",
    "⚡ Anti-Raid active",
    "🌈 RGB moderation mode",
    "🚨 Watching spam & links",
    "👑 Canvas premium moderation",
]


# =========================
# HELPERS
# =========================
def next_rgb_color() -> discord.Color:
    global rgb_index
    color = RGB_COLORS[rgb_index % len(RGB_COLORS)]
    rgb_index += 1
    return discord.Color(color)


def now_utc():
    return datetime.now(timezone.utc)


def account_age_days(member: discord.Member) -> int:
    return (now_utc() - member.created_at).days


def is_owner_user(user_id: int) -> bool:
    return user_id == config.get("owner_id")


def is_whitelisted(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if member.id in config.get("whitelist_user_ids", []):
        return True

    member_role_names = {r.name for r in member.roles}
    whitelist_role_names = set(config.get("whitelist_role_names", []))
    return bool(member_role_names & whitelist_role_names)


def contains_bad_word(content: str) -> bool:
    lowered = content.lower()
    return any(word.lower() in lowered for word in config.get("bad_words", []))


def has_link(content: str) -> bool:
    return bool(LINK_REGEX.search(content))


def is_spamming(user_id: int) -> bool:
    now = time.time()
    dq = message_times[user_id]
    dq.append(now)
    recent = [t for t in dq if now - t <= config["spam_time_window"]]
    return len(recent) >= config["spam_message_limit"]


def is_mass_mention(message: discord.Message) -> bool:
    unique_mentions = {m.id for m in message.mentions}
    return len(unique_mentions) >= config["mass_mention_limit"]


def is_raid_join(guild_id: int) -> bool:
    now = time.time()
    dq = join_times[guild_id]
    recent = [t for t in dq if now - t <= config["raid_join_window"]]
    return len(recent) >= config["raid_join_limit"]


async def get_log_channel(guild: discord.Guild):
    name = config.get("log_channel_name", "canvas-mod-logs")
    for channel in guild.text_channels:
        if channel.name == name:
            return channel
    return None


async def ensure_log_channel(guild: discord.Guild):
    channel = await get_log_channel(guild)
    if channel:
        return channel

    me = guild.me or guild.self_member
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }

    for role in guild.roles:
        if role.permissions.administrator or role.permissions.manage_messages or role.permissions.manage_guild:
            overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    return await guild.create_text_channel(config["log_channel_name"], overwrites=overwrites)


async def ensure_muted_role(guild: discord.Guild):
    role = discord.utils.get(guild.roles, name=config["mute_role_name"])
    if role:
        return role

    role = await guild.create_role(name=config["mute_role_name"], reason="Canvas premium moderation bot setup")

    for channel in guild.channels:
        try:
            await channel.set_permissions(role, send_messages=False, add_reactions=False, speak=False)
        except Exception:
            pass

    return role


async def log_embed(guild: discord.Guild, title: str, description: str):
    channel = await get_log_channel(guild)
    if not channel:
        return

    embed = discord.Embed(
        title=title,
        description=description,
        color=next_rgb_color(),
        timestamp=now_utc(),
    )
    embed.set_footer(text="Canvas Premium Moderation")
    await channel.send(embed=embed)


async def send_mod_reply(target, title: str, desc: str, ephemeral=False):
    embed = discord.Embed(title=title, description=desc, color=next_rgb_color())
    embed.set_footer(text="Canvas RGB Security")
    if isinstance(target, discord.Interaction):
        if target.response.is_done():
            await target.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await target.response.send_message(embed=embed, ephemeral=ephemeral)
    else:
        await target.send(embed=embed)


async def owner_only_interaction(interaction: discord.Interaction) -> bool:
    if is_owner_user(interaction.user.id):
        return True

    await send_mod_reply(
        interaction,
        "❌ Owner Only",
        "এই command শুধু bot owner use করতে পারবে.",
        ephemeral=True,
    )
    return False


def owner_only_prefix():
    async def predicate(ctx: commands.Context):
        if not is_owner_user(ctx.author.id):
            raise commands.CheckFailure("Only owner can use this command.")
        return True

    return commands.check(predicate)


async def auto_punish(message: discord.Message, reason: str):
    try:
        await message.delete()
    except Exception:
        pass

    user_id = message.author.id
    warns[user_id] += 1
    total_warns = warns[user_id]

    # DM notice
    try:
        dm_embed = discord.Embed(
            title="⚠️ Auto Warning",
            description=(
                f"Server: **{message.guild.name}**\n"
                f"Reason: {reason}\n"
                f"Warn Count: {total_warns}/3"
            ),
            color=next_rgb_color(),
        )
        dm_embed.set_footer(text="Canvas Premium Moderation")
        await message.author.send(embed=dm_embed)
    except Exception:
        pass

    try:
        warn_embed = discord.Embed(
            title="⚠ Auto Moderation",
            description=(
                f"{message.author.mention}, তোমার মেসেজ remove করা হয়েছে.\n"
                f"**Reason:** {reason}\n**Warns:** {total_warns}/3"
            ),
            color=next_rgb_color(),
        )
        await message.channel.send(embed=warn_embed, delete_after=6)
    except Exception:
        pass

    await log_embed(
        message.guild,
        "🚨 Auto Punish Triggered",
        f"**User:** {message.author.mention}\n"
        f"**Channel:** {message.channel.mention}\n"
        f"**Reason:** {reason}\n"
        f"**Warn Count:** {total_warns}",
    )

    if config.get("auto_punish", True) and total_warns >= 3:
        try:
            await message.author.timeout(timedelta(minutes=10), reason=f"Auto moderation: {reason}")
            warns[user_id] = 0
            await log_embed(
                message.guild,
                "⏳ Auto Timeout",
                f"**User:** {message.author.mention}\n**Duration:** 10 minutes\n**Reason:** 3 warnings reached",
            )
        except Exception:
            await log_embed(
                message.guild,
                "⚠ Timeout Failed",
                f"**User:** {message.author.mention}\nBot lacks permission or role is too low.",
            )


# =========================
# EVENTS
# =========================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Slash commands synced: {len(synced)}")
    except Exception as e:
        print("Sync error:", e)

    if not rotate_presence.is_running():
        rotate_presence.start()


@bot.event
async def on_member_join(member: discord.Member):
    if not config.get("anti_raid", True):
        return

    join_times[member.guild.id].append(time.time())

    age_days = account_age_days(member)
    suspicious = age_days < config["new_account_days_limit"]
    raid_detected = is_raid_join(member.guild.id)

    if suspicious:
        await log_embed(
            member.guild,
            "🆕 Suspicious New Account",
            f"**User:** {member.mention}\n"
            f"**Account Age:** {age_days} day(s)\n"
            f"**Joined:** <t:{int(now_utc().timestamp())}:R>",
        )

    if raid_detected:
        await log_embed(
            member.guild,
            "🚨 Anti-Raid Alert",
            f"Too many users joined quickly.\n"
            f"**Threshold:** {config['raid_join_limit']} joins in {config['raid_join_window']} seconds.",
        )


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    if is_whitelisted(message.author):
        await bot.process_commands(message)
        return

    content = message.content or ""

    if config.get("anti_link", True) and has_link(content):
        await auto_punish(message, "Unauthorized link detected")
        return

    if config.get("anti_badword", True) and contains_bad_word(content):
        await auto_punish(message, "Bad word detected")
        return

    if config.get("anti_mass_mention", True) and is_mass_mention(message):
        await auto_punish(message, "Mass mention detected")
        return

    if config.get("anti_spam", True) and is_spamming(message.author.id):
        await auto_punish(message, "Spam detected")
        return

    await bot.process_commands(message)


# =========================
# PRESENCE LOOP
# =========================
@tasks.loop(seconds=12)
async def rotate_presence():
    text = PRESENCE_TEXTS[int(time.time() // 12) % len(PRESENCE_TEXTS)]
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name=text),
        status=discord.Status.online,
    )


# =========================
# SLASH COMMANDS
# =========================
class ModGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="mod", description="Canvas premium moderation commands")

    @app_commands.command(name="setup", description="Setup log channel and muted role")
    async def setup(self, interaction: discord.Interaction):
        if not await owner_only_interaction(interaction):
            return
        await ensure_log_channel(interaction.guild)
        await ensure_muted_role(interaction.guild)
        await send_mod_reply(interaction, "✅ Setup Complete", "Log channel and muted role are ready.", ephemeral=True)

    @app_commands.command(name="warn", description="Warn a member")
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not await owner_only_interaction(interaction):
            return

        warns[member.id] += 1

        try:
            dm_embed = discord.Embed(
                title="⚠️ You Got Warned",
                description=(
                    f"Server: **{interaction.guild.name}**\n"
                    f"Reason: {reason}\n"
                    f"Warn Count: {warns[member.id]}"
                ),
                color=next_rgb_color(),
            )
            dm_embed.set_footer(text="Canvas Premium Moderation")
            await member.send(embed=dm_embed)
        except Exception:
            pass

        await send_mod_reply(
            interaction,
            "⚠ Member Warned",
            f"**User:** {member.mention}\n**Reason:** {reason}\n**Warns:** {warns[member.id]}",
            ephemeral=False,
        )
        await log_embed(
            interaction.guild,
            "⚠ Manual Warn",
            f"**Moderator:** {interaction.user.mention}\n"
            f"**User:** {member.mention}\n"
            f"**Reason:** {reason}\n"
            f"**Warns:** {warns[member.id]}",
        )

    @app_commands.command(name="clearwarns", description="Clear all warns of a member")
    async def clearwarns(self, interaction: discord.Interaction, member: discord.Member):
        if not await owner_only_interaction(interaction):
            return
        warns[member.id] = 0
        await send_mod_reply(interaction, "✅ Warns Cleared", f"{member.mention} এর warns reset করা হয়েছে.", ephemeral=False)

    @app_commands.command(name="mute", description="Timeout a member")
    async def mute(self, interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 10080], reason: str = "No reason provided"):
        if not await owner_only_interaction(interaction):
            return
        try:
            await member.timeout(timedelta(minutes=minutes), reason=reason)
            await send_mod_reply(
                interaction,
                "⏳ Member Timed Out",
                f"**User:** {member.mention}\n**Duration:** {minutes} minute(s)\n**Reason:** {reason}",
                ephemeral=False,
            )
            await log_embed(
                interaction.guild,
                "⏳ Manual Timeout",
                f"**Moderator:** {interaction.user.mention}\n**User:** {member.mention}\n**Duration:** {minutes} minute(s)\n**Reason:** {reason}",
            )
        except Exception as e:
            await send_mod_reply(interaction, "❌ Failed", f"Timeout করা যায়নি.\n`{e}`", ephemeral=True)

    @app_commands.command(name="unmute", description="Remove timeout from a member")
    async def unmute(self, interaction: discord.Interaction, member: discord.Member):
        if not await owner_only_interaction(interaction):
            return
        try:
            await member.timeout(None, reason="Manual untimeout")
            await send_mod_reply(interaction, "✅ Timeout Removed", f"{member.mention} এখন unmuted.", ephemeral=False)
        except Exception as e:
            await send_mod_reply(interaction, "❌ Failed", f"Untimeout করা যায়নি.\n`{e}`", ephemeral=True)

    @app_commands.command(name="kick", description="Kick a member")
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not await owner_only_interaction(interaction):
            return
        try:
            await member.kick(reason=reason)
            await send_mod_reply(interaction, "👢 Member Kicked", f"**User:** {member}\n**Reason:** {reason}", ephemeral=False)
            await log_embed(interaction.guild, "👢 Kick Action", f"**Moderator:** {interaction.user.mention}\n**User:** {member}\n**Reason:** {reason}")
        except Exception as e:
            await send_mod_reply(interaction, "❌ Failed", f"Kick করা যায়নি.\n`{e}`", ephemeral=True)

    @app_commands.command(name="ban", description="Ban a member")
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not await owner_only_interaction(interaction):
            return
        try:
            await member.ban(reason=reason, delete_message_days=0)
            await send_mod_reply(interaction, "🔨 Member Banned", f"**User:** {member}\n**Reason:** {reason}", ephemeral=False)
            await log_embed(interaction.guild, "🔨 Ban Action", f"**Moderator:** {interaction.user.mention}\n**User:** {member}\n**Reason:** {reason}")
        except Exception as e:
            await send_mod_reply(interaction, "❌ Failed", f"Ban করা যায়নি.\n`{e}`", ephemeral=True)

    @app_commands.command(name="purge", description="Delete messages")
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
        if not await owner_only_interaction(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        embed = discord.Embed(
            title="🧹 Purge Complete",
            description=f"Deleted **{len(deleted)}** message(s).",
            color=next_rgb_color(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        await log_embed(
            interaction.guild,
            "🧹 Purge Action",
            f"**Moderator:** {interaction.user.mention}\n**Channel:** {interaction.channel.mention}\n**Deleted:** {len(deleted)} messages",
        )

    @app_commands.command(name="whitelistadd", description="Add a user to whitelist")
    async def whitelistadd(self, interaction: discord.Interaction, member: discord.Member):
        if not await owner_only_interaction(interaction):
            return
        if member.id not in config["whitelist_user_ids"]:
            config["whitelist_user_ids"].append(member.id)
            save_config(config)
        await send_mod_reply(interaction, "✅ Whitelist Updated", f"{member.mention} is now whitelisted.", ephemeral=True)

    @app_commands.command(name="whitelistremove", description="Remove a user from whitelist")
    async def whitelistremove(self, interaction: discord.Interaction, member: discord.Member):
        if not await owner_only_interaction(interaction):
            return
        if member.id in config["whitelist_user_ids"]:
            config["whitelist_user_ids"].remove(member.id)
            save_config(config)
        await send_mod_reply(interaction, "✅ Whitelist Updated", f"{member.mention} removed from whitelist.", ephemeral=True)

    @app_commands.command(name="toggle", description="Toggle protection modules")
    @app_commands.choices(feature=[
        app_commands.Choice(name="anti_link", value="anti_link"),
        app_commands.Choice(name="anti_badword", value="anti_badword"),
        app_commands.Choice(name="anti_spam", value="anti_spam"),
        app_commands.Choice(name="anti_raid", value="anti_raid"),
        app_commands.Choice(name="anti_mass_mention", value="anti_mass_mention"),
        app_commands.Choice(name="auto_punish", value="auto_punish"),
    ])
    async def toggle(self, interaction: discord.Interaction, feature: app_commands.Choice[str], enabled: bool):
        if not await owner_only_interaction(interaction):
            return
        config[feature.value] = enabled
        save_config(config)
        await send_mod_reply(interaction, "⚙ Config Updated", f"**{feature.value}** সেট করা হয়েছে: **{enabled}**", ephemeral=True)

    @app_commands.command(name="config", description="Show current config")
    async def configshow(self, interaction: discord.Interaction):
        if not await owner_only_interaction(interaction):
            return
        desc = (
            f"**owner_id:** {config['owner_id']}\n"
            f"**anti_link:** {config['anti_link']}\n"
            f"**anti_badword:** {config['anti_badword']}\n"
            f"**anti_spam:** {config['anti_spam']}\n"
            f"**anti_raid:** {config['anti_raid']}\n"
            f"**anti_mass_mention:** {config['anti_mass_mention']}\n"
            f"**auto_punish:** {config['auto_punish']}\n"
            f"**spam limit:** {config['spam_message_limit']} / {config['spam_time_window']}s\n"
            f"**raid limit:** {config['raid_join_limit']} / {config['raid_join_window']}s\n"
            f"**new acc age limit:** {config['new_account_days_limit']} day(s)"
        )
        await send_mod_reply(interaction, "📋 Current Config", desc, ephemeral=True)


bot.tree.add_command(ModGroup())


# =========================
# ERRORS
# =========================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await send_mod_reply(interaction, "⚠ Error", f"`{error}`", ephemeral=True)


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CheckFailure):
        embed = discord.Embed(
            title="❌ Owner Only",
            description="এই command শুধু bot owner use করতে পারবে.",
            color=next_rgb_color(),
        )
        await ctx.send(embed=embed, delete_after=5)
        return

    embed = discord.Embed(
        title="⚠ Error",
        description=f"`{error}`",
        color=next_rgb_color(),
    )
    await ctx.send(embed=embed, delete_after=8)


# =========================
# PREFIX COMMANDS
# =========================
@bot.command()
@owner_only_prefix()
async def ping(ctx: commands.Context):
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"Latency: **{round(bot.latency * 1000)}ms**",
        color=next_rgb_color(),
    )
    await ctx.send(embed=embed)


@bot.command()
@owner_only_prefix()
async def sync(ctx: commands.Context):
    synced = await bot.tree.sync()
    await send_mod_reply(ctx, "✅ Synced", f"Synced **{len(synced)}** slash command(s).")


# =========================
# RUN
# =========================
if not TOKEN:
    raise ValueError("DISCORD_TOKEN missing in .env")

bot.run(TOKEN)
