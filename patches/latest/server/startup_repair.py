"""Recover validated saves under the server lifetime lease, before listeners start."""
from pathlib import Path
from types import SimpleNamespace
import logging
import uuid

import character_store

LOG = logging.getLogger('repair')
REPAIR_BANNER = 'CSXY-STALEBATTLE-v2'


def repair():
    from hot_control import flag
    from battle_service import interrupt_on_login, ACTIVE_PHASES, STATE_KEY
    from death_service import STATE_KEY as DEATH_KEY
    if not flag('repair', '1'):
        return 0
    state = character_store.load()  # Checksum/schema verification; corruption blocks startup.
    sessions = []
    for account, characters in state.items():
        for character in characters:
            world = character['world_state']
            if world.get(STATE_KEY, {}).get('phase') in ACTIVE_PHASES or DEATH_KEY in world:
                sessions.append(SimpleNamespace(username=bytes.fromhex(account),
                    selected_character=character['dbid'], superseded=False, battle_id='', battle_due=0))
    if sessions:
        path = character_store.PATH
        backup = path.with_name(path.name+'.pre_repair.'+uuid.uuid4().hex)
        backup.write_bytes(path.read_bytes())
        for session in sessions:
            interrupt_on_login(session)  # Preserve rewards; settle reservoirs and restore mount once.
        LOG.info('%s recovered=%d backup=%s', REPAIR_BANNER, len(sessions), backup.name)
    return len(sessions)
