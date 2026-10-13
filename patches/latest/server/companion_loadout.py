"""Read-only, level-appropriate companion equipment from original item tables.

Best means the highest deterministic combat score per slot: prefer the
school's attack, then survivability and speed. No random affixes, strengthening,
inventory items or currency are generated for the player.
"""
import copy
import functools
import math
import re
import struct

MAGIC_SCHOOLS = {1, 2, 3, 5, 6}


def equipment_score(template, school):
    from attribute_service import rules
    from growth_service import rules as growth_rules
    rates = growth_rules()['simulator_coefficients']
    recovered = rules()
    attrs = {}
    for key, value in template.get('attrs', {}).items():
        unit = recovered['equipment_units'][key]
        attrs['pro' + key[2:]] = unit * math.ceil(value / unit)
    for target, primary, factor in (
            ('pro7', 'pro1', 'gengu_xishu'), ('pro8', 'pro2', 'lingli_xishu'),
            ('pro31', 'pro3', 'liliang_xishu'), ('pro41', 'pro5', 'zhihui_xishu'),
            ('pro6', 'pro4', 'minjie_xishu')):
        attrs[target] = attrs.get(target, 0) + attrs.get(primary, 0) * rates[factor]
    magic = school in MAGIC_SCHOOLS
    attack, weapon = ('pro41', 'pro122') if magic else ('pro31', 'pro121')
    return (4 * (attrs.get(attack, 0) + attrs.get(weapon, 0))
            + attrs.get('pro32', 0) + attrs.get('pro42', 0)
            + .1 * attrs.get('pro7', 0) + .08 * attrs.get('pro8', 0)
            + 2 * attrs.get('pro6', 0))


@functools.lru_cache(maxsize=1040)
def selection(school, sex, level):
    from inventory_service import intro_reward
    from equipment_service import EQUIPMENT_SIZE, RING_PARTS
    data = intro_reward()
    selected = {}
    for key, template in data['equipment'].items():
        base = data['items'].get(key, {})
        req = template.get('req', {})
        # Zero-requirement entries include original GM test weapons and
        # precomputed forge fixtures; they are not normal wearable progression.
        if (base.get('type') != 0 or not 1 <= base.get('req_minlv', 0) <= level
                or (base.get('req_maxlv') and level > base['req_maxlv'])
                or (req.get('school') and school not in req['school'])
                or (req.get('sex') and sex not in req['sex'])):
            continue
        subtype = base.get('stype')
        if re.fullmatch(r'\d+(,\d+)+', base.get('name', '')):
            continue  # Unfinished numeric-name placeholder templates.
        # Normal original leggings also use model=1. Preserve their native
        # appearance sentinel rather than rejecting legitimate stat equipment.
        slots = RING_PARTS if subtype == 10 else (14,) if subtype == 11 else (subtype,)
        score = (equipment_score(template, school), base.get('req_minlv', 0),
                 template.get('quality', 0), -int(key))
        for slot in slots:
            if type(slot) is int and 0 <= slot < EQUIPMENT_SIZE:
                if slot not in selected or score > selected[slot][0]:
                    selected[slot] = (score, int(key))
    return tuple((slot, row[1]) for slot, row in sorted(selected.items()))


def equip(actor):
    from inventory_service import intro_reward
    from equipment_service import ASPECT_FIELDS, MODEL_PARTS, validate_equipment
    school, sex = bytes.fromhex(actor['physique'])[:2]
    appearance = dict.fromkeys(ASPECT_FIELDS, 0)
    actor['equipment'] = {}
    for slot, identifier in selection(school, sex, actor['level']):
        template = intro_reward()['equipment'][str(identifier)]
        item = {'id': identifier, 'amount': 1, 'page': 0, 'pos': slot,
                'uuid': 'companion-equipment-%d-%d' % (slot, identifier),
                'attributes': {'color': 0, 'strength': 0, 'dur': template.get('mdur', 0)}}
        validate_equipment(actor, item, slot)
        actor['equipment'][str(slot)] = copy.deepcopy(item)
        if slot in MODEL_PARTS:
            appearance[ASPECT_FIELDS[slot]] = template.get('model', 0)
    actor['aspect'] = struct.pack('<12I3bB', *appearance.values()).hex()
    actor['world_state']['companion_loadout'] = {'policy': 'school-combat-score-v1',
                                               'level': actor['level']}
