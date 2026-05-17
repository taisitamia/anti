"""
Bot de Discord — versión 3
Prefijo: , (coma) — igual que Greed
Slash commands sincronizados automáticamente al iniciar.
"""

import asyncio
import copy
import json
import logging
import os
import platform
import random
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
import datetime as dt
import functools

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s » %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8'),
    ],
)
log = logging.getLogger('bot')

# ─── Config ──────────────────────────────────────────────────────────────────
CONFIG_FILE   = 'config.json'
ANTINUKE_FILE = 'Antinuke.json'
WARNS_FILE    = 'warns.json'
PAREJAS_FILE  = 'parejas.json'
FAMILIA_FILE  = 'familia.json'
CUMPLE_FILE   = 'cumpleanos.json'
SNAP_FILE     = 'server_snapshot.json'   # snapshots de canales/categorías

PREFIX = ','


def cargar_config() -> dict:
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    token_env = os.environ.get('DISCORD_TOKEN')
    if token_env:
        cfg['token'] = token_env
    if cfg.get('token') in ('', 'TU_TOKEN_AQUÍ', None):
        log.critical('No se encontró DISCORD_TOKEN.')
        sys.exit(1)
    return cfg


CONFIG           = cargar_config()
TOKEN            = CONFIG['token']
ROLES_STAFF_CFG  = CONFIG.get('roles_staff', ['👑 Administración', '🛡️ Moderador'])

# Roles por servidor para el comando ,v (acceso rápido)
ROLES_POR_SERVIDOR: dict[int, dict] = CONFIG.get('roles_por_servidor', {})

# ─── Bot ─────────────────────────────────────────────────────────────────────
intents                 = discord.Intents.default()
intents.members         = True
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)
bot.remove_command('help')


# ─── Permisos helpers ────────────────────────────────────────────────────────
def es_admin(ctx) -> bool:
    return ctx.author.guild_permissions.administrator

def es_staff(ctx) -> bool:
    return (
        ctx.author.guild_permissions.administrator
        or ctx.author.guild_permissions.manage_roles
        or any(r.name in ROLES_STAFF_CFG for r in ctx.author.roles)
    )

def es_owner_o_admin(ctx) -> bool:
    return ctx.author.id == ctx.guild.owner_id or ctx.author.guild_permissions.administrator


# ═══════════════════════════════════════════════════════════════════════════════
# ANTINUKE — datos
# ═══════════════════════════════════════════════════════════════════════════════
ANTINUKE_DEFAULT: dict = {
    'activo': True,
    'whitelist': [],
    'owner_id': None,
    'limites': {'ban': 3, 'kick': 3, 'roles': 3, 'canales': 3, 'webhooks': 3, 'roles_peligrosos': 1},
    'ventana': 10,
    'accion': 'ban',
    'log_channel': None,
    'antiraid': {'activo': False, 'joins_limite': 10, 'joins_ventana': 10, 'accion': 'kick'},
    'antilinks': {'activo': False, 'whitelist_canales': [], 'whitelist_roles': []},
    'antispam': {'activo': False, 'mensajes_limite': 5, 'ventana': 5},
    'antibot': {'activo': False},
    'verificacion': {
        'activo': False, 'rol_verificado': None,
        'rol_no_verificado': None, 'canal': None, 'emoji': '✅',
    },
    'warn_sistema': {},
    'mute_rol': None,
}

# Permisos considerados «peligrosos» para el antinuke de roles
PERMS_PELIGROSOS = {
    'administrator', 'ban_members', 'kick_members',
    'manage_guild', 'manage_roles', 'manage_channels',
    'manage_webhooks', 'mention_everyone',
}


def _cargar_db_antinuke() -> dict:
    if os.path.exists(ANTINUKE_FILE):
        with open(ANTINUKE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def _guardar_db_antinuke(db: dict):
    with open(ANTINUKE_FILE, 'w', encoding='utf-8') as f:
        json.dump(db, f, indent=2, ensure_ascii=False)


def cargar_antinuke(guild_id: int | None = None) -> dict:
    db  = _cargar_db_antinuke()
    key = str(guild_id) if guild_id else '__global__'
    data = db.get(key, {})
    result = copy.deepcopy(ANTINUKE_DEFAULT)
    for k, v in data.items():
        if k == 'limites' and isinstance(v, dict):
            result['limites'].update(v)
        else:
            result[k] = v
    return result


def guardar_antinuke(cfg: dict, guild_id: int | None = None):
    db  = _cargar_db_antinuke()
    key = str(guild_id) if guild_id else '__global__'
    db[key] = cfg
    _guardar_db_antinuke(db)


# trackers en memoria
_acciones:      dict = defaultdict(lambda: defaultdict(list))
_joins_recents: dict = defaultdict(list)
_spam_tracker:  dict = defaultdict(lambda: defaultdict(list))


def registrar_accion(user_id: int, tipo: str, guild_id: int = 0) -> int:
    cfg     = cargar_antinuke(guild_id)
    ventana = cfg.get('ventana', 10)
    ahora   = time.time()
    _acciones[guild_id][user_id] = [
        (t, a) for t, a in _acciones[guild_id][user_id] if ahora - t <= ventana
    ]
    _acciones[guild_id][user_id].append((ahora, tipo))
    return sum(1 for _, a in _acciones[guild_id][user_id] if a == tipo)


def es_seguro(user_id: int, guild: discord.Guild) -> bool:
    cfg = cargar_antinuke(guild.id)
    if guild.owner_id == user_id:
        return True
    owner = cfg.get('owner_id')
    if owner and user_id == int(owner):
        return True
    return user_id in [int(x) for x in cfg.get('whitelist', [])]


def es_owner_an(ctx) -> bool:
    cfg   = cargar_antinuke(ctx.guild.id)
    owner = cfg.get('owner_id')
    return (
        ctx.author.id == ctx.guild.owner_id
        or (owner and ctx.author.id == int(owner))
    )


# ─── Castigos AntiNuke ───────────────────────────────────────────────────────
async def log_antinuke(guild: discord.Guild, titulo: str, desc: str, color: int = 0xFF0000):
    cfg = cargar_antinuke(guild.id)
    ch_id = cfg.get('log_channel')
    if not ch_id:
        return
    ch = guild.get_channel(int(ch_id))
    if not ch:
        return
    embed = discord.Embed(
        title=titulo, description=desc,
        color=color, timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text='AntiNuke — Sistema de Seguridad')
    try:
        await ch.send(embed=embed)
    except Exception:
        pass


async def ejecutar_castigo(guild: discord.Guild, member, razon: str, accion: str | None = None):
    cfg    = cargar_antinuke(guild.id)
    accion = accion or cfg.get('accion', 'ban')
    try:
        # primero quitar todos los roles peligrosos
        roles_peligrosos = [
            r for r in member.roles
            if any(getattr(r.permissions, p, False) for p in PERMS_PELIGROSOS)
            and r < guild.me.top_role
        ]
        if roles_peligrosos:
            await member.remove_roles(*roles_peligrosos, reason=f'[AntiNuke] {razon}')

        if accion == 'ban':
            await guild.ban(discord.Object(id=member.id), reason=f'[AntiNuke] {razon}', delete_message_days=0)
        elif accion == 'kick':
            await guild.kick(discord.Object(id=member.id), reason=f'[AntiNuke] {razon}')
        elif accion == 'strip':
            roles_a_quitar = [r for r in member.roles if r != guild.default_role and r < guild.me.top_role]
            if roles_a_quitar:
                await member.remove_roles(*roles_a_quitar, reason=f'[AntiNuke] {razon}')
        elif accion == 'timeout':
            until = discord.utils.utcnow() + dt.timedelta(hours=24)
            await member.timeout(until, reason=f'[AntiNuke] {razon}')
    except discord.Forbidden:
        log.error(f'[AntiNuke] Sin permisos para castigar a {member}')
    except Exception as e:
        log.error(f'[AntiNuke] Error castigando a {member}: {e}')


async def ejecutar_castigo_bot(guild: discord.Guild, bot_user, razon: str):
    try:
        await guild.ban(discord.Object(id=bot_user.id), reason=f'[AntiNuke] Bot nukero — {razon}', delete_message_days=0)
        await log_antinuke(guild, '🤖 Bot Nukero Baneado', f'**Bot:** {bot_user.mention} (`{bot_user.id}`)\n**Razón:** {razon}')
    except Exception as e:
        log.error(f'[AntiNuke] Error baneando bot nukero {bot_user.id}: {e}')


async def _obtener_miembro(guild: discord.Guild, user_id: int):
    m = guild.get_member(user_id)
    if not m:
        try:
            m = await guild.fetch_member(user_id)
        except Exception:
            pass
    return m


# ═══════════════════════════════════════════════════════════════════════════════
# SNAPSHOT de canales y categorías (guardado cada 30 min)
# ═══════════════════════════════════════════════════════════════════════════════
def cargar_snapshots() -> dict:
    if os.path.exists(SNAP_FILE):
        with open(SNAP_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def guardar_snapshots(data: dict):
    with open(SNAP_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _snapshot_guild(guild: discord.Guild) -> dict:
    cats = {}
    for cat in guild.categories:
        cats[str(cat.id)] = {
            'name': cat.name,
            'position': cat.position,
            'channels': [
                {
                    'id': str(c.id), 'name': c.name, 'type': str(c.type),
                    'position': c.position, 'topic': getattr(c, 'topic', None),
                    'nsfw': getattr(c, 'nsfw', False),
                    'slowmode': getattr(c, 'slowmode_delay', 0),
                }
                for c in cat.channels
            ],
        }
    # canales sin categoría
    no_cat = [
        {
            'id': str(c.id), 'name': c.name, 'type': str(c.type),
            'position': c.position, 'topic': getattr(c, 'topic', None),
            'nsfw': getattr(c, 'nsfw', False),
            'slowmode': getattr(c, 'slowmode_delay', 0),
        }
        for c in guild.channels
        if c.category is None and not isinstance(c, discord.CategoryChannel)
    ]
    return {
        'ts': int(time.time()),
        'categories': cats,
        'no_category': no_cat,
    }


@tasks.loop(minutes=30)
async def snapshot_loop():
    snaps = cargar_snapshots()
    for guild in bot.guilds:
        snaps[str(guild.id)] = _snapshot_guild(guild)
    guardar_snapshots(snaps)
    log.info(f'[Snapshot] Guardados {len(bot.guilds)} servidores.')


@snapshot_loop.before_loop
async def before_snapshot():
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════════════════════════════════════
# EVENTOS ANTINUKE
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    cfg = cargar_antinuke(guild.id)
    if not cfg.get('activo'):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban)]
        if not entries:
            return
        autor = entries[0].user
        if autor.id == bot.user.id or es_seguro(autor.id, guild):
            return
        count = registrar_accion(autor.id, 'ban', guild.id)
        try:
            await guild.unban(user, reason=f'[AntiNuke] Ban no autorizado por {autor}')
            await log_antinuke(guild, '♻️ Ban Revertido',
                               f'**Víctima:** {user.mention}\n**Baneado por:** {autor.mention}', 0x00FF88)
        except Exception as e:
            log.error(f'[AntiNuke] No pude desbanear a {user}: {e}')
        if autor.bot:
            await ejecutar_castigo_bot(guild, autor, f'Ban no autorizado ({count})')
        else:
            m = await _obtener_miembro(guild, autor.id)
            if m:
                await ejecutar_castigo(guild, m, f'Ban no autorizado ({count} bans)')
                await log_antinuke(guild, '🔨 Ban No Autorizado',
                                   f'**Usuario:** {autor.mention}\n**Bans:** {count}\n**Acción:** `{cfg["accion"]}`')
            else:
                try:
                    await guild.ban(discord.Object(id=autor.id), reason='[AntiNuke] Ban no autorizado')
                except Exception:
                    pass
    except Exception as e:
        log.error(f'[AntiNuke] on_member_ban: {e}')


@bot.event
async def on_member_remove(member: discord.Member):
    cfg = cargar_antinuke(member.guild.id)
    if not cfg.get('activo'):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.kick)]
        if not entries or entries[0].target.id != member.id:
            return
        autor = entries[0].user
        if autor.id == bot.user.id or es_seguro(autor.id, member.guild):
            return
        count = registrar_accion(autor.id, 'kick', member.guild.id)
        if autor.bot:
            await ejecutar_castigo_bot(member.guild, autor, f'Kick no autorizado ({count})')
        else:
            m = await _obtener_miembro(member.guild, autor.id)
            if m:
                await ejecutar_castigo(member.guild, m, f'Kick no autorizado ({count})')
                await log_antinuke(member.guild, '👢 Kick No Autorizado',
                                   f'**Por:** {autor.mention}\n**A:** {member.mention}\n**Kicks:** {count}')
    except Exception as e:
        log.error(f'[AntiNuke] on_member_remove: {e}')


@bot.event
async def on_guild_role_delete(role: discord.Role):
    cfg = cargar_antinuke(role.guild.id)
    if not cfg.get('activo'):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in role.guild.audit_logs(limit=5, action=discord.AuditLogAction.role_delete)]
        if not entries:
            return
        autor = entries[0].user
        if autor.id == bot.user.id or es_seguro(autor.id, role.guild):
            return
        count = registrar_accion(autor.id, 'roles', role.guild.id)
        # restaurar rol
        try:
            nuevo = await role.guild.create_role(
                name=role.name, color=role.color, hoist=role.hoist,
                mentionable=role.mentionable, permissions=role.permissions,
                reason=f'[AntiNuke] Restaurando rol eliminado por {autor}',
            )
            await log_antinuke(role.guild, '♻️ Rol Restaurado',
                               f'**Rol:** `{role.name}`\n**Eliminado por:** {autor.mention}\n**Nuevo:** {nuevo.mention}', 0x00FF88)
        except Exception as e:
            log.error(f'[AntiNuke] No pude restaurar rol {role.name}: {e}')
        if count >= cfg['limites']['roles']:
            m = await _obtener_miembro(role.guild, autor.id)
            if autor.bot:
                await ejecutar_castigo_bot(role.guild, autor, f'Borrado masivo de roles ({count})')
            elif m:
                await ejecutar_castigo(role.guild, m, f'Borrado masivo de roles ({count})')
                await log_antinuke(role.guild, '🗑️ Borrado Masivo de Roles',
                                   f'**Usuario:** {autor.mention}\n**Roles:** {count}')
    except Exception as e:
        log.error(f'[AntiNuke] on_guild_role_delete: {e}')


@bot.event
async def on_guild_role_create(role: discord.Role):
    cfg = cargar_antinuke(role.guild.id)
    if not cfg.get('activo'):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in role.guild.audit_logs(limit=5, action=discord.AuditLogAction.role_create)]
        if not entries:
            return
        autor = entries[0].user
        if autor.id == bot.user.id or es_seguro(autor.id, role.guild):
            return
        count = registrar_accion(autor.id, 'roles', role.guild.id)
        try:
            await role.delete(reason=f'[AntiNuke] Rol no autorizado por {autor}')
            await log_antinuke(role.guild, '🗑️ Rol No Autorizado Eliminado',
                               f'**Rol:** `{role.name}`\n**Por:** {autor.mention}')
        except Exception as e:
            log.error(f'[AntiNuke] No pude eliminar rol: {e}')
        if count >= cfg['limites']['roles']:
            m = await _obtener_miembro(role.guild, autor.id)
            if autor.bot:
                await ejecutar_castigo_bot(role.guild, autor, f'Creación masiva de roles ({count})')
            elif m:
                await ejecutar_castigo(role.guild, m, f'Creación masiva de roles ({count})')
                await log_antinuke(role.guild, '🆕 Creación Masiva de Roles',
                                   f'**Usuario:** {autor.mention}\n**Roles:** {count}')
    except Exception as e:
        log.error(f'[AntiNuke] on_guild_role_create: {e}')


@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    """
    Detecta cuando alguien (humano o bot) modifica un rol para añadirle
    permisos peligrosos (admin, ban, manage_guild, etc.).
    También detecta asignación manual de roles peligrosos.
    """
    cfg = cargar_antinuke(before.guild.id)
    if not cfg.get('activo'):
        return

    # ¿Se añadieron permisos peligrosos?
    before_perms = set(n for n, v in before.permissions if v)
    after_perms  = set(n for n, v in after.permissions  if v)
    nuevos_peligrosos = (after_perms - before_perms) & PERMS_PELIGROSOS
    if not nuevos_peligrosos:
        return

    await asyncio.sleep(0.3)
    try:
        entries = [e async for e in before.guild.audit_logs(limit=5, action=discord.AuditLogAction.role_update)]
        if not entries:
            return
        autor = entries[0].user
        if autor.id == bot.user.id or es_seguro(autor.id, before.guild):
            return

        limit = cfg['limites'].get('roles_peligrosos', 1)
        count = registrar_accion(autor.id, 'roles_peligrosos', before.guild.id)

        # revertir los permisos peligrosos
        try:
            perms_revertidos = discord.Permissions(**{n: False for n in nuevos_peligrosos})
            # fusionamos: quitamos solo los recién añadidos
            new_perm_value = discord.Permissions(after.permissions.value)
            for p in nuevos_peligrosos:
                setattr(new_perm_value, p, False)
            await after.edit(permissions=new_perm_value, reason=f'[AntiNuke] Permisos peligrosos revertidos por {autor}')
            await log_antinuke(
                before.guild, '⚠️ Permisos Peligrosos Revertidos',
                f'**Rol:** {after.mention}\n**Permisos bloqueados:** `{", ".join(nuevos_peligrosos)}`\n**Por:** {autor.mention}',
                0xFFAA00,
            )
        except Exception as e:
            log.error(f'[AntiNuke] No pude revertir permisos de {after.name}: {e}')

        if count >= limit:
            m = await _obtener_miembro(before.guild, autor.id)
            if autor.bot:
                await ejecutar_castigo_bot(before.guild, autor, f'Escalada de permisos ({count})')
            elif m:
                await ejecutar_castigo(before.guild, m, f'Escalada de permisos ({count})')
                await log_antinuke(
                    before.guild, '🛑 Escalada de Privilegios Detectada',
                    f'**Usuario:** {autor.mention}\n**Roles alterados:** {count}\n**Acción:** `{cfg["accion"]}`',
                )
    except Exception as e:
        log.error(f'[AntiNuke] on_guild_role_update: {e}')


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """
    Detecta cuando se le asigna manualmente (o por bot) un rol peligroso
    a un miembro que no estaba en la whitelist.
    """
    cfg = cargar_antinuke(before.guild.id)
    if not cfg.get('activo'):
        return
    if es_seguro(after.id, before.guild):
        return

    nuevos_roles = set(after.roles) - set(before.roles)
    roles_peligrosos = [
        r for r in nuevos_roles
        if any(getattr(r.permissions, p, False) for p in PERMS_PELIGROSOS)
    ]
    if not roles_peligrosos:
        return

    await asyncio.sleep(0.3)
    try:
        entries = [e async for e in before.guild.audit_logs(limit=5, action=discord.AuditLogAction.member_role_update)]
        if not entries:
            return
        autor = entries[0].user
        if autor.id == bot.user.id or es_seguro(autor.id, before.guild):
            return

        limit = cfg['limites'].get('roles_peligrosos', 1)
        count = registrar_accion(autor.id, 'roles_peligrosos', before.guild.id)

        # quitar los roles peligrosos asignados
        try:
            roles_a_quitar = [r for r in roles_peligrosos if r < before.guild.me.top_role]
            if roles_a_quitar:
                await after.remove_roles(*roles_a_quitar, reason=f'[AntiNuke] Rol peligroso no autorizado')
            await log_antinuke(
                before.guild, '🚨 Rol Peligroso Asignado — Revertido',
                f'**Miembro:** {after.mention}\n'
                f'**Roles revertidos:** {", ".join(r.mention for r in roles_peligrosos)}\n'
                f'**Asignado por:** {autor.mention}',
                0xFF5500,
            )
        except Exception as e:
            log.error(f'[AntiNuke] Error revirtiendo rol peligroso: {e}')

        if count >= limit:
            m = await _obtener_miembro(before.guild, autor.id)
            if autor.bot:
                await ejecutar_castigo_bot(before.guild, autor, f'Asignación masiva de roles peligrosos ({count})')
            elif m:
                await ejecutar_castigo(before.guild, m, f'Asignación de roles peligrosos ({count})')
                await log_antinuke(
                    before.guild, '🛑 Asignación Masiva de Roles Peligrosos',
                    f'**Por:** {autor.mention}\n**Incidentes:** {count}\n**Acción:** `{cfg["accion"]}`',
                )
    except Exception as e:
        log.error(f'[AntiNuke] on_member_update: {e}')


@bot.event
async def on_guild_channel_delete(channel):
    cfg = cargar_antinuke(channel.guild.id)
    if not cfg.get('activo'):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in channel.guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_delete)]
        if not entries:
            return
        autor = entries[0].user
        if autor.id == bot.user.id or es_seguro(autor.id, channel.guild):
            return
        count = registrar_accion(autor.id, 'canales', channel.guild.id)

        # restaurar canal usando snapshot si existe
        snaps      = cargar_snapshots()
        guild_snap = snaps.get(str(channel.guild.id), {})

        try:
            overwrites = channel.overwrites

            # Si el canal tenía categoría pero ya no existe (fue eliminada también),
            # intentar recrearla primero desde el snapshot
            categoria_obj = channel.category
            if channel.category is None and not isinstance(channel, discord.CategoryChannel):
                # buscar en snapshot si este canal pertenecía a alguna categoría
                for cat_id, cat_data in guild_snap.get('categories', {}).items():
                    for ch_data in cat_data.get('channels', []):
                        if ch_data['id'] == str(channel.id):
                            # la categoría ya no existe en el servidor, recrearla
                            cat_existente = channel.guild.get_channel(int(cat_id))
                            if not cat_existente:
                                try:
                                    cat_existente = await channel.guild.create_category(
                                        name=cat_data['name'],
                                        reason=f'[AntiNuke] Restaurando categoría por {autor}',
                                    )
                                    await log_antinuke(
                                        channel.guild, '♻️ Categoría Restaurada',
                                        f'**Categoría:** `{cat_data["name"]}`\n**Eliminada por:** {autor.mention}\n**Restaurada:** {cat_existente.mention}',
                                        0x00FF88,
                                    )
                                except Exception as e:
                                    log.error(f'[AntiNuke] No pude restaurar categoría {cat_data["name"]}: {e}')
                            categoria_obj = cat_existente
                            break

            if isinstance(channel, discord.TextChannel):
                nuevo = await channel.guild.create_text_channel(
                    name=channel.name, topic=channel.topic,
                    slowmode_delay=channel.slowmode_delay, nsfw=channel.nsfw,
                    overwrites=overwrites, category=categoria_obj,
                    reason=f'[AntiNuke] Restaurando canal por {autor}',
                )
            elif isinstance(channel, discord.VoiceChannel):
                nuevo = await channel.guild.create_voice_channel(
                    name=channel.name, bitrate=channel.bitrate,
                    user_limit=channel.user_limit, overwrites=overwrites,
                    category=categoria_obj, reason=f'[AntiNuke] Restaurando canal por {autor}',
                )
            elif isinstance(channel, discord.CategoryChannel):
                nuevo = await channel.guild.create_category(
                    name=channel.name, overwrites=overwrites,
                    reason=f'[AntiNuke] Restaurando categoría por {autor}',
                )
                # restaurar también los canales que estaban dentro según el snapshot
                cat_snap = guild_snap.get('categories', {}).get(str(channel.id), {})
                for ch_data in cat_snap.get('channels', []):
                    try:
                        if ch_data['type'] == 'text':
                            await channel.guild.create_text_channel(
                                name=ch_data['name'], topic=ch_data.get('topic'),
                                slowmode_delay=ch_data.get('slowmode', 0),
                                nsfw=ch_data.get('nsfw', False),
                                category=nuevo,
                                reason=f'[AntiNuke] Restaurando canal de categoría por {autor}',
                            )
                        elif ch_data['type'] == 'voice':
                            await channel.guild.create_voice_channel(
                                name=ch_data['name'], category=nuevo,
                                reason=f'[AntiNuke] Restaurando canal de categoría por {autor}',
                            )
                    except Exception as e:
                        log.error(f'[AntiNuke] No pude restaurar canal {ch_data["name"]} de categoría: {e}')
            else:
                nuevo = await channel.guild.create_text_channel(
                    name=channel.name, overwrites=overwrites,
                    category=categoria_obj, reason=f'[AntiNuke] Restaurando canal por {autor}',
                )
            try:
                await nuevo.edit(position=channel.position)
            except Exception:
                pass
            await log_antinuke(
                channel.guild, '♻️ Canal Restaurado',
                f'**Canal:** `#{channel.name}`\n**Eliminado por:** {autor.mention}\n**Restaurado:** {nuevo.mention}',
                0x00FF88,
            )
        except Exception as e:
            log.error(f'[AntiNuke] No pude restaurar canal {channel.name}: {e}')

        if count >= cfg['limites']['canales']:
            m = await _obtener_miembro(channel.guild, autor.id)
            if autor.bot:
                await ejecutar_castigo_bot(channel.guild, autor, f'Borrado masivo de canales ({count})')
            elif m:
                await ejecutar_castigo(channel.guild, m, f'Borrado masivo de canales ({count})')
                await log_antinuke(channel.guild, '🗑️ Borrado Masivo de Canales',
                                   f'**Por:** {autor.mention}\n**Canales:** {count}')
    except Exception as e:
        log.error(f'[AntiNuke] on_guild_channel_delete: {e}')


@bot.event
async def on_guild_channel_create(channel):
    cfg = cargar_antinuke(channel.guild.id)
    if not cfg.get('activo'):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in channel.guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_create)]
        if not entries:
            return
        autor = entries[0].user
        if autor.id == bot.user.id or es_seguro(autor.id, channel.guild):
            return
        count = registrar_accion(autor.id, 'canales', channel.guild.id)
        try:
            nombre = channel.name
            await channel.delete(reason=f'[AntiNuke] Canal no autorizado por {autor}')
            await log_antinuke(channel.guild, '🗑️ Canal No Autorizado Eliminado',
                               f'**Canal:** `#{nombre}`\n**Por:** {autor.mention}')
        except Exception as e:
            log.error(f'[AntiNuke] No pude eliminar canal: {e}')
        if count >= cfg['limites']['canales']:
            m = await _obtener_miembro(channel.guild, autor.id)
            if autor.bot:
                await ejecutar_castigo_bot(channel.guild, autor, f'Creación masiva de canales ({count})')
            elif m:
                await ejecutar_castigo(channel.guild, m, f'Creación masiva de canales ({count})')
                await log_antinuke(channel.guild, '🆕 Creación Masiva de Canales',
                                   f'**Por:** {autor.mention}\n**Canales:** {count}')
    except Exception as e:
        log.error(f'[AntiNuke] on_guild_channel_create: {e}')


@bot.event
async def on_webhooks_update(channel):
    cfg = cargar_antinuke(channel.guild.id)
    if not cfg.get('activo'):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in channel.guild.audit_logs(limit=5, action=discord.AuditLogAction.webhook_create)]
        if not entries:
            return
        autor = entries[0].user
        if autor.id == bot.user.id or es_seguro(autor.id, channel.guild):
            return
        count = registrar_accion(autor.id, 'webhooks', channel.guild.id)
        if count >= cfg['limites']['webhooks']:
            m = await _obtener_miembro(channel.guild, autor.id)
            if autor.bot:
                await ejecutar_castigo_bot(channel.guild, autor, f'Webhooks masivos ({count})')
            elif m:
                await ejecutar_castigo(channel.guild, m, f'Webhooks masivos ({count})')
                await log_antinuke(channel.guild, '🕸️ Webhooks Masivos',
                                   f'**Por:** {autor.mention}\n**Webhooks:** {count}')
    except Exception as e:
        log.error(f'[AntiNuke] on_webhooks_update: {e}')


@bot.event
async def on_guild_update(before: discord.Guild, after: discord.Guild):
    """Detecta cambio de nombre o ícono no autorizado del servidor."""
    cfg = cargar_antinuke(before.id)
    if not cfg.get('activo'):
        return
    if before.name == after.name and before.icon == after.icon:
        return
    await asyncio.sleep(0.3)
    try:
        entries = [e async for e in before.audit_logs(limit=5, action=discord.AuditLogAction.guild_update)]
        if not entries:
            return
        autor = entries[0].user
        if autor.id == bot.user.id or es_seguro(autor.id, before):
            return
        cambios = []
        if before.name != after.name:
            cambios.append(f'Nombre: `{before.name}` → `{after.name}`')
        if before.icon != after.icon:
            cambios.append('Ícono del servidor cambiado')
        await log_antinuke(
            after, '⚠️ Servidor Modificado',
            f'**Por:** {autor.mention}\n' + '\n'.join(cambios),
            0xFF9900,
        )
        # revertir nombre si cambió
        if before.name != after.name:
            try:
                await after.edit(name=before.name, reason=f'[AntiNuke] Cambio revertido')
            except Exception:
                pass
    except Exception as e:
        log.error(f'[AntiNuke] on_guild_update: {e}')


@bot.event
async def on_member_join(member: discord.Member):
    cfg = cargar_antinuke(member.guild.id)

    # AntiBot
    if cfg.get('antibot', {}).get('activo') and member.bot:
        try:
            entry = await anext(member.guild.audit_logs(limit=1, action=discord.AuditLogAction.bot_add))
            autor = entry.user
            if not es_seguro(autor.id, member.guild):
                await member.kick(reason='[AntiBot] Bot no autorizado')
                await log_antinuke(member.guild, '🤖 Bot No Autorizado',
                                   f'**Bot:** {member.mention}\n**Añadido por:** {autor.mention}', 0xFFAA00)
                return
        except Exception:
            pass

    # AntiRaid
    ar = cfg.get('antiraid', {})
    if ar.get('activo'):
        ahora   = time.time()
        gid     = member.guild.id
        ventana = ar.get('joins_ventana', 10)
        _joins_recents[gid].append(ahora)
        while _joins_recents[gid] and ahora - _joins_recents[gid][0] > ventana:
            _joins_recents[gid].pop(0)
        if len(_joins_recents[gid]) >= ar.get('joins_limite', 10):
            accion = ar.get('accion', 'kick')
            try:
                if accion == 'ban':
                    await member.ban(reason='[AntiRaid]', delete_message_days=0)
                else:
                    await member.kick(reason='[AntiRaid]')
            except Exception:
                pass
            await log_antinuke(member.guild, '🚨 Raid Detectada',
                               f'**Joins/{ventana}s:** {len(_joins_recents[gid])}\n'
                               f'**Último:** {member.mention}\n**Acción:** `{accion}`', 0xFF0000)

    # Rol de no verificado
    ver = cfg.get('verificacion', {})
    if ver.get('activo') and ver.get('rol_no_verificado'):
        rol = member.guild.get_role(int(ver['rol_no_verificado']))
        if rol:
            try:
                await member.add_roles(rol)
            except Exception:
                pass


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return

    cfg = cargar_antinuke(message.guild.id)

    # AntiLinks
    al = cfg.get('antilinks', {})
    if al.get('activo'):
        wl_c = [int(x) for x in al.get('whitelist_canales', [])]
        wl_r = [int(x) for x in al.get('whitelist_roles', [])]
        tiene_link = any(x in message.content for x in ['http://', 'https://', 'discord.gg/', 'discord.com/invite/'])
        if (tiene_link
                and message.channel.id not in wl_c
                and not any(r.id in wl_r for r in message.author.roles)
                and not es_seguro(message.author.id, message.guild)):
            try:
                await message.delete()
                await message.channel.send(
                    f'🔗 {message.author.mention} Los links no están permitidos aquí.', delete_after=5)
            except Exception:
                pass
            return

    # AntiSpam
    asp = cfg.get('antispam', {})
    if asp.get('activo') and not es_seguro(message.author.id, message.guild):
        ahora   = time.time()
        ventana = asp.get('ventana', 5)
        limite  = asp.get('mensajes_limite', 5)
        gid     = message.guild.id
        uid     = message.author.id
        _spam_tracker[gid][uid] = [t for t in _spam_tracker[gid][uid] if ahora - t <= ventana]
        _spam_tracker[gid][uid].append(ahora)
        if len(_spam_tracker[gid][uid]) >= limite:
            try:
                until = discord.utils.utcnow() + dt.timedelta(minutes=5)
                await message.author.timeout(until, reason='[AntiSpam]')
                await message.channel.send(f'🔇 {message.author.mention} silenciado por spam.', delete_after=5)
                _spam_tracker[gid][uid] = []
                await log_antinuke(message.guild, '💬 Spam Detectado',
                                   f'**Usuario:** {message.author.mention}\n**Canal:** {message.channel.mention}')
            except Exception:
                pass

    await bot.process_commands(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if not payload.guild_id:
        return
    cfg = cargar_antinuke(payload.guild_id)
    ver = cfg.get('verificacion', {})
    if not ver.get('activo'):
        return
    canal_id = ver.get('canal')
    if not canal_id or payload.channel_id != int(canal_id):
        return
    if str(payload.emoji) != ver.get('emoji', '✅'):
        return
    guild  = bot.get_guild(payload.guild_id)
    member = guild and guild.get_member(payload.user_id)
    if not member or member.bot:
        return
    rol_ver = ver.get('rol_verificado')
    rol_no  = ver.get('rol_no_verificado')
    if rol_ver:
        r = guild.get_role(int(rol_ver))
        if r:
            try:
                await member.add_roles(r, reason='Verificación')
            except Exception:
                pass
    if rol_no:
        r = guild.get_role(int(rol_no))
        if r and r in member.roles:
            try:
                await member.remove_roles(r, reason='Verificación')
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER — respuesta dual prefix + slash
# ═══════════════════════════════════════════════════════════════════════════════
async def send_embed(target, **kwargs):
    """Envía embed tanto a ctx como a interaction."""
    if isinstance(target, discord.Interaction):
        if target.response.is_done():
            await target.followup.send(**kwargs)
        else:
            await target.response.send_message(**kwargs)
    else:
        await target.send(**kwargs)


# ═══════════════════════════════════════════════════════════════════════════════
# ────────── COMANDOS ANTINUKE ──────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name='antinuke')
@commands.check(es_owner_an)
async def antinuke_status(ctx):
    cfg   = cargar_antinuke(ctx.guild.id)
    activo = '🟢 Activo' if cfg['activo'] else '🔴 Desactivado'
    ar     = cfg['antiraid']
    al     = cfg['antilinks']
    asp    = cfg['antispam']
    ab     = cfg['antibot']
    embed  = discord.Embed(title='🛡️ Panel AntiNuke', color=0x5865F2)
    embed.add_field(name='Estado', value=activo, inline=True)
    embed.add_field(name='Acción', value=f'`{cfg["accion"]}`', inline=True)
    embed.add_field(name='Ventana', value=f'`{cfg["ventana"]}s`', inline=True)
    lims = cfg['limites']
    embed.add_field(
        name='Límites',
        value=(f'Ban: `{lims["ban"]}` | Kick: `{lims["kick"]}` | Roles: `{lims["roles"]}`\n'
               f'Canales: `{lims["canales"]}` | Webhooks: `{lims["webhooks"]}`\n'
               f'Roles peligrosos: `{lims.get("roles_peligrosos", 1)}`'),
        inline=False,
    )
    embed.add_field(name='AntiRaid', value='🟢' if ar['activo'] else '🔴', inline=True)
    embed.add_field(name='AntiLinks', value='🟢' if al['activo'] else '🔴', inline=True)
    embed.add_field(name='AntiSpam', value='🟢' if asp['activo'] else '🔴', inline=True)
    embed.add_field(name='AntiBot', value='🟢' if ab['activo'] else '🔴', inline=True)
    embed.add_field(name='Verificación', value='🟢' if cfg['verificacion']['activo'] else '🔴', inline=True)
    wl = cfg['whitelist']
    embed.add_field(name=f'Whitelist ({len(wl)})',
                    value=' '.join(f'<@{u}>' for u in wl[:5]) or 'Vacía', inline=False)
    await ctx.send(embed=embed)


@bot.command(name='an_ayuda', aliases=['nuke_ayuda'])
async def an_ayuda(ctx):
    p = PREFIX
    embed = discord.Embed(title='🛡️ Comandos AntiNuke', color=0x5865F2)
    embed.add_field(name='Control', value=f'`{p}antinuke` `{p}an_activar` `{p}an_desactivar`\n`{p}an_accion <ban/kick/strip/timeout>`\n`{p}an_ventana <seg>` `{p}an_logs #canal`\n`{p}an_owner @u` `{p}an_whitelist [@u]`', inline=False)
    embed.add_field(name='Límites', value=f'`{p}an_limite <tipo> <n>` — tipos: ban kick roles canales webhooks roles_peligrosos', inline=False)
    embed.add_field(name='AntiRaid', value=f'`{p}an_antiraid` `{p}an_antiraid_on` `{p}an_antiraid_off`\n`{p}an_antiraid_config <joins> <ventana> <accion>`', inline=False)
    embed.add_field(name='AntiLinks / AntiSpam / AntiBot', value=f'`{p}an_antilinks_on/off` `{p}an_antispam_on/off` `{p}an_antibot_on/off`', inline=False)
    embed.add_field(name='Verificación', value=f'`{p}an_ver_setup #canal @rol_ver [@rol_no]` `{p}an_ver_on/off`', inline=False)
    embed.add_field(name='Snapshot', value=f'`{p}an_snapshot` — Ver última snapshot\n`{p}an_restore` — Restaurar canales desde snapshot', inline=False)
    await ctx.send(embed=embed)


@bot.command(name='an_activar', aliases=['activar'])
@commands.check(es_owner_an)
async def an_activar(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['activo'] = True
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('🟢 **AntiNuke activado.**')


@bot.command(name='an_desactivar', aliases=['desactivar'])
@commands.check(es_owner_an)
async def an_desactivar(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['activo'] = False
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('🔴 **AntiNuke desactivado.**')


@bot.command(name='an_whitelist', aliases=['whitelist'])
@commands.check(es_owner_an)
async def an_whitelist(ctx, member: discord.Member = None):
    cfg = cargar_antinuke(ctx.guild.id)
    wl  = cfg.get('whitelist', [])
    if member is None:
        mlist = ' '.join(f'<@{u}>' for u in wl) if wl else 'Vacía'
        return await ctx.send(f'🛡️ **Whitelist:** {mlist}')
    uid = str(member.id)
    if uid in wl:
        wl.remove(uid)
        msg = f'❌ {member.mention} quitado de whitelist.'
    else:
        wl.append(uid)
        msg = f'✅ {member.mention} añadido a whitelist.'
    cfg['whitelist'] = wl
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(msg)


@bot.command(name='an_accion', aliases=['accion'])
@commands.check(es_owner_an)
async def an_accion(ctx, accion: str):
    if accion not in ('ban', 'kick', 'strip', 'timeout'):
        return await ctx.send('❌ Usa: `ban`, `kick`, `strip`, `timeout`')
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['accion'] = accion
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ Acción cambiada a `{accion}`.')


@bot.command(name='an_limite', aliases=['limite'])
@commands.check(es_owner_an)
async def an_limite(ctx, tipo: str, cantidad: int):
    tipos_validos = ('ban', 'kick', 'roles', 'canales', 'webhooks', 'roles_peligrosos')
    if tipo not in tipos_validos or not 1 <= cantidad <= 20:
        return await ctx.send(f'❌ Tipo: {"/".join(tipos_validos)} | Cantidad: 1-20')
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['limites'][tipo] = cantidad
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ Límite de `{tipo}` → `{cantidad}`.')


@bot.command(name='an_ventana', aliases=['ventana'])
@commands.check(es_owner_an)
async def an_ventana(ctx, segundos: int):
    if not 5 <= segundos <= 120:
        return await ctx.send('❌ Entre 5 y 120 segundos.')
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['ventana'] = segundos
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ Ventana → `{segundos}s`.')


@bot.command(name='an_logs', aliases=['logs'])
@commands.check(es_owner_an)
async def an_logs(ctx, canal: discord.TextChannel = None):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['log_channel'] = str(canal.id) if canal else None
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ Logs → {canal.mention if canal else "desactivados"}.')


@bot.command(name='an_owner', aliases=['owner'])
@commands.check(lambda ctx: ctx.author.id == ctx.guild.owner_id)
async def an_owner(ctx, member: discord.Member):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['owner_id'] = str(member.id)
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ Owner AntiNuke → {member.mention}.')


# AntiRaid
@bot.command(name='an_antiraid', aliases=['antiraid'])
@commands.check(es_owner_an)
async def an_antiraid_status(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    ar  = cfg['antiraid']
    embed = discord.Embed(title='🚨 AntiRaid', color=0xFF4444)
    embed.add_field(name='Estado', value='🟢 Activo' if ar['activo'] else '🔴 Desactivado', inline=True)
    embed.add_field(name='Límite', value=f'{ar["joins_limite"]} joins', inline=True)
    embed.add_field(name='Ventana', value=f'{ar["joins_ventana"]}s', inline=True)
    embed.add_field(name='Acción', value=f'`{ar["accion"]}`', inline=True)
    await ctx.send(embed=embed)


@bot.command(name='an_antiraid_on', aliases=['antiraid_on'])
@commands.check(es_owner_an)
async def an_antiraid_on(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['antiraid']['activo'] = True
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('🟢 **AntiRaid activado.**')


@bot.command(name='an_antiraid_off', aliases=['antiraid_off'])
@commands.check(es_owner_an)
async def an_antiraid_off(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['antiraid']['activo'] = False
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('🔴 **AntiRaid desactivado.**')


@bot.command(name='an_antiraid_config', aliases=['antiraid_config'])
@commands.check(es_owner_an)
async def an_antiraid_config(ctx, joins: int, ventana: int, accion: str = 'kick'):
    if accion not in ('ban', 'kick'):
        return await ctx.send('❌ Acción: `ban` o `kick`.')
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['antiraid'].update({'joins_limite': joins, 'joins_ventana': ventana, 'accion': accion})
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ AntiRaid: `{joins}` joins / `{ventana}s` → `{accion}`.')


# AntiLinks
@bot.command(name='an_antilinks_on', aliases=['antilinks_on'])
@commands.check(es_owner_an)
async def an_antilinks_on(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['antilinks']['activo'] = True
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('🟢 **AntiLinks activado.**')


@bot.command(name='an_antilinks_off', aliases=['antilinks_off'])
@commands.check(es_owner_an)
async def an_antilinks_off(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['antilinks']['activo'] = False
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('🔴 **AntiLinks desactivado.**')


@bot.command(name='an_links_canal', aliases=['links_canal'])
@commands.check(es_owner_an)
async def an_links_canal(ctx, canal: discord.TextChannel):
    cfg = cargar_antinuke(ctx.guild.id)
    wl  = cfg['antilinks']['whitelist_canales']
    cid = str(canal.id)
    if cid in wl:
        wl.remove(cid)
        msg = f'❌ {canal.mention} quitado de whitelist de links.'
    else:
        wl.append(cid)
        msg = f'✅ {canal.mention} en whitelist de links.'
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(msg)


@bot.command(name='an_links_rol', aliases=['links_rol'])
@commands.check(es_owner_an)
async def an_links_rol(ctx, *, nombre_rol: str):
    rol = discord.utils.find(lambda r: r.name.lower() == nombre_rol.lower(), ctx.guild.roles)
    if not rol:
        return await ctx.send(f'❌ No encontré rol `{nombre_rol}`.')
    cfg = cargar_antinuke(ctx.guild.id)
    wl  = cfg['antilinks']['whitelist_roles']
    rid = str(rol.id)
    if rid in wl:
        wl.remove(rid)
        msg = f'❌ `{rol.name}` quitado de whitelist de links.'
    else:
        wl.append(rid)
        msg = f'✅ `{rol.name}` en whitelist de links.'
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(msg)


# AntiSpam
@bot.command(name='an_antispam_on', aliases=['antispam_on'])
@commands.check(es_owner_an)
async def an_antispam_on(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['antispam']['activo'] = True
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('🟢 **AntiSpam activado.**')


@bot.command(name='an_antispam_off', aliases=['antispam_off'])
@commands.check(es_owner_an)
async def an_antispam_off(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['antispam']['activo'] = False
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('🔴 **AntiSpam desactivado.**')


@bot.command(name='an_spam_config', aliases=['spam_config'])
@commands.check(es_owner_an)
async def an_spam_config(ctx, mensajes: int, ventana: int):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['antispam'].update({'mensajes_limite': mensajes, 'ventana': ventana})
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ AntiSpam: `{mensajes}` mensajes / `{ventana}s`.')


# AntiBot
@bot.command(name='an_antibot_on', aliases=['antibot_on'])
@commands.check(es_owner_an)
async def an_antibot_on(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['antibot']['activo'] = True
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('🟢 **AntiBot activado.**')


@bot.command(name='an_antibot_off', aliases=['antibot_off'])
@commands.check(es_owner_an)
async def an_antibot_off(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['antibot']['activo'] = False
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('🔴 **AntiBot desactivado.**')


# Verificación
@bot.command(name='an_ver_setup', aliases=['ver_setup'])
@commands.check(es_owner_an)
async def an_ver_setup(ctx, canal: discord.TextChannel, rol_ver: discord.Role, rol_no_ver: discord.Role = None):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['verificacion'].update({
        'canal': str(canal.id),
        'rol_verificado': str(rol_ver.id),
        'rol_no_verificado': str(rol_no_ver.id) if rol_no_ver else None,
    })
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ Verificación configurada — canal: {canal.mention} | rol: {rol_ver.mention}')


@bot.command(name='an_ver_on', aliases=['ver_on'])
@commands.check(es_owner_an)
async def an_ver_on(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['verificacion']['activo'] = True
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('🟢 **Verificación activada.**')


@bot.command(name='an_ver_off', aliases=['ver_off'])
@commands.check(es_owner_an)
async def an_ver_off(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['verificacion']['activo'] = False
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('🔴 **Verificación desactivada.**')


# Snapshot
@bot.command(name='an_snapshot', aliases=['snapshot'])
@commands.check(es_owner_an)
async def an_snapshot(ctx):
    snaps = cargar_snapshots()
    gsnap = snaps.get(str(ctx.guild.id))
    if not gsnap:
        return await ctx.send('❌ Sin snapshot guardado aún (se guarda cada 30 min).')
    ts    = datetime.fromtimestamp(gsnap['ts'], tz=timezone.utc)
    ncats = len(gsnap['categories'])
    nchs  = sum(len(v['channels']) for v in gsnap['categories'].values()) + len(gsnap['no_category'])
    embed = discord.Embed(title='📸 Última Snapshot', color=0x5865F2)
    embed.add_field(name='Fecha', value=f'<t:{gsnap["ts"]}:R>', inline=True)
    embed.add_field(name='Categorías', value=ncats, inline=True)
    embed.add_field(name='Canales totales', value=nchs, inline=True)
    await ctx.send(embed=embed)


@bot.command(name='an_restore', aliases=['restore'])
@commands.check(es_owner_an)
async def an_restore(ctx):
    snaps = cargar_snapshots()
    gsnap = snaps.get(str(ctx.guild.id))
    if not gsnap:
        return await ctx.send('❌ Sin snapshot. Espera al próximo ciclo de 30 min.')
    msg = await ctx.send('⏳ Restaurando estructura de canales desde snapshot...')
    restaurados = 0
    for cid, cdata in gsnap['categories'].items():
        if not ctx.guild.get_channel(int(cid)):
            try:
                await ctx.guild.create_category(name=cdata['name'], reason='[AntiNuke] Restauración')
                restaurados += 1
            except Exception:
                pass
    for cdata in gsnap['no_category']:
        if not ctx.guild.get_channel(int(cdata['id'])):
            try:
                if cdata['type'] == 'text':
                    await ctx.guild.create_text_channel(name=cdata['name'], reason='[AntiNuke] Restauración')
                elif cdata['type'] == 'voice':
                    await ctx.guild.create_voice_channel(name=cdata['name'], reason='[AntiNuke] Restauración')
                restaurados += 1
            except Exception:
                pass
    await msg.edit(content=f'✅ Restaurados **{restaurados}** canales/categorías desde snapshot.')


# ═══════════════════════════════════════════════════════════════════════════════
# ────────── WARNS ──────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def cargar_warns() -> dict:
    if os.path.exists(WARNS_FILE):
        with open(WARNS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def guardar_warns(data: dict):
    with open(WARNS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


@bot.command(name='warn')
@commands.check(es_staff)
async def warn(ctx, member: discord.Member, *, razon: str = 'Sin razón'):
    data  = cargar_warns()
    gid   = str(ctx.guild.id)
    uid   = str(member.id)
    data.setdefault(gid, {}).setdefault(uid, [])
    data[gid][uid].append({'razon': razon, 'por': str(ctx.author.id), 'ts': int(time.time())})
    guardar_warns(data)
    total = len(data[gid][uid])
    embed = discord.Embed(title='⚠️ Warn', color=0xFFAA00)
    embed.add_field(name='Usuario', value=member.mention, inline=True)
    embed.add_field(name='Razón', value=razon, inline=True)
    embed.add_field(name='Total warns', value=total, inline=True)
    embed.add_field(name='Por', value=ctx.author.mention, inline=True)
    await ctx.send(embed=embed)
    try:
        await member.send(embed=discord.Embed(
            title=f'⚠️ Warn en {ctx.guild.name}',
            description=f'**Razón:** {razon}\n**Total:** {total}',
            color=0xFFAA00,
        ))
    except Exception:
        pass


@bot.command(name='warns')
async def ver_warns(ctx, member: discord.Member = None):
    member = member or ctx.author
    data   = cargar_warns()
    ws     = data.get(str(ctx.guild.id), {}).get(str(member.id), [])
    if not ws:
        return await ctx.send(f'✅ {member.mention} no tiene warns.')
    embed = discord.Embed(title=f'⚠️ Warns de {member.display_name}', color=0xFFAA00)
    embed.set_thumbnail(url=member.display_avatar.url)
    for i, w in enumerate(ws, 1):
        ts = f'<t:{w["ts"]}:R>' if 'ts' in w else ''
        embed.add_field(name=f'#{i} {ts}', value=w['razon'], inline=False)
    await ctx.send(embed=embed)


@bot.command(name='clearwarns', aliases=['limpiarwarns'])
@commands.check(es_staff)
async def clearwarns(ctx, member: discord.Member):
    data = cargar_warns()
    data.get(str(ctx.guild.id), {}).pop(str(member.id), None)
    guardar_warns(data)
    await ctx.send(f'✅ Warns de {member.mention} eliminados.')


@bot.command(name='delwarn')
@commands.check(es_staff)
async def delwarn(ctx, member: discord.Member, numero: int):
    data = cargar_warns()
    gid  = str(ctx.guild.id)
    uid  = str(member.id)
    ws   = data.get(gid, {}).get(uid, [])
    if not 1 <= numero <= len(ws):
        return await ctx.send(f'❌ Warn #{numero} no existe.')
    ws.pop(numero - 1)
    data.setdefault(gid, {})[uid] = ws
    guardar_warns(data)
    await ctx.send(f'✅ Warn #{numero} de {member.mention} eliminado.')


# ═══════════════════════════════════════════════════════════════════════════════
# ────────── MODERACIÓN ─────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name='ban')
@commands.check(es_admin)
async def ban_cmd(ctx, member: discord.Member, *, razon: str = 'Sin razón'):
    if member == ctx.author:
        return await ctx.send('❌ No puedes banearte a ti mismo.')
    if member.top_role >= ctx.guild.me.top_role:
        return await ctx.send('❌ No puedo banear a alguien con rol igual o mayor al mío.')
    try:
        await ctx.guild.ban(member, reason=f'[{ctx.author}] {razon}', delete_message_days=0)
    except discord.Forbidden:
        return await ctx.send('❌ Sin permisos.')
    embed = discord.Embed(title='🔨 Baneado', color=0xFF0000)
    embed.add_field(name='Usuario', value=f'{member} (`{member.id}`)', inline=True)
    embed.add_field(name='Razón', value=razon, inline=True)
    embed.add_field(name='Por', value=ctx.author.mention, inline=True)
    await ctx.send(embed=embed)


@bot.command(name='unban')
@commands.check(es_admin)
async def unban_cmd(ctx, *, usuario: str):
    bans = [entry async for entry in ctx.guild.bans()]
    objetivo = next((e.user for e in bans if str(e.user.id) == usuario or str(e.user) == usuario), None)
    if not objetivo:
        return await ctx.send(f'❌ No encontré `{usuario}` en los bans.')
    await ctx.guild.unban(objetivo, reason=f'Desbaneado por {ctx.author}')
    embed = discord.Embed(title='✅ Desbaneado', color=0x00FF00)
    embed.add_field(name='Usuario', value=f'{objetivo} (`{objetivo.id}`)', inline=True)
    embed.add_field(name='Por', value=ctx.author.mention, inline=True)
    await ctx.send(embed=embed)


@bot.command(name='kick')
@commands.check(es_admin)
async def kick_cmd(ctx, member: discord.Member, *, razon: str = 'Sin razón'):
    if member == ctx.author:
        return await ctx.send('❌ No puedes kickearte.')
    try:
        await ctx.guild.kick(member, reason=f'[{ctx.author}] {razon}')
    except discord.Forbidden:
        return await ctx.send('❌ Sin permisos.')
    embed = discord.Embed(title='👢 Expulsado', color=0xFF8800)
    embed.add_field(name='Usuario', value=str(member), inline=True)
    embed.add_field(name='Razón', value=razon, inline=True)
    embed.add_field(name='Por', value=ctx.author.mention, inline=True)
    await ctx.send(embed=embed)


@bot.command(name='mute')
@commands.check(es_admin)
async def mute_cmd(ctx, member: discord.Member, tiempo: str = '10m', *, razon: str = 'Sin razón'):
    unidades = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    try:
        u = tiempo[-1].lower()
        n = int(tiempo[:-1])
        if u not in unidades or not 1 <= n:
            raise ValueError
    except Exception:
        return await ctx.send('❌ Ej: `,mute @u 10m razón` (s/m/h/d)')
    segundos = n * unidades[u]
    if segundos > 40320 * 60:
        return await ctx.send('❌ Máximo 28 días.')
    try:
        until = discord.utils.utcnow() + dt.timedelta(seconds=segundos)
        await member.timeout(until, reason=f'[{ctx.author}] {razon}')
    except discord.Forbidden:
        return await ctx.send('❌ Sin permisos.')
    embed = discord.Embed(title='🔇 Muteado', color=0x555555)
    embed.add_field(name='Usuario', value=member.mention, inline=True)
    embed.add_field(name='Duración', value=tiempo, inline=True)
    embed.add_field(name='Razón', value=razon, inline=True)
    embed.add_field(name='Por', value=ctx.author.mention, inline=True)
    await ctx.send(embed=embed)


@bot.command(name='unmute')
@commands.check(es_admin)
async def unmute_cmd(ctx, member: discord.Member):
    try:
        await member.timeout(None)
    except discord.Forbidden:
        return await ctx.send('❌ Sin permisos.')
    await ctx.send(f'✅ {member.mention} **desmuteado**.')


@bot.command(name='softban')
@commands.check(es_admin)
async def softban(ctx, member: discord.Member, *, razon: str = 'Sin razón'):
    """Banea y desbanea para borrar mensajes recientes."""
    try:
        await ctx.guild.ban(member, reason=f'[Softban] [{ctx.author}] {razon}', delete_message_days=7)
        await ctx.guild.unban(member, reason='Softban — mensajes borrados')
    except discord.Forbidden:
        return await ctx.send('❌ Sin permisos.')
    embed = discord.Embed(title='🧹 Softban', color=0xFF8800)
    embed.add_field(name='Usuario', value=str(member), inline=True)
    embed.add_field(name='Razón', value=razon, inline=True)
    embed.add_field(name='Por', value=ctx.author.mention, inline=True)
    await ctx.send(embed=embed)


@bot.command(name='massban')
@commands.check(es_admin)
async def massban(ctx, *members: discord.Member):
    """Banea múltiples usuarios a la vez."""
    if not members:
        return await ctx.send('❌ Menciona al menos un usuario.')
    count, errors = 0, 0
    for m in members:
        try:
            await ctx.guild.ban(m, reason=f'[Massban] {ctx.author}', delete_message_days=0)
            count += 1
        except Exception:
            errors += 1
    await ctx.send(f'🔨 Baneados: **{count}** | Errores: **{errors}**')


@bot.command(name='masskick')
@commands.check(es_admin)
async def masskick(ctx, *members: discord.Member):
    """Kickea múltiples usuarios a la vez."""
    if not members:
        return await ctx.send('❌ Menciona al menos un usuario.')
    count, errors = 0, 0
    for m in members:
        try:
            await ctx.guild.kick(m, reason=f'[Masskick] {ctx.author}')
            count += 1
        except Exception:
            errors += 1
    await ctx.send(f'👢 Kickeados: **{count}** | Errores: **{errors}**')


# ─── Ban List ─────────────────────────────────────────────────────────────────

@bot.command(name='an_banlist', aliases=['banlist'])
@commands.check(es_staff)
async def an_banlist(ctx):
    """Muestra la lista de usuarios baneados del servidor (paginada de 10 en 10)."""
    bans = [entry async for entry in ctx.guild.bans()]
    if not bans:
        return await ctx.send('✅ No hay usuarios baneados.')

    paginas = []
    por_pag = 10
    for i in range(0, len(bans), por_pag):
        chunk = bans[i:i + por_pag]
        embed = discord.Embed(
            title=f'🔨 Lista de Bans — Página {i // por_pag + 1}/{(len(bans) - 1) // por_pag + 1}',
            color=0xFF0000,
            description='\n'.join(
                f'`{e.user.id}` **{e.user}** — {e.reason or "Sin razón"}' for e in chunk
            ),
        )
        embed.set_footer(text=f'Total: {len(bans)} baneados')
        paginas.append(embed)

    if len(paginas) == 1:
        return await ctx.send(embed=paginas[0])

    # Paginador simple
    idx  = 0
    btns = discord.ui.View(timeout=120)

    async def _prev(i: discord.Interaction):
        nonlocal idx
        if i.user.id != ctx.author.id:
            return await i.response.send_message('❌ No es tu menú.', ephemeral=True)
        idx = (idx - 1) % len(paginas)
        await i.response.edit_message(embed=paginas[idx])

    async def _next(i: discord.Interaction):
        nonlocal idx
        if i.user.id != ctx.author.id:
            return await i.response.send_message('❌ No es tu menú.', ephemeral=True)
        idx = (idx + 1) % len(paginas)
        await i.response.edit_message(embed=paginas[idx])

    b1 = discord.ui.Button(emoji='◀️', style=discord.ButtonStyle.secondary)
    b2 = discord.ui.Button(emoji='▶️', style=discord.ButtonStyle.primary)
    b1.callback = _prev
    b2.callback = _next
    btns.add_item(b1)
    btns.add_item(b2)
    await ctx.send(embed=paginas[0], view=btns)


@bot.command(name='limpiar', aliases=['clear', 'purge'])
@commands.check(es_admin)
async def limpiar(ctx, cantidad: int = 10):
    if not 1 <= cantidad <= 1000:
        return await ctx.send('❌ Entre 1 y 1000.')
    borrados = await ctx.channel.purge(limit=cantidad + 1)
    msg = await ctx.send(f'🗑️ **{len(borrados) - 1}** mensajes borrados.')
    await asyncio.sleep(3)
    await msg.delete()


@bot.command(name='limpiar_bots', aliases=['purgebots'])
@commands.check(es_admin)
async def limpiar_bots(ctx, cantidad: int = 50):
    borrados = await ctx.channel.purge(limit=cantidad, check=lambda m: m.author.bot)
    msg = await ctx.send(f'🤖 **{len(borrados)}** mensajes de bots borrados.')
    await asyncio.sleep(3)
    await msg.delete()


@bot.command(name='limpiar_usuario', aliases=['purgeuser'])
@commands.check(es_admin)
async def limpiar_usuario(ctx, member: discord.Member, cantidad: int = 50):
    borrados = await ctx.channel.purge(limit=cantidad, check=lambda m: m.author == member)
    msg = await ctx.send(f'🗑️ **{len(borrados)}** mensajes de {member.mention} borrados.')
    await asyncio.sleep(3)
    await msg.delete()


@bot.command(name='slowmode', aliases=['sm', 'modo_lento'])
@commands.check(es_admin)
async def slowmode(ctx, segundos: int = 0, canal: discord.TextChannel = None):
    canal = canal or ctx.channel
    if not 0 <= segundos <= 21600:
        return await ctx.send('❌ Entre 0 y 21600 segundos.')
    await canal.edit(slowmode_delay=segundos)
    msg = f'✅ Slowmode en {canal.mention}: **{segundos}s**' if segundos else f'✅ Slowmode desactivado en {canal.mention}.'
    await ctx.send(msg)


@bot.command(name='lock', aliases=['bloquear'])
@commands.check(es_admin)
async def lock(ctx, canal: discord.TextChannel = None, *, razon: str = 'Sin razón'):
    canal = canal or ctx.channel
    ow    = canal.overwrites_for(ctx.guild.default_role)
    ow.send_messages = False
    await canal.set_permissions(ctx.guild.default_role, overwrite=ow)
    await ctx.send(f'🔒 {canal.mention} **bloqueado** — {razon}')


@bot.command(name='unlock', aliases=['desbloquear'])
@commands.check(es_admin)
async def unlock(ctx, canal: discord.TextChannel = None, *, razon: str = 'Sin razón'):
    canal = canal or ctx.channel
    ow    = canal.overwrites_for(ctx.guild.default_role)
    ow.send_messages = None
    await canal.set_permissions(ctx.guild.default_role, overwrite=ow)
    await ctx.send(f'🔓 {canal.mention} **desbloqueado** — {razon}')


@bot.command(name='lockall', aliases=['bloquear_todo'])
@commands.check(es_admin)
async def lockall(ctx, *, razon: str = 'Sin razón'):
    count = 0
    for canal in ctx.guild.text_channels:
        ow = canal.overwrites_for(ctx.guild.default_role)
        ow.send_messages = False
        try:
            await canal.set_permissions(ctx.guild.default_role, overwrite=ow)
            count += 1
        except Exception:
            pass
    await ctx.send(f'🔒 **{count}** canales bloqueados — {razon}')


@bot.command(name='unlockall', aliases=['desbloquear_todo'])
@commands.check(es_admin)
async def unlockall(ctx, *, razon: str = 'Sin razón'):
    count = 0
    for canal in ctx.guild.text_channels:
        ow = canal.overwrites_for(ctx.guild.default_role)
        ow.send_messages = None
        try:
            await canal.set_permissions(ctx.guild.default_role, overwrite=ow)
            count += 1
        except Exception:
            pass
    await ctx.send(f'🔓 **{count}** canales desbloqueados — {razon}')


@bot.command(name='hide', aliases=['ocultar'])
@commands.check(es_admin)
async def hide(ctx, canal: discord.TextChannel = None):
    canal = canal or ctx.channel
    ow    = canal.overwrites_for(ctx.guild.default_role)
    ow.view_channel = False
    await canal.set_permissions(ctx.guild.default_role, overwrite=ow)
    await ctx.send(f'🙈 {canal.mention} **ocultado**.')


@bot.command(name='show', aliases=['mostrar'])
@commands.check(es_admin)
async def show(ctx, canal: discord.TextChannel = None):
    canal = canal or ctx.channel
    ow    = canal.overwrites_for(ctx.guild.default_role)
    ow.view_channel = None
    await canal.set_permissions(ctx.guild.default_role, overwrite=ow)
    await ctx.send(f'👁️ {canal.mention} **visible**.')


@bot.command(name='topic', aliases=['tema'])
@commands.check(es_admin)
async def topic(ctx, *, texto: str):
    await ctx.channel.edit(topic=texto)
    await ctx.send(f'✅ Tema: **{texto}**')


@bot.command(name='rename_canal', aliases=['rc'])
@commands.check(es_admin)
async def rename_canal(ctx, *, nombre: str):
    nombre = nombre.lower().replace(' ', '-')
    viejo  = ctx.channel.name
    await ctx.channel.edit(name=nombre)
    await ctx.send(f'✅ **#{viejo}** → **#{nombre}**')


@bot.command(name='crear_canal', aliases=['cc'])
@commands.check(es_admin)
async def crear_canal(ctx, *, nombre: str):
    nombre = nombre.lower().replace(' ', '-')
    c = await ctx.guild.create_text_channel(nombre, reason=f'Creado por {ctx.author}')
    await ctx.send(f'✅ Canal: {c.mention}')


@bot.command(name='eliminar_canal', aliases=['ec'])
@commands.check(es_admin)
async def eliminar_canal(ctx, canal: discord.TextChannel = None):
    canal  = canal or ctx.channel
    nombre = canal.name
    await canal.delete(reason=f'Eliminado por {ctx.author}')
    if canal != ctx.channel:
        await ctx.send(f'🗑️ **#{nombre}** eliminado.')


@bot.command(name='clonar_canal', aliases=['clone'])
@commands.check(es_admin)
async def clonar_canal(ctx, canal: discord.TextChannel = None):
    canal = canal or ctx.channel
    nuevo = await canal.clone(reason=f'Clonado por {ctx.author}')
    await ctx.send(f'✅ Clonado: {nuevo.mention}')


@bot.command(name='nsfw')
@commands.check(es_admin)
async def nsfw_toggle(ctx, canal: discord.TextChannel = None):
    canal = canal or ctx.channel
    nuevo = not canal.is_nsfw()
    await canal.edit(nsfw=nuevo)
    await ctx.send(f'NSFW **{"activado 🔞" if nuevo else "desactivado ✅"}** en {canal.mention}.')


# ─── Roles ──────────────────────────────────────────────────────────────────

@bot.command(name='dar_rol', aliases=['dr', 'addrole'])
@commands.check(es_admin)
async def dar_rol(ctx, member: discord.Member, *, nombre_rol: str):
    rol = discord.utils.find(lambda r: r.name.lower() == nombre_rol.lower(), ctx.guild.roles)
    if not rol:
        similares = [r.name for r in ctx.guild.roles if nombre_rol.lower() in r.name.lower()][:5]
        msg = f'❌ No encontré `{nombre_rol}`.'
        if similares:
            msg += f'\n¿Quisiste decir? {", ".join(f"`{s}`" for s in similares)}'
        return await ctx.send(msg)
    if rol >= ctx.guild.me.top_role:
        return await ctx.send('❌ Ese rol está por encima del mío en la jerarquía.')
    if rol in member.roles:
        return await ctx.send(f'⚠️ {member.mention} ya tiene **{rol.name}**.')
    await member.add_roles(rol, reason=f'Dado por {ctx.author}')
    embed = discord.Embed(title='✅ Rol Dado', color=rol.color)
    embed.add_field(name='Usuario', value=member.mention, inline=True)
    embed.add_field(name='Rol', value=rol.mention, inline=True)
    await ctx.send(embed=embed)


@bot.command(name='r')
@commands.check(es_admin)
async def r_cmd(ctx, accion: str, *, nombre_rol: str):
    """Dar rol por ID o crear un rol nuevo.
    Uso: ,r <id_usuario> <nombre del rol>
         ,r create <nombre del rol>"""

    # ── MODO CREATE ──────────────────────────────────────────────────────────
    if accion.lower() == 'create':
        existente = discord.utils.find(lambda r: r.name.lower() == nombre_rol.lower(), ctx.guild.roles)
        if existente:
            return await ctx.send(f'⚠️ Ya existe el rol **{existente.name}** ({existente.mention}).')
        try:
            nuevo_rol = await ctx.guild.create_role(
                name=nombre_rol,
                reason=f'[,r create] Creado por {ctx.author}',
            )
        except discord.Forbidden:
            return await ctx.send('❌ No tengo permisos para crear roles.')
        embed = discord.Embed(title='✅ Rol Creado', color=nuevo_rol.color)
        embed.add_field(name='🎭 Rol',   value=nuevo_rol.mention,    inline=True)
        embed.add_field(name='🆔 ID',    value=f'`{nuevo_rol.id}`',  inline=True)
        embed.add_field(name='🛡️ Por',   value=ctx.author.mention,   inline=True)
        embed.set_footer(text='Usa ,r <id_usuario> para asignarlo, o edítalo desde el servidor.')
        return await ctx.send(embed=embed)

    # ── MODO DAR ROL POR ID ───────────────────────────────────────────────────
    try:
        user_id = int(accion)
    except ValueError:
        return await ctx.send(
            f'❌ Uso correcto:\n'
            f'• `,r <id_usuario> <rol>` — dar rol por ID\n'
            f'• `,r create <nombre>` — crear un rol nuevo'
        )

    member = ctx.guild.get_member(user_id)
    if not member:
        try:
            member = await ctx.guild.fetch_member(user_id)
        except discord.NotFound:
            return await ctx.send(f'❌ No encontré ningún miembro con la ID `{user_id}` en este servidor.')
        except discord.Forbidden:
            return await ctx.send('❌ No tengo permisos para buscar ese miembro.')

    # buscar rol (exacto primero, luego parcial)
    rol = discord.utils.find(lambda r: r.name.lower() == nombre_rol.lower(), ctx.guild.roles)
    if not rol:
        similares = [r for r in ctx.guild.roles if nombre_rol.lower() in r.name.lower()]
        if len(similares) == 1:
            rol = similares[0]
        elif similares:
            lista = ', '.join(f'`{r.name}`' for r in similares[:5])
            return await ctx.send(f'❌ Rol ambiguo. ¿Quisiste decir? {lista}')
        else:
            return await ctx.send(f'❌ No encontré el rol `{nombre_rol}`.')

    if rol >= ctx.guild.me.top_role:
        return await ctx.send('❌ Ese rol está por encima del mío en la jerarquía.')
    if rol in member.roles:
        return await ctx.send(f'⚠️ {member.mention} ya tiene **{rol.name}**.')

    await member.add_roles(rol, reason=f'[,r] Dado por {ctx.author}')
    embed = discord.Embed(title='✅ Rol Dado', color=rol.color)
    embed.add_field(name='👤 Usuario', value=f'{member.mention} (`{member.id}`)', inline=True)
    embed.add_field(name='🎭 Rol',     value=rol.mention,                          inline=True)
    embed.add_field(name='🛡️ Por',     value=ctx.author.mention,                   inline=True)
    await ctx.send(embed=embed)



@bot.command(name='quitar_rol', aliases=['qr', 'removerole'])
@commands.check(es_admin)
async def quitar_rol(ctx, member: discord.Member, *, nombre_rol: str):
    rol = discord.utils.find(lambda r: r.name.lower() == nombre_rol.lower(), ctx.guild.roles)
    if not rol:
        return await ctx.send(f'❌ No encontré `{nombre_rol}`.')
    if rol >= ctx.guild.me.top_role:
        return await ctx.send('❌ Ese rol está por encima del mío.')
    if rol not in member.roles:
        return await ctx.send(f'⚠️ {member.mention} no tiene **{rol.name}**.')
    await member.remove_roles(rol, reason=f'Quitado por {ctx.author}')
    embed = discord.Embed(title='✅ Rol Quitado', color=0xFF4444)
    embed.add_field(name='Usuario', value=member.mention, inline=True)
    embed.add_field(name='Rol', value=rol.name, inline=True)
    await ctx.send(embed=embed)


@bot.command(name='crear_rol', aliases=['cr'])
@commands.check(es_admin)
async def crear_rol(ctx, color: str = '#99AAB5', *, nombre: str):
    try:
        color_obj = discord.Color.from_str(color)
    except Exception:
        return await ctx.send('❌ Color inválido. Usa `#RRGGBB`.')
    rol = await ctx.guild.create_role(name=nombre, color=color_obj, reason=f'Creado por {ctx.author}')
    await ctx.send(f'✅ Rol {rol.mention} creado.')


@bot.command(name='eliminar_rol', aliases=['er'])
@commands.check(es_admin)
async def eliminar_rol(ctx, *, nombre_rol: str):
    rol = discord.utils.find(lambda r: r.name.lower() == nombre_rol.lower(), ctx.guild.roles)
    if not rol:
        return await ctx.send(f'❌ No encontré `{nombre_rol}`.')
    await rol.delete(reason=f'Eliminado por {ctx.author}')
    await ctx.send(f'🗑️ Rol **{nombre_rol}** eliminado.')


@bot.command(name='roles_usuario', aliases=['ru'])
async def roles_usuario(ctx, member: discord.Member = None):
    member = member or ctx.author
    roles  = [r.mention for r in reversed(member.roles) if r != ctx.guild.default_role]
    embed  = discord.Embed(title=f'🎭 Roles de {member.display_name}', color=member.color)
    embed.description = ' '.join(roles) if roles else 'Sin roles'
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name='listar_roles', aliases=['lroles', 'roles'])
async def listar_roles(ctx):
    roles = [r for r in reversed(ctx.guild.roles) if r != ctx.guild.default_role]
    if not roles:
        return await ctx.send('❌ Sin roles.')
    paginas, chunk = [], ''
    for r in roles:
        linea = f'{r.mention} — `{r.id}` — {len(r.members)} miembros\n'
        if len(chunk) + len(linea) > 900:
            paginas.append(chunk)
            chunk = ''
        chunk += linea
    if chunk:
        paginas.append(chunk)
    for i, p in enumerate(paginas, 1):
        embed = discord.Embed(title=f'🎭 Roles ({i}/{len(paginas)})', description=p, color=0x5865F2)
        await ctx.send(embed=embed)


@bot.command(name='nick', aliases=['apodo'])
@commands.check(es_admin)
async def nick(ctx, member: discord.Member, *, nuevo: str = None):
    try:
        viejo = member.display_name
        await member.edit(nick=nuevo)
        if nuevo:
            await ctx.send(f'✅ Nick: **{viejo}** → **{nuevo}**')
        else:
            await ctx.send(f'✅ Nick de {member.mention} restablecido.')
    except discord.Forbidden:
        await ctx.send('❌ Sin permisos para cambiar ese nick.')


@bot.command(name='massnick')
@commands.check(es_admin)
async def massnick(ctx, *, nuevo: str):
    msg   = await ctx.send(f'⏳ Cambiando nicks...')
    count = 0
    for m in ctx.guild.members:
        if not m.bot:
            try:
                await m.edit(nick=nuevo)
                count += 1
            except Exception:
                pass
    await msg.edit(content=f'✅ Nick **{nuevo}** en **{count}** miembros.')


@bot.command(name='fn')
@commands.check(es_admin)
async def fn(ctx, member: discord.Member, *, apodo: str):
    """Fuerza un apodo permanente a un usuario. Si intenta cambiarlo, el bot lo restaura.
    Uso: ,fn @usuario apodo"""
    try:
        await member.edit(nick=apodo, reason=f'[FN] Apodo forzado por {ctx.author}')
    except discord.Forbidden:
        return await ctx.send('❌ No tengo permisos para cambiar ese nick (¿tiene un rol superior al mío?).')
    gid = str(ctx.guild.id)
    uid = str(member.id)
    _fn_forzados.setdefault(gid, {})[uid] = apodo
    embed = discord.Embed(title='📌 Apodo Forzado', color=0xFF8C00)
    embed.add_field(name='👤 Usuario',  value=member.mention,  inline=True)
    embed.add_field(name='📝 Apodo',    value=f'**{apodo}**',  inline=True)
    embed.add_field(name='🛡️ Por',      value=ctx.author.mention, inline=True)
    embed.set_footer(text='El bot restaurará el apodo si el usuario intenta cambiarlo.')
    await ctx.send(embed=embed)


@bot.command(name='unfn')
@commands.check(es_admin)
async def unfn(ctx, member: discord.Member):
    """Libera el apodo forzado de un usuario.
    Uso: ,unfn @usuario"""
    gid = str(ctx.guild.id)
    uid = str(member.id)
    if uid not in _fn_forzados.get(gid, {}):
        return await ctx.send(f'⚠️ {member.mention} no tiene apodo forzado.')
    del _fn_forzados[gid][uid]
    if not _fn_forzados[gid]:
        del _fn_forzados[gid]
    await ctx.send(f'✅ Apodo forzado de {member.mention} **liberado**. Ya puede cambiar su nick.')


@bot.command(name='fnlist')
@commands.check(es_admin)
async def fnlist(ctx):
    """Muestra todos los usuarios con apodo forzado en el servidor."""
    gid  = str(ctx.guild.id)
    data = _fn_forzados.get(gid, {})
    if not data:
        return await ctx.send('✅ No hay usuarios con apodo forzado.')
    embed = discord.Embed(title='📌 Apodos Forzados', color=0xFF8C00)
    for uid, apodo in data.items():
        m = ctx.guild.get_member(int(uid))
        nombre = m.mention if m else f'<@{uid}>'
        embed.add_field(name=nombre, value=f'`{apodo}`', inline=True)
    embed.set_footer(text=f'{len(data)} usuario(s) con apodo forzado')
    await ctx.send(embed=embed)


@bot.command(name='anuncio', aliases=['ann'])
@commands.check(es_admin)
async def anuncio(ctx, canal: discord.TextChannel = None, *, mensaje: str):
    canal = canal or ctx.channel
    embed = discord.Embed(
        title='📢 Anuncio', description=mensaje,
        color=0xFFD700, timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name=ctx.guild.name, icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
    await canal.send('@everyone', embed=embed)
    if canal != ctx.channel:
        await ctx.send(f'✅ Anuncio en {canal.mention}.')


@bot.command(name='embed_msg', aliases=['emb'])
@commands.check(es_admin)
async def embed_msg(ctx, canal: discord.TextChannel = None, titulo: str = 'Mensaje', *, mensaje: str):
    canal = canal or ctx.channel
    embed = discord.Embed(
        title=titulo, description=mensaje,
        color=0x5865F2, timestamp=datetime.now(timezone.utc),
    )
    await canal.send(embed=embed)
    if canal != ctx.channel:
        await ctx.send(f'✅ Embed en {canal.mention}.')


# ─── Acceso rápido (,v) ──────────────────────────────────────────────────────

class BuscarRolModal(discord.ui.Modal):
    def __init__(self, tipo: str, view):
        super().__init__(title=f"{'🟢 Rol a DAR' if tipo == 'dar' else '🔴 Rol a QUITAR'}")
        self.tipo        = tipo
        self.parent_view = view
        self.input       = discord.ui.TextInput(
            label='Nombre del rol', placeholder='Ej: Members, Admin...', required=True, max_length=100)
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction):
        buscar      = self.input.value.lower().strip()
        coincidencias = [
            r for r in interaction.guild.roles
            if buscar in r.name.lower()
            and r != interaction.guild.default_role
            and not r.managed
            and r < interaction.guild.me.top_role
        ]
        if not coincidencias:
            return await interaction.response.send_message('❌ Sin coincidencias.', ephemeral=True)
        rol = coincidencias[0]
        if self.tipo == 'dar':
            self.parent_view.rol_dar_id = rol.id
        else:
            self.parent_view.rol_quitar_id = rol.id
        await interaction.response.send_message(
            f'{"🟢" if self.tipo == "dar" else "🔴"} Rol seleccionado: **{rol.name}**', ephemeral=True)


class VerView(discord.ui.View):
    def __init__(self, ctx, member: discord.Member):
        super().__init__(timeout=60)
        self.ctx            = ctx
        self.member         = member
        self.confirmado     = False
        cfg_srv             = ROLES_POR_SERVIDOR.get(ctx.guild.id, {})
        self.rol_dar_id     = cfg_srv.get('dar')
        self.rol_quitar_id  = cfg_srv.get('quitar', 'ALL') or 'ALL'

        for label, tipo, style, row in [
            ('🟢 Cambiar rol a dar',    'dar',   discord.ButtonStyle.primary,   0),
            ('🔴 Cambiar rol a quitar', 'quitar', discord.ButtonStyle.secondary,  0),
        ]:
            btn = discord.ui.Button(label=label, style=style, row=row)
            btn.callback = functools.partial(self._abrir_modal, tipo=tipo)
            self.add_item(btn)

        btn_ok = discord.ui.Button(label='✅ Confirmar', style=discord.ButtonStyle.success, row=1)
        btn_ok.callback = self._confirmar
        self.add_item(btn_ok)
        btn_no = discord.ui.Button(label='❌ Cancelar', style=discord.ButtonStyle.danger, row=1)
        btn_no.callback = self._cancelar
        self.add_item(btn_no)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.ctx.author.id:
            await i.response.send_message('❌ No es tu menú.', ephemeral=True)
            return False
        return True

    async def _abrir_modal(self, interaction: discord.Interaction, tipo: str):
        await interaction.response.send_modal(BuscarRolModal(tipo, self))

    async def _confirmar(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.confirmado = True
        self.stop()

    async def _cancelar(self, interaction: discord.Interaction):
        await interaction.response.send_message('❌ Cancelado.', ephemeral=True)
        self.stop()


@bot.command(name='v')
@commands.check(es_admin)
async def dar_acceso(ctx, member: discord.Member):
    embed = discord.Embed(
        title='🔑 Dar Acceso',
        description=f'Configurando acceso para {member.mention}',
        color=0x5865F2,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    view = VerView(ctx, member)
    msg  = await ctx.send(embed=embed, view=view)
    await view.wait()
    try:
        await msg.delete()
    except Exception:
        pass
    if not view.confirmado:
        return
    rol_dar = ctx.guild.get_role(view.rol_dar_id) if view.rol_dar_id else None
    if not rol_dar:
        return await ctx.send('❌ No hay rol a dar configurado.')
    # quitar roles anteriores
    if view.rol_quitar_id == 'ALL':
        roles_a_quitar = [
            r for r in member.roles
            if r != ctx.guild.default_role and not r.managed and r < ctx.guild.me.top_role and r.id != rol_dar.id
        ]
    else:
        r = ctx.guild.get_role(view.rol_quitar_id)
        roles_a_quitar = [r] if r and r in member.roles else []
    try:
        if roles_a_quitar:
            await member.remove_roles(*roles_a_quitar, reason=f',v — {ctx.author}')
        await member.add_roles(rol_dar, reason=f',v — acceso por {ctx.author}')
    except discord.Forbidden:
        return await ctx.send('❌ Sin permisos suficientes.')
    embed_ok = discord.Embed(title='✅ Acceso Concedido', color=0x00FF00)
    embed_ok.add_field(name='Miembro', value=member.mention, inline=True)
    embed_ok.add_field(name='Rol dado', value=rol_dar.mention, inline=True)
    embed_ok.add_field(name='Por', value=ctx.author.mention, inline=True)
    msg_ok = await ctx.send(embed=embed_ok)
    await asyncio.sleep(15)
    try:
        await msg_ok.delete()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# ────────── INFO / UTILIDADES ──────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name='userinfo', aliases=['ui', 'whois', 'user'])
async def userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    roles  = [r.mention for r in member.roles if r != ctx.guild.default_role]
    perms  = [n.replace('_', ' ').title() for n, v in member.guild_permissions if v]
    embed  = discord.Embed(title=f'👤 {member}', color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name='🆔 ID',          value=member.id,                                     inline=True)
    embed.add_field(name='📅 Cuenta',       value=f'<t:{int(member.created_at.timestamp())}:R>', inline=True)
    embed.add_field(name='📥 Se unió',      value=f'<t:{int(member.joined_at.timestamp())}:R>',  inline=True)
    embed.add_field(name='🎨 Color',        value=str(member.color),                             inline=True)
    embed.add_field(name='🤖 Bot',          value='Sí' if member.bot else 'No',                  inline=True)
    embed.add_field(name='💎 Boost',        value='Sí' if member.premium_since else 'No',        inline=True)
    embed.add_field(name=f'🏆 Roles ({len(roles)})', value=' '.join(roles[:10]) or 'Sin roles', inline=False)
    await ctx.send(embed=embed)


@bot.command(name='serverinfo', aliases=['si', 'servidor', 'server'])
async def serverinfo(ctx):
    g      = ctx.guild
    bots   = sum(1 for m in g.members if m.bot)
    humanos = g.member_count - bots
    embed  = discord.Embed(title=f'🏠 {g.name}', color=0x5865F2)
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    if g.banner:
        embed.set_image(url=g.banner.url)
    embed.add_field(name='🆔 ID',        value=g.id,                                      inline=True)
    embed.add_field(name='👑 Owner',     value=g.owner.mention,                            inline=True)
    embed.add_field(name='📅 Creado',    value=f'<t:{int(g.created_at.timestamp())}:R>',   inline=True)
    embed.add_field(name='👥 Miembros',  value=g.member_count,                             inline=True)
    embed.add_field(name='🧑 Humanos',   value=humanos,                                    inline=True)
    embed.add_field(name='🤖 Bots',      value=bots,                                       inline=True)
    embed.add_field(name='💬 Canales',   value=len(g.channels),                            inline=True)
    embed.add_field(name='🎭 Roles',     value=len(g.roles),                               inline=True)
    embed.add_field(name='💎 Boosts',    value=g.premium_subscription_count,               inline=True)
    embed.add_field(name='📢 Nivel verificación', value=str(g.verification_level), inline=True)
    embed.add_field(name='😄 Emojis',   value=len(g.emojis),                               inline=True)
    embed.add_field(name='🔊 Región',    value=str(g.preferred_locale),                    inline=True)
    await ctx.send(embed=embed)


@bot.command(name='ping')
async def ping(ctx):
    lat   = round(bot.latency * 1000)
    color = 0x00FF00 if lat < 100 else 0xFFAA00 if lat < 200 else 0xFF0000
    await ctx.send(embed=discord.Embed(title='🏓 Pong!', description=f'**{lat}ms**', color=color))


@bot.command(name='avatar', aliases=['av', 'foto', 'pfp'])
async def avatar(ctx, member: discord.Member = None):
    member = member or ctx.author
    embed  = discord.Embed(title=f'🖼️ {member.display_name}', color=member.color)
    embed.set_image(url=member.display_avatar.url)
    embed.add_field(name='🔗 Link', value=f'[Descargar]({member.display_avatar.url})', inline=False)
    await ctx.send(embed=embed)


@bot.command(name='banner')
async def banner(ctx, member: discord.Member = None):
    member = member or ctx.author
    user   = await bot.fetch_user(member.id)
    if not user.banner:
        return await ctx.send(f'❌ {member.display_name} no tiene banner.')
    embed  = discord.Embed(title=f'🖼️ Banner de {member.display_name}', color=member.color)
    embed.set_image(url=user.banner.url)
    await ctx.send(embed=embed)


@bot.command(name='stats', aliases=['estadisticas'])
async def stats(ctx):
    g      = ctx.guild
    bots   = sum(1 for m in g.members if m.bot)
    en_linea = sum(1 for m in g.members if m.status != discord.Status.offline and not m.bot)
    embed  = discord.Embed(title=f'📊 {g.name}', color=0x5865F2)
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name='👥 Total',    value=g.member_count, inline=True)
    embed.add_field(name='🧑 Humanos', value=g.member_count - bots, inline=True)
    embed.add_field(name='🤖 Bots',    value=bots, inline=True)
    embed.add_field(name='🟢 En línea', value=en_linea, inline=True)
    embed.add_field(name='💬 Canales', value=len(g.text_channels), inline=True)
    embed.add_field(name='🔊 Voz',     value=len(g.voice_channels), inline=True)
    embed.add_field(name='🎭 Roles',   value=len(g.roles), inline=True)
    embed.add_field(name='😄 Emojis',  value=len(g.emojis), inline=True)
    embed.add_field(name='💎 Boosts',  value=g.premium_subscription_count, inline=True)
    await ctx.send(embed=embed)


@bot.command(name='botinfo', aliases=['bot_info', 'about'])
async def botinfo(ctx):
    embed = discord.Embed(title='🤖 Info del Bot', color=0x5865F2)
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(name='🏷️ Nombre',    value=str(bot.user),          inline=True)
    embed.add_field(name='🆔 ID',        value=bot.user.id,             inline=True)
    embed.add_field(name='🖥️ Python',   value=platform.python_version(), inline=True)
    embed.add_field(name='📚 discord.py', value=discord.__version__,   inline=True)
    embed.add_field(name='🏠 Servidores', value=len(bot.guilds),        inline=True)
    embed.add_field(name='👥 Usuarios',  value=len(bot.users),          inline=True)
    embed.add_field(name='📜 Comandos',  value=len(bot.commands),       inline=True)
    embed.add_field(name='⚙️ Prefijo',   value=f'`{PREFIX}`',           inline=True)
    await ctx.send(embed=embed)


@bot.command(name='invitar', aliases=['invite'])
async def invitar(ctx):
    url   = f'https://discord.com/api/oauth2/authorize?client_id={bot.user.id}&permissions=8&scope=bot%20applications.commands'
    embed = discord.Embed(title='🔗 Invitar Bot', description=f'[Haz clic aquí]({url})', color=0x5865F2)
    await ctx.send(embed=embed)


@bot.command(name='say')
@commands.check(es_admin)
async def say(ctx, *, mensaje: str):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await ctx.send(mensaje)


@bot.command(name='say_canal')
@commands.check(es_admin)
async def say_canal(ctx, canal: discord.TextChannel, *, mensaje: str):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await canal.send(mensaje)


@bot.command(name='setprefix', aliases=['prefix', 'cambiar_prefijo'])
@commands.check(es_owner_o_admin)
async def setprefix(ctx, nuevo: str):
    if len(nuevo) > 3:
        return await ctx.send('❌ Máx 3 caracteres.')
    bot.command_prefix = nuevo
    await ctx.send(f'✅ Prefijo: `{PREFIX}` → `{nuevo}`')


@bot.command(name='clima', aliases=['weather', 'tiempo'])
async def clima(ctx, *, ciudad: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://wttr.in/{ciudad.replace(' ', '+')}?format=j1") as resp:
                if resp.status != 200:
                    return await ctx.send('❌ Ciudad no encontrada.')
                data   = await resp.json()
                actual = data['current_condition'][0]
                embed  = discord.Embed(title=f'🌤️ {ciudad.title()}', color=0x4169E1)
                embed.add_field(name='🌡️ Temperatura',  value=f"{actual['temp_C']}°C",              inline=True)
                embed.add_field(name='🤔 Sensación',    value=f"{actual['FeelsLikeC']}°C",          inline=True)
                embed.add_field(name='💧 Humedad',      value=f"{actual['humidity']}%",             inline=True)
                embed.add_field(name='💨 Viento',       value=f"{actual['windspeedKmph']} km/h",    inline=True)
                embed.add_field(name='☁️ Estado',       value=actual['weatherDesc'][0]['value'],    inline=True)
                embed.add_field(name='👁️ Visibilidad',  value=f"{actual['visibility']} km",         inline=True)
                await ctx.send(embed=embed)
    except Exception:
        await ctx.send('❌ No pude obtener el clima.')


@bot.command(name='traducir', aliases=['translate', 'tr'])
async def traducir(ctx, idioma: str, *, texto: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'https://api.mymemory.translated.net/get?q={texto}&langpair=es|{idioma}') as resp:
                data = await resp.json()
                trad = data['responseData']['translatedText']
                embed = discord.Embed(title='🌍 Traducción', color=0x00CED1)
                embed.add_field(name='📝 Original',  value=texto, inline=False)
                embed.add_field(name='✅ Traducido', value=trad,  inline=False)
                await ctx.send(embed=embed)
    except Exception:
        await ctx.send('❌ No pude traducir.')


@bot.command(name='calcular', aliases=['calc', 'matematica'])
async def calcular(ctx, *, expresion: str):
    try:
        if not all(c in '0123456789+-*/.() %' for c in expresion):
            return await ctx.send('❌ Solo números y operadores `+ - * / ( ) %`.')
        resultado = eval(expresion)  # noqa: S307
        embed = discord.Embed(title='🧮 Calculadora', color=0x00FF00)
        embed.add_field(name='📝 Expresión', value=f'`{expresion}`', inline=False)
        embed.add_field(name='✅ Resultado', value=f'**{resultado}**', inline=False)
        await ctx.send(embed=embed)
    except ZeroDivisionError:
        await ctx.send('❌ División entre cero.')
    except Exception:
        await ctx.send('❌ Expresión inválida.')


@bot.command(name='color')
async def color_cmd(ctx, *, hex_color: str):
    hex_color = hex_color.strip('#')
    try:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
    except Exception:
        return await ctx.send('❌ Usa `,color FF0000`')
    embed = discord.Embed(title=f'🎨 #{hex_color.upper()}', color=int(hex_color, 16))
    embed.add_field(name='R', value=r, inline=True)
    embed.add_field(name='G', value=g, inline=True)
    embed.add_field(name='B', value=b, inline=True)
    embed.set_thumbnail(url=f'https://singlecolorimage.com/get/{hex_color}/100x100')
    await ctx.send(embed=embed)


@bot.command(name='buscar', aliases=['google', 'search'])
async def buscar(ctx, *, termino: str):
    url   = f"https://www.google.com/search?q={termino.replace(' ', '+')}"
    embed = discord.Embed(
        title=f'🔍 {termino}',
        description=f'[Buscar en Google]({url})',
        color=0x4285F4,
    )
    await ctx.send(embed=embed)


@bot.command(name='rng', aliases=['random', 'aleatorio'])
async def rng(ctx, minimo: int = 1, maximo: int = 100):
    if minimo >= maximo:
        return await ctx.send('❌ El mínimo debe ser menor que el máximo.')
    resultado = random.randint(minimo, maximo)
    embed = discord.Embed(title='🎲 Número Aleatorio', color=0x5865F2)
    embed.add_field(name='Rango',     value=f'`{minimo}` – `{maximo}`', inline=True)
    embed.add_field(name='Resultado', value=f'**{resultado}**',          inline=True)
    await ctx.send(embed=embed)


@bot.command(name='sugerencia', aliases=['suggest'])
async def sugerencia(ctx, canal: discord.TextChannel = None, *, texto: str):
    canal = canal or ctx.channel
    embed = discord.Embed(
        title='💡 Sugerencia', description=texto,
        color=0xFFD700, timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    msg = await canal.send(embed=embed)
    await msg.add_reaction('✅')
    await msg.add_reaction('❌')
    if canal != ctx.channel:
        await ctx.send(f'✅ Enviada en {canal.mention}.')


@bot.command(name='reporte', aliases=['report'])
async def reporte(ctx, member: discord.Member, *, razon: str):
    if member == ctx.author:
        return await ctx.send('❌ No puedes reportarte.')
    embed = discord.Embed(title='🚨 Reporte', color=0xFF0000, timestamp=datetime.now(timezone.utc))
    embed.add_field(name='Reportado', value=f'{member.mention} (`{member.id}`)', inline=False)
    embed.add_field(name='Razón',     value=razon,                               inline=False)
    embed.add_field(name='Por',       value=ctx.author.mention,                  inline=False)
    embed.add_field(name='Canal',     value=ctx.channel.mention,                 inline=False)
    cfg   = cargar_antinuke(ctx.guild.id)
    ch_id = cfg.get('log_channel')
    destino = ctx.guild.get_channel(int(ch_id)) if ch_id else ctx.channel
    await destino.send(embed=embed)
    try:
        await ctx.message.delete()
    except Exception:
        pass
    try:
        await ctx.author.send(f'✅ Reporte sobre **{member.display_name}** enviado.')
    except Exception:
        pass


# ─── Recordatorios y cumpleaños ─────────────────────────────────────────────

@bot.command(name='recordar', aliases=['remind', 'reminder'])
async def recordar(ctx, tiempo: str, *, mensaje: str):
    unidades = {'s': 1, 'm': 60, 'h': 3600}
    try:
        u = tiempo[-1].lower()
        n = int(tiempo[:-1])
        if u not in unidades or not 1 <= n:
            raise ValueError
    except Exception:
        return await ctx.send('❌ Ej: `,recordar 10m mensaje` (s/m/h)')
    segundos = n * unidades[u]
    nombres  = {'s': 'segundo(s)', 'm': 'minuto(s)', 'h': 'hora(s)'}
    await ctx.send(f'⏰ Te recordaré en **{n} {nombres[u]}**.')
    await asyncio.sleep(segundos)
    embed = discord.Embed(
        title='⏰ Recordatorio', description=mensaje,
        color=0xFF8800, timestamp=datetime.now(timezone.utc),
    )
    try:
        await ctx.author.send(embed=embed)
    except Exception:
        pass
    await ctx.send(f'⏰ {ctx.author.mention} ¡Recordatorio! **{mensaje}**')


def cargar_cumples() -> dict:
    if os.path.exists(CUMPLE_FILE):
        with open(CUMPLE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def guardar_cumples(data: dict):
    with open(CUMPLE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


@bot.command(name='cumple', aliases=['birthday'])
async def cumple(ctx, fecha: str = None):
    cumples = cargar_cumples()
    uid     = str(ctx.author.id)
    if fecha is None:
        if uid in cumples:
            return await ctx.send(f'🎂 Tu cumpleaños: **{cumples[uid]}**.')
        return await ctx.send('❌ No tienes cumpleaños registrado. Usa `,cumple DD/MM`.')
    try:
        dia, mes = map(int, fecha.split('/'))
        if not (1 <= dia <= 31 and 1 <= mes <= 12):
            raise ValueError
    except Exception:
        return await ctx.send('❌ Usa `DD/MM`. Ej: `,cumple 25/12`')
    cumples[uid] = f'{dia:02d}/{mes:02d}'
    guardar_cumples(cumples)
    await ctx.send(f'🎂 Cumpleaños registrado: **{dia:02d}/{mes:02d}**')


@bot.command(name='cumple_ver', aliases=['ver_cumple'])
async def cumple_ver(ctx, member: discord.Member = None):
    member  = member or ctx.author
    cumples = cargar_cumples()
    uid     = str(member.id)
    if uid not in cumples:
        return await ctx.send(f'❌ {member.display_name} sin cumpleaños registrado.')
    fecha    = cumples[uid]
    dia, mes = map(int, fecha.split('/'))
    hoy      = datetime.now(timezone.utc)
    este     = datetime(hoy.year, mes, dia, tzinfo=timezone.utc)
    if este < hoy:
        este = datetime(hoy.year + 1, mes, dia, tzinfo=timezone.utc)
    dias = (este - hoy).days
    embed = discord.Embed(title=f'🎂 {member.display_name}', color=0xFFD700)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name='📅 Fecha',  value=fecha,              inline=True)
    embed.add_field(name='⏰ Faltan', value=f'**{dias}** días', inline=True)
    await ctx.send(embed=embed)


@bot.command(name='cumples_lista', aliases=['lista_cumples'])
async def cumples_lista(ctx):
    cumples = cargar_cumples()
    if not cumples:
        return await ctx.send('❌ Nadie ha registrado su cumpleaños.')
    hoy   = datetime.now(timezone.utc)
    lista = []
    for uid, fecha in cumples.items():
        try:
            dia, mes = map(int, fecha.split('/'))
            este      = datetime(hoy.year, mes, dia, tzinfo=timezone.utc)
            if este < hoy:
                este = datetime(hoy.year + 1, mes, dia, tzinfo=timezone.utc)
            lista.append(((este - hoy).days, uid, fecha))
        except Exception:
            pass
    lista.sort()
    embed = discord.Embed(title='🎂 Próximos Cumpleaños', color=0xFFD700)
    for dias, uid, fecha in lista[:10]:
        m     = ctx.guild.get_member(int(uid))
        nombre = m.display_name if m else f'<@{uid}>'
        embed.add_field(name=f'🎉 {nombre}', value=f'**{fecha}** — en {dias} días', inline=False)
    await ctx.send(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
# ────────── JUEGOS ─────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name='trivia')
async def trivia(ctx):
    preguntas = [
        ('¿Cuál es el planeta más grande del sistema solar?', ['Júpiter', 'Saturno', 'Neptuno', 'Urano'], 0),
        ('¿Cuántos huesos tiene el cuerpo humano adulto?', ['206', '208', '198', '212'], 0),
        ('¿Qué país tiene más habitantes?', ['China', 'India', 'EEUU', 'Indonesia'], 0),
        ('¿Cuál es el océano más grande?', ['Pacífico', 'Atlántico', 'Índico', 'Ártico'], 0),
        ('¿En qué año llegó el hombre a la Luna?', ['1969', '1971', '1965', '1975'], 0),
        ('¿Cuál es el elemento más abundante en el universo?', ['Hidrógeno', 'Helio', 'Oxígeno', 'Carbono'], 0),
        ('¿Cuántos lados tiene un hexágono?', ['6', '5', '7', '8'], 0),
        ('¿Cuál es el río más largo del mundo?', ['Nilo', 'Amazonas', 'Yangtsé', 'Misisipi'], 0),
    ]
    pregunta, opciones, correcto = random.choice(preguntas)
    indices = list(range(len(opciones)))
    random.shuffle(indices)
    opciones_s = [opciones[i] for i in indices]
    idx_correcto = opciones_s.index(opciones[correcto])
    emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣']
    desc   = '\n'.join(f'{emojis[i]} {op}' for i, op in enumerate(opciones_s))
    embed  = discord.Embed(title='❓ Trivia', description=f'**{pregunta}**\n\n{desc}', color=0x5865F2)
    embed.set_footer(text='Responde con el emoji correcto — 30 segundos')
    msg = await ctx.send(embed=embed)
    for e in emojis[:len(opciones_s)]:
        await msg.add_reaction(e)

    def check(r, u):
        return u != bot.user and r.message.id == msg.id and str(r.emoji) in emojis

    try:
        reaction, user = await bot.wait_for('reaction_add', timeout=30, check=check)
        elegido = emojis.index(str(reaction.emoji))
        if elegido == idx_correcto:
            await ctx.send(f'✅ ¡**{user.display_name}** acertó! Era **{opciones[correcto]}** 🎉')
        else:
            await ctx.send(f'❌ **{user.display_name}** falló. Era **{opciones[correcto]}**.')
    except asyncio.TimeoutError:
        await ctx.send(f'⌛ Tiempo. Era **{opciones[correcto]}**.')


@bot.command(name='adivina', aliases=['guess', 'numero'])
async def adivina_numero(ctx, maximo: int = 100):
    if not 10 <= maximo <= 10000:
        return await ctx.send('❌ Entre 10 y 10000.')
    numero  = random.randint(1, maximo)
    intentos = 0
    await ctx.send(f'🎯 Adivina el número del **1** al **{maximo}**. Tienes **7 intentos**.')

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    for _ in range(7):
        try:
            msg = await bot.wait_for('message', timeout=30, check=check)
            intentos += 1
            try:
                guess = int(msg.content)
            except ValueError:
                await ctx.send('❌ Escribe un número.')
                continue
            if guess == numero:
                return await ctx.send(f'🎉 ¡**{ctx.author.display_name}** acertó en **{intentos}** intentos!')
            elif guess < numero:
                await ctx.send(f'📈 Más **alto**. ({7 - intentos} restantes)')
            else:
                await ctx.send(f'📉 Más **bajo**. ({7 - intentos} restantes)')
        except asyncio.TimeoutError:
            return await ctx.send(f'⌛ Tiempo. Era **{numero}**.')
    await ctx.send(f'💀 Sin más intentos. Era **{numero}**.')


@bot.command(name='dado', aliases=['dice', 'd6'])
async def dado(ctx, lados: int = 6):
    if not 2 <= lados <= 1000:
        return await ctx.send('❌ Entre 2 y 1000.')
    resultado = random.randint(1, lados)
    embed = discord.Embed(title='🎲 Dado', color=0x5865F2)
    embed.add_field(name=f'D{lados}', value=f'**{resultado}**', inline=True)
    await ctx.send(embed=embed)


@bot.command(name='dado_personalizado', aliases=['dp'])
async def dado_personalizado(ctx, cantidad: int = 1, lados: int = 6):
    if not 1 <= cantidad <= 20:
        return await ctx.send('❌ Entre 1 y 20 dados.')
    if not 2 <= lados <= 1000:
        return await ctx.send('❌ Entre 2 y 1000 lados.')
    resultados = [random.randint(1, lados) for _ in range(cantidad)]
    total = sum(resultados)
    embed = discord.Embed(title=f'🎲 {cantidad}d{lados}', color=0x5865F2)
    embed.add_field(name='Resultados', value=' + '.join(f'`{r}`' for r in resultados), inline=False)
    embed.add_field(name='Total',      value=f'**{total}**',                            inline=True)
    if cantidad > 1:
        embed.add_field(name='Promedio', value=f'**{total / cantidad:.1f}**', inline=True)
    await ctx.send(embed=embed)


@bot.command(name='moneda', aliases=['coin', 'flip'])
async def moneda(ctx):
    resultado = random.choice(['🪙 Cara', '🪙 Sello'])
    embed = discord.Embed(title='🪙 Moneda', description=f'**{resultado}**', color=0xFFD700)
    await ctx.send(embed=embed)


@bot.command(name='ruleta', aliases=['roulette'])
async def ruleta(ctx, *opciones):
    if len(opciones) < 2:
        return await ctx.send('❌ Al menos 2 opciones. Ej: `,ruleta A B C`')
    elegida = random.choice(opciones)
    embed = discord.Embed(title='🎡 Ruleta', color=0xFF4444)
    embed.add_field(name='Opciones', value=' | '.join(f'`{o}`' for o in opciones), inline=False)
    embed.add_field(name='🏆 Elegida', value=f'**{elegida}**', inline=False)
    await ctx.send(embed=embed)


@bot.command(name='8ball', aliases=['bola8'])
async def bola_ocho(ctx, *, pregunta: str):
    respuestas = [
        '✅ Sí, definitivamente.', '✅ Todo indica que sí.', '✅ Sin duda.',
        '🤔 No está claro.', '🤔 Pregunta de nuevo más tarde.',
        '❌ No cuentes con ello.', '❌ Mi respuesta es no.', '❌ Definitivamente no.',
    ]
    embed = discord.Embed(title='🎱 Bola Mágica', color=0x330066)
    embed.add_field(name='❓ Pregunta',  value=pregunta,                  inline=False)
    embed.add_field(name='🔮 Respuesta', value=random.choice(respuestas), inline=False)
    await ctx.send(embed=embed)


@bot.command(name='piedra', aliases=['rps'])
async def piedra_papel_tijera(ctx, eleccion: str):
    opciones = {'piedra': '🪨', 'papel': '📄', 'tijera': '✂️'}
    eleccion = eleccion.lower()
    if eleccion not in opciones:
        return await ctx.send('❌ Opciones: `piedra`, `papel`, `tijera`')
    bot_e = random.choice(list(opciones.keys()))
    gana  = {'piedra': 'tijera', 'papel': 'piedra', 'tijera': 'papel'}
    if eleccion == bot_e:
        resultado, color = '🤝 Empate', 0xFFAA00
    elif gana[eleccion] == bot_e:
        resultado, color = '🏆 ¡Ganaste!', 0x00FF00
    else:
        resultado, color = '😈 ¡Perdiste!', 0xFF0000
    embed = discord.Embed(title='🎮 Piedra Papel Tijera', description=resultado, color=color)
    embed.add_field(name='Tú',  value=opciones[eleccion], inline=True)
    embed.add_field(name='Bot', value=opciones[bot_e],    inline=True)
    await ctx.send(embed=embed)


@bot.command(name='verdad_o_reto', aliases=['tor'])
async def verdad_o_reto(ctx, member: discord.Member = None):
    member   = member or ctx.author
    verdades = [
        '¿Cuál es tu mayor miedo?', '¿Qué es lo más embarazoso que te ha pasado?',
        '¿Tienes algún crush aquí?', '¿Cuál es tu secreto más oscuro?',
        '¿A quién de aquí considerarías como pareja?',
    ]
    retos = [
        "Cambia tu nick a 'Pollo Frito' por 1 hora.",
        'Manda un meme al canal principal.',
        'Escribe un poema sobre el bot.',
        "Di 'amo a mi bot' 3 veces en el chat.",
    ]
    tipo      = random.choice(['Verdad 🔮', 'Reto 💥'])
    contenido = random.choice(verdades if 'Verdad' in tipo else retos)
    color     = 0x9932CC if 'Verdad' in tipo else 0xFF8C00
    embed = discord.Embed(
        title=f'🎮 {tipo}',
        description=f'Para {member.mention}\n\n**{contenido}**',
        color=color,
    )
    await ctx.send(embed=embed)


@bot.command(name='acertijo', aliases=['riddle'])
async def acertijo(ctx):
    acertijos = [
        ('Tengo ciudades, pero no hay casas. Tengo montañas, pero no hay árboles. ¿Qué soy?', 'Un mapa'),
        ('Cuanto más me seques, más mojado te quedas. ¿Qué soy?', 'Una toalla'),
        ('Tengo manos pero no puedo aplaudir. ¿Qué soy?', 'Un reloj'),
        ('Soy ligero como una pluma, pero ningún hombre puede sostenerme más de minutos. ¿Qué soy?', 'El aliento'),
        ('Siempre delante de ti, pero no se puede ver. ¿Qué soy?', 'El futuro'),
    ]
    pregunta, respuesta = random.choice(acertijos)
    embed = discord.Embed(title='🧩 Acertijo', description=pregunta, color=0x9932CC)
    await ctx.send(embed=embed)

    def check(m):
        return m.channel == ctx.channel and not m.author.bot

    try:
        msg_r = await bot.wait_for('message', timeout=30, check=check)
        if respuesta.lower() in msg_r.content.lower():
            await ctx.send(f'✅ ¡{msg_r.author.mention} acertó! Era **{respuesta}** 🎉')
        else:
            await ctx.send(f'❌ Era **{respuesta}**.')
    except asyncio.TimeoutError:
        await ctx.send(f'⌛ Tiempo. Era **{respuesta}**.')


FRASES_MOTIVACION = [
    'El éxito no es definitivo, el fracaso no es fatal. — Churchill',
    'El único modo de hacer un gran trabajo es amar lo que haces. — Jobs',
    'La vida es 10% lo que te sucede y 90% cómo reaccionas. — Swindoll',
    'Sé el cambio que quieres ver en el mundo. — Gandhi',
    'No esperes oportunidades extraordinarias. Aprovecha las ordinarias.',
    'Cree en ti mismo y todo lo demás vendrá solo.',
]
CHISTES = [
    '¿Por qué los pájaros vuelan al sur? Porque caminar es muy lejos 🐦',
    '¿Qué le dijo el 0 al 8? Bonito cinturón 😂',
    '¿Por qué el libro de matemáticas estaba triste? Porque tenía muchos problemas 📚',
    '¿Qué hace una abeja en el gimnasio? ¡Zum-ba! 🐝',
    '¿Por qué los esqueletos no pelean? No tienen agallas 💀',
]


@bot.command(name='frase', aliases=['motivacion', 'quote'])
async def frase_random(ctx):
    embed = discord.Embed(title='💬 Frase del día', description=f'*{random.choice(FRASES_MOTIVACION)}*', color=0x00CED1)
    await ctx.send(embed=embed)


@bot.command(name='chiste', aliases=['joke'])
async def chiste_random(ctx):
    embed = discord.Embed(title='😂 Chiste', description=random.choice(CHISTES), color=0xFFD700)
    await ctx.send(embed=embed)


@bot.command(name='meme')
async def meme_random(ctx):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get('https://meme-api.com/gimme') as resp:
                if resp.status == 200:
                    data  = await resp.json()
                    embed = discord.Embed(title=data['title'], color=0xFF8C00)
                    embed.set_image(url=data['url'])
                    return await ctx.send(embed=embed)
    except Exception:
        pass
    await ctx.send('❌ No pude obtener un meme.')


@bot.command(name='sorteo', aliases=['giveaway'])
@commands.check(es_staff)
async def sorteo(ctx, segundos: int, *, premio: str):
    if not 10 <= segundos <= 86400:
        return await ctx.send('❌ Entre 10s y 86400s (24h).')
    embed = discord.Embed(
        title='🎁 ¡SORTEO!',
        description=f'**Premio:** {premio}\nReacciona con 🎉\n⏰ **{segundos}s**',
        color=0xFFD700,
    )
    embed.set_footer(text=f'Organizado por {ctx.author.display_name}')
    msg = await ctx.send(embed=embed)
    await msg.add_reaction('🎉')
    await asyncio.sleep(segundos)
    msg         = await ctx.channel.fetch_message(msg.id)
    reaction    = discord.utils.get(msg.reactions, emoji='🎉')
    participantes = [u async for u in reaction.users() if not u.bot]
    if not participantes:
        embed_fin = discord.Embed(title='🎁 Sin participantes 😢', color=0xFF0000)
    else:
        ganador   = random.choice(participantes)
        embed_fin = discord.Embed(
            title='🎉 ¡Ganador!',
            description=f'**Premio:** {premio}\n🏆 {ganador.mention}',
            color=0xFFD700,
        )
    await ctx.send(embed=embed_fin)


@bot.command(name='encuesta', aliases=['poll'])
async def encuesta(ctx, *, texto: str):
    partes = [p.strip() for p in texto.split('|')]
    if len(partes) < 2:
        return await ctx.send('❌ Formato: `,encuesta ¿Pregunta? | op1 | op2`')
    pregunta = partes[0]
    opciones = partes[1:]
    if len(opciones) > 9:
        return await ctx.send('❌ Máximo 9 opciones.')
    nums = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣']
    desc = '\n'.join(f'{nums[i]} {op}' for i, op in enumerate(opciones))
    embed = discord.Embed(title=f'📊 {pregunta}', description=desc, color=0x5865F2)
    msg   = await ctx.send(embed=embed)
    for i in range(len(opciones)):
        await msg.add_reaction(nums[i])


@bot.command(name='encuesta_si_no', aliases=['yesno'])
async def encuesta_si_no(ctx, *, pregunta: str):
    embed = discord.Embed(title=f'📊 {pregunta}', color=0x5865F2)
    msg   = await ctx.send(embed=embed)
    await msg.add_reaction('✅')
    await msg.add_reaction('❌')


# ═══════════════════════════════════════════════════════════════════════════════
# ────────── NUEVOS COMANDOS AL ESTILO GREED ────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Economía básica ────────────────────────────────────────────────────────
ECONOMIA_FILE = 'economia.json'


def cargar_eco() -> dict:
    if os.path.exists(ECONOMIA_FILE):
        with open(ECONOMIA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def guardar_eco(data: dict):
    with open(ECONOMIA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def eco_user(data: dict, uid: str) -> dict:
    data.setdefault(uid, {'balance': 0, 'bank': 0, 'daily_ts': 0, 'work_ts': 0})
    return data[uid]


@bot.command(name='balance', aliases=['bal', 'dinero', 'coins'])
async def balance(ctx, member: discord.Member = None):
    member = member or ctx.author
    data   = cargar_eco()
    u      = eco_user(data, str(member.id))
    embed  = discord.Embed(title=f'💰 Balance de {member.display_name}', color=0xFFD700)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name='💵 Cartera',  value=f'**{u["balance"]:,}** monedas',  inline=True)
    embed.add_field(name='🏦 Banco',    value=f'**{u["bank"]:,}** monedas',     inline=True)
    embed.add_field(name='📊 Total',    value=f'**{u["balance"] + u["bank"]:,}** monedas', inline=True)
    await ctx.send(embed=embed)


@bot.command(name='daily')
async def daily(ctx):
    data  = cargar_eco()
    uid   = str(ctx.author.id)
    u     = eco_user(data, uid)
    ahora = int(time.time())
    if ahora - u['daily_ts'] < 86400:
        restante = 86400 - (ahora - u['daily_ts'])
        h, m = divmod(restante // 60, 60)
        return await ctx.send(f'⏰ Ya reclamaste tu daily. Vuelve en **{h}h {m}m**.')
    cantidad  = random.randint(100, 500)
    u['balance']  += cantidad
    u['daily_ts']  = ahora
    guardar_eco(data)
    embed = discord.Embed(title='💸 Daily', description=f'+**{cantidad}** monedas', color=0x00FF00)
    embed.add_field(name='Total cartera', value=f'{u["balance"]:,}', inline=True)
    await ctx.send(embed=embed)


@bot.command(name='work', aliases=['trabajar'])
async def work(ctx):
    data  = cargar_eco()
    uid   = str(ctx.author.id)
    u     = eco_user(data, uid)
    ahora = int(time.time())
    if ahora - u['work_ts'] < 3600:
        restante = 3600 - (ahora - u['work_ts'])
        m = restante // 60
        return await ctx.send(f'⏰ Ya trabajaste. Descansa **{m}min** más.')
    trabajos = [
        ('💻 Programador', 50, 200),
        ('🎨 Diseñador',   40, 180),
        ('🍕 Repartidor',  30, 150),
        ('🔧 Mecánico',    60, 220),
        ('📚 Tutor',       45, 190),
    ]
    trabajo, mn, mx = random.choice(trabajos)
    cantidad        = random.randint(mn, mx)
    u['balance'] += cantidad
    u['work_ts']  = ahora
    guardar_eco(data)
    embed = discord.Embed(
        title=f'{trabajo}',
        description=f'Ganaste **{cantidad}** monedas trabajando.',
        color=0x00FF00,
    )
    await ctx.send(embed=embed)


@bot.command(name='depositar', aliases=['dep', 'deposit'])
async def depositar(ctx, cantidad: str):
    data = cargar_eco()
    uid  = str(ctx.author.id)
    u    = eco_user(data, uid)
    if cantidad.lower() in ('all', 'todo'):
        cantidad = u['balance']
    else:
        try:
            cantidad = int(cantidad)
        except ValueError:
            return await ctx.send('❌ Escribe una cantidad o `all`.')
    if cantidad <= 0 or cantidad > u['balance']:
        return await ctx.send('❌ No tienes suficientes monedas.')
    u['balance'] -= cantidad
    u['bank']    += cantidad
    guardar_eco(data)
    await ctx.send(f'🏦 Depositaste **{cantidad:,}** monedas. Banco: **{u["bank"]:,}**')


@bot.command(name='retirar', aliases=['withdraw', 'ret'])
async def retirar(ctx, cantidad: str):
    data = cargar_eco()
    uid  = str(ctx.author.id)
    u    = eco_user(data, uid)
    if cantidad.lower() in ('all', 'todo'):
        cantidad = u['bank']
    else:
        try:
            cantidad = int(cantidad)
        except ValueError:
            return await ctx.send('❌ Escribe una cantidad o `all`.')
    if cantidad <= 0 or cantidad > u['bank']:
        return await ctx.send('❌ No tienes suficiente en el banco.')
    u['bank']    -= cantidad
    u['balance'] += cantidad
    guardar_eco(data)
    await ctx.send(f'💵 Retiraste **{cantidad:,}** monedas. Cartera: **{u["balance"]:,}**')


@bot.command(name='transferir', aliases=['pay', 'send_money'])
async def transferir(ctx, member: discord.Member, cantidad: int):
    if member == ctx.author:
        return await ctx.send('❌ No puedes transferirte a ti mismo.')
    if cantidad <= 0:
        return await ctx.send('❌ Cantidad inválida.')
    data = cargar_eco()
    uid  = str(ctx.author.id)
    tid  = str(member.id)
    u    = eco_user(data, uid)
    t    = eco_user(data, tid)
    if cantidad > u['balance']:
        return await ctx.send('❌ Saldo insuficiente.')
    u['balance'] -= cantidad
    t['balance'] += cantidad
    guardar_eco(data)
    embed = discord.Embed(title='💸 Transferencia', color=0x00FF00)
    embed.add_field(name='De',      value=ctx.author.mention, inline=True)
    embed.add_field(name='Para',    value=member.mention,     inline=True)
    embed.add_field(name='Monto',   value=f'{cantidad:,}',    inline=True)
    await ctx.send(embed=embed)


@bot.command(name='leaderboard', aliases=['lb', 'top', 'ranking'])
async def leaderboard(ctx):
    data   = cargar_eco()
    sorted_data = sorted(
        ((uid, u['balance'] + u['bank']) for uid, u in data.items()),
        key=lambda x: x[1], reverse=True,
    )[:10]
    embed = discord.Embed(title='🏆 Top 10 Más Ricos', color=0xFFD700)
    for i, (uid, total) in enumerate(sorted_data, 1):
        m   = ctx.guild.get_member(int(uid))
        nombre = m.display_name if m else f'<@{uid}>'
        medal  = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else f'`{i}.`'
        embed.add_field(name=f'{medal} {nombre}', value=f'**{total:,}** monedas', inline=False)
    await ctx.send(embed=embed)


@bot.command(name='slots', aliases=['tragamonedas'])
async def slots(ctx, apuesta: int = 50):
    data = cargar_eco()
    uid  = str(ctx.author.id)
    u    = eco_user(data, uid)
    if apuesta < 10:
        return await ctx.send('❌ Apuesta mínima: 10 monedas.')
    if apuesta > u['balance']:
        return await ctx.send('❌ Saldo insuficiente.')
    simbolos = ['🍒', '🍋', '🍊', '⭐', '💎', '🔔']
    reels    = [random.choice(simbolos) for _ in range(3)]
    u['balance'] -= apuesta
    if reels[0] == reels[1] == reels[2]:
        multiplicador = 10 if reels[0] == '💎' else 5
        ganancia      = apuesta * multiplicador
        u['balance'] += ganancia
        resultado = f'🎉 ¡**JACKPOT**! +**{ganancia:,}** monedas (x{multiplicador})'
        color     = 0xFFD700
    elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
        ganancia     = apuesta
        u['balance'] += ganancia
        resultado = f'✅ ¡Par! +**{ganancia:,}** monedas'
        color     = 0x00FF00
    else:
        resultado = f'❌ Perdiste **{apuesta:,}** monedas'
        color     = 0xFF0000
    guardar_eco(data)
    embed = discord.Embed(title='🎰 Tragamonedas', color=color)
    embed.add_field(name='Resultado', value=' '.join(reels), inline=False)
    embed.add_field(name='Estado',    value=resultado,       inline=False)
    embed.add_field(name='Cartera',   value=f'{u["balance"]:,}', inline=True)
    await ctx.send(embed=embed)


@bot.command(name='coinflip', aliases=['cf', 'apostar'])
async def coinflip(ctx, eleccion: str, apuesta: int):
    if eleccion.lower() not in ('cara', 'sello', 'c', 's'):
        return await ctx.send('❌ Elige `cara` o `sello`.')
    data = cargar_eco()
    uid  = str(ctx.author.id)
    u    = eco_user(data, uid)
    if apuesta < 10 or apuesta > u['balance']:
        return await ctx.send('❌ Apuesta inválida o saldo insuficiente.')
    resultado = random.choice(['cara', 'sello'])
    acierto   = eleccion.lower()[0] == resultado[0]
    if acierto:
        u['balance'] += apuesta
        msg   = f'✅ ¡Acertaste! +**{apuesta:,}** monedas'
        color = 0x00FF00
    else:
        u['balance'] -= apuesta
        msg   = f'❌ Fallaste. -**{apuesta:,}** monedas'
        color = 0xFF0000
    guardar_eco(data)
    embed = discord.Embed(title=f'🪙 {resultado.capitalize()}', description=msg, color=color)
    embed.add_field(name='Cartera', value=f'{u["balance"]:,}', inline=True)
    await ctx.send(embed=embed)


# ─── Niveles ────────────────────────────────────────────────────────────────
NIVELES_FILE = 'niveles.json'
XP_POR_MENSAJE = (15, 40)
COOLDOWN_XP = 60  # segundos entre ganancias de XP


def cargar_niveles() -> dict:
    if os.path.exists(NIVELES_FILE):
        with open(NIVELES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def guardar_niveles(data: dict):
    with open(NIVELES_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def xp_para_nivel(nivel: int) -> int:
    return 5 * nivel ** 2 + 50 * nivel + 100


def nivel_de_xp(xp: int) -> int:
    nivel = 0
    while xp >= xp_para_nivel(nivel):
        xp -= xp_para_nivel(nivel)
        nivel += 1
    return nivel


_xp_cooldown: dict = {}


@bot.event
async def on_message_xp(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    gid = str(message.guild.id)
    uid = str(message.author.id)
    key = f'{gid}_{uid}'
    ahora = time.time()
    if ahora - _xp_cooldown.get(key, 0) < COOLDOWN_XP:
        return
    _xp_cooldown[key] = ahora
    data = cargar_niveles()
    data.setdefault(gid, {}).setdefault(uid, {'xp': 0, 'nivel': 0})
    u = data[gid][uid]
    xp_ganada = random.randint(*XP_POR_MENSAJE)
    u['xp']  += xp_ganada
    nuevo_nivel = nivel_de_xp(u['xp'])
    if nuevo_nivel > u['nivel']:
        u['nivel'] = nuevo_nivel
        guardar_niveles(data)
        try:
            await message.channel.send(
                f'🎉 ¡{message.author.mention} subió al **nivel {nuevo_nivel}**!',
                delete_after=10,
            )
        except Exception:
            pass
    else:
        guardar_niveles(data)


@bot.command(name='nivel', aliases=['lvl', 'rank', 'xp'])
async def nivel(ctx, member: discord.Member = None):
    member = member or ctx.author
    data   = cargar_niveles()
    gid    = str(ctx.guild.id)
    uid    = str(member.id)
    u      = data.get(gid, {}).get(uid, {'xp': 0, 'nivel': 0})
    xp_act = u['xp']
    n      = u['nivel']
    xp_req = xp_para_nivel(n)
    # progreso visual
    pct    = min(int((xp_act % xp_req if xp_req else xp_act) / max(xp_req, 1) * 20), 20) if xp_req else 20
    barra  = '█' * pct + '░' * (20 - pct)
    embed  = discord.Embed(title=f'⭐ Nivel de {member.display_name}', color=0x5865F2)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name='🏅 Nivel', value=f'**{n}**', inline=True)
    embed.add_field(name='✨ XP',    value=f'**{xp_act:,}**', inline=True)
    embed.add_field(name='📊 Progreso', value=f'`[{barra}]` {xp_act % xp_req if xp_req else 0}/{xp_req}', inline=False)
    await ctx.send(embed=embed)


@bot.command(name='topnivel', aliases=['topxp', 'lvlboard'])
async def topnivel(ctx):
    data = cargar_niveles()
    gid  = str(ctx.guild.id)
    serv = data.get(gid, {})
    sorted_data = sorted(serv.items(), key=lambda x: x[1]['xp'], reverse=True)[:10]
    embed = discord.Embed(title='⭐ Top 10 Nivel', color=0x5865F2)
    for i, (uid, u) in enumerate(sorted_data, 1):
        m      = ctx.guild.get_member(int(uid))
        nombre = m.display_name if m else f'<@{uid}>'
        medal  = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else f'`{i}.`'
        embed.add_field(name=f'{medal} {nombre}', value=f'Nivel **{u["nivel"]}** — XP: **{u["xp"]:,}**', inline=False)
    await ctx.send(embed=embed)


# ─── Roles Reaction / Roles por reacción ────────────────────────────────────
RROLES_FILE = 'reaction_roles.json'


def cargar_rroles() -> dict:
    if os.path.exists(RROLES_FILE):
        with open(RROLES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def guardar_rroles(data: dict):
    with open(RROLES_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


@bot.command(name='rroles_add', aliases=['rr_add'])
@commands.check(es_admin)
async def rroles_add(ctx, mensaje_id: int, emoji: str, rol: discord.Role):
    """Agrega un role-reaction a un mensaje."""
    try:
        msg = await ctx.channel.fetch_message(mensaje_id)
        await msg.add_reaction(emoji)
    except Exception:
        return await ctx.send('❌ Mensaje no encontrado o emoji inválido.')
    data = cargar_rroles()
    key  = str(mensaje_id)
    data.setdefault(key, {})[emoji] = str(rol.id)
    guardar_rroles(data)
    await ctx.send(f'✅ Reacción `{emoji}` → {rol.mention} añadida.')


@bot.command(name='rroles_remove', aliases=['rr_remove'])
@commands.check(es_admin)
async def rroles_remove(ctx, mensaje_id: int, emoji: str):
    data = cargar_rroles()
    key  = str(mensaje_id)
    if key not in data or emoji not in data[key]:
        return await ctx.send('❌ No existe esa configuración.')
    del data[key][emoji]
    if not data[key]:
        del data[key]
    guardar_rroles(data)
    await ctx.send(f'✅ Reacción `{emoji}` eliminada.')


@bot.event
async def on_raw_reaction_add_rroles(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    data = cargar_rroles()
    key  = str(payload.message_id)
    if key not in data:
        return
    emoji = str(payload.emoji)
    if emoji not in data[key]:
        return
    guild  = bot.get_guild(payload.guild_id)
    member = guild and guild.get_member(payload.user_id)
    if not member or member.bot:
        return
    rol = guild.get_role(int(data[key][emoji]))
    if rol:
        try:
            await member.add_roles(rol, reason='Role Reaction')
        except Exception:
            pass


@bot.event
async def on_raw_reaction_remove_rroles(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    data = cargar_rroles()
    key  = str(payload.message_id)
    if key not in data:
        return
    emoji = str(payload.emoji)
    if emoji not in data[key]:
        return
    guild  = bot.get_guild(payload.guild_id)
    member = guild and guild.get_member(payload.user_id)
    if not member or member.bot:
        return
    rol = guild.get_role(int(data[key][emoji]))
    if rol and rol in member.roles:
        try:
            await member.remove_roles(rol, reason='Role Reaction removida')
        except Exception:
            pass


# ─── Stickymessage ──────────────────────────────────────────────────────────
STICKY_FILE = 'sticky.json'
_sticky_lock: dict = {}

# {guild_id: {user_id: apodo_forzado}}  — persiste en memoria mientras el bot corra
_fn_forzados: dict = {}


def cargar_sticky() -> dict:
    if os.path.exists(STICKY_FILE):
        with open(STICKY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def guardar_sticky(data: dict):
    with open(STICKY_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


@bot.command(name='sticky')
@commands.check(es_admin)
async def sticky(ctx, *, mensaje: str):
    data  = cargar_sticky()
    gid   = str(ctx.guild.id)
    cid   = str(ctx.channel.id)
    embed = discord.Embed(description=f'📌 {mensaje}', color=0xFFD700)
    msg   = await ctx.send(embed=embed)
    data.setdefault(gid, {})[cid] = {'msg_id': str(msg.id), 'texto': mensaje}
    guardar_sticky(data)
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name='unsticky')
@commands.check(es_admin)
async def unsticky(ctx):
    data = cargar_sticky()
    gid  = str(ctx.guild.id)
    cid  = str(ctx.channel.id)
    if gid in data and cid in data[gid]:
        try:
            old_msg = await ctx.channel.fetch_message(int(data[gid][cid]['msg_id']))
            await old_msg.delete()
        except Exception:
            pass
        del data[gid][cid]
        guardar_sticky(data)
        await ctx.send('✅ Sticky eliminado.', delete_after=5)
    else:
        await ctx.send('❌ Sin sticky en este canal.')


# ─── Autorol ────────────────────────────────────────────────────────────────
AUTOROL_FILE = 'autorol.json'


def cargar_autorol() -> dict:
    if os.path.exists(AUTOROL_FILE):
        with open(AUTOROL_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def guardar_autorol(data: dict):
    with open(AUTOROL_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


@bot.command(name='autorol', aliases=['autorole'])
@commands.check(es_admin)
async def autorol(ctx, rol: discord.Role = None):
    data = cargar_autorol()
    gid  = str(ctx.guild.id)
    if rol is None:
        rid = data.get(gid)
        r   = ctx.guild.get_role(int(rid)) if rid else None
        return await ctx.send(f'🔧 Autorol actual: {r.mention if r else "ninguno"}.')
    data[gid] = str(rol.id)
    guardar_autorol(data)
    await ctx.send(f'✅ Autorol → {rol.mention}.')


# ─── Welcome / Farewell ─────────────────────────────────────────────────────
WELCOME_FILE = 'welcome.json'


def cargar_welcome() -> dict:
    if os.path.exists(WELCOME_FILE):
        with open(WELCOME_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def guardar_welcome(data: dict):
    with open(WELCOME_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


@bot.command(name='welcome_set', aliases=['setwelcome'])
@commands.check(es_admin)
async def welcome_set(ctx, canal: discord.TextChannel, *, mensaje: str = None):
    data = cargar_welcome()
    gid  = str(ctx.guild.id)
    data.setdefault(gid, {})
    data[gid]['canal']   = str(canal.id)
    data[gid]['mensaje'] = mensaje or '¡Bienvenido {mention} a **{server}**! 🎉'
    guardar_welcome(data)
    await ctx.send(f'✅ Welcome configurado en {canal.mention}.')


@bot.command(name='farewell_set', aliases=['setfarewell'])
@commands.check(es_admin)
async def farewell_set(ctx, canal: discord.TextChannel, *, mensaje: str = None):
    data = cargar_welcome()
    gid  = str(ctx.guild.id)
    data.setdefault(gid, {})
    data[gid]['farewell_canal']   = str(canal.id)
    data[gid]['farewell_mensaje'] = mensaje or 'Adiós **{name}**, esperamos que vuelvas.'
    guardar_welcome(data)
    await ctx.send(f'✅ Farewell configurado en {canal.mention}.')


# ─── Prefix por servidor ────────────────────────────────────────────────────
PREFIXES_FILE = 'prefixes.json'


def cargar_prefixes() -> dict:
    if os.path.exists(PREFIXES_FILE):
        with open(PREFIXES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


async def get_prefix(bot, message):
    prefixes = cargar_prefixes()
    if message.guild:
        p = prefixes.get(str(message.guild.id), PREFIX)
    else:
        p = PREFIX
    return commands.when_mentioned_or(p)(bot, message)


# ─── Gag / Ungag (bloqueo temporal de un usuario en el canal) ───────────────
@bot.command(name='gag')
@commands.check(es_admin)
async def gag(ctx, member: discord.Member, *, razon: str = 'Sin razón'):
    ow = ctx.channel.overwrites_for(member)
    ow.send_messages = False
    await ctx.channel.set_permissions(member, overwrite=ow, reason=f'Gag por {ctx.author}: {razon}')
    await ctx.send(f'🤐 {member.mention} silenciado en este canal.')


@bot.command(name='ungag')
@commands.check(es_admin)
async def ungag(ctx, member: discord.Member):
    ow = ctx.channel.overwrites_for(member)
    ow.send_messages = None
    await ctx.channel.set_permissions(member, overwrite=ow, reason=f'Ungag por {ctx.author}')
    await ctx.send(f'✅ {member.mention} puede hablar de nuevo.')


# ─── Historial de sanciones ─────────────────────────────────────────────────
@bot.command(name='historial', aliases=['modlog', 'infracciones'])
@commands.check(es_staff)
async def historial(ctx, member: discord.Member = None):
    member = member or ctx.author
    data   = cargar_warns()
    ws     = data.get(str(ctx.guild.id), {}).get(str(member.id), [])
    embed  = discord.Embed(title=f'📋 Historial de {member.display_name}', color=0xFF8800)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name='⚠️ Warns', value=str(len(ws)), inline=True)
    if ws:
        embed.add_field(
            name='Últimos warns',
            value='\n'.join(f'`#{i+1}` {w["razon"]}' for i, w in enumerate(ws[-5:])),
            inline=False,
        )
    await ctx.send(embed=embed)


# ─── Info de invitación ─────────────────────────────────────────────────────
@bot.command(name='invites')
@commands.check(es_admin)
async def invites(ctx, member: discord.Member = None):
    member = member or ctx.author
    try:
        all_invites = await ctx.guild.invites()
        mis_invites = [i for i in all_invites if i.inviter and i.inviter.id == member.id]
        total_uses  = sum(i.uses for i in mis_invites)
        embed = discord.Embed(title=f'📨 Invitaciones de {member.display_name}', color=0x5865F2)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name='Links creados', value=len(mis_invites), inline=True)
        embed.add_field(name='Total usos',    value=total_uses,       inline=True)
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await ctx.send('❌ Sin permisos para ver invitaciones.')


# ─── Emojis info ────────────────────────────────────────────────────────────
@bot.command(name='emojis')
async def emojis_cmd(ctx):
    g      = ctx.guild
    emojis = g.emojis
    if not emojis:
        return await ctx.send('❌ Sin emojis personalizados.')
    chunks, chunk = [], ''
    for e in emojis:
        parte = f'{e} `:{e.name}:` '
        if len(chunk) + len(parte) > 1000:
            chunks.append(chunk)
            chunk = ''
        chunk += parte
    if chunk:
        chunks.append(chunk)
    for i, c in enumerate(chunks, 1):
        embed = discord.Embed(
            title=f'😄 Emojis del servidor ({i}/{len(chunks)}) — {len(emojis)} total',
            description=c, color=0x5865F2,
        )
        await ctx.send(embed=embed)


# ─── Rol info ───────────────────────────────────────────────────────────────
@bot.command(name='rolinfo', aliases=['roleinfo', 'ri'])
async def rolinfo(ctx, *, nombre_rol: str):
    rol = discord.utils.find(lambda r: r.name.lower() == nombre_rol.lower(), ctx.guild.roles)
    if not rol:
        return await ctx.send(f'❌ No encontré `{nombre_rol}`.')
    embed = discord.Embed(title=f'🎭 {rol.name}', color=rol.color)
    embed.add_field(name='🆔 ID',        value=rol.id,                                           inline=True)
    embed.add_field(name='🎨 Color',     value=str(rol.color),                                   inline=True)
    embed.add_field(name='👥 Miembros',  value=len(rol.members),                                 inline=True)
    embed.add_field(name='📅 Creado',    value=f'<t:{int(rol.created_at.timestamp())}:R>',        inline=True)
    embed.add_field(name='🔔 Mention',   value='Sí' if rol.mentionable else 'No',               inline=True)
    embed.add_field(name='📌 Hoisted',   value='Sí' if rol.hoist else 'No',                     inline=True)
    perms = [n.replace('_', ' ').title() for n, v in rol.permissions if v]
    embed.add_field(name='🔑 Permisos', value=', '.join(perms[:10]) or 'Ninguno', inline=False)
    await ctx.send(embed=embed)


# ─── Canal info ─────────────────────────────────────────────────────────────
@bot.command(name='canalinfo', aliases=['channelinfo', 'ci'])
async def canalinfo(ctx, canal: discord.TextChannel = None):
    canal = canal or ctx.channel
    embed = discord.Embed(title=f'💬 #{canal.name}', color=0x5865F2)
    embed.add_field(name='🆔 ID',        value=canal.id,                                          inline=True)
    embed.add_field(name='📁 Categoría', value=canal.category.name if canal.category else 'Ninguna', inline=True)
    embed.add_field(name='📅 Creado',    value=f'<t:{int(canal.created_at.timestamp())}:R>',      inline=True)
    embed.add_field(name='🔞 NSFW',      value='Sí' if canal.is_nsfw() else 'No',                inline=True)
    embed.add_field(name='⏱️ Slowmode',  value=f'{canal.slowmode_delay}s',                        inline=True)
    embed.add_field(name='📌 Tema',      value=canal.topic or 'Sin tema',                         inline=False)
    await ctx.send(embed=embed)


# ─── Roleplay / Social ──────────────────────────────────────────────────────

def cargar_parejas() -> dict:
    if os.path.exists(PAREJAS_FILE):
        with open(PAREJAS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def guardar_parejas(data: dict):
    with open(PAREJAS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def cargar_familia() -> dict:
    if os.path.exists(FAMILIA_FILE):
        with open(FAMILIA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def guardar_familia(data: dict):
    with open(FAMILIA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


_propuestas: dict = {}


@bot.command(name='casar', aliases=['proponer', 'marry'])
async def casar(ctx, member: discord.Member):
    if member == ctx.author:
        return await ctx.send('❌ No puedes casarte contigo mismo.')
    if member.bot:
        return await ctx.send('❌ Los bots no se casan.')
    data = cargar_parejas()
    if str(ctx.author.id) in data:
        return await ctx.send('💔 Ya estás casado/a. Usa `,divorcio` primero.')
    if str(member.id) in data:
        return await ctx.send(f'💔 {member.display_name} ya está casado/a.')
    _propuestas[ctx.guild.id] = {str(ctx.author.id): str(member.id)}
    embed = discord.Embed(
        title='💍 Propuesta de Matrimonio',
        description=f'{ctx.author.mention} le ha propuesto matrimonio a {member.mention}\n\n{member.mention}, escribe `,aceptar` o `,rechazar`.',
        color=0xFF69B4,
    )
    await ctx.send(embed=embed)


@bot.command(name='aceptar')
async def aceptar(ctx):
    prop = _propuestas.get(ctx.guild.id, {})
    pareja_id = next((k for k, v in prop.items() if v == str(ctx.author.id)), None)
    if not pareja_id:
        return await ctx.send('❌ No tienes ninguna propuesta pendiente.')
    data = cargar_parejas()
    data[pareja_id]          = str(ctx.author.id)
    data[str(ctx.author.id)] = pareja_id
    guardar_parejas(data)
    _propuestas.get(ctx.guild.id, {}).clear()
    pareja = ctx.guild.get_member(int(pareja_id))
    embed  = discord.Embed(
        title='💍 ¡Se casaron!',
        description=f'🎉 {ctx.author.mention} y {pareja.mention if pareja else f"<@{pareja_id}>"}',
        color=0xFF69B4,
    )
    await ctx.send(embed=embed)


@bot.command(name='rechazar')
async def rechazar(ctx):
    prop = _propuestas.get(ctx.guild.id, {})
    pareja_id = next((k for k, v in prop.items() if v == str(ctx.author.id)), None)
    if not pareja_id:
        return await ctx.send('❌ Sin propuesta pendiente.')
    _propuestas.get(ctx.guild.id, {}).clear()
    pareja = ctx.guild.get_member(int(pareja_id))
    await ctx.send(f'💔 {ctx.author.mention} rechazó a {pareja.mention if pareja else f"<@{pareja_id}>"}.')


@bot.command(name='divorcio', aliases=['divorciar'])
async def divorcio(ctx):
    data = cargar_parejas()
    uid  = str(ctx.author.id)
    if uid not in data:
        return await ctx.send('❌ No estás casado/a.')
    pareja_id = data.pop(uid)
    data.pop(pareja_id, None)
    guardar_parejas(data)
    pareja = ctx.guild.get_member(int(pareja_id))
    await ctx.send(f'💔 {ctx.author.mention} se divorció de {pareja.mention if pareja else f"<@{pareja_id}>"}.')


@bot.command(name='pareja', aliases=['esposo', 'esposa'])
async def ver_pareja(ctx, member: discord.Member = None):
    member = member or ctx.author
    data   = cargar_parejas()
    uid    = str(member.id)
    if uid not in data:
        return await ctx.send(f'💔 {member.display_name} no está casado/a.')
    pareja = ctx.guild.get_member(int(data[uid]))
    embed  = discord.Embed(title=f'💍 Pareja de {member.display_name}', color=0xFF69B4)
    embed.description = pareja.mention if pareja else f'<@{data[uid]}>'
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name='adoptar')
async def adoptar(ctx, member: discord.Member):
    if member == ctx.author or member.bot:
        return await ctx.send('❌ No válido.')
    data = cargar_familia()
    uid  = str(ctx.author.id)
    mid  = str(member.id)
    if uid not in data:
        data[uid] = {'hijos': []}
    if mid in data[uid]['hijos']:
        return await ctx.send(f'⚠️ Ya adoptaste a {member.display_name}.')
    data[uid]['hijos'].append(mid)
    guardar_familia(data)
    embed = discord.Embed(
        title='👨‍👧 Adopción',
        description=f'💖 {ctx.author.mention} adoptó a {member.mention}',
        color=0xFF69B4,
    )
    await ctx.send(embed=embed)


@bot.command(name='familia')
async def ver_familia(ctx, member: discord.Member = None):
    member = member or ctx.author
    data   = cargar_familia()
    uid    = str(member.id)
    info   = data.get(uid, {'hijos': []})
    embed  = discord.Embed(title=f'👨‍👩‍👧 Familia de {member.display_name}', color=0xFF69B4)
    embed.set_thumbnail(url=member.display_avatar.url)
    parejas = cargar_parejas()
    if uid in parejas:
        p = ctx.guild.get_member(int(parejas[uid]))
        embed.add_field(name='💍 Pareja', value=p.mention if p else f'<@{parejas[uid]}>', inline=False)
    hijos = info.get('hijos', [])
    if hijos:
        embed.add_field(
            name=f'👶 Hijos ({len(hijos)})',
            value=' '.join(f'<@{h}>' for h in hijos[:10]),
            inline=False,
        )
    await ctx.send(embed=embed)


# ─── Anime acciones ──────────────────────────────────────────────────────────
ANIME_ACCIONES = {
    'abrazar': {'emoji': '🤗', 'gif_tag': 'hug',    'msg': '{a} abraza a {b} 🤗',                 'boton': 'Abrazar 🤗'},
    'pat':     {'emoji': '👋', 'gif_tag': 'pat',    'msg': '{a} le da palmaditas a {b} 👋',        'boton': 'Palmaditas 👋'},
    'slap':    {'emoji': '😤', 'gif_tag': 'slap',   'msg': '{a} cachetea a {b} 😤',               'boton': 'Devolver 😤'},
    'kiss':    {'emoji': '💋', 'gif_tag': 'kiss',   'msg': '{a} besa a {b} 💋',                   'boton': 'Beso 💋'},
    'cry':     {'emoji': '😢', 'gif_tag': 'cry',    'msg': '{a} está llorando 😢',                 'boton': 'Consolar 🫂'},
    'poke':    {'emoji': '👉', 'gif_tag': 'poke',   'msg': '{a} toca a {b} 👉',                   'boton': 'Devolver 👉'},
    'cuddle':  {'emoji': '🥰', 'gif_tag': 'cuddle', 'msg': '{a} acurruca a {b} 🥰',               'boton': 'Acurrucarse 🥰'},
    'bite':    {'emoji': '😬', 'gif_tag': 'bite',   'msg': '{a} muerde a {b} 😬',                 'boton': 'Morder 😬'},
    'wave':    {'emoji': '👋', 'gif_tag': 'wave',   'msg': '{a} saluda a {b} 👋',                 'boton': 'Saludar 👋'},
    'dance':   {'emoji': '💃', 'gif_tag': 'dance',  'msg': '{a} baila con {b} 💃',                'boton': 'Bailar 💃'},
    'highfive':{'emoji': '🙌', 'gif_tag': 'highfive','msg': '{a} choca los cinco con {b} 🙌',     'boton': 'Chocar 🙌'},
    'blush':   {'emoji': '😊', 'gif_tag': 'blush',  'msg': '{a} se ruboriza por {b} 😊',          'boton': 'Sonreír 😊'},
}
_contadores_anime: dict = {}


def get_contador(uid1: int, uid2: int, accion: str) -> int:
    key = f'{min(uid1, uid2)}-{max(uid1, uid2)}-{accion}'
    _contadores_anime[key] = _contadores_anime.get(key, 0) + 1
    return _contadores_anime[key]


async def obtener_gif_anime(tag: str) -> str | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'https://nekos.best/api/v2/{tag}') as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data['results'][0]['url']
    except Exception:
        pass
    return None


class AnimeView(discord.ui.View):
    def __init__(self, autor, target, accion, info):
        super().__init__(timeout=60)
        self.autor  = autor
        self.target = target
        self.accion = accion
        self.info   = info
        btn_r = discord.ui.Button(label=info['boton'],  style=discord.ButtonStyle.primary)
        btn_x = discord.ui.Button(label='Rechazar ✖', style=discord.ButtonStyle.danger)

        async def r_cb(interaction):
            if interaction.user.id != self.target.id:
                return await interaction.response.send_message('❌ No es para ti.', ephemeral=True)
            gif  = await obtener_gif_anime(self.info['gif_tag'])
            msg  = self.info['msg'].format(a=self.target.display_name, b=self.autor.display_name)
            embed = discord.Embed(description=msg, color=0xFF69B4)
            if gif:
                embed.set_image(url=gif)
            await interaction.response.send_message(embed=embed)
            self.stop()

        async def x_cb(interaction):
            if interaction.user.id != self.target.id:
                return await interaction.response.send_message('❌ No es para ti.', ephemeral=True)
            await interaction.response.send_message(
                f'💔 **{self.target.display_name}** rechazó a **{self.autor.display_name}**.')
            self.stop()

        btn_r.callback = r_cb
        btn_x.callback = x_cb
        self.add_item(btn_r)
        self.add_item(btn_x)


def make_anime_cmd(accion: str, info: dict):
    @bot.command(name=accion)
    async def _cmd(ctx, member: discord.Member = None):
        a   = ctx.author.display_name
        b   = member.display_name if member else 'todos'
        get_contador(ctx.author.id, member.id if member else 0, accion)
        msg = info['msg'].format(a=a, b=b)
        gif = await obtener_gif_anime(info['gif_tag'])
        embed = discord.Embed(description=f'**{msg}**', color=0xFF69B4)
        if gif:
            embed.set_image(url=gif)
        if member and member != ctx.author:
            view = AnimeView(ctx.author, member, accion, info)
            await ctx.send(embed=embed, view=view)
        else:
            await ctx.send(embed=embed)
    _cmd.__name__ = accion


for _a, _i in ANIME_ACCIONES.items():
    make_anime_cmd(_a, _i)


# ─── Fun varios ─────────────────────────────────────────────────────────────
@bot.command(name='horoscopo', aliases=['signo', 'zodiac'])
async def horoscopo(ctx, *, signo: str):
    signos = {
        'aries': '♈', 'tauro': '♉', 'geminis': '♊', 'cancer': '♋',
        'leo': '♌', 'virgo': '♍', 'libra': '♎', 'escorpio': '♏',
        'sagitario': '♐', 'capricornio': '♑', 'acuario': '♒', 'piscis': '♓',
    }
    s = signo.lower()
    emoji = signos.get(s, '🔮')
    mensajes = [
        'Hoy es un gran día para nuevos comienzos. 🌟',
        'La paciencia será tu mayor virtud hoy. 🧘',
        'Una oportunidad inesperada se presentará. 💡',
        'Alguien especial pensará en ti. 💕',
        'Cuida tu energía y descansa cuando lo necesites. 😴',
    ]
    embed = discord.Embed(title=f'{emoji} Horóscopo de {signo.title()}', description=random.choice(mensajes), color=0x9932CC)
    await ctx.send(embed=embed)


@bot.command(name='personalidad', aliases=['quiensoy', 'tipo'])
async def personalidad(ctx, member: discord.Member = None):
    member = member or ctx.author
    tipos  = [
        ('🌟 El Líder',          'Nato para guiar a los demás.'),
        ('🎨 El Creativo',       'Tu mente no tiene límites.'),
        ('🧠 El Estratega',      'Siempre un paso adelante.'),
        ('❤️ El Empático',       'Sientes todo intensamente.'),
        ('🤪 El Caótico Bueno', 'Impredecible, pero divertido.'),
        ('🦊 El Astuto',         'Siempre encuentras la salida.'),
    ]
    random.seed(member.id)
    tipo, desc = random.choice(tipos)
    embed = discord.Embed(title=tipo, description=f'{member.mention}: {desc}', color=0x9932CC)
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name='compatibilidad', aliases=['compat', 'shipper'])
async def compatibilidad(ctx, member: discord.Member):
    pct   = random.randint(1, 100)
    color = 0x00FF00 if pct >= 70 else 0xFFAA00 if pct >= 40 else 0xFF0000
    barra = '❤️' * (pct // 10) + '🖤' * (10 - pct // 10)
    embed = discord.Embed(title='💕 Compatibilidad', color=color)
    embed.add_field(name='Pareja', value=f'{ctx.author.mention} ❤️ {member.mention}', inline=False)
    embed.add_field(name='Porcentaje', value=f'**{pct}%**', inline=True)
    embed.add_field(name='Medidor', value=barra, inline=False)
    await ctx.send(embed=embed)


@bot.command(name='frase_personaje', aliases=['fp', 'anime_quote'])
async def frase_personaje(ctx, *, personaje: str = None):
    frases = {
        'naruto':  ['¡Nunca me rendiré, ese es mi ninja way!', '¡Creo en ti!'],
        'goku':    ['¡Kaaa-meee-haaa-meee-haaa!', 'Siempre habrá alguien más fuerte.'],
        'luffy':   ['¡Seré el Rey de los Piratas!', 'No me importa morir, me importa luchar.'],
        'default': ['El poder viene del interior.', 'La amistad es la verdadera fortaleza.'],
    }
    key  = personaje.lower() if personaje else 'default'
    lista = frases.get(key, frases['default'])
    embed = discord.Embed(
        title=f'💬 {personaje.title() if personaje else "Cita Anime"}',
        description=f'*"{random.choice(lista)}"*',
        color=0xFF8C00,
    )
    await ctx.send(embed=embed)


@bot.command(name='dado_personalizado2', aliases=['dp2'])
async def dado_personalizado2(ctx, *args):
    """Tira dados estilo D&D: ej. ,dp2 1d20 2d6"""
    resultados = []
    total      = 0
    for arg in args:
        try:
            n, d = map(int, arg.lower().split('d'))
            if not (1 <= n <= 20 and 2 <= d <= 1000):
                continue
            tiradas = [random.randint(1, d) for _ in range(n)]
            resultados.append(f'`{arg}`: {" + ".join(str(t) for t in tiradas)} = **{sum(tiradas)}**')
            total += sum(tiradas)
        except Exception:
            pass
    if not resultados:
        return await ctx.send('❌ Formato: `,dp2 1d20 2d6`')
    embed = discord.Embed(title='🎲 Dados D&D', description='\n'.join(resultados), color=0x5865F2)
    embed.add_field(name='Total', value=f'**{total}**', inline=True)
    await ctx.send(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
# ────────── SLASH COMMANDS ─────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

tree = bot.tree


# Utilidades
@tree.command(name='ping', description='Muestra la latencia del bot')
async def slash_ping(interaction: discord.Interaction):
    lat   = round(bot.latency * 1000)
    color = 0x00FF00 if lat < 100 else 0xFFAA00 if lat < 200 else 0xFF0000
    await interaction.response.send_message(
        embed=discord.Embed(title='🏓 Pong!', description=f'**{lat}ms**', color=color), ephemeral=True)


@tree.command(name='userinfo', description='Información de un usuario')
@app_commands.describe(member='El usuario a consultar')
async def slash_userinfo(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    roles  = [r.mention for r in member.roles if r != interaction.guild.default_role]
    embed  = discord.Embed(title=f'👤 {member}', color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name='🆔 ID',     value=member.id,                                     inline=True)
    embed.add_field(name='📅 Cuenta', value=f'<t:{int(member.created_at.timestamp())}:R>', inline=True)
    embed.add_field(name='📥 Unión',  value=f'<t:{int(member.joined_at.timestamp())}:R>',  inline=True)
    embed.add_field(name='🎭 Roles',  value=' '.join(roles[:10]) or 'Sin roles',           inline=False)
    await interaction.response.send_message(embed=embed)


@tree.command(name='serverinfo', description='Información del servidor')
async def slash_serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    embed = discord.Embed(title=f'🏠 {g.name}', color=0x5865F2)
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name='👥 Miembros', value=g.member_count, inline=True)
    embed.add_field(name='💬 Canales',  value=len(g.channels), inline=True)
    embed.add_field(name='🎭 Roles',    value=len(g.roles),    inline=True)
    embed.add_field(name='👑 Owner',    value=g.owner.mention, inline=True)
    await interaction.response.send_message(embed=embed)


@tree.command(name='avatar', description='Muestra el avatar de un usuario')
@app_commands.describe(member='El usuario')
async def slash_avatar(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    embed  = discord.Embed(title=f'🖼️ {member.display_name}', color=member.color)
    embed.set_image(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@tree.command(name='ban', description='Banea a un miembro')
@app_commands.describe(member='Miembro a banear', razon='Razón')
@app_commands.default_permissions(ban_members=True)
async def slash_ban(interaction: discord.Interaction, member: discord.Member, razon: str = 'Sin razón'):
    try:
        await interaction.guild.ban(member, reason=f'[{interaction.user}] {razon}')
        await interaction.response.send_message(f'🔨 **{member}** baneado. Razón: {razon}', ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message('❌ Sin permisos.', ephemeral=True)


@tree.command(name='kick', description='Kickea a un miembro')
@app_commands.describe(member='Miembro a kickear', razon='Razón')
@app_commands.default_permissions(kick_members=True)
async def slash_kick(interaction: discord.Interaction, member: discord.Member, razon: str = 'Sin razón'):
    try:
        await interaction.guild.kick(member, reason=f'[{interaction.user}] {razon}')
        await interaction.response.send_message(f'👢 **{member}** kickeado.', ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message('❌ Sin permisos.', ephemeral=True)


@tree.command(name='mute', description='Silencia a un miembro')
@app_commands.describe(member='Miembro', tiempo='Duración (ej: 10m, 1h)', razon='Razón')
@app_commands.default_permissions(moderate_members=True)
async def slash_mute(interaction: discord.Interaction, member: discord.Member, tiempo: str = '10m', razon: str = 'Sin razón'):
    unidades = {'s': 1, 'm': 60, 'h': 3600}
    try:
        u = tiempo[-1].lower()
        n = int(tiempo[:-1])
        segundos = n * unidades[u]
        until = discord.utils.utcnow() + dt.timedelta(seconds=segundos)
        await member.timeout(until, reason=f'[{interaction.user}] {razon}')
        await interaction.response.send_message(f'🔇 **{member}** muteado por {tiempo}.', ephemeral=True)
    except Exception:
        await interaction.response.send_message('❌ Error al mutear.', ephemeral=True)


@tree.command(name='warn', description='Advierte a un usuario')
@app_commands.describe(member='Miembro', razon='Razón')
@app_commands.default_permissions(manage_messages=True)
async def slash_warn(interaction: discord.Interaction, member: discord.Member, razon: str = 'Sin razón'):
    data = cargar_warns()
    gid  = str(interaction.guild_id)
    uid  = str(member.id)
    data.setdefault(gid, {}).setdefault(uid, [])
    data[gid][uid].append({'razon': razon, 'por': str(interaction.user.id), 'ts': int(time.time())})
    guardar_warns(data)
    total = len(data[gid][uid])
    await interaction.response.send_message(
        f'⚠️ {member.mention} advertido. Razón: **{razon}** | Total: **{total}**', ephemeral=True)


@tree.command(name='purge', description='Borra mensajes')
@app_commands.describe(cantidad='Número de mensajes a borrar')
@app_commands.default_permissions(manage_messages=True)
async def slash_purge(interaction: discord.Interaction, cantidad: int = 10):
    if not 1 <= cantidad <= 100:
        return await interaction.response.send_message('❌ Entre 1 y 100.', ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    borrados = await interaction.channel.purge(limit=cantidad)
    await interaction.followup.send(f'🗑️ **{len(borrados)}** mensajes borrados.', ephemeral=True)


@tree.command(name='lock', description='Bloquea el canal actual')
@app_commands.default_permissions(manage_channels=True)
async def slash_lock(interaction: discord.Interaction):
    canal = interaction.channel
    ow    = canal.overwrites_for(interaction.guild.default_role)
    ow.send_messages = False
    await canal.set_permissions(interaction.guild.default_role, overwrite=ow)
    await interaction.response.send_message(f'🔒 {canal.mention} bloqueado.', ephemeral=True)


@tree.command(name='unlock', description='Desbloquea el canal actual')
@app_commands.default_permissions(manage_channels=True)
async def slash_unlock(interaction: discord.Interaction):
    canal = interaction.channel
    ow    = canal.overwrites_for(interaction.guild.default_role)
    ow.send_messages = None
    await canal.set_permissions(interaction.guild.default_role, overwrite=ow)
    await interaction.response.send_message(f'🔓 {canal.mention} desbloqueado.', ephemeral=True)


@tree.command(name='slowmode', description='Configura el modo lento')
@app_commands.describe(segundos='Segundos de espera (0 para desactivar)')
@app_commands.default_permissions(manage_channels=True)
async def slash_slowmode(interaction: discord.Interaction, segundos: int = 0):
    await interaction.channel.edit(slowmode_delay=segundos)
    msg = f'⏱️ Slowmode: **{segundos}s**' if segundos else '✅ Slowmode desactivado.'
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name='dar_rol', description='Da un rol a un miembro')
@app_commands.describe(member='Miembro', rol='Rol a dar')
@app_commands.default_permissions(manage_roles=True)
async def slash_dar_rol(interaction: discord.Interaction, member: discord.Member, rol: discord.Role):
    if rol in member.roles:
        return await interaction.response.send_message(f'⚠️ Ya tiene **{rol.name}**.', ephemeral=True)
    try:
        await member.add_roles(rol, reason=f'Dado por {interaction.user}')
        await interaction.response.send_message(f'✅ {rol.mention} dado a {member.mention}.', ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message('❌ Sin permisos.', ephemeral=True)


@tree.command(name='quitar_rol', description='Quita un rol a un miembro')
@app_commands.describe(member='Miembro', rol='Rol a quitar')
@app_commands.default_permissions(manage_roles=True)
async def slash_quitar_rol(interaction: discord.Interaction, member: discord.Member, rol: discord.Role):
    if rol not in member.roles:
        return await interaction.response.send_message(f'⚠️ No tiene **{rol.name}**.', ephemeral=True)
    try:
        await member.remove_roles(rol, reason=f'Quitado por {interaction.user}')
        await interaction.response.send_message(f'✅ **{rol.name}** quitado de {member.mention}.', ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message('❌ Sin permisos.', ephemeral=True)


@tree.command(name='balance', description='Consulta tu balance de monedas')
@app_commands.describe(member='Usuario a consultar')
async def slash_balance(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    data   = cargar_eco()
    u      = eco_user(data, str(member.id))
    embed  = discord.Embed(title=f'💰 Balance de {member.display_name}', color=0xFFD700)
    embed.add_field(name='💵 Cartera', value=f'{u["balance"]:,}', inline=True)
    embed.add_field(name='🏦 Banco',   value=f'{u["bank"]:,}',    inline=True)
    await interaction.response.send_message(embed=embed)


@tree.command(name='daily', description='Reclama tu recompensa diaria')
async def slash_daily(interaction: discord.Interaction):
    data  = cargar_eco()
    uid   = str(interaction.user.id)
    u     = eco_user(data, uid)
    ahora = int(time.time())
    if ahora - u['daily_ts'] < 86400:
        restante = 86400 - (ahora - u['daily_ts'])
        h, m = divmod(restante // 60, 60)
        return await interaction.response.send_message(f'⏰ Vuelve en **{h}h {m}m**.', ephemeral=True)
    cantidad      = random.randint(100, 500)
    u['balance'] += cantidad
    u['daily_ts'] = ahora
    guardar_eco(data)
    await interaction.response.send_message(
        embed=discord.Embed(title='💸 Daily', description=f'+**{cantidad}** monedas', color=0x00FF00))


@tree.command(name='nivel', description='Ve tu nivel y XP')
@app_commands.describe(member='Usuario a consultar')
async def slash_nivel(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    data   = cargar_niveles()
    gid    = str(interaction.guild_id)
    uid    = str(member.id)
    u      = data.get(gid, {}).get(uid, {'xp': 0, 'nivel': 0})
    xp_req = xp_para_nivel(u['nivel'])
    pct    = min(int((u['xp'] % xp_req if xp_req else u['xp']) / max(xp_req, 1) * 20), 20)
    barra  = '█' * pct + '░' * (20 - pct)
    embed  = discord.Embed(title=f'⭐ {member.display_name}', color=0x5865F2)
    embed.add_field(name='Nivel',    value=f'**{u["nivel"]}**', inline=True)
    embed.add_field(name='XP',       value=f'**{u["xp"]:,}**', inline=True)
    embed.add_field(name='Progreso', value=f'`[{barra}]`',      inline=False)
    await interaction.response.send_message(embed=embed)


@tree.command(name='slots', description='Juega a las tragamonedas')
@app_commands.describe(apuesta='Cuántas monedas apostar')
async def slash_slots(interaction: discord.Interaction, apuesta: int = 50):
    await interaction.response.defer()
    data = cargar_eco()
    uid  = str(interaction.user.id)
    u    = eco_user(data, uid)
    if apuesta < 10 or apuesta > u['balance']:
        return await interaction.followup.send('❌ Apuesta inválida o saldo insuficiente.', ephemeral=True)
    simbolos = ['🍒', '🍋', '🍊', '⭐', '💎', '🔔']
    reels    = [random.choice(simbolos) for _ in range(3)]
    u['balance'] -= apuesta
    if reels[0] == reels[1] == reels[2]:
        mult         = 10 if reels[0] == '💎' else 5
        ganancia     = apuesta * mult
        u['balance'] += ganancia
        resultado    = f'🎉 JACKPOT! +**{ganancia:,}** (x{mult})'
        color        = 0xFFD700
    elif len(set(reels)) == 2:
        u['balance'] += apuesta
        resultado    = f'✅ Par! +**{apuesta:,}**'
        color        = 0x00FF00
    else:
        resultado = f'❌ Perdiste **{apuesta:,}**'
        color     = 0xFF0000
    guardar_eco(data)
    embed = discord.Embed(title='🎰 Tragamonedas', color=color)
    embed.add_field(name='Reels',   value=' '.join(reels), inline=False)
    embed.add_field(name='Estado',  value=resultado,       inline=False)
    embed.add_field(name='Cartera', value=f'{u["balance"]:,}', inline=True)
    await interaction.followup.send(embed=embed)


@tree.command(name='8ball', description='La bola mágica responde tu pregunta')
@app_commands.describe(pregunta='Tu pregunta')
async def slash_8ball(interaction: discord.Interaction, pregunta: str):
    respuestas = [
        '✅ Sí, definitivamente.', '✅ Todo indica que sí.', '✅ Sin duda.',
        '🤔 No está claro.', '❌ No cuentes con ello.', '❌ Definitivamente no.',
    ]
    embed = discord.Embed(title='🎱 Bola Mágica', color=0x330066)
    embed.add_field(name='❓', value=pregunta,                  inline=False)
    embed.add_field(name='🔮', value=random.choice(respuestas), inline=False)
    await interaction.response.send_message(embed=embed)


@tree.command(name='clima', description='Consulta el clima de una ciudad')
@app_commands.describe(ciudad='Nombre de la ciudad')
async def slash_clima(interaction: discord.Interaction, ciudad: str):
    await interaction.response.defer()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://wttr.in/{ciudad.replace(' ', '+')}?format=j1") as resp:
                if resp.status != 200:
                    return await interaction.followup.send('❌ Ciudad no encontrada.')
                data   = await resp.json()
                actual = data['current_condition'][0]
                embed  = discord.Embed(title=f'🌤️ {ciudad.title()}', color=0x4169E1)
                embed.add_field(name='🌡️ Temp',  value=f"{actual['temp_C']}°C",            inline=True)
                embed.add_field(name='💧 Hum',   value=f"{actual['humidity']}%",           inline=True)
                embed.add_field(name='💨 Viento', value=f"{actual['windspeedKmph']} km/h", inline=True)
                await interaction.followup.send(embed=embed)
    except Exception:
        await interaction.followup.send('❌ No pude obtener el clima.')


@tree.command(name='rolinfo', description='Información sobre un rol')
@app_commands.describe(rol='El rol a consultar')
async def slash_rolinfo(interaction: discord.Interaction, rol: discord.Role):
    embed = discord.Embed(title=f'🎭 {rol.name}', color=rol.color)
    embed.add_field(name='🆔 ID',       value=rol.id,              inline=True)
    embed.add_field(name='👥 Miembros', value=len(rol.members),    inline=True)
    embed.add_field(name='🎨 Color',    value=str(rol.color),      inline=True)
    embed.add_field(name='🔔 Mention',  value='Sí' if rol.mentionable else 'No', inline=True)
    await interaction.response.send_message(embed=embed)


@tree.command(name='antinuke', description='Ver el panel de AntiNuke')
async def slash_antinuke(interaction: discord.Interaction):
    cfg    = cargar_antinuke(interaction.guild_id)
    activo = '🟢 Activo' if cfg['activo'] else '🔴 Desactivado'
    embed  = discord.Embed(title='🛡️ Panel AntiNuke', color=0x5865F2)
    embed.add_field(name='Estado',  value=activo,              inline=True)
    embed.add_field(name='Acción',  value=f'`{cfg["accion"]}`', inline=True)
    embed.add_field(name='Ventana', value=f'`{cfg["ventana"]}s`', inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name='leaderboard', description='Top 10 más ricos del servidor')
async def slash_leaderboard(interaction: discord.Interaction):
    data       = cargar_eco()
    sorted_data = sorted(
        ((uid, u['balance'] + u['bank']) for uid, u in data.items()),
        key=lambda x: x[1], reverse=True,
    )[:10]
    embed = discord.Embed(title='🏆 Top 10 Más Ricos', color=0xFFD700)
    for i, (uid, total) in enumerate(sorted_data, 1):
        m      = interaction.guild.get_member(int(uid))
        nombre = m.display_name if m else f'<@{uid}>'
        medal  = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else f'`{i}.`'
        embed.add_field(name=f'{medal} {nombre}', value=f'{total:,} monedas', inline=False)
    await interaction.response.send_message(embed=embed)


@tree.command(name='warns', description='Ver los warns de un usuario')
@app_commands.describe(member='El usuario')
async def slash_warns(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    data   = cargar_warns()
    ws     = data.get(str(interaction.guild_id), {}).get(str(member.id), [])
    if not ws:
        return await interaction.response.send_message(f'✅ {member.mention} sin warns.', ephemeral=True)
    embed = discord.Embed(title=f'⚠️ Warns de {member.display_name}', color=0xFFAA00)
    for i, w in enumerate(ws, 1):
        embed.add_field(name=f'#{i}', value=w['razon'], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name='meme', description='Obtiene un meme aleatorio')
async def slash_meme(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get('https://meme-api.com/gimme') as resp:
                if resp.status == 200:
                    data  = await resp.json()
                    embed = discord.Embed(title=data['title'], color=0xFF8C00)
                    embed.set_image(url=data['url'])
                    return await interaction.followup.send(embed=embed)
    except Exception:
        pass
    await interaction.followup.send('❌ No pude obtener un meme.')


@tree.command(name='rng', description='Número aleatorio')
@app_commands.describe(minimo='Número mínimo', maximo='Número máximo')
async def slash_rng(interaction: discord.Interaction, minimo: int = 1, maximo: int = 100):
    if minimo >= maximo:
        return await interaction.response.send_message('❌ El mínimo debe ser menor.', ephemeral=True)
    embed = discord.Embed(title='🎲 Número Aleatorio', color=0x5865F2)
    embed.add_field(name='Resultado', value=f'**{random.randint(minimo, maximo)}**', inline=True)
    await interaction.response.send_message(embed=embed)


@tree.command(name='botinfo', description='Información del bot')
async def slash_botinfo(interaction: discord.Interaction):
    embed = discord.Embed(title='🤖 Bot Info', color=0x5865F2)
    embed.add_field(name='Versión',    value='3.0',                inline=True)
    embed.add_field(name='Servidores', value=len(bot.guilds),      inline=True)
    embed.add_field(name='Comandos',   value=len(bot.commands),    inline=True)
    embed.add_field(name='Prefijo',    value=f'`{PREFIX}`',        inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name='coinflip', description='Apuesta monedas a cara o sello')
@app_commands.describe(eleccion='cara o sello', apuesta='Monto a apostar')
async def slash_coinflip(interaction: discord.Interaction, eleccion: str, apuesta: int):
    if eleccion.lower() not in ('cara', 'sello'):
        return await interaction.response.send_message('❌ `cara` o `sello`.', ephemeral=True)
    data = cargar_eco()
    uid  = str(interaction.user.id)
    u    = eco_user(data, uid)
    if apuesta < 10 or apuesta > u['balance']:
        return await interaction.response.send_message('❌ Apuesta inválida.', ephemeral=True)
    resultado = random.choice(['cara', 'sello'])
    acierto   = eleccion.lower()[0] == resultado[0]
    if acierto:
        u['balance'] += apuesta
        msg   = f'✅ ¡{resultado.upper()}! +**{apuesta:,}**'
        color = 0x00FF00
    else:
        u['balance'] -= apuesta
        msg   = f'❌ {resultado.upper()}. -**{apuesta:,}**'
        color = 0xFF0000
    guardar_eco(data)
    embed = discord.Embed(title='🪙 Coinflip', description=msg, color=color)
    await interaction.response.send_message(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
# ────────── AYUDA ──────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def _build_ayuda_pages() -> list:
    p = PREFIX
    secciones = [
        ('🛡️', 'AntiNuke — Seguridad',
         f'`{p}antinuke` `{p}an_ayuda` `{p}an_activar` `{p}an_desactivar`\n'
         f'`{p}an_whitelist [@u]` `{p}an_accion` `{p}an_limite` `{p}an_ventana`\n'
         f'`{p}an_antiraid_on/off` `{p}an_antilinks_on/off` `{p}an_antispam_on/off`\n'
         f'`{p}an_antibot_on/off` `{p}an_ver_on/off` `{p}an_snapshot` `{p}an_restore`\n'
         f'Detecta: bans/kicks/roles/canales masivos · roles peligrosos · escalada de perms · cambio de servidor'),
        ('🔒', 'Moderación',
         f'`{p}ban` `{p}unban` `{p}kick` `{p}mute` `{p}unmute`\n'
         f'`{p}softban` `{p}massban` `{p}masskick` `{p}banlist`\n'
         f'`{p}warn` `{p}warns` `{p}clearwarns` `{p}delwarn` `{p}historial`\n'
         f'`{p}limpiar` `{p}limpiar_bots` `{p}limpiar_usuario`\n'
         f'`{p}gag` `{p}ungag` `{p}nick` `{p}massnick`\n'
         f'`{p}fn @u <apodo>` `{p}unfn @u` `{p}fnlist` — Apodo forzado permanente'),
        ('💬', 'Canales y Roles',
         f'`{p}lock/unlock` `{p}lockall/unlockall` `{p}hide/show`\n'
         f'`{p}slowmode` `{p}topic` `{p}rc` `{p}cc` `{p}ec` `{p}clone` `{p}nsfw`\n'
         f'`{p}dar_rol` `{p}quitar_rol` `{p}crear_rol` `{p}eliminar_rol`\n'
         f'`{p}r <id> <rol>` — Dar rol por ID de usuario\n'
         f'`{p}r create <nombre>` — Crear rol nuevo\n'
         f'`{p}listar_roles` `{p}roles_usuario` `{p}rolinfo` `{p}canalinfo`\n'
         f'`{p}v @u` — Dar acceso · `{p}anuncio` `{p}emb`'),
        ('⚙️', 'Configuración del Servidor',
         f'`{p}autorol [@rol]` — Rol automático al unirse\n'
         f'`{p}welcome_set #canal [msg]` — Bienvenida\n'
         f'`{p}farewell_set #canal [msg]` — Despedida\n'
         f'`{p}sticky <msg>` `{p}unsticky` — Mensaje fijo\n'
         f'`{p}rroles_add <msg_id> <emoji> @rol` — Role Reactions\n'
         f'`{p}rroles_remove <msg_id> <emoji>`\n'
         f'`{p}invites [@u]`'),
        ('💰', 'Economía',
         f'`{p}balance [@u]` `{p}daily` `{p}work`\n'
         f'`{p}depositar` `{p}retirar` `{p}transferir @u <monto>`\n'
         f'`{p}slots [apuesta]` `{p}coinflip <cara/sello> <apuesta>`\n'
         f'`{p}leaderboard`'),
        ('⭐', 'Niveles',
         f'`{p}nivel [@u]` `{p}topnivel`\n'
         f'XP automática por mensajes (cooldown 60s) · Notifica al subir de nivel'),
        ('🌐', 'Utilidades',
         f'`{p}ping` `{p}avatar [@u]` `{p}banner [@u]`\n'
         f'`{p}userinfo` `{p}serverinfo` `{p}stats` `{p}botinfo`\n'
         f'`{p}clima <ciudad>` `{p}traducir <idioma> <texto>`\n'
         f'`{p}calcular <expr>` `{p}color <hex>` `{p}buscar <txt>`\n'
         f'`{p}rng [min] [max]` `{p}say <msg>` `{p}say_canal #c <msg>`\n'
         f'`{p}sugerencia` `{p}reporte @u <razon>` `{p}invitar`\n'
         f'`{p}emojis` `{p}recordar <tiempo> <msg>`'),
        ('🎰', 'Juegos y Entretenimiento',
         f'`{p}trivia` `{p}adivina [max]` `{p}acertijo`\n'
         f'`{p}dado [lados]` `{p}dp [n] [lados]` `{p}dp2 1d20 2d6`\n'
         f'`{p}moneda` `{p}ruleta op1 op2...`\n'
         f'`{p}8ball <preg>` `{p}piedra <eleccion>`\n'
         f'`{p}tor [@u]` `{p}sorteo <seg> <premio>`\n'
         f'`{p}encuesta <preg>|op1|op2` `{p}encuesta_si_no <preg>`'),
        ('🎂', 'Cumpleaños y Recordatorios',
         f'`{p}cumple [DD/MM]` `{p}cumple_ver [@u]` `{p}cumples_lista`\n'
         f'`{p}recordar <10m/2h/30s> <msg>`'),
        ('🤝', 'Social y Roleplay',
         f'`{p}casar @u` `{p}aceptar` `{p}rechazar` `{p}divorcio` `{p}pareja [@u]`\n'
         f'`{p}adoptar @u` `{p}familia [@u]`\n'
         f'`{p}horoscopo <signo>` `{p}personalidad [@u]` `{p}compatibilidad @u`\n'
         f'`{p}frase` `{p}chiste` `{p}meme` `{p}fp [personaje]`'),
        ('🐱', 'Anime Acciones',
         f'`{p}abrazar` `{p}pat` `{p}slap` `{p}kiss` `{p}poke`\n'
         f'`{p}cuddle` `{p}bite` `{p}wave` `{p}dance` `{p}cry` `{p}highfive` `{p}blush`\n'
         f'*(Con botón de respuesta interactivo)*'),
        ('⚡', 'Slash Commands',
         '`/ping` `/userinfo` `/serverinfo` `/avatar` `/ban` `/kick` `/mute`\n'
         '`/warn` `/warns` `/purge` `/lock` `/unlock` `/slowmode`\n'
         '`/dar_rol` `/quitar_rol` `/rolinfo` `/balance` `/daily` `/nivel`\n'
         '`/slots` `/coinflip` `/leaderboard` `/8ball` `/clima` `/meme`\n'
         '`/antinuke` `/rng` `/botinfo`'),
    ]
    pages = []
    for i, (emoji, titulo, valor) in enumerate(secciones):
        embed = discord.Embed(title=f'{emoji} {titulo}', description=valor, color=0x5865F2)
        embed.set_footer(text=f'Página {i+1}/{len(secciones)} • Prefijo: {p}')
        pages.append(embed)
    return pages


class AyudaView(discord.ui.View):
    def __init__(self, author_id: int, pages: list):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.pages     = pages
        self.current   = 0
        # Select principal
        opts_main = [
            discord.SelectOption(label='AntiNuke', value='0', emoji='🛡️'),
            discord.SelectOption(label='Moderación', value='1', emoji='🔒'),
            discord.SelectOption(label='Canales/Roles', value='2', emoji='💬'),
            discord.SelectOption(label='Configuración', value='3', emoji='⚙️'),
            discord.SelectOption(label='Economía', value='4', emoji='💰'),
            discord.SelectOption(label='Niveles', value='5', emoji='⭐'),
        ]
        opts_extra = [
            discord.SelectOption(label='Utilidades', value='6', emoji='🌐'),
            discord.SelectOption(label='Juegos', value='7', emoji='🎰'),
            discord.SelectOption(label='Cumpleaños', value='8', emoji='🎂'),
            discord.SelectOption(label='Social', value='9', emoji='🤝'),
            discord.SelectOption(label='Anime', value='10', emoji='🐱'),
            discord.SelectOption(label='Slash Commands', value='11', emoji='⚡'),
        ]
        sel1 = discord.ui.Select(placeholder='📋 Módulos principales', options=opts_main, row=1)
        sel1.callback = self._cb
        self.add_item(sel1)
        sel2 = discord.ui.Select(placeholder='🎮 Módulos extra', options=opts_extra, row=2)
        sel2.callback = self._cb
        self.add_item(sel2)

    async def _guard(self, i: discord.Interaction) -> bool:
        if i.user.id != self.author_id:
            await i.response.send_message('❌ No es tu menú.', ephemeral=True)
            return False
        return True

    async def _cb(self, i: discord.Interaction):
        if not await self._guard(i):
            return
        self.current = int(i.data['values'][0])
        await i.response.edit_message(embed=self.pages[self.current], view=self)

    @discord.ui.button(emoji='⏮️', style=discord.ButtonStyle.secondary, row=0)
    async def btn_first(self, i: discord.Interaction, _):
        if not await self._guard(i): return
        self.current = 0
        await i.response.edit_message(embed=self.pages[0], view=self)

    @discord.ui.button(emoji='◀️', style=discord.ButtonStyle.primary, row=0)
    async def btn_prev(self, i: discord.Interaction, _):
        if not await self._guard(i): return
        self.current = (self.current - 1) % len(self.pages)
        await i.response.edit_message(embed=self.pages[self.current], view=self)

    @discord.ui.button(emoji='🗑️', style=discord.ButtonStyle.danger, row=0)
    async def btn_del(self, i: discord.Interaction, _):
        if not await self._guard(i): return
        await i.message.delete()

    @discord.ui.button(emoji='▶️', style=discord.ButtonStyle.primary, row=0)
    async def btn_next(self, i: discord.Interaction, _):
        if not await self._guard(i): return
        self.current = (self.current + 1) % len(self.pages)
        await i.response.edit_message(embed=self.pages[self.current], view=self)

    @discord.ui.button(emoji='⏭️', style=discord.ButtonStyle.secondary, row=0)
    async def btn_last(self, i: discord.Interaction, _):
        if not await self._guard(i): return
        self.current = len(self.pages) - 1
        await i.response.edit_message(embed=self.pages[self.current], view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


@bot.command(name='ayuda', aliases=['help', 'h', 'comandos'])
async def ayuda(ctx):
    pages = _build_ayuda_pages()
    view  = AyudaView(ctx.author.id, pages)
    await ctx.send(embed=pages[0], view=view)


@tree.command(name='help', description='Muestra el menú de ayuda')
async def slash_help(interaction: discord.Interaction):
    pages = _build_ayuda_pages()
    view  = AyudaView(interaction.user.id, pages)
    await interaction.response.send_message(embed=pages[0], view=view)


# ═══════════════════════════════════════════════════════════════════════════════
# EVENTOS PRINCIPALES
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    log.info(f'Bot conectado: {bot.user} (ID: {bot.user.id})')
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name=f'{PREFIX}ayuda | AntiNuke v3'))
    try:
        synced = await bot.tree.sync()
        log.info(f'Slash commands sincronizados: {len(synced)}')
    except Exception as e:
        log.error(f'Error sincronizando slash: {e}')
    snapshot_loop.start()
    # primer snapshot al arrancar
    await asyncio.sleep(2)
    snaps = cargar_snapshots()
    for guild in bot.guilds:
        snaps[str(guild.id)] = _snapshot_guild(guild)
    guardar_snapshots(snaps)
    log.info(f'Snapshot inicial guardado para {len(bot.guilds)} servidor(es).')


@bot.event
async def on_member_join_welcome(member: discord.Member):
    """Bienvenida y autorol al unirse."""
    # Welcome
    data = cargar_welcome()
    gid  = str(member.guild.id)
    cfg  = data.get(gid, {})
    if cfg.get('canal'):
        canal = member.guild.get_channel(int(cfg['canal']))
        if canal:
            msg = cfg.get('mensaje', '¡Bienvenido {mention} a **{server}**! 🎉').format(
                mention=member.mention, name=member.display_name, server=member.guild.name)
            try:
                await canal.send(msg)
            except Exception:
                pass
    # Autorol
    ar = cargar_autorol()
    rid = ar.get(gid)
    if rid:
        rol = member.guild.get_role(int(rid))
        if rol:
            try:
                await member.add_roles(rol, reason='Autorol')
            except Exception:
                pass


@bot.event
async def on_member_remove_farewell(member: discord.Member):
    """Mensaje de despedida."""
    data = cargar_welcome()
    gid  = str(member.guild.id)
    cfg  = data.get(gid, {})
    if cfg.get('farewell_canal'):
        canal = member.guild.get_channel(int(cfg['farewell_canal']))
        if canal:
            msg = cfg.get('farewell_mensaje', 'Adiós **{name}**.').format(
                name=member.display_name, mention=member.mention, server=member.guild.name)
            try:
                await canal.send(msg)
            except Exception:
                pass


@bot.event
async def on_message_all(message: discord.Message):
    """on_message global: XP + sticky re-post."""
    if message.author.bot or not message.guild:
        return
    # XP
    await on_message_xp(message)
    # Sticky
    data = cargar_sticky()
    gid  = str(message.guild.id)
    cid  = str(message.channel.id)
    cfg  = data.get(gid, {}).get(cid)
    if cfg:
        key = f'{gid}_{cid}'
        if not _sticky_lock.get(key):
            _sticky_lock[key] = True
            try:
                old_id = int(cfg['msg_id'])
                old_msg = await message.channel.fetch_message(old_id)
                await old_msg.delete()
            except Exception:
                pass
            embed = discord.Embed(description=f'📌 {cfg["texto"]}', color=0xFFD700)
            new_msg = await message.channel.send(embed=embed)
            data[gid][cid]['msg_id'] = str(new_msg.id)
            guardar_sticky(data)
            _sticky_lock[key] = False


@bot.event
async def on_raw_reaction_add_all(payload: discord.RawReactionActionEvent):
    """Combina verificación y role-reactions."""
    await on_raw_reaction_add_rroles(payload)


@bot.event
async def on_raw_reaction_remove_all(payload: discord.RawReactionActionEvent):
    await on_raw_reaction_remove_rroles(payload)


@bot.event
async def on_member_update_fn(before: discord.Member, after: discord.Member):
    """Restaura el apodo forzado si el usuario lo cambia."""
    if before.nick == after.nick:
        return
    gid = str(after.guild.id)
    uid = str(after.id)
    apodo = _fn_forzados.get(gid, {}).get(uid)
    if not apodo:
        return
    if after.nick != apodo:
        try:
            await after.edit(nick=apodo, reason='[FN] Restaurando apodo forzado')
        except discord.Forbidden:
            pass


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send('🔒 No tienes permisos para ese comando.')
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send('❌ Miembro no encontrado.')
    elif isinstance(error, commands.RoleNotFound):
        await ctx.send('❌ Rol no encontrado.')
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f'❌ Argumento inválido. Usa `{PREFIX}ayuda`.')
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f'❌ Falta un argumento. Usa `{PREFIX}ayuda`.')
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        log.error(f"Error en '{ctx.command}': {error}\n{traceback.format_exc()}")
        await ctx.send(f'⚠️ Error inesperado: `{error}`')


# ═══════════════════════════════════════════════════════════════════════════════
# INICIO
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    while True:
        try:
            log.info('Iniciando bot v3...')
            bot.run(TOKEN, reconnect=True)
        except discord.LoginFailure:
            log.critical('TOKEN INVÁLIDO.')
            sys.exit(1)
        except KeyboardInterrupt:
            log.info('Detenido por el usuario.')
            sys.exit(0)
        except Exception:
            log.error(f'Error crítico:\n{traceback.format_exc()}')
            log.info('Reiniciando en 5s...')
            time.sleep(5)
