"""First-bag reward storage and original inventory synchronization messages."""
import functools
import copy
import json
import logging
import uuid

from csxy_protocol import ROOT
from entity_methods import avatar_client_message

RES_KIND_INV = 0
# const.INV_PAGE_FIXED=0; page 1 is the first additional bag.
FIRST_BAG_PAGE = 0
QUEST_BAG_PAGE = 6
BAG_SIZE = 24
INTRO_BAG_LAYOUT = 1
LOG = logging.getLogger('inventory')


@functools.lru_cache(maxsize=1)
def intro_reward():
    return json.loads((ROOT / 'server/data/intro_reward_items.json').read_text('utf-8'))


def grant_intro(character):
    """Called inside a character transaction so items and task commit together."""
    return grant_reward(character, 45195)


def max_stack(identifier):
    return intro_reward()['items'].get(str(identifier), {}).get('mwrap', 1)


class InventoryFullError(ValueError):
    """No partial reward may be published when its container is full."""


def grant_reward(character, quest_no):
    """Allocate original quest rewards, stacking only identical plain items."""
    school, sex = bytes.fromhex(character['physique'])[:2]
    reward = dict(intro_reward()['quest_rewards'][str(quest_no)])
    from quest_catalog import definition
    # Both exports contain the same literal reward. Prefer the quest table
    # without appending its item list a second time.
    reward.update((definition(quest_no) or {}).get('cq', {}).get('reward', {}))
    selected = [(item_id, amount) for req_school, req_sex, item_id, amount in reward.get('items', [])
                if req_school in (0, school) and req_sex in (0, sex)]
    from quest_rewards import drop_rewards, circle_cash
    drops, receipts = drop_rewards(character, quest_no, reward)
    selected.extend(drops)
    from quest_controller import reward_multiplier
    multiplier = reward_multiplier(character, quest_no)
    cash, policy = circle_cash(character, quest_no)
    amounts = {key: int((reward.get(key, 0) + cash[key]) * multiplier)
               for key in ('cash', 'binded_cash')}
    if quest_no == 45195 and not any(str(identifier) in intro_reward()['equipment'] for identifier, _ in selected):
        raise ValueError('no original introductory weapon for character school')
    granted = grant_items(character, selected)
    character['currency']['cash'] += amounts['cash']
    character['currency']['binded_cash'] += amounts['binded_cash']
    yuanbao = int(reward.get('yuanbao', 0) or 0)
    if yuanbao:
        wallet = character['currency'].setdefault('yuanbao', {'pay': 0, 'free': 0})
        wallet['free'] = int(wallet.get('free', 0)) + yuanbao
        if not 0 <= wallet['free'] + int(wallet.get('pay', 0)) < 2 ** 32:
            raise ValueError('yuanbao exceeds client UINT32 range')
    if not 0 <= character['currency']['cash'] < 2 ** 31:
        raise ValueError('cash exceeds client INT_CASH range')
    if not 0 <= character['currency']['binded_cash'] < 2 ** 31:
        raise ValueError('bound cash exceeds client range')
    return {**amounts,
            'yuanbao': yuanbao, 'items': selected, 'item_uuids': granted,
            'drop_libraries': receipts, 'cash_policy': policy, 'reward_multiplier': multiplier,
            'inventory_layout': INTRO_BAG_LAYOUT}


def grant_items(character, selected, page=FIRST_BAG_PAGE, *, properties=None):
    """Allocate items inside the caller's transaction; instances never alias."""
    granted = []
    for identifier, amount in selected:
        target_page = page
        attributes = copy.deepcopy(properties or {})
        capacity = BAG_SIZE
        if identifier == 20154:
            identifier = 21154
        template = intro_reward()['items'].get(str(identifier), {})
        if template.get('type') == 6 and template.get('stype') in (0, 1):
            from summon_service import PAGE, CAPACITY, initial_attributes
            target_page = PAGE
            capacity = CAPACITY
            from quest_catalog import catalog as quest_catalog
            if str(identifier) in quest_catalog()['summons']:
                attributes = initial_attributes(identifier)
        occupied = {item['pos'] for item in character['inventory'] if item['page'] == target_page}
        free = [position for position in range(capacity) if position not in occupied]
        if str(identifier) not in intro_reward()['items']:
            raise ValueError('quest reward item template is not restored')
        limit = max_stack(identifier)
        for item in sorted(character['inventory'], key=lambda value: (value['page'], value['pos'])):
            if (item['id'] == identifier and item['page'] == target_page
                    and item['attributes'] == attributes and item['amount'] < limit):
                added = min(amount, limit - item['amount'])
                item['amount'] += added
                amount -= added
                if added:
                    granted.append(item['uuid'])
                if not amount:
                    break
        while amount:
            if not free:
                raise InventoryFullError('inventory bag has insufficient reward space')
            item = {'id': identifier, 'uuid': uuid.uuid4().hex, 'amount': min(amount, limit),
                    'page': target_page, 'pos': free.pop(0), 'attributes': copy.deepcopy(attributes)}
            character['inventory'].append(item)
            granted.append(item['uuid'])
            amount -= item['amount']
    return list(dict.fromkeys(granted))


def repair_summon_bag(session):
    """Move legacy summon supplies to the bag without replacing instances."""
    import character_store
    moved = []

    def change(character):
        supplies = [i for i in character['inventory'] if i['page'] == 7
                    and intro_reward()['items'].get(str(i['id']), {}).get('type') == 6
                    and intro_reward()['items'][str(i['id'])].get('stype') not in (0, 1)]
        occupied = {i['pos'] for i in character['inventory'] if i['page'] == FIRST_BAG_PAGE}
        free = [p for p in range(BAG_SIZE) if p not in occupied]
        if len(supplies) > len(free):
            raise InventoryFullError('背包空间不足，召唤兽道具暂未移出兽栏。')
        for item, pos in zip(supplies, free):
            moved.append((item['uuid'], item['pos'], pos))
            item.update(page=FIRST_BAG_PAGE, pos=pos)
            # Check wire compatibility before saving the repaired instance.
            avatar_client_message(session, 'res_insert', 0, FIRST_BAG_PAGE, pos, item_info(item))

    character = character_store.transact(session, change)
    if moved:
        LOG.info('Summon supply bag repaired: moved=%d', len(moved))
    return character, moved


def use_currency_gift(session, page, pos):
    """Use literal cash-only quest gifts, including original item 74093."""
    import character_store
    from attribute_service import notice
    from quest_catalog import catalog
    current = next((c for c in character_store.characters(session)
                    if c['dbid'] == session.selected_character), None)
    item = next((i for i in (current or {}).get('inventory', [])
                 if (i['page'], i['pos']) == (page, pos)), None)
    data = catalog()['quest_items'].get(str((item or {}).get('id')), {})
    gains = data.get('gain', {}).get('items', [])
    # These are original const.ITEM_ID_CASH and ITEM_ID_BINDEDCASH. Gifts
    # with requirements, counters or other effects retain the quest handler.
    currencies = {23904: 'cash', 23935: 'binded_cash'}
    if (not gains or set(data) != {'id', 'gain'} or set(data['gain']) != {'items'}
            or any(identifier not in currencies or type(amount) is not int or amount <= 0
                   for identifier, amount in gains)
            or intro_reward()['items'].get(str(item['id']), {}).get('mlife') != 1):
        return False, []
    replies = []

    def change(character):
        if character['world_state'].get('tutorial_battle', {}).get('phase') in ('preparing', 'input', 'animating'):
            return
        selected = next((i for i in character['inventory']
                         if (i['page'], i['pos'], i['uuid']) == (page, pos, item['uuid'])), None)
        if selected is None or selected['amount'] <= 0:
            return
        before = copy.deepcopy(character)
        totals = {}
        for identifier, amount in gains:
            key = currencies[identifier]
            totals[key] = totals.get(key, 0) + amount
            character['currency'][key] += amount
            if not 0 <= character['currency'][key] < 2 ** 31:
                raise InventoryRequestError('银两已达到上限，请先使用部分银两。')
        selected['amount'] -= 1
        if not selected['amount']:
            character['inventory'].remove(selected)
        replies.extend(delta_messages(session, before, character))
        rewards = '，'.join('%d%s' % (n, '银两' if k == 'cash' else '绑定银两') for k, n in totals.items())
        replies.append(notice(session, '使用%s，获得%s。' % (intro_reward()['items'][str(item['id'])]['name'], rewards), chat=True))

    try:
        character_store.transact(session, change)
    except ValueError as error:
        return True, [notice(session, str(error))]
    if replies:
        LOG.info('Currency gift committed: item=%d gains=%s', item['id'], gains)
    return True, replies


def repair_intro_bag(session):
    """Move only rewards from the known page-1 bug, once, in a transaction."""
    import character_store
    moved = []

    def change(character):
        quest = character['tasks'].get('45195', {})
        reward = quest.get('reward', {})
        if (quest.get('status') != 'completed' or not quest.get('rewarded')
                or reward.get('inventory_layout', 0) >= INTRO_BAG_LAYOUT):
            return
        identifiers = set(reward.get('item_uuids', []))
        items = [item for item in character['inventory']
                 if item['uuid'] in identifiers and item['page'] == 1]
        occupied = {item['pos'] for item in character['inventory']
                    if item['page'] == FIRST_BAG_PAGE}
        free = [position for position in range(BAG_SIZE) if position not in occupied]
        if len(free) < len(items):
            raise ValueError('first bag has insufficient space for reward layout repair')
        for item, position in zip(items, free):
            moved.append((item['page'], item['pos'], FIRST_BAG_PAGE, position))
            item.update(page=FIRST_BAG_PAGE, pos=position)
        reward['inventory_layout'] = INTRO_BAG_LAYOUT

    character = character_store.transact(session, change)
    if moved:
        LOG.info('Introductory reward bag layout repaired and saved: items=%d', len(moved))
    return character, moved


def repair_messages(session):
    character, moved = repair_intro_bag(session)
    if character is None or not moved:
        return []
    result = [avatar_client_message(session, 'res_grant', RES_KIND_INV, FIRST_BAG_PAGE, BAG_SIZE)]
    result.extend(avatar_client_message(session, 'res_move_whole', RES_KIND_INV, page, pos,
                                        RES_KIND_INV, new_page, new_pos)
                  for page, pos, new_page, new_pos in moved)
    # The old version also opened an extra bag. Close it only if now empty.
    if not any(item['page'] == 1 for item in character['inventory']):
        result.append(avatar_client_message(session, 'res_grant', RES_KIND_INV, 1, 0))
    return result


class InventoryRequestError(ValueError):
    pass


def removal_error(item, template):
    """Protection rules shared by inventory, equipment and NPC sales."""
    if not template:
        return '该物品尚未恢复，不能移除。'
    if item.get('attributes', {}).get('item_lock') or item.get('attributes', {}).get('time_lock'):
        return '请先解除物品锁定。'
    intro_ids = {r[2] for r in intro_reward()['quest_rewards']['45195'].get('items', [])
                 if str(r[2]) in intro_reward().get('equipment', {})}
    if template.get('ndrop') or template.get('is_quest') or item['id'] in intro_ids:
        return '该物品不能丢弃。'
    if template.get('type') == 6 and template.get('stype') in (0, 1):
        return '召唤兽请在兽栏中管理。'
    return None


def sale_error(item, template):
    """Validate an NPC sale independently from the discard rules.

    The client enables ``canSell`` whenever a template has a cash price.  A
    starter weapon may be protected from dropping while still being a valid
    sale, so reusing :func:`removal_error` incorrectly rejected items such as
    the red-copper greatsword.
    """
    if not template:
        return '该物品尚未恢复，不能出售。'
    if item.get('attributes', {}).get('item_lock') or item.get('attributes', {}).get('time_lock'):
        return '请先解除物品锁定。'
    if int(template.get('sell_cash', 0)) <= 0:
        return '该物品不能出售。'
    return None


def discard_item(session, page, pos, amount):
    """Handle the client's confirmed discard request for a bag item.

    ``discardItem`` is also used for items bought from StoreMan/Mall.  The
    previous restoration shortcut only accepted page 0 consumables, which
    made every normal material and every additional bag item appear to be
    undeletable.  The client has separate storage for quest and summon items;
    ordinary bag items are discardable unless their template or instance is
    explicitly protected.
    """
    import character_store
    from attribute_service import notice
    if not session.world_ready or getattr(session, 'battle_id', ''):
        return []
    # Pages 0..5 are ordinary inventory pages.  Page 6 is the quest bag and
    # page 7 is the summon pen; both have dedicated server-side lifecycle.
    if not 0 <= page < QUEST_BAG_PAGE or not 0 <= pos < BAG_SIZE or not 0 < amount < 2 ** 16:
        return []
    operations = []

    def change(character):
        item = next((value for value in character['inventory']
                     if (value['page'], value['pos']) == (page, pos)), None)
        if item is None or amount > item['amount']:
            raise InventoryRequestError('empty slot or stale discard amount')
        template = intro_reward()['items'].get(str(item['id']))
        if template is None:
            raise InventoryRequestError('该物品不能丢弃。')
        error = removal_error(item, template)
        if error:
            raise InventoryRequestError(error)
        item['amount'] -= amount
        if item['amount']:
            operations.append(('res_wrap', RES_KIND_INV, page, pos, item['amount']))
        else:
            character['inventory'].remove(item)
            operations.append(('res_remove', RES_KIND_INV, page, pos))

        # Encode before commit: a serialization error must not destroy an item.
        for name, *args in operations:
            avatar_client_message(session, name, *args)

    try:
        character = character_store.transact(session, change)
    except InventoryRequestError as error:
        if str(error) != 'empty slot or stale discard amount':
            return [notice(session, str(error))]
        return []
    if character is None:
        return []
    LOG.info('Confirmed inventory discard saved: page=%d slot=%d amount=%d', page, pos, amount)
    return [avatar_client_message(session, name, *args) for name, *args in operations]


def exchange(session, page, pos, amount, new_page, new_pos):
    """Persist moves, splits, stacking or swaps before emitting client replies."""
    import character_store
    if not session.world_ready or getattr(session,'battle_id',''):
        return []
    if (page != FIRST_BAG_PAGE or new_page != FIRST_BAG_PAGE
            or not 0 <= pos < BAG_SIZE or not 0 <= new_pos < BAG_SIZE
            or amount <= 0):
        LOG.info('Inventory exchange rejected: invalid or unopened bag/slot')
        return []
    if (page, pos) == (new_page, new_pos):
        return []
    operations = []

    def change(character):
        source = next((item for item in character['inventory']
                       if (item['page'], item['pos']) == (page, pos)), None)
        target = next((item for item in character['inventory']
                       if (item['page'], item['pos']) == (new_page, new_pos)), None)
        if source is None:
            raise InventoryRequestError('empty source slot')
        if amount > source['amount']:
            raise InventoryRequestError('move amount exceeds the current item amount')
        if (target is not None and source['id'] == target['id']
                and source['attributes'] == target['attributes'] and max_stack(source['id']) > 1):
            moved = min(amount, max_stack(target['id']) - target['amount'])
            if moved <= 0:
                raise InventoryRequestError('target stack is full')
            target['amount'] += moved
            source['amount'] -= moved
            operations.append(('res_wrap', RES_KIND_INV, new_page, new_pos, target['amount']))
            if source['amount']:
                operations.append(('res_wrap', RES_KIND_INV, page, pos, source['amount']))
            else:
                character['inventory'].remove(source)
                operations.append(('res_remove', RES_KIND_INV, page, pos))
            return
        if amount < source['amount']:
            if target is not None:
                raise InventoryRequestError('partial stack cannot swap with another item')
            divided = dict(copy.deepcopy(source), uuid=uuid.uuid4().hex,
                           amount=amount, page=new_page, pos=new_pos)
            source['amount'] -= amount
            character['inventory'].append(divided)
            operations.extend([('res_wrap', RES_KIND_INV, page, pos, source['amount']),
                               ('res_insert', RES_KIND_INV, new_page, new_pos, item_info(divided))])
            return
        source.update(page=new_page, pos=new_pos)
        if target is not None:
            target.update(page=page, pos=pos)
        operations.append(('res_exchange' if target is not None else 'res_move_whole',
                           RES_KIND_INV, page, pos, RES_KIND_INV, new_page, new_pos))

    try:
        character = character_store.transact(session, change)
    except InventoryRequestError as error:
        LOG.info('Inventory exchange rejected: %s', error)
        return []
    if character is None:
        return []
    LOG.info('Inventory drag saved: from=(%d,%d) to=(%d,%d) messages=%d',
             page, pos, new_page, new_pos, len(operations))
    return [avatar_client_message(session, name, *arguments) for name, *arguments in operations]


def sort_items(session):
    import character_store
    from collections import defaultdict
    if not session.world_ready or getattr(session,'battle_id',''):
        return []
    operations = []

    def change(character):
        items = [item for item in character['inventory'] if item['page'] == FIRST_BAG_PAGE]
        groups = defaultdict(list)
        for item in sorted(items, key=lambda value: value['pos']):
            groups[(item['id'], json.dumps(item['attributes'], sort_keys=True))].append(item)
        packed = []
        for (identifier, _), stack in sorted(groups.items(), key=lambda pair: (
                intro_reward()['items'].get(str(pair[0][0]), {}).get('type', 255), pair[0])):
            total = sum(item['amount'] for item in stack)
            limit = max_stack(identifier)
            for item in stack:
                if not total:
                    break
                updated = dict(copy.deepcopy(item), amount=min(total, limit), pos=len(packed))
                packed.append(updated)
                total -= updated['amount']
            if total:
                raise InventoryRequestError('invalid stored stack exceeds template limits')
        old = sorted(items, key=lambda item: item['pos'])
        if old == packed:
            return
        operations.extend(('res_remove', RES_KIND_INV, FIRST_BAG_PAGE, item['pos']) for item in old)
        operations.extend(('res_insert', RES_KIND_INV, FIRST_BAG_PAGE, item['pos'], item_info(item))
                          for item in packed)
        character['inventory'] = [item for item in character['inventory'] if item['page'] != FIRST_BAG_PAGE] + packed

    character = character_store.transact(session, change)
    if character is None:
        return []
    LOG.info('Inventory sorted and saved: client_updates=%d', len(operations))
    return [avatar_client_message(session, name, *arguments) for name, *arguments in operations] + [
        avatar_client_message(session, 'sort_inv_items_over')]


def use_item(session, page, pos):
    import character_store
    from attribute_service import combat_attributes, message, notice, save_vitals, vitals_message
    if (getattr(session, 'superseded', False) or not session.world_ready or getattr(session,'battle_id','')
            or getattr(session, 'scene_transfer', {}) or getattr(session, 'scene_messages', [])):
        return []
    if page not in (FIRST_BAG_PAGE, QUEST_BAG_PAGE) or not 0 <= pos < BAG_SIZE:
        return []
    # Mounts are learned through the normal item-use request.  Keep this
    # before quest/box/consumable handling because mount templates are type 14
    # and are otherwise reported as an unimplemented item.
    from mount_service import use_item as use_mount_item
    handled, replies = use_mount_item(session, page, pos)
    if handled:
        return replies
    from consumable_service import use_pool_item
    handled, replies = use_pool_item(session, page, pos)
    if handled:
        return replies
    handled, replies = use_currency_gift(session, page, pos)
    if handled:
        return replies
    from quest_event_service import use_quest_item
    handled, replies = use_quest_item(session, page, pos)
    if handled:
        return replies
    from item_box_service import use_box
    handled, replies = use_box(session, page, pos)
    if handled:
        return replies
    operation = []
    encoded_replies = []

    def change(character):
        if character['world_state'].get('tutorial_battle', {}).get('phase') in ('preparing', 'input', 'animating'):
            return
        item = next((value for value in character['inventory'] if (value['page'],value['pos'])==(page,pos)),None)
        if item is None:
            raise InventoryRequestError('该格子没有物品。')
        effects = {43502: ('pro101','pro8',500), 43505: ('pro100','pro7',500),
                   23021: ('pro100','pro7',400), 23023: ('pro101','pro8',400)}
        if item['id'] not in effects:
            raise InventoryRequestError('该物品的使用功能尚未恢复。')
        template = intro_reward()['items'][str(item['id'])]
        if page != FIRST_BAG_PAGE:
            raise InventoryRequestError('请在主背包中使用药品。')
        if item['attributes'].get('item_lock') or item['attributes'].get('time_lock'):
            raise InventoryRequestError('请先解除药品锁定。')
        if not template.get('req_minlv', 0) <= character['level'] <= template.get('req_maxlv', 255):
            raise InventoryRequestError('角色等级不符合药品使用要求。')
        current, maximum, amount = effects[item['id']]
        attrs = combat_attributes(character)
        if attrs['pro100'] <= 0:
            raise InventoryRequestError('倒地时不能使用该物品。')
        if attrs[current] >= attrs[maximum]:
            raise InventoryRequestError('生命或法力已满，无需使用。')
        attrs[current] = min(int(attrs[maximum]), attrs[current] + amount)
        save_vitals(character, attrs)
        item['amount'] -= 1
        if item['amount']:
            operation.append(('res_wrap', RES_KIND_INV, page, pos, item['amount']))
        else:
            character['inventory'].remove(item)
            operation.append(('res_remove', RES_KIND_INV, page, pos))
        name, *args = operation[0]
        encoded_replies.extend([avatar_client_message(session, name, *args), message(session, character),
                                vitals_message(session, character)])

    try:
        character = character_store.transact(session, change)
    except InventoryRequestError as error:
        return [notice(session, str(error))]
    if character is None or not operation:
        return []
    LOG.info('Consumable used and saved: page=%d slot=%d', page, pos)
    return encoded_replies


def item_info(item):
    # ItemInfo.createObjFromDict calls Item.loadProp(cPickle.loads), then
    # Item.__init__ fills template defaults. A valid empty pickle is allowed.
    from entity_schema import python2_pickle
    attributes = item['attributes']
    if item['page'] == 7 and 'sname' in attributes:
        from summon_service import wire_attributes
        attributes = wire_attributes(attributes)
    return {'id': item['id'], 'uuid': bytes.fromhex(item['uuid']), 'cwrap': item['amount'],
            'attributes': python2_pickle(attributes)}


def messages(session, character):
    pages = {FIRST_BAG_PAGE} | {item['page'] for item in character['inventory']}
    result = [avatar_client_message(session, 'res_grant', RES_KIND_INV, page, 12 if page == 7 else BAG_SIZE)
              for page in sorted(pages)]
    result.extend(avatar_client_message(session, 'res_insert', RES_KIND_INV, item['page'],
                                        item['pos'], item_info(item))
                  for item in character['inventory'])
    result.extend(avatar_client_message(session, 'res_insert', 2, 0, int(part), item_info(item))
                  for part, item in sorted(character['equipment'].items()))
    result.append(avatar_client_message(session, 'update_cash', character['currency']['cash']))
    result.append(avatar_client_message(session, 'update_binded_cash', character['currency']['binded_cash']))
    wallet = character['currency'].get('yuanbao', {})
    result.append(avatar_client_message(session, 'update_yuanbao',
                                        int(wallet.get('pay', 0)) + int(wallet.get('free', 0))))
    for key in ('tongbao', 'credit'):
        result.append(avatar_client_message(session, 'update_' + key, character['currency'].get(key, 0)))
    result.extend(avatar_client_message(session, 'add_retrieve_item', item_info(item))
                  for item in character.get('world_state', {}).get('store_retrieves', []))
    return result


def resync_messages(session, character):
    """Replace stale client slots from the save without changing any items."""
    from equipment_service import EQUIPMENT_SIZE, RES_KIND_EQUIPMENT, aspect_info
    from summon_service import PAGE, CAPACITY, messages as summon_messages
    from attribute_service import message
    from entity_methods import avatar_property_message
    pages = {FIRST_BAG_PAGE, PAGE} | {item['page'] for item in character['inventory']}
    result = []
    for page in sorted(pages):
        size = CAPACITY if page == PAGE else BAG_SIZE
        result.append(avatar_client_message(session, 'res_grant', RES_KIND_INV, page, size))
        result.extend(avatar_client_message(session, 'res_remove', RES_KIND_INV, page, pos)
                      for pos in range(size))
    # Native equipment is a fixed Package(15), without resize(). res_grant
    # applies only to inventory pages; sending it to equipment raises an error.
    result.extend(avatar_client_message(session, 'res_remove', RES_KIND_EQUIPMENT, 0, part)
                  for part in range(EQUIPMENT_SIZE))
    result.extend(messages(session, character))
    from mount_service import visible_aspect
    result.append(avatar_property_message(session, 'aspect', visible_aspect(character)))
    result.append(message(session, character))
    result.extend(summon_messages(session, character))
    return result


def delta_messages(session, before, after):
    """Synchronize removals as well as additions after an atomic quest event."""
    old = {(i['page'], i['pos']): i for i in before['inventory']}
    new = {(i['page'], i['pos']): i for i in after['inventory']}
    result = []
    for page in sorted({p for p, _ in new} - {p for p, _ in old}):
        result.append(avatar_client_message(session, 'res_grant', 0, page, 12 if page == 7 else BAG_SIZE))
    for slot in sorted(old.keys() - new.keys()):
        result.append(avatar_client_message(session, 'res_remove', 0, *slot))
    for slot, item in new.items():
        if old.get(slot) != item:
            result.append(avatar_client_message(session, 'res_insert', 0, *slot, item_info(item)))
    for key in ('cash', 'binded_cash'):
        if before['currency'][key] != after['currency'][key]:
            result.append(avatar_client_message(session, 'update_' + key, after['currency'][key]))
    old_wallet = before['currency'].get('yuanbao', {})
    new_wallet = after['currency'].get('yuanbao', {})
    old_total = int(old_wallet.get('pay', 0)) + int(old_wallet.get('free', 0))
    new_total = int(new_wallet.get('pay', 0)) + int(new_wallet.get('free', 0))
    if old_total != new_total:
        result.append(avatar_client_message(session, 'update_yuanbao', new_total))
    return result
