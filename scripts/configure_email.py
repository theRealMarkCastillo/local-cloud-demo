"""Apply local Mailpit settings to an existing realm without resetting its users."""
import json
from keycloak_admin import admin

if __name__ == '__main__':
    # Explicit local-only target. Cloud IdP SMTP is managed separately.
    smtp = {'host':'mailpit','port':'1025','from':'workbench@example.test',
            'fromDisplayName':'Local Workbench','ssl':'false','starttls':'false','auth':'false'}
    with admin() as run:
        run('update', 'realms/workbench', '-s', 'smtpServer=' + json.dumps(smtp),
            '-s', 'resetPasswordAllowed=true')
    print('Configured local Keycloak account emails through Mailpit; users preserved')
