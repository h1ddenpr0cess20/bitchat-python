import asyncio
import logging
import httpx
import textwrap
import json
import time
from bitchat.bot_api import BitchatBotAPI
from bitchat.models import Peer
from bitchat.terminal_ux import Channel, PrivateDM
from tools import *

class InfiniGPTBitchat:
    """BitChat adaptation of the InfiniGPT IRC bot."""
    def __init__(self):
        with open("config.json", "r") as f:
            self.config = json.load(f)
        with open("schema.json") as f:
            self.tools = json.load(f)

        cfg_llm = self.config["llm"]
        self.models = cfg_llm["models"]
        self.api_keys = cfg_llm["api_keys"]
        (self.default_model,
         self.default_personality,
         self.prompt,
         self.options,
         self.history_size,
         self.ollama_url) = (
            cfg_llm["default_model"],
            cfg_llm["personality"],
            cfg_llm["prompt"],
            cfg_llm["options"],
            cfg_llm["history_size"],
            cfg_llm["ollama_url"],
        )
        self.openai_key = self.api_keys.get("openai")
        self.xai_key = self.api_keys.get("xai")
        self.google_key = self.api_keys.get("google")
        self.mistral_key = self.api_keys.get("mistral")

        self.messages = {}
        self.bitchat = BitchatBotAPI(self.on_message)
        self.nickname = self.bitchat.nickname
        self.model = self.default_model
        self.system_prompt = self.prompt[0] + self.default_personality + self.prompt[1]

        logging.basicConfig(level=logging.INFO,
                            format='%(asctime)s - %(levelname)s - %(message)s')
        self.log = logging.getLogger(__name__).info

    def chop(self, message):
        lines = message.splitlines()
        newlines = []
        for line in lines:
            if len(line) > 420:
                wrapped_lines = textwrap.wrap(
                    line,
                    width=420,
                    drop_whitespace=False,
                    replace_whitespace=False,
                    fix_sentence_endings=True,
                    break_long_words=False)
                newlines.extend(wrapped_lines)
            else:
                newlines.append(line)
        return newlines

    async def respond(self, channel, sender, messages, sender2=False, tools=None):
        if self.model in self.models.get("openai", []):
            bearer = self.openai_key
            self.url = "https://api.openai.com/v1"
        elif self.model in self.models.get("xai", []):
            bearer = self.xai_key
            self.url = "https://api.x.ai/v1"
        elif self.model in self.models.get("google", []):
            bearer = self.google_key
            self.url = "https://generativelanguage.googleapis.com/v1beta/openai"
        elif self.model in self.models.get("ollama", []):
            bearer = "hello_friend"
            self.url = f"http://{self.ollama_url}/v1"
        elif self.model in self.models.get("mistral", []):
            bearer = self.mistral_key
            self.url = "https://api.mistral.ai/v1"
        else:
            bearer = None
            self.url = ""

        headers = {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json"
        }
        data = {
            "model": self.model,
            "messages": messages,
            "tools": tools
        }
        if self.model not in self.models.get("google", []):
            data.update(self.options)

        name = sender2 if sender2 else sender
        url = f"{self.url}/chat/completions"

        async def get_completion(d):
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
                response = await client.post(url, headers=headers, json=d)
                return response.json()

        if tools is not None:
            result = await get_completion(data)
            max_iter = 10
            iterations = 0
            while result['choices'][0]['message'].get('tool_calls', []) and iterations < max_iter:
                msg = result['choices'][0]['message']
                self.messages[channel][sender].append(msg)
                for tool_call in msg.get('tool_calls', []):
                    tool_name = tool_call['function']['name']
                    args = json.loads(tool_call['function']['arguments'])
                    self.log(f"Calling tool: {tool_name} with args: {args}")
                    try:
                        tool_result = await globals()[tool_name](**args)
                    except Exception as e:
                        self.log(f"Error calling tool {tool_name}: {e}")
                        tool_result = f"Error calling tool {tool_name}: {e}"
                    self.messages[channel][sender].append({
                        "role": "tool",
                        "tool_call_id": tool_call['id'],
                        "content": tool_result
                    })
                data["messages"] = self.messages[channel][sender]
                result = await get_completion(data)
                iterations += 1
            text = result['choices'][0]['message'].get('content', '')
            lines = self.chop(text.strip())
            return name, lines

        result = await get_completion(data)
        lines = self.chop(result['choices'][0]['message']['content'])
        return name, lines

    async def thinking(self, lines):
        try:
            thinking = ' '.join(lines[lines.index('<think>'):lines.index('</think>')+1])
        except ValueError:
            thinking = None
        if thinking is not None:
            self.log(f"Thinking: {thinking}")
            lines = lines[lines.index('</think>')+2:]
        joined_lines = ' '.join(lines)
        return lines, joined_lines

    async def add_history(self, role, channel, sender, message, default=True):
        if channel not in self.messages:
            self.messages[channel] = {}
        if sender not in self.messages[channel]:
            self.messages[channel][sender] = []
            if default:
                self.messages[channel][sender].append({"role": "system", "content": self.prompt[0] + self.default_personality + self.prompt[1]})
        self.messages[channel][sender].append({"role": role, "content": message})
        if len(self.messages[channel][sender]) > self.history_size:
            if self.messages[channel][sender][0]["role"] == "system":
                self.messages[channel][sender].pop(1)
            else:
                self.messages[channel][sender].pop(0)
            self.messages[channel][sender] = [m for m in self.messages[channel][sender] if not ((m['role'] == "tool") or ('tool_calls' in m))]

    async def ai(self, channel, sender, message, x=False, peer_id=None):
        if x and len(message) > 2:
            target = message[1]
            message = ' '.join(message[2:])
            if target in self.messages.get(channel, {}):
                await self.add_history("user", channel, target, message)
                name, lines = await self.respond(channel, target, self.messages[channel][target], sender2=sender, tools=self.tools)
                lines, joined_lines = await self.thinking(lines)
                await self.add_history("assistant", channel, target, joined_lines)
            else:
                return
        else:
            message = ' '.join(message[1:])
            await self.add_history("user", channel, sender, message)
            name, lines = await self.respond(channel, sender, self.messages[channel][sender], tools=self.tools)
            lines, joined_lines = await self.thinking(lines)
            await self.add_history("assistant", channel, name, joined_lines)

        self.log(f"Sending response to {name} in {channel}: '{joined_lines}'")
        if channel == "privmsg":
            for line in lines:
                await self.bitchat.send_private_message(line, peer_id, sender)
                await asyncio.sleep(1.5)
        else:
            old_mode = self.bitchat.chat_context.current_mode
            if isinstance(old_mode, Channel) and old_mode.name != channel:
                self.bitchat.chat_context.switch_to_channel_silent(channel)
            elif not isinstance(old_mode, Channel):
                self.bitchat.chat_context.switch_to_channel_silent(channel)
            for line in lines:
                await self.bitchat.send_public_message(line)
                await asyncio.sleep(1.5)
            self.bitchat.chat_context.current_mode = old_mode

    async def set_prompt(self, channel, sender, persona=None, custom=None, respond=True, peer_id=None):
        if channel in self.messages and sender in self.messages[channel]:
            self.messages[channel][sender].clear()
        if persona is not None:
            system_prompt = self.prompt[0] + persona + self.prompt[1]
        elif custom is not None:
            system_prompt = custom
        else:
            system_prompt = self.system_prompt
        await self.add_history("system", channel, sender, system_prompt, default=False)
        self.log(f"System prompt for {sender} set to '{system_prompt}'")
        if respond:
            await self.add_history("user", channel, sender, "introduce yourself")
            name, lines = await self.respond(channel, sender, self.messages[channel][sender], tools=self.tools)
            lines, joined_lines = await self.thinking(lines)
            await self.add_history("assistant", channel, name, joined_lines)
            if channel == "privmsg":
                for line in lines:
                    await self.bitchat.send_private_message(line, peer_id, sender)
                    await asyncio.sleep(1.5)
            else:
                old_mode = self.bitchat.chat_context.current_mode
                if isinstance(old_mode, Channel) and old_mode.name != channel:
                    self.bitchat.chat_context.switch_to_channel_silent(channel)
                elif not isinstance(old_mode, Channel):
                    self.bitchat.chat_context.switch_to_channel_silent(channel)
                for line in lines:
                    await self.bitchat.send_public_message(line)
                    await asyncio.sleep(1.5)
                self.bitchat.chat_context.current_mode = old_mode

    async def reset(self, channel, sender, stock=False, peer_id=None):
        if channel in self.messages and sender in self.messages[channel]:
            self.messages[channel][sender].clear()
        if not stock:
            await self.set_prompt(channel, sender, persona=self.default_personality, respond=False, peer_id=peer_id)
            if channel == "privmsg":
                await self.bitchat.send_private_message(f"{self.nickname} reset to default for {sender}", peer_id, sender)
            else:
                old_mode = self.bitchat.chat_context.current_mode
                self.bitchat.chat_context.switch_to_channel_silent(channel)
                await self.bitchat.send_public_message(f"{self.nickname} reset to default for {sender}")
                self.bitchat.chat_context.current_mode = old_mode
        else:
            if channel == "privmsg":
                await self.bitchat.send_private_message(f"Stock settings applied for {sender}", peer_id, sender)
            else:
                old_mode = self.bitchat.chat_context.current_mode
                self.bitchat.chat_context.switch_to_channel_silent(channel)
                await self.bitchat.send_public_message(f"Stock settings applied for {sender}")
                self.bitchat.chat_context.current_mode = old_mode

    async def gpersona(self, persona):
        if persona is not None:
            self.default_personality = persona
            self.system_prompt = self.prompt[0] + self.default_personality + self.prompt[1]
            self.log(f"Default personality set to {self.default_personality}")

    async def handle_message(self, channel, sender, message, peer_id):
        user_commands = {
            ".ai": lambda: self.ai(channel, sender, message, peer_id=peer_id),
            f"{self.nickname}:": lambda: self.ai(channel, sender, message, peer_id=peer_id),
            f"{self.nickname},": lambda: self.ai(channel, sender, message, peer_id=peer_id),
            ".x": lambda: self.ai(channel, sender, message, x=True, peer_id=peer_id),
            ".persona": lambda: self.set_prompt(channel, sender, persona=' '.join(message[1:]), peer_id=peer_id),
            ".custom": lambda: self.set_prompt(channel, sender, custom=' '.join(message[1:]), peer_id=peer_id),
            ".reset": lambda: self.reset(channel, sender, peer_id=peer_id),
            ".stock": lambda: self.reset(channel, sender, stock=True, peer_id=peer_id),
            ".help": lambda: self.help_menu(sender, peer_id)
        }
        admin_commands = {
            ".model": lambda: self.change_model(channel, model=message[1] if len(message) > 1 else None, sender=sender, peer_id=peer_id),
            ".gpersona": lambda: self.gpersona(' '.join(message[1:]) if len(message) > 1 else None)
        }
        command = message[0]
        if command in user_commands:
            self.log(f"Received message from {sender} in {channel}: '{' '.join(message)}'")
            await user_commands[command]()
        if sender in self.config.get("irc", {}).get("admins", []) and command in admin_commands:
            await admin_commands[command]()

    async def handle_privmsg(self, sender, message, peer_id):
        user_commands = {
            ".persona": lambda: self.set_prompt("privmsg", sender, persona=' '.join(message[1:]), peer_id=peer_id),
            ".custom": lambda: self.set_prompt("privmsg", sender, custom=' '.join(message[1:]), peer_id=peer_id),
            ".reset": lambda: self.reset("privmsg", sender, peer_id=peer_id),
            ".stock": lambda: self.reset("privmsg", sender, stock=True, peer_id=peer_id),
            ".help": lambda: self.help_menu(sender, peer_id)
        }
        admin_commands = {
            ".model": lambda: self.change_model("privmsg", model=message[1] if len(message) > 1 else None, sender=sender, peer_id=peer_id),
            ".gpersona": lambda: self.gpersona(' '.join(message[1:]) if len(message) > 1 else None)
        }
        command = message[0]
        if command in user_commands:
            self.log(f"Received private message from {sender}: '{' '.join(message)}'")
            await user_commands[command]()
        elif sender in self.config.get("irc", {}).get("admins", []) and command in admin_commands:
            await admin_commands[command]()
        else:
            await self.add_history("user", "privmsg", sender, ' '.join(message))
            name, lines = await self.respond("", sender, self.messages["privmsg"][sender], tools=self.tools)
            lines, joined_lines = await self.thinking(lines)
            await self.add_history("assistant", "privmsg", sender, joined_lines)
            for line in lines:
                await self.bitchat.send_private_message(line, peer_id, sender)
                await asyncio.sleep(1.5)

    async def change_model(self, channel, model=None, sender=None, peer_id=None):
        if model is not None:
            for provider, models in self.models.items():
                if model in models:
                    self.model = model
                    self.log(f"Model set to {self.model}")
                    if channel == "privmsg":
                        await self.bitchat.send_private_message(f"Model set to {self.model}", peer_id, sender)
                    else:
                        old_mode = self.bitchat.chat_context.current_mode
                        self.bitchat.chat_context.switch_to_channel_silent(channel)
                        await self.bitchat.send_public_message(f"Model set to {self.model}")
                        self.bitchat.chat_context.current_mode = old_mode
                    return
            if channel == "privmsg":
                await self.bitchat.send_private_message(f"Model {model} not found in available models.", peer_id, sender)
            else:
                old_mode = self.bitchat.chat_context.current_mode
                self.bitchat.chat_context.switch_to_channel_silent(channel)
                await self.bitchat.send_public_message(f"Model {model} not found in available models.")
                self.bitchat.chat_context.current_mode = old_mode
        else:
            if channel == "privmsg":
                current_model = [f"Current model: {self.model}", "Available models: " + ", ".join([m for prov, models in self.models.items() for m in models])]
                lines = self.chop('\n'.join(current_model))
                for line in lines:
                    await self.bitchat.send_private_message(line, peer_id, sender)
            else:
                current_model = [f"Current model: {self.model}", "Available models: " + ", ".join([m for prov, models in self.models.items() for m in models])]
                lines = self.chop('\n'.join(current_model))
                old_mode = self.bitchat.chat_context.current_mode
                self.bitchat.chat_context.switch_to_channel_silent(channel)
                for line in lines:
                    await self.bitchat.send_public_message(line)
                self.bitchat.chat_context.current_mode = old_mode

    async def help_menu(self, sender, peer_id):
        with open("help.txt", "r") as f:
            help_text = f.readlines()
        for line in help_text:
            await self.bitchat.send_private_message(line.strip(), peer_id, sender)
            await asyncio.sleep(1.5)

    async def on_message(self, message, packet, is_private):
        sender_nick = self.bitchat.peers.get(packet.sender_id_str, Peer()).nickname or packet.sender_id_str
        words = message.content.split()
        if is_private:
            if "privmsg" not in self.messages:
                self.messages["privmsg"] = {}
            if sender_nick not in self.messages["privmsg"]:
                self.messages["privmsg"][sender_nick] = []
                self.messages["privmsg"][sender_nick].append({"role": "system", "content": self.prompt[0] + self.default_personality + self.prompt[1]})
            await self.handle_privmsg(sender_nick, words, packet.sender_id_str)
        else:
            channel = message.channel or "public"
            if channel not in self.messages:
                self.messages[channel] = {}
            if sender_nick not in self.messages[channel]:
                self.messages[channel][sender_nick] = []
                self.messages[channel][sender_nick].append({"role": "system", "content": self.prompt[0] + self.default_personality + self.prompt[1]})
            await self.handle_message(channel, sender_nick, words, packet.sender_id_str)

    async def start(self):
        await self.bitchat.run_bot()

if __name__ == "__main__":
    bot = InfiniGPTBitchat()
    asyncio.run(bot.start())
