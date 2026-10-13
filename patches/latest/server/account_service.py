"""Account messages recovered from Account.def and the client Account script."""
import logging
import struct

from csxy_protocol import pack_bytes, packed_bytes, variable_message
import character_store

LOG = logging.getLogger('account')
AVATAR_BASE_METHODS = None


def avatar_method(message, payload):
    global AVATAR_BASE_METHODS
    if AVATAR_BASE_METHODS is None:
        import xml.etree.ElementTree as ET
        from csxy_protocol import ROOT
        root = ET.parse(ROOT / 'analysis/resources/record_0214.xml').getroot()
        AVATAR_BASE_METHODS = [p for p in root.findall('BaseMethods/*') if p.find('Exposed') is not None]
    index = message & 63
    beginning = 62 - ((len(AVATAR_BASE_METHODS) - 63 + 255) // 255)
    if index >= beginning:
        if not payload:
            raise ValueError('missing entity method subslot')
        index = beginning + ((index - beginning) << 8) + payload[0]
        payload = payload[1:]
    method = AVATAR_BASE_METHODS[index].tag if index < len(AVATAR_BASE_METHODS) else 'unknown'
    return method, payload


def create_account(session):
    # Entity type 0 in entities.xml. iProxyNoCell.playerName is the only
    # BASE_AND_CLIENT property, streamed untagged in definition order.
    payload = struct.pack('<IH', session.entity_id, 0) + pack_bytes(session.username)
    return variable_message(5, payload)


def client_method(session, method, arguments):
    return variable_message(0x80 | method,
                            struct.pack('<I', session.entity_id) + arguments)


def character_list(session):
    items = character_store.characters(session)
    result = client_method(session, 1, struct.pack('<BBBq', len(items), 5 - len(items), 5,
                                                 items[-1]['dbid'] if items else 0))
    for character in items:
        result += client_method(session, 2, character_store.detail(character))
    return result


def process_messages(session, body):
    if getattr(session, 'superseded', False):
        return []
    responses = []
    offset = 0
    while offset < len(body):
        message = body[offset]
        offset += 1
        if message == 1:  # BaseAppExtInterface::authenticate
            if offset + 4 > len(body):
                raise ValueError('short authentication message')
            if struct.unpack_from('<I', body, offset)[0] != session.login_id:
                raise ValueError('incorrect authentication token')
            offset += 4
            continue
        if message >= 0x80 and message < 255:
            if offset + 2 > len(body):
                raise ValueError('short entity method header')
            length = struct.unpack_from('<H', body, offset)[0]
            offset += 2
            if offset + length > len(body):
                raise ValueError('entity method payload exceeds packet')
            payload = body[offset:offset + length]
            offset += length
            method = message & 0x3f
            if session.world_started:
                if not message & 0x40:
                    # Cell requests identify their target entity before the
                    # method arguments; base requests target the player's proxy.
                    if len(payload) < 4:
                        raise ValueError('short cell entity method target')
                    target = struct.unpack_from('<I', payload)[0]
                    from npc_service import find_npc, interact
                    npc = find_npc(target, session)
                    if npc:
                        responses.extend(interact(session, npc, method, payload[4:]))
                    elif target == session.entity_id:
                        from chat_service import on_cell
                        responses.extend(on_cell(session, target, method, payload[4:]))
                    else:
                        LOG.info('Cell entity=%d unknown target slot=%d payload_bytes=%d',
                                 target, method, len(payload) - 4)
                    continue
                name, arguments = avatar_method(message, payload)
                LOG.info('Avatar base method=%s payload_bytes=%d', name, len(arguments))
                from chat_service import on_base
                chat_replies = on_base(session, name, arguments)
                if chat_replies is not None:
                    responses.extend(chat_replies)
                    continue
                if name == 'reborn':
                    if len(arguments) != 1:
                        raise ValueError('reborn needs one UINT8')
                    from death_service import tick as death_tick
                    responses.extend(death_tick(session))
                    continue
                from team_service import request as team_request
                team_replies = team_request(session, name, arguments)
                if team_replies is not None:
                    responses.extend(team_replies)
                    continue
                if name == 'onClientLoadScene':
                    if arguments:
                        raise ValueError('onClientLoadScene takes no arguments')
                    from world_service import scene_loaded
                    responses.extend(scene_loaded(session))
                if name == 'saveClientSettings':
                    if len(arguments) < 1:
                        raise ValueError('saveClientSettings needs ID and BLOB')
                    identifier = arguments[0]
                    value, end = packed_bytes(arguments, 1)
                    if end != len(arguments) or len(value) > 256 * 1024:
                        raise ValueError('invalid client setting BLOB')
                    def change(character):
                        settings = character.setdefault('client_settings', {})
                        settings[str(identifier)] = value.hex()
                    character_store.transact(session, change)
                if name == 'saveShortcut':
                    value, end = packed_bytes(arguments, 0)
                    if end != len(arguments) or len(value) > 256 * 1024:
                        raise ValueError('invalid shortcut BLOB')
                    # GUI setup can emit empty slots before the scene and
                    # learned skills are ready. Never let that overwrite the
                    # login snapshot awaiting restoration.
                    if (not session.world_ready
                            or getattr(session, 'shortcut_restore_pending', '')):
                        LOG.info('Ignoring shortcut save while loading: character=%d bytes=%d',
                                 session.selected_character, len(value))
                        continue
                    def change(character):
                        character['shortcut'] = value.hex()
                    character_store.transact(session, change)
                    LOG.info('Shortcut saved: character=%d bytes=%d',
                             session.selected_character, len(value))
                if name == 'onTeleportOver':
                    token, end = packed_bytes(arguments, 0)
                    if end != len(arguments):
                        raise ValueError('onTeleportOver needs one UUID')
                    from world_service import scene_loaded
                    responses.extend(scene_loaded(session, teleport_token=token))
                if name == 'gotoTeleportPoint':
                    if len(arguments) != 4:
                        raise ValueError('gotoTeleportPoint needs one NPC_ID')
                    from teleport_service import goto_point
                    responses.extend(goto_point(session, struct.unpack('<I', arguments)[0]))
                if name in ('onPatrolOver', 'onPatrolFailed'):
                    if arguments:
                        raise ValueError(name + ' takes no arguments')
                    from tutorial_service import patrol_result
                    responses.extend(patrol_result(session, name == 'onPatrolOver'))
                if name == 'logOff':
                    character_store.flush(session)
                    LOG.info('Character state flushed before client logout')
                if name == 'abandonQuest':
                    if len(arguments) != 4:
                        raise ValueError('quest abandonment needs one QUEST_NO')
                    from quest_service import abandon
                    responses.extend(abandon(session, struct.unpack('<I', arguments)[0]))
                if name == 'exchangeInv':
                    if len(arguments) != 6:
                        raise ValueError('inventory exchange needs PAGE, POS, AMOUNT, PAGE, POS')
                    from inventory_service import exchange
                    responses.extend(exchange(session, *struct.unpack('<bbHbb', arguments)))
                if name == 'exchangeInvEqu':
                    if len(arguments) != 3:
                        raise ValueError('equipment exchange needs PAGE, POS, PART')
                    from equipment_service import exchange
                    responses.extend(exchange(session, *struct.unpack('<bbB', arguments)))
                if name == 'discardItem':
                    if len(arguments) != 4:
                        raise ValueError('discardItem needs PAGE, POS, AMOUNT')
                    from inventory_service import discard_item
                    responses.extend(discard_item(session, *struct.unpack('<bbH', arguments)))
                if name == 'discardEquip':
                    if len(arguments) != 1:
                        raise ValueError('discardEquip needs PART')
                    from equipment_service import discard
                    responses.extend(discard(session, arguments[0]))
                if name in ('queryMallHotItems', 'queryMallAllItems', 'searchMallAllItemsByKind',
                            'queryMallNewItems', 'queryMallCreditItems', 'queryMallHistoryItems',
                            'queryMallPackageItems', 'searchMallAllItemsByName', 'searchMallCreditItems'):
                    from mall_service import query
                    responses.extend(query(session, name, arguments))
                if name == 'buyMallItems':
                    from mall_service import buy
                    responses.extend(buy(session, arguments))
                if name == 'sortInvItems':
                    if arguments:
                        raise ValueError('sortInvItems takes no arguments')
                    from inventory_service import sort_items
                    responses.extend(sort_items(session))
                if name == 'useItem':
                    if len(arguments) != 2:
                        raise ValueError('useItem needs PAGE, POS')
                    from inventory_service import use_item
                    responses.extend(use_item(session, *struct.unpack('<bb', arguments)))
                if name == 'ridesInform':
                    if arguments:
                        raise ValueError('ridesInform takes no arguments')
                    from mount_service import inform
                    responses.extend(inform(session))
                if name == 'platesInform':
                    if arguments:
                        raise ValueError('platesInform takes no arguments')
                    # The mount window requests both collections. No saddle
                    # ownership is created by learning a mount item.
                    if session.world_ready:
                        from entity_methods import avatar_client_message
                        responses.append(avatar_client_message(session, 'plate_all',
                                                               {'active': -1, 'last': 0, 'plates': []}))
                if name == 'rideOn':
                    if len(arguments) != 1:
                        raise ValueError('rideOn needs INDEX')
                    from mount_service import ride_on
                    responses.extend(ride_on(session, arguments[0]))
                if name in ('rideOff', 'dismountRide'):
                    if arguments:
                        raise ValueError(name + ' takes no arguments')
                    from mount_service import ride_off
                    responses.extend(ride_off(session))
                if name == 'useItemOnTarget':
                    if len(arguments) != 6:
                        raise ValueError('useItemOnTarget needs PAGE, POS, EID')
                    from quest_event_service import use_quest_item
                    _, replies = use_quest_item(session, *struct.unpack('<bbI', arguments))
                    responses.extend(replies)
                if name == 'onClientScriptFinished':
                    token, pos = packed_bytes(arguments, 0)
                    if len(token) != 16 or len(arguments) != pos + 1:
                        raise ValueError('onClientScriptFinished needs UUID, BOOL')
                    from quest_story import script_finished
                    responses.extend(script_finished(session, token, bool(arguments[pos])))
                if name == 'levelUp':
                    if arguments:
                        raise ValueError('levelUp takes no arguments')
                    from progression_service import level_up
                    responses.extend(level_up(session))
                if name == 'usePotential':
                    if len(arguments) != 14 or struct.unpack_from('<I', arguments)[0] != 5:
                        raise ValueError('usePotential needs ARRAY of five UINT16 points')
                    from growth_service import allocate
                    responses.extend(allocate(session, struct.unpack_from('<5H', arguments, 4)))
                if name == 'learnSpell':
                    if len(arguments) != 2:
                        raise ValueError('learnSpell needs SPELL_ID')
                    from skill_service import learn
                    responses.extend(learn(session, struct.unpack('<H', arguments)[0]))
                if name == 'performSkill':
                    if len(arguments) != 4 or arguments[2] not in (0, 1):
                        raise ValueError('performSkill needs SKILL_ID, BOOL, WARRIOR_SID')
                    from battle_service import command
                    identifier, enhanced, target = struct.unpack('<HBB', arguments)
                    responses.extend(command(session, 'skill', target=target,
                                             skill_id=identifier, enhanced=bool(enhanced)))
                if name == 'activeSummon':
                    token, pos = packed_bytes(arguments, 1)
                    if pos != len(arguments):
                        raise ValueError('activeSummon needs POS, UUID')
                    from summon_service import activate
                    responses.extend(activate(session, struct.unpack_from('<b', arguments)[0], token))
                if name == 'callSummonToFight':
                    if len(arguments) != 1:
                        raise ValueError('callSummonToFight needs POS')
                    from summon_service import join_battle
                    responses.extend(join_battle(session, struct.unpack('<b', arguments)[0]))
                if name == 'summonLevelUp':
                    if len(arguments) != 1:
                        raise ValueError('summonLevelUp needs POS')
                    from summon_progression import level_up as summon_level_up
                    responses.extend(summon_level_up(session, struct.unpack('<b', arguments)[0]))
                if name == 'summonUsePotential':
                    if len(arguments) != 15 or struct.unpack_from('<I', arguments, 1)[0] != 5:
                        raise ValueError('summonUsePotential needs POS and ARRAY of five UINT16 points')
                    from summon_progression import allocate
                    responses.extend(allocate(session, struct.unpack_from('<b', arguments)[0],
                                              struct.unpack_from('<5H', arguments, 5)))
                if name in ('physicsAttack', 'guard', 'escapeBattle', 'useBattleItem'):
                    from battle_service import command
                    if name == 'physicsAttack':
                        if len(arguments) != 1:
                            raise ValueError('physicsAttack needs WARRIOR_SID')
                        responses.extend(command(session,'attack',target=arguments[0]))
                    elif name == 'useBattleItem':
                        if len(arguments) != 3:
                            raise ValueError('useBattleItem needs WARRIOR_SID, PAGE, POS')
                        target, page, pos = struct.unpack('<Bbb',arguments)
                        responses.extend(command(session,'item',target=target,page=page,pos=pos))
                    else:
                        if arguments:
                            raise ValueError(name + ' takes no arguments')
                        responses.extend(command(session,'guard' if name=='guard' else 'escape'))
                if name in ('summonPhysicAttack', 'summonGuard'):
                    if len(arguments) != (1 if name == 'summonPhysicAttack' else 0):
                        raise ValueError('incorrect basic summon command arguments')
                    from summon_service import battle_command
                    responses.extend(battle_command(session, 'attack' if name == 'summonPhysicAttack' else 'guard',
                                                     arguments[0] if arguments else 2))
                if name in ('summonPerformSkill','captureMonster','protect','summonProtect'):
                    from battle_service import command
                    responses.extend(command(session,'unsupported'))
                if name == 'reportException':
                    _, pos = packed_bytes(arguments, 0)
                    count = struct.unpack_from('<I', arguments, pos)[0]
                    pos += 4
                    if count > 100:
                        raise ValueError('oversized client exception report')
                    lines = []
                    for _ in range(count):
                        line, pos = packed_bytes(arguments, pos)
                        # Locals may contain private account data; retain only
                        # exception type and stack locations in readable logs.
                        if not line.startswith(b'Locals:'):
                            lines.append(line.decode('gbk', 'replace'))
                    LOG.error('Client script error: %s', '\n'.join(lines))
                continue
            LOG.info('Account base method=%d payload_bytes=%d', method, length)
            if message & 0x40 and method in (0, 1):
                # Parse the first password field for framing validation only.
                # Never write passwords, hardware IDs, or hashes to text logs.
                packed_bytes(payload, 0)
                if not session.account_logged_in:
                    session.account_logged_in = True
                    responses.append(client_method(session, 0, b'\x02\x00'))
                    responses.append(character_list(session))
                    LOG.info('Account accepted; character list sent')
            elif message & 0x40 and method == 4:
                character_store.create(session, payload)
                responses.append(character_list(session))
                LOG.info('Character saved; updated character list sent')
            elif message & 0x40 and method == 5:
                if len(payload) != 8:
                    raise ValueError('selectCharacter needs an INT64 ID')
                selected = struct.unpack('<q', payload)[0]
                if not any(c['dbid'] == selected for c in character_store.characters(session)):
                    raise ValueError('unknown character ID')
                from csxy_protocol import SESSIONS
                if not SESSIONS.claim_character(session, selected):
                    LOG.info('Character selection ignored on superseded channel')
                    continue
                from world_service import initial_messages
                responses.extend(initial_messages(session))
                LOG.info('Character selected; player and scene initialization sent')
            continue
        # The client also sends fixed engine control messages. Its actual
        # enableEntities is ID 8 with a dummy byte (captured from this build).
        if message == 8 and offset < len(body):
            offset += 1
            from world_service import finish_scene_reset
            responses.extend(finish_scene_reset(session))
            continue
        if message in (9, 10):
            # CSXY setSpaceViewportAck / setVehicleAck: EID + uint32 value.
            # Consume the complete fixed message so coalesced movement and
            # load callbacks after it are still processed.
            if len(body) - offset < 8:
                raise ValueError('short native viewport/vehicle acknowledgement')
            entity, value = struct.unpack_from('<II', body, offset)
            offset += 8
            LOG.info('Native space acknowledgement: message=%d entity=%d value=%d', message, entity, value)
            continue
        if message == 0 and len(body) - offset == 6:
            # Captured build-specific enableEntities trailer after 08 00.
            offset = len(body)
            continue
        if message == 2 and len(body) - offset >= 20:
            position = list(struct.unpack_from('<3f', body, offset))
            offset += 20
            if getattr(session,'battle_id','') or not session.world_ready:
                continue
            session.position = position
            character_store.update(session, position=session.position)
            from tutorial_service import movement
            responses.extend(movement(session))
            from quest_story import movement as story_movement
            responses.extend(story_movement(session))
            from quest_offline import movement as offline_movement
            responses.extend(offline_movement(session))
            from npc_service import update_interest
            responses.extend(update_interest(session))
            continue
        if message == 3 and len(body) - offset >= 28:
            # Explicit avatar updates include space and vehicle IDs first.
            incoming_space = struct.unpack_from('<I', body, offset)[0]
            position = list(struct.unpack_from('<3f', body, offset + 8))
            offset += 28
            if (getattr(session,'battle_id','') or not session.world_ready
                    or incoming_space != (getattr(session, 'engine_space_id', 0) or 1)):
                continue
            session.position = position
            character_store.update(session, position=session.position)
            from tutorial_service import movement
            responses.extend(movement(session))
            from quest_story import movement as story_movement
            responses.extend(story_movement(session))
            from quest_offline import movement as offline_movement
            responses.extend(offline_movement(session))
            from npc_service import update_interest
            responses.extend(update_interest(session))
            continue
        if message == 7:  # CSXY requestEntityUpdate, uint16 length then EID/stamps.
            if offset + 2 > len(body):
                raise ValueError('short entity update request')
            length = struct.unpack_from('<H', body, offset)[0]
            offset += 2
            if length < 4 or offset + length > len(body):
                raise ValueError('invalid entity update request length')
            target = struct.unpack_from('<I', body, offset)[0]
            offset += length
            from npc_service import create_npc, find_npc
            npc = find_npc(target, session)
            if npc and target in session.visible_npcs:
                responses.append(create_npc(npc))
            else:
                from companion_scene import entity_update
                responses.extend(entity_update(session, target))
            continue
        LOG.info('Unimplemented engine message=%d remaining_bytes=%d', message, len(body) - offset)
        break
    return responses
