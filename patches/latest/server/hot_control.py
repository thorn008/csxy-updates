"""Local runtime configuration and diagnostic command/status files.

Configuration reloads on changes. Commands execute only after changed content;
first observation registers old content without replay. Only status/say supported."""
import json
import logging
import os
import time
from pathlib import Path

from csxy_protocol import ROOT

LOG = logging.getLogger('hot')

HOT_CONTROL_BANNER = 'CSXY-HOTCONTROL-v2'

STATE = ROOT / 'server' / 'state'
CONFIG_PATH = STATE / 'hot_config.txt'
CMD_PATH = STATE / 'hot_cmd.txt'
STATUS_PATH = STATE / 'hot_status.json'

DEFAULTS = {'chatbm':'1','chatns':'0','isdead':'1','panel':'1',
            'deathflow':'1','deathsec':'5','deathpct':'50','repair':'1',
            'monstats':'1','monhpk':'0.35','mondmgk':'0.25','moncap':'200',
            'unitinput':'0','unitwait':'8'}

_cache = {'mtime': None, 'cfg': None}
_cmd_last = {'text': None, 'first': True}
_stats = {'cmds': 0, 'config_reads': 0, 'started': time.time()}


def _read_text(path):
    """读文本：优先 UTF-8（自动剥 BOM），失败回退 GBK。

    必要性（实测）：PowerShell `Set-Content -Encoding UTF8` 会写 BOM ⇒
    不剥就会得到 `'\\ufeffstatus'` 这样的动词（真机日志实锤）。
    面板/用户也可能用 GBK 编辑器改热控文件 ⇒ 必须双向兼容。
    """
    raw = Path(path).read_bytes()
    for encoding in ('utf-8-sig', 'gbk'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace')


def config():
    """现读热控配置（按 mtime 缓存文件读，但每次调用都重解析语义 = 改动即时生效）。"""
    try:
        if CONFIG_PATH.exists():
            mtime = (CONFIG_PATH.stat().st_mtime_ns, CONFIG_PATH.stat().st_size)
            if _cache['mtime'] != mtime:
                raw = _read_text(CONFIG_PATH)
                parsed = dict(DEFAULTS)
                # 逐行处理：先剥 `#` 注释（否则注释里的 key=value 会被误当配置）
                for line in raw.splitlines():
                    line = line.split('#', 1)[0]
                    for token in line.split():
                        if '=' in token:
                            key, _, value = token.partition('=')
                            key = key.strip()
                            if key:
                                parsed[key] = value.strip()
                _cache['mtime'] = mtime
                _cache['cfg'] = parsed
                _stats['config_reads'] += 1
                changed = {k: v for k, v in parsed.items() if DEFAULTS.get(k) != v}
                LOG.info('hot_config reloaded: %d keys, non-default=%s', len(parsed), changed or '{}')
        else:
            _cache.update(mtime=None, cfg=dict(DEFAULTS))
        if _cache['cfg'] is None:
            _cache['cfg'] = dict(DEFAULTS)
    except Exception:
        LOG.exception('hot_config read failed; keeping last good values')
        if _cache['cfg'] is None:
            _cache['cfg'] = dict(DEFAULTS)
    return _cache['cfg']


def flag(name, default='1'):
    """取布尔型热控键；写 `key=0` 即关。"""
    return str(config().get(name, default)).strip() != '0'


def value(name, default=''):
    return str(config().get(name, default)).strip()


def ensure_files():
    """首次运行时落一份带注释的缺省配置（不覆盖已有文件）。"""
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        if not CONFIG_PATH.exists():
            CONFIG_PATH.write_text(
                '# hy3a 热控配置（空格分隔的 key=value；改动即生效，无需重启）\n'
                + ' '.join('%s=%s' % (k, v) for k, v in sorted(DEFAULTS.items())) + '\n',
                'utf-8')
        if not CMD_PATH.exists():
            CMD_PATH.write_text('', 'utf-8')
    except Exception:
        LOG.exception('hot_control ensure_files failed')


def poll_commands(handler, session=None):
    """`hot_cmd.txt` 内容变化即执行一次 `handler(text, session)`。

    语义与 hy3a 线 `_v18_lvl_cmd.txt` 一致：**只认变化**（首见只登记不执行，
    防止服务端重启后把上一次的命令重放一遍）。
    """
    try:
        if not CMD_PATH.exists():
            return None
        text = _read_text(CMD_PATH).strip().lstrip('\ufeff')
        if _cmd_last['first']:
            _cmd_last['first'] = False
            _cmd_last['text'] = text
            LOG.info('hot_cmd first sight registered (len=%d)', len(text))
            return None
        if text == _cmd_last['text']:
            return None
        _cmd_last['text'] = text
        if not text:
            return None
        # ★面板写的是「注释头 + 命令」多行格式（真机实测）：
        #       # panel 2026-10-13 19:21:05 #1
        #       equip 170184
        #   原先把第一行当动词 ⇒ 得到 '# panel ...' ⇒ unknown verb ⇒ 面板按钮被静默忽略。
        #   现在逐行处理：整行以 # 开头视为注释跳过；其余**逐条**执行（某条失败不影响后续）。
        results = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            _stats['cmds'] += 1
            LOG.info('hot_cmd execute #%d: %s', _stats['cmds'], line[:200])
            try:
                results.append(handler(line, session))
            except Exception:
                LOG.exception('hot_cmd 单条执行失败（继续后续）: %s', line[:120])
        return results
    except Exception:
        LOG.exception('hot_cmd failed')
        return None


def write_status(**extra):
    """每 tick 覆写状态文件（面板据此显示服务端实时状态）。"""
    try:
        if not flag('panel'):
            return
        payload = {
            'banner': HOT_CONTROL_BANNER,
            'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'uptime': round(time.time() - _stats['started'], 1),
            'commands': _stats['cmds'],
            'config_reads': _stats['config_reads'],
        }
        payload.update(config())
        payload.update(extra)
        tmp = STATUS_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), 'utf-8')
        os.replace(tmp, STATUS_PATH)
    except Exception:
        LOG.exception('hot_status write failed')
