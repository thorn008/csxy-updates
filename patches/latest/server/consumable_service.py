"""Original life/mana reservoirs and exact post-battle replenishment."""
import copy
import functools
import json

import character_store
from csxy_protocol import ROOT
from attribute_service import combat_attributes, message, notice, rules as attribute_rules, save_vitals
from entity_methods import avatar_client_message
from inventory_service import FIRST_BAG_PAGE, InventoryRequestError, intro_reward, delta_messages


@functools.lru_cache(maxsize=1)
def rules():
    return json.loads((ROOT/'server/data/mall_consumables.json').read_text('utf-8'))


def pool_messages(session, character):
    attrs = combat_attributes(character)
    return [avatar_client_message(session, 'update_combat_attrs', {107:attrs['pro107'],108:attrs['pro108']})]


def use_pool_item(session, page, pos):
    current = next((c for c in character_store.characters(session)
                    if c['dbid'] == session.selected_character), None)
    item = next((i for i in (current or {}).get('inventory', [])
                 if (i['page'], i['pos']) == (page, pos)), None)
    definition = rules()['pool_items'].get(str((item or {}).get('id')))
    if definition is None:
        return False, []
    if (not session.world_ready or getattr(session,'superseded',False) or getattr(session,'battle_id','')
            or getattr(session,'scene_transfer',{}) or getattr(session,'scene_messages',[])):
        return True, []
    replies = []

    def change(character):
        if character['world_state'].get('tutorial_battle',{}).get('phase') in ('preparing','input','animating'):
            return
        selected = next((i for i in character['inventory']
                         if (i['page'],i['pos'],i['uuid']) == (page,pos,item['uuid'])), None)
        if not selected or selected['amount'] < 1:
            return
        template = intro_reward()['items'][str(selected['id'])]
        if page != FIRST_BAG_PAGE or not template.get('use_condition',0) & 2:
            raise InventoryRequestError('请在主背包中使用丹药。')
        if not template.get('req_minlv',0) <= character['level'] <= template.get('req_maxlv',255):
            raise InventoryRequestError('角色等级不符合丹药使用要求。')
        if selected['attributes'].get('item_lock') or selected['attributes'].get('time_lock'):
            raise InventoryRequestError('请先解除丹药锁定。')
        key, amount = definition['pool'], definition['amount']
        old = int(combat_attributes(character)[key])
        maximum = attribute_rules()['ranges'][key][1]
        if old+amount > maximum:
            raise InventoryRequestError('命源或法源剩余容量不足，丹药已保留。')
        before = copy.deepcopy(character)
        character['combat_attr'][key] = old+amount
        selected['amount'] -= 1
        if not selected['amount']:
            character['inventory'].remove(selected)
        replies.extend(delta_messages(session,before,character))
        replies.append(message(session,character))
        replies.extend(pool_messages(session,character))
        from quest_service import resync_item_objectives
        replies.extend(resync_item_objectives(session,character))
        replies.append(notice(session,'使用%s，补充%s%d点；战斗结束后自动补给角色与参战召唤兽。' % (
            template['name'],'命源' if key=='pro107' else '法源',amount),chat=True))

    try:
        character = character_store.transact(session,change)
    except InventoryRequestError as error:
        return True,[notice(session,str(error))]
    return True,replies if character is not None else []


def settle_after_battle(session, character, battle, *, emit=True):
    """Called inside the finish transaction; owner first, then actual participant."""
    if battle.get('supply_settled'):
        return []
    battle['supply_settled'] = True
    attrs = combat_attributes(character)
    pools = {key:int(attrs[key]) for key in ('pro107','pro108')}
    changed = False
    pet_changed = False
    from summon_progression import participant
    pet = participant(character,battle) if battle.get('summon_joined') else None
    pet_attrs = pet['attributes'].get('combatAttr',{}) if pet and pet['attributes'].get('slife',0)>0 else None
    for values in (attrs,pet_attrs):
        if values is None:
            continue
        for pool,current,maximum in (('pro107','pro100','pro7'),('pro108','pro101','pro8')):
            need = max(0,int(values.get(maximum,0))-int(values.get(current,0)))
            added = min(pools[pool],need)
            if added:
                values[current] = int(values.get(current,0))+added
                pools[pool] -= added
                changed = True
                if values is pet_attrs:
                    pet_changed = True
    if not changed:
        return []
    save_vitals(character,attrs)
    character['combat_attr'].update(pools)
    if not emit:
        return []
    replies = pool_messages(session,character)
    if pet_changed:
        replies.append(avatar_client_message(session,'res_setattr',0,pet['page'],pet['pos'],
                                             [('combatAttr',pet_attrs)]))
    return replies
