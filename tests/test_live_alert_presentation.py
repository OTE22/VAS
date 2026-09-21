import unittest
from types import SimpleNamespace
from backend.core.live_alert_presentation import alert_display_name, alert_identity_type
from db_models import IdentityType


class AlertPresentationTests(unittest.TestCase):
    def test_generated_name_follows_current_identity_without_rewriting_history(self):
        identity = SimpleNamespace(display_name='Joey', type=IdentityType.KNOWN)
        alert = SimpleNamespace(auto_name=True, name='Track Unknown Person - 2026-09-16', identity=identity)
        self.assertEqual(alert_display_name(alert), 'Track Joey')
        self.assertEqual(alert_identity_type(alert), 'known')
        identity.display_name = 'Joseph'
        self.assertEqual(alert_display_name(alert), 'Track Joseph')
        self.assertEqual(alert.name, 'Track Unknown Person - 2026-09-16')

    def test_custom_and_legacy_titles_are_preserved_even_when_they_look_generated(self):
        for name in ['Gate investigation', 'Track Unknown Person - 2026-09-16']:
            alert = SimpleNamespace(auto_name=False,name=name,identity=SimpleNamespace(display_name='Joey',type=IdentityType.KNOWN))
            self.assertEqual(alert_display_name(alert),name)

    def test_unknown_status_comes_from_type_not_display_name(self):
        alert = SimpleNamespace(auto_name=True,name='original',identity=SimpleNamespace(display_name='Visitor',type=IdentityType.UNKNOWN))
        self.assertEqual(alert_display_name(alert),'Track Visitor')
        self.assertEqual(alert_identity_type(alert),'unknown')
        alert.identity.display_name=None
        self.assertEqual(alert_display_name(alert),'Track Unknown Person')
        alert.identity=None
        self.assertIsNone(alert_identity_type(alert))
