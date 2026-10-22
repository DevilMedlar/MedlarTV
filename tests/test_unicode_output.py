"""Real formatting/policy/frame methods with complete graphemes and fake I/O."""
from collections.abc import Callable
from typing import Protocol, TypedDict, runtime_checkable

import unittest
from unittest.mock import patch

from MedlarTV.core.text_clusters import clusters, limit_emoji_clusters, truncate_clusters
import test_twitch_outgoing as outgoing

COMPLEX=('e\u0301','漢','한','한','👩🏽‍💻','🇵🇭','1️⃣','🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f')


class _Toggle(TypedDict):
    enabled: bool


class _EmojiLimit(_Toggle):
    max_per_message: int


class _ResponseModes(TypedDict):
    all_caps: _Toggle
    emoji_limit: _EmojiLimit


class _Safety(TypedDict):
    max_message_length: int


class _Config(TypedDict):
    blocked_words: list[str]
    blocked_topics: list[str]
    safety: _Safety
    response_modes: _ResponseModes


class _Filter(Protocol):
    limit_emojis: Callable[[str, _Config], str]


class _Policy(Protocol):
    def format_body(self, text: str) -> str: ...


class _Socket(Protocol):
    sent: list[bytes]


class _Client(Protocol):
    sock: _Socket
    _decorate_message: Callable[[str], str]

    def _load_output_policy(self) -> _Policy: ...
    def send_message(self, text: str) -> bool: ...


class _ClientClass(Protocol):
    MAX_IRC_FRAME_BYTES: int
    MAX_CHAT_CHARACTERS: int

    @staticmethod
    def _fit_chat_body(text: str, channel: str, suffix: str = "") -> str: ...


@runtime_checkable
class _Harness(Protocol):
    config: _Config
    filter_module: _Filter
    client: _Client
    client_class: _ClientClass

    def setUp(self) -> None: ...
    def doCleanups(self) -> bool: ...


def checked_harness(value: object) -> _Harness:
    assert isinstance(value, _Harness), "Expected outgoing fixture interfaces"
    return value


def unchanged_text(text: str) -> str:
    return text


class UnicodeOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture=outgoing.TwitchOutgoingTests('runTest')
        fixture.setUp()
        self.h=checked_harness(fixture)
        self.addCleanup(self.h.doCleanups)
        self.limit=self.h.filter_module.limit_emojis

    def config(self,maximum: int) -> _Config:
        self.h.config['response_modes']['emoji_limit']['max_per_message']=maximum
        return self.h.config

    def test_cjk_hangul_and_mixed_language_are_never_removed_as_emoji(self) -> None:
        for text in ('中文 日本語 漢字 語言','한글 한국 서울 부산','café 漢字 한글 e\u0301'):
            self.assertEqual(text,self.limit(text,self.config(0)))

    def test_ten_adjacent_emoji_are_ten_not_one_run(self) -> None:
        self.assertEqual('😀'*3,self.limit('😀'*10,self.config(3)))
        self.assertEqual('',self.limit('😀'*10,self.config(0)))

    def test_flags_modifiers_and_zwj_each_count_as_one_complete_cluster(self) -> None:
        text='👩🏽‍💻🇵🇭👍🏽😀'
        self.assertEqual('👩🏽‍💻🇵🇭',self.limit(text,self.config(2)))
        self.assertEqual(['👩🏽‍💻','🇵🇭','👍🏽','😀'],list(clusters(text)))

    def test_keycaps_count_but_plain_digits_punctuation_and_symbols_remain(self) -> None:
        text='123 # * © ™ ♥ ☀ 1️⃣2️⃣'
        self.assertEqual('123 # * © ™ ♥ ☀ ',self.limit(text,self.config(0)))
        self.assertEqual('123 # * © ™ ♥ ☀ 1️⃣',self.limit(text,self.config(1)))

    def test_explicit_text_variation_is_preserved_and_emoji_variation_is_limited(self) -> None:
        self.assertEqual('♥︎©︎',self.limit('♥︎©︎♥️©️',self.config(0)))
        self.assertEqual('♥︎©︎♥️',self.limit('♥︎©︎♥️©️',self.config(1)))

    def test_tag_sequence_is_removed_as_one_unit(self) -> None:
        flag=COMPLEX[-1]
        self.assertEqual([flag],list(clusters(flag)))
        self.assertEqual('A B',self.limit('A'+flag+' B'+flag,self.config(0)))

    def test_disabled_limit_preserves_original_text(self) -> None:
        self.h.config['response_modes']['emoji_limit']['enabled']=False
        text='中文😀'*10
        self.assertEqual(text,self.limit(text,self.config(0)))

    def test_repeated_and_separated_emoji_keep_first_occurrences_and_whitespace(self) -> None:
        self.assertEqual('😀 a 😀 b  c ',self.limit('😀 a 😀 b 😀 c 😀',self.config(2)))

    def test_configured_character_limit_drops_whole_straddling_clusters(self) -> None:
        self.h.config['response_modes']['emoji_limit']['enabled']=False
        for cluster in COMPLEX:
            self.h.config['safety']['max_message_length']=len(cluster)+5
            policy=self.h.client._load_output_policy()  # pyright: ignore[reportPrivateUsage] - White-box output boundary check.
            self.assertEqual('abc...',policy.format_body('abc'+cluster+'tail'),repr(cluster))

    def test_configured_tiny_limits_preserve_existing_marker_budget(self) -> None:
        for maximum in (1,2,3):
            self.h.config['safety']['max_message_length']=maximum
            self.assertEqual('.'*maximum,self.h.client._load_output_policy().format_body('👩🏽‍💻tail'))  # pyright: ignore[reportPrivateUsage] - White-box output boundary check.

    def test_decorated_wire_byte_limit_never_leaves_partial_cluster(self) -> None:
        channel='#devilmedlar'
        suffix=' Kappa'
        header=len(('PRIVMSG '+channel+' :\r\n').encode())
        cls=self.h.client_class
        for cluster in COMPLEX:
            budget=len(cluster.encode('utf-8'))+5+len(suffix.encode())
            with patch.object(cls,'MAX_IRC_FRAME_BYTES',header+budget):
                result=cls._fit_chat_body('abc'+cluster+'tail',channel,suffix)  # pyright: ignore[reportPrivateUsage] - White-box output boundary check.
            self.assertEqual('abc... Kappa',result,repr(cluster))
            self.assertLessEqual(len(('PRIVMSG '+channel+' :'+result+'\r\n').encode()),header+budget)

    def test_decorated_character_limit_never_leaves_partial_cluster(self) -> None:
        cls=self.h.client_class
        for cluster in COMPLEX:
            with patch.object(cls,'MAX_CHAT_CHARACTERS',len(cluster)+5+len(' Kappa')):
                self.assertEqual('abc... Kappa',cls._fit_chat_body('abc'+cluster+'tail','#devilmedlar',' Kappa'))  # pyright: ignore[reportPrivateUsage] - White-box output boundary check.

    def test_exact_fit_preserves_complete_clusters_without_added_marker(self) -> None:
        for cluster in COMPLEX:
            self.assertEqual(cluster,truncate_clusters(cluster,len(cluster),len(cluster.encode())))
        self.assertEqual('café',truncate_clusters('café',4,5))

    def test_real_send_preserves_cjk_and_caps_adjacent_emoji_before_write(self) -> None:
        self.h.client._decorate_message=unchanged_text  # pyright: ignore[reportPrivateUsage] - White-box output boundary check.
        self.assertTrue(self.h.client.send_message('中文 日本語 한글 😀😀😀😀😀'))
        body=self.h.client.sock.sent[-1].decode().partition(' :')[2].removesuffix('\r\n')
        self.assertEqual('中文 日本語 한글 😀😀😀',body)

    def test_invalid_emoji_limit_and_negative_budgets_fail_without_partial_output(self) -> None:
        for value in (-1,True,'2'):
            with self.assertRaises(ValueError):
                limit_emoji_clusters('😀',value)
        with self.assertRaises(ValueError):
            truncate_clusters('abc',-1)
