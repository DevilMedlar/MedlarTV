from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from MedlarTV.core.viewer_profiles import ViewerProfileStore


_NOW = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)


class ViewerProfileSafetyTests(unittest.TestCase):
    def test_ambiguous_or_child_identifying_favorites_are_not_saved(self):
        unsafe = (
            "My favorite color is my son Kai’s blue",
            "My favorite color is my son Kai's blue",
            "My favorite color is blue, that's a joke",
            "My favorite color is anything but blue",
            "My favorite color is blue or green",
            "My favorite color is blue and green",
            "My favorite color is blue because my daughter Maya likes it",
            "My favorite food is noodles, and my child's name is Kai",
            "My favorite food is pizza or sushi",
            "My favorite food is pizza and my spouse is sick",
            "My favorite movie is my kid's school film",
            "My favorite color is my little one Kai's blue",
            "My favorite color is my 7yo Kai's blue",
            "My favorite color is my spouse Rina's blue",
            "My favorite color is Kai's blue",
            "My favorite color is Kai blue",
            "My favorite color is 17 Orchard Lane",
            "My favorite color is Silverwood Elementary blue",
            "My favorite food is my friend Nora's recipe",
            "My favorite movie is ‘a pretend title’",
            "My favorite color is blue?",
            "My favorite color is probably blue",
            "My favorite food is pizza no wait",
            "My favorite food is pizza I lied",
            "My favorite food is jambalaya. I prefer risotto now",
            "My favorite food is jambalaya! Scratch that",
            "My favorite color is Blue! Wait, I meant green",
            "My favorite food is pasta (Kai likes it)",
            "My favorite food is pasta Mila likes it",
            "My favorite movie is The Adventure where Nora lives",
            "My favorite food is mobile 55 50123",
            "My favorite food is contact number: 55 50123",
            "My favorite food is pizza at 17 Oak Place",
            "My favorite food is pizza 123 Elm Parade",
            "My favorite drink is tea from 8 Birch Terrace",
            "My favorite food is pizza at 10 Maple Row",
            "My favorite sport is walking near 21 Cedar Mews",
            "My favorite game is example.com",
            "My favorite book is stories.example.net",
            "My favorite game is example dot com",
            "My favorite game is example dot app",
            "My favorite game is example dot ai",
            "My favorite food is pizza due to diabetes",
            "My favorite food is pizza despite allergies",
            "My favorite food is pizza due to a heart condition",
            "My favorite drink is tea despite a respiratory disorder",
            "My favorite food is pizza because of my medical condition",
            "My favorite food is pizza!sushi",
            "My real name is Jane maybe",
            "My real name is Elara Voss LOL",
            "My full name is Mira probably",
            "My name is Jane allegedly",
            "My name is Jane who knows",
            "My name is Jane uncertain",
        )
        for message in unsafe:
            with self.subTest(message=message), TemporaryDirectory() as temporary:
                path = Path(temporary) / "profiles.sqlite3"
                with ViewerProfileStore(path) as store:
                    result = store.observe("room-a", "stable-100", message, now=_NOW)
                    self.assertEqual(result.kind, "ignored")
                    self.assertEqual(store.get_facts("room-a", "stable-100"), {})
                    self.assertFalse(path.exists())

    def test_unsafe_correction_does_not_replace_a_clear_favorite(self):
        with TemporaryDirectory() as temporary:
            with ViewerProfileStore(Path(temporary) / "profiles.sqlite3") as store:
                store.observe("room-a", "stable-100", "My favorite color is indigo", now=_NOW)
                for message in (
                    "Actually, my favorite color is blue, that's a joke",
                    "Actually, my favorite color is my daughter Maya's blue",
                    "Actually, my favorite color is anything but blue",
                    "Actually, my favorite color is blue. I think I have asthma",
                    "Actually, my favorite color is blue. I prefer green now",
                    "Actually, my favorite color is blue due to diabetes",
                    "Actually, my favorite food is pizza due to a heart condition",
                    "Actually, my favorite color is blue!green",
                ):
                    self.assertEqual(
                        store.observe("room-a", "stable-100", message, now=_NOW).kind,
                        "ignored",
                    )
                self.assertEqual(
                    store.get_facts("room-a", "stable-100")["favorite_color"].value,
                    "indigo",
                )

    def test_joking_name_correction_does_not_replace_real_name(self):
        with TemporaryDirectory() as temporary:
            with ViewerProfileStore(Path(temporary) / "profiles.sqlite3") as store:
                self.assertEqual(store.observe(
                    "room-a", "stable-100", "My real name is Elara Voss", now=_NOW,
                ).kind, "saved")
                self.assertEqual(store.observe(
                    "room-a", "stable-100", "Actually, my real name is Elara Voss LOL",
                    now=_NOW,
                ).kind, "ignored")
                self.assertEqual(store.get_facts(
                    "room-a", "stable-100",
                )["real_name"].value, "Elara Voss")

    def test_clear_favorites_still_save(self):
        with TemporaryDirectory() as temporary:
            with ViewerProfileStore(Path(temporary) / "profiles.sqlite3") as store:
                self.assertEqual(store.observe(
                    "room-a", "stable-100", "My favorite food is mac and cheese",
                    now=_NOW,
                ).kind, "saved")
                self.assertEqual(store.observe(
                    "room-a", "stable-100", "My favorite color is cerulean",
                    now=_NOW,
                ).kind, "saved")
                self.assertEqual(store.observe(
                    "room-a", "stable-100", "My favorite color is Blue",
                    now=_NOW,
                ).kind, "saved")
                self.assertEqual(store.observe(
                    "room-a", "stable-100", "My favorite color is Light Blue",
                    now=_NOW,
                ).kind, "saved")
                self.assertEqual(store.observe(
                    "room-a", "stable-100", "My favorite game is Stardew Valley",
                    now=_NOW,
                ).kind, "saved")
                facts = store.get_facts("room-a", "stable-100")
                self.assertEqual(facts["favorite_food"].value, "mac and cheese")
                self.assertEqual(facts["favorite_color"].value, "Light Blue")
                self.assertEqual(facts["favorite_game"].value, "Stardew Valley")

    def test_clear_compound_health_report_saves_two_separate_scoped_facts(self):
        with TemporaryDirectory() as temporary:
            with ViewerProfileStore(Path(temporary) / "profiles.sqlite3") as store:
                result = store.observe(
                    "room-a", "stable-100",
                    "My favorite food is pizza. I have diabetes.",
                    message_id="synthetic-1", now=_NOW,
                )
                self.assertEqual(result.kind, "saved")
                facts = store.get_facts("room-a", "stable-100")
                self.assertEqual(facts["favorite_food"].value, "pizza")
                self.assertEqual(facts["health_condition"].value, "diabetes")
                self.assertEqual(facts["favorite_food"].source_message_id, "synthetic-1")
                self.assertEqual(facts["health_condition"].source_message_id, "synthetic-1")
                self.assertEqual(facts["favorite_food"].basis, "explicit self-report")
                self.assertEqual(facts["health_condition"].basis, "explicit self-report")
                self.assertNotIn("diabetes", store.format_profile_context(
                    "room-a", "stable-100", query="How is the dragon boss?",
                ))
                self.assertNotIn("diabetes", store.format_profile_context(
                    "room-a", "stable-100", query="What do you remember about me?",
                ))
                food_answer = store.get_answer(
                    "room-a", "stable-100", "What is my favorite food?",
                )
                assert isinstance(food_answer, str), "Expected a viewer-profile answer"
                self.assertIn("pizza", food_answer)
                health_answer = store.get_answer(
                    "room-a", "stable-100", "What health condition did I tell you about?",
                )
                assert isinstance(health_answer, str), "Expected a viewer-profile answer"
                self.assertIn("previously reported diabetes", health_answer)
                self.assertIsNone(store.get_answer(
                    "room-a", "stable-200", "What is my health condition?",
                ))
                self.assertIsNone(store.get_answer(
                    "room-b", "stable-100", "What is my health condition?",
                ))

    def test_health_correction_negation_and_forget_are_scoped(self):
        with TemporaryDirectory() as temporary:
            with ViewerProfileStore(Path(temporary) / "profiles.sqlite3") as store:
                store.observe("room-a", "stable-100", "I have diabetes", now=_NOW)
                store.observe("room-a", "stable-200", "I have asthma", now=_NOW)
                self.assertEqual(store.observe(
                    "room-a", "stable-100", "I have asthma", now=_NOW,
                ).kind, "clarify")
                self.assertEqual(store.get_facts("room-a", "stable-100")["health_condition"].value, "diabetes")
                result = store.observe("room-a", "stable-100", "Actually, I have asthma", now=_NOW)
                self.assertEqual(result.kind, "saved")
                self.assertEqual(store.get_facts("room-a", "stable-100")["health_condition"].value, "asthma")
                self.assertEqual(store.observe(
                    "room-a", "stable-100", "I do not have diabetes", now=_NOW,
                ).kind, "ignored")
                self.assertEqual(store.observe(
                    "room-a", "stable-100", "I no longer have asthma", now=_NOW,
                ).kind, "forgotten")
                self.assertNotIn("health_condition", store.get_facts("room-a", "stable-100"))
                self.assertEqual(store.get_facts("room-a", "stable-200")["health_condition"].value, "asthma")
                store.observe("room-a", "stable-100", "I have diabetes", now=_NOW)
                self.assertEqual(store.forget("room-a", "stable-100", "health condition"), 1)
                self.assertNotIn("health_condition", store.get_facts("room-a", "stable-100"))
                self.assertEqual(store.observe(
                    "room-a", "stable-100", "I am diabetic", now=_NOW,
                ).kind, "saved")
                self.assertEqual(store.get_facts("room-a", "stable-100")["health_condition"].value, "diabetes")

    def test_uncertain_quoted_or_third_party_health_does_not_persist(self):
        unsafe = (
            '"I have diabetes"', "My sister has diabetes",
            "I think I have diabetes", "I do not have diabetes",
            "My favorite food is pizza. I think I have diabetes",
            "My favorite food is pizza. My sister has diabetes",
            "My favorite food is pizza. I have diabetes. Just kidding",
            "My favorite food is pizza. I have diabetes?",
        )
        for message in unsafe:
            with self.subTest(message=message), TemporaryDirectory() as temporary:
                path = Path(temporary) / "profiles.sqlite3"
                with ViewerProfileStore(path) as store:
                    self.assertEqual(store.observe(
                        "room-a", "stable-100", message, now=_NOW,
                    ).kind, "ignored")
                    self.assertFalse(path.exists())

    def test_explicit_real_name_is_scoped_and_contact_values_are_rejected(self):
        with TemporaryDirectory() as temporary:
            with ViewerProfileStore(Path(temporary) / "profiles.sqlite3") as store:
                self.assertEqual(store.observe(
                    "room-a", "stable-100", "My real name is Elara Voss",
                    message_id="synthetic-name", now=_NOW,
                ).kind, "saved")
                fact = store.get_facts("room-a", "stable-100")["real_name"]
                self.assertEqual(fact.value, "Elara Voss")
                self.assertEqual(fact.source_message_id, "synthetic-name")
                self.assertEqual(fact.basis, "explicit self-report")
                name_answer = store.get_answer(
                    "room-a", "stable-100", "What is my real name?",
                )
                assert isinstance(name_answer, str), "Expected a viewer-profile answer"
                self.assertIn("Elara Voss", name_answer)
                self.assertIsNone(store.get_answer(
                    "room-a", "stable-200", "What is my real name?",
                ))
                self.assertIsNone(store.get_answer(
                    "room-b", "stable-100", "What is my real name?",
                ))
                for query in ("How is the boss?", "What do you remember about me?"):
                    self.assertNotIn("Elara", store.format_profile_context(
                        "room-a", "stable-100", query=query,
                    ))
                for message in (
                    "My son's name is Kai", "Her name is Mara",
                    "My real name is Mira, and my daughter is sick",
                    "My real name is @mira", "My real name is mira@example.invalid",
                    "My real name is Mira no wait", "My favorite number is 5550123",
                    "My real name is Vesper perhaps",
                    "My favorite food is pizza. My phone is 555-0199",
                ):
                    self.assertEqual(store.observe(
                        "room-a", "stable-100", message, now=_NOW,
                    ).kind, "ignored", message)
                self.assertEqual(store.get_facts("room-a", "stable-100")["real_name"].value, "Elara Voss")
                self.assertEqual(store.observe(
                    "room-a", "stable-100", "Actually, my name is Mira Vale", now=_NOW,
                ).kind, "saved")
                self.assertEqual(store.get_facts("room-a", "stable-100")["real_name"].value, "Mira Vale")
                self.assertEqual(store.observe(
                    "room-a", "stable-100", "Forget my real name", now=_NOW,
                ).kind, "forgotten")
                self.assertNotIn("real_name", store.get_facts("room-a", "stable-100"))
                store.observe("room-a", "stable-100", "My name is Mira Vale", now=_NOW)
                self.assertEqual(store.forget("room-a", "stable-100", "name"), 1)
                self.assertNotIn("real_name", store.get_facts("room-a", "stable-100"))
                self.assertEqual(store.observe(
                    "room-a", "stable-100", "My name is Ivy Vale",
                    source_role="assistant", now=_NOW,
                ).kind, "ignored")


if __name__ == "__main__":
    unittest.main()
