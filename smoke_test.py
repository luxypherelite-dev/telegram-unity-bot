import os
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as tmp:
    os.environ["BOT_DATA_DIR"] = tmp
    os.environ["TELEGRAM_BOT_TOKEN"] = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    import bot

    assert bot.safe_session_name("UI_Mod") == "UI_Mod"
    try:
        bot.safe_session_name("../escape")
    except ValueError:
        pass
    else:
        raise AssertionError("path traversal session name accepted")

    assert list(bot.archive_candidates("Skin_123.png")) == ["skin_123", "skin"]
    assert bot.safe_filename("../../secret.png") == "secret.png"
    bot.init_db()
    bot.session_path(7, "UI_Mod")
    bot.create_session(7, "UI_Mod")
    assert bot.list_sessions(7) == ["UI_Mod"]
    bot.set_active_session(7, "UI_Mod")
    assert bot.active_session(7)[0] == "UI_Mod"
    application = bot.build_application()
    assert application is not None

print("smoke tests passed")
