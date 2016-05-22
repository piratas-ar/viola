# coding=utf-8
""" viola.py : Handles and sends viola packets """

import crypto
import json # for saving state to disk
import os

import util
import otrlib
import json
import base64
import room
import transport
import introduction
import accounts

import nacl.exceptions # XXX dirty

VIOLA_TAG = b'?VLA,'

INTRODUCTION_OPCODE = "00"
ROOM_JOIN_OPCODE = "01"
KEY_TRANSPORT_OPCODE = "02"
ROOM_MESSAGE_OPCODE = "03"

IRC_MAXIMUM_MESSAGE_SIZE = 399 # XXX figure out the right size here.

SIG_LEN = 64 # bytes per ed25519 sig
PUBKEY_LEN = 32 # bytes per ed25519/curve25519 key
SYMMETRIC_KEY_LEN = 32 # bytes per symmetric Box() key
MESSAGE_KEY_ARRAY_CELL_LEN = 72 # bytes: 32 bytes symmetric key + 40 bytes of nacl overhead

#XXX need to verify ROOM_MESSAGE packets with long-term secret!!!
#XXX what happens if a user does join-room multiple times without leaving the channel
#XXX kill weechat logging wtf

def handle_room_message_packet(packet_payload, parsed, server):
    sender_host = parsed['from']
    sender_nick = parsed['from_nick']
    channel = parsed['to_channel']
    account = accounts.get_my_account()

    if not channel: # XXX functionify
        util.debug("Received ROOM_MESSAGE not in channel from %s. Ignoring." % sender_host)
        return ""

    # Is this channel a viola room for us?
    # XXX Code duplication
    try:
        viola_room = account.get_viola_room(channel, server)
    except accounts.NoSuchRoom:
        util.debug("Received ROOM_MESSAGE in a regular channel (%s). Ignoring." % channel)
        buf = util.get_current_buffer() # XXX weechat API shouldn't polute viola.py
        util.viola_channel_msg(buf,
                               "[You hear a viola screeching... Please do '/viola join-room' to join the session.]",
                               "lightcyan")
        return ""

    try:
        room_message_key = viola_room.get_room_message_key()
    except room.NoMessageKey:
        util.debug("Received ROOM_MESSAGE in %s but no message key. Ignoring." % channel) # XXX ???
        util.viola_channel_msg(viola_room.buf,
                               "[You hear a viola screeching... Please do '/viola join-room' to join the session.]",
                               "grey")
        return ""

    payload = base64.b64decode(packet_payload)

    if len(payload) <= 64:
        raise IncompletePacket

    util.debug("Attempting to decrypt ROOM_MESSAGE in %s" % channel)

    # Get packet fields
    signature = payload[:SIG_LEN]
    message_ciphertext = payload[SIG_LEN:]

    # XXX catch decrypt exception
    try:
        plaintext = crypto.decrypt_room_message(room_message_key, message_ciphertext)
    except nacl.exceptions.CryptoError:
        util.viola_channel_msg(viola_room.buf,
                               "Could not decrypt message sent in room. Maybe old key. Try rejoining the channel.",
                               "red") # XXX this won't work
        return ""

    msg_in = otrlib.build_privmsg_in(sender_host, channel, plaintext)
    return msg_in[0] + '⚷' + msg_in[1:]


MINIMUM_KEY_TRANSPORT_PAYLOAD_LEN = SIG_LEN + PUBKEY_LEN*2 + MESSAGE_KEY_ARRAY_CELL_LEN

def handle_key_transport_packet(packet_payload, parsed, server):
    sender = parsed['from_nick']
    channel = parsed['to_channel']
    account = accounts.get_my_account()

    if not channel: # XXX functionify
        util.debug("Received KEY_TRANSPORT not in channel from %s. Ignoring." % sender)
        return ""

    # Is this channel a viola room for us?
    # XXX Code duplication
    try:
        viola_room = account.get_viola_room(channel, server)
    except accounts.NoSuchRoom:
        util.debug("Received KEY_TRANSPORT in a regular channel (%s). Ignoring." % channel)
        return ""

    payload = base64.b64decode(packet_payload)

    if len(payload) < MINIMUM_KEY_TRANSPORT_PAYLOAD_LEN:
        raise IncompletePacket

    if viola_room.i_am_captain:
        util.debug("Received KEY_TRANSPORT in %s by %s but I am captain! Ignoring." % (channel, sender))
        return ""

    util.debug("Received KEY_TRANSPORT in %s! Handling it." % channel)

    # Start parsing the packet
    signature = payload[:SIG_LEN]

    captain_identity_pubkey = payload[SIG_LEN:SIG_LEN+PUBKEY_LEN]
    captain_identity_pubkey = crypto.parse_signing_pubkey(captain_identity_pubkey)

    captain_transport_pubkey = payload[SIG_LEN+PUBKEY_LEN : SIG_LEN+PUBKEY_LEN+PUBKEY_LEN]
    captain_transport_pubkey = crypto.parse_pub_key(captain_transport_pubkey)

    encrypted_message_key_array = payload[SIG_LEN+PUBKEY_LEN+PUBKEY_LEN:]

    # Check if we trust the captain.
    try:
        captain_friend_name = account.get_friend_from_identity_key(captain_identity_pubkey)
    except accounts.IdentityKeyNotTrusted:
        hexed_captain_key = crypto.get_hexed_key(captain_identity_pubkey)
        buf = viola_room.buf
        util.viola_channel_msg(buf, "Untrusted nickname %s is the captain of this channel with key: %s" % (sender, hexed_captain_key),
                               color="red")
        util.viola_channel_msg(buf, "Ignoring KEY_TRANSPORT by untrusted captain. If you trust that key and "
                               "you want to join the channel, please issue the following command and rejoin:\n"
                               "\t /viola trust-key <name> %s\n"
                               "where <name> is the nickname you want to assign to the key."  % hexed_captain_key,
                               color="red")
        util.viola_channel_msg(buf, "Example: /viola trust-key alice %s" % hexed_captain_key,
                               color="red")
        return ""

    # Verify captain signature
    captain_identity_pubkey.verify(payload)    # XXX catch exception

    # Try to decrypt the message key array
    try:
        room_message_key = crypto.decrypt_room_message_key(encrypted_message_key_array,
                                                           captain_transport_pubkey,
                                                           viola_room.get_room_participant_privkey())
    except crypto.NoKeyFound:
        util.debug("Received KEY_TRANSPORT but did not find my key. Fuck.")
        return ""

    # We got the room message key!
    viola_room.set_room_message_key(room_message_key)
    viola_room.status = "done"

    # We found our captain. Add them to the room!
    # XXX should we do this here or before the checks?
    viola_room.add_member(sender, captain_identity_pubkey, captain_transport_pubkey)

    # Print some messages to the user
    buf = util.viola_channel_msg(viola_room.buf, "Joined room %s with captain %s!" % (channel, sender))
    util.debug("Got a new room message key from captain %s: %s" % \
               (sender, crypto.get_hexed_key(room_message_key)))

    return ""

ROOM_JOIN_PAYLOAD_LEN = SIG_LEN + PUBKEY_LEN*2

def handle_room_join_packet(packet_payload, parsed, server):
    sender_nick = parsed['from_nick']
    channel = parsed['to_channel']
    account = accounts.get_my_account()

    if not parsed['to_channel']: # XXX functionify
        util.debug("Received ROOM_JOIN not in channel from %s. Ignoring." % sender_nick)
        return ""

    # Is this channel a viola room for us?
    try:
        viola_room = account.get_viola_room(channel, server)
    except accounts.NoSuchRoom:
        util.debug("Received ROOM_JOIN in a regular channel (%s). Ignoring." % channel)
        return ""

    payload = base64.b64decode(packet_payload)

    if len(payload) < ROOM_JOIN_PAYLOAD_LEN:
        raise IncompletePacket

    signature = payload[:SIG_LEN]
    identity_pubkey = crypto.parse_signing_pubkey(payload[SIG_LEN:SIG_LEN+PUBKEY_LEN])
    room_pubkey = crypto.parse_pub_key(payload[SIG_LEN+PUBKEY_LEN:SIG_LEN+PUBKEY_LEN+PUBKEY_LEN])

    # Verify signature
    # XXX is this the right logic for this particular packet?
    # XXX catch exception
    identity_pubkey.verify(payload)

    # XXX should we add all members even if we don't know them?
    viola_room.add_member(sender_nick, identity_pubkey, room_pubkey)

    # No need to do anything more if we are not captain in this channel.
    if not viola_room.i_am_captain:
        util.debug("Received ROOM_JOIN in %s but not captain. Ignoring." % channel)
        return ""

    util.debug("Received ROOM_JOIN in %s. Sending KEY_TRANSPORT (%d members)!" % (channel, len(viola_room.members)))

    # We are the captain. Check if we trust this key. Reject member otherwise.
    try:
        joining_friend_name = account.get_friend_from_identity_key(identity_pubkey)
    except accounts.IdentityKeyNotTrusted:
        buf = viola_room.buf
        util.viola_channel_msg(buf, "%s nickname %s is trying to join the channel with key %s." %
                               (otrlib.colorize("Untrusted", "red"), sender_nick,
                                crypto.get_hexed_key(identity_pubkey)), "red")
        util.viola_channel_msg(buf, "If you trust that key and you want them to join the channel, "
                               "please issue the following command and ask them to rejoin the channel:\n"
                               "\t /viola trust-key <name> %s\n"
                               "where <name> is the nickname you want to assign to the key."  %
                               crypto.get_hexed_key(identity_pubkey), "red")

        util.viola_channel_msg(buf, "Example: /viola trust-key alice %s" % crypto.get_hexed_key(identity_pubkey), "red")
        return ""

    # XXX Security: Maybe we should ask the captain before autoadding them!
    buf = viola_room.buf
    util.viola_channel_msg(buf, "Friend '%s' was added to the viola room!" % joining_friend_name)

    # We are captains in the channel. Act like it!
    # There is a new room member! Refresh and send new key!
    send_key_transport_packet(viola_room)
    return ""


def handle_viola_packet(packet, parsed, server):
    """
    Handle a generic viola packet. Parse its opcode and call the correct
    function for this specific type of packet.
    """

    util.debug("Parsing viola packet.")

    # XXX terrible functionify
    opcode = packet[:2]
    packet_payload = packet[2:]

    if opcode == "00":
        msg = introduction.handle_introduction_packet(packet_payload, parsed)
    elif opcode == "01":
        msg = handle_room_join_packet(packet_payload, parsed, server)
    elif opcode == "02":
        msg = handle_key_transport_packet(packet_payload, parsed, server)
    elif opcode == "03":
        msg = handle_room_message_packet(packet_payload, parsed, server)
    else:
        util.debug("Received viola packet with opcode: %s" % opcode)
        raise NotImplementedError("wtf")

    return msg

def forward_received_unencrypted_msg_to_user(parsed, server):
    """We received a regular IRC message that has nothing to do with Viola.
    Mark as unencrypted and forward to user"""
    sender = parsed['from']
    channel = parsed['to_channel']

    if channel:
        target = channel
    else:
        target = parsed['to_nick']

    return otrlib.build_privmsg_in(sender, channel, parsed['text'])

def received_irc_msg_cb(irc_msg, server):
    """Received IRC message 'msg'. Decode and return the message."""

    parsed = otrlib.parse_irc_privmsg(irc_msg, server)

    # Check whether the received message is a viola message
    msg = parsed['text']
    try:
        msg.index(VIOLA_TAG)
    except ValueError: # Not a viola message. Treat as plaintext and forward.
        return forward_received_unencrypted_msg_to_user(parsed, server)

    complete_packet = transport.accumulate_viola_fragment(msg, parsed, server)
    if not complete_packet: # Need to collect more fragments!
        return ""

    # We reconstructed a fragmented viola message! Handle it!
    return handle_viola_packet(complete_packet, parsed, server)

def handle_outgoing_irc_msg_to_channel(parsed, server):
    """We are about to send 'parsed' to a channel. If the channel is a viola room
    where the room message key is known, encrypt the message and send it
    directly. Otherwise if no Viola session is going on, return the plaintext
    string that should be output to the channel."""
    channel = parsed['to_channel']
    msg = parsed['text']
    account = accounts.get_my_account()

    try:
        viola_room = account.get_viola_room(channel, server)
    except accounts.NoSuchRoom:
        util.debug("No viola room at %s. Sending plaintext." % channel)
        return msg

    try:
        room_message_key = viola_room.get_room_message_key()
    except room.NoMessageKey:
        util.debug("No message key at %s. Sending plaintext." % channel) # XXX ???
        return msg

    if not viola_room.status == "done":
        util.debug("Room %s not setup yet. Sending plaintext." % channel) # XXX ???
        return msg

    util.debug("Sending encrypted msg to %s" % channel)

    # OK we are in a viola room and we even know the key!
    # Send a ROOM_MESSAGE!
    # XXX functionify
    ciphertext = crypto.get_room_message_ciphertext(room_message_key, msg)
    packet_signed = account.sign_msg_with_identity_key(ciphertext)

    payload_b64 = base64.b64encode(packet_signed)

    msg = ROOM_MESSAGE_OPCODE + payload_b64
    transport.send_viola_privmsg(server, channel, msg)

    return ""

def handle_outgoing_irc_msg_to_user(parsed):
    """We are about to send 'parsed' to a user. Just send plaintext."""
    return parsed['text']

def send_irc_msg_cb(msg, server):
    """Sending IRC message 'msg'. Return the bytes we should send to the network."""
    parsed = otrlib.parse_irc_privmsg(msg, server)

    if parsed['to_channel']:
        target = parsed['to_channel']
        msg = handle_outgoing_irc_msg_to_channel(parsed, server)
    else:
        target = parsed['to_nick']
        msg = handle_outgoing_irc_msg_to_user(parsed)

    if msg:
        msg_out = otrlib.build_privmsg_out(target, msg)
        return msg_out
    else:
        return ""

def send_key_transport_packet(viola_room):
    util.debug("I'm captain in %s: Membership changed. Refreshing message key." % viola_room.name)

    account = accounts.get_my_account()
    channel = viola_room.name
    server = viola_room.server

    assert(viola_room.i_am_captain) # Only captains should be here!

    # Prepare necessary packet fields.
    captain_identity_key = account.get_identity_pubkey()
    captain_transport_key = viola_room.get_room_participant_pubkey() # XXX maybe special func for captain's key?
    message_key_array = viola_room.get_message_key_array() # XXX also generates key. rename.
    # our array must be a multiple of 72 bytes
    assert(len(message_key_array) % MESSAGE_KEY_ARRAY_CELL_LEN == 0)

    # Sign all previous fields.
    packet_fields = captain_identity_key + captain_transport_key + message_key_array
    packet_signed = account.sign_msg_with_identity_key(packet_fields)

    payload_b64 = base64.b64encode(packet_signed)

    viola_room.status = "bootstrapping"
    util.debug("Sending KEY_TRANSPORT in %s!" % channel)

    msg = KEY_TRANSPORT_OPCODE + payload_b64
    transport.send_viola_privmsg(server, channel, msg)

    viola_room.status = "done"

def send_room_join(channel, server, buf):
    """Send ROOM_JOIN message."""
    account = accounts.get_my_account()

    # Don't send ROOM_JOIN to empty channel. No one to handle it.
    if util.irc_channel_is_empty(channel, server):
        util.viola_channel_msg(buf, "Can't 'join-room' in an empty channel!", "red")
        util.viola_channel_msg(buf, "Do '/viola start-room' if you want to start a new viola room instead.", "red")
        return

    # First of all, register this new room.
    viola_room = account.register_viola_room(channel, server, buf)

    # Get the keys to be placed in the packet
    my_pub_key = account.get_identity_pubkey()
    my_room_key = viola_room.get_room_participant_pubkey()

    # Sign them
    packet_fields = my_pub_key + my_room_key
    packet_signed = account.sign_msg_with_identity_key(packet_fields)

    payload_b64 = base64.b64encode(packet_signed)

    msg = ROOM_JOIN_OPCODE + payload_b64
    transport.send_viola_privmsg(server, channel, msg)

    util.viola_channel_msg(buf, "Requested to join room %s..." % channel)

def start_viola_room(channel, server, buf):
    """Start a Viola room in 'channel'@'server' on weechat buffer 'buf'."""
    account = accounts.get_my_account()
    util.debug("Starting a viola session in %s." % channel)

    # Make sure we are the only nick in the channel otherwise someone else
    # might already be captaining.
    if not util.irc_channel_is_empty(channel, server):
        util.viola_channel_msg(buf, "Can only start viola session in empty channel!", "red")
        util.viola_channel_msg(buf, "Try '/viola join-room' in this channel instead.", "red")
        return

    account.register_viola_room(channel, server, buf, i_am_captain=True)

    util.viola_channel_msg(buf, "We are now the captain in room %s!" % channel)

def user_left_channel(irc_msg, server):
    """A user left a channel we are in. Remove them from the channel if we are captain."""
    account = accounts.get_my_account()

    parsed = util.parse_irc_quit_kick_part(irc_msg, server)

    nick = parsed['from_nick']
    channel = parsed['channel']
    command = parsed['command']

    assert(command.upper() == "PART")

    util.debug("Received %s from %s in channel %s." % (command, nick, channel))

    try:
        viola_room = account.get_viola_room(channel, server)
    except accounts.NoSuchRoom:
        util.debug("No viola room at %s. Sending plaintext." % channel)
        return

    try:
        viola_room.remove_member_and_rekey(nick)
    except room.NoSuchMember:
        util.control_msg("A non-existent nick left the room. WTF.") # XXX i think this also catches ourselves
        return

def user_got_kicked(irc_msg, server):
    """A user got kicked from a channel we are in. Remove them from the member list."""
    account = accounts.get_my_account()

    parsed = util.parse_irc_quit_kick_part(irc_msg, server)

    nick = parsed['from_nick']
    channel = parsed['channel']
    command = parsed['command']
    target = parsed['target']

    assert(command.upper() == "KICK")

    util.debug("%s got kicked from %s by %s." % (target, channel, nick))

    try:
        viola_room = account.get_viola_room(channel, server)
    except accounts.NoSuchRoom:
        util.debug("No viola room at %s. Sending plaintext." % channel)
        return

    try:
        viola_room.remove_member_and_rekey(target)
    except room.NoSuchMember:
        util.control_msg("A non-existent nick left the room. WTF.") # XXX i think this also catches ourselves

def user_quit_irc(irc_msg, server):
    """A user quit IRC. Remove them form the member list of any channel they are in."""
    account = accounts.get_my_account()

    parsed = util.parse_irc_quit_kick_part(irc_msg, server)

    nick = parsed['from_nick']
    command = parsed['command']

    assert(command.upper() == "QUIT")

    account.user_quit_irc(nick)

def user_changed_irc_nick(old_nick, new_nick):
    """User changed nickname. Track the change."""
    account = accounts.get_my_account()
    # A user changed nick: we need to update the viola rooms.
    account.user_changed_nick(old_nick, new_nick)

class ViolaCommandError(Exception): pass
class IncompletePacket(Exception): pass
