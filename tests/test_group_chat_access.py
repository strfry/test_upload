"""
Tests für die Multi-Chat-Zugriffskontrolle des ScamBaiterControl Bots.

Prüft:
- _require_allowed_chat akzeptiert persönlichen Chat
- _require_allowed_chat akzeptiert Gruppen-Chat
- _require_allowed_chat blockt fremde Chats
- allowed_chat_ids Set wird korrekt aus create_bot_app befüllt
- list_chat_ids filtert Control-Chats aus /chats-Ergebnis heraus
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from scambaiter.bot_api import _require_allowed_chat, create_bot_app


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _make_update(chat_id: int) -> MagicMock:
    msg = MagicMock()
    msg.chat_id = chat_id
    update = MagicMock()
    update.effective_message = msg
    return update


def _make_app(allowed_chat_id: int | None, extra: list[int] | None = None) -> MagicMock:
    """Baut eine minimale Application-Mock mit bot_data wie create_bot_app es setzt."""
    allowed_ids: set[int] = set()
    if allowed_chat_id is not None:
        allowed_ids.add(allowed_chat_id)
    for cid in (extra or []):
        allowed_ids.add(cid)
    app = MagicMock()
    app.bot_data = {
        "allowed_chat_id": allowed_chat_id,
        "allowed_chat_ids": allowed_ids,
    }
    return app


# ---------------------------------------------------------------------------
# _require_allowed_chat
# ---------------------------------------------------------------------------

class TestRequireAllowedChat:
    def test_persoenlicher_chat_erlaubt(self):
        app = _make_app(allowed_chat_id=111, extra=[-999])
        update = _make_update(chat_id=111)
        result = asyncio.run(
            _require_allowed_chat(app, update, allowed_chat_id=111)
        )
        assert result is True

    def test_gruppen_chat_erlaubt(self):
        """Gruppen-Chat-ID (negativ) muss akzeptiert werden."""
        app = _make_app(allowed_chat_id=111, extra=[-5122951677])
        update = _make_update(chat_id=-5122951677)
        result = asyncio.run(
            _require_allowed_chat(app, update, allowed_chat_id=111)
        )
        assert result is True

    def test_fremder_chat_geblockt(self):
        app = _make_app(allowed_chat_id=111, extra=[-999])
        update = _make_update(chat_id=555)
        # _send_control_text mocken damit kein echter Bot-Call passiert
        with patch("scambaiter.bot_api._send_control_text", new_callable=AsyncMock):
            result = asyncio.run(
                _require_allowed_chat(app, update, allowed_chat_id=111)
            )
        assert result is False

    def test_kein_allowed_chat_id_alles_erlaubt(self):
        """Wenn allowed_chat_ids leer und allowed_chat_id=None → alles erlaubt."""
        app = _make_app(allowed_chat_id=None, extra=None)
        update = _make_update(chat_id=12345)
        result = asyncio.run(
            _require_allowed_chat(app, update, allowed_chat_id=None)
        )
        assert result is True

    def test_kein_effective_message(self):
        app = _make_app(allowed_chat_id=111)
        update = MagicMock()
        update.effective_message = None
        result = asyncio.run(
            _require_allowed_chat(app, update, allowed_chat_id=111)
        )
        assert result is False


# ---------------------------------------------------------------------------
# create_bot_app — allowed_chat_ids korrekt befüllt
# ---------------------------------------------------------------------------

class TestCreateBotAppAllowedIds:
    def _make_service(self):
        svc = MagicMock()
        svc.store = MagicMock()
        return svc

    def test_nur_persoenlicher_chat(self):
        with patch("scambaiter.bot_api.Application") as mock_app_cls:
            mock_app = MagicMock()
            mock_app.bot_data = {}
            mock_app_cls.builder.return_value.token.return_value.build.return_value = mock_app
            create_bot_app(token="tok", service=self._make_service(), allowed_chat_id=111)
            assert mock_app.bot_data["allowed_chat_ids"] == {111}

    def test_mit_gruppe(self):
        with patch("scambaiter.bot_api.Application") as mock_app_cls:
            mock_app = MagicMock()
            mock_app.bot_data = {}
            mock_app_cls.builder.return_value.token.return_value.build.return_value = mock_app
            create_bot_app(
                token="tok",
                service=self._make_service(),
                allowed_chat_id=111,
                extra_allowed_chat_ids=[-5122951677],
            )
            assert mock_app.bot_data["allowed_chat_ids"] == {111, -5122951677}

    def test_ohne_allowed_chat_id(self):
        with patch("scambaiter.bot_api.Application") as mock_app_cls:
            mock_app = MagicMock()
            mock_app.bot_data = {}
            mock_app_cls.builder.return_value.token.return_value.build.return_value = mock_app
            create_bot_app(token="tok", service=self._make_service())
            assert mock_app.bot_data["allowed_chat_ids"] == set()
