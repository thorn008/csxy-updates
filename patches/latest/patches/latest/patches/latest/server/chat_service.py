"""Native solo channel echo using the selected character identity.

Reference: hy3a channel-routing submission; this is local echo, not network broadcast."""
import logging

from csxy_protocol import ROOT  # noqa: F401  (保持与本包其它模块一致的导入面)

LOG = logging.getLogger('chat')

CHAT_BANNER = 'CSXY-CHATCHANNEL-v2'

# 上行 BaseMethod 名 -> 下行 ClientMethod 名
BASE_ROUTES = {
    'chatToWorld': 'show_msg_world',
    'chatToTeam': 'show_msg_team',
    'chatToSchool': 'show_msg_school',
    'chatToClan': 'show_msg_clan',
    'chatToTop': 'show_msg_top',
    'chatToFuben': 'show_msg_fuben',
    'chatToView': 'show_msg_view',      # 名字路由（若走 Base 通道也能接住）
}

# 玩家 CellMethods 槽 -> 频道（`record_0214.xml` CellMethods 暴露序；hy3a 线实测 wire=0x81）
CELL_ROUTES = {
    1: 'show_msg_view',
}

# 末参为 BOOL 的频道（领袖/成员标记）
BOOL_CHANNELS = {'show_msg_clan', 'show_msg_fuben'}

_FIRST = {'logged': False}


def _flag(name, default='1'):
    try:
        from hot_control import flag
        return flag(name, default)
    except Exception:
        return str(default).strip() != '0'


def speaker(session):
    """当前会话的说话人 (name_bytes, school_int)。

    `character_store` 里 `name` 是 **hex 编码的 GBK 字节**、`physique` 是 5 字节 hex
    （`battle_service.py` 的 `bytes.fromhex(character['name'])` 同口径）。
    """
    try:
        from character_store import characters
        character = next((c for c in characters(session)
                          if c['dbid'] == session.selected_character), None)
        if character is None:
            return None, None
        name = bytes.fromhex(character.get('name') or '')
        school = bytes.fromhex(character.get('physique') or '0100000000')[0]
        return name, int(school)
    except Exception:
        LOG.exception('chat speaker lookup failed')
        return None, None


def _decode_message(arguments):
    """上行 MESSAGE（BLOB：长度前缀字节串）-> GBK 文本。"""
    if not arguments:
        return ''
    if len(arguments) > 4100:
        return ""
    size = arguments[0]
    offset = 1
    if size == 255:
        if len(arguments) < 4:
            return ''
        size = int.from_bytes(arguments[1:4], 'little')
        offset = 4
    if size > 4096 or offset + size != len(arguments):
        return ""
    raw = arguments[offset:offset + size]
    for encoding in ('gbk', 'utf-8'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode('gbk', 'replace')


def announce(session, channel, text, character=None, leader=False, name_override=None):
    """构造下行回显帧（返回 body 列表；空列表表示被热控关掉或参数不合法）。"""
    from entity_methods import avatar_client_message
    if not _flag('chatbm', '1'):
        LOG.info('chat suppressed by chatbm=0 channel=%s len=%d', channel, len(text or ''))
        return []
    if channel not in ('show_msg_view', 'show_msg_team', 'show_msg_school', 'show_msg_clan',
                       'show_msg_world', 'show_msg_top', 'show_msg_fuben', 'show_msg_yell'):
        LOG.warning('chat unknown channel: %s', channel)
        return []
    if name_override is not None:
        name, school = name_override
    else:
        name, school = speaker(session)
    if name is None:
        return []
    if _flag('chatns', '0'):
        name = b''
    message = (text or '').encode('gbk', 'replace')[:4096]
    arguments = [name, int(school), message]
    if channel in BOOL_CHANNELS:
        arguments.append(bool(leader))
    try:
        body = avatar_client_message(session, channel, *arguments)
    except Exception:
        LOG.exception('chat build failed channel=%s (def 缺该方法或参数不符)', channel)
        return []
    if not _FIRST['logged']:
        _FIRST['logged'] = True
        LOG.info('★%s 首次回显成功: channel=%s speaker=%r school=%d bytes=%d',
                 CHAT_BANNER, channel, name, school, len(message))
    return [body]


def on_base(session, name, arguments):
    """上行 BaseMethod 分发；**非聊天返回 None**（交回调用方的原链，fail-closed）。"""
    channel = BASE_ROUTES.get(name)
    if channel is None:
        return None
    if not getattr(session, 'world_ready', False) or getattr(session, 'superseded', False):
        return []
    text = _decode_message(arguments)
    if not text:
        LOG.info('chat base %s with empty message ignored', name)
        return []
    LOG.info('chat base %s -> %s text=%r', name, channel, text)
    leader = name == 'chatToClan'
    return announce(session, channel, text, leader=leader)


def on_cell(session, entity_id, slot, arguments):
    """上行 CellMethod（玩家自己的 cell）；**不认则返回 []** 交回原链。"""
    if (entity_id != session.entity_id or not getattr(session, 'world_ready', False)
            or getattr(session, 'superseded', False)):
        return []
    channel = CELL_ROUTES.get(slot)
    if channel is None:
        return []
    text = _decode_message(arguments)
    if not text:
        return []
    LOG.info('chat cell slot=%d entity=%d -> %s text=%r', slot, entity_id, channel, text)
    return announce(session, channel, text)


def status():
    return {'banner': CHAT_BANNER, 'base_routes': sorted(BASE_ROUTES),
            'cell_routes': {str(k): v for k, v in CELL_ROUTES.items()},
            'enabled': _flag('chatbm', '1'), 'first_echo': _FIRST['logged']}
