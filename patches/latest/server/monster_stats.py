"""Local client monster identities with configurable simulator scaling.

The native client has identity/level data, not official server combat formulas.
Submitted identities were checked and regenerated from this build of entities.bag."""
import math
import json
import logging

from csxy_protocol import ROOT

LOG = logging.getLogger('monstats')

MONSTATS_BANNER = 'CSXY-MONSTATS-v2'

INDEX_PATH = ROOT / 'server' / 'data' / 'monster_index.json'

_cache = {'rows': None, 'loaded': False, 'stats': {'hits': 0, 'misses': 0}}


def _cfg(name, default):
    try:
        from hot_control import value
        got = value(name, str(default))
        return got if got != '' else str(default)
    except Exception:
        return str(default)


def _flag(name, default='1'):
    try:
        from hot_control import flag
        return flag(name, default)
    except Exception:
        return str(default).strip() != '0'


def _num(name, default):
    try:
        value = float(_cfg(name, default))
        return value if math.isfinite(value) and value >= 0 else float(default)
    except (TypeError, ValueError):
        return float(default)


def enabled():
    return _flag('monstats', '1')


def rows():
    if not _cache['loaded']:
        _cache['loaded'] = True
        try:
            payload = json.loads(INDEX_PATH.read_text('utf-8'))
            _cache['rows'] = payload.get('rows') or {}
            LOG.info('★%s 已载入怪物等级表: %d 条 (来源: %s)',
                     MONSTATS_BANNER, len(_cache['rows']), INDEX_PATH.name)
        except Exception:
            LOG.exception('monster_index.json 载入失败；回退参考实现常量')
            _cache['rows'] = {}
    return _cache['rows']


def entry(template_id):
    return rows().get(str(template_id))


def level(template_id, default=None):
    """怪物**自己的**等级（179 权威）；查不到返回 default（调用方回退玩家等级）。"""
    row = entry(template_id)
    if row and row.get('level'):
        _cache['stats']['hits'] += 1
        return int(row['level'])
    _cache['stats']['misses'] += 1
    return default


def display_name(template_id):
    row = entry(template_id)
    if not row:
        return None
    try:
        return bytes.fromhex(row.get('name_gbk') or '').decode('gbk')
    except Exception:
        return None


def scale(template_id, base, slope_key, default_slope):
    """按怪物等级缩放一个基础值。"""
    if not enabled():
        return base
    got = level(template_id, None)
    if got is None:
        return base
    cap = max(1, min(255, int(_num('moncap', 200))))
    got = max(1, min(cap, got))
    slope = min(10.0, _num(slope_key, default_slope))
    return base * (1.0 + slope * (got - 1))


def enemy_hp(template_id, base):
    return scale(template_id, base, 'monhpk', 0.35)


def enemy_damage(template_id, base):
    return scale(template_id, base, 'mondmgk', 0.25)


def status():
    return {'banner': MONSTATS_BANNER, 'enabled': enabled(),
            'rows': len(rows()), 'lookups': dict(_cache['stats']),
            'monhpk': _cfg('monhpk', 0.35), 'mondmgk': _cfg('mondmgk', 0.25)}
