"""Keyboard substitution limits without changing other trigger routes."""
import unittest
from unittest.mock import patch
from MedlarTV.core import fuzzy_trigger as fuzzy


class FuzzyKeyboardTests(unittest.TestCase):
    def test_identical_words_use_zero_substitutions(self):
        self.assertTrue(fuzzy.check_keyboard_distance('MEDLAR','medlar',0))

    def test_one_adjacent_substitution_counts_against_budget(self):
        self.assertTrue(fuzzy.check_keyboard_distance('nedlar','medlar',1))
        self.assertFalse(fuzzy.check_keyboard_distance('nedlar','medlar',0))

    def test_two_adjacent_substitutions_require_two_budget_units(self):
        self.assertFalse(fuzzy.check_keyboard_distance('nedkar','medlar',1))
        self.assertTrue(fuzzy.check_keyboard_distance('nedkar','medlar',2))

    def test_nonadjacent_substitution_is_not_a_keyboard_neighbor(self):
        self.assertFalse(fuzzy.check_keyboard_distance('xedlar','medlar',1))
        self.assertFalse(fuzzy.check_keyboard_distance('medla','medlar',2))

    def test_unrelated_all_neighbor_string_does_not_trigger(self):
        with patch.object(fuzzy,'load_trigger_keywords',return_value=['medlar']):
            self.assertFalse(fuzzy.is_trigger_word('jrsksf'))

    def test_exact_missing_extra_swapped_and_historical_typos_remain(self):
        with patch.object(fuzzy,'load_trigger_keywords',return_value=['medlartv','medlar']):
            for word in ('medlar','MEDLARTV','@medlartv','medlrtv','medltv',
                         'meedlartv','medlarttvv','mdelartv','nedlartv','medkartv','medlarvt'):
                self.assertTrue(fuzzy.is_trigger_word(word),word)

    def test_strict_mode_still_requires_exact_normalized_trigger(self):
        with patch.object(fuzzy,'load_trigger_keywords',return_value=['medlar']):
            self.assertTrue(fuzzy.is_trigger_word('@MEDLAR!',strict=True))
            self.assertFalse(fuzzy.is_trigger_word('nedlar',strict=True))
