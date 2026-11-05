let csrf = '';
let active = false;
const notice = document.querySelector('#notice');
async function api(path, options = {}) {
  const response = await fetch(path, {credentials: 'same-origin', ...options,
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf, ...options.headers}});
  if (response.status === 401) { active = false; showLogin(); throw new Error('Please sign in.'); }
  if (!response.ok) throw new Error(`Request failed (${response.status}). Check service logs.`);
  return response.json();
}
function showLogin() {
  document.querySelector('#signed-out').hidden = false;
  document.querySelector('#signed-in').hidden = true;
}
async function refresh() {
  if (!active) return;
  try {
    const {jobs} = await api('/api/jobs');
    const target = document.querySelector('#jobs'); target.replaceChildren();
    if (!jobs.length) target.textContent = 'No jobs yet. Send your first sample to the worker.';
    for (const job of jobs) {
      const row = document.createElement('div'); row.className = 'job';
      const title = document.createElement('strong'); title.textContent = job.title;
      const badge = document.createElement('span'); badge.className = `badge ${job.status}`; badge.textContent = job.status;
      row.append(title, badge);
      if (job.status === 'complete') {
        const link = document.createElement('a'); link.href = `/api/jobs/${job.id}/report`; link.textContent = 'Download report ↗'; row.append(link);
      }
      target.append(row);
    }
    notice.textContent = '';
  } catch (error) { notice.textContent = error.message; }
}
document.querySelector('#job-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = document.querySelector('#submit'); button.disabled = true;
  try {
    await api('/api/jobs', {method: 'POST', body: JSON.stringify({title: document.querySelector('#title').value, text: document.querySelector('#text').value})});
    await refresh();
  } catch (error) { notice.textContent = error.message; }
  finally { button.disabled = false; }
});
document.querySelector('#logout').addEventListener('click', async () => {
  try { await api('/auth/logout', {method: 'POST'}); active = false; csrf = ''; showLogin(); }
  catch (error) { notice.textContent = error.message; }
});
(async () => {
  try {
    const config = await api('/api/config');
    if (location.origin !== config.public_url) {
      showLogin();
      const link = document.querySelector('#signed-out a');
      link.href = config.public_url + '/auth/login';
      notice.textContent = `Sign in uses the configured address: ${config.public_url}`;
      return;
    }
    const data = await api('/api/me'); csrf = data.csrf; active = true;
    document.querySelector('#name').textContent = `Signed in as ${data.user.name}`;
    document.querySelector('#signed-in').hidden = false;
    await refresh();
  } catch (error) { showLogin(); if (error.message !== 'Please sign in.') notice.textContent = error.message; }
  setInterval(refresh, 2000);
})();
