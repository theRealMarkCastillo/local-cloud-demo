"""Local-only synthetic Keycloak email verification/reset and application SMTP smoke."""
from pathlib import Path
from html.parser import HTMLParser
import http.cookiejar
import json
import re
import secrets
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
from keycloak_admin import admin

ROOT=Path(__file__).resolve().parents[1]
values=dict(line.split('=',1) for line in (ROOT/'.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
base=values.get('PUBLIC_URL','http://localhost:8080').rstrip('/')
host=urllib.parse.urlparse(base).hostname or ''
if host not in ('localhost','127.0.0.1') and not host.endswith('.orb.local'):
    raise SystemExit('Email smoke is restricted to localhost/OrbStack; use a disposable local stack for other configured origins')
mail='http://localhost:'+values.get('MAIL_PORT','18026')
tls=ssl.create_default_context()
if host.endswith('.orb.local'):
    cert=subprocess.run(['security','find-certificate','-a','-c','OrbStack','-p'],check=True,text=True,capture_output=True)
    tls.load_verify_locations(cadata=cert.stdout)
def browser():
    # Match browser localhost Secure-cookie behavior, only within this local-only smoke.
    policy=http.cookiejar.DefaultCookiePolicy(secure_protocols=('https','wss','http'))
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar(policy=policy)))
opener=browser()

class Form(HTMLParser):
    def __init__(self):super().__init__();self.action=None;self.fields={}
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='form' and self.action is None:self.action=attrs.get('action')
        if tag=='input' and attrs.get('name') and attrs.get('type') not in ('submit','button'):self.fields[attrs['name']]=attrs.get('value','')

def page(url):
    url=urllib.parse.urljoin(base,url)
    assert urllib.parse.urlparse(url).netloc==urllib.parse.urlparse(base).netloc,'Action URL escaped configured origin'
    with opener.open(url,timeout=20) as response:return response.read().decode()

def submit(html,**fields):
    form=Form();form.feed(html);assert form.action,'Expected Keycloak form'
    target=urllib.parse.urljoin(base,form.action)
    assert urllib.parse.urlparse(target).netloc==urllib.parse.urlparse(base).netloc
    form.fields.update(fields)
    with opener.open(target,data=urllib.parse.urlencode(form.fields).encode(),timeout=20) as response:return response.read().decode()

def confirm_action(html):
    # Keycloak asks a fresh browser context to confirm a token-bearing email action.
    from html import unescape
    for target,label in re.findall(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',html,re.S):
        if 'Click here to proceed' in re.sub('<[^>]*>','',label):
            return page(urllib.parse.urljoin(base,unescape(target)))
    return html

def messages():
    with urllib.request.urlopen(mail+'/api/v1/messages',timeout=10) as response:return json.load(response)['messages']

def email_link(recipient,seen):
    for _ in range(50):
        for summary in messages():
            if summary['ID'] not in seen and any(to['Address']==recipient for to in summary['To']):
                with urllib.request.urlopen(mail+'/api/v1/message/'+summary['ID'],timeout=10) as response:message=json.load(response)
                links=re.findall(r'href="([^" ]+)"',message['HTML'])
                from html import unescape
                for link in links:
                    link=unescape(link)
                    if '/login-actions/' in link:
                        assert link.startswith(base+'/identity/'),'Email link uses wrong origin/prefix'
                        return link
        time.sleep(.2)
    raise AssertionError('Expected captured account email')

username='email-check-'+secrets.token_hex(6)
recipient=username+'@example.test'
password=secrets.token_urlsafe(24)+'aA1!'
uid=None
with admin() as run:
    try:
        run('create','users','-r','workbench','-s','username='+username,'-s','email='+recipient,'-s','enabled=true','-s','firstName=Email','-s','lastName=Check',stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        users=json.loads(run('get','users','-r','workbench','-q','username='+username,'--fields','id',capture_output=True).stdout)
        assert len(users)==1;uid=users[0]['id']
        seen={message['ID'] for message in messages()}
        # Send the normal Keycloak verification action without modifying existing demo users.
        run('update',f'users/{uid}/send-verify-email','-r','workbench',stdout=subprocess.DEVNULL)
        html=confirm_action(page(email_link(recipient,seen)))
        current=json.loads(run('get','users/'+uid,'-r','workbench',capture_output=True).stdout)
        assert current['emailVerified'],'Email confirmation did not verify synthetic user'
        print('PASS: Keycloak verification email captured; action link verifies synthetic account')
        seen={message['ID'] for message in messages()}
        opener=browser()
        login_html=page(base+'/auth/login')
        from html import unescape
        reset=re.search(r'href="([^" ]*login-actions/reset-credentials[^" ]*)"',login_html)
        assert reset,'Forgot-password link missing'
        reset_html=page(unescape(reset[1]));submit(reset_html,username=recipient)
        html=confirm_action(page(email_link(recipient,seen)))
        html=submit(html,**{'password-new':password,'password-confirm':password})
        # Fresh app OIDC login with the reset password proves the change completed.
        opener=browser()
        html=page(base+'/auth/login');submit(html,username=username,password=password)
        with opener.open(base+'/api/me',timeout=20) as response:assert json.load(response)['user']['id']
        print('PASS: forgot-password email captured; reset link changes password and new OIDC login succeeds')
        before={message['ID'] for message in messages()}
        subprocess.run(['docker','compose','exec','-T','api','python','-m','app.email'],cwd=ROOT,check=True,stdout=subprocess.DEVNULL)
        for _ in range(20):
            if any(message['ID'] not in before and message['Subject']=='Workbench application email' for message in messages()):break
            time.sleep(.2)
        else:raise AssertionError('Application email not captured')
        print('PASS: application SMTP email captured; no external delivery')
    finally:
        if uid:run('delete','users/'+uid,'-r','workbench',stdout=subprocess.DEVNULL)
        print('Removed only the synthetic Keycloak email-test user; captured messages retained locally')
