"""Persistent, bounded first tutorial encounter using original client methods.

Protocol, models, quest items and attributes are recovered originals. Basic
physical damage uses an explicit simulator midpoint-minus-defence policy.
"""
import logging
import struct
import time
import uuid

import character_store
from attribute_service import combat_attributes, message, notice, save_vitals
from csxy_protocol import variable_message
from entity_methods import avatar_client_message
from inventory_service import grant_items, intro_reward, QUEST_BAG_PAGE
from quest_service import resync_item_objectives
from warrior_codec import catalog, encode_message

LOG = logging.getLogger('battle')
QUEST = 45198
STATE_KEY = 'tutorial_battle'
PLAYER_SID = 1
ENEMY_SID = 2
ACTIVE_PHASES = ('preparing', 'input', 'animating')
# Recovered cdata.battle_array[0].pos[1] (no formation), corroborated by the
# original .cs recordings: avatars stand at y=-4, enemies at positive y.
# Scene.getCoord mirrors BOTH coordinates for a defending viewer; it does
# not transform each doll by its camp. Scene.getPosition maps these slots
# to (50, ground, 43)/(50, ground, 57), facing one another. A y=0 slot stays
# on the field centre and has the wrong orientation for an opposing doll.
PLAYER_BATTLE_POSITION = (0.0, -4.0)
ENEMY_BATTLE_POSITION = (0.0, 4.0)


class BattleRequestError(ValueError):
    pass


def physical_damage(base, attack, defence):
    """Simulator rule: displayed attack midpoint minus effective defence.

    Original server hit/crit/armour formulas are unavailable. Use the derived
    equipment/level/potential range rather than the old fixed tutorial damage.
    Strength buffs in the current skill handler modify pro31, so carry that
    delta into the already-derived range as well.
    """
    low = base.get('pro38', base.get('pro31', 0))
    high = base.get('pro37', base.get('pro31', 0))
    raw = (low + high) / 2 + attack.get('pro31', 0) - base.get('pro31', 0)
    return max(1, int(raw - defence.get('pro32', 0)))


def selected(session):
    return next((c for c in character_store.characters(session)
                 if c['dbid'] == session.selected_character), None)


def synchronize_runtime(session, character):
    battle = character['world_state'].get(STATE_KEY, {})
    if battle.get('phase') in ACTIVE_PHASES:
        session.battle_id = battle['id']
        session.battle_due = battle['deadline']
    else:
        session.battle_id = ''
        session.battle_due = 0


def warrior(character, enemy=False, encounter=None):
    attrs = combat_attributes(character)
    data = catalog()
    target = encounter or {'monster': data['monster'], 'level': data['born']['level']}
    x, y = ENEMY_BATTLE_POSITION if enemy else PLAYER_BATTLE_POSITION
    formation = character.get('world_state', {}).get(STATE_KEY, {}).get('formation_player', {})
    if not enemy and formation and character['world_state'][STATE_KEY].get('phase') in ACTIVE_PHASES:
        x, y = formation['x'], formation['y']
    result = {'type': 3 if enemy else 1, 'sid': ENEMY_SID if enemy else PLAYER_SID,
              'x': x, 'y': y, 'camp': 2 if enemy else 1,
              'level': target['level'] if enemy else character['level'],
              'hp': data['simulator_rules']['enemy_hp'] if enemy else attrs['pro100'],
              'mhp': data['simulator_rules']['enemy_hp'] if enemy else attrs['pro7'],
              'mp': 0 if enemy else attrs['pro101'], 'mmp': 0 if enemy else attrs['pro8'],
              'isDead': False}
    if enemy:
        result['monster'] = {'monsterID': target['monster']['id'], 'aspect': target['monster']['aspect'],
                             'roleName': target['monster']['name'].encode('gbk'), 'title': b''}
    else:
        from equipment_service import aspect_info
        result['avatar'] = {'aspect': aspect_info(character),
                            'physique': dict(zip(('school','sex','facestyle','hairstyle','haircolor'),
                                                 bytes.fromhex(character['physique']))),
                            'roleName': bytes.fromhex(character['name']), 'entityID': 1002, 'gscolor': 0}
    return encode_message('WarriorInfo', result)


def encounter_for(character, npc, story_quest=None):
    from dungeon_service import encounter as dungeon_encounter
    dungeon = dungeon_encounter(character, npc)
    if dungeon:
        return dungeon
    from quest_catalog import catalog as quests, active
    data = quests()
    offline = npc.get('offline_encounter')
    if offline:
        if offline.get('quest', story_quest or 0) == 14025:
            raise BattleRequestError('请通过崔大勇进入长生丹室，击败三大仙真身完成任务。')
        result = {'quest': offline.get('quest', story_quest or 0),
                'generation': offline.get('generation'),
                'objective': offline.get('objective'),
                'custom_id': offline.get('custom_id'),
                'monster': {'id': npc.get('template_id', 0), 'aspect': npc.get('aspect', 0),
                            'name': bytes.fromhex(npc.get('name_gbk', '')).decode('gbk', 'replace')},
                'level': character.get('level', 1), 'born_id': npc.get('template_id', 0),
                'drops': [], 'field': catalog()['simulator_rules']['field'], 'story': True,
                'offline': True}
        custom = data['battles'].get(str(offline.get('custom_id')), {})
        if custom.get('map'):
            result['field'] = 'universes/eg/wf/' + custom['map']
        objective = offline.get('objective') or {}
        if objective.get('kind') in ('monsters', 'outside_monsters'):
            table = data['monsters' if objective['kind'] == 'monsters' else 'born']
            target = table.get(str(objective.get('id')), {})
            if target:
                result['monster'] = dict(result['monster'], id=target['id'],
                    aspect=target.get('aspect', npc['aspect']), name=target.get('name', '任务目标'))
        return result
    born = data['born'].get(str(npc['template_id']), {})
    monster = data['monsters'].get(str(npc['template_id']))
    if not monster or monster.get('name') != born.get('name'):
        monster = next((m for m in data['monsters'].values() if m.get('aspect') == npc['aspect']
                        and m.get('name') == born.get('name')), None)
    if story_quest:
        monster = {'id': npc['template_id'], 'aspect': npc['aspect'],
                   'name': bytes.fromhex(npc['name_gbk']).decode('gbk')}
    if not monster:
        monster = {'id': npc['template_id'], 'aspect': npc['aspect'],
                   'name': bytes.fromhex(npc['name_gbk']).decode('gbk')}
    # Prefer the original monster ID demanded by an active quest when several
    # encounter tables reuse the same visible model and display name.
    for number in character['tasks']:
        if not active(character, int(number)):
            continue
        for identifier, _ in data['quests'].get(number, {}).get('cq', {}).get('req', {}).get('monsters', []):
            candidate = data['monsters'].get(str(identifier), {})
            if candidate.get('aspect') == npc['aspect'] and candidate.get('name') == born.get('name'):
                monster = candidate
    result = {'quest': story_quest or 0, 'monster': monster,
              'level': born.get('level', character['level']), 'born_id': npc['template_id'],
              'drops': [], 'field': catalog()['simulator_rules']['field'], 'story': bool(story_quest)}
    if npc['template_id'] == 42503:
        result.update(quest=45198, drops=[[58081, 1, 2]])
    elif npc['template_id'] in (42504, 42505, 42506) and active(character, 45207):
        result.update(quest=45207, drops=[[58082, 1, 3]])
    elif npc['template_id'] in (35237, 35238, 35239):
        number, identifier = {35237: (45211, 74083), 35238: (45212, 74084), 35239: (45213, 74085)}[npc['template_id']]
        if active(character, number):
            result.update(quest=number, drops=[[identifier, 1, 3]])
    if active(character, 45209) and not result['quest']:
        result['quest'] = 45209
    if active(character, 13464) and monster.get('id') in (3031, 3032, 3033):
        result.update(quest=13464, drops=[[74091, 1, 3]])
    elif active(character, 42266) and monster.get('id') in (3034, 3035, 3036):
        result.update(quest=42266, drops=[[58106, 1, 20]])
    if story_quest:
        custom_rule = data['simulator_rules'].get('custom_battles', {}).get(str(story_quest), {})
        battle_no = custom_rule.get('battle', data['quests'][str(story_quest)].get('custom_battle'))
        custom = data['battles'][str(battle_no)]
        if custom.get('map'):
            result['field'] = 'universes/eg/wf/' + custom['map']
        if story_quest == 13468:
            from npc_service import npc_by_template
            enemy = npc_by_template(5145, 4, 'QuestMan')
            result['monster'] = {'id': 5145, 'aspect': enemy['aspect'],
                                 'name': bytes.fromhex(enemy['name_gbk']).decode('gbk')}
        if story_quest == 45219:
            result['drops'] = [[74090, 1, 1]]
        if story_quest == 45215:
            # This counter belongs to the original map item, not merely a win.
            for item_id, item in data['quest_items'].items():
                if item.get('gain', {}).get('counters', [None])[0] == 8104:
                    result['drops'] = [[int(item_id), 1, 1]]
                    break
        # Fixed 淮南三害 encounters are launched from a QuestMan but the
        # combatant is the corresponding hidden story actor in the original
        # custom battle (牛二、白虎、长蛟).
        target_id = custom_rule.get('npc')
        if target_id:
            from npc_service import npc_by_template
            target = npc_by_template(target_id, character['space_no'])
            if target:
                result['monster'] = {
                    'id': custom_rule['monster'], 'aspect': target['aspect'],
                    'name': bytes.fromhex(target['name_gbk']).decode('gbk')}
        if custom_rule.get('items'):
            result['drops'] = [[int(row[0]), int(row[1]), 1] for row in custom_rule['items']]
        result['custom_id'] = battle_no
    if result['quest']:
        result['generation'] = character['tasks'].get(str(result['quest']), {}).get('generation')
    return result


def start_story_battle(session, quest_no):
    from npc_service import npc_by_template
    from quest_catalog import definition
    character = selected(session)
    if not character:
        return []
    npc = npc_by_template(definition(quest_no)['aquire_npcs'][0], character['space_no'], 'QuestMan')
    if not npc:
        return []
    return start(session, npc, story_quest=quest_no)


def start_idler_battle(session, npc):
    """Route only explicitly recovered quest encounters from map actors."""
    from quest_catalog import catalog as quests, active, supported
    character = selected(session)
    if not character or npc['type'] != 'Idler':
        return []
    for number, rule in quests()['simulator_rules'].get('custom_battles', {}).items():
        number = int(number)
        progress = character['tasks'].get(str(number), {})
        if (number in supported() and active(character, number)
                and not progress.get('battle_won')
                and rule.get('npc') == npc['template_id']
                and rule['battle'] in npc.get('custom_battle', {}).get('id', [])):
            return start(session, npc, story_quest=number)
    return []


def start(session, npc, now=None, story_quest=None):
    now = time.time() if now is None else now
    if (not session.world_ready or (npc['type'] != 'Monster' and not story_quest)
            or (npc['entity_id'] not in session.visible_npcs and not story_quest)):
        return []
    if sum((session.position[i] - npc['position'][i]) ** 2 for i in (0,2)) > 25 ** 2:
        return [notice(session, '请靠近目标后再战斗。')]
    started = []
    mount_replies = []

    def change(character):
        if character['space_no'] != npc.get('space_no', 14):
            raise BattleRequestError('目标不在当前地图。')
        from monster_service import available
        if not story_quest and not available(character, npc, now):
            raise BattleRequestError('这个怪物已经被击败，正在等待重新出现。')
        encounter = encounter_for(character, npc, story_quest)
        quest_no = encounter['quest']
        if quest_no and character['tasks'].get(str(quest_no), {}).get('status') not in ('active', 'ready'):
            raise BattleRequestError('请先向梦珠接取「风影悠然扑彩蝶」。')
        if story_quest and not npc.get('offline_encounter') and character['tasks'][str(quest_no)].get('battle_won'):
            return
        previous = character['world_state'].get(STATE_KEY, {})
        if previous.get('phase') in ACTIVE_PHASES:
            return
        if npc['template_id'] == 42503 and sum(i['amount'] for i in character['inventory'] if i['id'] == 58081) >= 2:
            raise BattleRequestError('已经捉到两只玉凤蝶，可以去提交任务了。')
        if combat_attributes(character)['pro100'] <= 0:
            raise BattleRequestError('倒地时不能发起战斗。')
        # Original mount buffs are excluded from battles. Commit the dismount
        # with the encounter, so a failed start leaves riding untouched.
        from mount_service import clear_active, active_ride
        resume_ride = active_ride(character)
        mount_replies.extend(clear_active(session, character))
        # Build both blobs before committing so encoding failures cannot leave
        # a persisted encounter that the client never had a chance to enter.
        from companion_service import prepare_all, warrior as companion_warrior
        from team_service import battle_plan
        party = prepare_all(character, encounter)
        array_id, formation_player = battle_plan(character, party)
        # Stage formation attributes before encoding; the whole start remains
        # in the same save transaction and rolls back if any packet fails.
        character['world_state'][STATE_KEY] = {'phase': 'preparing', 'formation_player': formation_player}
        started.extend([warrior(character), warrior(character, enemy=True, encounter=encounter)])
        from summon_service import active_item, warrior as summon_warrior
        summon = active_item(character)
        if summon:
            started.append(summon_warrior(summon))
        companion = party[0] if party else None
        started.extend(companion_warrior(comp) for comp in party)
        avatar_client_message(session, 'battle_add_dolls', started)
        character['world_state'][STATE_KEY] = {
            'id': uuid.uuid4().hex, 'npc': npc['entity_id'], 'phase': 'preparing', 'round': 1,
            'enemy_hp': catalog()['simulator_rules']['enemy_hp'], 'outcome': None,
            'created_at': now, 'deadline': now + 3, 'drop_committed': False,
            'reward_items': [], 'encounter': encounter, 'counter_updates': [],
            'formation_player': formation_player, 'array_id': array_id, 'resume_ride': resume_ride,
            'summon_joined': bool(summon), 'summon_uuid': summon['uuid'] if summon else None}
        if companion:
            character['world_state'][STATE_KEY]['companion'] = companion
            character['world_state'][STATE_KEY]['other_companions'] = party[1:]
        # Quest 45209 tracks the moment the summon actually joins the battle.
        # Older code only awarded this counter during victory settlement, so
        # the task panel stayed at 0/1 while the battle was in progress.
        if summon and character['tasks'].get('45209', {}).get('status') in ('active', 'ready'):
            from quest_service import gain_counters
            character['world_state'][STATE_KEY]['counter_updates'].extend(
                gain_counters(character, [(6560, 1)]))

    try:
        character = character_store.transact(session, change)
    except (BattleRequestError, ValueError) as error:
        return [notice(session, str(error))]
    if character is None or not started:
        return []
    synchronize_runtime(session, character)
    # The encounter monster is represented again by battle_add_dolls.  Remove
    # its scene entity first so the two client models cannot coexist.  Keep the
    # persisted entity id in tutorial_battle so a normal battle completion can
    # restore the AOI entry even after several turns.
    session.visible_npcs = [entity_id for entity_id in session.visible_npcs
                            if entity_id != npc['entity_id']]
    LOG.info('Tutorial battle started and saved: npc=%d', npc['entity_id'])
    replies = [variable_message(14, struct.pack('<I', npc['entity_id'])),
            avatar_client_message(session, 'battle_prepare', character['world_state'][STATE_KEY]['encounter']['field'], 1),
            avatar_client_message(session, 'battle_set_escape', True),
            avatar_client_message(session, 'battle_add_dolls', started),
            avatar_client_message(session, 'battle_self_sid', PLAYER_SID),
            # Battle.custom is the original beginBattleBGM entry. Zero selects
            # ordinary PVE music without enabling a special battle tutorial.
            avatar_client_message(session, 'battle_custom', 0),
            avatar_client_message(session, 'battle_action')]
    if character['world_state'][STATE_KEY]['summon_joined']:
        from summon_service import SID
        from summon_service import active_item
        replies.insert(-1, avatar_client_message(session, 'battle_current_summon', active_item(character)['pos'], SID))
    if character['world_state'][STATE_KEY].get('counter_updates'):
        from quest_service import counter_messages
        replies.extend(counter_messages(session, character,
                                        character['world_state'][STATE_KEY]['counter_updates']))
    from companion_scene import hide
    return mount_replies + hide(session) + replies


def command(session, action, target=ENEMY_SID, page=0, pos=0, now=None, skill_id=0, enhanced=False):
    now = time.time() if now is None else now
    if not session.world_ready or not getattr(session, 'battle_id', ''):
        return []
    operations = []
    inventory_updates = []
    encoded_replies = []

    def change(character):
        battle = character['world_state'].get(STATE_KEY, {})
        if battle.get('id') != session.battle_id or battle.get('phase') != 'input':
            return  # Retransmission/rapid double click during animation is inert.
        if action not in ('attack', 'guard', 'escape', 'item', 'skill'):
            raise BattleRequestError('该战斗操作尚未恢复。')
        if action == 'attack' and target != ENEMY_SID:
            raise BattleRequestError('请选择敌方玉凤蝶。')
        from companion_service import patient
        if action == 'item' and target != PLAYER_SID and patient(battle, target) is None:
            raise BattleRequestError('请选择队伍中的友方目标。')
        quest_no = battle.get('encounter', {}).get('quest', QUEST)
        if action != 'escape' and quest_no and character['tasks'].get(str(quest_no), {}).get('status') not in ('active', 'ready'):
            raise BattleRequestError('任务已经取消，请选择逃跑。')
        attrs = combat_attributes(character)
        if attrs['pro100'] <= 0 and action not in ('guard', 'escape'):
            raise BattleRequestError('角色已经倒地，队友将继续战斗。')
        rules = catalog()['simulator_rules']
        operations.append(('battle_input_ready', PLAYER_SID))
        if action == 'escape':
            operations.extend([('battle_escape', PLAYER_SID, True), ('battle_motion_end',)])
            battle['outcome'] = 'escape'
        else:
            if action == 'attack':
                from skill_service import effective_attributes
                enemy_attrs = effective_attributes({}, battle, ENEMY_SID)
                player_attrs = effective_attributes(attrs, battle, PLAYER_SID)
                damage = physical_damage(attrs, player_attrs, enemy_attrs)
                battle['enemy_hp'] = max(0, battle['enemy_hp'] - damage)
                operations.extend([('battle_attack', PLAYER_SID, ENEMY_SID),
                                   ('battle_hp_change', ENEMY_SID, -damage),
                                   ('battle_set_combat_attr', ENEMY_SID, 'pro100', battle['enemy_hp'])])
                if not battle['enemy_hp']:
                    operations.append(('battle_die', ENEMY_SID))
                operations.append(('battle_motion_end',))
            elif action == 'guard':
                pass  # Defence decorates the enemy attack below in the original client.
            elif action == 'skill':
                from skill_service import cast
                attrs = cast(character, battle, skill_id, target, enhanced, operations)
            elif action == 'item':
                from companion_service import patient as companion_patient
                patient = attrs if target == PLAYER_SID else companion_patient(battle, target)
                if not patient or patient['pro100'] <= 0:
                    raise BattleRequestError('请选择存活的友方目标。')
                item = next((i for i in character['inventory'] if (i['page'],i['pos'])==(page,pos)), None)
                if item is None or page != 0 or not 0 <= pos < 24:
                    raise BattleRequestError('该位置没有可用药品。')
                if not intro_reward()['items'].get(str(item['id']), {}).get('use_condition', 0) & 1:
                    raise BattleRequestError('此物品只能在战斗外使用。')
                effects = {23021: ('pro100','pro7',400), 23023: ('pro101','pro8',400)}
                if item['id'] not in effects:
                    raise BattleRequestError('该战斗药品尚未恢复。')
                key, maximum, amount = effects[item['id']]
                if patient[key] >= patient[maximum]:
                    raise BattleRequestError('生命或法力已满，无需使用。')
                amount = min(amount, patient[maximum] - patient[key])
                patient[key] += amount
                item['amount'] -= 1
                inventory_updates.append(('res_wrap',0,page,pos,item['amount']) if item['amount']
                                         else ('res_remove',0,page,pos))
                if not item['amount']:
                    character['inventory'].remove(item)
                operations.extend([('battle_use_item',PLAYER_SID,target,item['id']),
                                   ('battle_hp_change' if key=='pro100' else 'battle_mp_change',target,amount),
                                   ('battle_set_combat_attr',target,key,patient[key]), ('battle_motion_end',)])
            from skill_service import periodic_effects
            periodic_effects(attrs, battle, operations, character)
            from companion_service import act as companion_act
            companion_act(character, battle, attrs, operations)
            if battle.get('summon_joined') and battle['enemy_hp'] and battle.get('summon_action', 'attack') == 'attack':
                from summon_service import SID
                from summon_progression import participant
                pet = participant(character, battle)
                pet_attrs = pet['attributes']['combatAttr'] if pet else {}
                if pet and pet_attrs.get('pro100', 0) > 0:
                    from skill_service import effective_attributes
                    damage = physical_damage(pet_attrs, effective_attributes(pet_attrs, battle, SID),
                                             effective_attributes({}, battle, ENEMY_SID))
                    battle['enemy_hp'] = max(0, battle['enemy_hp'] - damage)
                    operations.extend([('battle_attack', SID, ENEMY_SID),
                                   ('battle_hp_change', ENEMY_SID, -damage),
                                   ('battle_set_combat_attr', ENEMY_SID, 'pro100', battle['enemy_hp'])])
                    if not battle['enemy_hp']:
                        operations.append(('battle_die', ENEMY_SID))
                    operations.append(('battle_motion_end',))
            if battle['enemy_hp'] == 0:
                # Persist the last turn's vitals before XP/link rewards can
                # level and heal the character; stale attrs must not undo it.
                if battle.get('companion') and attrs['pro100'] <= 0:
                    attrs['pro100'] = 1  # Surviving companion carries the owner home.
                save_vitals(character, attrs)
                drops = battle.get('encounter', {}).get('drops', [[rules['drop_item'], 1, 2]])
                for identifier, amount, limit in drops:
                    held = sum(i['amount'] for i in character['inventory'] if i['id'] == identifier)
                    amount = min(amount, limit - held)
                    if amount <= 0:
                        continue
                    grant_items(character, [(identifier, amount)], QUEST_BAG_PAGE)
                    battle['drop_committed'] = True
                    battle['reward_items'].append([identifier, amount])
                battle['outcome'] = 'victory'
                from monster_service import victory as monster_victory_cooldown
                monster_victory_cooldown(character, battle, now)
                from progression_service import grant_battle_experience as grant_avatar_experience
                grant_avatar_experience(character, battle)
                from summon_progression import grant_battle_experience, experience_messages
                grant_battle_experience(character, battle)
                # Validate the original XP update packet before the victory
                # commit; delivery follows battle_end at the timer callback.
                experience_messages(session, character, battle)
                from quest_service import objectives_met, gain_counters
                from quest_story import STORY_BATTLES
                if quest_no in STORY_BATTLES:
                    character['tasks'][str(quest_no)]['battle_won'] = True
                    character['tasks'][str(quest_no)]['data']['2'] = True
                    battle['counter_updates'].append(quest_no)
                    from quest_catalog import catalog as quest_catalog
                    custom_rule = quest_catalog()['simulator_rules'].get('custom_battles', {}).get(str(quest_no), {})
                    if custom_rule.get('counter'):
                        from quest_service import gain_counters
                        battle['counter_updates'].extend(
                            gain_counters(character, [(int(custom_rule['counter']), 1)]))
                if quest_no and objectives_met(character, quest_no):
                    character['tasks'][str(quest_no)]['status'] = 'ready'
                if battle.get('summon_joined') and character['tasks'].get('45209', {}).get('status') in ('active', 'ready'):
                    battle['counter_updates'].extend(gain_counters(character, [(6560, 1)]))
                from quest_event_service import monster_victory
                from dungeon_service import victory as dungeon_victory
                dungeon_victory(character, battle)
                if not battle.get('encounter', {}).get('offline'):
                    battle['counter_updates'].extend(monster_victory(character, battle.get('encounter', {})))
                from quest_controller import battle_event
                battle['counter_updates'].extend(battle_event(character, battle.get('encounter', {})))
                from quest_event_service import settle_links
                slots_before = {(i['page'], i['pos']) for i in character['inventory']}
                battle['linked_events'] = settle_links(character)
                battle['removed_slots'] = [list(p) for p in slots_before -
                                          {(i['page'], i['pos']) for i in character['inventory']}]
                from progression_service import battle_experience_messages
                battle_experience_messages(session, character, battle)
            else:
                from skill_service import effective_attributes
                from companion_service import enemy_target, living
                target_sid, target_attrs = enemy_target(battle, attrs)
                player_attrs = effective_attributes(target_attrs, battle, target_sid)
                enemy_attrs = effective_attributes({}, battle, ENEMY_SID)
                enemy_base = {'pro31': rules['enemy_damage']}
                enemy_attack = dict(enemy_base, pro31=rules['enemy_damage'] + enemy_attrs.get('pro31', 0))
                damage = physical_damage(enemy_base, enemy_attack, player_attrs)
                if action == 'guard' and target_sid == PLAYER_SID:
                    damage = max(1, int(damage * rules['guard_damage'] / rules['enemy_damage']))
                damage = min(target_attrs['pro100'], damage)
                target_attrs['pro100'] -= damage
                operations.append(('battle_attack',ENEMY_SID,target_sid))
                if action == 'guard' and target_sid == PLAYER_SID:
                    operations.append(('battle_defend',PLAYER_SID))
                operations.extend([('battle_hp_change',target_sid,-damage),
                                   ('battle_set_combat_attr',target_sid,'pro100',target_attrs['pro100'])])
                if target_attrs['pro100'] == 0:
                    operations.append(('battle_die',target_sid))
                if attrs['pro100'] == 0 and not living(battle):
                    battle['outcome'] = 'defeat'
                    attrs['pro100'] = 1  # Simulator recovery; original death penalties are not restored.
                operations.append(('battle_motion_end',))
                if battle['round'] >= 50 and battle['outcome'] is None:
                    battle['outcome'] = 'draw'
            if battle['outcome'] != 'victory':
                save_vitals(character, attrs)
        operations.append(('battle_action',))
        animation_seconds = rules['animation_seconds']
        if battle.get('companion'):
            animation_seconds = max(animation_seconds,
                                    sum(op[0] == 'battle_motion_end' for op in operations) * 4)
        battle.update(phase='animating', deadline=now + animation_seconds)
        # Encode every turn response before committing HP, MP and rewards.
        encoded_replies.extend(avatar_client_message(session, name, *args)
                               for name, *args in inventory_updates + operations)

    try:
        character = character_store.transact(session, change)
    except (BattleRequestError, ValueError) as error:
        current = selected(session)
        battle = current['world_state'].get(STATE_KEY, {}) if current else {}
        seconds = max(1, min(30, int(battle.get('deadline', now) - now)))
        return [notice(session, str(error)), avatar_client_message(session, 'battle_input', seconds)]
    if character is None or not operations:
        return []
    synchronize_runtime(session, character)
    LOG.info('Tutorial battle turn saved: action=%s round=%d outcome=%s', action,
             character['world_state'][STATE_KEY]['round'], character['world_state'][STATE_KEY]['outcome'])
    summon_reward = character['world_state'][STATE_KEY].get('summon_experience', {})
    if summon_reward and character['world_state'][STATE_KEY]['outcome'] == 'victory':
        LOG.info('Summon combat XP committed: summon=%s experience=%d',
                 summon_reward['uuid'], summon_reward['experience'])
    avatar_reward = character['world_state'][STATE_KEY].get('avatar_experience', {})
    if avatar_reward:
        LOG.info('Avatar combat XP committed: experience=%d levels=%s',
                 avatar_reward['experience'], avatar_reward['levels_gained'])
    return encoded_replies


def tick(session, now=None):
    now = time.time() if now is None else now
    if (not session.world_ready or not getattr(session, 'battle_id', '')
            or now < getattr(session, 'battle_due', 0)):
        return []
    current = selected(session)
    battle = current['world_state'].get(STATE_KEY, {}) if current else {}
    if battle.get('id') != session.battle_id or battle.get('phase') not in ACTIVE_PHASES:
        session.battle_id, session.battle_due = '', 0
        from npc_service import update_interest
        return [avatar_client_message(session, 'battle_end')] + update_interest(session)
    if battle['phase'] == 'input':
        return command(session, 'guard', now=now)
    responses = []

    def change(character):
        battle = character['world_state'][STATE_KEY]
        if battle['id'] != session.battle_id or now < battle['deadline']:
            return
        if battle['outcome']:
            battle.update(phase='finished', finished_at=now, deadline=0)
            save_vitals(character, combat_attributes(character))
            if battle.get('companion') and combat_attributes(character)['pro100'] <= 0:
                character['combat_attr']['pro100'] = 1
            responses.append(avatar_client_message(session,'battle_end'))
            from consumable_service import settle_after_battle
            responses.extend(settle_after_battle(session, character, battle))
            # Synchronize the quest item, then its tracker metadata. Replaying
            # unrelated inventory/equipment as new insertions is unnecessary.
            responses.extend([message(session,character)] + resync_item_objectives(session,character))
            text = {'victory':'战斗胜利。',
                    'escape':'已经退出战斗。', 'defeat':'战斗失败，已返回场景，请先补充生命。',
                    'draw':'战斗回合达到上限，已返回场景。'}[battle['outcome']]
            if battle['outcome'] == 'victory':
                from progression_service import battle_experience_messages
                responses.extend(battle_experience_messages(session, character, battle))
                from summon_progression import experience_messages
                responses.extend(experience_messages(session, character, battle))
                # Compatibility for an already committed pre-fix encounter.
                rewards = battle.get('reward_items', [[58081, 1]] if battle.get('drop_committed') else [])
                from quest_service import client_quest_data, ITEM_OBJECTIVES, counter_messages
                from inventory_service import item_info
                tracked = {i for n in client_quest_data(character) for i, _ in ITEM_OBJECTIVES.get(n, [])}
                for item in character['inventory']:
                    if item['id'] in {row[0] for row in rewards} - tracked:
                        responses.append(avatar_client_message(session, 'res_insert', 0, item['page'],
                                                               item['pos'], item_info(item)))
                responses.extend(counter_messages(session, character, battle.get('counter_updates', [])))
                if battle.get('linked_events'):
                    from quest_event_service import linked_messages
                    from inventory_service import messages as inventory_messages
                    from progression_service import messages as progression_messages
                    responses.extend(avatar_client_message(session, 'res_remove', 0, *slot)
                                     for slot in battle.get('removed_slots', []))
                    responses.extend(inventory_messages(session, character))
                    responses.extend(linked_messages(session, character, battle['linked_events']))
                    responses.extend(progression_messages(session, character))
                for item_id, amount in rewards:
                    name = intro_reward()['items'][str(item_id)]['name']
                    text += '获得%s × %d，已放入任务背包。' % (name, amount)
                avatar_reward = battle.get('avatar_experience', {})
                if avatar_reward:
                    text += '获得经验 × %d。' % avatar_reward['experience']
                    if avatar_reward['levels_gained']:
                        text += '升至%d级，生命和法力已回满。' % character['level']
                summon_reward = battle.get('summon_experience', {})
                if summon_reward:
                    from summon_progression import participant
                    pet = participant(character, battle)
                    if pet:
                        text += '召唤兽%s获得经验 × %d。' % (
                            pet['attributes']['sname'], summon_reward['experience'])
            responses.append(notice(session,text,chat=True))
            from team_service import messages as team_messages
            responses.extend(team_messages(session, character, character))
            from mount_service import restore_after_battle
            responses.extend(restore_after_battle(session, character, battle))
            if battle.get('encounter', {}).get('dungeon_run'):
                from dungeon_service import messages as dungeon_messages, progress_text
                responses.extend(dungeon_messages(session, character))
                if battle['outcome'] == 'victory':
                    responses.append(notice(session, progress_text(character), chat=True))
                for event in battle.get('dungeon_events', []):
                    from dungeon_service import catalog as dungeon_catalog
                    text = dungeon_catalog()['events'][str(event)].get('tips')
                    if text:
                        responses.append(notice(session, text, chat=True))
                if 704 in battle.get('dungeon_events', []):
                    responses.append(notice(session, '三大仙已经驱逐，可从出口返回崔府找崔大忠交任务。', chat=True))
        else:
            if battle['phase'] == 'animating':
                battle['round'] += 1
            battle.update(phase='input', deadline=now + catalog()['simulator_rules']['input_seconds'])
            battle.pop('summon_action', None)
            responses.append(avatar_client_message(session,'battle_round_begin',battle['round']))
            if battle.get('companion') and combat_attributes(character)['pro100'] <= 0:
                battle['deadline'] = now  # Next timer tick resolves the companion's turn.
            else:
                responses.append(avatar_client_message(session,'battle_input',catalog()['simulator_rules']['input_seconds']))

    character = character_store.transact(session, change)
    if character is not None:
        synchronize_runtime(session, character)
        # update_interest is deliberately called only after the battle state is
        # committed.  While the state is active it suppresses the encounter
        # entity; once marked finished it emits one enterAoI/createEntity pair.
        if character['world_state'].get(STATE_KEY, {}).get('phase') == 'finished':
            from npc_service import update_interest
            responses.extend(update_interest(session))
            if (character['world_state'][STATE_KEY]['outcome'] == 'victory' and
                    character['world_state'][STATE_KEY].get('encounter', {}).get('quest') == 13465):
                from npc_service import npc_by_template
                target = npc_by_template(5144, 11, 'QuestMan')
                character_store.update(session, space_no=11, position=list(target['position']))
                from world_service import transfer_scene
                responses.extend(transfer_scene(session))
    return responses


def interrupt_on_login(session):
    """A fresh world login ends the old encounter, retaining committed outcomes."""
    def change(character):
        character['world_state'].pop('quest_action', None)
        battle = character['world_state'].get(STATE_KEY, {})
        if battle.get('phase') in ACTIVE_PHASES:
            battle.update(phase='finished', outcome=battle.get('outcome') or 'interrupted',
                          finished_at=time.time(), deadline=0)
            if battle.get('companion') and combat_attributes(character)['pro100'] <= 0:
                character['combat_attr']['pro100'] = 1
            from consumable_service import settle_after_battle
            settle_after_battle(session, character, battle, emit=False)
            from mount_service import restore_after_battle
            restore_after_battle(session, character, battle, emit=False)
    character = character_store.transact(session, change)
    if character is not None:
        synchronize_runtime(session, character)
