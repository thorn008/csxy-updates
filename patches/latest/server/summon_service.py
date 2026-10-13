"""Persistent tutorial summon and a basic, explicitly simulated battle role."""
import logging
import time

import character_store
from attribute_service import notice
from entity_methods import avatar_client_message
from quest_catalog import catalog
from warrior_codec import encode_message

PAGE = 7
CAPACITY = 12
SID = 3
LOG = logging.getLogger('summon')


def initial_attributes(identifier):
    data = catalog()['summons'][str(identifier)]
    attrs = {'pro' + k: v for k, v in data.get('attrs', {}).items()}
    attrs.update(pro106=1, pro10=data['grow_range'][0],
                 pro100=attrs.get('pro7', 100), pro101=attrs.get('pro8', 100))
    attrs.update({k: 0 for k in ('pro110', 'pro111', 'pro112', 'pro113', 'pro114')})
    now = int(time.time())
    return {'sname': data['name'], 'slife': 6000, 'loy': 90, 'friendship': 100,
            'exp': 0, 'woo': 0, 'potential': 0, 'combatAttr': attrs,
            'trans': False, 'neidan': {}, 'fightRecord': [0, 0], 'statesData': {'states': []},
            'spells': {str(n): 1 for n in data.get('fixed_spells', {}).values()},
            'aptfac': 1, 'aptfix': {}, 'aptnv': {}, 'apttrans': [], 'apt': data['apt'],
            'eat': 0, 'eat_time': now, 'mood_id': 3, 'mood_time': now}


def wire_attributes(attributes):
    result = dict(attributes)
    result['sname'] = attributes['sname'].encode('gbk')
    result['spells'] = {int(n): v for n, v in attributes['spells'].items()}
    # ItemInfo invokes StateInfo.createObjFromDict before Item.__init__;
    # an empty dict raises KeyError('states') and the item never gets inserted.
    # Adapt legacy empty state data on the wire, retaining the saved instance.
    result['statesData'] = dict(attributes.get('statesData', {}))
    result['statesData'].setdefault('states', [])
    return result


def usable(character, item):
    data = catalog()['summons'].get(str(item['id']))
    return (data and item['page'] == PAGE and item['attributes'].get('slife', 0) > 0
            and data.get('req_level', 0) <= character['level']
            and item['attributes'].get('combatAttr', {}).get('pro106', 1) <= character['level'] + 7)


def active_item(character):
    token = character['world_state'].get('active_summon')
    return next((i for i in character['inventory'] if i['uuid'] == token and usable(character, i)), None)


def repair(session):
    """Select the first recovered summon when the legacy save has no selection.

    The native portrait and summon panel both read ``activeSummonIdx``. Older
    quest reward saves contained the page-7 item but omitted the corresponding
    avatar selection, so the client correctly showed an empty portrait even
    though the item had been inserted.
    """
    changed = []

    def change(character):
        if active_item(character) is not None:
            return
        candidate = next((item for item in character['inventory']
                          if usable(character, item)), None)
        if candidate is not None:
            character['world_state']['active_summon'] = candidate['uuid']
            changed.append(True)

    character = character_store.transact(session, change)
    if changed:
        LOG.info('Legacy summon selection repaired and saved: page=7 pos=%d',
                 next(item['pos'] for item in character['inventory']
                      if item['uuid'] == character['world_state']['active_summon']))
    return character


def messages(session, character):
    item = active_item(character) if character else None
    return [avatar_client_message(session, 'update_active_summon', item['pos'] if item else -1)]


def hatch(session, page, pos):
    # Item.initLittleSummon automatically identifies 20154 on the client.
    # New rewards are normalized when granted; retain this old-save path.
    changed = []
    def change(c):
        item = next((i for i in c['inventory'] if (i['page'], i['pos']) == (page, pos)), None)
        if item is None or item['id'] != 20154:
            return
        occupied = {i['pos'] for i in c['inventory'] if i['page'] == PAGE and i is not item}
        free = next((p for p in range(CAPACITY) if p not in occupied), None)
        if free is None:
            raise ValueError('召唤兽栏已满。')
        item.update(id=21154, page=PAGE, pos=free, attributes=initial_attributes(21154))
        changed.append(item)
    try:
        c = character_store.transact(session, change)
    except ValueError as error:
        return [notice(session, str(error))]
    if not changed:
        return []
    from inventory_service import item_info
    return [avatar_client_message(session, 'res_remove', 0, page, pos),
            avatar_client_message(session, 'res_grant', 0, PAGE, CAPACITY),
            avatar_client_message(session, 'res_insert', 0, PAGE, changed[0]['pos'], item_info(changed[0]))]


def activate(session, pos, token):
    if not session.world_ready or getattr(session, 'battle_id', ''):
        return []
    changed = []
    def change(c):
        if pos == -1:
            c['world_state'].pop('active_summon', None)
            changed.append(True)
            return
        item = next((i for i in c['inventory'] if i['page'] == PAGE and i['pos'] == pos
                     and i['uuid'] == token.hex()), None)
        if item and usable(c, item):
            c['world_state']['active_summon'] = item['uuid']
            changed.append(True)
    c = character_store.transact(session, change)
    return messages(session, c) if changed else [notice(session, '请选择可参战的召唤兽。')]


def warrior(item, *, sid=SID, owner=1, x=0.0, y=-2.0):
    data = catalog()['summons'][str(item['id'])]
    attrs = item['attributes']['combatAttr']
    return encode_message('WarriorInfo', {
        'type': 2, 'sid': sid, 'x': x, 'y': y, 'camp': 1,
        'level': attrs['pro106'], 'hp': attrs['pro100'], 'mhp': attrs['pro7'],
        'mp': attrs['pro101'], 'mmp': attrs['pro8'], 'isDead': False,
        'summon': {'summonID': item['id'], 'summonName': item['attributes']['sname'].encode('gbk'),
                   'title': b'', 'owner': owner, 'grow': attrs['pro10'] / 1000.0,
                   'gscolor': 0, 'rupu': 0, 'summonPose': 0, 'aspect': data['aspect']}})


def join_battle(session, pos):
    if not session.world_ready or not getattr(session, 'battle_id', ''):
        return []
    joined = []
    counter_updates = []
    def change(c):
        battle = c['world_state'].get('tutorial_battle', {})
        if (battle.get('id') != session.battle_id or battle.get('phase') != 'input'
                or battle.get('summon_joined')):
            return
        item = next((i for i in c['inventory'] if i['page'] == PAGE and i['pos'] == pos), None)
        if not item or not usable(c, item):
            return
        joined.append(warrior(item))
        battle.update(summon_joined=True, summon_uuid=item['uuid'])
        # 45209 is completed by bringing the summon into the encounter. Send
        # its counter immediately so the task tracker does not wait for the
        # eventual victory callback.
        if c['tasks'].get('45209', {}).get('status') in ('active', 'ready'):
            from quest_service import gain_counters
            counter_updates.extend(gain_counters(c, [(6560, 1)]))
    character = character_store.transact(session, change)
    if not joined or character is None:
        return []
    replies = [avatar_client_message(session, 'battle_add_dolls', joined),
            avatar_client_message(session, 'battle_current_summon', pos, SID),
            avatar_client_message(session, 'battle_action')]
    if counter_updates:
        from quest_service import counter_messages
        replies.extend(counter_messages(session, character, counter_updates))
    return replies


def battle_command(session, action, target=2):
    """Accept the basic pet input; the avatar command resolves the joint turn."""
    if action not in ('attack', 'guard') or not session.world_ready or not getattr(session, 'battle_id', ''):
        return []
    changed = []
    def change(c):
        battle = c['world_state'].get('tutorial_battle', {})
        if (battle.get('id') == session.battle_id and battle.get('phase') == 'input'
                and battle.get('summon_joined') and (action == 'guard' or target == 2)):
            battle['summon_action'] = action
            changed.append(True)
    character_store.transact(session, change)
    replies = [avatar_client_message(session, 'battle_input_ready', SID)] if changed else []
    if changed:
        from battle_service import resolve_pending
        replies.extend(resolve_pending(session))
    return replies
