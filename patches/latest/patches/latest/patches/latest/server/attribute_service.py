"""Equipment and original level formulas over the simulator's level-1 baseline."""
import functools
import json
import math

from csxy_protocol import ROOT
from entity_methods import avatar_client_message, avatar_property_message


@functools.lru_cache(maxsize=1)
def rules():
    return json.loads((ROOT / 'server/data/attribute_rules.json').read_text('utf-8'))


def equipment_attributes(character):
    """Match Item.calcEquipBaseAttrs, including rounding to original units."""
    from inventory_service import intro_reward
    recovered = rules()
    result = {}
    for item in character['equipment'].values():
        template = intro_reward()['equipment'].get(str(item['id']))
        if template is None:
            continue
        properties = item['attributes']
        # Item.initEquipment defaults both fields to zero. Template quality is
        # not the per-instance colour and must not multiply plain rewards.
        colour = properties.get('color', 0)
        strength = properties.get('strength', 0)
        ratio = (1 + 0.05 * colour) * (1 + 0.03 * strength + 0.05 * int(strength / 10))
        for key, value in template.get('attrs', {}).items():
            factor = 1.0 if key in recovered['unscaled_equipment_attributes'] else ratio
            unit = recovered['equipment_units'][key]
            amount = unit * math.ceil(value * factor / unit)
            target = 'pro' + key[2:]
            result[target] = result.get(target, 0.0) + amount
    return result


def combat_attributes(character):
    # Preserve the established level-1 baseline; growth is derived on each read.
    values = {'pro100': 100, 'pro101': 100, 'pro107': 0, 'pro108': 0,
              'pro7': 100, 'pro8': 100, 'pro73': 1.0, 'pro80': 1.0, 'pro89': 1.0}
    values.update(character['combat_attr'])
    values['pro105'] = bytes.fromhex(character['physique'])[0]
    values['pro106'] = character['level']
    from growth_service import contributions
    growth = contributions(character)
    for key, value in growth.items():
        if key in ('pro1', 'pro2', 'pro3', 'pro4', 'pro5') or key.startswith('pro11'):
            values[key] = value
        else:
            values[key] = values.get(key, 0) + value
    for target, attack in (('pro37', 'pro31'), ('pro38', 'pro31'),
                           ('pro47', 'pro41'), ('pro48', 'pro41')):
        if target in values:
            values[target] += growth[attack]
    baseline = dict(values)
    bonuses = equipment_attributes(character)
    for key, value in bonuses.items():
        values[key] = values.get(key, 0) + value
    # Accessory primary stats feed the same formulas as allocated points.
    # Keep these contributions derived so repeated equip/relogin cannot stack.
    from growth_service import coefficients
    rates = coefficients(character)
    for target, primary, factor in (
            ('pro7', 'pro1', 'gengu_xishu'), ('pro8', 'pro2', 'lingli_xishu'),
            ('pro6', 'pro4', 'minjie_xishu'), ('pro31', 'pro3', 'liliang_xishu'),
            ('pro41', 'pro5', 'zhihui_xishu')):
        if primary in bonuses:
            values[target] = values.get(target, 0) + bonuses[primary] * rates[factor]
    # Original CombatAttrCalculator pro37/38/47/48 depend on weapon strength
    # and coefficients after their equipment additions have been calculated.
    for maximum, minimum, attack, weapon, high, low in (
            ('pro37', 'pro38', 'pro31', 'pro121', 'pro39', 'pro40'),
            ('pro47', 'pro48', 'pro41', 'pro122', 'pro49', 'pro50')):
        if any(key in values for key in (attack, weapon, high, low)):
            for target, coefficient in ((maximum, high), (minimum, low)):
                original = (baseline.get(attack, 0)
                            + baseline.get(weapon, 0) * baseline.get(coefficient, 0))
                equipped = (values.get(attack, 0)
                            + values.get(weapon, 0) * values.get(coefficient, 0))
                values[target] = baseline.get(target, original) + equipped - original
    for key, value in list(values.items()):
        bounds = rules()['ranges'].get(key)
        if bounds:
            value = min(bounds[1], max(bounds[0], value))
        if rules()['attributes'].get(key, {}).get('type') == 'int':
            value = int(value)
        values[key] = value
    battle = character.get('world_state', {}).get('tutorial_battle', {})
    if battle.get('phase') in ('preparing', 'input', 'animating'):
        for key, amount in battle.get('formation_player', {}).get('changes', {}).items():
            values[key] = values.get(key, 0) + amount
            # Keep attack ranges coherent with the underlying strength bonus.
            for target in {'pro31': ('pro37', 'pro38'), 'pro41': ('pro47', 'pro48')}.get(key, ()):
                values[target] = values.get(target, 0) + amount
    values['pro7'] = max(1, values['pro7'])
    values['pro100'] = min(values['pro100'], values['pro7'])
    values['pro101'] = min(values['pro101'], values['pro8'])
    return values


def save_vitals(character, values):
    """Store mutable HP/MP without feeding equipment totals into the next read."""
    character['combat_attr'].update({key: int(values[key]) for key in ('pro100', 'pro101')})


def message(session, character):
    # The FIXED_DICT property supplies native types and defaults, including
    # zero bonuses after unequipping. Replacement alone does not notify UI.
    attrs = combat_attributes(character)
    # Follow it in the same Mercury bundle with the verified HP/MP update.
    # setCombatAttr emits AVATAR_COMBAT_ATTR, and the panel then reads the
    # entire refreshed object. A raw PYTHON snapshot of every field caused
    # TypeError in the real client's setCombatAttr.
    return (avatar_property_message(session, 'combatAttr', attrs)
            + avatar_client_message(session, 'update_combat_attrs',
                                    {100: attrs['pro100'], 101: attrs['pro101']}))


def vitals_message(session, character):
    # PlayerAvatar.update_combat_attrs converts integer indices via
    # csxyattr.indexToPro and calls setCombatAttr, which emits the UI signal.
    # Replacing combatAttr alone does not notify the portrait/status widgets.
    attrs = combat_attributes(character)
    return avatar_client_message(session, 'update_combat_attrs',
                                 {100: attrs['pro100'], 101: attrs['pro101']})


def notice(session, text, *, chat=False):
    # MSG_B=2 writes to the system chat; MSG_C=4 is the screen notice box.
    return avatar_client_message(session, 'show_msg', 6 if chat else 4, text, 0)
