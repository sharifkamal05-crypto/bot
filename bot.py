import discord
from discord.ext import commands
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

TEMP = Path(tempfile.gettempdir()) / "video_bot"
TEMP.mkdir(exist_ok=True)

MAX_UPLOAD_MB = 25


# ── helpers ─────────────────────────

def ffmpeg(*args):
    cmd = ["ffmpeg", "-y", *map(str, args)]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(result.stderr[-500:])

    return result


def size_mb(path):
    return path.stat().st_size / 1_048_576


async def download(url, dest):

    async with aiohttp.ClientSession() as session:

        async with session.get(url) as r:
            r.raise_for_status()

            with open(dest, "wb") as f:
                async for chunk in r.content.iter_chunked(65536):
                    f.write(chunk)


async def upload_streamable(path):

    url = "https://api.streamable.com/upload"

    auth = aiohttp.BasicAuth(STREAMABLE_USER, STREAMABLE_PASS)

    async with aiohttp.ClientSession(auth=auth) as session:

        data = aiohttp.FormData()
        data.add_field("file", open(path, "rb"), filename=path.name)

        async with session.post(url, data=data) as r:
            res = await r.json()

    return f"https://streamable.com/{res['shortcode']}"


async def send_result(ctx, file):

    mb = size_mb(file)

    if mb > MAX_UPLOAD_MB:

        url = await upload_streamable(file)

        await ctx.reply(
            f"📦 File too large for Discord ({mb:.1f}MB)\n🔗 {url}"
        )

    else:

        await ctx.reply(file=discord.File(file))


# ── input handler ───────────────────

async def get_input_file(ctx, url=None):

    if ctx.message.attachments:

        att = ctx.message.attachments[0]
        path = TEMP / f"in_{ctx.message.id}{Path(att.filename).suffix}"

        await download(att.url, path)

        return path

    if url:

        path = TEMP / f"url_{ctx.message.id}.mp4"

        await download(url, path)

        return path

    raise RuntimeError("No file or link provided")


# ── trim ────────────────────────────

@bot.command()
async def trim(ctx, start: float, end: float, url: str = None):

    async with ctx.typing():

        inp = await get_input_file(ctx, url)

        out = TEMP / f"trim_{ctx.message.id}.mp4"

        ffmpeg(
            "-ss", start,
            "-i", inp,
            "-t", end - start,
            "-c:v", "libx264",
            "-c:a", "aac",
            out
        )

        await send_result(ctx, out)

        inp.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


# ── merge ───────────────────────────

@bot.command()
async def merge(ctx, *urls):

    paths = []

    async with ctx.typing():

        if ctx.message.attachments:

            for att in ctx.message.attachments:

                p = TEMP / f"m_{ctx.message.id}_{len(paths)}.mp4"
                await download(att.url, p)

                paths.append(p)

        else:

            for url in urls:

                p = TEMP / f"m_{ctx.message.id}_{len(paths)}.mp4"
                await download(url, p)

                paths.append(p)

        re = []

        for i, p in enumerate(paths):

            r = TEMP / f"re_{i}.mp4"

            ffmpeg(
                "-i", p,
                "-c:v", "libx264",
                "-c:a", "aac",
                r
            )

            re.append(r)

        lst = TEMP / "list.txt"
        lst.write_text("\n".join(f"file '{p}'" for p in re))

        out = TEMP / f"merge_{ctx.message.id}.mp4"

        ffmpeg("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out)

        await send_result(ctx, out)

        for p in paths + re:
            p.unlink(missing_ok=True)

        out.unlink(missing_ok=True)


# ── add music ───────────────────────

@bot.command()
async def addmusic(ctx, volume: float = 0.5, video_url: str = None, audio_url: str = None):

    async with ctx.typing():

        if ctx.message.attachments:

            video = ctx.message.attachments[0]
            audio = ctx.message.attachments[1]

            vid = TEMP / "video.mp4"
            aud = TEMP / "audio.mp3"

            await download(video.url, vid)
            await download(audio.url, aud)

        else:

            vid = TEMP / "video.mp4"
            aud = TEMP / "audio.mp3"

            await download(video_url, vid)
            await download(audio_url, aud)

        out = TEMP / f"music_{ctx.message.id}.mp4"

        ffmpeg(
            "-i", vid,
            "-stream_loop", "-1",
            "-i", aud,
            "-filter_complex",
            f"[0:a]volume=0.4[a0];[1:a]volume={volume}[a1];[a0][a1]amix=inputs=2",
            "-map", "0:v",
            "-map", "[a0]",
            "-c:v", "libx264",
            "-shortest",
            out
        )

        await send_result(ctx, out)

        vid.unlink(missing_ok=True)
        aud.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


# ── help ────────────────────────────

@bot.command()
async def videohelp(ctx):

    embed = discord.Embed(
        title="🎬 Video Bot",
        description="Video editing commands",
        color=0x5865F2
    )

    embed.add_field(
        name="✂️ Trim",
        value="`!trim 5 30` + attach video\nor\n`!trim 5 30 <url>`"
    )

    embed.add_field(
        name="🔗 Merge",
        value="Attach 2-5 videos\nor\n`!merge url1 url2`"
    )

    embed.add_field(
        name="🎵 Add Music",
        value="Attach video + audio\nor\n`!addmusic 0.5 video_url audio_url`"
    )

    embed.set_footer(text="Files over 25MB upload to Streamable")

    await ctx.reply(embed=embed)


@bot.event
async def on_ready():

    print(f"✅ Logged in as {bot.user}")
    print("Commands ready: !trim !merge !addmusic !videohelp")


bot.run(TOKEN)
