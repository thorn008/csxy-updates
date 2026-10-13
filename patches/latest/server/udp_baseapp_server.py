"""Encrypted Mercury UDP BaseApp login and evidence capture."""
import datetime
import json
import logging
import math
import os
from pathlib import Path
import socket
import struct
import threading
import time
from world_service import initial_messages, mapping_message, reload_scene
from csxy_protocol import ROOT

from csxy_protocol import (SESSIONS, decrypt_packet, encrypt_packet,
                           packet_parts, reply_bundle, channel_packet)
from account_service import create_account, process_messages, character_list
from evidence_journal import append_record
from capture_settings import OUT as CAPTURE_ROOT, write_packet

OUT = CAPTURE_ROOT / 'baseapp_frames'
LOG = logging.getLogger('baseapp-udp')
FRAGMENTS = {}
LAST_RECEIVED = {}
LAST_SENT = {}
RECEIVE_WINDOWS = {}


def decode_session(data, peer):
    for session in SESSIONS.candidates(peer):
        try:
            # Real client captures are 21-byte, unencrypted once-off login
            # requests. The receive-side channel already has its filter, so
            # our reply is still encrypted. Post-login traffic is encrypted.
            clear = data if len(data) == 21 and data[:5] == b'\x01\x00\x00\x08\x00' else decrypt_packet(session.key, data)
            parts = packet_parts(clear)
            body = parts['body']
            # msgID 0, length 8, replyID(4), next-request offset(2), key(4),
            # attempt(4). The two payload words are written by 0x4fff20.
            if parts['request_offset'] == 2 and len(body) == 17 and body[:3] == b'\x00\x08\x00':
                rid = body[3:7]
                key, attempt = struct.unpack_from('<II', body, 9)
                if body[7:9] != b'\x00\x00' or key != session.login_id:
                    continue
                return session, clear, parts, (rid, attempt)
            if session.base_peer == peer:
                return session, clear, parts, None
        except (ValueError, struct.error):
            continue
    raise ValueError('no matching session or unsupported BaseApp packet')


def response_for(session, parts, login):
    if login:
        return reply_bundle(login[0], struct.pack('<I', session.login_id))
    acks = [p['sequence'] for p in ordered_parts(parts)
            if p['sequence'] is not None and p['flags'] & 0x0010]
    if acks:
        return channel_packet(acks=acks)
    return None


def ordered_parts(parts):
    packets = []
    for piggyback in parts.get('piggybacks', []):
        packets.extend(ordered_parts(piggyback))
    packets.append(parts)
    return sorted(packets, key=lambda p: p['sequence'] if p['sequence'] is not None else -1)


def ordered_application_parts(session, parts):
    """Hold later reliable packets until missing earlier packets are resent."""
    if session.login_id not in RECEIVE_WINDOWS:
        # A saved sequence alone cannot restore the receive window: the old
        # process may have acknowledged newer packets without saving them.
        # Begin at the first datagram of this process's channel lifetime.
        session.last_in_sequence = -1
        RECEIVE_WINDOWS[session.login_id] = {}
    window = RECEIVE_WINDOWS[session.login_id]
    for incoming in ordered_parts(parts):
        sequence = incoming['sequence']
        if sequence is None or not incoming['flags'] & 0x0010:
            yield incoming
            continue
        if session.last_in_sequence < 0:
            session.last_in_sequence = (sequence - 1) & 0x0fffffff
        expected = (session.last_in_sequence + 1) & 0x0fffffff
        distance = (sequence - expected) & 0x0fffffff
        if distance >= 0x08000000:
            continue  # Already delivered.
        if distance > 4096:
            raise ValueError('reliable receive window exceeded')
        window.setdefault(sequence, incoming)
        while expected in window:
            ready = window.pop(expected)
            session.last_in_sequence = expected
            expected = (expected + 1) & 0x0fffffff
            yield ready


def send_reliable(sock, session, body):
    if session.superseded:
        return
    sequence = session.next_sequence
    session.next_sequence = (sequence + 1) & 0x0fffffff
    clear = channel_packet(body, sequence)
    encoded = encrypt_packet(session.key, clear)
    session.pending[str(sequence)] = {'packet': encoded.hex(), 'sent': time.time(), 'tries': 1}
    sock.sendto(encoded, session.base_peer)
    LAST_SENT[session.login_id] = time.monotonic()
    if body:
        LOG.info('BaseApp sent reliable sequence=%d message_ids=%02x bytes=%d', sequence, body[0], len(body))
    SESSIONS.save()


def resend_pending(sock):
    now = time.time()
    for session in list(SESSIONS.items.values()):
        if not session.base_peer or session.superseded:
            continue
        for pending in session.pending.values():
            if now - pending['sent'] >= 1 and pending['tries'] < 15:
                sock.sendto(bytes.fromhex(pending['packet']), session.base_peer)
                pending.update(sent=now, tries=pending['tries'] + 1)


def maintain_channels(sock):
    now = time.monotonic()
    for session in list(SESSIONS.items.values()):
        if session.superseded:
            continue
        if session.base_peer and now - LAST_RECEIVED.get(session.login_id, -1000) < 30:
            from battle_service import tick
            try:
                for response in tick(session):
                    send_reliable(sock, session, response)
                from death_service import tick as death_tick
                for response in death_tick(session):
                    send_reliable(sock, session, response)
                from tutorial_service import tick as tutorial_tick
                for response in tutorial_tick(session):
                    send_reliable(sock, session, response)
                from quest_event_service import tick as quest_tick
                for response in quest_tick(session):
                    send_reliable(sock, session, response)
                from quest_offline import tick as offline_quest_tick
                for response in offline_quest_tick(session):
                    send_reliable(sock, session, response)
            except Exception:
                LOG.exception('Battle timer failed; listeners remain active')
                session.battle_due = time.time() + 5
        if (session.base_peer and now - LAST_RECEIVED.get(session.login_id, -1000) < 30
                and now - LAST_SENT.get(session.login_id, -1000) >= 5):
            # ACK-only replies do not advance the client's reliable channel.
            # Keep the channel alive even when the application has no updates.
            send_reliable(sock, session, b'')
    for key, group in list(FRAGMENTS.items()):
        if now - group['updated'] > 60:
            del FRAGMENTS[key]


def complete_body(session, incoming):
    fragment = incoming.get('fragment')
    if fragment is None:
        return incoming['body']
    begin, finish = fragment
    key = (session.login_id, begin, finish)
    group = FRAGMENTS.setdefault(key, {'parts': {}, 'updated': time.monotonic()})
    group['updated'] = time.monotonic()
    group['parts'][incoming['sequence']] = incoming['body']
    if len(group['parts']) != finish - begin + 1:
        return None
    body = b''.join(group['parts'][n] for n in range(begin, finish + 1))
    del FRAGMENTS[key]
    LOG.info('BaseApp reassembled fragments=%d application_bytes=%d', finish - begin + 1, len(body))
    return body


def local_control(sock):
    path = ROOT / 'server' / 'state' / 'control.json'
    if not path.exists():
        return
    command = json.loads(path.read_text('utf-8'))
    path.unlink()
    active = sorted((s for s in SESSIONS.items.values()
                     if s.base_peer and s.world_started and not s.superseded),
                    key=lambda s: s.created, reverse=True)
    if not active:
        LOG.warning('Local control: no active world session')
        return
    session = active[0]
    if command['action'] == 'remap':
        # Remove the original mapping entry (zero ID used by the first probe)
        # and register a nonzero entry after the player's cell exists.
        remove = struct.pack('<H', 65535)
        from csxy_protocol import variable_message
        space_id = session.engine_space_id or 1
        send_reliable(sock, session, variable_message(7, struct.pack('<I', space_id) + bytes(8) + remove))
        send_reliable(sock, session, mapping_message(path=command.get('map'), space_id=space_id))
        LOG.info('Local control: mapping resent after player creation')
    elif command['action'] == 'messages':
        for body in command['bodies']:
            send_reliable(sock, session, bytes.fromhex(body))
    elif command['action'] == 'npcs':
        from npc_service import create_npc, find_npc, update_interest
        session.world_ready = True
        for entity_id in command.get('already_visible', []):
            npc = find_npc(entity_id)
            if npc:
                if entity_id not in session.visible_npcs:
                    session.visible_npcs.append(entity_id)
                send_reliable(sock, session, create_npc(npc))
        for body in update_interest(session):
            send_reliable(sock, session, body)
    elif command['action'] == 'restart_world':
        for body in reload_scene(session):
            send_reliable(sock, session, body)
    elif command['action'] == 'transfer_world':
        from world_service import transfer_scene
        from npc_service import map_config
        from character_store import update
        if session.world_ready and not session.battle_id:
            if 'space_no' in command or 'position' in command:
                # Local diagnostic transport; never advances quests or rewards.
                target = int(command.get('space_no', session.space_no))
                map_config(space_no=target)
                position = command.get('position', session.position)
                if len(position) != 3 or not all(math.isfinite(float(v)) for v in position):
                    raise ValueError('transport position needs three finite coordinates')
                update(session, space_no=target, position=[float(v) for v in position])
            for body in transfer_scene(session):
                send_reliable(sock, session, body)
        else:
            LOG.warning('Local control: native transfer skipped while loading or fighting')
    elif command['action'] == 'initialize_time':
        from world_service import time_message
        send_reliable(sock, session, time_message(session))
        LOG.info('Local control: server time and client quest initialization sent')
    elif command['action'] == 'sync_status':
        from attribute_service import message
        from growth_service import repair, potential
        from entity_methods import avatar_client_message
        if session.world_ready and not getattr(session, 'battle_id', ''):
            character = repair(session)
        else:
            character = None
        if character is not None:
            send_reliable(sock, session, message(session, character))
            send_reliable(sock, session, avatar_client_message(session, 'update_potential', potential(character)))
            LOG.info('Local control: level growth, health/mana and potential synchronized')
    elif command['action'] == 'repair_intro_bag':
        from inventory_service import repair_messages
        responses = repair_messages(session)
        for body in responses:
            send_reliable(sock, session, body)
        LOG.info('Local control: introductory bag repair messages=%d', len(responses))
    elif command['action'] == 'sync_inventory':
        from character_store import characters
        from inventory_service import resync_messages, repair_summon_bag
        if session.world_ready and not getattr(session, 'battle_id', ''):
            try:
                character, _ = repair_summon_bag(session)
            except ValueError as error:
                LOG.warning('Summon supply repair postponed: %s', error)
                character = next((c for c in characters(session)
                                  if c['dbid'] == session.selected_character), None)
            if character is not None:
                responses = resync_messages(session, character)
                for body in responses:
                    send_reliable(sock, session, body)
                LOG.info('Local control: saved inventory, equipment and summons synchronized; messages=%d', len(responses))
        else:
            LOG.warning('Local control: inventory synchronization skipped while loading or fighting')
    elif command['action'] == 'sync_skills':
        from skill_service import repair, messages as skill_messages
        from attribute_service import message
        if session.world_ready and not getattr(session, 'battle_id', ''):
            character = repair(session)
            if character is not None:
                for body in [message(session, character)] + skill_messages(session, character):
                    send_reliable(sock, session, body)
                LOG.info('Local control: role skills synchronized')


_HOT_STATUS_DUE = 0


def hot_control_tick(sock):
    """Diagnostic commands only; incomplete external inventory editor is omitted."""
    import hot_control
    global _HOT_STATUS_DUE
    active = sorted((s for s in SESSIONS.items.values()
                     if s.base_peer and s.world_ready and not s.superseded),
                    key=lambda s: s.created, reverse=True)
    session = active[0] if active else None
    def handle(text, current):
        verb, _, argument = text.partition(' ')
        if verb == 'status':
            return
        if verb == 'say' and current:
            from chat_service import announce
            for body in announce(current, 'show_msg_world', argument):
                send_reliable(sock, current, body)
            return
        raise ValueError('支持的热控命令：status、say 文字')
    hot_control.poll_commands(handle, session)
    now = time.monotonic()
    if now >= _HOT_STATUS_DUE:
        _HOT_STATUS_DUE = now + 2
        from chat_service import status as chat_status
        from death_service import status as death_status
        from monster_stats import status as monster_status
        hot_control.write_status(active_sessions=len(active), chat=chat_status(),
                                 death=death_status(), monsters=monster_status())


def main(done=None, on_ready=None):
    done = done or threading.Event()
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    seq = 0
    journal = OUT / ('udp_baseapp_%s.manifest.jsonl' % ts)
    resumed = set()
    bind = os.environ.get('BASEAPP_BIND', '0.0.0.0')
    port = int(os.environ.get('BASEAPP_PORT', '28000'))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((bind, port))
        sock.settimeout(0.5)
        LOG.info('BaseApp UDP listening on %s:%d', bind, sock.getsockname()[1])
        if on_ready:
            on_ready(sock.getsockname()[1])
        while not done.is_set():
            resend_pending(sock)
            maintain_channels(sock)
            try:
                local_control(sock)
                hot_control_tick(sock)
            except Exception:
                LOG.exception('Local control failed; listeners remain active')
            try:
                data, peer = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue
            seq += 1
            stem = 'udp_baseapp_%s_%03d' % (ts, seq)
            rec = {'file': stem + '.bin', 'size': len(data), 'peer': '%s:%d' % peer,
                   'time': datetime.datetime.now().isoformat(timespec='milliseconds')}
            login = None
            parts = {'body': b''}
            positional = False
            try:
                write_packet(OUT / rec['file'], data)
                session, clear, parts, login = decode_session(data, peer)
                LAST_RECEIVED[session.login_id] = time.monotonic()
                write_packet(OUT / (stem + '.clear.bin'), clear)
                rec.update(flags=parts['flags'], sequence=parts['sequence'],
                           acks=parts['acks'], body_size=len(parts['body']))
                reply = response_for(session, parts, login)
                if login:
                    session.base_peer = peer
                    rec['login_attempt'] = login[1]
                    SESSIONS.save()
                if reply:
                    encoded = encrypt_packet(session.key, reply)
                    sock.sendto(encoded, peer)
                    write_packet(OUT / (stem + '.reply.bin'), encoded)
                    write_packet(OUT / (stem + '.reply.clear.bin'), reply)
                    rec['reply_size'] = len(encoded)
                for ack in parts['acks']:
                    if session.pending.pop(str(ack), None):
                        LOG.info('BaseApp client acknowledged server sequence=%d', ack)
                        SESSIONS.save()
                if not login and parts['flags'] & 0x0008:
                    for incoming in ordered_application_parts(session, parts):
                        if not session.account_created:
                            session.account_created = True
                            send_reliable(sock, session, create_account(session))
                        elif session.account_logged_in and session.login_id not in resumed:
                            if session.selected_character and not session.world_started:
                                for message in initial_messages(session):
                                    send_reliable(sock, session, message)
                            elif not session.world_started:
                                send_reliable(sock, session, character_list(session))
                            elif session.world_ready and not session.battle_id:
                                from battle_service import selected
                                from quest_service import resync_item_objectives
                                character = selected(session)
                                if character is not None:
                                    for message in resync_item_objectives(session, character):
                                        send_reliable(sock, session, message)
                        resumed.add(session.login_id)
                        body = complete_body(session, incoming)
                        if body is not None:
                            for response in process_messages(session, body):
                                send_reliable(sock, session, response)
                positional = parts['body'][5:6] in (b'\x02', b'\x03')
                if login or (len(parts['body']) > 5 and not positional) or seq % 100 == 0:
                    LOG.info('BaseApp %s:%d received=%d flags=%04x sequence=%s body_bytes=%d%s', *peer,
                             len(data), parts['flags'], parts['sequence'], len(parts['body']),
                             ' LOGIN_REPLIED' if login else '')
            except Exception as exc:
                rec['error'] = type(exc).__name__ + ': ' + str(exc)
                LOG.exception('BaseApp datagram rejected from %s:%d (%d bytes)', *peer, len(data))
            append_record(journal, rec)
            try:
                if seq % 100 == 0 or login or (len(parts['body']) > 5 and not positional):
                    SESSIONS.save()
            except (OSError, MemoryError):
                LOG.exception('Cannot save BaseApp session metadata')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    main()
