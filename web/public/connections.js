let csrf = '';
const notice = document.querySelector('#notice');
const NAMES = {'upstream-demo': 'Upstream demo (Jira stand-in)'};
const RESULTS = {success: 'Account connected.', cancelled: 'Connecting was cancelled.', error: 'Connecting failed. Try again.'};
async function api(path, options = {}) {
  const response = await fetch(path, {credentials: 'same-origin', ...options,
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf, ...options.headers}});
  if (response.status === 401) { showLogin(); throw new Error('Please sign in.'); }
  if (!response.ok) throw new Error(`Request failed (${response.status}). Check service logs.`);
  return response.json();
}
function showLogin() {
  document.querySelector('#signed-out').hidden = false;
  document.querySelector('#connections').hidden = true;
}
async function load() {
  const {connections} = await api('/api/connections');
  const target = document.querySelector('#list'); target.replaceChildren();
  for (const item of connections) {
    const row = document.createElement('div'); row.className = 'connection';
    const label = document.createElement('div');
    const name = document.createElement('strong'); name.textContent = NAMES[item.alias] || item.alias;
    const badge = document.createElement('span'); badge.className = `badge ${item.connected ? 'complete' : ''}`;
    badge.textContent = item.connected ? 'connected' : 'not connected';
    label.append(name, ' ', badge);
    const button = document.createElement('button');
    button.textContent = item.connected ? 'Disconnect' : 'Connect →';
    if (item.connected) button.className = 'secondary';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        if (item.connected) {
          await api(`/api/connections/${item.alias}`, {method: 'DELETE'});
          notice.textContent = 'Account disconnected.';
          await load();
        } else {
          const {redirect} = await api(`/api/connections/${item.alias}/connect`, {method: 'POST'});
          if (new URL(redirect).origin !== location.origin) throw new Error('Unexpected redirect.');
          location.assign(redirect);
        }
      } catch (error) { notice.textContent = error.message; button.disabled = false; }
    });
    row.append(label, button); target.append(row);
  }
  document.querySelector('#connections').hidden = false;
}
(async () => {
  const status = new URLSearchParams(location.search).get('status');
  if (RESULTS[status]) notice.textContent = RESULTS[status];
  try {
    const data = await api('/api/me'); csrf = data.csrf;
    await load();
  } catch (error) { if (error.message !== 'Please sign in.') notice.textContent = error.message; }
})();
