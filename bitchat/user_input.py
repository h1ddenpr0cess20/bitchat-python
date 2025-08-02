"""User input handler for BitchatClient"""
from __future__ import annotations
from typing import TYPE_CHECKING
import asyncio
import hashlib
from .protocol import MessageType, create_bitchat_packet, debug_println
from .terminal_ux import ChatMode, Channel, PrivateDM, print_help, clear_screen
from . import commands

if TYPE_CHECKING:
    from .client import BitchatClient

async def handle_user_input(client: "BitchatClient", line: str):
    """Handle user input commands and messages"""
    # Number switching
    if len(line) == 1 and line.isdigit():
        num = int(line)
        if client.chat_context.switch_to_number(num):
            debug_println(client.chat_context.get_status_line())
        else:
            print("» Invalid conversation number")
        return
    
    # Commands
    if line == "/help":
        print_help()
        return
    
    if line == "/exit":
        # Send leave notification if connected
        if client.client and client.client.is_connected:
            leave_packet = create_bitchat_packet(
                client.my_peer_id, MessageType.LEAVE, client.nickname.encode()
            )
            await client.send_packet(leave_packet)
            await asyncio.sleep(0.1)  # Give time for the packet to send

        try:
            await client.save_app_state()
        except Exception as e:
            debug_println(f"[ERROR] Failed to save app state: {e}")
        client.running = False
        return
    
    if line.startswith("/name "):
        new_name = line[6:].strip()
        if not new_name:
            print("\033[93m⚠ Usage: /name <new_nickname>\033[0m")
            print("\033[90mExample: /name Alice\033[0m")
        elif len(new_name) > 20:
            print("\033[93m⚠ Nickname too long\033[0m")
            print("\033[90mMaximum 20 characters allowed.\033[0m")
        elif not all(c.isalnum() or c in '-_' for c in new_name):
            print("\033[93m⚠ Invalid nickname\033[0m")
            print("\033[90mNicknames can only contain letters, numbers, hyphens and underscores.\033[0m")
        elif new_name in ["system", "all"]:
            print("\033[93m⚠ Reserved nickname\033[0m")
            print("\033[90mThis nickname is reserved and cannot be used.\033[0m")
        else:
            client.nickname = new_name
            announce_packet = create_bitchat_packet(
                client.my_peer_id, MessageType.ANNOUNCE, client.nickname.encode()
            )
            await client.send_packet(announce_packet)
            print(f"\033[90m» Nickname changed to: {client.nickname}\033[0m")
            await client.save_app_state()
        return
    
    if line == "/list":
        client.chat_context.show_conversation_list()
        return
    
    if line == "/switch":
        print(f"\n{client.chat_context.get_conversation_list_with_numbers()}")
        switch_input = await aioconsole.ainput("Enter number to switch to: ")
        if switch_input.strip().isdigit():
            num = int(switch_input.strip())
            if client.chat_context.switch_to_number(num):
                debug_println(client.chat_context.get_status_line())
            else:
                print("» Invalid selection")
        return
    
    if line.startswith("/j "):
        await client.handle_join_channel(line)
        return
    
    if line == "/public":
        client.chat_context.switch_to_public()
        debug_println(client.chat_context.get_status_line())
        return
    
    if line in ["/online", "/w"]:
        if not client.client or not client.client.is_connected:
            print("» You're not connected to any peers yet.")
            print("\033[90mWaiting for other BitChat devices...\033[0m")
        else:
            online_list = [p.nickname for p in client.peers.values() if p.nickname]
            if online_list:
                print(f"» Online users: {', '.join(sorted(online_list))}")
            else:
                print("» No one else is online right now.")
        print("> ", end='', flush=True)
        return
    
    if line == "/channels":
        all_channels = set(client.chat_context.active_channels) | set(client.channel_keys.keys())
        if all_channels:
            print("» Discovered channels:")
            for channel in sorted(all_channels):
                status = ""
                if channel in client.chat_context.active_channels:
                    status += " ✓"
                if channel in client.password_protected_channels:
                    status += " 🔒"
                    if channel in client.channel_keys:
                        status += " 🔑"
                print(f"  {channel}{status}")
            print("\n✓ = joined, 🔒 = password protected, 🔑 = authenticated")
        else:
            print("» No channels discovered yet. Channels appear as people use them.")
        print("> ", end='', flush=True)
        return
    
    if line == "/status":
        peer_count = len(client.peers)
        channel_count = len(client.chat_context.active_channels)
        dm_count = len(client.chat_context.active_dms)
        connection_status = "Connected" if (client.client and client.client.is_connected) else "Offline"
        session_count = client.encryption_service.get_session_count()
        pending_handshakes = len(client.encryption_service.handshake_states)
        pending_messages = sum(len(msgs) for msgs in client.pending_private_messages.values())
        
        print("\n╭─── Connection Status ──────╮")
        print(f"│ Status: {connection_status:^18} │")
        print(f"│ Peers connected: {peer_count:6}     │")
        print(f"│ Active channels: {channel_count:6}     │")
        print(f"│ Active DMs:      {dm_count:6}     │")
        print("│                           │")
        print(f"│ Secure sessions: {session_count:6}     │")
        print(f"│ Pending handshakes: {pending_handshakes:3}     │")
        print(f"│ Queued messages: {pending_messages:6}     │")
        print("│                           │")
        print(f"│ Your nickname: {client.nickname[:11]:^11}  │")
        print(f"│ Your ID: {client.my_peer_id[:8]}...    │")
        print("╰───────────────────────────╯")
        
        # Show encryption session details if any
        if session_count > 0:
            print("\n🔒 Secure Sessions:")
            for peer_id in client.encryption_service.get_active_peers():
                nickname = client.peers.get(peer_id, Peer()).nickname or peer_id[:8] + "..."
                fingerprint = client.encryption_service.get_peer_fingerprint(peer_id)
                print(f"  • {nickname} ({fingerprint[:8] if fingerprint else 'Unknown'}...)")
        
        # Show pending handshakes if any
        if pending_handshakes > 0:
            print("\n🤝 Pending Handshakes:")
            for peer_id in client.encryption_service.handshake_states.keys():
                nickname = client.peers.get(peer_id, Peer()).nickname or peer_id[:8] + "..."
                print(f"  • {nickname}")
        
        # Show pending messages if any
        if pending_messages > 0:
            print("\n📝 Queued Messages:")
            for peer_id, messages in client.pending_private_messages.items():
                nickname = client.peers.get(peer_id, Peer()).nickname or peer_id[:8] + "..."
                print(f"  • {len(messages)} message(s) for {nickname}")
        
        print("> ", end='', flush=True)
        return
    
    if line == "/clear":
        clear_screen()
        print_banner()
        mode_name = {
            ChatMode.Public: "public chat",
            ChatMode.Channel: f"channel {client.chat_context.current_mode.name}",
            ChatMode.PrivateDM: f"DM with {client.chat_context.current_mode.nickname}"
        }.get(type(client.chat_context.current_mode), "unknown")
        print(f"» Cleared {mode_name}")
        print("> ", end='', flush=True)
        return
    
    if line.startswith("/dm "):
        await client.handle_dm_command(line)
        return
    
    if line == "/reply":
        if client.chat_context.last_private_sender:
            peer_id, nickname = client.chat_context.last_private_sender
            client.chat_context.enter_dm_mode(nickname, peer_id)
            debug_println(client.chat_context.get_status_line())
        else:
            print("» No private messages received yet.")
        return
    
    if line.startswith("/block"):
        await client.handle_block_command(line)
        return
    
    if line.startswith("/unblock "):
        await client.handle_unblock_command(line)
        return
    
    if line == "/leave":
        await client.handle_leave_command()
        return
    
    if line.startswith("/pass "):
        await client.handle_pass_command(line)
        return
    
    if line.startswith("/transfer "):
        await client.handle_transfer_command(line)
        return
    
    # Unknown command
    if line.startswith("/"):
        cmd = line.split()[0]
        print(f"\033[93m⚠ Unknown command: {cmd}\033[0m")
        print("\033[90mType /help to see available commands.\033[0m")
        return
    
    # Regular message - check mode
    if isinstance(client.chat_context.current_mode, PrivateDM):
        await client.send_private_message(
            line,
            client.chat_context.current_mode.peer_id,
            client.chat_context.current_mode.nickname
        )
    else:
        # Check if we're connected before sending
        if not client.client or not client.client.is_connected:
            print("\033[93m⚠ You're not connected to any peers yet.\033[0m")
            print("\033[90mYour message will be sent once someone joins the network.\033[0m")
            print("\033[90m(This Python client doesn't queue messages while offline)\033[0m")
        else:
            await client.send_public_message(line)


__all__ = ["handle_user_input"]
