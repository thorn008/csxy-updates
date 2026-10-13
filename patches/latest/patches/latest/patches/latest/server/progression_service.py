"""Persistent XP using original thresholds and explicit simulator quest awards."""
import character_store
from attribute_service import notice
from entity_methods import avatar_client_message, avatar_property_message
from quest_catalog import catalog


def grant_experience(character, amount):
    """Shared atomic XP/auto-level policy for quest and combat rewards."""
    rule = catalog()['simulator_rules']
    character['experience'] += amount
    upgraded = []
    while character['level'] < rule['auto_level_until']:
        need = catalog()['experience'][str(character['level'])]['avatar']
        if character['experience'] < need:
            break
        character['experience'] -= need
        character['level'] += 1
        upgraded.append(character['level'])
    from growth_service import reconcile
    # Login repair handles legacy level baselines. During a reward transaction
    # only an actual level transition refills the new maxima; awarding XP to a
    # damaged high-level character must not heal it as a side effect.
    if upgraded:
        reconcile(character, refill=True)
    elif character.get('world_state', {}).get('attribute_growth'):
        reconcile(character)
    from skill_service import reconcile as reconcile_skills
    reconcile_skills(character)
    return {'experience': amount, 'levels_gained': upgraded, 'experience_policy': 'simulator-v1'}


def grant_quest_experience(character, quest):
    # Preserve already established first-six reward behavior and old saves.
    if 45195 <= quest['id'] <= 45200:
        return {}
    level = max(1, min(130, quest.get('level', character['level'])))
    fraction = catalog()['simulator_rules']['quest_experience_threshold_fraction']
    from quest_controller import reward_multiplier
    return grant_experience(character, int(catalog()['experience'][str(level)]['avatar'] * fraction
                                           * reward_multiplier(character, quest['id'])))


def grant_battle_experience(character, battle):
    """Award one committed victory, independently of pet and item rewards."""
    if battle.get('outcome') != 'victory':
        return {}
    if 'avatar_experience' in battle:
        return battle['avatar_experience']
    level = max(1, min(130, int(battle.get('encounter', {}).get('level', 1))))
    # Original server combat XP rates are absent. Keep the established
    # simulator reward fraction explicit rather than inventing an original rate.
    fraction = catalog()['simulator_rules']['quest_experience_threshold_fraction']
    amount = max(1, int(catalog()['experience'][str(level)]['avatar'] * fraction))
    battle['avatar_experience'] = grant_experience(character, amount)
    battle['avatar_experience']['experience_policy'] = 'simulator-battle-xp-v1'
    return battle['avatar_experience']


def battle_experience_messages(session, character, battle):
    reward = battle.get('avatar_experience', {})
    if battle.get('outcome') != 'victory' or not reward:
        return []
    if reward['levels_gained']:
        return messages(session, character)
    return [avatar_client_message(session, 'update_exp', character['experience'])]


def messages(session, character):
    from attribute_service import message
    from growth_service import potential
    from skill_service import messages as skill_messages
    from companion_service import collection
    from team_service import messages as team_messages
    return [avatar_property_message(session, 'clevel', character['level']),
            avatar_client_message(session, 'update_exp', character['experience']), message(session, character),
            avatar_client_message(session, 'update_potential', potential(character))] + skill_messages(session, character) + (
                team_messages(session, character) if collection(character)['owned'] else [])


def level_up(session):
    if not session.world_ready or getattr(session, 'battle_id', ''):
        return []
    changed = []

    def change(character):
        row = catalog()['experience'].get(str(character['level']))
        if row and character['level'] < 130 and character['experience'] >= row['avatar']:
            character['experience'] -= row['avatar']
            character['level'] += 1
            from growth_service import reconcile
            reconcile(character, refill=True)
            from skill_service import reconcile as reconcile_skills
            reconcile_skills(character)
            changed.append(True)

    character = character_store.transact(session, change)
    if character is None:
        return []
    if changed:
        from companion_scene import update_interest
        return messages(session, character) + update_interest(session, character)
    return [notice(session, '经验尚未达到升级要求。')]
