import asyncio
import os
import json
import discord
from discord.ext import commands

from db import (
    init_db,
    get_discord_id_from_tiktok,
    get_db_connection,
    get_daily_top_supporters,
    save_gift_data,
    save_daily_gift_data,
    get_setting,
    set_setting,
    is_tiktok_user_in_db,
    is_user_already_claimed,
    get_user_mapping,
    bind_user_mapping,
    create_pending_verification,
    get_pending_verification,
    find_pending_live_verification,
    cancel_pending_verification,
    log_activity_event
)
from utils import get_schedule_info, load_role_config, load_summary_config
from server import keep_alive
from observer import run_all_observers, get_current_live_hosts, is_any_host_live
from verification import async_verify_tiktok_bio

DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
ANNOUNCEMENT_CHANNEL_ID = int(os.environ.get('ANNOUNCEMENT_CHANNEL_ID', "0"))

NOTIFICATION_LOCK = asyncio.Lock()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

def get_verified_role(guild: discord.Guild) -> discord.Role:
    """Mengambil object Role verifikasi dari setting, env, roles.json, atau nama role."""
    if not guild:
        return None
    # 1. Cek dari bot_settings di database
    role_id_str = get_setting("verified_role_id")
    if role_id_str and role_id_str.isdigit():
        role = guild.get_role(int(role_id_str))
        if role:
            return role
    # 2. Cek dari environment variable
    env_id = os.environ.get("VERIFIED_ROLE_ID")
    if env_id and env_id.isdigit():
        role = guild.get_role(int(env_id))
        if role:
            return role
    # 3. Cek dari config/roles.json
    try:
        if os.path.exists("config/roles.json"):
            with open("config/roles.json", "r") as f:
                data = json.load(f)
                val = data.get("verified_role_id")
                if val:
                    role = guild.get_role(int(val))
                    if role:
                        return role
    except Exception:
        pass
    # 4. Cari otomatis berdasarkan nama role standar
    for r in guild.roles:
        if r.name.lower() in ["verified", "terverifikasi", "member"]:
            return r
    return None

async def grant_verified_access(member: discord.Member, tiktok_user: str, total_coins: int) -> tuple:
    """Memberikan role verifikasi dan role coin tier yang sesuai kepada member Discord."""
    roles_added_names = []
    verified_role = get_verified_role(member.guild)
    if verified_role and verified_role not in member.roles:
        try:
            await member.add_roles(verified_role)
            roles_added_names.append(verified_role.name)
        except Exception as e:
            print(f"[Role Error] Failed to add verified role: {e}")

    # Berikan juga tier roles sesuai total koin yang ada
    await auto_assign_role(tiktok_user, total_coins)
    
    role_info = f" Role **{', '.join(roles_added_names)}** telah diberikan." if roles_added_names else " Role telah diaktifkan."
    return True, role_info

def handle_api_announcement(username: str, is_live: bool, host_name: str = None):
    try:
        loop = bot.loop if bot.is_ready() else asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(
            process_live_announcement(username, is_live, host_name),
            loop
        )
    except Exception as e:
        print(f"[API Error] Failed to schedule announcement for @{username}: {e}")

async def auto_assign_role(tiktok_user: str, total_coins: int):
    discord_id = get_discord_id_from_tiktok(tiktok_user)
    if not discord_id:
        return

    role_tiers, stack_roles = load_role_config()
    if not role_tiers:
        return

    for guild_id, tiers in role_tiers.items():
        try:
            guild = bot.get_guild(int(guild_id))
        except Exception:
            guild = None
        if not guild:
            continue

        try:
            member = await guild.fetch_member(discord_id)
        except discord.NotFound:
            continue

        roles_to_add = []
        roles_to_remove = []

        if stack_roles:
            for item in tiers:
                min_coins = item.get("min_coins") if isinstance(item, dict) else item[0]
                role_id = item.get("role_id") if isinstance(item, dict) else item[1]
                role = guild.get_role(role_id)
                if role and total_coins >= min_coins and role not in member.roles:
                    roles_to_add.append(role)
        else:
            highest_eligible_role = None
            all_tier_roles = []

            for item in tiers:
                min_coins = item.get("min_coins") if isinstance(item, dict) else item[0]
                role_id = item.get("role_id") if isinstance(item, dict) else item[1]
                role = guild.get_role(role_id)
                if role:
                    all_tier_roles.append(role)
                    if total_coins >= min_coins:
                        highest_eligible_role = role

            if highest_eligible_role:
                if highest_eligible_role not in member.roles:
                    roles_to_add.append(highest_eligible_role)

                for role in all_tier_roles:
                    if role != highest_eligible_role and role in member.roles:
                        roles_to_remove.append(role)

        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add)
                role_names = ", ".join([r.name for r in roles_to_add])
                print(f"[Discord] Assigned role {role_names} to {member.display_name} in '{guild.name}'!")
            except Exception as e:
                print(f"[Discord] Error assigning tier roles: {e}")

        if roles_to_remove:
            try:
                await member.remove_roles(*roles_to_remove)
                role_names = ", ".join([r.name for r in roles_to_remove])
                print(f"[Discord] Removed previous tier role ({role_names}) from {member.display_name} in '{guild.name}'.")
            except Exception as e:
                print(f"[Discord] Error removing previous tier roles: {e}")

async def process_live_announcement(username: str, is_live: bool, host_name: str = None):
    user_key = username.lower()
    async with NOTIFICATION_LOCK:
        if not ANNOUNCEMENT_CHANNEL_ID:
            print("[Discord] Notification failed: ANNOUNCEMENT_CHANNEL_ID is not set.")
            return

        await bot.wait_until_ready()
        channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
        if not channel:
            try:
                channel = await bot.fetch_channel(ANNOUNCEMENT_CHANNEL_ID)
            except Exception as e:
                print(f"[Discord] Notification failed: Channel ID {ANNOUNCEMENT_CHANNEL_ID} not found ({e}).")
                return

        sched_host, account_name = get_schedule_info(user_key)
        final_host_name = host_name or sched_host or account_name or "Host"
        live_url = f"https://www.tiktok.com/@{user_key}/live"
        live_content = (
            f"🔴 **{final_host_name} just live!**\n"
            f"Currently streaming on **{account_name}** (@{user_key})\n\n"
            f"Watch here:\n{live_url}"
        )

        conn = get_db_connection()
        try:
            res = conn.execute(
                "SELECT channel_id, message_id FROM active_live_announcements WHERE tiktok_username = ?",
                (user_key,)
            )
            row = res.fetchone()

            old_msg = None
            if row:
                old_message_id = int(row[1])
                try:
                    old_msg = await channel.fetch_message(old_message_id)
                except Exception:
                    old_msg = None

            if is_live:
                # Batasi hanya mengirim terbatas 1 kali per akun track ke discord. Jika update, edit postingan
                if old_msg:
                    if old_msg.content != live_content:
                        try:
                            await old_msg.edit(content=live_content)
                            print(f"[Discord] Edited/updated live announcement for @{user_key} (Host: {final_host_name})")
                        except Exception as e:
                            print(f"[Discord] Failed to edit live announcement for @{user_key}: {e}")
                    # Jangan pernah kirim pesan baru berulang kali jika pesan lama sudah ada
                    return

                new_msg = await channel.send(live_content)
                conn.execute(
                    "INSERT OR REPLACE INTO active_live_announcements (tiktok_username, channel_id, message_id) VALUES (?, ?, ?)",
                    (user_key, str(channel.id), str(new_msg.id))
                )
                conn.commit()
                print(f"[Discord] Sent 1-time live announcement for @{user_key} (Msg ID: {new_msg.id})")
            else:
                # "jika offline maka hapus pesan, jadi bot tidak mengirim pesan baru terus menerus"
                if old_msg:
                    try:
                        await old_msg.delete()
                        print(f"[Discord] Deleted live announcement message for @{user_key} because stream is offline.")
                    except Exception as e:
                        print(f"[Discord] Failed to delete message for @{user_key}: {e}")

                conn.execute("DELETE FROM active_live_announcements WHERE tiktok_username = ?", (user_key,))
                conn.commit()

        except Exception as e:
            print(f"[Discord Error] Failed to process announcement for @{user_key}: {e}")
        finally:
            conn.close()

# --- Discord UI Components (Modals & Views) ---

class VerificationChannelView(discord.ui.View):
    """View permanen dengan tombol untuk verifikasi akun TikTok di channel verifikasi."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verifikasi Akun TikTok", 
        style=discord.ButtonStyle.primary, 
        emoji="🔑", 
        custom_id="btn_tiktok_verify_persistent"
    )
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TikTokUsernameModal())

class TikTokUsernameModal(discord.ui.Modal, title="Verifikasi Akun TikTok"):
    username_input = discord.ui.TextInput(
        label="Username TikTok Anda",
        placeholder="Contoh: clarissadreams (tanpa tanda @)",
        min_length=2,
        max_length=32,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        u_clean = self.username_input.value.strip().lstrip('@').lower()
        
        # 1. Cek apakah user Discord ini sudah memiliki akun TikTok yang terikat
        existing_tiktok = get_user_mapping(interaction.user.id)
        if existing_tiktok:
            await interaction.response.send_message(
                f"ℹ️ Akun Discord Anda sudah terverifikasi dengan akun TikTok **@{existing_tiktok}**.\n"
                f"Anda tidak perlu melakukan verifikasi ulang.",
                ephemeral=True
            )
            return

        # 2. Cek apakah username TikTok ini sudah di-claim orang lain
        is_claimed, claimed_discord_id = is_user_already_claimed(u_clean)
        if is_claimed:
            await interaction.response.send_message(
                f"❌ Akun TikTok **@{u_clean}** sudah terikat ke akun Discord lain.\n"
                f"Satu akun TikTok hanya dapat diklaim oleh satu akun Discord. Jika ini milik Anda, silakan hubungi admin.",
                ephemeral=True
            )
            return

        # 3. Cek apakah username TikTok ini ada di database bot
        exists, total_coins, display_name = is_tiktok_user_in_db(u_clean)
        if not exists:
            await interaction.response.send_message(
                f"❌ Akun TikTok **@{u_clean}** **tidak ditemukan di database kami**.\n\n"
                f"⚠️ **Syarat Verifikasi:**\n"
                f"1. Pastikan penulisan username TikTok Anda benar (tanpa spasi/typo).\n"
                f"2. Anda harus sudah pernah berinteraksi atau mengirim gift di live stream host kami.\n\n"
                f"Jika baru saja mengirim gift, mohon tunggu sebentar agar observer selesai mencatatnya.",
                ephemeral=True
            )
            return

        # 4. Buat kode verifikasi acak unik (DM-XXXX)
        code = create_pending_verification(interaction.user.id, u_clean, method='choice')
        
        embed = discord.Embed(
            title="✅ Akun TikTok Ditemukan!",
            description=(
                f"Akun TikTok **@{u_clean}** (`{display_name or u_clean}`) terdaftar di database kami!\n"
                f"💰 **Total Koin Tercatat:** `{total_coins} c`\n\n"
                f"🔒 **Pilih Metode Verifikasi:**\n"
                f"Untuk membuktikan bahwa Anda adalah pemilik sah akun ini, silakan pilih salah satu dari **2 metode** berikut:"
            ),
            color=discord.Color.blue()
        )
        embed.add_field(
            name="📝 Pilihan 1: Verifikasi via Bio Profil",
            value="Tambahkan kode verifikasi ke Bio profil TikTok Anda, lalu klik tombol konfirmasi. *(Bisa dilakukan kapan saja tanpa perlu live)*",
            inline=False
        )
        embed.add_field(
            name="🔴 Pilihan 2: Verifikasi via Live Chat",
            value="Ketik kode verifikasi di Live Chat saat salah satu host kami sedang Live Streaming. Bot akan mendeteksi komentar Anda secara otomatis.",
            inline=False
        )

        view = VerificationMethodChoiceView(u_clean, code, total_coins)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class VerificationMethodChoiceView(discord.ui.View):
    def __init__(self, tiktok_user: str, code: str, total_coins: int):
        super().__init__(timeout=900)
        self.tiktok_user = tiktok_user
        self.code = code
        self.total_coins = total_coins

    @discord.ui.button(label="Verifikasi Lewat Bio", style=discord.ButtonStyle.primary, emoji="📝")
    async def via_bio(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="📝 Verifikasi Akun via Bio TikTok",
            description=(
                f"Akun TikTok: **@{self.tiktok_user}**\n\n"
                f"🔑 **Kode Verifikasi Anda:**\n"
                f"```text\n{self.code}\n```\n"
                f"**Langkah-langkah:**\n"
                f"1. Buka aplikasi TikTok Anda.\n"
                f"2. Buka **Profil** -> **Edit Profil** -> Bagian **Bio**.\n"
                f"3. Masukkan / tambahkan kode `{self.code}` ke dalam Bio profil Anda.\n"
                f"4. Simpan perubahan profil di TikTok.\n"
                f"5. Klik tombol **[ 🔍 Konfirmasi & Cek Bio ]** di bawah ini.\n\n"
                f"⏱️ *Kode berlaku selama 15 menit. Anda dapat menghapus kode ini dari bio setelah verifikasi selesai.*"
            ),
            color=discord.Color.gold()
        )
        view = BioVerificationView(self.tiktok_user, self.code, self.total_coins)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Verifikasi Lewat Live Chat", style=discord.ButtonStyle.danger, emoji="🔴")
    async def via_live(self, interaction: discord.Interaction, button: discord.ui.Button):
        live_hosts = get_current_live_hosts()
        if live_hosts:
            host_text = "🟢 **Host yang SEDANG LIVE saat ini:**\n" + "\n".join([f"• `@{h}` (https://www.tiktok.com/@{h}/live)" for h in live_hosts])
        else:
            host_text = "⚪ **Saat ini belum ada host yang sedang live.**\n💡 *Disarankan menggunakan metode Verifikasi Bio agar dapat langsung selesai tanpa menunggu live.*"

        embed = discord.Embed(
            title="🔴 Verifikasi Akun via Live Chat TikTok",
            description=(
                f"Akun TikTok: **@{self.tiktok_user}**\n\n"
                f"🔑 **Kode Verifikasi Anda:**\n"
                f"```text\n{self.code}\n```\n"
                f"{host_text}\n\n"
                f"**Langkah-langkah:**\n"
                f"1. Masuk ke live streaming host kami yang sedang live.\n"
                f"2. Kirim komentar di Live Chat yang memuat kode: `{self.code}` menggunakan akun TikTok **@{self.tiktok_user}** Anda.\n"
                f"3. Bot akan otomatis mendeteksi komentar tersebut dan langsung mengaktifkan role Anda!\n\n"
                f"⏱️ *Bot sedang mendengarkan live chat... (Kode berlaku 15 menit)*"
            ),
            color=discord.Color.red()
        )
        view = LiveVerificationView(self.tiktok_user, self.code, self.total_coins)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Batal", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        cancel_pending_verification(interaction.user.id)
        await interaction.response.edit_message(content="❌ Proses verifikasi dibatalkan.", embed=None, view=None)

class BioVerificationView(discord.ui.View):
    def __init__(self, tiktok_user: str, code: str, total_coins: int):
        super().__init__(timeout=900)
        self.tiktok_user = tiktok_user
        self.code = code
        self.total_coins = total_coins

    @discord.ui.button(label="Konfirmasi & Cek Bio", style=discord.ButtonStyle.success, emoji="🔍")
    async def check_bio(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        is_valid, current_bio = await async_verify_tiktok_bio(self.tiktok_user, self.code)
        
        if is_valid:
            bind_user_mapping(interaction.user.id, self.tiktok_user)
            log_activity_event("verification", f"[VERIFIKASI BIO] @{self.tiktok_user} berhasil verifikasi akun via Bio!", self.tiktok_user)
            
            if isinstance(interaction.user, discord.Member):
                _, role_info = await grant_verified_access(interaction.user, self.tiktok_user, self.total_coins)
            else:
                role_info = " Akses server telah dibuka."

            embed = discord.Embed(
                title="🎉 Verifikasi Berhasil!",
                description=(
                    f"Selamat {interaction.user.mention}!\n\n"
                    f"✅ Akun TikTok **@{self.tiktok_user}** berhasil di-binding ke akun Discord Anda.\n"
                    f"🔒 Akun telah terkunci dan tidak dapat di-claim oleh orang lain.\n"
                    f"🌟{role_info}\n\n"
                    f"*Anda sekarang dapat menghapus kode `{self.code}` dari bio TikTok Anda. Selamat bersenang-senang!*"
                ),
                color=discord.Color.green()
            )
            await interaction.edit_original_response(embed=embed, view=None)
        else:
            embed = discord.Embed(
                title="⚠️ Kode Belum Ditemukan di Bio",
                description=(
                    f"Kode **`{self.code}`** belum terdeteksi di bio profil TikTok **@{self.tiktok_user}**.\n\n"
                    f"📄 **Bio yang terbaca saat ini:**\n"
                    f"> *\"{current_bio or '(Bio Kosong)'}\"*\n\n"
                    f"💡 **Petunjuk:**\n"
                    f"1. Pastikan Anda sudah menekan tombol **Simpan** di profil TikTok.\n"
                    f"2. Tunggu 5-10 detik jika baru saja disimpan agar TikTok meng-update datanya.\n"
                    f"3. Pastikan kode `{self.code}` tertulis lengkap di dalam bio."
                ),
                color=discord.Color.orange()
            )
            await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Ganti Metode", style=discord.ButtonStyle.secondary, emoji="🔙")
    async def change_method(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = VerificationMethodChoiceView(self.tiktok_user, self.code, self.total_coins)
        embed = discord.Embed(
            title="🔒 Pilih Metode Verifikasi",
            description=f"Pilih metode verifikasi untuk akun TikTok **@{self.tiktok_user}**:",
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Batal", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        cancel_pending_verification(interaction.user.id)
        await interaction.response.edit_message(content="❌ Proses verifikasi dibatalkan.", embed=None, view=None)

class LiveVerificationView(discord.ui.View):
    def __init__(self, tiktok_user: str, code: str, total_coins: int):
        super().__init__(timeout=900)
        self.tiktok_user = tiktok_user
        self.code = code
        self.total_coins = total_coins

    @discord.ui.button(label="Cek Status Verifikasi", style=discord.ButtonStyle.primary, emoji="🔄")
    async def check_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_mapped = get_user_mapping(interaction.user.id)
        if current_mapped and current_mapped.lower() == self.tiktok_user.lower():
            if isinstance(interaction.user, discord.Member):
                _, role_info = await grant_verified_access(interaction.user, self.tiktok_user, self.total_coins)
            else:
                role_info = " Akses server telah dibuka."

            embed = discord.Embed(
                title="🎉 Verifikasi Live Berhasil!",
                description=(
                    f"Selamat {interaction.user.mention}!\n\n"
                    f"✅ Komentar Anda telah terdeteksi di Live Chat!\n"
                    f"Akun TikTok **@{self.tiktok_user}** telah di-binding ke akun Discord Anda.\n"
                    f"🌟{role_info}"
                ),
                color=discord.Color.green()
            )
            await interaction.response.edit_message(embed=embed, view=None)
            return

        live_hosts = get_current_live_hosts()
        if live_hosts:
            host_text = "🟢 **Host yang SEDANG LIVE saat ini:**\n" + "\n".join([f"• `@{h}`" for h in live_hosts])
        else:
            host_text = "⚪ **Saat ini belum ada host yang sedang live.**"

        embed = discord.Embed(
            title="⏳ Menunggu Komentar di Live Chat",
            description=(
                f"Akun TikTok: **@{self.tiktok_user}**\n"
                f"Kode: **`{self.code}`**\n\n"
                f"{host_text}\n\n"
                f"Kirim komentar yang memuat kode **`{self.code}`** di live chat host kami.\n"
                f"Begitu komentar Anda masuk, verifikasi akan otomatis selesai!"
            ),
            color=discord.Color.yellow()
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Ganti ke Verifikasi Bio", style=discord.ButtonStyle.secondary, emoji="📝")
    async def switch_to_bio(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="📝 Verifikasi Akun via Bio TikTok",
            description=(
                f"Akun TikTok: **@{self.tiktok_user}**\n\n"
                f"🔑 **Kode Verifikasi Anda:**\n"
                f"```text\n{self.code}\n```\n"
                f"**Langkah-langkah:**\n"
                f"1. Buka aplikasi TikTok -> **Profil** -> **Edit Profil** -> **Bio**.\n"
                f"2. Masukkan kode `{self.code}` ke dalam Bio Anda dan simpan.\n"
                f"3. Klik tombol **[ 🔍 Konfirmasi & Cek Bio ]** di bawah ini."
            ),
            color=discord.Color.gold()
        )
        view = BioVerificationView(self.tiktok_user, self.code, self.total_coins)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Batal", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        cancel_pending_verification(interaction.user.id)
        await interaction.response.edit_message(content="❌ Proses verifikasi dibatalkan.", embed=None, view=None)

async def handle_live_comment_verification(tiktok_user: str, comment_text: str, target_host: str):
    """
    Callback yang dipanggil ketika ada komentar masuk di live stream TikTok.
    Mengecek apakah komentar tersebut cocok dengan verifikasi live pending.
    """
    pending = find_pending_live_verification(tiktok_user, comment_text)
    if not pending:
        return

    discord_id = pending['discord_id']
    u_clean = tiktok_user.lower()

    # Binding akun ke user_mappings
    bind_user_mapping(discord_id, u_clean)
    log_activity_event("verification", f"[VERIFIKASI LIVE] @{u_clean} berhasil verifikasi via Live Chat @{target_host}!", target_host)
    print(f"[Verification] Live comment verified for @{u_clean} (Discord ID: {discord_id})")

    # Ambil total koin
    _, total_coins, _ = is_tiktok_user_in_db(u_clean)

    # Berikan role verifikasi di setiap guild tempat member berada
    for guild in bot.guilds:
        member = guild.get_member(discord_id)
        if member:
            try:
                await grant_verified_access(member, u_clean, total_coins)
                print(f"[Verification] Granted verified role to {member.display_name} in {guild.name}")
            except Exception as e:
                print(f"[Verification] Error granting role in {guild.name}: {e}")

    # Kirim pesan notifikasi langsung ke DM pengguna jika DM terbuka
    try:
        user_obj = await bot.fetch_user(discord_id)
        if user_obj:
            embed = discord.Embed(
                title="🎉 Verifikasi Live TikTok Berhasil!",
                description=(
                    f"Halo {user_obj.name}!\n\n"
                    f"Komentar Anda di Live Chat host **@{target_host}** telah terdeteksi!\n"
                    f"Akun TikTok **@{u_clean}** telah berhasil di-binding ke akun Discord Anda.\n"
                    f"✅ Role verifikasi telah diberikan dan akses server telah dibuka!"
                ),
                color=discord.Color.green()
            )
            await user_obj.send(embed=embed)
    except Exception:
        pass

# --- Discord Events & Commands ---

@bot.event
async def on_ready():
    print(f"[Discord] Bot logged in as {bot.user}")
    # Daftarkan persistent view agar tombol verifikasi selalu aktif
    bot.add_view(VerificationChannelView())

@bot.event
async def on_member_join(member: discord.Member):
    # Jika member yang bergabung kembali sudah pernah verifikasi sebelumnya, pulihkan role-nya
    tt_user = get_user_mapping(member.id)
    if tt_user:
        _, total_coins, _ = is_tiktok_user_in_db(tt_user)
        await grant_verified_access(member, tt_user, total_coins)

@bot.command(name="setup_verification")
@commands.has_permissions(administrator=True)
async def setup_verification(ctx):
    """Admin command untuk mengirim panel verifikasi akun TikTok ke channel."""
    embed = discord.Embed(
        title="🛡️ VERIFIKASI AKUN TIKTOK",
        description=(
            "Selamat datang di server!\n\n"
            "Untuk mendapatkan role dan membuka akses ke seluruh channel server, "
            "Anda diwajibkan untuk **mengklaim & memverifikasi akun TikTok** Anda terlebih dahulu.\n\n"
            "📌 **Cara Verifikasi:**\n"
            "1. Klik tombol **[ 🔑 Verifikasi Akun TikTok ]** di bawah ini.\n"
            "2. Masukkan username TikTok Anda pada formulir yang muncul.\n"
            "3. Percakapan bersifat **100% Privat (hanya Anda dan bot yang bisa melihat)**.\n"
            "4. Pilih metode verifikasi kepemilikan akun (**Lewat Bio** atau **Lewat Live Chat**).\n"
            "5. Setelah sesuai, akun TikTok akan terikat secara permanen dan role akses server otomatis aktif!"
        ),
        color=discord.Color.from_rgb(0, 168, 255)
    )
    embed.set_footer(text="Dummy Dream Bot • Sistem Verifikasi & Keamanan")
    view = VerificationChannelView()
    await ctx.send(embed=embed, view=view)
    set_setting("verification_channel_id", str(ctx.channel.id))
    try:
        await ctx.message.delete()
    except Exception:
        pass

@bot.command(name="set_verified_role")
@commands.has_permissions(administrator=True)
async def set_verified_role_cmd(ctx, role: discord.Role):
    """Admin command untuk menentukan role yang diberikan setelah verifikasi berhasil."""
    set_setting("verified_role_id", str(role.id))
    await ctx.send(f"✅ Role verifikasi berhasil disetel ke: **{role.name}** (`{role.id}`)")

@bot.command(name="claim", aliases=["verify"])
async def claim(ctx, tiktok_user: str = None):
    """Memulai proses claim / verifikasi akun TikTok."""
    if not tiktok_user:
        view = VerificationChannelView()
        embed = discord.Embed(
            title="🔑 Verifikasi Akun TikTok",
            description="Klik tombol di bawah ini untuk memulai proses verifikasi akun TikTok Anda secara privat.",
            color=discord.Color.blue()
        )
        await ctx.send(embed=embed, view=view)
        return

    u_clean = tiktok_user.strip().lstrip('@').lower()
    existing_tiktok = get_user_mapping(ctx.author.id)
    if existing_tiktok:
        await ctx.send(f"ℹ️ Akun Discord Anda sudah terverifikasi dengan akun TikTok **@{existing_tiktok}**.")
        return

    is_claimed, claimed_discord_id = is_user_already_claimed(u_clean)
    if is_claimed:
        await ctx.send(f"❌ Akun TikTok **@{u_clean}** sudah terikat ke akun Discord lain.")
        return

    exists, total_coins, display_name = is_tiktok_user_in_db(u_clean)
    if not exists:
        await ctx.send(
            f"❌ Akun TikTok **@{u_clean}** **tidak ditemukan di database kami**.\n\n"
            f"⚠️ **Syarat Verifikasi:**\n"
            f"1. Pastikan username sudah benar.\n"
            f"2. Anda harus sudah pernah mengirim gift / berinteraksi di live stream host kami."
        )
        return

    code = create_pending_verification(ctx.author.id, u_clean, method='choice')
    view = VerificationMethodChoiceView(u_clean, code, total_coins)
    embed = discord.Embed(
        title="✅ Akun TikTok Ditemukan!",
        description=(
            f"Akun TikTok **@{u_clean}** (`{display_name or u_clean}`) terdaftar di database!\n"
            f"💰 Total Koin: `{total_coins} c`\n\n"
            f"Silakan pilih metode verifikasi di bawah ini:"
        ),
        color=discord.Color.blue()
    )
    try:
        await ctx.author.send(embed=embed, view=view)
        await ctx.send(f"📬 {ctx.author.mention}, petunjuk verifikasi telah dikirimkan ke Direct Message (DM) Anda!", delete_after=10)
    except discord.Forbidden:
        await ctx.send(f"⚠️ {ctx.author.mention}, DM Anda tertutup. Silakan gunakan tombol di channel verifikasi untuk interaksi privat.", delete_after=15)

@bot.command(name="unclaim")
@commands.has_permissions(administrator=True)
async def unclaim(ctx, member: discord.Member):
    """Admin command untuk menghapus ikatan akun TikTok dari member Discord."""
    conn = get_db_connection()
    try:
        res = conn.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (member.id,)).fetchone()
        if not res:
            await ctx.send(f"❌ {member.mention} belum memiliki akun TikTok yang terikat.")
            return
        tt_user = res[0]
        conn.execute("DELETE FROM user_mappings WHERE discord_id = ?", (member.id,))
        conn.commit()
        await ctx.send(f"✅ Berhasil membatalkan ikatan akun TikTok `@{tt_user}` dari {member.mention}.")
    finally:
        conn.close()

@bot.command(name="mycoins", aliases=["check", "coins"])
async def mycoins(ctx):
    conn = get_db_connection()
    try:
        res = conn.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (ctx.author.id,))
        mapping = res.fetchone()
        if not mapping:
            await ctx.send(f"❌ {ctx.author.mention}, kamu belum memverifikasi akun TikTok! Gunakan `!claim` atau klik tombol di channel verifikasi.")
            return

        tiktok_user = mapping[0]
        res = conn.execute("SELECT display_name, total_coins FROM tiktok_gifts WHERE tiktok_user = ?", (tiktok_user,))
        gift_data = res.fetchone()
        display_name = gift_data[0] if (gift_data and gift_data[0]) else tiktok_user
        total_coins = gift_data[1] if gift_data else 0

        await ctx.send(f"📊 **Coin Balance for {ctx.author.display_name}:** @{tiktok_user} | **{total_coins} c**")
    finally:
        conn.close()

@bot.command(name="myhistory")
async def myhistory(ctx):
    conn = get_db_connection()
    try:
        res = conn.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (ctx.author.id,))
        mapping = res.fetchone()
        if not mapping:
            await ctx.send(f"❌ {ctx.author.mention}, kamu belum link akun! Gunakan `!claim` atau tombol di channel verifikasi.")
            return

        tiktok_user = mapping[0]
        res = conn.execute("""
            SELECT target_username, gift_name, gift_count, coins, created_at 
            FROM gift_logs 
            WHERE viewer_username = ? 
            ORDER BY created_at DESC LIMIT 5
        """, (tiktok_user,))
        rows = res.fetchall()

        if not rows:
            await ctx.send("📜 Belum ada riwayat gift yang tercatat.")
            return

        msg = f"📜 **Riwayat Gift Terbaru untuk @{tiktok_user}:**\n"
        for host, g_name, g_count, coins, dt in rows:
            msg += f"• `{dt}` - **{g_count}x {g_name}** ({coins}c) ke host **@{host}**\n"

        await ctx.send(msg)
    finally:
        conn.close()

@bot.command(name="topsupporter", aliases=["leaderboard", "koin", "topdonator"])
async def topsupporter(ctx):
    conn = get_db_connection()
    try:
        res = conn.execute("SELECT tiktok_user, display_name, total_coins FROM tiktok_gifts ORDER BY total_coins DESC LIMIT 10")
        rows = res.fetchall()
        if not rows:
            await ctx.send("❌ No gift data recorded yet.")
            return

        table = "```text\n"
        table += f"{'NO':<4} | {'NAME / USERNAME':<24} | {'TOTAL COINS':<10}\n"
        table += "-" * 46 + "\n"
        for idx, (username, display_name, coins) in enumerate(rows, start=1):
            name_str = display_name if display_name else f"@{username}"
            if len(name_str) > 23:
                name_str = name_str[:20] + "..."
            table += f"{idx:<4} | {name_str:<24} | {coins:<10} c\n"
        table += "```"
        await ctx.send(f"🏆 **TIKTOK LIVE TOP SUPPORTERS LEADERBOARD**\n{table}")
    finally:
        conn.close()

@bot.command(name="dailyrank", aliases=["daily", "harini"])
async def dailyrank(ctx):
    rows = get_daily_top_supporters()
    if not rows:
        await ctx.send("❌ Belum ada data koin yang tercatat untuk hari ini.")
        return

    table = "```text\n"
    table += f"{'NO':<4} | {'NAME / USERNAME':<24} | {'TODAY COINS':<10}\n"
    table += "-" * 46 + "\n"
    for idx, (username, display_name, coins) in enumerate(rows, start=1):
        name_str = display_name if display_name else f"@{username}"
        if len(name_str) > 23:
            name_str = name_str[:20] + "..."
        table += f"{idx:<4} | {name_str:<24} | {coins:<10} c\n"
    table += "```"
    await ctx.send(f"🏆 **TIKTOK LIVE DAILY RANKING (HARI INI)**\n{table}")

@bot.command(name="livesessions", aliases=["sessions"])
async def livesessions(ctx, host_username: str = None):
    conn = get_db_connection()
    try:
        if host_username:
            u_clean = host_username.lstrip("@").lower()
            res = conn.execute("""
                SELECT session_id, tiktok_username, start_time, duration_seconds, total_coins 
                FROM live_sessions WHERE tiktok_username = ? ORDER BY start_time DESC LIMIT 5
            """, (u_clean,))
        else:
            res = conn.execute("""
                SELECT session_id, tiktok_username, start_time, duration_seconds, total_coins 
                FROM live_sessions ORDER BY start_time DESC LIMIT 5
            """)
        
        rows = res.fetchall()
        if not rows:
            await ctx.send("❌ Belum ada riwayat sesi live yang tercatat.")
            return

        msg = "📹 **Riwayat Sesi Live Terbaru:**\n"
        for s_id, host, s_time, dur_sec, coins in rows:
            hrs = dur_sec // 3600
            mins = (dur_sec % 3600) // 60
            secs = dur_sec % 60
            dur_str = f"{hrs:02d}:{mins:02d}:{secs:02d}" if dur_sec > 0 else "Masih Live / Disconnect"
            msg += f"• Sesi `#{s_id}` (@{host}) | Mulai: `{s_time}` | Durasi: `{dur_str}` | Total: **{coins} c**\n"

        await ctx.send(msg)
    finally:
        conn.close()

async def main():
    init_db()
    keep_alive(handle_api_announcement)

    # Jalankan observer secara dinamis dengan callback announcement, role, dan verifikasi komentar live chat
    asyncio.create_task(run_all_observers(
        announcement_callback=process_live_announcement,
        role_callback=auto_assign_role,
        comment_callback=handle_live_comment_verification
    ))

    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Bot] Stopped successfully.")
