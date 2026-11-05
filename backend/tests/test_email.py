"""SMTP security-mode and recipient handling regressions; no external delivery."""
from unittest import TestCase
from unittest.mock import patch, MagicMock
from app import email


class EmailTests(TestCase):
    def test_starttls_precedes_credentials_and_delivery(self):
        client=MagicMock();client.__enter__.return_value=client
        with patch.multiple(email.s, SMTP_HOST='smtp.example.test', SMTP_PORT=587,
                            SMTP_SECURITY='starttls', SMTP_USERNAME='synthetic', SMTP_PASSWORD='synthetic'):
            with patch.object(email.smtplib,'SMTP',return_value=client):
                email.send_email('recipient@example.test','Synthetic subject','Synthetic body')
        self.assertEqual([call[0] for call in client.method_calls],['starttls','login','send_message'])
        self.assertEqual(client.send_message.call_args.kwargs['to_addrs'],['recipient@example.test'])
    def test_tls_failure_never_sends_credentials_or_message(self):
        client=MagicMock();client.__enter__.return_value=client
        client.starttls.side_effect=RuntimeError('Synthetic TLS refusal')
        with patch.multiple(email.s,SMTP_HOST='smtp.example.test',SMTP_PORT=587,SMTP_SECURITY='starttls'):
            with patch.object(email.smtplib,'SMTP',return_value=client),self.assertRaises(RuntimeError):
                email.send_email('recipient@example.test','Test','Body')
        client.login.assert_not_called();client.send_message.assert_not_called()
    def test_implicit_tls_uses_verified_ssl_client(self):
        client=MagicMock();client.__enter__.return_value=client
        with patch.multiple(email.s,SMTP_HOST='smtp.example.test',SMTP_PORT=465,SMTP_SECURITY='tls',SMTP_USERNAME='',SMTP_PASSWORD=''):
            with patch.object(email.smtplib,'SMTP_SSL',return_value=client) as connect:
                email.send_email('recipient@example.test','Test','Body')
        self.assertIn('context',connect.call_args.kwargs)
        client.starttls.assert_not_called();client.login.assert_not_called()
    def test_invalid_recipients_fail_before_connecting(self):
        with patch.object(email.smtplib,'SMTP') as connect:
            for value in ('a@example.test,b@example.test','a@example.test\r\nBcc: b@example.test','invalid'):
                with self.subTest(value=value),self.assertRaises(ValueError):
                    email.send_email(value,'Test','Body')
        connect.assert_not_called()
