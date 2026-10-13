"""Offline party companions, using native warriors and shared skills.

Companions rest before each encounter and scale to the owner's level. These
are explicit single-player rules, not recovered original server balance.
"""
import copy
import uuid

import character_store
from attribute_service import combat_attributes, notice
from entity_methods import avatar_client_message
from warrior_codec import encode_message

SID = 4  # Player=1, enemy=2, existing summon=3.
ENTITY_ID = 1900000001  # Shared scene/team/battle identity, never a real account.
MAX_COMPANIONS = 4  # The owner occupies the fifth native party slot.
PET_SID = 8  # Companion avatars occupy 4..7; player's summon remains 3.
PET_TEMPLATE = 21153
TEMPLATES = {
    'fighter': {'name': '护行武者', 'school': 4, 'skill': 1208},
    'healer': {'name': '济世医者', 'school': 2, 'skill': 1143},
    'immortal': {'name': '仙家道者', 'school': 1, 'skill': 1127},
    'scholar': {'name': '文士谋者', 'school': 3, 'skill': 1177},
    'sky_demon': {'name': '天魔术者', 'school': 5, 'skill': 1257},
    'earth_demon': {'name': '地魔灵者', 'school': 6, 'skill': 1290},
    'asura': {'name': '修罗战者', 'school': 7, 'skill': 1306},
    'rakshasa': {'name': '罗刹影者', 'school': 8, 'skill': 1338},
}


def collection(character):
    data = character.get('world_state', {}).get('companions', {})
    if not isinstance(data, dict) or data.get('version', 1) not in (1, 2):
        raise ValueError('伙伴存档版本不受支持。')
    owned = data.get('owned', {})
    if not isinstance(owned, dict) or any(
            not isinstance(row, dict) or row.get('template', key) not in TEMPLATES
            or not isinstance(row.get('id'), str) or not row['id']
            for key, row in owned.items()):
        raise ValueError('伙伴存档记录异常。')
    active = data.get('active')
    if active is not None and active not in owned:
        raise ValueError('出战伙伴记录异常。')
    owned = copy.deepcopy(owned)
    if len(owned) > MAX_COMPANIONS:
        raise ValueError('伙伴数量超过队伍上限。')
    for index, (key, row) in enumerate(owned.items()):
        row.setdefault('template', key)
        row.setdefault('slot', index + 1)
    slots = [r['slot'] for r in owned.values()]
    if len(set(slots)) != len(slots) or any(type(s) is not int or not 1 <= s <= 4 for s in slots):
        raise ValueError('伙伴队伍位置异常。')
    return {'version': 2, 'owned': owned, 'active': active}


def ready(session, character):
    from mount_service import ready as idle
    return idle(session, character)


def actor_for(character, key):
    template = TEMPLATES[key]
    actor = {'level': character['level'], 'physique': bytes(
        [template['school'], 2, 1, 2, 3]).hex(), 'combat_attr': {},
        'equipment': {}, 'aspect': bytes(52).hex(), 'world_state': {},
        'name': template['name'].encode('gbk').hex()}
    # Use the same level formulas, with modest simulator base offence.
    actor['combat_attr'].update(pro31=8, pro41=12)
    from skill_service import reconcile
    reconcile(actor)
    from companion_loadout import equip
    equip(actor)
    attrs = combat_attributes(actor)
    actor['combat_attr'].update(pro100=attrs['pro7'], pro101=attrs['pro8'])
    return actor


def prepare(character, encounter):
    """First member retained for compatibility with existing battle saves."""
    party = prepare_all(character, encounter)
    return party[0] if party else None


def prepare_all(character, encounter):
    data = collection(character)
    from quest_catalog import definition
    q = definition(encounter.get('quest', 0)) or {}
    if any(req.get('max_teamsize', 5) < 2 for req in
           (q.get('req', {}), q.get('cq', {}).get('req', {}))):
        return []
    result = []
    for key, row in data['owned'].items():
        actor = actor_for_member(character, row)
        attrs = combat_attributes(actor)
        attrs['pro100'], attrs['pro101'] = attrs['pro7'], attrs['pro8']
        companion = {'id': row['id'], 'key': key, 'template': row['template'],
            'sid': SID + row['slot'] - 1, 'entity_id': ENTITY_ID + row['slot'] - 1,
            'team_pos': len(result) + 2, 'actor': actor, 'attrs': attrs, 'cooldowns': {},
            'policy': 'offline-companion-team-v3', 'last_action': None}
        companion['pet'] = prepare_pet(actor['level'], companion['sid'], row['slot'])
        result.append(companion)
    return result


def actor_for_member(character, row):
    actor = actor_for(character, row['template'])
    actor['name'] = (TEMPLATES[row['template']]['name'] + str(row['slot'])).encode('gbk').hex()
    return actor


def members(battle):
    return ([battle['companion']] if battle.get('companion') else []) + battle.get('other_companions', [])


def patient(battle, sid):
    return next((c['attrs'] for c in members(battle) + pets(battle) if c['sid'] == sid), None)


def prepare_pet(level, owner, slot):
    from summon_service import initial_attributes
    from summon_progression import level_attributes
    item = {'id': PET_TEMPLATE, 'attributes': initial_attributes(PET_TEMPLATE)}
    item['attributes']['combatAttr']['pro106'] = level
    level_attributes(item)
    return {'sid': PET_SID + slot - 1, 'owner': owner, 'item': item,
            'attrs': item['attributes']['combatAttr'], 'last_action': None}


def pets(battle):
    # Old in-progress battles have no pet record and continue unchanged.
    return [c['pet'] for c in members(battle) if c.get('pet')]


def pet_warrior(companion):
    from summon_service import warrior as summon_warrior
    pet = companion['pet']
    x = companion.get('x', (-2.0, 2.0, -4.0, 4.0)[companion.get('team_pos', 2) - 2])
    item = dict(pet['item'], attributes=dict(pet['item']['attributes'], combatAttr=pet['attrs']))
    return summon_warrior(item, sid=pet['sid'], owner=companion['sid'],
                          x=x, y=companion.get('y', -4.0) + 2.0)


def warrior(companion):
    from equipment_service import aspect_info
    actor, attrs = companion['actor'], companion['attrs']
    return encode_message('WarriorInfo', {
        'type': 1, 'sid': companion['sid'], 'camp': 1,
        'x': companion.get('x', (-2.0, 2.0, -4.0, 4.0)[companion.get('team_pos', 2) - 2]),
        'y': companion.get('y', -4.0),
        'level': actor['level'], 'hp': attrs['pro100'], 'mhp': attrs['pro7'],
        'mp': attrs['pro101'], 'mmp': attrs['pro8'], 'isDead': False,
        'avatar': {'aspect': aspect_info(actor),
                   'physique': dict(zip(('school', 'sex', 'facestyle', 'hairstyle', 'haircolor'),
                                        bytes.fromhex(actor['physique']))),
                   'roleName': bytes.fromhex(actor['name']), 'entityID': companion.get('entity_id', ENTITY_ID),
                   'gscolor': 0}})


def manage(session, op, key=None):
    """Ownership/selection transaction; never purchase, consume or touch quests."""
    from battle_service import selected
    current = selected(session)
    if not current or not ready(session, current):
        return [notice(session, '请在场景加载和战斗结束后管理伙伴。')]
    replies = []
    scene_plan = []

    def change(character):
        if not ready(session, character):
            raise ValueError('当前不能管理伙伴。')
        previous = copy.deepcopy(character)
        data = collection(character)
        if op == 'recruit':
            if key not in TEMPLATES:
                raise ValueError('没有这名伙伴。')
            if len(data['owned']) >= MAX_COMPANIONS:
                raise ValueError('队伍已满（5人），请先解除一名伙伴的招募。')
            template = key
            identifier = key if key not in data['owned'] else template + '_' + uuid.uuid4().hex[:8]
            slot = next(s for s in range(1, 5) if s not in [r['slot'] for r in data['owned'].values()])
            data['owned'][identifier] = {'id': uuid.uuid4().hex, 'template': template, 'slot': slot}
            data['active'] = next(iter(data['owned']))
            # Validate its original protobuf before publishing selection.
            character['world_state']['companions'] = data
            for comp in prepare_all(character, {}):
                warrior(comp)
                pet_warrior(comp)
            text = TEMPLATES[template]['name'] + '已加入队伍，将自动参战。'
        elif op == 'dismiss':
            if key not in data['owned']:
                raise ValueError('尚未招募这名伙伴。')
            removed = data['owned'].pop(key)
            data['active'] = next(iter(data['owned']), None)
            text = '已解除' + TEMPLATES[removed['template']]['name'] + '的招募。'
        elif op in ('activate', 'rest'):
            raise ValueError('已招募伙伴全部参战；请用解除招募腾出队伍位置。')
        else:
            raise ValueError('未知伙伴操作。')
        replies.append(notice(session, text, chat=True))
        character['world_state']['companions'] = data
        from team_service import messages, state as team_state
        party_state = team_state(character)
        if not data['owned']:
            party_state['array'] = 0
        character['world_state']['offline_team'] = party_state
        replies.extend(messages(session, character, previous))
        from companion_scene import prepare
        scene_plan.append(prepare(session, character))

    try:
        result = character_store.transact(session, change)
    except ValueError as error:
        return [notice(session, str(error))]
    if result and scene_plan:
        from companion_scene import apply
        replies.extend(apply(session, scene_plan[0]))
    return replies if result else []


def menu(session, npc, key=None, section=None, page=0):
    from quest_offline import current, dialogue, NAV_PREVIOUS, NAV_NEXT, NAV_RETURN
    character = current(session)
    if not character:
        return []
    data = collection(character)
    template = data['owned'].get(key, {}).get('template', key)
    back_page = list(TEMPLATES).index(template) // 2 if template in TEMPLATES else 0
    back = ('返回伙伴列表', {'op': 'companion_menu', 'page': back_page})
    if section == 'help':
        return dialogue(session, npc,
            '招募即入队，人物最多5人。#r伙伴同级，自动选配装备。#r每人携带小海龙，单人战不加入。', [back])
    if section == 'owned':
        rows = list(data['owned'].items())
        pages = max(1, (len(rows) + 1) // 2)
        page = max(0, min(int(page), pages - 1))
        options = [(TEMPLATES[row['template']]['name'] + str(row['slot']),
                    {'op': 'companion_menu', 'section': 'member', 'key': identifier})
                   for identifier, row in rows[page * 2:page * 2 + 2]]
        if pages > 1:
            options.append(('下一页' if page == 0 else '上一页',
                {'op': 'companion_menu', 'section': 'owned', 'page': 1 - page}))
        options.append(back)
        return dialogue(session, npc, '队伍 %d/5人 · %d/%d页' % (1 + len(rows), page + 1, pages), options)
    if section in ('member', 'dismiss') and key in data['owned']:
        row = data['owned'][key]
        name = TEMPLATES[row['template']]['name'] + str(row['slot'])
        if section == 'dismiss':
            return dialogue(session, npc, '确认解除%s？#r以后可以免费重新招募。' % name, [
                ('确认解除招募', {'op': 'companion', 'command': 'dismiss', 'key': key}),
                ('取消', {'op': 'companion_menu', 'section': 'member', 'key': key})])
        return dialogue(session, npc, name + ' · %d级#r携带小海龙，自动配装。' % character['level'], [
            ('解除招募', {'op': 'companion_menu', 'section': 'dismiss', 'key': key}),
            ('返回已招募伙伴', {'op': 'companion_menu', 'section': 'owned'})])
    if key in TEMPLATES:
        name = TEMPLATES[key]['name']
        text = name + ' · %d级#r同级小海龙，自动配装。#r' % character['level'] + (
            '优先救急，21级可持续治疗。' if key == 'healer' else
            '攻击敌人，持久战增加攻击。' if key == 'fighter' else
            '按战况自动使用本流派技能。')
        options = []
        if len(data['owned']) < MAX_COMPANIONS:
            options.append(('免费招募并入队', {'op': 'companion', 'command': 'recruit', 'key': key}))
        else:
            text += '#r队伍已满，请先解除招募。'
        if key in data['owned']:
            options.append(('解除招募', {'op': 'companion_menu', 'section': 'dismiss', 'key': key}))
        options.append(back)
        return dialogue(session, npc, text, options)
    # Two role rows + management + one native horizontal navigation row.
    rows = list(TEMPLATES.items())
    pages = (len(rows) + 1) // 2
    page = max(0, min(int(page), pages - 1))
    text = '队伍%d/5人 · %d/%d页#r同级伙伴与小海龙，自动配装。' % (
        1 + len(data['owned']), page + 1, pages)
    options = [(template['name'], {'op': 'companion_menu', 'key': identifier})
               for identifier, template in rows[page * 2:page * 2 + 2]]
    options.append(('已招募伙伴', {'op': 'companion_menu', 'section': 'owned'}))
    option_ids = list(range(len(options)))
    if page:
        options.append(('上一页', {'op': 'companion_menu', 'page': page - 1}))
        option_ids.append(NAV_PREVIOUS)
    if page < pages - 1:
        options.append(('下一页', {'op': 'companion_menu', 'page': page + 1}))
        option_ids.append(NAV_NEXT)
    options.append(('返回', {'op': 'menu', 'section': 'main'}))
    option_ids.append(NAV_RETURN)
    return dialogue(session, npc, text, options, option_ids)


def active_state(battle, sid, identifier):
    return any(s['sid'] == sid and s['id'] == identifier and s['expires'] >= battle['round']
               for s in battle.get('skill_states', []))


def skill_cost(companion, battle, identifier):
    """Return legal resource cost, including learned rank, level and cooldown."""
    from skill_service import rank, learned, eligible, effective_attributes
    actor, attrs = companion['actor'], companion['attrs']
    row = rank(identifier, learned(actor).get(str(identifier), 0))
    try:
        eligible(actor, row)
    except ValueError:
        return None
    effective = effective_attributes(attrs, battle, companion['sid'])
    consume = row['spell']['consume']
    mp = max(0, int((consume['mp'] + consume['fix'] * actor['level']) * effective['pro73']))
    hp = max(0, int(consume.get('hp', 0) * effective['pro89']))
    if (attrs['pro101'] < mp or attrs['pro100'] <= hp
            or companion['cooldowns'].get(str(identifier), 0) > battle['round']):
        return None
    return mp


def allies(character, battle, player_attrs):
    patients = [(1, player_attrs)] + [(c['sid'], c['attrs']) for c in members(battle) + pets(battle)]
    if battle.get('summon_joined'):
        from summon_progression import participant
        pet = participant(character, battle)
        if pet:
            patients.append((3, pet['attributes']['combatAttr']))
    return [(sid, a) for sid, a in patients if a.get('pro100', 0) > 0]


def choose_skill(character, battle, player_attrs, comp=None):
    """One deterministic decision per turn; use only recovered original spells."""
    from battle_service import physical_damage
    from skill_service import effective_attributes
    from warrior_codec import catalog
    comp = comp or battle['companion']
    attrs = comp['attrs']
    available = lambda i: skill_cost(comp, battle, i)
    effective = effective_attributes(attrs, battle, comp['sid'])
    enemy = effective_attributes({}, battle, 2)
    basic = physical_damage(attrs, effective, enemy)
    patients = allies(character, battle, player_attrs)
    if comp['template'] == 'healer':
        hurt = [(sid, a) for sid, a in patients if a['pro100'] < a['pro7']]
        threatened_sid, threatened = enemy_target(battle, player_attrs)
        rules = catalog()['simulator_rules']
        enemy_base = {'pro31': rules['enemy_damage']}
        danger = physical_damage(enemy_base, dict(enemy_base,
            pro31=rules['enemy_damage'] + enemy.get('pro31', 0)),
            effective_attributes(threatened, battle, threatened_sid))
        # Estimate without guard: safe even when the owner changes commands.
        urgent = [(sid, a) for sid, a in hurt if a['pro100'] <= a['pro7'] * .35
                  or (sid == threatened_sid and a['pro100'] <= danger * 2)]
        if urgent and available(1143) is not None:
            sid, a = min(urgent, key=lambda p: (p[0] != threatened_sid or p[1]['pro100'] > danger,
                                              p[1]['pro100'] / p[1]['pro7'], p[0] != 1))
            return 1143, sid, a, 'emergency_heal'
        # Moderate injuries in a continuing battle benefit from a healing state.
        reserve = available(1143)
        hot = available(1145)
        candidates = [(sid, a) for sid, a in hurt if a['pro100'] <= a['pro7'] * .85
                      and not active_state(battle, sid, 5016)]
        if (candidates and hot is not None and reserve is not None
                and attrs['pro101'] >= hot + reserve and battle['enemy_hp'] > basic * 2):
            sid, a = min(candidates, key=lambda p: (p[1]['pro100'] / p[1]['pro7'], p[0] != 1))
            return 1145, sid, a, 'sustained_heal'
        candidates = [(sid, a) for sid, a in hurt if a['pro100'] < a['pro7'] * .7]
        if candidates and reserve is not None:
            sid, a = min(candidates, key=lambda p: (p[1]['pro100'] / p[1]['pro7'], p[0] != 1))
            return 1143, sid, a, 'heal'
        attack = available(1159)
        if attack is not None and reserve is not None and attrs['pro101'] >= attack + reserve:
            return 1159, 2, None, 'attack_with_heal_reserve'
        return None
    if comp['template'] != 'fighter':
        return choose_school_skill(comp, battle, patients, effective, enemy)
    # Attack immediately in short fights; enhance the strongest living ally
    # only when the enemy can survive several preferred attacks.
    preferred_damage = max(1, int(120 + .3 * effective['pro31']) - int(enemy.get('pro32', 0)))
    buff_cost, attack_cost = available(1224), available(1208)
    if (battle['enemy_hp'] > preferred_damage * 3 and buff_cost is not None
            and attack_cost is not None and attrs['pro101'] >= buff_cost + attack_cost):
        candidates = [(sid, a) for sid, a in patients if not active_state(battle, sid, 5046)]
        if candidates:
            sid, a = max(candidates, key=lambda p: max(p[1].get('pro31', 0), p[1].get('pro41', 0)))
            return 1224, sid, a, 'attack_buff'
    if attack_cost is not None:
        return 1208, 2, None, 'attack'
    return None


def choose_school_skill(comp, battle, patients, effective, enemy):
    """Heal injured allies, then choose a legal original attack or debuff."""
    from skill_service import learned, rank
    legal = []
    for identifier, level in learned(comp['actor']).items():
        row = rank(identifier, level)
        spell = row['spell']
        # Guardian summons, transformations and prerequisite states are not
        # implemented by the shared cast engine; never simulate them as attacks.
        if spell.get('guardian') or spell.get('req') or spell.get('restore') and spell['target_type'] == 5:
            continue
        if skill_cost(comp, battle, int(identifier)) is not None:
            legal.append((int(identifier), spell))
    hurt = [(sid, a) for sid, a in patients if a['pro100'] < a['pro7'] * .7]
    if hurt:
        target, attrs = min(hurt, key=lambda p: (p[1]['pro100'] / p[1]['pro7'], p[0]))
        heals = [(identifier, spell) for identifier, spell in legal if spell['target_type'] == 4
                 and spell.get('effect') and spell.get('restore')]
        if heals:
            identifier, _ = max(heals, key=lambda pair: spell_strength(pair[1], effective))
            return identifier, target, attrs, 'heal'
        for identifier, spell in legal:
            states = spell.get('target_buff', {})
            if spell['target_type'] == 4 and states and not any(
                    active_state(battle, target, state['id']) for state in states.values()):
                return identifier, target, attrs, 'sustained_heal'
    attacks = [(identifier, spell) for identifier, spell in legal if spell['target_type'] == 5]
    scored = []
    for identifier, spell in attacks:
        damage = spell_strength(spell, effective, enemy)
        states = spell.get('target_buff', {})
        fresh = any(not active_state(battle, 2, state['id']) for state in states.values())
        # Apply DOT/defence reduction only if the encounter will last.
        if fresh and battle['enemy_hp'] > max(1, damage) * 3:
            damage += 180
        if damage:
            scored.append((damage, -skill_cost(comp, battle, identifier), -identifier, identifier))
    if scored:
        return max(scored)[-1], 2, None, 'school_attack'
    return None


def spell_strength(spell, attrs, enemy=None):
    import math
    amount = 0
    for effect in spell.get('effect', {}).values():
        physical = effect['formula'] == 11
        args = effect['args']
        value = math.floor(abs(args.get('fix', 0) + args.get('a', 0) * attrs.get('pro31' if physical else 'pro41', 0)))
        amount += max(1, value - int((enemy or {}).get('pro32' if physical else 'pro42', 0)))
    return amount


def act(character, battle, player_attrs, operations):
    for companion in members(battle):
        act_member(character, battle, player_attrs, operations, companion)
    for pet in pets(battle):
        act_pet(battle, pet, operations)


def act_pet(battle, pet, operations):
    if (pet['attrs']['pro100'] <= 0 or battle['enemy_hp'] <= 0
            or (pet.get('last_action') or {}).get('round') == battle['round']):
        return
    from battle_service import physical_damage
    from skill_service import effective_attributes
    attrs, sid = pet['attrs'], pet['sid']
    damage = min(battle['enemy_hp'], physical_damage(
        attrs, effective_attributes(attrs, battle, sid), effective_attributes({}, battle, 2)))
    battle['enemy_hp'] -= damage
    operations.extend([('battle_attack', sid, 2), ('battle_hp_change', 2, -damage),
                       ('battle_set_combat_attr', 2, 'pro100', battle['enemy_hp'])])
    if not battle['enemy_hp']:
        operations.append(('battle_die', 2))
    operations.append(('battle_motion_end',))
    pet['last_action'] = {'round': battle['round'], 'kind': 'attack', 'target': 2}


def act_member(character, battle, player_attrs, operations, companion):
    if not companion or companion['attrs']['pro100'] <= 0 or battle['enemy_hp'] <= 0:
        return
    if (companion.get('last_action') or {}).get('round') == battle['round']:
        return
    from skill_service import cast, effective_attributes
    attrs, actor = companion['attrs'], companion['actor']
    sid = companion['sid']
    choice = choose_skill(character, battle, player_attrs, companion)
    if choice:
        skill_id, target, target_attrs, reason = choice
        cast(actor, battle, skill_id, target, False, operations,
             caster_sid=sid, caster_attrs=attrs, friendly_attrs=target_attrs,
             cooldowns=companion['cooldowns'])
        companion['last_action'] = {'round': battle['round'], 'kind': 'skill',
                                    'skill': skill_id, 'target': target, 'reason': reason}
        return
    from battle_service import physical_damage
    damage = min(battle['enemy_hp'], physical_damage(
        attrs, effective_attributes(attrs, battle, sid), effective_attributes({}, battle, 2)))
    battle['enemy_hp'] -= damage
    operations.extend([('battle_attack', sid, 2), ('battle_hp_change', 2, -damage),
                       ('battle_set_combat_attr', 2, 'pro100', battle['enemy_hp'])])
    if not battle['enemy_hp']:
        operations.append(('battle_die', 2))
    operations.append(('battle_motion_end',))
    companion['last_action'] = {'round': battle['round'], 'kind': 'attack', 'target': 2,
                                'reason': 'basic_attack'}


def living(battle):
    return any(c['attrs']['pro100'] > 0 for c in members(battle) + pets(battle))


def enemy_target(battle, player_attrs):
    targets = ([(1, player_attrs)] if player_attrs['pro100'] > 0 else [])
    targets += [(c['sid'], c['attrs']) for c in members(battle) + pets(battle) if c['attrs']['pro100'] > 0]
    return targets[(battle['round'] - 1) % len(targets)] if targets else (1, player_attrs)
