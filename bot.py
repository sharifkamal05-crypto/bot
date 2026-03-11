import discord
from discord.ext import commands
from discord import app_commands
import os
import tempfile
import aiohttp
import subprocess
import asyncio
import json
import re
import time
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

# ── config ──────────────────────────────────────────────────────────────────

TOKEN           = os.environ.get("DISCORD_TOKEN")
STREAMABLE_USER = os.environ.get("STREAMABLE_USER")
STREAMABLE_PASS = os.environ.get("STREAMABLE_PASS")

MAX_UPLOAD_MB   = 25
TEMP            = Path(tempfile.gettempdir()) / "video_bot"
TEMP.mkdir(exist_ok=True)

WARNS_FILE = Path("warns.json")

# ── bot setup ────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members         = True
intents.guilds          = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ── warn storage ─────────────────────────────────────────────────────────────

def load_warns():
    if WARNS_FILE.exists():
        return json.loads(WARNS_FILE.read_text())
    return {}

def save_warns(data):
    WARNS_FILE.write_text(json.dumps(data, indent=2))

warns_db: dict = load_warns()   # { guild_id: { user_id: [ {reason, mod, ts}, ... ] } }

# ── rate limiter ──────────────────────────────────────────────────────────────

command_cooldowns: dict[int, float] = {}
COOLDOWN_SECONDS = 10

def is_on_cooldown(user_id: int) -> float:
    last = command_cooldowns.get(user_id, 0)
    diff = time.time() - last
    if diff < COOLDOWN_SECONDS:
        return COOLDOWN_SECONDS - diff
    command_cooldowns[user_id] = time.time()
    return 0.0

# ── helpers ───────────────────────────────────────────────────────────────────

def ffmpeg(*args) -> subprocess.CompletedProcess:
    cmd = ["ffmpeg", "-y", *map(str, args)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-800:])
    return result


def size_mb(path: Path) -> float:
    return path.stat().st_size / 1_048_576


async def download(url: str, dest: Path):
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in r.content.iter_chunked(65_536):
                    f.write(chunk)


async def upload_streamable(path: Path) -> str:
    if not STREAMABLE_USER or not STREAMABLE_PASS:
        raise RuntimeError("Streamable credentials not configured.")
    auth = aiohttp.BasicAuth(STREAMABLE_USER, STREAMABLE_PASS)
    async with aiohttp.ClientSession(auth=auth) as session:
        data = aiohttp.FormData()
        data.add_field("file", open(path, "rb"), filename=path.name)
        async with session.post("https://api.streamable.com/upload", data=data) as r:
            res = await r.json()
    return f"https://streamable.com/{res['shortcode']}"


async def send_result(ctx: commands.Context, file: Path):
    mb = size_mb(file)
    if mb > MAX_UPLOAD_MB:
        url = await upload_streamable(file)
        await ctx.reply(f"📦 Datei zu groß für Discord ({mb:.1f} MB)\n🔗 {url}")
    else:
        await ctx.reply(file=discord.File(file))


async def get_input_file(ctx: commands.Context, url: str | None = None, index: int = 0) -> Path:
    if ctx.message.attachments and index < len(ctx.message.attachments):
        att  = ctx.message.attachments[index]
        path = TEMP / f"in_{ctx.message.id}_{index}{Path(att.filename).suffix}"
        await download(att.url, path)
        return path
    if url:
        path = TEMP / f"url_{ctx.message.id}_{index}.mp4"
        await download(url, path)
        return path
    raise RuntimeError("Kein Video oder Link angegeben.")


def unique(ctx: commands.Context, name: str, ext: str = ".mp4") -> Path:
    return TEMP / f"{name}_{ctx.message.id}{ext}"


def check_mod(ctx: commands.Context):
    if not ctx.author.guild_permissions.manage_messages:
        raise commands.MissingPermissions(["manage_messages"])


# ═══════════════════════════════════════════════════════════════════════════════
#  VIDEO COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(help="Schneidet ein Video zu. Verwendung: !trim <start_sek> <end_sek> [url]")
async def trim(ctx: commands.Context, start: float, end: float, url: str = None):
    if wait := is_on_cooldown(ctx.author.id):
        return await ctx.reply(f"⏳ Cooldown – warte noch {wait:.1f}s.")
    async with ctx.typing():
        inp = out = None
        try:
            inp = await get_input_file(ctx, url)
            out = unique(ctx, "trim")
            ffmpeg("-ss", start, "-i", inp, "-t", end - start,
                   "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", out)
            await send_result(ctx, out)
        finally:
            for p in (inp, out):
                if p: p.unlink(missing_ok=True)


@bot.command(help="Fügt mehrere Videos zusammen. Dateien anhängen oder URLs angeben.")
async def merge(ctx: commands.Context, *urls):
    if wait := is_on_cooldown(ctx.author.id):
        return await ctx.reply(f"⏳ Cooldown – warte noch {wait:.1f}s.")
    paths, re_paths, out = [], [], None
    async with ctx.typing():
        try:
            if ctx.message.attachments:
                for i, att in enumerate(ctx.message.attachments):
                    p = TEMP / f"m_{ctx.message.id}_{i}{Path(att.filename).suffix}"
                    await download(att.url, p)
                    paths.append(p)
            else:
                for i, url in enumerate(urls):
                    p = TEMP / f"m_{ctx.message.id}_{i}.mp4"
                    await download(url, p)
                    paths.append(p)

            if len(paths) < 2:
                return await ctx.reply("❌ Mindestens 2 Videos benötigt.")

            for i, p in enumerate(paths):
                r = TEMP / f"re_{ctx.message.id}_{i}.mp4"
                ffmpeg("-i", p, "-c:v", "libx264", "-c:a", "aac",
                       "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                       r)
                re_paths.append(r)

            lst = TEMP / f"list_{ctx.message.id}.txt"
            lst.write_text("\n".join(f"file '{p}'" for p in re_paths))

            out = unique(ctx, "merge")
            ffmpeg("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out)
            lst.unlink(missing_ok=True)

            await send_result(ctx, out)
        finally:
            for p in paths + re_paths + ([out] if out else []):
                if p: p.unlink(missing_ok=True)


@bot.command(help="Fügt Musik zu einem Video hinzu. !addmusic <lautstärke 0-1> [video_url] [audio_url]")
async def addmusic(ctx: commands.Context, volume: float = 0.5, video_url: str = None, audio_url: str = None):
    if wait := is_on_cooldown(ctx.author.id):
        return await ctx.reply(f"⏳ Cooldown – warte noch {wait:.1f}s.")
    vid = aud = out = None
    async with ctx.typing():
        try:
            if len(ctx.message.attachments) >= 2:
                vid = TEMP / f"vid_{ctx.message.id}.mp4"
                aud = TEMP / f"aud_{ctx.message.id}.mp3"
                await download(ctx.message.attachments[0].url, vid)
                await download(ctx.message.attachments[1].url, aud)
            elif video_url and audio_url:
                vid = TEMP / f"vid_{ctx.message.id}.mp4"
                aud = TEMP / f"aud_{ctx.message.id}.mp3"
                await download(video_url, vid)
                await download(audio_url, aud)
            else:
                return await ctx.reply("❌ Video + Audio anhängen oder beide URLs angeben.")

            out = unique(ctx, "music")
            ffmpeg(
                "-i", vid,
                "-stream_loop", "-1", "-i", aud,
                "-filter_complex",
                f"[0:a]volume=0.4[a0];[1:a]volume={volume}[a1];[a0][a1]amix=inputs=2:normalize=0[aout]",
                "-map", "0:v",
                "-map", "[aout]",
                "-c:v", "libx264",
                "-c:a", "aac",
                "-shortest",
                "-movflags", "+faststart",
                out
            )
            await send_result(ctx, out)
        finally:
            for p in (vid, aud, out):
                if p: p.unlink(missing_ok=True)


@bot.command(help="Konvertiert ein Video in ein GIF. !togif [fps] [width] [url]")
async def togif(ctx: commands.Context, fps: int = 15, width: int = 480, url: str = None):
    if wait := is_on_cooldown(ctx.author.id):
        return await ctx.reply(f"⏳ Cooldown – warte noch {wait:.1f}s.")
    inp = out = None
    async with ctx.typing():
        try:
            inp = await get_input_file(ctx, url)
            out = unique(ctx, "gif", ".gif")
            palette = TEMP / f"pal_{ctx.message.id}.png"
            ffmpeg("-i", inp, "-vf", f"fps={fps},scale={width}:-1:flags=lanczos,palettegen", palette)
            ffmpeg("-i", inp, "-i", palette,
                   "-filter_complex", f"fps={fps},scale={width}:-1:flags=lanczos[x];[x][1:v]paletteuse",
                   out)
            palette.unlink(missing_ok=True)
            await send_result(ctx, out)
        finally:
            for p in (inp, out):
                if p: p.unlink(missing_ok=True)


@bot.command(help="Extrahiert Audio aus einem Video als MP3. !extractaudio [url]")
async def extractaudio(ctx: commands.Context, url: str = None):
    if wait := is_on_cooldown(ctx.author.id):
        return await ctx.reply(f"⏳ Cooldown – warte noch {wait:.1f}s.")
    inp = out = None
    async with ctx.typing():
        try:
            inp = await get_input_file(ctx, url)
            out = unique(ctx, "audio", ".mp3")
            ffmpeg("-i", inp, "-vn", "-c:a", "libmp3lame", "-q:a", "2", out)
            await send_result(ctx, out)
        finally:
            for p in (inp, out):
                if p: p.unlink(missing_ok=True)


@bot.command(help="Ändert die Geschwindigkeit eines Videos. !speed <faktor> [url]  (0.5 = halb so schnell, 2 = doppelt)")
async def speed(ctx: commands.Context, factor: float = 1.5, url: str = None):
    if wait := is_on_cooldown(ctx.author.id):
        return await ctx.reply(f"⏳ Cooldown – warte noch {wait:.1f}s.")
    if not 0.25 <= factor <= 4.0:
        return await ctx.reply("❌ Faktor muss zwischen 0.25 und 4.0 liegen.")
    inp = out = None
    async with ctx.typing():
        try:
            inp  = await get_input_file(ctx, url)
            out  = unique(ctx, "speed")
            vpts = 1.0 / factor
            ffmpeg("-i", inp,
                   "-filter_complex", f"[0:v]setpts={vpts}*PTS[v];[0:a]atempo={factor}[a]",
                   "-map", "[v]", "-map", "[a]",
                   "-c:v", "libx264", "-c:a", "aac", out)
            await send_result(ctx, out)
        finally:
            for p in (inp, out):
                if p: p.unlink(missing_ok=True)


@bot.command(help="Komprimiert ein Video. !compress <ziel_mb> [url]")
async def compress(ctx: commands.Context, target_mb: float = 8.0, url: str = None):
    if wait := is_on_cooldown(ctx.author.id):
        return await ctx.reply(f"⏳ Cooldown – warte noch {wait:.1f}s.")
    inp = out = None
    async with ctx.typing():
        try:
            inp = await get_input_file(ctx, url)
            # probe duration
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(inp)],
                capture_output=True, text=True)
            duration = float(probe.stdout.strip() or 0)
            if duration <= 0:
                raise RuntimeError("Konnte Videolänge nicht ermitteln.")
            target_kbps = int((target_mb * 8 * 1024) / duration)
            out = unique(ctx, "compressed")
            ffmpeg("-i", inp, "-b:v", f"{max(target_kbps - 128, 100)}k",
                   "-b:a", "128k", "-c:v", "libx264", "-c:a", "aac",
                   "-movflags", "+faststart", out)
            await send_result(ctx, out)
        finally:
            for p in (inp, out):
                if p: p.unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  MODERATION COMMANDS  (require Manage Messages)
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(help="Löscht Nachrichten. !clear <anzahl>")
@commands.has_permissions(manage_messages=True)
async def clear(ctx: commands.Context, amount: int = 10):
    if amount < 1 or amount > 500:
        return await ctx.reply("❌ Zwischen 1 und 500 Nachrichten.")
    deleted = await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(f"🗑️ {len(deleted)-1} Nachrichten gelöscht.")
    await asyncio.sleep(4)
    await msg.delete()


@bot.command(help="Verwarnt einen User. !warn @user <grund>")
@commands.has_permissions(manage_messages=True)
async def warn(ctx: commands.Context, member: discord.Member, *, reason: str = "Kein Grund angegeben"):
    gid = str(ctx.guild.id)
    uid = str(member.id)
    warns_db.setdefault(gid, {}).setdefault(uid, []).append({
        "reason": reason,
        "mod": str(ctx.author),
        "ts": datetime.now(timezone.utc).isoformat()
    })
    save_warns(warns_db)
    count = len(warns_db[gid][uid])
    embed = discord.Embed(title="⚠️ Verwarnung", color=0xF59E0B)
    embed.add_field(name="User", value=member.mention)
    embed.add_field(name="Grund", value=reason)
    embed.add_field(name="Verwarnungen gesamt", value=str(count))
    embed.set_footer(text=f"Moderator: {ctx.author}")
    await ctx.send(embed=embed)
    try:
        await member.send(f"⚠️ Du wurdest auf **{ctx.guild.name}** verwarnt.\n**Grund:** {reason}")
    except discord.Forbidden:
        pass


@bot.command(help="Zeigt Verwarnungen eines Users. !warnings @user")
@commands.has_permissions(manage_messages=True)
async def warnings(ctx: commands.Context, member: discord.Member):
    gid, uid = str(ctx.guild.id), str(member.id)
    user_warns = warns_db.get(gid, {}).get(uid, [])
    if not user_warns:
        return await ctx.reply(f"✅ {member.mention} hat keine Verwarnungen.")
    embed = discord.Embed(title=f"Verwarnungen – {member.display_name}", color=0xF59E0B)
    for i, w in enumerate(user_warns, 1):
        embed.add_field(name=f"#{i} – {w['ts'][:10]}", value=f"**Grund:** {w['reason']}\n**Mod:** {w['mod']}", inline=False)
    await ctx.reply(embed=embed)


@bot.command(help="Entfernt eine Verwarnung. !clearwarn @user <index>")
@commands.has_permissions(manage_messages=True)
async def clearwarn(ctx: commands.Context, member: discord.Member, index: int):
    gid, uid = str(ctx.guild.id), str(member.id)
    user_warns = warns_db.get(gid, {}).get(uid, [])
    if not user_warns or index < 1 or index > len(user_warns):
        return await ctx.reply("❌ Ungültiger Index.")
    removed = user_warns.pop(index - 1)
    save_warns(warns_db)
    await ctx.reply(f"✅ Verwarnung #{index} von {member.mention} entfernt. (Grund war: {removed['reason']})")


@bot.command(help="Kickt einen User. !kick @user [grund]")
@commands.has_permissions(kick_members=True)
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "Kein Grund"):
    await member.kick(reason=reason)
    await ctx.reply(f"👢 {member.mention} wurde gekickt. Grund: {reason}")


@bot.command(help="Bannt einen User. !ban @user [grund]")
@commands.has_permissions(ban_members=True)
async def ban(ctx: commands.Context, member: discord.Member, *, reason: str = "Kein Grund"):
    await member.ban(reason=reason)
    await ctx.reply(f"🔨 {member.mention} wurde gebannt. Grund: {reason}")


@bot.command(help="Entbannt einen User. !unban <user_id>")
@commands.has_permissions(ban_members=True)
async def unban(ctx: commands.Context, user_id: int):
    user = await bot.fetch_user(user_id)
    await ctx.guild.unban(user)
    await ctx.reply(f"✅ {user} wurde entbannt.")


@bot.command(help="Mutet einen User (Server-Timeout). !mute @user <minuten> [grund]")
@commands.has_permissions(moderate_members=True)
async def mute(ctx: commands.Context, member: discord.Member, minutes: int = 10, *, reason: str = "Kein Grund"):
    from datetime import timedelta
    until = discord.utils.utcnow() + timedelta(minutes=minutes)
    await member.timeout(until, reason=reason)
    await ctx.reply(f"🔇 {member.mention} wurde für {minutes} Minuten gemutet. Grund: {reason}")


@bot.command(help="Hebt Mute auf. !unmute @user")
@commands.has_permissions(moderate_members=True)
async def unmute(ctx: commands.Context, member: discord.Member):
    await member.timeout(None)
    await ctx.reply(f"🔊 {member.mention} wurde geunmutet.")


@bot.command(help="Sperrt einen Kanal für alle. !lockdown [grund]")
@commands.has_permissions(manage_channels=True)
async def lockdown(ctx: commands.Context, *, reason: str = "Kein Grund"):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send(f"🔒 Kanal gesperrt. Grund: {reason}")


@bot.command(help="Entsperrt einen Kanal. !unlock")
@commands.has_permissions(manage_channels=True)
async def unlock(ctx: commands.Context):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send("🔓 Kanal entsperrt.")


# ═══════════════════════════════════════════════════════════════════════════════
#  SERVER INFO COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(help="Zeigt Server-Infos.")
async def serverinfo(ctx: commands.Context):
    g = ctx.guild
    embed = discord.Embed(title=g.name, color=0x5865F2, timestamp=datetime.now(timezone.utc))
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="👑 Owner", value=g.owner.mention if g.owner else "?")
    embed.add_field(name="👥 Mitglieder", value=str(g.member_count))
    embed.add_field(name="📢 Kanäle", value=str(len(g.channels)))
    embed.add_field(name="🎭 Rollen", value=str(len(g.roles)))
    embed.add_field(name="😀 Emojis", value=str(len(g.emojis)))
    embed.add_field(name="📅 Erstellt", value=g.created_at.strftime("%d.%m.%Y"))
    embed.add_field(name="🌍 Region / Tier", value=f"Boost Level {g.premium_tier}")
    await ctx.reply(embed=embed)


@bot.command(help="Zeigt User-Infos. !userinfo [@user]")
async def userinfo(ctx: commands.Context, member: discord.Member = None):
    m = member or ctx.author
    embed = discord.Embed(title=str(m), color=m.color, timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=m.display_avatar.url)
    embed.add_field(name="🆔 ID", value=str(m.id))
    embed.add_field(name="📅 Account erstellt", value=m.created_at.strftime("%d.%m.%Y"))
    embed.add_field(name="📥 Beigetreten", value=m.joined_at.strftime("%d.%m.%Y") if m.joined_at else "?")
    roles = [r.mention for r in m.roles if r.name != "@everyone"]
    embed.add_field(name=f"🎭 Rollen ({len(roles)})", value=" ".join(roles) or "Keine", inline=False)
    gid, uid = str(ctx.guild.id), str(m.id)
    wc = len(warns_db.get(gid, {}).get(uid, []))
    embed.add_field(name="⚠️ Verwarnungen", value=str(wc))
    await ctx.reply(embed=embed)


@bot.command(help="Zeigt den Avatar eines Users. !avatar [@user]")
async def avatar(ctx: commands.Context, member: discord.Member = None):
    m = member or ctx.author
    embed = discord.Embed(title=f"Avatar von {m.display_name}", color=0x5865F2)
    embed.set_image(url=m.display_avatar.url)
    await ctx.reply(embed=embed)


@bot.command(help="Zeigt die Top 10 Rollen nach Mitgliederzahl.")
async def toproles(ctx: commands.Context):
    roles = sorted(
        [r for r in ctx.guild.roles if r.name != "@everyone"],
        key=lambda r: len(r.members), reverse=True
    )[:10]
    embed = discord.Embed(title="🎭 Top Rollen", color=0x5865F2)
    for r in roles:
        embed.add_field(name=r.name, value=f"{len(r.members)} Mitglieder", inline=True)
    await ctx.reply(embed=embed)


@bot.command(help="Zeigt die Ping-Latenz des Bots.")
async def ping(ctx: commands.Context):
    await ctx.reply(f"🏓 Pong! Latenz: **{round(bot.latency * 1000)}ms**")


@bot.command(help="Sendet eine Embed-Nachricht. !say <nachricht>")
@commands.has_permissions(manage_messages=True)
async def say(ctx: commands.Context, *, message: str):
    await ctx.message.delete()
    embed = discord.Embed(description=message, color=0x5865F2)
    embed.set_footer(text=f"via {ctx.guild.name}")
    await ctx.send(embed=embed)


@bot.command(help="Erstellt eine Abstimmung. !poll <frage> | <option1> | <option2> ...")
async def poll(ctx: commands.Context, *, content: str):
    parts = [p.strip() for p in content.split("|")]
    if len(parts) < 2:
        return await ctx.reply("❌ Format: `!poll Frage | Option1 | Option2`")
    question, options = parts[0], parts[1:]
    if len(options) > 9:
        return await ctx.reply("❌ Maximal 9 Optionen.")
    emojis = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣"]
    embed = discord.Embed(title=f"📊 {question}", color=0x5865F2,
                          timestamp=datetime.now(timezone.utc))
    for i, opt in enumerate(options):
        embed.add_field(name=f"{emojis[i]} {opt}", value="\u200b", inline=False)
    embed.set_footer(text=f"Abstimmung von {ctx.author.display_name}")
    msg = await ctx.send(embed=embed)
    for i in range(len(options)):
        await msg.add_reaction(emojis[i])
    await ctx.message.delete()


# ═══════════════════════════════════════════════════════════════════════════════
#  HELP
# ═══════════════════════════════════════════════════════════════════════════════

bot.remove_command("help")

@bot.command()
async def help(ctx: commands.Context, category: str = None):
    if category == "video":
        embed = discord.Embed(title="🎬 Video Commands", color=0x5865F2)
        embed.add_field(name="✂️ !trim <start> <end> [url]",       value="Video trimmen",              inline=False)
        embed.add_field(name="🔗 !merge [url1 url2 ...]",           value="Videos zusammenfügen",       inline=False)
        embed.add_field(name="🎵 !addmusic <vol> [vid] [aud]",      value="Musik hinzufügen (0-1)",     inline=False)
        embed.add_field(name="🖼️ !togif [fps] [width] [url]",       value="Video → GIF",                inline=False)
        embed.add_field(name="🎧 !extractaudio [url]",              value="Audio als MP3 extrahieren",  inline=False)
        embed.add_field(name="⚡ !speed <faktor> [url]",            value="Geschwindigkeit ändern",     inline=False)
        embed.add_field(name="📦 !compress <ziel_mb> [url]",        value="Video komprimieren",         inline=False)
        embed.set_footer(text="Dateien über 25MB → Streamable Upload")
    elif category == "mod":
        embed = discord.Embed(title="🔨 Moderations-Commands", color=0xEF4444)
        embed.add_field(name="🗑️ !clear <n>",             value="Nachrichten löschen",         inline=False)
        embed.add_field(name="⚠️ !warn @user <grund>",    value="User verwarnen",              inline=False)
        embed.add_field(name="📋 !warnings @user",         value="Verwarnungen anzeigen",       inline=False)
        embed.add_field(name="✅ !clearwarn @user <idx>",  value="Verwarnung entfernen",        inline=False)
        embed.add_field(name="👢 !kick @user [grund]",    value="User kicken",                 inline=False)
        embed.add_field(name="🔨 !ban @user [grund]",     value="User bannen",                 inline=False)
        embed.add_field(name="✅ !unban <id>",             value="User entbannen",              inline=False)
        embed.add_field(name="🔇 !mute @user <min>",      value="User muten (Timeout)",        inline=False)
        embed.add_field(name="🔊 !unmute @user",          value="Mute aufheben",               inline=False)
        embed.add_field(name="🔒 !lockdown [grund]",      value="Kanal sperren",               inline=False)
        embed.add_field(name="🔓 !unlock",                value="Kanal entsperren",            inline=False)
    elif category == "server":
        embed = discord.Embed(title="ℹ️ Server-Commands", color=0x22C55E)
        embed.add_field(name="🏠 !serverinfo",            value="Server-Infos",                inline=False)
        embed.add_field(name="👤 !userinfo [@user]",      value="User-Infos",                  inline=False)
        embed.add_field(name="🖼️ !avatar [@user]",        value="Avatar anzeigen",             inline=False)
        embed.add_field(name="🎭 !toproles",              value="Top 10 Rollen",               inline=False)
        embed.add_field(name="🏓 !ping",                  value="Bot-Latenz",                  inline=False)
        embed.add_field(name="📢 !say <text>",            value="Embed-Nachricht senden (Mod)",inline=False)
        embed.add_field(name="📊 !poll Frage|Opt1|Opt2",  value="Abstimmung erstellen",        inline=False)
    else:
        embed = discord.Embed(
            title="🤖 Bot Help",
            description="Wähle eine Kategorie:",
            color=0x5865F2
        )
        embed.add_field(name="🎬 !help video",  value="Video-Bearbeitungs-Commands", inline=False)
        embed.add_field(name="🔨 !help mod",    value="Moderations-Commands",        inline=False)
        embed.add_field(name="ℹ️ !help server", value="Server-Info-Commands",        inline=False)
        embed.set_footer(text="Dateien über 25MB werden automatisch auf Streamable hochgeladen.")
    await ctx.reply(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
#  ERROR HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        return await ctx.reply("❌ Du hast keine Berechtigung für diesen Befehl.")
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.reply(f"❌ Fehlende Argumente. Schreib `!help` für Hilfe.")
    if isinstance(error, commands.BadArgument):
        return await ctx.reply("❌ Ungültiger Parameter. Schreib `!help` für Hilfe.")
    if isinstance(error, commands.CommandInvokeError):
        original = error.original
        await ctx.reply(f"❌ Fehler: {original}")
        raise original
    await ctx.reply(f"❌ Unbekannter Fehler: {error}")
    raise error


# ═══════════════════════════════════════════════════════════════════════════════
#  READY
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    print(f"✅ Eingeloggt als {bot.user} (ID: {bot.user.id})")
    print("━" * 40)
    print("Video:  !trim !merge !addmusic !togif !extractaudio !speed !compress")
    print("Mod:    !warn !warnings !clearwarn !kick !ban !unban !mute !unmute !clear !lockdown !unlock")
    print("Server: !serverinfo !userinfo !avatar !toproles !ping !say !poll")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="!help | Video Bot"
    ))


bot.run(TOKEN)
