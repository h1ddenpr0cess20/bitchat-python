# infinigpt-bitchat

![AGPL v3](https://img.shields.io/badge/license-AGPL--v3-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Issues](https://img.shields.io/github/issues/h1ddenpr0cess20/bitchat-python)

InfiniGPT-Bitchat is an asynchronous, multi-channel AI chatbot for BitChat, ported from [infinigpt-irc](https://github.com/h1ddenpr0cess20/infinigpt-irc). It supports OpenAI, xAI, Google, Mistral, HuggingFace, Anthropic, and Ollama models. Each user has their own chat history and personality setting, and the bot can roleplay as almost anything you can imagine.

## Features
- Multi-channel and private messaging support
- Per-user chat history and personality
- Supports multiple LLM providers: OpenAI, xAI, Google, Mistral, HuggingFace, Anthropic, Ollama
- Customizable system prompts and personalities
- Admin commands for model and personality management
- Extensible tool system (add your own tools in `tools.py` and `schema.json`)

## Setup
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Get API keys for the services you want to use (OpenAI, xAI, Google, Mistral, HuggingFace, Anthropic). Add them to `config.json`.
3. Edit `config.json` to set your BitChat nickname, channels, admins, and available models.
4. (Optional) Install and configure [Ollama](https://ollama.com/) for local LLMs.
5. (Optional) Add your own tools in `tools.py` and register them in `schema.json`.

## Usage
Run the bot:
```bash
python infinigpt_bitchat.py
```

### Commands
- `.ai <message>` or `<botname>: <message>`: Talk to the bot
- `.x <user> <message>`: Talk to another user's chat history
- `.persona <personality>`: Change your personality (character, type, object, idea)
- `.custom <prompt>`: Set a custom system prompt
- `.reset`: Reset to default personality
- `.stock`: Remove personality and reset to standard settings
- `.model`: List available models (admin only)
- `.model <modelname>`: Change model (admin only)
- `.join <channel>`: Join a channel (admin only)
- `.part <channel>`: Leave a channel (admin only)
- `.gpersona <personality>`: Set a new global default personality (admin only)
- `.help`: Display the help menu

You can also privately message the bot to chat without using commands.

## Extending
- Add new tools in `tools.py`
- Register tool schemas in `schema.json`

## License
This project is licensed under the GNU AGPL v3. See [LICENSE](LICENSE).

## Credits
- [infinigpt-irc](https://github.com/h1ddenpr0cess20/infinigpt-irc)
- [bitchat-python](https://github.com/kaganisildak/bitchat-python)
