# Reference findings

The provided `jackolegamer1/not-uabe` repository is a lightweight Unity Asset Bundle editor focused on `Texture2D` assets. Its README states that it supports viewing, PNG export, replacement, and rebuilding a modified bundle, with ASTC, ETC1, and ETC2 decoding noted as supported. The core implementation uses `UnityPy.load(...)`, iterates `env.objects`, filters `obj.type.name == "Texture2D"`, reads objects, obtains `data.image`, assigns a Pillow image to `asset.image`, calls `asset.save()`, and serializes the edited bundle with `env.file.save()`.

The reference dependency list contains Flask, UnityPy, Pillow, and texture2ddecoder. The new bot will add `python-telegram-bot` v20+ and use an asynchronous command architecture. The reference code has a single global workspace, so the new implementation must improve it with per-user named session directories and explicit active-session selection. The bot will support raw asset stream export in addition to PNG export, and all user-controlled paths/names must be sanitized and constrained to each user’s workspace.

Reference URLs:
- https://github.com/jackolegamer1/not-uabe
- https://raw.githubusercontent.com/jackolegamer1/not-uabe/main/README.md
- https://raw.githubusercontent.com/jackolegamer1/not-uabe/main/uabe.py
- https://raw.githubusercontent.com/jackolegamer1/not-uabe/main/gui_modder.py
- https://raw.githubusercontent.com/jackolegamer1/not-uabe/main/requirements.txt
