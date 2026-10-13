"""Durable solo revival, using native isDead/die messages without death penalties.

Called inside character transactions. No session-global pending state can escape
a failed save/encoding operation. Countdown starts after battle_end.
"""
import math
import time

STATE_KEY = 'solo_death'
DEATH_BANNER = 'CSXY-DEATHFLOW-v2'


def number(key, default, low, high):
    from hot_control import value
    try:
        result = float(value(key, str(default)))
        return max(low, min(high, result)) if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def enabled():
    from hot_control import flag
    return flag('deathflow', '1')


def is_dead_property(session, dead):
    from entity_methods import avatar_property_message
    from hot_control import flag
    return avatar_property_message(session, 'isDead', bool(dead)) if flag('isdead', '1') else b''


def revive_dialog(session, lock_time=0):
    from entity_methods import avatar_client_message
    return avatar_client_message(session, 'die', 0, 0, True, False, False, int(lock_time))


def begin(session, character, why='battle', now=None):
    from attribute_service import vitals_message
    now = time.time() if now is None else now
    character['combat_attr']['pro100'] = 0
    seconds = number('deathsec', 5, 1, 300)
    character['world_state'][STATE_KEY] = {'until': now + seconds, 'why': why}
    return [body for body in (is_dead_property(session, True),
                             revive_dialog(session, math.ceil(seconds)),
                             vitals_message(session, character)) if body]


def recover_on_login(character, force=False):
    from attribute_service import combat_attributes
    pending = character['world_state'].get(STATE_KEY)
    if pending is None and not force:
        return False
    attrs = combat_attributes(character)
    character['combat_attr']['pro100'] = max(1, int(attrs['pro7'] * number('deathpct', 50, 1, 100) / 100))
    character['world_state'].pop(STATE_KEY, None)
    return True


def tick(session, now=None):
    import character_store
    from attribute_service import message
    if (not getattr(session, 'world_ready', False) or getattr(session, 'superseded', False)
            or getattr(session, 'battle_id', '')):
        return []
    now = time.time() if now is None else now
    # Avoid loading/locking all accounts twice per idle tick.
    from battle_service import selected
    current = selected(session)
    pending = current.get('world_state', {}).get(STATE_KEY) if current else None
    if not pending or (enabled() and now < pending['until']):
        return []
    replies = []
    def change(character):
        entry = character['world_state'].get(STATE_KEY)
        if not entry or (enabled() and now < entry['until']):
            return
        recover_on_login(character)
        replies.extend(body for body in (is_dead_property(session, False), message(session, character)) if body)
        battle = character['world_state'].get('tutorial_battle', {})
        from consumable_service import settle_after_battle
        from mount_service import restore_after_battle
        replies.extend(settle_after_battle(session, character, battle))
        replies.extend(restore_after_battle(session, character, battle))
    character_store.transact(session, change)
    return replies


def status():
    return {'banner': DEATH_BANNER, 'enabled': enabled(),
            'deathsec': number('deathsec', 5, 1, 300), 'deathpct': number('deathpct', 50, 1, 100)}
