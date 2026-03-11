import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import tempfile
import aiohttp
import subprocess
from pathlib import Path

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TOKEN = "MTQ4MTM0MjMzMzI5NDIxNTM3Mw.Gn1TEN.u8WYtba_jWf2zrR2-3EE8veGua_Zl8WYot7DmY"   # ← paste your token here
# ───────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

TEMP = Path(tempfile.gettempdir()) / "discord_video_bot"
TEMP.mkdir(exist_ok=True)

MAX_UPLOAD_MB = 25  # Discord free tier limit


# ── helpers ────────────────────────────────────────────────────────────────────

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
            async for chunk in r.content.iter_chunked(1 << 16):
                f.write(chunk)


def size_mb(path: Path) -> float:
    return path.stat().st_size / 1_048_576


async def send_file(ctx_or_interaction, path: Path, label: str):
    mb = size_mb(path)
    if mb > MAX_UPLOAD_MB:
        msg = f"⚠️ Output is **{mb:.1f} MB** — over Discord's {MAX_UPLOAD_MB} MB limit. Try a shorter clip."
        if isinstance(ctx_or_interaction, discord.Interaction):
            await ctx_or_interaction.followup.send(msg)
        else:
            await ctx_or_interaction.reply(msg)
        return

    file = discord.File(str(path), filename=path.name)
    if isinstance(ctx_or_interaction, discord.Interaction):
        await ctx_or_interaction.followup.send(f"✅ {label}", file=file)
    else:
        await ctx_or_interaction.reply(f"✅ {label}", file=file)


# ── slash commands ─────────────────────────────────────────────────────────────

@tree.command(name="trim", description="Trim a video clip. Attach the video and specify start/end in seconds.")
@app_commands.describe(
    start="Start time in seconds (e.g. 5)",
    end="End time in seconds (e.g. 30)",
)
async def trim_cmd(interaction: discord.Interaction, start: float, end: float):
    await interaction.response.defer(thinking=True)

    attachments = interaction.message.attachments if interaction.message else []
    # Slash commands don't carry attachments; user must run !trim instead
    await interaction.followup.send(
        "⚠️ Slash commands can't receive attachments directly.\n"
        "Please use: `!trim <start> <end>` and **attach your video** to that message."
    )


@bot.command(name="trim", help="Trim a video. Usage: !trim <start_sec> <end_sec>  (attach video)")
async def trim_prefix(ctx, start: float, end: float):
    if not ctx.message.attachments:
        return await ctx.reply("❌ Please attach a video file.")

    att = ctx.message.attachments[0]
    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            inp = TEMP / f"in_{ctx.message.id}{Path(att.filename).suffix}"
            out = TEMP / f"trim_{ctx.message.id}.mp4"
            await download(att.url, inp, session)

        dur = end - start
        if dur <= 0:
            return await ctx.reply("❌ End time must be greater than start time.")

        try:
            ffmpeg("-ss", start, "-i", inp, "-t", dur,
                   "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", out)
        except RuntimeError as e:
            return await ctx.reply(f"❌ FFmpeg error:\n```{e}```")

        await send_file(ctx, out, f"Trimmed {start}s → {end}s")
        inp.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


@bot.command(name="merge", help="Merge 2–5 videos. Attach all videos in one message.")
async def merge_cmd(ctx):
    atts = ctx.message.attachments
    if len(atts) < 2:
        return await ctx.reply("❌ Please attach **at least 2** video files.")
    if len(atts) > 5:
        return await ctx.reply("❌ Max 5 videos at once.")

    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            paths = []
            for i, att in enumerate(atts):
                p = TEMP / f"merge_{ctx.message.id}_{i}{Path(att.filename).suffix}"
                await download(att.url, p, session)
                paths.append(p)

        # Re-encode all to same format then concat
        reencoded = []
        for i, p in enumerate(paths):
            re = TEMP / f"re_{ctx.message.id}_{i}.mp4"
            try:
                ffmpeg("-i", p, "-c:v", "libx264", "-c:a", "aac",
                       "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                       re)
            except RuntimeError as e:
                return await ctx.reply(f"❌ Error processing clip {i+1}:\n```{e}```")
            reencoded.append(re)

        list_file = TEMP / f"list_{ctx.message.id}.txt"
        list_file.write_text("\n".join(f"file '{p}'" for p in reencoded))

        out = TEMP / f"merged_{ctx.message.id}.mp4"
        try:
            ffmpeg("-f", "concat", "-safe", "0", "-i", list_file,
                   "-c", "copy", out)
        except RuntimeError as e:
            return await ctx.reply(f"❌ Merge error:\n```{e}```")

        await send_file(ctx, out, f"Merged {len(atts)} clips!")

        for p in paths + reencoded:
            p.unlink(missing_ok=True)
        list_file.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


@bot.command(name="addmusic", help="Add music to a video. First attachment = video, second = audio.")
async def addmusic_cmd(ctx, volume: float = 0.5):
    atts = ctx.message.attachments
    if len(atts) < 2:
        return await ctx.reply(
            "❌ Attach **2 files**: first the video, then the audio track.\n"
            f"Optional: specify volume 0.0–1.0 (default 0.5). E.g. `!addmusic 0.3`"
        )

    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            vid_path = TEMP / f"vid_{ctx.message.id}{Path(atts[0].filename).suffix}"
            aud_path = TEMP / f"aud_{ctx.message.id}{Path(atts[1].filename).suffix}"
            await download(atts[0].url, vid_path, session)
            await download(atts[1].url, aud_path, session)

        out = TEMP / f"music_{ctx.message.id}.mp4"
        vol = max(0.0, min(1.0, volume))

        try:
            # Mix original audio (quieter) with new music, loop music if shorter than video
            ffmpeg(
                "-i", vid_path,
                "-stream_loop", "-1", "-i", aud_path,
                "-filter_complex",
                f"[0:a]volume=0.4[orig];[1:a]volume={vol}[music];[orig][music]amix=inputs=2:duration=first[aout]",
                "-map", "0:v",
                "-map", "[aout]",
                "-c:v", "libx264",
                "-c:a", "aac",
                "-shortest",
                "-movflags", "+faststart",
                out
            )
        except RuntimeError as e:
            return await ctx.reply(f"❌ FFmpeg error:\n```{e}```")

        await send_file(ctx, out, f"Music added (volume: {vol})")
        vid_path.unlink(missing_ok=True)
        aud_path.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


@bot.command(name="videohelp", help="Show all commands.")
async def help_cmd(ctx):
    embed = discord.Embed(title="🎬 Video Bot Commands", color=0x5865F2)
    embed.add_field(
        name="✂️ `!trim <start> <end>`",
        value="Trim a video. Attach video + give start/end in **seconds**.\n`!trim 5 30`",
        inline=False
    )
    embed.add_field(
        name="🔗 `!merge`",
        value="Merge 2–5 videos into one. Attach all clips in **one message**.",
        inline=False
    )
    embed.add_field(
        name="🎵 `!addmusic [volume]`",
        value="Add music to a video. Attach **video first, then audio**.\nOptional volume 0.0–1.0 (default 0.5).\n`!addmusic 0.3`",
        inline=False
    )
    embed.set_footer(text="Max upload size: 25 MB per file (Discord limit)")
    await ctx.reply(embed=embed)


# ── startup ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    print("Commands ready: !trim  !merge  !addmusic  !videohelp")


bot.run(TOKEN)
