import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import tempfile
import aiohttp
import subprocess
from pathlib import Path

TOKEN = os.environ.get("DISCORD_TOKEN")

STREAMABLE_USER = os.environ.get("STREAMABLE_USER")
STREAMABLE_PASS = os.environ.get("STREAMABLE_PASS")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

TEMP = Path(tempfile.gettempdir()) / "discord_video_bot"
TEMP.mkdir(exist_ok=True)

MAX_UPLOAD_MB = 25


# ── helpers ─────────────────────────────────────────

def ffmpeg(*args, capture=True):
    cmd = ["ffmpeg", "-y", *[str(a) for a in args]]
    result = subprocess.run(cmd, capture_output=capture, text=True)

    if result.returncode != 0 and capture:
        raise RuntimeError(result.stderr[-800:])

    return result


async def download(url: str, dest: Path, session: aiohttp.ClientSession):
    async with session.get(url) as r:
        r.raise_for_status()

        with open(dest, "wb") as f:
            async for chunk in r.content.iter_chunked(65536):
                f.write(chunk)


def size_mb(path: Path):
    return path.stat().st_size / 1_048_576


# ── streamable upload ───────────────────────────────

async def upload_streamable(path: Path):
    url = "https://api.streamable.com/upload"

    auth = aiohttp.BasicAuth(STREAMABLE_USER, STREAMABLE_PASS)

    async with aiohttp.ClientSession(auth=auth) as session:
        data = aiohttp.FormData()
        data.add_field("file", open(path, "rb"), filename=path.name)

        async with session.post(url, data=data) as r:
            res = await r.json()

    if "shortcode" not in res:
        raise RuntimeError(res)

    return f"https://streamable.com/{res['shortcode']}"


# ── send file or streamable ─────────────────────────

async def send_file(ctx_or_interaction, path: Path, label: str):

    mb = size_mb(path)

    if mb > MAX_UPLOAD_MB:

        try:
            url = await upload_streamable(path)

            msg = (
                f"📦 File too large for Discord (**{mb:.1f} MB**)\n"
                f"🔗 Uploaded to Streamable:\n{url}"
            )

        except Exception as e:
            msg = f"❌ Streamable upload failed:\n{e}"

        if isinstance(ctx_or_interaction, discord.Interaction):
            await ctx_or_interaction.followup.send(msg)
        else:
            await ctx_or_interaction.reply(msg)

        return

    file = discord.File(str(path), filename=path.name)

    if isinstance(ctx_or_interaction, discord.Interaction):
        await ctx_or_interaction.followup.send(label, file=file)
    else:
        await ctx_or_interaction.reply(label, file=file)


# ── trim command ────────────────────────────────────

@bot.command(name="trim")
async def trim_prefix(ctx, start: float, end: float):

    if not ctx.message.attachments:
        return await ctx.reply("❌ Please attach a video.")

    att = ctx.message.attachments[0]

    async with ctx.typing():

        async with aiohttp.ClientSession() as session:
            inp = TEMP / f"in_{ctx.message.id}{Path(att.filename).suffix}"
            out = TEMP / f"trim_{ctx.message.id}.mp4"

            await download(att.url, inp, session)

        dur = end - start

        if dur <= 0:
            return await ctx.reply("❌ End must be greater than start.")

        try:
            ffmpeg(
                "-ss", start,
                "-i", inp,
                "-t", dur,
                "-c:v", "libx264",
                "-c:a", "aac",
                "-movflags", "+faststart",
                out
            )

        except RuntimeError as e:
            return await ctx.reply(f"❌ FFmpeg error:\n```{e}```")

        await send_file(ctx, out, f"✅ Trimmed {start}s → {end}s")

        inp.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


# ── merge command ───────────────────────────────────

@bot.command(name="merge")
async def merge_cmd(ctx):

    atts = ctx.message.attachments

    if len(atts) < 2:
        return await ctx.reply("❌ Attach at least 2 videos.")

    if len(atts) > 5:
        return await ctx.reply("❌ Max 5 videos.")

    async with ctx.typing():

        async with aiohttp.ClientSession() as session:

            paths = []

            for i, att in enumerate(atts):
                p = TEMP / f"merge_{ctx.message.id}_{i}{Path(att.filename).suffix}"

                await download(att.url, p, session)
                paths.append(p)

        reencoded = []

        for i, p in enumerate(paths):

            re = TEMP / f"re_{ctx.message.id}_{i}.mp4"

            try:

                ffmpeg(
                    "-i", p,
                    "-c:v", "libx264",
                    "-c:a", "aac",
                    "-vf",
                    "scale=1280:720:force_original_aspect_ratio=decrease,"
                    "pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                    re
                )

            except RuntimeError as e:
                return await ctx.reply(f"❌ Error clip {i+1}:\n```{e}```")

            reencoded.append(re)

        list_file = TEMP / f"list_{ctx.message.id}.txt"

        list_file.write_text("\n".join(f"file '{p}'" for p in reencoded))

        out = TEMP / f"merged_{ctx.message.id}.mp4"

        try:
            ffmpeg("-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", out)

        except RuntimeError as e:
            return await ctx.reply(f"❌ Merge error:\n```{e}```")

        await send_file(ctx, out, f"✅ Merged {len(atts)} clips")

        for p in paths + reencoded:
            p.unlink(missing_ok=True)

        list_file.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


# ── add music command ───────────────────────────────

@bot.command(name="addmusic")
async def addmusic_cmd(ctx, volume: float = 0.5):

    atts = ctx.message.attachments

    if len(atts) < 2:
        return await ctx.reply("❌ Attach video then audio.")

    async with ctx.typing():

        async with aiohttp.ClientSession() as session:

            vid = TEMP / f"vid_{ctx.message.id}{Path(atts[0].filename).suffix}"
            aud = TEMP / f"aud_{ctx.message.id}{Path(atts[1].filename).suffix}"

            await download(atts[0].url, vid, session)
            await download(atts[1].url, aud, session)

        out = TEMP / f"music_{ctx.message.id}.mp4"

        vol = max(0.0, min(1.0, volume))

        try:

            ffmpeg(
                "-i", vid,
                "-stream_loop", "-1",
                "-i", aud,
                "-filter_complex",
                f"[0:a]volume=0.4[orig];[1:a]volume={vol}[music];"
                f"[orig][music]amix=inputs=2:duration=first[aout]",
                "-map", "0:v",
                "-map", "[aout]",
                "-c:v", "libx264",
                "-c:a", "aac",
                "-shortest",
                out
            )

        except RuntimeError as e:
            return await ctx.reply(f"❌ FFmpeg error:\n```{e}```")

        await send_file(ctx, out, f"🎵 Music added (volume {vol})")

        vid.unlink(missing_ok=True)
        aud.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


# ── help command ────────────────────────────────────

@bot.command(name="videohelp")
async def help_cmd(ctx):

    embed = discord.Embed(
        title="🎬 Video Bot Commands",
        color=0x5865F2
    )

    embed.add_field(
        name="✂️ !trim <start> <end>",
        value="Trim a video.\nExample: `!trim 5 30`",
        inline=False
    )

    embed.add_field(
        name="🔗 !merge",
        value="Merge 2–5 videos.",
        inline=False
    )

    embed.add_field(
        name="🎵 !addmusic [volume]",
        value="Attach video then audio.\nExample: `!addmusic 0.3`",
        inline=False
    )

    embed.set_footer(text="Files over 25MB automatically upload to Streamable")

    await ctx.reply(embed=embed)


# ── startup ─────────────────────────────────────────

@bot.event
async def on_ready():

    await tree.sync()

    print(f"Logged in as {bot.user}")
    print("Commands ready: !trim !merge !addmusic !videohelp")


bot.run(TOKEN)
